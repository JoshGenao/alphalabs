"""SRS-SIM-001 / SyRS SYS-82 — paper order intake creates NO IB API order calls.

L7 domain (safety) test. The acceptance criterion's safety core is that paper
strategy orders are simulated locally and *never* reach a brokerage. This test
proves that from two angles:

  1. Behavioral — it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_001_paper_order_intake.rs`` and asserts
     that every (asset class, side, order type, single/multi-leg) order routes to
     ``OrderRouting::InternalSimulation`` and that malformed orders fail closed
     (a rejected order cannot be silently re-routed to a broker).

  2. Structural — it asserts, via ``tools/sim_order_check.py``, that the
     ``OrderRouting`` type exposes no brokerage variant and that the
     ``atp-simulation`` crate declares no dependency on the live/broker path
     (``atp-execution`` / ``atp-adapters``). Because there is no broker routing
     variant to construct and no broker crate to call, "no IB API order calls" is
     a compile-time guarantee. The test also confirms the structural guard is not
     vacuous by checking it *would* reject an injected ``Broker`` variant.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_order_check import (  # noqa: E402
    SimOrderCheckError,
    cargo_source,
    check_no_broker_dependency,
    check_routing_internal_only,
    load_config,
    order_source,
)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-simulation",
            "--test",
            "srs_sim_001_paper_order_intake",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output for {label}:\n{combined}"


def test_no_accepted_order_can_reach_a_broker() -> None:
    # Sweeps every order shape and asserts each routes through the internal
    # simulation engine (the type-level no-broker proof).
    _assert_one_passed(
        _run_cargo_test("no_accepted_order_can_reach_a_broker"),
        "SRS-SIM-001 no-broker-route sweep",
    )


def test_every_order_type_routes_to_the_internal_simulation_engine() -> None:
    _assert_one_passed(
        _run_cargo_test("every_order_type_routes_to_the_internal_simulation_engine"),
        "SRS-SIM-001 order-type routing",
    )


def test_intake_fails_closed_on_bad_input() -> None:
    # Negative control: a malformed paper order is rejected, never silently
    # routed anywhere (and certainly not to a broker).
    _assert_one_passed(
        _run_cargo_test("intake_fails_closed_on_bad_input"),
        "SRS-SIM-001 fail-closed intake",
    )


def test_routing_type_exposes_no_broker_variant() -> None:
    config = load_config()
    # The real source must satisfy the internal-only routing contract.
    check_routing_internal_only(config, order_source(config))
    # ...and the guard must not be vacuous: an injected Broker variant is caught.
    mutated = order_source(config).replace(
        "    InternalSimulation {",
        "    Broker { order_id: String },\n    InternalSimulation {",
        1,
    )
    with pytest.raises(SimOrderCheckError):
        check_routing_internal_only(config, mutated)


def test_simulation_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(SimOrderCheckError):
        check_no_broker_dependency(config, mutated)
