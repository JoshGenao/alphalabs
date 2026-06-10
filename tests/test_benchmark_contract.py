"""Contract tests for SRS-BT-005 (compare strategy performance against a user-selected
benchmark defaulting to SPY).

SRS-BT-005 / SyRS SYS-17, SYS-36, SYS-37 / StRS SN-1.04 -- select a benchmark (SPY by
default), compute alpha/beta against it, and identify it in reports. This slice ships
the deterministic selection + resolution-seam + comparison-report surface in
``crates/atp-simulation`` (module ``benchmark``), wrapping the SRS-BT-004 metric family;
the deferred halves (real benchmark level-series resolution via SRS-DATA-007, the
dashboard/report rendering via SRS-UI / SRS-API, and the SRS-BT-009 persisted-comparison
record) keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_sim_metrics_contract.py``: shells out to
``tools/benchmark_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in
memory and assert the contract actually catches the regression (a dropped SPY default,
a renamed resolution port fn, a non-Option comparison ratio, a renamed report field, a
renamed compare fn, a dropped error variant, a removed trust-boundary guard, a removed
metric-family reuse, an injected nondeterminism source, a dropped NaN guard, a
money-into-float input, a dropped lib re-export, an injected broker dependency, a leaked
vendor token).
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

from benchmark_check import (  # noqa: E402
    BenchmarkCheckError,
    assert_sim_benchmark_static,
    benchmark_source,
    cargo_source,
    check_cargo_test_smoke,
    check_compare_fn,
    check_comparison_struct,
    check_determinism,
    check_error_enum,
    check_metrics_reuse,
    check_module_reexport,
    check_nan_guard,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_report_struct,
    check_resolved_identity,
    check_run_window_binding,
    check_selection,
    check_source_failure,
    check_source_trait,
    check_spy_default,
    check_trust_boundary,
    check_vendor_isolation,
    lib_source,
    load_config,
    run_checks,
)


class BenchmarkScriptTest(unittest.TestCase):
    def test_srs_bt_005_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/benchmark_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-005 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "BenchmarkSelection resolves an unselected benchmark to SPY",
            "BenchmarkSource resolution port (levels -> Result<ResolvedBenchmark",
            "declares BenchmarkComparison carrying the benchmark identity",
            "declares BenchmarkReport bundling the full PerformanceMetrics family",
            "exposes `pub fn compare`",
            "declares BenchmarkError with 12 fail-closed variants",
            "re-validates the resolved series at the trust boundary",
            "bound to the strategy run's INCLUSIVE evaluation window",
            "binds benchmark identity to the returned data",
            "declares SourceFailure (Timeout, Unavailable, NotFound, StaleData)",
            "applies the SPY default in BenchmarkSelection::resolve",
            "reuses the SRS-BT-004 metric family",
            "benchmark comparison is deterministic",
            "verifies every comparison ratio is finite (fn finite_opt + is_finite)",
            "keeps levels in integer minor units on input (level_minor: i64)",
            "lib.rs re-exports `pub mod benchmark;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "benchmark module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-005 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = benchmark_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class SelectionTest(_Fixture):
    def test_selection_evidence(self) -> None:
        evidence = check_selection(self.config, self.src)
        self.assertIn("resolves an unselected benchmark to SPY", evidence)

    def test_dropped_spy_default_is_caught(self) -> None:
        # Replacing unwrap_or_default with unwrap drops the SPY fallback, so an
        # unselected benchmark would panic instead of becoming SPY.
        mutated = self.src.replace(
            "self.selected.clone().unwrap_or_default()",
            "self.selected.clone().unwrap()",
            1,
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_selection(self.config, mutated)
        self.assertIn("SPY", str(ctx.exception))


class SourceTraitTest(_Fixture):
    def test_source_trait_evidence(self) -> None:
        evidence = check_source_trait(self.config, self.src)
        self.assertIn("resolution port", evidence)

    def test_renamed_levels_fn_is_caught(self) -> None:
        mutated = self.src.replace("fn levels", "fn fetched")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_source_trait(self.config, mutated)
        self.assertIn("levels", str(ctx.exception))

    def test_unlabeled_return_type_is_caught(self) -> None:
        # Returning a bare Vec<BenchmarkPoint> (not a ResolvedBenchmark) would decouple the
        # identity from the returned data again.
        mutated = self.src.replace(
            "Result<ResolvedBenchmark, SourceFailure>",
            "Result<Vec<BenchmarkPoint>, SourceFailure>",
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_source_trait(self.config, mutated)
        self.assertIn("ResolvedBenchmark", str(ctx.exception))


class ResolvedIdentityTest(_Fixture):
    def test_resolved_identity_evidence(self) -> None:
        evidence = check_resolved_identity(self.config, self.src)
        self.assertIn("binds benchmark identity to the returned data", evidence)

    def test_dropped_post_fetch_identity_guard_is_caught(self) -> None:
        # Removing the post-fetch returned-symbol check would let a source return one
        # benchmark's levels while the report identifies another (Codex R3).
        mutated = self.src.replace("resolved.symbol != benchmark.symbol()", "false")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_resolved_identity(self.config, mutated)
        self.assertIn("after the fetch", str(ctx.exception).lower())

    def test_dropped_resolved_symbol_field_is_caught(self) -> None:
        mutated = self.src.replace("pub symbol: String,", "pub label: String,", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_resolved_identity(self.config, mutated)
        self.assertIn("resolved symbol", str(ctx.exception).lower())


class ComparisonStructTest(_Fixture):
    def test_comparison_evidence(self) -> None:
        evidence = check_comparison_struct(self.config, self.src)
        self.assertIn("benchmark identity", evidence)

    def test_non_option_ratio_is_caught(self) -> None:
        # Typing a ratio as bare f64 would force a fabricated value on degenerate input.
        mutated = self.src.replace("excess_return: Option<f64>,", "excess_return: f64,", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_comparison_struct(self.config, mutated)
        self.assertIn("excess_return", str(ctx.exception))


class ReportStructTest(_Fixture):
    def test_report_evidence(self) -> None:
        evidence = check_report_struct(self.config, self.src)
        self.assertIn("bundling the full PerformanceMetrics family", evidence)

    def test_renamed_metrics_field_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub metrics: PerformanceMetrics,", "pub stats: PerformanceMetrics,", 1
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_report_struct(self.config, mutated)
        self.assertIn("metrics", str(ctx.exception))


class CompareFnTest(_Fixture):
    def test_compare_evidence(self) -> None:
        evidence = check_compare_fn(self.config, self.src)
        self.assertIn("single SRS-BT-005 comparison entry point", evidence)

    def test_renamed_compare_is_caught(self) -> None:
        mutated = self.src.replace("pub fn compare(", "pub fn renamed_compare(", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_compare_fn(self.config, mutated)
        self.assertIn("compare", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in (
            "SourceSymbolMismatch",
            "SourceLengthMismatch",
            "NonPositiveSourceLevel",
            "NonFiniteComparison",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.src.replace(
            "SourceSymbolMismatch { requested: String, returned: String },", "", 1
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("SourceSymbolMismatch", str(ctx.exception))


class TrustBoundaryTest(_Fixture):
    def test_trust_boundary_evidence(self) -> None:
        evidence = check_trust_boundary(self.config, self.src)
        self.assertIn("trust boundary", evidence)

    def test_dropped_length_guard_is_caught(self) -> None:
        mutated = self.src.replace(
            "BenchmarkError::SourceLengthMismatch", "BenchmarkError::EmptyEquityCurve"
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_trust_boundary(self.config, mutated)
        self.assertIn("SourceLengthMismatch", str(ctx.exception))

    def test_dropped_defense_in_depth_is_caught(self) -> None:
        # Removing the map_err wrap would let a metric error bypass the benchmark
        # error type (and skip the defense-in-depth re-validation contract).
        mutated = self.src.replace("map_err(BenchmarkError::Metrics)", 'expect("compute")')
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_trust_boundary(self.config, mutated)
        self.assertIn("metrics::compute", str(ctx.exception))


class RunWindowBindingTest(_Fixture):
    def test_window_binding_evidence(self) -> None:
        evidence = check_run_window_binding(self.config, self.src)
        self.assertIn("bound to the strategy run's INCLUSIVE evaluation window", evidence)

    def test_dropped_window_coherence_guard_is_caught(self) -> None:
        # Dropping the equity-mark-within-window check would let a stale/foreign window
        # measure the benchmark over a different period than the strategy (Codex R1).
        mutated = self.src.replace(
            "BenchmarkError::EquityMarkOutsideWindow", "BenchmarkError::EmptyEquityCurve"
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_run_window_binding(self.config, mutated)
        self.assertIn("window", str(ctx.exception).lower())

    def test_dropped_baseline_before_run_guard_is_caught(self) -> None:
        # Dropping the baseline-before-first-mark check would let a baseline at/after the
        # first mark through, breaking the strictly-increasing benchmark series and the
        # first-period interval coherence (Codex R6).
        mutated = self.src.replace("if levels[0].ts >= first_ts", "if false")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_run_window_binding(self.config, mutated)
        self.assertIn("baseline", str(ctx.exception).lower())


class SourceFailureTest(_Fixture):
    def test_source_failure_evidence(self) -> None:
        evidence = check_source_failure(self.config, self.src)
        self.assertIn("SourceFailure", evidence)

    def test_dropped_failure_variant_is_caught(self) -> None:
        # Dropping the StaleData variant would prevent a real resolver from surfacing a
        # stale-data-blocking outcome distinctly (Codex R2).
        mutated = self.src.replace("    StaleData,", "", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_source_failure(self.config, mutated)
        self.assertIn("StaleData", str(ctx.exception))

    def test_dropped_source_unavailable_variant_is_caught(self) -> None:
        mutated = self.src.replace("SourceUnavailable { failure: SourceFailure }", "", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_source_failure(self.config, mutated)
        self.assertIn("source-failure", str(ctx.exception).lower())

    def test_broadened_source_error_type_is_caught(self) -> None:
        # Widening levels() back to Result<_, BenchmarkError> would let a source return a
        # consumer-only variant the compiler should forbid (Codex R4).
        mutated = self.src.replace(
            "Result<ResolvedBenchmark, SourceFailure>",
            "Result<ResolvedBenchmark, BenchmarkError>",
        )
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_source_failure(self.config, mutated)
        self.assertIn("narrow", str(ctx.exception).lower())


class SpyDefaultTest(_Fixture):
    def test_spy_default_evidence(self) -> None:
        evidence = check_spy_default(self.config, self.src)
        self.assertIn("SPY default", evidence)

    def test_dropped_selection_resolution_is_caught(self) -> None:
        mutated = self.src.replace("selection.resolve()", "Benchmark::spy()")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_spy_default(self.config, mutated)
        self.assertIn("resolve", str(ctx.exception))


class MetricsReuseTest(_Fixture):
    def test_metrics_reuse_evidence(self) -> None:
        evidence = check_metrics_reuse(self.config, self.src)
        self.assertIn("reuses the SRS-BT-004 metric family", evidence)

    def test_dropped_metrics_import_is_caught(self) -> None:
        # Importing alpha/beta math from anywhere but the SRS-BT-004 metric family would
        # duplicate it (a divergence-from-SYS-86 hazard).
        mutated = self.src.replace("use crate::metrics::{", "use crate::xmetrics::{", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_metrics_reuse(self.config, mutated)
        self.assertIn("metric family", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("deterministic", evidence)

    def test_injected_parallelism_is_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("nondeterminism", str(ctx.exception))


class NanGuardTest(_Fixture):
    def test_nan_guard_evidence(self) -> None:
        evidence = check_nan_guard(self.config, self.src)
        self.assertIn("finite", evidence)

    def test_removed_finite_check_is_caught(self) -> None:
        mutated = self.src.replace("is_finite()", "is_nan()")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_nan_guard(self.config, mutated)
        self.assertIn("finite", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_numeric_boundary_evidence(self) -> None:
        evidence = check_numeric_boundary(self.config, self.src)
        self.assertIn("integer minor units", evidence)

    def test_float_money_input_is_caught(self) -> None:
        mutated = self.src.replace("level_minor: i64", "level_minor: f64")
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("integer minor units", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod benchmark;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod benchmark;", "pub mod renamed_benchmark;", 1)
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("benchmark", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("broker-independent", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// benchmark levels streamed through ib_insync under the hood\n"
        with self.assertRaises(BenchmarkCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable benchmark path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("benchmark_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("benchmark_check.shutil.which", return_value=None):
            with self.assertRaises(BenchmarkCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_nineteen_items(self) -> None:
        # 18 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 19)

    def test_static_evidence_is_eighteen_items(self) -> None:
        self.assertEqual(len(assert_sim_benchmark_static(load_config(), ROOT)), 18)


if __name__ == "__main__":
    unittest.main()
