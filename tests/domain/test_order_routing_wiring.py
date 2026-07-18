"""SRS-EXE-002 / SyRS SYS-2b / SYS-2e / AC-10 — route all non-live strategy
orders to the internal simulation engine; paper strategy orders never create IB
orders.

L7 domain (safety) test for the ORCHESTRATOR WIRING — the real components
behind the routing authority that ``test_order_routing_dispatch.py`` proves
with panic-on-touch stubs. The Rust integration test at
``crates/atp-orchestrator/tests/srs_exe_002_routing_wiring.rs`` binds:

  * the REAL SRS-SIM-001 ``PaperSimulationEngine`` (structurally broker-free:
    its ``OrderRouting`` type has no IB variant) behind the
    ``InternalSimulationSubmit`` port, with the SRS-DATA-021
    ``VirtualOrderBook`` as the single order store every accepted order rests
    in;
  * the REAL SRS-EXE-006 ``InteractiveBrokersBrokerage`` adapter behind the
    ``LiveBrokerageSubmit`` port over a deterministic mocked-IB recording
    transport, so "an IB order was created" is a wire-level count;
  * the REAL ``ExecutionEngine::dispatch_order`` as the sole entry.

This Python test shells out to ``cargo test`` for those invariants, then
drives the ``exe002_order_routing_cli`` operator verification binary (the
Step-2 "CLI calls and logs" workflow of the feature's verification — a
deterministic fixture scenario, not the deployed strategy-runtime order
path, which stays deferred to the SRS-SDK strategy host) and
asserts the AC-10 proof lines: a paper-only run creates ZERO IB orders, and a
mixed run creates exactly the designated live strategy's one.

The IB paper account is not touched by any of this: the only paper-account
surface in the repo is the operator-initiated SRS-EXE-006 adapter integration
test (``ATP_RUN_INTEGRATION=1`` + ``--ignored``, port 4002) — the SYS-2e
boundary.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return cargo


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _cargo(),
            "test",
            "-p",
            "atp-orchestrator",
            "--test",
            "srs_exe_002_routing_wiring",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-EXE-002 wiring test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _cargo(),
            "run",
            "--quiet",
            "-p",
            "atp-orchestrator",
            "--bin",
            "exe002_order_routing_cli",
            "--",
            *args,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_paper_order_routes_to_real_simulation_engine_with_zero_ib_wire_ops() -> None:
    _assert_single_pass(
        _run_cargo_test(
            "srs_exe_002_paper_order_routes_to_real_simulation_engine_with_zero_ib_wire_ops"
        )
    )


def test_one_live_among_thirty_paper_creates_exactly_one_ib_order() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_002_one_live_among_thirty_paper_creates_exactly_one_ib_order")
    )


def test_envelope_maps_to_order_leg_field_for_field() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_002_envelope_maps_to_order_leg_field_for_field"))


def test_port_side_rejection_fails_closed() -> None:
    _assert_single_pass(
        _run_cargo_test(
            "srs_exe_002_port_side_rejection_maps_to_structured_error_and_rests_nothing"
        )
    )


def test_malformed_dispatch_fails_closed_before_both_ports() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_002_malformed_dispatch_fails_closed_before_both_ports")
    )


def test_live_leg_routes_through_the_real_adapter() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_002_live_leg_routes_through_the_real_adapter"))


def test_cli_paper_only_run_creates_zero_ib_orders() -> None:
    result = _run_cli("route", "--paper-orders", "30")
    assert result.returncode == 0, (
        f"paper-only CLI run failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "ib_orders_created:0" in result.stdout, result.stdout
    assert "simulated_orders_accepted:30" in result.stdout, result.stdout
    assert "verdict:PASS" in result.stdout, result.stdout
    assert "live_brokerage" not in result.stdout, result.stdout


def test_cli_designated_live_run_creates_exactly_one_ib_order() -> None:
    result = _run_cli("route", "--paper-orders", "30", "--designate-live")
    assert result.returncode == 0, (
        f"mixed CLI run failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "ib_orders_created:1" in result.stdout, result.stdout
    assert "scenario.designated_live:live-alpha" in result.stdout, result.stdout
    assert result.stdout.count("route:live_brokerage") == 1, result.stdout
    assert result.stdout.count("route:internal_simulation") == 30, result.stdout
    assert "verdict:PASS" in result.stdout, result.stdout
