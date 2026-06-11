"""Contract tests for SRS-BT-009 (persist completed backtest results).

SRS-BT-009 / SyRS SYS-21, SYS-79 / StRS SN-1.02, SN-1.04 -- persist a completed backtest's
parameters, metrics, trade log, equity curve, benchmark comparison, code version, and
timestamp, queryable by strategy, date range, and parameter set. This slice ships the
deterministic record + store + query + codec surface in ``crates/atp-simulation`` (module
``backtest_store``), wrapping the SRS-BT-004 metric family and the SRS-BT-005 benchmark
comparison; the deferred halves (the SSD/NAS durable tier via SRS-DATA-008, the
dashboard/report rendering via SRS-UI-004 / SRS-API, and the orchestrated run producer via
SRS-BT-001) keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_benchmark_contract.py``: shells out to
``tools/backtest_store_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in memory
and assert the contract actually catches the regression (a dropped record field, a renamed
identity newtype, a renamed query fn, a dropped duplicate-run-id guard, a dropped codec
finite guard, a dropped error variant, a money-into-float input, an injected nondeterminism
source, a divergent metric shape, a dropped lib re-export, an injected broker dependency, a
leaked vendor token).
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

from backtest_store_check import (  # noqa: E402
    BacktestStoreCheckError,
    assert_backtest_store_static,
    cargo_source,
    check_cargo_test_smoke,
    check_codec,
    check_determinism,
    check_error_enum,
    check_from_result,
    check_identity_newtypes,
    check_insert,
    check_metrics_reuse,
    check_module_reexport,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_query_fns,
    check_record_coherence,
    check_record_struct,
    check_store_struct,
    check_strategy_parameters,
    check_vendor_isolation,
    lib_source,
    load_config,
    run_checks,
    store_source,
)


class BacktestStoreScriptTest(unittest.TestCase):
    def test_srs_bt_009_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/backtest_store_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-009 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "declares BacktestRecord bundling the seven SRS-BT-009 artifacts",
            "declares the RunId and CodeVersion identity newtypes",
            "declares the SAFE producer constructor BacktestRecord::from_result",
            "declares BacktestResultStore -- the queryable, persistable collection",
            "answers the SRS-BT-009 query axes",
            "query_by_run_window: the backtest's tested period request.range",
            "declares StrategyParameters (the tuned parameter set)",
            "query_by_parameter_set filters on record.parameters",
            "validate_record is fail-closed on trade-log coherence",
            "per-metric DOMAIN bounds (win rate / max drawdown in [0, 1]",
            "rejects a duplicate run id (DuplicateRunId) and inserts in canonical",
            "serialize/restore is a deterministic, dependency-free text codec",
            "declares StoreError with 6 fail-closed variants",
            "keeps trade-log/equity money in integer minor units",
            "backtest_store is deterministic",
            "persists the SRS-BT-004 PerformanceMetrics family and the SRS-BT-005 "
            "BenchmarkComparison",
            "lib.rs re-exports `pub mod backtest_store;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "backtest_store module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-009 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = store_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class RecordStructTest(_Fixture):
    def test_record_evidence(self) -> None:
        evidence = check_record_struct(self.config, self.src)
        self.assertIn("seven SRS-BT-009 artifacts", evidence)

    def test_dropped_timestamp_field_is_caught(self) -> None:
        mutated = self.src.replace("completed_at_ts: u64,", "finished_at_ts: u64,", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_struct(self.config, mutated)
        self.assertIn("completed_at_ts", str(ctx.exception))

    def test_dropped_comparison_field_is_caught(self) -> None:
        mutated = self.src.replace("comparison: BenchmarkComparison,", "", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_struct(self.config, mutated)
        self.assertIn("comparison", str(ctx.exception))


class IdentityNewtypesTest(_Fixture):
    def test_identity_evidence(self) -> None:
        evidence = check_identity_newtypes(self.config, self.src)
        self.assertIn("identity newtypes", evidence)

    def test_renamed_run_id_newtype_is_caught(self) -> None:
        mutated = self.src.replace("pub struct RunId", "pub struct RunIdent")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_identity_newtypes(self.config, mutated)
        self.assertIn("RunId", str(ctx.exception))


class FromResultTest(_Fixture):
    def test_from_result_evidence(self) -> None:
        evidence = check_from_result(self.config, self.src)
        self.assertIn("never be persisted under false provenance", evidence)

    def test_dropped_data_source_provenance_guard_is_caught(self) -> None:
        mutated = self.src.replace("result.data_source != request.data_source", "false")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_from_result(self.config, mutated)
        self.assertIn("data source", str(ctx.exception).lower())

    def test_artifacts_not_bound_to_result_is_caught(self) -> None:
        # Passing independent artifacts instead of taking them from the BacktestResult would
        # un-bind the persisted trade log from its producing run.
        mutated = self.src.replace("result.trade_log.clone()", "trade_log")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_from_result(self.config, mutated)
        self.assertIn("BacktestResult", str(ctx.exception))


class StoreStructTest(_Fixture):
    def test_store_evidence(self) -> None:
        evidence = check_store_struct(self.config, self.src)
        self.assertIn("queryable, persistable collection", evidence)

    def test_renamed_records_field_is_caught(self) -> None:
        mutated = self.src.replace("records: Vec<BacktestRecord>,", "rows: Vec<BacktestRecord>,", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_store_struct(self.config, mutated)
        self.assertIn("records", str(ctx.exception))


class QueryFnsTest(_Fixture):
    def test_query_evidence(self) -> None:
        evidence = check_query_fns(self.config, self.src)
        self.assertIn("the SYS-21 date range (query_by_run_window", evidence)

    def test_renamed_parameter_set_query_is_caught(self) -> None:
        mutated = self.src.replace("pub fn query_by_parameter_set", "pub fn query_by_params")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_query_fns(self.config, mutated)
        self.assertIn("query_by_parameter_set", str(ctx.exception))

    def test_run_window_axis_uses_request_range_overlap(self) -> None:
        # SYS-21: the date-range axis must use run-window overlap (request.range), so dropping
        # the overlap helper everywhere (e.g. filtering completion time instead) is caught.
        mutated = self.src.replace("windows_overlap", "ranges_touch")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_query_fns(self.config, mutated)
        self.assertIn("run-window", str(ctx.exception).lower())


class StrategyParametersTest(_Fixture):
    def test_strategy_parameters_evidence(self) -> None:
        evidence = check_strategy_parameters(self.config, self.src)
        self.assertIn("told apart", evidence)

    def test_querying_request_instead_of_parameters_is_caught(self) -> None:
        # The high-severity scope gap: querying the launch BacktestRequest instead of the tuned
        # parameter set cannot tell two sweep points apart.
        mutated = self.src.replace("record.parameters == *params", "record.request == *params")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_strategy_parameters(self.config, mutated)
        self.assertIn("parameter set", str(ctx.exception))

    def test_dropped_duplicate_key_guard_is_caught(self) -> None:
        mutated = self.src.replace("duplicate strategy parameter key", "ignored")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_strategy_parameters(self.config, mutated)
        self.assertIn("duplicate", str(ctx.exception).lower())


class RecordCoherenceTest(_Fixture):
    def test_coherence_evidence(self) -> None:
        evidence = check_record_coherence(self.config, self.src)
        self.assertIn("trade-log coherence", evidence)

    def test_dropped_fill_symbol_guard_is_caught(self) -> None:
        mutated = self.src.replace("fill.symbol != record.request.symbol", "false")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_coherence(self.config, mutated)
        self.assertIn("symbol", str(ctx.exception).lower())

    def test_dropped_benchmark_identity_guard_is_caught(self) -> None:
        mutated = self.src.replace(
            "record.metrics.benchmark_symbol != record.comparison.benchmark_symbol", "false"
        )
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_coherence(self.config, mutated)
        self.assertIn("benchmark", str(ctx.exception).lower())

    def test_dropped_producer_invariant_is_caught(self) -> None:
        # Dropping the non-empty-equity-curve guard would let a record persist metrics that
        # metrics::compute could never have produced (it rejects an empty curve).
        mutated = self.src.replace("empty equity curve", "ok empty curve")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_coherence(self.config, mutated)
        self.assertIn("producer", str(ctx.exception).lower())

    def test_dropped_excess_return_identity_is_caught(self) -> None:
        # Dropping the excess = strategy - benchmark identity would let a record claim an
        # excess_return that contradicts its own total returns.
        mutated = self.src.replace("strategy - benchmark - excess", "0.0_f64")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_coherence(self.config, mutated)
        self.assertIn("excess_return", str(ctx.exception))

    def test_dropped_metric_domain_bound_is_caught(self) -> None:
        # Dropping the win-rate domain bound would let an impossible win_rate > 1 persist.
        mutated = self.src.replace("win rate outside [0, 1]", "ok win rate")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_record_coherence(self.config, mutated)
        self.assertIn("out-of-domain", str(ctx.exception).lower())


class InsertTest(_Fixture):
    def test_insert_evidence(self) -> None:
        evidence = check_insert(self.config, self.src)
        self.assertIn("rejects a duplicate run id", evidence)

    def test_dropped_duplicate_guard_is_caught(self) -> None:
        # Removing the duplicate-run-id rejection would let two results share an identity.
        # (The token appears in both insert and restore, so drop it everywhere.)
        mutated = self.src.replace("StoreError::DuplicateRunId", "StoreError::CorruptRecord")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_insert(self.config, mutated)
        self.assertIn("DuplicateRunId", str(ctx.exception))

    def test_dropped_canonical_order_is_caught(self) -> None:
        mutated = self.src.replace("order_key", "ordering_key")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_insert(self.config, mutated)
        self.assertIn("canonical order", str(ctx.exception))


class CodecTest(_Fixture):
    def test_codec_evidence(self) -> None:
        evidence = check_codec(self.config, self.src)
        self.assertIn("deterministic, dependency-free text codec", evidence)

    def test_dropped_checksum_first_is_caught(self) -> None:
        # Verifying the checksum AFTER building state would let a tampered blob restore
        # partial fabricated results before the integrity check.
        mutated = self.src.replace("if checksum(body) != stored_checksum", "if false")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("checksum", str(ctx.exception).lower())

    def test_dropped_finite_guard_is_caught(self) -> None:
        mutated = self.src.replace("is_finite()", "is_nan()")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("finite", str(ctx.exception).lower())

    def test_inexact_ratio_encoding_is_caught(self) -> None:
        # Encoding a ratio via Display instead of to_bits would lose precision / determinism.
        mutated = self.src.replace("to_bits()", "to_string()")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("to_bits", str(ctx.exception))

    def test_reintroduced_unbounded_alloc_is_caught(self) -> None:
        # Pre-allocating a vector from the untrusted decoded count would let a checksum-valid
        # oversized count abort on an OOM allocation instead of failing closed.
        mutated = self.src.replace(
            "let mut trade_log = Vec::new();",
            "let mut trade_log = Vec::with_capacity(trade_count);",
        )
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("untrusted count", str(ctx.exception).lower())

    def test_dropped_bulk_restore_sort_is_caught(self) -> None:
        # Reverting to a per-record sorted insert would make restore O(n^2) for a large history.
        mutated = self.src.replace("records.sort_by", "records.iter")
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("single sort", str(ctx.exception).lower())


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in ("DuplicateRunId", "NonFiniteRatio", "ChecksumMismatch"):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.src.replace("    ChecksumMismatch,", "", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("ChecksumMismatch", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_numeric_evidence(self) -> None:
        evidence = check_numeric_boundary(self.config, self.src)
        self.assertIn("integer minor units", evidence)

    def test_money_into_float_is_caught(self) -> None:
        mutated = self.src.replace("i128::from(point.equity_minor)", "point.equity_minor as f64", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("integer minor units", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("deterministic", evidence)

    def test_injected_parallelism_is_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("nondeterminism", str(ctx.exception))


class MetricsReuseTest(_Fixture):
    def test_metrics_reuse_evidence(self) -> None:
        evidence = check_metrics_reuse(self.config, self.src)
        self.assertIn("SRS-BT-004 PerformanceMetrics family", evidence)

    def test_divergent_metric_shape_is_caught(self) -> None:
        mutated = self.src.replace(
            "use crate::metrics::PerformanceMetrics", "use crate::xmetrics::PerformanceMetrics", 1
        )
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_metrics_reuse(self.config, mutated)
        self.assertIn("PerformanceMetrics", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod backtest_store;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod backtest_store;", "pub mod renamed_store;", 1)
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("backtest_store", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("broker-independent", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// backtest records mirrored to ib_insync under the hood\n"
        with self.assertRaises(BacktestStoreCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable persistence path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("backtest_store_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("backtest_store_check.shutil.which", return_value=None):
            with self.assertRaises(BacktestStoreCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_seventeen_items(self) -> None:
        # 16 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 17)

    def test_static_evidence_is_sixteen_items(self) -> None:
        self.assertEqual(len(assert_backtest_store_static(load_config(), ROOT)), 16)


if __name__ == "__main__":
    unittest.main()
