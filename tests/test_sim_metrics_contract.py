"""Contract tests for SRS-BT-004 (compute required backtest and paper/live
performance metrics).

SRS-BT-004 / SyRS SYS-16, SYS-86 / StRS SN-1.04 / SN-1.05 / SN-1.29 -- compute the
standard performance-metric family (Sharpe, Sortino, alpha, beta, maximum drawdown,
annualized return, annualized volatility, win rate). This slice ships the
deterministic, dependency-free computation in ``crates/atp-simulation`` (module
``metrics``); the deferred halves (the live dashboard reporting path, the paper/live
runtime accumulators that feed this family, the SRS-BT-005 benchmark-resolution
surface) keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_sim_persistence_contract.py``: shells out to
``tools/metrics_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / Cargo.toml in memory and
assert the contract actually catches the regression (a metric typed non-Option, a
changed SPY default, a removed validation guard, a dropped error variant, a renamed
compute fn, a dropped metric fn, an injected nondeterminism source, a removed
fail-closed guard, a fabricated-zero win rate, a dropped NaN guard, a broken win-rate
accounting, a money-into-float input, a dropped lib re-export, an injected broker
dependency, a leaked vendor token).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from metrics_check import (  # noqa: E402
    MetricsCheckError,
    assert_sim_metrics_static,
    cargo_source,
    check_baseline,
    check_benchmark,
    check_cargo_test_smoke,
    check_compute_fn,
    check_config_struct,
    check_config_validation,
    check_determinism,
    check_dispersion_tolerance,
    check_error_enum,
    check_fail_closed,
    check_metric_fns,
    check_metrics_struct,
    check_module_reexport,
    check_nan_guard,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_paper_accumulator,
    check_run_coherence,
    check_undefined_semantics,
    check_vendor_isolation,
    check_win_rate,
    lib_source,
    load_config,
    metrics_source,
    paper_metrics_source,
    run_checks,
)


class MetricsScriptTest(unittest.TestCase):
    def test_srs_bt_004_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/metrics_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-004 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "PerformanceMetrics with all eight SYS-16 metrics as Option<f64>",
            "Benchmark defaults to SPY (DEFAULT_BENCHMARK_SYMBOL + Default impl)",
            "MetricsConfig with periods_per_year: u32 and risk_free_rate_per_period: f64, "
            "defaulting the annualization factor to 252",
            "MetricsConfig::new fails closed on a zero annualization factor "
            "(MetricsError::NonPositivePeriodsPerYear)",
            "MetricsError with 16 fail-closed variants",
            "exposes `pub fn compute`",
            "takes the pre-trade baseline as a REQUIRED compute input (starting_equity_minor)",
            "computes each metric in a dedicated fn",
            "metric folds are deterministic: no parallelism / RNG / clock token",
            "fails closed on a non-positive equity mark",
            "reports an undefined metric as None (Option<f64>; win rate None on no closed trade)",
            "verifies every computed metric is finite (fn finite + is_finite) and fails closed "
            "(NonFiniteComputation)",
            "win rate counts COMPLETE flat-to-flat round trips on canonicalized symbols",
            "a win on strictly positive NET round-trip P&L",
            "requires every trade-log fill to fall within the equity curve's run window",
            "guards the Sharpe/Sortino/beta denominators with a scale-aware tolerance",
            "keeps money in integer minor units on input (equity_minor / level_minor: i64) and "
            "outputs the metrics as dimensionless f64 ratios",
            "lib.rs re-exports `pub mod metrics;`",
            "Cargo.toml declares no dependency on the live/broker path (atp-adapters, atp-execution)",
            "metrics module is free of all 5 forbidden vendor SDK tokens",
            "declares PaperMetricsAccumulator (paper_metrics.rs)",
            "paper computes the SAME family as the backtest",
            "feature_list.json keeps SRS-BT-004 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = metrics_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.paper_src = paper_metrics_source(self.config)


class MetricsStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_metrics_struct(self.config, self.src)
        self.assertIn("all eight SYS-16 metrics as Option<f64>", evidence)

    def test_non_option_metric_is_caught(self) -> None:
        # Typing a metric as bare f64 would force a fabricated value on degenerate
        # input instead of None.
        mutated = self.src.replace("win_rate: Option<f64>,", "win_rate: f64,", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_metrics_struct(self.config, mutated)
        self.assertIn("win_rate", str(ctx.exception))


class BenchmarkTest(_Fixture):
    def test_benchmark_evidence(self) -> None:
        evidence = check_benchmark(self.config, self.src)
        self.assertIn("defaults to SPY", evidence)

    def test_changed_spy_default_is_caught(self) -> None:
        mutated = self.src.replace(
            'pub const DEFAULT_BENCHMARK_SYMBOL: &str = "SPY";',
            'pub const DEFAULT_BENCHMARK_SYMBOL: &str = "QQQ";',
            1,
        )
        with self.assertRaises(MetricsCheckError) as ctx:
            check_benchmark(self.config, mutated)
        self.assertIn("SPY", str(ctx.exception))


class ConfigStructTest(_Fixture):
    def test_config_evidence(self) -> None:
        evidence = check_config_struct(self.config, self.src)
        self.assertIn("periods_per_year: u32", evidence)

    def test_changed_default_factor_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub const DEFAULT_PERIODS_PER_YEAR: u32 = 252;",
            "pub const DEFAULT_PERIODS_PER_YEAR: u32 = 365;",
            1,
        )
        with self.assertRaises(MetricsCheckError) as ctx:
            check_config_struct(self.config, mutated)
        self.assertIn("252", str(ctx.exception))


class ConfigValidationTest(_Fixture):
    def test_validation_evidence(self) -> None:
        evidence = check_config_validation(self.config, self.src)
        self.assertIn("fails closed", evidence)

    def test_removed_factor_guard_is_caught(self) -> None:
        mutated = self.src.replace("if periods_per_year == 0", "if false", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_config_validation(self.config, mutated)
        self.assertIn("annualization factor", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in (
            "NonFiniteComputation",
            "BenchmarkLengthMismatch",
            "NonMonotonicTradeLog",
            "NegativeFillCost",
            "TradeLogOutsideRun",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.src.replace("NonFiniteComputation { metric: &'static str },", "", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("NonFiniteComputation", str(ctx.exception))


class ComputeFnTest(_Fixture):
    def test_compute_evidence(self) -> None:
        evidence = check_compute_fn(self.config, self.src)
        self.assertIn("single entry point", evidence)

    def test_renamed_compute_is_caught(self) -> None:
        mutated = self.src.replace("pub fn compute(", "pub fn renamed_compute(", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_compute_fn(self.config, mutated)
        self.assertIn("compute", str(ctx.exception))


class BaselineTest(_Fixture):
    def test_baseline_evidence(self) -> None:
        evidence = check_baseline(self.config, self.src)
        self.assertIn("REQUIRED compute input", evidence)

    def test_dropped_baseline_param_is_caught(self) -> None:
        # Removing the required baseline param (it appears in compute and the helper
        # signatures) would let a caller omit the first period; remove every occurrence
        # to prove the guard is non-vacuous.
        mutated = self.src.replace("starting_equity_minor: i64,", "removed: i64,")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_baseline(self.config, mutated)
        self.assertIn("baseline", str(ctx.exception).lower())

    def test_drawdown_not_from_baseline_is_caught(self) -> None:
        # Starting the drawdown peak at the first mark instead of the baseline would
        # miss an initial drop below starting equity.
        mutated = self.src.replace(
            "let mut peak = starting_equity_minor as f64",
            "let mut peak = equity_curve[0].equity_minor as f64",
            1,
        )
        with self.assertRaises(MetricsCheckError) as ctx:
            check_baseline(self.config, mutated)
        self.assertIn("peak", str(ctx.exception).lower())


class MetricFnsTest(_Fixture):
    def test_metric_fns_evidence(self) -> None:
        evidence = check_metric_fns(self.config, self.src)
        self.assertIn("dedicated fn", evidence)

    def test_dropped_metric_fn_is_caught(self) -> None:
        mutated = self.src.replace("fn sharpe_ratio(", "fn sharpe_removed(", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_metric_fns(self.config, mutated)
        self.assertIn("sharpe_ratio", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("deterministic", evidence)

    def test_injected_parallelism_is_caught(self) -> None:
        # Injecting a parallel iterator would make the metric folds order-dependent.
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(MetricsCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("nondeterminism", str(ctx.exception))

    def test_removed_monotonic_guard_is_caught(self) -> None:
        mutated = self.src.replace("MetricsError::NonMonotonicTimestamps", "MetricsError::Overflow")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("non-strictly-increasing", str(ctx.exception))


class FailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_fail_closed(self.config, self.src)
        self.assertIn("fails closed", evidence)

    def test_removed_equity_guard_is_caught(self) -> None:
        mutated = self.src.replace("MetricsError::NonPositiveEquity", "MetricsError::Overflow")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("non-positive equity", str(ctx.exception))


class UndefinedSemanticsTest(_Fixture):
    def test_undefined_evidence(self) -> None:
        evidence = check_undefined_semantics(self.config, self.src)
        self.assertIn("None", evidence)

    def test_fabricated_win_rate_is_caught(self) -> None:
        # Removing the "no completed round trip" guard would let win rate report a
        # fabricated 0.0 instead of None.
        mutated = self.src.replace("if completed == 0", "if false", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_undefined_semantics(self.config, mutated)
        self.assertIn("None", str(ctx.exception))


class NanGuardTest(_Fixture):
    def test_nan_guard_evidence(self) -> None:
        evidence = check_nan_guard(self.config, self.src)
        self.assertIn("finite", evidence)

    def test_removed_finite_check_is_caught(self) -> None:
        mutated = self.src.replace("is_finite()", "is_nan()")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_nan_guard(self.config, mutated)
        self.assertIn("finite", str(ctx.exception))


class WinRateTest(_Fixture):
    def test_win_rate_evidence(self) -> None:
        evidence = check_win_rate(self.config, self.src)
        self.assertIn("flat-to-flat round trips", evidence)

    def test_broken_win_condition_is_caught(self) -> None:
        # Counting every settled round trip as a win (dropping the > 0 condition) would
        # inflate the win rate. `if net > 0` appears in both the flat-settle and the
        # flip-settle branches, so mutate every occurrence to prove non-vacuity.
        mutated = self.src.replace("if net > 0", "if true")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("strictly positive", str(ctx.exception))

    def test_dropped_round_trip_settle_is_caught(self) -> None:
        # Without the flat-to-flat settle, the win rate would not count completed round
        # trips (the fragmentation-invariant unit).
        mutated = self.src.replace("if new_qty == 0", "if false", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("round trip", str(ctx.exception).lower())

    def test_removed_order_guard_is_caught(self) -> None:
        # Dropping the backwards-timestamp rejection would let the win rate depend on
        # the (reorderable) trade-log order.
        mutated = self.src.replace("MetricsError::NonMonotonicTradeLog", "MetricsError::Overflow")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("order", str(ctx.exception))

    def test_removed_canonicalization_is_caught(self) -> None:
        # Keying win-rate positions on the raw (un-canonicalized) symbol would break
        # SYS-86 parity with the ledger (AAPL vs aapl).
        mutated = self.src.replace("canonical_symbol(&fill.symbol)", "fill.symbol.clone()")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("canonical", str(ctx.exception).lower())

    def test_dropped_cash_flow_helper_is_caught(self) -> None:
        # Removing the round-trip cash-flow summation would break the fragmentation- and
        # order-invariant net P&L.
        mutated = self.src.replace("fn closing_cash_flow", "fn unused_cash_flow")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("cash flow", str(ctx.exception).lower())

    def test_removed_negative_cost_guard_is_caught(self) -> None:
        mutated = self.src.replace("MetricsError::NegativeFillCost", "MetricsError::Overflow")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_win_rate(self.config, mutated)
        self.assertIn("negative", str(ctx.exception).lower())


class RunCoherenceTest(_Fixture):
    def test_coherence_evidence(self) -> None:
        evidence = check_run_coherence(self.config, self.src)
        self.assertIn("run window", evidence.lower())

    def test_dropped_coherence_guard_is_caught(self) -> None:
        # Dropping the run-window check would let a stale/mismatched trade log be combined
        # with a different run's equity curve.
        mutated = self.src.replace("fill.ts < run_start || fill.ts > run_end", "false", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_run_coherence(self.config, mutated)
        self.assertIn("window", str(ctx.exception).lower())


class DispersionToleranceTest(_Fixture):
    def test_dispersion_evidence(self) -> None:
        evidence = check_dispersion_tolerance(self.config, self.src)
        self.assertIn("scale-aware tolerance", evidence)

    def test_exact_zero_check_in_sharpe_is_caught(self) -> None:
        # Reverting Sharpe to an exact == 0.0 denominator check would let a
        # floating-point-noise dispersion produce a spurious enormous ratio.
        mutated = self.src.replace("negligible_dispersion(stddev, returns)", "stddev == 0.0", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_dispersion_tolerance(self.config, mutated)
        self.assertIn("Sharpe", str(ctx.exception))

    def test_dropped_tolerance_helper_is_caught(self) -> None:
        mutated = self.src.replace("fn negligible_dispersion", "fn unused_dispersion")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_dispersion_tolerance(self.config, mutated)
        self.assertIn("near-zero dispersion", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_numeric_boundary_evidence(self) -> None:
        evidence = check_numeric_boundary(self.config, self.src)
        self.assertIn("integer minor units", evidence)

    def test_float_money_input_is_caught(self) -> None:
        # Money entering as f64 instead of integer minor units is a correctness leak.
        # `level_minor: i64` appears in both the BenchmarkPoint struct and the
        # NonPositiveBenchmarkLevel error variant, so mutate every occurrence to prove
        # the guard is non-vacuous.
        mutated = self.src.replace("level_minor: i64", "level_minor: f64")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("integer minor units", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod metrics;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod metrics;", "pub mod renamed_metrics;", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("metrics", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("broker-independent", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(MetricsCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// metrics streamed through ib_insync under the hood\n"
        with self.assertRaises(MetricsCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class PaperAccumulatorTest(_Fixture):
    """SYS-86: the paper accumulator computes the SAME family as the backtest engine."""

    def test_paper_accumulator_evidence(self) -> None:
        evidence = check_paper_accumulator(self.config, self.paper_src)
        self.assertIn("PaperMetricsAccumulator", evidence)
        self.assertIn("SAME family as the backtest", evidence)

    def test_missing_struct_is_caught(self) -> None:
        mutated = self.paper_src.replace(
            "pub struct PaperMetricsAccumulator", "pub struct RenamedAccumulator", 1
        )
        with self.assertRaises(MetricsCheckError) as ctx:
            check_paper_accumulator(self.config, mutated)
        self.assertIn("PaperMetricsAccumulator", str(ctx.exception))

    def test_dropped_missing_mark_guard_is_caught(self) -> None:
        # Removing the MissingMark guard would let an open position with no supplied
        # mark be silently valued at zero -- a fabricated-equity bug.
        mutated = self.paper_src.replace(
            "PaperMetricsError::MissingMark", "PaperMetricsError::Overflow"
        )
        with self.assertRaises(MetricsCheckError) as ctx:
            check_paper_accumulator(self.config, mutated)
        self.assertIn("missing mark", str(ctx.exception).lower())

    def test_dropped_cross_stream_ordering_guard_is_caught(self) -> None:
        # Dropping either cross-stream coherence guard would let an out-of-order fill/mark
        # interleaving fabricate a time-incoherent equity curve.
        for token, needle in (
            ("PaperMetricsError::FillBeforeMark", "already-recorded mark"),
            ("PaperMetricsError::MarkBeforeFill", "already-applied fill"),
        ):
            mutated = self.paper_src.replace(token, "PaperMetricsError::Overflow")
            with self.assertRaises(MetricsCheckError) as ctx:
                check_paper_accumulator(self.config, mutated)
            self.assertIn(needle, str(ctx.exception))

    def test_dropped_compute_reuse_is_caught(self) -> None:
        # The accumulator must DELEGATE to the shared metrics::compute, not re-derive
        # the math (else paper and backtest metrics could diverge).
        mutated = self.paper_src.replace("use crate::metrics::{", "use crate::nowhere::{", 1)
        with self.assertRaises(MetricsCheckError) as ctx:
            check_paper_accumulator(self.config, mutated)
        self.assertIn("shared metrics family", str(ctx.exception).lower())

    def test_dropped_ledger_marking_is_caught(self) -> None:
        # Net liquidation must mark through the ledger primitive (mark * quantity).
        mutated = self.paper_src.replace("market_value_minor(mark_minor)", "0i128.into_inner()")
        with self.assertRaises(MetricsCheckError) as ctx:
            check_paper_accumulator(self.config, mutated)
        self.assertIn("net-liq", str(ctx.exception).lower())


class CargoSmokeTest(unittest.TestCase):
    """The runnable metric path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("metrics_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("metrics_check.shutil.which", return_value=None):
            with self.assertRaises(MetricsCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_twenty_one_items(self) -> None:
        # 20 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 21)

    def test_static_evidence_is_twenty_items(self) -> None:
        self.assertEqual(len(assert_sim_metrics_static(load_config(), ROOT)), 20)


if __name__ == "__main__":
    unittest.main()
