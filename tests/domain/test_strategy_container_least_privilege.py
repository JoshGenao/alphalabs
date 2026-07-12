"""L7 domain/safety — SRS-SEC-003 least-privilege strategy containers.

Every user strategy runs in its own Docker container, cloned by the Strategy
Orchestrator from the ``phase1-strategy-runtime`` template in ``docker-compose.yml``.
SRS-SEC-003 (NFR-S5 / CIS Docker Benchmark) requires that template to run with
least-privilege permissions — **no privileged mode, no host network access, and no
access to other strategy containers' filesystems**.

The concrete Docker-backed ``StrategyContainerRuntime`` is deferred (owner:
SRS-ARCH-004 / SRS-ORCH-002), so the compose template is the authoritative
declarative source and this static inspection is the primary evidence — the same
convention SRS-ARCH-004 / SRS-SEC-004 are verified under. A companion gated L5 test
(``tests/integration/test_strategy_container_inspect.py``) runs ``docker inspect`` on
a real strategy container when ``ATP_RUN_INTEGRATION=1``.

These assertions are **structural invariants that never skip**: each proves the
hardened directive is present in the effective (comment-stripped) compose block, and
the parametrized rejection test proves the ``container_isolation_check`` fails closed
when any one clause is violated.

SRS trace: SRS-SEC-003 (NFR-S5 / StRS SN-1.10, SN-3.01).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# tools/ is on sys.path via tests/conftest.py.
import container_isolation_check as cic

pytestmark = [pytest.mark.domain, pytest.mark.safety]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
CHECK = ROOT / "tools" / "container_isolation_check.py"


@pytest.fixture()
def contract() -> dict:
    return cic.isolation_contract(cic.load_config())


@pytest.fixture()
def strategy_blocks(contract) -> list[str]:
    """The effective (comment-stripped) compose block for each subject service."""

    compose_text = COMPOSE.read_text("utf-8")
    blocks: list[str] = []
    for service in contract["subject_services"]:
        raw = cic._service_block(compose_text, service)
        assert raw is not None, f"compose is missing the strategy service {service!r}"
        blocks.append(cic._strip_comments(raw))
    assert blocks, "container_isolation_contract names no subject services"
    return blocks


def test_container_isolation_inspection_check_passes() -> None:
    """The full SRS-SEC-003 inspection evidence passes (raises on any violation)."""

    evidence = cic.assert_container_isolation_static(cic.load_config())
    assert evidence and all(isinstance(item, str) and item for item in evidence)


def test_no_privileged_mode(strategy_blocks) -> None:
    """AC clause 1 — no privileged mode; drop all caps; no privilege escalation."""

    for block in strategy_blocks:
        assert "privileged: false" in block
        assert "privileged: true" not in block
        assert "no-new-privileges:true" in block
        dropped = cic._yaml_list_items(block, "cap_drop")
        assert dropped is not None and "ALL" in {c.upper() for c in dropped}
        # No dangerous capability is added back.
        added = cic._yaml_list_items(block, "cap_add") or []
        assert not ({c.upper() for c in added} & {"ALL", "NET_ADMIN", "SYS_ADMIN"})


def test_no_host_network_access(strategy_blocks, contract) -> None:
    """AC clause 2 — no host network / namespace sharing on the strategy service."""

    for block in strategy_blocks:
        for token in contract["forbidden_host_namespace_directives"]:
            assert token not in block, f"strategy block must not contain {token!r}"


def test_repo_wide_no_host_networking() -> None:
    """No compose service anywhere uses host networking or privileged mode."""

    effective_all = cic._strip_comments(COMPOSE.read_text("utf-8"))
    assert "network_mode: host" not in effective_all
    assert 'network_mode: "host"' not in effective_all
    assert "network_mode: 'host'" not in effective_all
    assert "privileged: true" not in effective_all


def test_no_host_network_egress(strategy_blocks) -> None:
    """AC clause 2 (stronger) — the strategy is confined to an internal, no-egress network.

    Absence of ``network_mode: host`` alone still leaves a container on the default
    Compose bridge with host/LAN/internet egress; SRS-SEC-003 requires no host
    network access, so the strategy must join a dedicated ``internal: true`` network.
    """

    compose_text = COMPOSE.read_text("utf-8")
    for block in strategy_blocks:
        networks = cic._yaml_list_items(block, "networks")
        assert networks, "strategy service must attach to a dedicated internal network"
        for net in networks:
            net_block = cic._service_block(compose_text, net)
            assert net_block is not None, f"network {net!r} is not declared top-level"
            assert "internal: true" in cic._strip_comments(net_block), (
                f"network {net!r} must be internal: true (no egress)"
            )


def test_no_cross_strategy_filesystem_access(strategy_blocks, contract) -> None:
    """AC clause 3 — mounts are EXACTLY the sanctioned read-only tiers, nothing else.

    Strict allow-list: no credential vault, no docker socket, no host bind, and no
    extra shared named volume (which would be a cross-strategy channel once the
    template is cloned per strategy).
    """

    allowed = set()
    for sanctioned in contract["sanctioned_readonly_data_volumes"]:
        parsed = cic._parse_volume_entry(f"- {sanctioned}")
        allowed.add((parsed["source"], parsed["target"]))

    for block in strategy_blocks:
        assert not cic._service_has_vault_mount(block)
        for token in contract["forbidden_mount_tokens"]:
            assert token not in block, f"strategy block must not reference {token!r}"
        mounts = cic._volume_mounts(block) or []
        assert mounts, "strategy service must declare its read-only data-tier mounts"
        for mount in mounts:
            assert mount["kind"] != "bind", f"bind mount not allowed: {mount['raw']!r}"
            assert mount["source"][:1] not in ("/", ".", "~", "{"), (
                f"host-path bind not allowed: {mount['raw']!r}"
            )
            assert (mount["source"], mount["target"]) in allowed, (
                f"non-sanctioned mount not allowed: {mount['raw']!r}"
            )
            assert mount["read_only"], f"data tier must be read-only: {mount['raw']!r}"
        present = {(m["source"], m["target"]) for m in mounts if m["read_only"]}
        assert allowed <= present, "all sanctioned read-only data tiers must be mounted"


@pytest.mark.parametrize(
    "fixture",
    [
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
    ],
)
def test_check_rejects_each_violation(fixture: str) -> None:
    """The check fails closed (non-zero exit) on every seeded least-privilege violation.

    This is the load-bearing proof that the inspection is not vacuous: removing any
    single hardening directive makes ``container_isolation_check.py`` reject the stack.
    """

    result = subprocess.run(
        [sys.executable, str(CHECK), "--fixture", fixture],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"fixture {fixture!r} should be rejected but check passed"
    assert "SRS-SEC-003 FAIL" in result.stderr
