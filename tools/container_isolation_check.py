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

# Security-relevant service keys that must appear at most once on the strategy
# service — a duplicate silently overrides the hardened value (Docker last-wins).
_SINGLETON_SECURITY_KEYS = (
    "privileged",
    "network_mode",
    "pid",
    "ipc",
    "user",
    "userns_mode",
    "cap_drop",
    "cap_add",
    "security_opt",
    "networks",
    "volumes",
)


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


def _flow_items(inline: str) -> list[str]:
    """Split a YAML flow sequence ``[a, b, c]`` into its unquoted items."""

    inner = inline.strip()
    if inner.startswith("["):
        inner = inner[1:]
    if inner.endswith("]"):
        inner = inner[:-1]
    return [part.strip().strip('"').strip("'") for part in inner.split(",") if part.strip()]


def _yaml_list_items(block: str, key: str) -> list[str] | None:
    """Return the items under ``key:`` in a compose block, or None if absent.

    Handles BOTH YAML list spellings so a flow list cannot hide a value from the
    security checks: block (``key:`` then ``- item`` lines at deeper indent) and
    flow (``key: [a, b, c]`` inline). Returns None when the key is absent so callers
    can distinguish "no such directive" from "an empty list".
    """

    items: list[str] = []
    key_indent: int | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if key_indent is None:
            stripped = line.strip()
            if stripped == f"{key}:":
                key_indent = indent
            elif stripped.startswith(f"{key}:"):
                inline = stripped[len(key) + 1 :].strip()
                if inline.startswith("["):
                    return _flow_items(inline)
                return []  # an inline scalar — not a list
            continue
        if indent <= key_indent:
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip().strip('"').strip("'"))
    return items if key_indent is not None else None


_TRUE_SCALARS = frozenset({"true", "yes", "on", "1"})
_FALSE_SCALARS = frozenset({"false", "no", "off", "0"})


def _scalar_bool(value: str | None) -> bool | None:
    """Normalize a YAML scalar to a bool: handles true/"true"/yes/on and negatives."""

    if value is None:
        return None
    token = value.strip().strip('"').strip("'").lower()
    if token in _TRUE_SCALARS:
        return True
    if token in _FALSE_SCALARS:
        return False
    return None


def _child_indent(block: str) -> int:
    indents = [len(ln) - len(ln.lstrip()) for ln in block.splitlines() if ln.strip()]
    return min(indents) if indents else 0


def _service_key_count(block: str, key: str) -> int:
    """Count ``key:`` occurrences at the service's direct-child indent."""

    child = _child_indent(block)
    count = 0
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == child and line.strip().startswith(f"{key}:"):
            count += 1
    return count


def _service_scalar(block: str, key: str) -> str | None:
    """Return the unquoted scalar value of ``key:`` at the service-child indent."""

    child = _child_indent(block)
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == child and line.strip().startswith(f"{key}:"):
            return line.strip()[len(key) + 1 :].strip().strip('"').strip("'")
    return None


def _yaml_list_blocks(block: str, key: str) -> list[str] | None:
    """Return each block-list item under ``key:`` as its full raw text.

    Unlike ``_yaml_list_items`` (which keeps only the ``- `` line), this preserves
    a list item's nested continuation lines — needed to see the ``source:`` /
    ``target:`` of a Compose **long-syntax** volume entry. An item begins at a
    ``- `` line at the list-item indent; deeper-indented lines belong to it.
    Returns None when ``key:`` is absent.
    """

    key_indent: int | None = None
    item_indent: int | None = None
    entries: list[list[str]] = []
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
        if line.lstrip().startswith("- ") and (item_indent is None or indent <= item_indent):
            item_indent = indent
            entries.append([line])
        elif entries:
            entries[-1].append(line)
    return ["\n".join(item) for item in entries] if key_indent is not None else None


