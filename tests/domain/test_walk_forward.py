"""SRS-BT-008 / SyRS SYS-20 -- in-sample windows are optimized, out-of-sample windows are
evaluated, and outputs preserve the parameter set and metrics per window, deterministically
and fail-closed.

L7 domain (safety) test. Walk-forward analysis is how an operator decides whether a strategy
configuration's backtested edge SURVIVES on unseen data before sizing capital on it. Its
safety core is the NO-LOOKAHEAD invariant: the out-of-sample window an operator judges by must
lie strictly after the in-sample window the parameters were optimized on. A leak in any of
these is a trading-decision safety bug:

  - a lookahead window (out-of-sample overlapping or preceding in-sample) reports performance
    the optimizer already saw -- a fabricated out-of-sample edge that would promote an over-fit
    configuration to live capital;
  - a fabricated out-of-sample objective (a stand-in for an undefined metric) would rank a
    configuration on a number that does not exist;
  - a non-deterministic or partial report would promote a different configuration on re-run, or
    silently drop a window;
  - an in-sample window with no rankable optimum must fail closed, never silently select an
    unranked point;
  - the surface must be independent of the IB account.

This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_008_walk_forward.rs`` and asserts that each fold's
     in-sample optimization equals an INDEPENDENT sweep's rank-1 point (both SYS-19
     objectives), the out-of-sample metrics equal an INDEPENDENT raw backtest of the winner on
     the unseen window, the no-lookahead invariant holds (and a lookahead schedule fails
     closed), an in-sample window with no optimum fails closed naming the window, a per-fold
     failure aborts naming the window, every scheduled fold is accounted for, and repeat runs
     are identical.

  2. Structural -- it asserts, via ``tools/walk_forward_check.py``, that the walk_forward
     module enforces the no-lookahead guard, reuses the shipped ``SweepRunner`` (no
     re-implementation of the ``BacktestEngine`` + ``benchmark::compare`` chain), preserves the
     out-of-sample objective as an honest ``Option`` (no fabricated fallback), uses
     overflow-checked rolling arithmetic, uses no nondeterminism source, declares no broker
     dependency, and leaks no vendor token.

Each structural guard is checked for non-vacuity: a dropped no-lookahead guard, a
re-implemented engine chain, a fabricated out-of-sample objective, an injected nondeterminism
source, an injected broker dependency, and a leaked vendor token are each shown to be caught.
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

from walk_forward_check import (  # noqa: E402
    WalkForwardCheckError,
    cargo_source,
    check_determinism,
    check_no_broker_dependency,
    check_no_fabrication,
    check_no_lookahead,
    check_rolling_generator,
    check_runner_reuse,
    check_vendor_isolation,
    load_config,
    walk_forward_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_bt_008_walk_forward"
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
# Behavioral: the per-window results are provably correct, complete, fail-closed
# --------------------------------------------------------------------------- #


def test_in_sample_optimized_matches_independent_sweep_maximize_sharpe() -> None:
    # Safety core: each fold's optimized parameter set equals an INDEPENDENT sweep's rank-1
    # point over that fold's in-sample window -- the configuration an operator carries forward
    # is provably the in-sample optimum.
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_in_sample_optimized_matches_independent_sweep_maximize_sharpe"),
        "SRS-BT-008 in-sample optimization (maximize Sharpe)",
    )


def test_in_sample_optimized_matches_independent_sweep_minimize_drawdown() -> None:
    _assert_one_passed(
        _run_cargo_test(
            "srs_bt_008_in_sample_optimized_matches_independent_sweep_minimize_drawdown"
        ),
        "SRS-BT-008 in-sample optimization (minimize drawdown; selection drives the result)",
    )


def test_out_of_sample_evaluates_winner_on_unseen_window() -> None:
    # Safety: the out-of-sample metrics equal a RAW backtest of the winner over the unseen
    # window (an oracle independent of the runner's singleton-sweep path), and the out-of-sample
    # objective is read honestly off those metrics.
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_out_of_sample_evaluates_winner_on_the_unseen_window"),
        "SRS-BT-008 out-of-sample evaluation on unseen data",
    )


def test_no_lookahead_holds_and_lookahead_fails_closed() -> None:
    # The safety invariant: every fold's out-of-sample window is strictly after its in-sample
    # window, and a lookahead schedule fails closed.
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_no_lookahead_holds_across_folds"),
        "SRS-BT-008 no-lookahead across folds",
    )
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_lookahead_schedule_fails_closed"),
        "SRS-BT-008 lookahead schedule rejection",
    )


def test_no_optimum_fails_closed() -> None:
    # Safety: an in-sample window with no rankable optimum fails closed naming the window,
    # never silently selecting an unranked point.
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_no_optimum_fails_closed"),
        "SRS-BT-008 no-optimum fail-closed",
    )


def test_per_fold_failure_aborts_naming_the_window() -> None:
    # Safety: a per-fold sweep failure aborts the WHOLE analysis naming the offending window
    # (in-sample and out-of-sample paths both) -- a partial report could mis-rank a config.
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_in_sample_failure_names_the_window"),
        "SRS-BT-008 in-sample failure names the window",
    )
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_out_of_sample_failure_names_the_window"),
        "SRS-BT-008 out-of-sample failure names the window",
    )


def test_report_is_complete_and_deterministic() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_total_folds_accounts_for_every_window"),
        "SRS-BT-008 every scheduled fold accounted for",
    )
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_deterministic_repeat_runs_identical"),
        "SRS-BT-008 repeat-run determinism",
    )


def test_degenerate_schedules_fail_closed() -> None:
    _assert_one_passed(
        _run_cargo_test("srs_bt_008_degenerate_schedules_fail_closed"),
        "SRS-BT-008 degenerate-schedule rejection",
    )


# --------------------------------------------------------------------------- #
# Structural: the guards exist in the shipped source, and none is vacuous
# --------------------------------------------------------------------------- #


def test_no_lookahead_guard_is_structural() -> None:
    config = load_config()
    # The real module enforces in_sample.end < out_of_sample.start and fails closed.
    check_no_lookahead(config, walk_forward_source(config))
    # ...and the guard must not be vacuous: neutering it (allowing overlap) is caught.
    mutated = walk_forward_source(config).replace(
        "if self.in_sample.end >= self.out_of_sample.start {", "if false {"
    )
    with pytest.raises(WalkForwardCheckError):
        check_no_lookahead(config, mutated)


def test_runner_reuses_sweep_runner() -> None:
    config = load_config()
    # The real runner reuses SweepRunner for both phases; re-implementing the compare chain
    # in-module is caught.
    check_runner_reuse(config, walk_forward_source(config))
    mutated = walk_forward_source(config) + "\nfn _leak() { let _ = benchmark::compare; }\n"
    with pytest.raises(WalkForwardCheckError):
        check_runner_reuse(config, mutated)


def test_out_of_sample_objective_is_not_fabricated() -> None:
    config = load_config()
    check_no_fabrication(config, walk_forward_source(config))
    mutated = walk_forward_source(config) + "\nfn _fab() { let _ = x.unwrap_or(0.0); }\n"
    with pytest.raises(WalkForwardCheckError):
        check_no_fabrication(config, mutated)


def test_rolling_generator_is_overflow_checked() -> None:
    config = load_config()
    check_rolling_generator(config, walk_forward_source(config))
    mutated = walk_forward_source(config).replace("checked_mul", "wrapping_mul_stub")
    with pytest.raises(WalkForwardCheckError):
        check_rolling_generator(config, mutated)


def test_fold_count_is_capped_before_allocation() -> None:
    # Safety: an unbounded operator-supplied fold count must fail closed BEFORE any allocation
    # (never a Vec::with_capacity panic / OOM). Removing the cap is caught.
    config = load_config()
    check_rolling_generator(config, walk_forward_source(config))
    mutated = walk_forward_source(config).replace(
        "return Err(WalkForwardError::TooManyFolds", "// no cap ("
    )
    with pytest.raises(WalkForwardCheckError):
        check_rolling_generator(config, mutated)


def test_walk_forward_is_deterministic() -> None:
    config = load_config()
    check_determinism(config, walk_forward_source(config))
    mutated = walk_forward_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(WalkForwardCheckError):
        check_determinism(config, mutated)


def test_walk_forward_crate_has_no_broker_dependency() -> None:
    config = load_config()
    check_no_broker_dependency(config, cargo_source(config))
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(WalkForwardCheckError):
        check_no_broker_dependency(config, mutated)


def test_walk_forward_module_leaks_no_vendor_token() -> None:
    config = load_config()
    check_vendor_isolation(config, walk_forward_source(config))
    mutated = walk_forward_source(config) + "\n// folds mirrored to ib_insync under the hood\n"
    with pytest.raises(WalkForwardCheckError):
        check_vendor_isolation(config, mutated)
