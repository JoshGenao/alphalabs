"""SRS-SAFE-003 / ERR-2 -- blocking live order submission when IB Gateway is unreachable is safe.

L7 domain (safety) test, paired with the ``safe003_connectivity_block_cli`` operator surface. The
acceptance criterion: during an IB-unreachable state, live order submissions must fail with
``CONNECTIVITY_BLOCKED`` until reconnection and readiness checks pass. The ERR-2 gate itself is
covered by ``test_connectivity_blocked.py`` (which drives ``submit_live_order`` directly); this test
covers the half that gate does not -- the block proven END TO END through the PRODUCTION authority
boundary ``dispatch_order -> route_order -> submit_live_order``, with a wire-attempt witness.

Why this is a safety boundary, not a formatting concern. If a live submission reached IB while the
gateway was unreachable, the operator would believe an order was placed that was not (or a stale one
executed against a broken session). So the invariant has real teeth:

  * a blocked submission must create ZERO IB orders -- proven by the recording transport's
    wire-attempt count, not merely by the absence of a receipt;
  * the block must be reached through the single-live designation authority (``route_order``), so a
    regression in how the authority gate propagates the connectivity block is visible; and
  * the block must be SELECTIVE -- a ``Connected`` gate must still route the same order through, else
    the gate would silently disable the live path even when IB is healthy.

This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-orchestrator/tests/srs_safe_003_connectivity_block_cli.rs`` (which drives the
     safe003_connectivity_block_cli binary in fresh OS processes) and asserts a blocked state refuses
     the submission with the full envelope, a Connected state routes it through, and the opposite-class
     ``--inject`` makes each proof fail closed with no proof.

  2. Structural (non-vacuity) -- it scans the CLI + wiring sources and asserts the proof genuinely
     runs through the production authority chain (``dispatch_order`` / ``run_connectivity_block_scenario``)
     and NOT by calling ``submit_live_order`` directly, self-labels its transports ``FIXTURE``, and
     prints both ``:true`` proof headlines.

Scope: this is deterministic FIXTURE verification of the GATE over the real authority chain. No
runtime code produces a ``ConnectivityState`` today; the real IB-disconnect -> ``Unreachable``
producer + readiness wiring + a live fault-injection e2e are deferred, so SRS-SAFE-003 stays
``passes:false`` until they land.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
L5_TEST = "srs_safe_003_connectivity_block_cli"

_CLI_SOURCE = REPO_ROOT / "crates/atp-orchestrator/src/bin/safe003_connectivity_block_cli.rs"
_WIRING_SOURCE = REPO_ROOT / "crates/atp-orchestrator/src/order_routing_wiring.rs"


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            L5_TEST,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], test_name: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"{test_name} failed:\n{combined}"
    assert "1 passed" in combined, f"{test_name} did not run (filtered out?):\n{combined}"


# --------------------------------------------------------------------------- #
# Behavioral -- the real binary, driven in fresh processes
# --------------------------------------------------------------------------- #


def test_unreachable_state_blocks_live_submission() -> None:
    """During an IB-unreachable state a live submission fails with CONNECTIVITY_BLOCKED, no IB order."""
    name = "unreachable_state_blocks_with_full_envelope"
    _assert_one_passed(_run_cargo_test(name), name)


def test_scheduled_restart_window_blocks_with_suppression_flag() -> None:
    """SRS-MD-005: the daily-restart window also blocks, with scheduled_restart=true."""
    name = "scheduled_restart_state_sets_the_suppression_flag"
    _assert_one_passed(_run_cargo_test(name), name)


def test_connected_state_routes_the_live_order_through() -> None:
    """The block is SELECTIVE: a Connected gate still routes the same order through to the broker."""
    name = "connected_state_routes_the_live_order_through"
    _assert_one_passed(_run_cargo_test(name), name)


def test_inject_connected_makes_the_block_proof_fail_closed() -> None:
    """Non-vacuity: a reachable gate routes through, so connectivity-block cannot be derived."""
    name = "prove_block_inject_connected_fails_closed"
    _assert_one_passed(_run_cargo_test(name), name)


def test_inject_unreachable_makes_the_routes_proof_fail_closed() -> None:
    """Non-vacuity: a blocked gate refuses the order, so routes-when-connected cannot be derived."""
    name = "routes_inject_unreachable_fails_closed"
    _assert_one_passed(_run_cargo_test(name), name)


def test_prove_block_refuses_a_non_blocked_state() -> None:
    """prove-block proves a blocked state; --state connected must fail closed."""
    name = "prove_block_state_connected_fails_closed"
    _assert_one_passed(_run_cargo_test(name), name)


def test_proofs_are_deterministic_across_processes() -> None:
    name = "identical_inputs_are_byte_identical_across_processes"
    _assert_one_passed(_run_cargo_test(name), name)


# --------------------------------------------------------------------------- #
# Structural (non-vacuity) -- the proof runs through the production authority chain
# --------------------------------------------------------------------------- #


def _cli_source() -> str:
    assert _CLI_SOURCE.is_file(), f"CLI source missing: {_CLI_SOURCE}"
    return _CLI_SOURCE.read_text(encoding="utf-8")


def _wiring_source() -> str:
    assert _WIRING_SOURCE.is_file(), f"wiring source missing: {_WIRING_SOURCE}"
    return _WIRING_SOURCE.read_text(encoding="utf-8")


def test_cli_drives_the_production_authority_scenario() -> None:
    """The CLI must derive its evidence from the shared scenario, not a hand-rolled classifier."""
    src = _cli_source()
    assert "run_connectivity_block_scenario" in src, (
        "the CLI must drive run_connectivity_block_scenario (the real dispatch_order chain)"
    )
    # A CLI that CALLED submit_live_order directly would sidestep the single-live designation
    # authority (route_order) and prove a weaker claim than the AC requires. The doc-comment chain
    # diagram may name it (prose); what must be absent is a method call `.submit_live_order(`.
    assert ".submit_live_order(" not in src, (
        "the CLI must reach the gate through the authority boundary (dispatch_order), never by "
        "calling submit_live_order directly"
    )


def test_scenario_drives_dispatch_order_through_the_authority_chain() -> None:
    """The scenario must route through the real dispatch_order authority boundary."""
    src = _wiring_source()
    assert "fn run_connectivity_block_scenario" in src
    # The scenario designates a single live strategy and routes through dispatch_order (not a direct
    # submit_live_order), so the block is proven through the production authority chain.
    body = src.split("fn run_connectivity_block_scenario", 1)[1]
    assert ".designate(" in body, (
        "the scenario must designate a live strategy (SRS-EXE-001 authority)"
    )
    assert ".dispatch_order(" in body, "the scenario must route through dispatch_order"
    assert "InjectableConnectivity::in_state" in body, (
        "the scenario must inject the connectivity state"
    )


def test_cli_self_labels_fixture_transports() -> None:
    """A drill must never be mistaken for live evidence -- every proof path labels transports FIXTURE."""
    assert "transports:FIXTURE" in _cli_source(), (
        "the CLI must self-label its transports FIXTURE so a drill is not mistaken for live evidence"
    )


def test_cli_prints_both_proof_headlines() -> None:
    src = _cli_source()
    for headline in (
        "connectivity-block-proven:true",
        "connectivity-routes-when-connected:true",
    ):
        assert headline in src, f"the CLI must be able to print the proof headline {headline!r}"


def test_cli_asserts_the_zero_ib_order_wire_witness() -> None:
    """The 'no IB order created' claim must be the wire-attempt count, checked == 0 on the block path."""
    src = _cli_source()
    assert "ib_orders_created == 0" in src, (
        "the block proof must assert ZERO IB orders were created (the wire-attempt witness), not "
        "merely that no receipt came back"
    )
    assert "ib_orders_created == 1" in src, (
        "the Connected positive control must assert exactly one IB order was created, so the zero "
        "count is meaningful"
    )
