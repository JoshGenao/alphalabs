#!/usr/bin/env python3
"""Least-privilege strategy-container evidence for SRS-SEC-003 (NFR-S5 / CIS Docker Benchmark).

Proves, by static inspection of ``docker-compose.yml`` (no Docker daemon, no live
container), that the strategy-container template runs with least-privilege
permissions — the three SRS-SEC-003 acceptance clauses:

* **No privileged mode.** The strategy service declares ``privileged: false`` and
  never ``privileged: true``; it drops ALL Linux capabilities, adds none of the
  dangerous ones back, and sets ``no-new-privileges:true`` so nothing can escalate
  at exec time.
* **No host network access.** The strategy service declares no ``network_mode:
  host`` (nor ``service:`` / ``container:`` namespace sharing), no ``pid: host`` and
  no ``ipc: host`` / ``ipc: shareable`` — it joins only the default isolated Compose
  project bridge, reaching the data / execution / simulation engines through the
  SYS-12 internal service interface. Asserted repo-wide as well: NO service in the
  stack uses host networking or privileged mode.
* **No access to other strategy filesystems.** The strategy service mounts no host
  Docker socket, no host-path bind, and no ``volumes_from`` importing another
  container's mounts; the shared SSD/NAS data tiers are mounted READ-ONLY and the
  credential vault is not mounted at all — so a strategy can neither write into a
  tier a sibling reads nor reach another container's filesystem. Container-per-
  strategy already gives each instance its own writable root layer.

The concrete Docker-backed ``StrategyContainerRuntime`` is deferred (owner:
SRS-ARCH-004 + SRS-ORCH-002); until it lands, ``docker-compose.yml`` is the
declarative template the orchestrator clones per strategy, so this static template
inspection is the authoritative least-privilege evidence — the same convention
SRS-ARCH-004 / SRS-SEC-004 are verified under. A gated ``tests/integration`` test
additionally runs ``docker inspect`` on a real strategy container when
``ATP_RUN_INTEGRATION=1``.

Forbidden-token scans run against the *comment-stripped* compose text: directive
tokens such as ``network_mode: host`` legitimately appear in explanatory ``#``
comments, and only the effective configuration must be judged.

Mirrors the PASS/FAIL output style of ``tools/deployment_check.py`` and reuses its
service-block parser.

Invoke:
    python3 tools/container_isolation_check.py                       # exit 0 PASS, 1 FAIL
    python3 tools/container_isolation_check.py --fixture host-network  # must FAIL
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from deployment_check import _service_block, _service_has_vault_mount

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class ContainerIsolationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ContainerIsolationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def isolation_contract(config: dict) -> dict:
    if "container_isolation_contract" not in config:
        fail("architecture metadata is missing the container_isolation_contract block")
    return config["container_isolation_contract"]


# --------------------------------------------------------------------------- #
# compose text helpers (text-level; no YAML dependency, no docker)
# --------------------------------------------------------------------------- #


def _strip_comments(text: str) -> str:
    """Return the compose text with YAML comments removed.

    Directive tokens (``network_mode: host`` etc.) legitimately appear inside
    explanatory ``#`` comments; forbidden-token scans must run against the
    *effective* configuration only, never the prose. Splits each line at its first
    ``#`` — safe here because no compose directive value in this file contains a
    literal ``#``.
    """

    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _yaml_list_items(block: str, key: str) -> list[str] | None:
    """Return the block-list items under ``key:`` in a compose block, or None.

    ``key:`` on its own line, followed by ``- item`` lines at deeper indent. Stops
    at the first non-blank line indented no deeper than the key (the next sibling
    directive). Returns None when the key is absent so callers can distinguish
    "no such directive" from "an empty list".
    """

    items: list[str] = []
    key_indent: int | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if key_indent is None:
            if line.strip() == f"{key}:":
                key_indent = indent
            continue
        if indent <= key_indent:
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().strip('"').strip("'"))
    return items if key_indent is not None else None


# --------------------------------------------------------------------------- #
# per-service least-privilege assertions
# --------------------------------------------------------------------------- #


def _assert_service_least_privilege(
    contract: dict, compose_text: str, service: str
) -> list[str]:
    raw = _service_block(compose_text, service)
    if raw is None:
        fail(f"compose is missing the strategy-container service {service!r} (SRS-SEC-003)")
    eff = _strip_comments(raw)

    # (1) No privileged mode + no privilege escalation at exec time.
    for directive in contract["required_directives"]:
        if directive not in eff:
            fail(f"{service} must declare {directive!r} (SRS-SEC-003 least-privilege)")

    # (2) Drop the ALL capability set; add none of the dangerous ones back.
    required_drop = contract["required_dropped_capability"]
    dropped = _yaml_list_items(eff, "cap_drop")
    if not dropped or required_drop not in {cap.upper() for cap in dropped}:
        fail(
            f"{service} must drop the {required_drop!r} capability set via cap_drop "
            f"(SRS-SEC-003 least-privilege)"
        )
    added = _yaml_list_items(eff, "cap_add") or []
    bad_added = sorted(
        {cap.upper() for cap in added} & set(contract["forbidden_added_capabilities"])
    )
    if bad_added:
        fail(
            f"{service} adds forbidden capabilities via cap_add: {', '.join(bad_added)} "
            f"(SRS-SEC-003 least-privilege)"
        )

    # (3) No host network access / namespace sharing on the service.
    for token in contract["forbidden_host_namespace_directives"]:
        if token in eff:
            fail(
                f"{service} must not declare {token!r} "
                f"(SRS-SEC-003 no host network / namespace sharing)"
            )

    # (4) Filesystem isolation: no host-path binds, no docker socket, no
    #     volumes_from, no credential vault; the data tiers are read-only.
    for token in contract["forbidden_mount_tokens"]:
        if token in eff:
            fail(f"{service} must not reference {token!r} (SRS-SEC-003 filesystem isolation)")
    if _service_has_vault_mount(eff):
        fail(f"{service} must not mount the credential vault (SRS-SEC-003 / SRS-SEC-004)")

    volumes = _yaml_list_items(eff, "volumes") or []
    if not volumes:
        fail(
            f"{service} declares no volumes (compose drift?) — expected only the "
            f"read-only named data tiers"
        )
    for item in volumes:
        source = item.split(":", 1)[0].strip()
        if source[:1] in ("/", ".", "~"):
            fail(
                f"{service} mounts a host path {item!r}; strategy containers may mount only "
                f"the read-only named data tiers (SRS-SEC-003 filesystem isolation)"
            )
        if (":/ssd" in item or ":/nas" in item) and not item.endswith(":ro"):
            fail(
                f"{service} mounts data tier {item!r} read-write; SRS-SEC-003 requires the "
                f"shared tiers be read-only so a strategy cannot write into a tier a sibling reads"
            )
    for sanctioned in contract["sanctioned_readonly_data_volumes"]:
        if sanctioned not in eff:
            fail(f"{service} must mount the read-only data tier {sanctioned!r} (SRS-SEC-003)")

    return [
        f"{service}: no privileged mode (privileged:false + no-new-privileges), all Linux "
        f"capabilities dropped, no host network / namespace sharing, and only read-only named "
        f"data tiers (no host bind, no docker socket, no vault, no volumes_from)",
    ]


def _assert_repo_wide_no_host_namespace(contract: dict, effective_all: str) -> list[str]:
    for token in (
        "network_mode: host",
        'network_mode: "host"',
        "network_mode: 'host'",
    ):
        if token in effective_all:
            fail(f"no compose service may use host networking, found {token!r} (SRS-SEC-003 NFR-S5)")
    if "privileged: true" in effective_all:
        fail("no compose service may run privileged (found 'privileged: true') (SRS-SEC-003 NFR-S5)")
    return ["repo-wide: no compose service uses host networking or privileged mode"]


def _assert_security_doc(contract: dict, root: Path) -> list[str]:
    doc_rel = contract["security_doc"]
    doc_path = root / doc_rel
    if not doc_path.exists():
        fail(f"security doc is missing: {doc_rel}")
    text = doc_path.read_text(encoding="utf-8")
    marker = contract["security_doc_marker"]
    if marker not in text:
        fail(f"{doc_rel} does not document {marker} (least-privilege strategy containers)")
    low = text.lower()
    if "least-privilege" not in low and "least privilege" not in low:
        fail(f"{doc_rel} does not describe least-privilege strategy containers (SRS-SEC-003)")
    return [f"{doc_rel} documents {marker} least-privilege strategy containers"]


def assert_container_isolation_static(config: dict, root: Path = ROOT) -> list[str]:
    contract = isolation_contract(config)
    compose_path = root / contract["compose_file"]
    if not compose_path.exists():
        fail(f"compose file is missing: {contract['compose_file']}")
    compose_text = compose_path.read_text(encoding="utf-8")
    effective_all = _strip_comments(compose_text)

    subject_services = contract["subject_services"]
    if not subject_services:
        fail("container_isolation_contract names no subject_services (SRS-SEC-003 would be vacuous)")

    evidence: list[str] = ["SRS-SEC-003 least-privilege strategy-container evidence:"]
    for service in subject_services:
        evidence.extend(_assert_service_least_privilege(contract, compose_text, service))
    evidence.extend(_assert_repo_wide_no_host_namespace(contract, effective_all))
    evidence.extend(_assert_security_doc(contract, root))
    return evidence


# --------------------------------------------------------------------------- #
# negative self-test fixtures (prove the check FAILS on a violation)
# --------------------------------------------------------------------------- #

_FIXTURES = ("allow-privileged", "host-network", "writable-data-tier", "docker-socket", "no-cap-drop")


def make_fixture_root(fixture: str) -> tempfile.TemporaryDirectory[str]:
    temp_dir = tempfile.TemporaryDirectory()
    temp_root = Path(temp_dir.name)
    (temp_root / "architecture").mkdir()

    shutil.copy2(CONFIG_PATH, temp_root / "architecture" / "runtime_services.json")
    shutil.copy2(ROOT / "docker-compose.yml", temp_root / "docker-compose.yml")
    shutil.copy2(ROOT / "SECURITY.md", temp_root / "SECURITY.md")

    compose_path = temp_root / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    if fixture == "allow-privileged":
        text = text.replace("privileged: false", "privileged: true")
    elif fixture == "host-network":
        text = text.replace(
            "    privileged: false\n",
            "    privileged: false\n    network_mode: host\n",
        )
    elif fixture == "writable-data-tier":
        text = text.replace("- atp_ssd:/ssd:ro", "- atp_ssd:/ssd")
    elif fixture == "docker-socket":
        text = text.replace(
            "      - atp_ssd:/ssd:ro\n",
            "      - atp_ssd:/ssd:ro\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n",
        )
    elif fixture == "no-cap-drop":
        text = text.replace("    cap_drop:\n      - ALL\n", "")
    else:
        temp_dir.cleanup()
        raise ValueError(f"unknown fixture: {fixture}")
    compose_path.write_text(text, encoding="utf-8")

    return temp_dir


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        choices=list(_FIXTURES),
        help="Run the check against a temporary workspace containing a known violation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    root = ROOT
    if args.fixture:
        temp_dir = make_fixture_root(args.fixture)
        root = Path(temp_dir.name)

    try:
        config = load_config(root)
        evidence = assert_container_isolation_static(config, root)
    except ContainerIsolationCheckError as error:
        print(f"SRS-SEC-003 FAIL: {error}", file=sys.stderr)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print("SRS-SEC-003 PASS — least-privilege strategy containers")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
