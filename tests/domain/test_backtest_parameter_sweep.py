"""SRS-BT-007 / SyRS SYS-19 -- a parameter space definition produces ranked backtest
results by the selected objective function, deterministically and fail-closed.

L7 domain (safety) test. The acceptance criterion's safety core is that the ranking an
operator selects a strategy configuration from -- and ultimately sizes capital on -- is
*trustworthy*: the ranking must be provably ordered by the selected objective (verified
against an independent hand-derived ranking, under both SYS-19 named objectives), a
point whose objective metric is mathematically undefined must be reported unranked
(never a fabricated stand-in value, never silently ranked last, never dropped -- the
report's accounting proves every enumerated point is present), any per-point failure
must abort the whole sweep naming the offending point (a partial ranking could silently
mis-rank), the space's cardinality must be bounded BEFORE any backtest runs, identical
inputs must produce an identical report, and the surface must be independent of the
IB account. A leak in any of these is a trading-decision safety bug: a fabricated,
partial, or nondeterministic ranking would promote the wrong parameter configuration.
This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_007_parameter_sweep.rs`` and asserts that the
     ranking equals an independently hand-derived ranking under maximize-Sharpe and
     minimize-max-drawdown, that an undefined objective routes to the unranked bucket
     with the accounting intact, that repeat runs are identical, that ties break by the
     canonical parameter order, that the cardinality cap fires before a single strategy
     is built, and that a factory/engine failure aborts the sweep naming the point.

  2. Structural -- it asserts, via ``tools/backtest_sweep_check.py``, that the sweep
     module ranks via ``f64::total_cmp`` driven by the selected direction with the
     canonical tie-break, routes an undefined objective to the unranked bucket (no
     fabricated fallback), enforces the ``checked_mul`` cardinality cap before
     materialization, reuses the shipped ``BacktestEngine`` + ``benchmark::compare``
     chain (no parallel re-implementation), uses no nondeterminism source, declares no
     broker dependency, and leaks no vendor token.

Each structural guard is checked for non-vacuity: a swapped-out total_cmp sort, a
fabricated-zero objective fallback, a neutered cardinality cap, an injected
nondeterminism source, a dropped unranked route, a dropped lib.rs re-export, an
injected broker dependency, and a leaked vendor token are each shown to be caught.
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

from backtest_sweep_check import (  # noqa: E402
    BacktestSweepCheckError,
    cargo_source,
    check_determinism,
    check_module_reexport,
    check_no_broker_dependency,
    check_none_fail_closed,
    check_objective_ranking,
    check_point_cap,
    check_point_failure,
    check_runner_reuse,
    check_space_validation,
    check_vendor_isolation,
    lib_source,
    load_config,
    sweep_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_bt_007_parameter_sweep"
) -> subprocess.CompletedProcess[str]:
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
            test_file,
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


# --------------------------------------------------------------------------- #
# Behavioral: the ranked results are provably correct, complete, and fail-closed
# --------------------------------------------------------------------------- #


def test_ranking_matches_hand_derived_order_maximize_sharpe() -> None:
    # The safety core: the sweep's ranking equals an independent hand-derived ranking
    # (hand-run engine + compare per point, hand sort) under the first SYS-19 named
    # objective -- the order an operator allocates capital by is provably correct.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_ranks_by_maximize_sharpe"),
        "SRS-BT-007 hand-verified maximize-Sharpe ranking",
    )


def test_ranking_matches_hand_derived_order_minimize_drawdown() -> None:
    # The second SYS-19 named objective: minimize max drawdown produces the hand-derived
    # ascending order, and genuinely differs from the Sharpe ranking -- the SELECTION
    # drives the result.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_direction_minimize_max_drawdown"),
        "SRS-BT-007 hand-verified minimize-drawdown ranking",
    )


def test_undefined_objective_is_unranked_never_fabricated() -> None:
    # Safety: a point whose objective is mathematically undefined (zero trades -> no win
    # rate) is reported unranked with its reason and its metrics intact -- never a
    # fabricated stand-in, never silently dropped (total accounting proves presence).
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_undefined_objective_is_unranked_not_fabricated"),
        "SRS-BT-007 undefined-objective unranked routing",
    )


def test_repeat_runs_identical() -> None:
    # Safety: identical inputs produce an identical report (SRS-BT-010 discipline) -- a
    # nondeterministic ranking could promote a different configuration on re-run.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_deterministic_repeat_runs_identical"),
        "SRS-BT-007 repeat-run determinism",
    )


def test_ties_break_deterministically() -> None:
    # Safety: genuinely tied objective values order by the canonical parameter entries,
    # so a tie can never make the ranking order-dependent.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_ties_break_by_canonical_parameter_order"),
        "SRS-BT-007 deterministic tie-break",
    )


def test_cardinality_cap_fires_before_any_backtest() -> None:
    # Safety: an over-cap space fails closed BEFORE a single strategy is built (a
    # counting factory proves zero builds) -- a sweep is a bounded operator workflow.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_point_cap_fails_before_any_backtest"),
        "SRS-BT-007 pre-run cardinality cap",
    )


def test_point_failure_aborts_naming_the_point() -> None:
    # Safety: a point the factory rejects aborts the WHOLE sweep naming the offending
    # point -- a partial ranking could silently mis-rank.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_factory_rejection_names_offending_point"),
        "SRS-BT-007 factory-rejection abort",
    )
    # ...and an engine failure inside one point likewise aborts, point-attributed.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_backtest_failure_fails_sweep_closed"),
        "SRS-BT-007 engine-failure abort",
    )


def test_degenerate_space_definitions_fail_closed() -> None:
    # Safety: every malformed space definition (zero axes, empty/duplicate names,
    # empty/duplicate values) maps to its exact fail-closed error -- a degenerate space
    # never silently enumerates.
    _assert_one_passed(
        _run_cargo_test("srs_bt_007_degenerate_spaces_fail_closed"),
        "SRS-BT-007 degenerate-space rejection",
    )


# --------------------------------------------------------------------------- #
# Structural: the guards exist in the shipped source, and none is vacuous
# --------------------------------------------------------------------------- #


def test_ranking_is_total_ordered_and_direction_driven() -> None:
    config = load_config()
    # The real module sorts via f64::total_cmp driven by the selected direction with the
    # canonical tie-break, so the ranking is a deterministic total order.
    check_objective_ranking(config, sweep_source(config))
    # ...and the guards must not be vacuous: swapping total_cmp for a partial-order
    # comparison is caught.
    mutated = sweep_source(config).replace("total_cmp", "partial_cmp_stub")
    with pytest.raises(BacktestSweepCheckError):
        check_objective_ranking(config, mutated)
    # ...dropping the canonical tie-break (an order-dependent ranking on ties) is caught.
    mutated = sweep_source(config).replace("a.0.entries().cmp(b.0.entries())", "Ordering::Equal")
    with pytest.raises(BacktestSweepCheckError):
        check_objective_ranking(config, mutated)


def test_undefined_objective_routing_is_structural() -> None:
    config = load_config()
    # The real extraction routes None to the unranked bucket and declares the
    # NonFiniteObjective defense.
    check_none_fail_closed(config, sweep_source(config))
    # ...and the guard must not be vacuous: dropping the unranked route (silently
    # discarding undefined points) is caught.
    mutated = sweep_source(config).replace("None => unranked.push(UnrankedPoint", "None => (drop(")
    with pytest.raises(BacktestSweepCheckError):
        check_none_fail_closed(config, mutated)
    # ...and fabricating a stand-in objective for an undefined metric is caught.
    mutated = sweep_source(config).replace(
        "match request.objective.metric.value(&report.metrics) {",
        "match Some(request.objective.metric.value(&report.metrics).unwrap_or(0.0)) {",
    )
    with pytest.raises(BacktestSweepCheckError):
        check_none_fail_closed(config, mutated)


def test_cardinality_cap_is_structural() -> None:
    config = load_config()
    # The real cap uses checked_mul and fires before materialization.
    check_point_cap(config, sweep_source(config))
    # ...and the guard must not be vacuous: neutering the overflow-checked arithmetic is
    # caught.
    mutated = sweep_source(config).replace("checked_mul", "wrapping_mul_stub")
    with pytest.raises(BacktestSweepCheckError):
        check_point_cap(config, mutated)
    # ...and moving/removing the pre-materialization cap return is caught.
    mutated = sweep_source(config).replace("return Err(SweepError::TooManyPoints", "// no cap (")
    with pytest.raises(BacktestSweepCheckError):
        check_point_cap(config, mutated)


def test_runner_reuses_the_shipped_chain() -> None:
    config = load_config()
    # The real runner reuses BacktestEngine + benchmark::compare -- a sweep result is
    # exactly what a standalone run of that point would report.
    check_runner_reuse(config, sweep_source(config))
    # ...and the guard must not be vacuous: dropping the compare() reuse (a parallel
    # metric re-implementation could diverge from what BT-009 persists) is caught.
    mutated = sweep_source(config).replace(
        "use crate::benchmark::{compare", "use crate::benchmark::{"
    )
    with pytest.raises(BacktestSweepCheckError):
        check_runner_reuse(config, mutated)


def test_sweep_is_deterministic() -> None:
    config = load_config()
    # The real module uses no parallelism/RNG/clock, so identical inputs rank
    # identically.
    check_determinism(config, sweep_source(config))
    mutated = sweep_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(BacktestSweepCheckError):
        check_determinism(config, mutated)


def test_space_validation_is_structural() -> None:
    config = load_config()
    # The real space validation raises every fail-closed variant; renaming the
    # duplicate-value guard away (allowing two identical enumerated points, which would
    # make ranking ambiguous) is caught.
    check_space_validation(config, sweep_source(config))
    mutated = sweep_source(config).replace("DuplicateAxisValue", "IgnoredAxisValue")
    with pytest.raises(BacktestSweepCheckError):
        check_space_validation(config, mutated)


def test_point_failure_naming_is_structural() -> None:
    config = load_config()
    # The real per-point failure names the offending point.
    check_point_failure(config, sweep_source(config))
    mutated = sweep_source(config).replace("parameters: parameters.clone()", "reason: reason")
    with pytest.raises(BacktestSweepCheckError):
        check_point_failure(config, mutated)


def test_sweep_module_is_reexported() -> None:
    config = load_config()
    # The real lib.rs re-exports the sweep surface; dropping it is caught.
    check_module_reexport(config, lib_source(config))
    mutated = lib_source(config).replace("pub mod sweep;", "mod sweep_disabled;")
    with pytest.raises(BacktestSweepCheckError):
        check_module_reexport(config, mutated)


def test_sweep_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml declares no live/broker-path dependency, so a parameter sweep
    # is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(BacktestSweepCheckError):
        check_no_broker_dependency(config, mutated)


def test_sweep_module_leaks_no_vendor_token() -> None:
    config = load_config()
    check_vendor_isolation(config, sweep_source(config))
    mutated = sweep_source(config) + "\n// sweeps mirrored to ib_insync under the hood\n"
    with pytest.raises(BacktestSweepCheckError):
        check_vendor_isolation(config, mutated)
