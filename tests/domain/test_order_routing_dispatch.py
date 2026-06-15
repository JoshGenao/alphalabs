"""SRS-EXE-002 / SyRS SYS-2b / SYS-2e / AC-10 — route all non-live strategy
orders to the internal simulation engine; paper strategy orders never create IB
orders.

L7 domain (safety) test. The order-routing dispatch authority derives its
destination from the engine-owned SRS-EXE-001 live-designation authority: the
single designated live strategy is dispatched to the live IB gate; every other
(non-live) strategy is dispatched to the internal simulation engine through the
``InternalSimulationSubmit`` port and never reaches IB. The Rust integration
test at ``crates/atp-execution/tests/srs_exe_002_order_routing.rs`` routes a
non-live order to a counting simulation spy while the broker / connectivity /
freshness ports are panic-on-touch stubs (so the test fails loudly if a paper
order ever consults an IB port), and a designated order to the live broker
while the simulation port is panic-on-touch. This Python test shells out to
``cargo test`` and asserts the routing authority holds end to end:

  * a non-live strategy routes to the internal simulation engine, never IB;
  * with no designation, every order routes to the simulation engine;
  * the designated strategy routes to IB, never the simulation port;
  * with one live + 30 paper strategies, exactly one IB order side effect
    occurs and all 30 paper orders are simulated (the AC-10 acceptance).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-execution",
            "--test",
            "srs_exe_002_order_routing",
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
        f"SRS-EXE-002 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_non_live_strategy_routes_to_internal_simulation() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_002_non_live_strategy_routes_to_internal_simulation")
    )


def test_no_designation_routes_every_strategy_to_simulation() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_002_no_designation_routes_every_strategy_to_simulation")
    )


def test_designated_strategy_routes_to_ib_only() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_002_designated_strategy_routes_to_ib_only"))


def test_one_live_among_thirty_paper_routes_only_the_live_to_ib() -> None:
    # AC-10 acceptance: 1 live + 30 paper -> exactly one IB order; 30 simulated.
    _assert_single_pass(
        _run_cargo_test("srs_exe_002_one_live_among_thirty_paper_routes_only_the_live_to_ib")
    )
