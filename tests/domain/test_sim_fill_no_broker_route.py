"""SRS-SIM-002 / SyRS SYS-83 / SYS-87 — simulated fills stay inside the engine.

L7 domain (safety) test. The acceptance criterion's safety core is that a paper
strategy's fills are simulated from live market data *inside* the internal
simulation engine and never reach a brokerage, and that the SYS-87b realism
constraint (a fill may not exceed the observed bar volume) is enforced. This test
proves that from three angles:

  1. Behavioral — it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_002_fill_models.rs`` and asserts that a
     triggered fill flows through the shared transaction-cost family (staying inside
     the engine, no IB API order call), that the SYS-87b volume cap is enforced, and
     that corrupt market data fails closed (a rejected fill cannot be silently
     re-routed anywhere, and certainly not to a broker).

  2. Structural (no broker route) — it asserts, via ``tools/sim_fill_check.py``,
     that the ``atp-simulation`` crate declares no dependency on the live/broker
     path (``atp-execution`` / ``atp-adapters``) and that the fill module leaks no
     vendor SDK token. Because there is no broker crate to call, a simulated fill
     cannot produce an IB API order call.

  3. Structural (volume cap) — it asserts the SYS-87b volume constraint is present
     in ``evaluate_fill`` (the fill is capped at the observed bar volume and a
     zero-volume bar fills nothing).

Each structural guard is checked for non-vacuity: an injected broker dependency, a
leaked vendor token, and a removed volume cap are each shown to be caught.
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

from sim_fill_check import (  # noqa: E402
    SimFillCheckError,
    cargo_source,
    check_no_broker_dependency,
    check_vendor_isolation,
    check_volume_budget,
    check_volume_cap,
    fill_source,
    load_config,
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
            "srs_sim_002_fill_models",
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


def test_filled_decision_stays_inside_the_simulation_engine() -> None:
    # A triggered fill flows through the shared cost family -- it stays inside the
    # internal simulation engine and produces no IB API order call.
    _assert_one_passed(
        _run_cargo_test("fill_flows_through_cost_family"),
        "SRS-SIM-002 fill-through-cost-family",
    )


def test_volume_cap_is_enforced_behaviorally() -> None:
    # SYS-87b: a simulated fill may not exceed the observed bar volume.
    _assert_one_passed(
        _run_cargo_test("volume_cap_is_enforced"),
        "SRS-SIM-002 volume cap",
    )


def test_aggregate_volume_cap_is_enforced_behaviorally() -> None:
    # SYS-87b "for the bar period": the SUM of fills threaded through one
    # BarVolumeBudget cannot exceed the bar's observed volume, even across orders.
    _assert_one_passed(
        _run_cargo_test("aggregate_volume_cap_holds_across_orders"),
        "SRS-SIM-002 aggregate volume cap",
    )


def test_mismatched_budget_cannot_overfill_a_thin_bar_behaviorally() -> None:
    # SYS-87b: a budget built for a larger bar must not overfill a thinner snapshot;
    # the budget is bound to its bar and a mismatch fails closed.
    _assert_one_passed(
        _run_cargo_test("mismatched_budget_cannot_overfill_a_thin_bar"),
        "SRS-SIM-002 budget/snapshot binding",
    )


def test_corrupt_market_data_fails_closed() -> None:
    # Negative control: corrupt market data is rejected, never silently filled or
    # routed anywhere (and certainly not to a broker).
    _assert_one_passed(
        _run_cargo_test("fill_model_fails_closed_on_corrupt_data"),
        "SRS-SIM-002 fail-closed fills",
    )


def test_simulation_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(SimFillCheckError):
        check_no_broker_dependency(config, mutated)


def test_fill_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real fill module must carry no vendor SDK token.
    check_vendor_isolation(config, fill_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = fill_source(config) + "\n// fills arrive via ib_insync under the hood\n"
    with pytest.raises(SimFillCheckError):
        check_vendor_isolation(config, mutated)


def test_volume_cap_is_structurally_enforced() -> None:
    config = load_config()
    # The real budget-aware evaluator must cap each fill at the bar's remaining
    # volume and CONSUME the budget (SYS-87b aggregate enforcement).
    check_volume_cap(config, fill_source(config))
    # ...and the guard must not be vacuous: removing the per-fill cap is caught.
    mutated_cap = fill_source(config).replace(
        "requested_quantity.min(budget.remaining())",
        "requested_quantity",
        1,
    )
    with pytest.raises(SimFillCheckError):
        check_volume_cap(config, mutated_cap)
    # ...and dropping the budget consumption (which would let the aggregate exceed
    # the bar volume) is caught too.
    mutated_consume = fill_source(config).replace(
        "budget.consume(fill_quantity)",
        "let _ = fill_quantity",
        1,
    )
    with pytest.raises(SimFillCheckError):
        check_volume_cap(config, mutated_consume)


def test_budget_is_bound_to_its_bar() -> None:
    config = load_config()
    # The real budget-aware evaluator must reject a budget whose observed volume
    # does not match the snapshot, or an oversized budget could overfill a thin bar.
    check_volume_budget(config, fill_source(config))
    # ...and the binding guard must not be vacuous: removing it is caught.
    mutated = fill_source(config).replace(
        "budget.observed_bar_volume() != snapshot.bar_volume",
        "false",
        1,
    )
    with pytest.raises(SimFillCheckError):
        check_volume_budget(config, mutated)