def _parse_volume_entry(entry: str) -> dict:
    """Normalize one Compose volume entry (short, long, or flow) to a common shape.

    Returns ``{"raw", "kind", "source", "target", "read_only"}`` covering all three
    Compose spellings so a long-/flow-syntax bind mount cannot slip past the
    filesystem checks:

    * short   ``- source:target[:ro]``
    * long    ``- type: bind`` / ``source:`` / ``target:`` / ``read_only:``
    * flow    ``- {type: bind, source: /h, target: /t, read_only: true}``

    ``kind`` is the mount ``type`` when stated ("bind" / "volume" / "tmpfs"),
    else "" (short named-volume form).
    """

    out = {"raw": " ".join(entry.split()), "kind": "", "source": "", "target": "", "read_only": False}
    lines = entry.splitlines()
    first = lines[0].strip()
    if first.startswith("- "):
        first = first[2:].strip()

    def _apply(mapping: dict) -> None:
        out["kind"] = mapping.get("type", "").strip().strip('"').strip("'")
        out["source"] = mapping.get("source", "").strip().strip('"').strip("'")
        out["target"] = mapping.get("target", "").strip().strip('"').strip("'")
        out["read_only"] = mapping.get("read_only", "").strip().lower() in ("true", "yes", "on")

    # Flow mapping: ``{type: bind, source: /h, target: /t}``.
    if first.startswith("{"):
        mapping: dict[str, str] = {}
        for pair in first.strip("{}").split(","):
            if ":" in pair:
                key, value = pair.split(":", 1)
                mapping[key.strip()] = value
        _apply(mapping)
        return out

    # Long (block) mapping: gather ``key: value`` pairs from the inline first key
    # plus every nested line.
    mapping = {}
    candidates = [first] + [ln.strip() for ln in lines[1:]]
    for candidate in candidates:
        parts = candidate.split(":", 1)
        if len(parts) == 2 and parts[0].strip() in {
            "type",
            "source",
            "target",
            "read_only",
        }:
            mapping[parts[0].strip()] = parts[1]
    if mapping:
        _apply(mapping)
        return out

    # Short scalar: ``source:target[:mode]``.
    scalar = first.strip('"').strip("'")
    segments = scalar.split(":")
    if len(segments) >= 2:
        out["source"] = segments[0]
        out["target"] = segments[1]
        out["read_only"] = "ro" in segments[2:]
        out["kind"] = "bind" if out["source"][:1] in ("/", ".", "~") else "volume"
    else:
        out["source"] = scalar
    return out


