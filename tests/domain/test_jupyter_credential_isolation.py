"""L7 domain/safety — SRS-SEC-004 Jupyter isolation from live credentials + execution APIs.

The embedded Jupyter research environment (``phase1-jupyter`` in ``docker-compose.yml``)
must be isolated from live trading credentials and the execution APIs — SRS-SEC-004
(SyRS NFR-S6 / StRS SN-1.18):

* **read-only** access to market data and backtest results (via the data layer), and
* it **cannot submit live orders** and **cannot read brokerage credentials**.

Refined by NFR-S6 to: no write access to brokerage credentials, no direct access to
the execution engine, and no ability to submit live orders. The concrete
operator-supplied JupyterLab image and the dashboard->Jupyter proxy are deferred
(IF-13 / SRS-RES-001), so the compose template is the authoritative declarative source
and this static inspection is the primary SRS-SEC-004 "Security test" evidence — the
same convention SRS-ARCH-004 / SRS-SEC-003 are verified under. A companion gated L5 test
(``tests/integration/test_jupyter_isolation_inspect.py``) runs ``docker inspect`` on a
real ``phase1-jupyter`` container when ``ATP_RUN_INTEGRATION=1``.

These assertions are **structural invariants that never skip**: each proves the isolation
directive is present in the effective (comment-stripped) compose block, and the
parametrized rejection test proves ``jupyter_isolation_check`` fails closed when any one
clause is violated (non-vacuity).

SRS trace: SRS-SEC-004 (SyRS NFR-S6 / StRS SN-1.18).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# tools/ is on sys.path via tests/conftest.py.
import jupyter_isolation_check as jic
import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
CHECK = ROOT / "tools" / "jupyter_isolation_check.py"


@pytest.fixture()
def contract() -> dict:
    return jic.jupyter_isolation_contract(jic.load_config())


@pytest.fixture()
def compose_text() -> str:
    return COMPOSE.read_text("utf-8")


@pytest.fixture()
def jupyter_blocks(contract, compose_text) -> list[str]:
    """The effective (comment-stripped) compose block for each subject service."""

    blocks: list[str] = []
    for service in contract["subject_services"]:
        raw = jic._service_block(compose_text, service)
        assert raw is not None, f"compose is missing the Jupyter service {service!r}"
        blocks.append(jic._strip_comments(raw))
    assert blocks, "jupyter_isolation_contract names no subject services"
    return blocks


def test_jupyter_isolation_inspection_check_passes() -> None:
    """The full SRS-SEC-004 inspection evidence passes (raises on any violation)."""

    evidence = jic.assert_jupyter_isolation_static(jic.load_config())
    assert evidence and all(isinstance(item, str) and item for item in evidence)


def test_credentials_blanked_first(contract, compose_text, jupyter_blocks) -> None:
    """AC clause — cannot read brokerage credentials: the blanking anchor is merged
    FIRST (YAML merge is earlier-wins) and blanks every catalogued secret; no
    catalogued secret is re-set inline."""

    anchor_block = jic._anchor_block(compose_text, contract["no_secrets_anchor"])
    assert anchor_block is not None, "compose is missing the credential-blanking anchor"
    label = jic._anchor_label(anchor_block)
    assert label, "the blanking anchor defines no &label"
    eff_anchor = jic._strip_comments(anchor_block)
    from deployment_check import _SECRET_BLANK_KEYS

    for key in _SECRET_BLANK_KEYS:
        assert f'{key}: ""' in eff_anchor, f"blanking anchor must blank {key}"

    for block in jupyter_blocks:
        env_lines = jic._environment_lines(block)
        assert env_lines is not None, "Jupyter must declare an environment block"
        order = jic._merge_alias_order(env_lines)
        assert order and order[0] == label, (
            f"the blanking anchor {label!r} must be merged FIRST (order: {order})"
        )
        inline = jic._inline_env_keys(env_lines)
        for key in _SECRET_BLANK_KEYS:
            assert inline.get(key, "") == "", f"no inline secret override of {key} allowed"


def test_no_vault_or_credential_mount(contract, jupyter_blocks) -> None:
    """AC clause — cannot read brokerage credentials: no vault mount, no docker socket."""

    for block in jupyter_blocks:
        assert not jic._service_has_vault_mount(block), (
            "Jupyter must not mount the credential vault"
        )
        assert contract["credential_vault_mount_token"] not in block
        for token in contract["forbidden_mount_tokens"]:
            assert token not in block, f"Jupyter block must not reference {token!r}"


def test_read_only_data_access_only(contract, jupyter_blocks) -> None:
    """AC clause — read-only access to market data and backtest results only.

    Strict allow-list: only the sanctioned read-only data tiers, no read-write tier,
    no host bind, no extra shared named volume.
    """

    allowed = set()
    for sanctioned in contract["sanctioned_readonly_data_volumes"]:
        parsed = jic._parse_volume_entry(f"- {sanctioned}")
        allowed.add((parsed["source"], parsed["target"]))

    for block in jupyter_blocks:
        mounts = jic._volume_mounts(block) or []
        assert mounts, "Jupyter must declare its read-only data-tier mounts"
        for mount in mounts:
            assert mount["kind"] != "bind", f"bind mount not allowed: {mount['raw']!r}"
            assert mount["source"][:1] not in ("/", ".", "~", "{", "$"), (
                f"host-path bind not allowed: {mount['raw']!r}"
            )
            assert (mount["source"], mount["target"]) in allowed, (
                f"non-sanctioned mount not allowed: {mount['raw']!r}"
            )
            assert mount["read_only"], f"data tier must be read-only: {mount['raw']!r}"
        present = {(m["source"], m["target"]) for m in mounts if m["read_only"]}
        assert allowed <= present, "all sanctioned read-only data tiers must be mounted"


def test_no_execution_network_path(contract, compose_text, jupyter_blocks) -> None:
    """AC clause — no direct access to the execution engine / no live orders.

    Jupyter is confined to a dedicated ``internal: true`` network (no egress), never
    the default bridge, and shares NO network with an execution-API peer.
    """

    for block in jupyter_blocks:
        networks = jic._service_networks(block)
        assert networks, "Jupyter must attach to a dedicated internal network"
        assert "default" not in networks, "Jupyter must not be on the default bridge"
        jupyter_nets = set(networks)
        for net in networks:
            net_block = jic._service_block(compose_text, net)
            assert net_block is not None, f"network {net!r} is not declared top-level"
            eff_net = jic._strip_comments(net_block)
            assert jic._scalar_bool(jic._service_scalar(eff_net, "external")) is not True
            internal = jic._service_scalar(eff_net, "internal")
            assert jic._scalar_bool(internal) is True, (
                f"network {net!r} must have direct-child internal: true (no egress)"
            )
        # No execution-API peer shares a network with Jupyter.
        for peer in contract["forbidden_network_peers"]:
            peer_block = jic._service_block(compose_text, peer)
            if peer_block is None:
                continue
            peer_nets = jic._service_networks(jic._strip_comments(peer_block))
            peer_net_set = set(peer_nets) if peer_nets else {"default"}
            assert not (jupyter_nets & peer_net_set), (
                f"Jupyter must share no network with the execution-API peer {peer!r}"
            )


def test_no_host_network_or_unresolvable_constructs(jupyter_blocks) -> None:
    """Jupyter uses no host / shared-namespace networking and no service-level merge /
    aliased security value that Docker would resolve but a static check cannot see."""

    for block in jupyter_blocks:
        network_mode = jic._service_scalar(block, "network_mode")
        assert network_mode is None or not (
            network_mode == "host"
            or network_mode.startswith("service:")
            or network_mode.startswith("container:")
        )
        assert jic._service_scalar(block, "pid") != "host"
        assert jic._service_scalar(block, "ipc") not in ("host", "shareable")
        child = jic._child_indent(block)
        for line in block.splitlines():
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) != child:
                continue
            stripped = line.strip()
            assert not stripped.startswith("<<:"), "no service-level merge allowed"
            assert not stripped.startswith("extends:"), "no extends inheritance allowed"
            for key in jic._ALIAS_REFUSED_KEYS:
                if stripped.startswith(f"{key}:"):
                    value = stripped[len(key) + 1 :].strip()
                    assert "*" not in value and "$" not in value, (
                        f"security key {key!r} must not use an alias / interpolation: {value!r}"
                    )


@pytest.mark.parametrize("fixture", list(jic._FIXTURES))
def test_check_rejects_each_violation(fixture: str) -> None:
    """The check fails closed (non-zero exit) on every seeded isolation violation.

    This is the load-bearing proof that the inspection is not vacuous: removing any
    single isolation directive makes ``jupyter_isolation_check.py`` reject the stack.
    """

    result = subprocess.run(
        [sys.executable, str(CHECK), "--fixture", fixture],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"fixture {fixture!r} should be rejected but check passed"
    assert "SRS-SEC-004 FAIL" in result.stderr
