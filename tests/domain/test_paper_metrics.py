"""SRS-BT-004 / SyRS SYS-16, SYS-86 -- the shared performance-metric family is
deterministic, reports undefined metrics honestly, stays broker-independent, and
fails closed on degenerate input.

L7 domain (safety) test. The acceptance criterion's safety core is that the metrics
an operator ranks strategies and sizes capital on are *trustworthy*: the same inputs
must always produce the same metrics (a non-deterministic Sharpe would rank two
identical runs differently); an undefined metric must be reported as *undefined*
(None), never a fabricated 0.0 that reads as "flat" performance; a poison value
(NaN/inf) must never leak into a ranking or dashboard; the family must be computed
independently of the IB account; and a degenerate or misaligned input must fail
closed rather than silently emit a corrupt number. A leak in any of these is a
trading-decision safety bug: a fabricated or diverging metric would mis-promote a
strategy in the Reservoir or misstate live P&L on the dashboard. This test proves the
invariant from three angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_bt_004_metrics.rs`` and asserts that the eight
     metrics are computed from a real backtest, that repeated runs are identical
     (determinism), that the benchmark defaults to SPY, that undefined metrics are
     None, that the win rate matches the ledger's average-cost accounting, and that a
     misaligned benchmark fails closed.

  2. Structural (broker independence) -- it asserts, via ``tools/metrics_check.py``,
     that the ``atp-simulation`` crate declares no dependency on the live/broker path
     (``atp-execution`` / ``atp-adapters``) and that the ``metrics`` module leaks no
     vendor SDK token, so the family cannot be reconciled against the IB account.

  3. Structural (determinism + fail-closed + no-poison) -- it asserts the module
     rejects a non-monotonic timestamp (so the folds are order-independent), fails
     closed on a degenerate curve / misaligned benchmark, and verifies every result
     is finite before returning it.

Each structural guard is checked for non-vacuity: an injected broker dependency, a
leaked vendor token, a removed monotonic-timestamp guard, a removed fail-closed
guard, and a removed finite check are each shown to be caught.
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

from metrics_check import (  # noqa: E402
    MetricsCheckError,
    cargo_source,
    check_determinism,
    check_dispersion_tolerance,
    check_fail_closed,
    check_nan_guard,
    check_no_broker_dependency,
    check_paper_accumulator,
    check_vendor_isolation,
    load_config,
    metrics_source,
    paper_metrics_source,
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
            "srs_bt_004_metrics",
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


def test_metrics_computed_from_real_backtest() -> None:
    # The safety core: the eight metrics are produced from real engine output.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_metrics_computed_from_real_backtest"),
        "SRS-BT-004 metric computation",
    )


def test_metrics_are_deterministic() -> None:
    # Identical inputs must produce identical metrics, or a ranking would be unstable.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_metrics_are_deterministic"),
        "SRS-BT-004 metric determinism",
    )


def test_benchmark_defaults_to_spy() -> None:
    # A report with no selected benchmark must still identify SPY (SYS-17).
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_benchmark_defaults_to_spy"),
        "SRS-BT-004 SPY default",
    )


def test_undefined_metrics_are_none() -> None:
    # A metric undefined on the input must be None, never a fabricated 0.0.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_undefined_metrics_are_none"),
        "SRS-BT-004 undefined-as-None",
    )


def test_win_rate_matches_ledger_accounting() -> None:
    # The win rate must use the same average-cost close accounting as the ledger.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_matches_ledger_accounting"),
        "SRS-BT-004 win-rate accounting",
    )


def test_fails_closed_on_misaligned_benchmark() -> None:
    # Negative control: a benchmark series that cannot align period-for-period is
    # rejected, never silently computed against a partial overlap.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_fails_closed_on_misaligned_benchmark"),
        "SRS-BT-004 fail-closed alignment",
    )


def test_alpha_uses_risk_free_rate() -> None:
    # Money-math correctness: with a non-zero risk-free rate and beta != 1, Jensen's
    # alpha must use excess returns, or a ranking on alpha would be materially wrong.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_alpha_uses_risk_free_rate"),
        "SRS-BT-004 excess-return alpha",
    )


def test_win_rate_rejects_out_of_order_fills() -> None:
    # Determinism safety: a reordered (backwards-timestamp) trade log is rejected, so
    # the win rate cannot silently depend on input order across paper/live comparisons.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_rejects_out_of_order_fills"),
        "SRS-BT-004 win-rate ordering",
    )


def test_win_rate_is_net_of_transaction_costs() -> None:
    # Money-meaning safety: a gross-positive but net-negative round trip is a loss, so
    # the win rate is not inflated by trades that actually reduced equity.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_is_net_of_transaction_costs"),
        "SRS-BT-004 net-of-cost win rate",
    )


def test_win_rate_canonicalizes_symbols() -> None:
    # SYS-86 parity safety: an open on AAPL and close on aapl close the same position,
    # exactly as the ledger keys them, so paper and backtest win rates stay comparable.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_canonicalizes_symbols"),
        "SRS-BT-004 win-rate symbol parity",
    )


def test_win_rate_is_invariant_to_fill_fragmentation() -> None:
    # SYS-86 comparability safety: a round trip closed in one fill (backtest) vs three
    # volume-capped partial fills (paper/live) is the same trade, so the win rate is not
    # distorted by how an execution fragments.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_is_invariant_to_fill_fragmentation"),
        "SRS-BT-004 win-rate fragmentation invariance",
    )


def test_win_rate_applies_same_timestamp_fills_in_order() -> None:
    # Comparability safety: several orders / partial fills against one bar (same
    # timestamp) are applied in execution (trade-log) order and form one round trip, so
    # legitimate paper fill streams are processed rather than rejected.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_applies_same_timestamp_fills_in_order"),
        "SRS-BT-004 same-timestamp slice order",
    )


def test_rejects_trade_log_outside_the_run_window() -> None:
    # Coherence safety: a trade log whose fills fall outside the equity curve's run
    # window (a stale or mismatched snapshot) is rejected, so the win rate and the
    # equity-derived metrics cannot silently describe different runs.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_rejects_trade_log_outside_the_run_window"),
        "SRS-BT-004 run coherence",
    )


def test_first_period_captured_from_the_baseline() -> None:
    # Money-correctness safety: the pre-trade baseline is a required input, so the first
    # period's P&L (incl. entry costs) and an initial drawdown below starting equity are
    # always captured -- they cannot be silently omitted by a post-fill-start curve.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_first_period_captured_from_the_baseline"),
        "SRS-BT-004 first-period baseline capture",
    )


def test_terminal_zero_equity_is_a_total_loss() -> None:
    # Robustness safety: a completed run that ends at zero equity (bankruptcy) is a
    # defined -100% total loss, so one bad mark must not abort the whole metric family.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_terminal_zero_equity_is_a_total_loss"),
        "SRS-BT-004 terminal-zero total loss",
    )


def test_win_rate_flip_attributes_cost_to_close() -> None:
    # Money-correctness safety: a single-fill reversal attributes its full cost to the
    # closing round trip (no integer-floor bias), matching the same reversal written as
    # a close-then-open, so a small reversing close is not a spurious win.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_win_rate_flip_attributes_cost_to_close"),
        "SRS-BT-004 flip cost attribution",
    )


def test_paper_metrics_match_the_backtest_family() -> None:
    # SYS-86 safety core: the internal simulation engine must compute the SAME metric
    # family for a paper strategy that the backtest engine computes, or an operator would
    # rank a paper strategy against a backtest on incomparable numbers. The integration
    # test drives a real backtest AND the paper accumulator from the same activity and
    # asserts the metric families are equal.
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_paper_metrics_match_the_backtest_family"),
        "SRS-BT-004 paper/backtest metric parity",
    )


def test_paper_metrics_match_the_backtest_family_with_costs() -> None:
    # The parity must survive transaction costs (the cost decomposition flows into both
    # the cash curve and the net-of-cost win rate identically on both paths).
    _assert_one_passed(
        _run_cargo_test("srs_bt_004_paper_metrics_match_the_backtest_family_with_costs"),
        "SRS-BT-004 paper/backtest parity with costs",
    )


def test_paper_accumulator_fails_closed_on_a_missing_mark() -> None:
    config = load_config()
    # The real paper accumulator computes the family from the SYS-84 ledger and delegates
    # to the shared metrics::compute (so paper == backtest), failing closed on the
    # fabricated-equity hazards.
    check_paper_accumulator(config, paper_metrics_source(config))
    # ...and the headline guard must not be vacuous: dropping the MissingMark rejection
    # would let an open position with no supplied mark be silently valued at zero --
    # a fabricated net-liquidation equity, the worst failure mode for this accumulator.
    mutated = paper_metrics_source(config).replace(
        "PaperMetricsError::MissingMark", "PaperMetricsError::Overflow"
    )
    with pytest.raises(MetricsCheckError):
        check_paper_accumulator(config, mutated)


def test_paper_accumulator_enforces_cross_stream_ordering() -> None:
    config = load_config()
    # Coherence safety: the fill and mark streams must stay in chronological lockstep, or
    # a fill at/before an already-recorded mark (or a mark before an applied fill) would
    # fabricate a time-incoherent equity curve that metrics::compute cannot detect.
    check_paper_accumulator(config, paper_metrics_source(config))
    # ...and the guards must not be vacuous: dropping either cross-stream rejection is caught.
    for token in ("PaperMetricsError::FillBeforeMark", "PaperMetricsError::MarkBeforeFill"):
        mutated = paper_metrics_source(config).replace(token, "PaperMetricsError::Overflow")
        with pytest.raises(MetricsCheckError):
            check_paper_accumulator(config, mutated)


def test_metrics_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency, so the metric
    # family is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(MetricsCheckError):
        check_no_broker_dependency(config, mutated)


def test_metrics_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real metrics module must carry no vendor SDK token.
    check_vendor_isolation(config, metrics_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = metrics_source(config) + "\n// metrics mirrored to ib_insync under the hood\n"
    with pytest.raises(MetricsCheckError):
        check_vendor_isolation(config, mutated)


def test_metric_folds_are_deterministic() -> None:
    config = load_config()
    # The real module uses no parallelism/RNG/clock and rejects a non-monotonic
    # timestamp, so the folds are order-independent.
    check_determinism(config, metrics_source(config))
    # ...and the guard must not be vacuous: dropping the monotonic-timestamp rejection
    # (which would let an unordered curve produce order-dependent returns) is caught.
    mutated = metrics_source(config).replace(
        "MetricsError::NonMonotonicTimestamps", "MetricsError::Overflow"
    )
    with pytest.raises(MetricsCheckError):
        check_determinism(config, mutated)


def test_module_fails_closed_on_degenerate_input() -> None:
    config = load_config()
    # The real module rejects a non-positive equity mark and a misaligned benchmark.
    check_fail_closed(config, metrics_source(config))
    # ...and the guard must not be vacuous: dropping the non-positive-equity guard
    # (which would divide a return by zero) is caught.
    mutated = metrics_source(config).replace(
        "MetricsError::NonPositiveEquity", "MetricsError::Overflow"
    )
    with pytest.raises(MetricsCheckError):
        check_fail_closed(config, mutated)


def test_module_verifies_results_are_finite() -> None:
    config = load_config()
    # The real module verifies each computed metric is finite before returning it, so
    # a NaN/inf never leaks into a ranking or dashboard.
    check_nan_guard(config, metrics_source(config))
    # ...and the guard must not be vacuous: dropping the finite check is caught.
    mutated = metrics_source(config).replace("is_finite()", "is_nan()")
    with pytest.raises(MetricsCheckError):
        check_nan_guard(config, mutated)


def test_module_guards_near_zero_dispersion() -> None:
    config = load_config()
    # The real module divides Sharpe/Sortino/beta by a scale-aware-tolerant dispersion
    # so a floating-point-noise denominator yields None rather than an enormous,
    # ranking-corrupting ratio.
    check_dispersion_tolerance(config, metrics_source(config))
    # ...and the guard must not be vacuous: reverting Sharpe to an exact == 0.0 check
    # is caught.
    mutated = metrics_source(config).replace(
        "negligible_dispersion(stddev, returns)", "stddev == 0.0", 1
    )
    with pytest.raises(MetricsCheckError):
        check_dispersion_tolerance(config, mutated)