def _volume_mounts(block: str) -> list[dict] | None:
    # Flow list — ``volumes: [atp_ssd:/ssd:ro, ...]`` — must be parsed too, so a
    # mount cannot hide from the allow-list behind inline syntax.
    child = _child_indent(block)
    for line in block.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == child and line.strip().startswith("volumes:"):
            inline = line.strip()[len("volumes:") :].strip()
            if inline.startswith("["):
                return [_parse_volume_entry(f"- {item}") for item in _flow_items(inline)]
            break
    blocks = _yaml_list_blocks(block, "volumes")
    if blocks is None:
        return None
    return [_parse_volume_entry(entry) for entry in blocks]


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

    # (0a) Fail closed on Compose constructs this static check cannot resolve but
    #      Docker WOULD apply, on the strategy's security surface:
    #        * a service-level `<<:` merge could inject `cap_add` / `pid: host` /
    #          etc. from an anchor (invisible to a direct-line read);
    #        * a YAML alias (`*anchor`) or `${VAR}` interpolation on a security key
    #          resolves to a value only known later.
    #      The template's sole merge is `environment: <<: [*...]` — nested under
    #      `environment` (neither a service-level key nor a security key), so it is
    #      unaffected. This mirrors SEC-002's fail-closed-on-interpolation stance.
    child_indent = _child_indent(eff)
    for line in eff.splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == child_indent and stripped.startswith("<<:"):
            fail(
                f"{service} uses a service-level `<<:` merge; SRS-SEC-003 refuses a merge that "
                f"could inject an unchecked privilege / namespace setting from an anchor"
            )
        if indent == child_indent:
            for key in _SINGLETON_SECURITY_KEYS:
                if stripped.startswith(f"{key}:"):
                    value = stripped[len(key) + 1 :].strip()
                    if "*" in value or "$" in value:
                        fail(
                            f"{service} sets `{key}:` to a YAML alias / interpolation ({value!r}); "
                            f"SRS-SEC-003 refuses a security value it cannot resolve statically"
                        )

    # (0b) Reject a DUPLICATE security key: a second `privileged:` / `network_mode:`
    #      / `cap_add:` etc. silently overrides the hardened value (Docker takes the
    #      last), so a duplicate is a bypass and is refused outright.
    for key in _SINGLETON_SECURITY_KEYS:
        count = _service_key_count(eff, key)
        if count > 1:
            fail(
                f"{service} declares `{key}:` {count} times; duplicate security keys are refused "
                f"— a later value could silently override the hardened one (SRS-SEC-003)"
            )

    # (1) No privileged mode + no privilege escalation at exec time. `privileged`
    #     is normalized as a bool, so true / "true" / yes / on are all rejected.
    privileged = _service_scalar(eff, "privileged")
    if _scalar_bool(privileged) is not False:
        fail(
            f"{service} must declare `privileged: false` (got {privileged!r}); "
            f"SRS-SEC-003 forbids privileged mode"
        )
    security_opt = [opt.replace(" ", "").lower() for opt in (_yaml_list_items(eff, "security_opt") or [])]
    if "no-new-privileges:true" not in security_opt:
        fail(
            f"{service} must set `security_opt: [no-new-privileges:true]` "
            f"(SRS-SEC-003 no privilege escalation)"
        )

    # (2) Drop the ALL capability set; add NONE back. Both block and flow
    #     (`cap_add: [CHOWN]`) list forms are parsed, and ANY non-empty cap_add is
    #     rejected — a strategy container regains no kernel capability (the docs and
    #     compose comments state nothing is added back), so even a "benign" cap
    #     (CHOWN / SETUID / …) is refused, not just a hazardous deny-list.
    required_drop = contract["required_dropped_capability"]
    dropped = _yaml_list_items(eff, "cap_drop")
    if not dropped or required_drop not in {cap.upper() for cap in dropped}:
        fail(
            f"{service} must drop the {required_drop!r} capability set via cap_drop "
            f"(SRS-SEC-003 least-privilege)"
        )
    added = _yaml_list_items(eff, "cap_add") or []
    if added:
        fail(
            f"{service} adds capabilities via cap_add: {', '.join(added)}; SRS-SEC-003 strategy "
            f"containers drop ALL capabilities and add NONE back (least-privilege)"
        )

    # (3) No host network access / namespace sharing on the service. Scalars are
    #     compared after unquoting, so `network_mode: "host"` is caught too.
    network_mode = _service_scalar(eff, "network_mode")
    if network_mode is not None and (
        network_mode == "host"
        or network_mode.startswith("service:")
        or network_mode.startswith("container:")
    ):
        fail(
            f"{service} must not declare `network_mode: {network_mode}` "
            f"(SRS-SEC-003 no host network / namespace sharing)"
        )
    if _service_scalar(eff, "pid") == "host":
        fail(f"{service} must not share the host PID namespace (SRS-SEC-003)")
    if _service_scalar(eff, "ipc") in ("host", "shareable"):
        fail(f"{service} must not share the host IPC namespace (SRS-SEC-003)")

    # (3b) No host network EGRESS: the strategy is confined to a dedicated,
    #      `internal: true` network. A container with no explicit network joins
    #      the default Compose bridge, which routes outbound to the host / LAN /
    #      internet — so "no `network_mode: host`" alone is NOT no-host-network
    #      access. Requiring an internal (no-gateway) network removes all egress.
    if contract.get("require_internal_network"):
        networks = _yaml_list_items(eff, "networks")
        if not networks:
            fail(
                f"{service} declares no networks, so it joins the default Compose bridge "
                f"(host/LAN/internet egress); SRS-SEC-003 requires a dedicated internal network"
            )
        for net in networks:
            net_block = _service_block(compose_text, net)
            if net_block is None:
                fail(
                    f"{service} is attached to network {net!r}, which is not declared at the top "
                    f"level; SRS-SEC-003 requires an internal, no-egress network"
                )
            # Read the network's DIRECT-child `internal:` scalar — not a substring
            # of the whole block, so a nested `labels: {internal: true}` under an
            # `internal: false` network cannot masquerade as internal.
            internal = _service_scalar(_strip_comments(net_block), "internal")
            if _scalar_bool(internal) is not True:
                fail(
                    f"{service} is attached to network {net!r} whose `internal:` is {internal!r}, "
                    f"not true; SRS-SEC-003 requires a no-egress (internal) network"
                )

    # (4) Filesystem isolation: no host-path binds, no docker socket, no
    #     volumes_from, no credential vault; the data tiers are read-only.
    for token in contract["forbidden_mount_tokens"]:
        if token in eff:
            fail(f"{service} must not reference {token!r} (SRS-SEC-003 filesystem isolation)")
    if _service_has_vault_mount(eff):
        fail(f"{service} must not mount the credential vault (SRS-SEC-003 / SRS-SEC-004)")

    #     Volumes are normalized across BOTH Compose spellings (short + long +
    #     flow) so a long-syntax host bind cannot slip past the host-path check.
    mounts = _volume_mounts(eff)
    if not mounts:
        fail(
            f"{service} declares no volumes (compose drift?) — expected only the "
            f"read-only named data tiers"
        )
    # STRICT allow-list: a strategy container may mount ONLY the sanctioned
    # read-only data tiers. ANYTHING else — a host bind, a host-path source, or an
    # extra shared named volume — is rejected, because the orchestrator clones this
    # template for every strategy, so any extra shared mount becomes a cross-
    # strategy filesystem channel the SRS-SEC-003 acceptance clause forbids.
    allowed = {}
    for sanctioned in contract["sanctioned_readonly_data_volumes"]:
        parsed = _parse_volume_entry(f"- {sanctioned}")
        allowed[(parsed["source"], parsed["target"])] = parsed["read_only"]
    for mount in mounts:
        if mount["kind"] == "bind" or mount["source"][:1] in ("/", ".", "~", "{"):
            fail(
                f"{service} uses a host/bind mount ({mount['raw']!r}); strategy containers may "
                f"mount only the sanctioned read-only named data tiers (SRS-SEC-003 filesystem "
                f"isolation)"
            )
        if (mount["source"], mount["target"]) not in allowed:
            fail(
                f"{service} mounts {mount['raw']!r}, which is not a sanctioned read-only data "
                f"tier; a strategy container may share NO other filesystem with sibling "
                f"strategies (SRS-SEC-003 filesystem isolation)"
            )
        if not mount["read_only"]:
            fail(
                f"{service} mounts data tier {mount['raw']!r} read-write; SRS-SEC-003 requires the "
                f"shared tiers be read-only so a strategy cannot write into a tier a sibling reads"
            )
    present = {(m["source"], m["target"]) for m in mounts if m["read_only"]}
    for source, target in allowed:
        if (source, target) not in present:
            fail(
                f"{service} must mount the read-only data tier {source}:{target} (SRS-SEC-003)"
            )

    return [
        f"{service}: no privileged mode (privileged:false + no-new-privileges), all Linux "
        f"capabilities dropped, no host network / namespace sharing, confined to an internal "
        f"(no-egress) network, and only read-only named data tiers (no host bind, no docker "
        f"socket, no vault, no volumes_from)",
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

_FIXTURES = (
    "allow-privileged",
    "host-network",
    "writable-data-tier",
    "docker-socket",
    "no-cap-drop",
    "long-syntax-bind",
    "flow-syntax-bind",
    "default-bridge",
    "external-network",
    "extra-shared-volume",
    "inline-cap-add",
    "quoted-privileged",
    "duplicate-privileged",
    "cap-add-benign",
    "nested-internal-label",
    "service-level-merge",
    "aliased-security-value",
)


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
    elif fixture == "long-syntax-bind":
        # A Compose long-syntax bind of the host root — the exact bypass the
        # short-only parser missed. Must be rejected.
        text = text.replace(
            "    volumes:\n      - atp_ssd:/ssd:ro",
            "    volumes:\n      - type: bind\n        source: /\n        target: /host_root\n"
            "      - atp_ssd:/ssd:ro",
        )
    elif fixture == "flow-syntax-bind":
        text = text.replace(
            "    volumes:\n      - atp_ssd:/ssd:ro",
            "    volumes:\n      - {type: bind, source: /etc, target: /host_etc}\n"
            "      - atp_ssd:/ssd:ro",
        )
    elif fixture == "default-bridge":
        # Drop the internal-network attachment: the strategy falls back to the
        # default Compose bridge (host/LAN/internet egress). Must be rejected.
        text = text.replace("    networks:\n      - atp_strategy_net\n", "")
    elif fixture == "external-network":
        # The strategy network loses its no-egress guarantee. Must be rejected.
        text = text.replace("    internal: true", "    internal: false")
    elif fixture == "extra-shared-volume":
        # A valid extra shared named volume — mounted by every cloned strategy, so
        # a cross-strategy filesystem channel. Must be rejected (allow-list).
        text = text.replace(
            "      - atp_ssd:/ssd:ro\n",
            "      - atp_ssd:/ssd:ro\n      - strategy_shared:/shared\n",
        )
    elif fixture == "inline-cap-add":
        # A flow-list cap_add that the block-only parser used to miss. Rejected.
        text = text.replace(
            "    cap_drop:\n      - ALL\n",
            "    cap_drop:\n      - ALL\n    cap_add: [SYS_ADMIN]\n",
        )
    elif fixture == "quoted-privileged":
        # A quoted boolean the substring check used to miss. Rejected.
        text = text.replace("privileged: false", 'privileged: "true"')
    elif fixture == "duplicate-privileged":
        # A duplicate `privileged:` whose later value (Docker last-wins) re-enables
        # privileged mode. The duplicate is refused outright.
        text = text.replace(
            "    privileged: false\n",
            '    privileged: false\n    privileged: "true"\n',
        )
    elif fixture == "cap-add-benign":
        # Even a "benign"-looking capability re-added is rejected — the template
        # drops ALL and adds none back.
        text = text.replace(
            "    cap_drop:\n      - ALL\n",
            "    cap_drop:\n      - ALL\n    cap_add:\n      - CHOWN\n",
        )
    elif fixture == "nested-internal-label":
        # internal:false network whose nested label says internal:true — must not
        # masquerade as a no-egress network.
        text = text.replace(
            "    driver: bridge\n    internal: true",
            '    driver: bridge\n    internal: false\n    labels:\n      internal: "true"',
        )
    elif fixture == "service-level-merge":
        # A service-level `<<:` merge could inject security keys from an anchor.
        text = text.replace(
            "    privileged: false\n",
            "    privileged: false\n    <<: *atp-env\n",
        )
    elif fixture == "aliased-security-value":
        # A YAML alias on a security key hides its resolved value from the check.
        text = text.replace(
            "    cap_drop:\n      - ALL\n",
            "    cap_drop:\n      - ALL\n    cap_add: *atp-env\n",
        )
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
