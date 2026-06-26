"""Contract tests for SRS-BT-001 (runnable backtest engine).

SRS-BT-001 / SyRS SYS-14 / SYS-43a / StRS SN-1.02 / SN-1.13 / C-4 — a backtest
of Python strategies against stored data and user-uploaded Parquet data over
configurable date ranges. This slice ships the runnable deterministic engine in
``crates/atp-simulation`` (module ``backtest``); the launch / data / strategy
halves stay deferred (feature_list.json keeps ``passes:false``).

Mirrors ``tests/test_subscription_fanout_contract.py``: shells out to
``tools/backtest_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source in memory and assert
the contract actually catches the regression (dropped field, leaked vendor
field, a float in the money path, removed range validation, removed strategy
drive, leaked vendor token).
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

from backtest_check import (  # noqa: E402
    BacktestCheckError,
    assert_backtest_static,
    backtest_source,
    check_bar_source_port,
    check_bar_struct,
    check_cargo_test_smoke,
    check_data_source_enum,
    check_date_range,
    check_engine,
    check_error_enum,
    check_money_invariant,
    check_result_struct,
    check_strategy_port,
    check_vendor_isolation,
    load_config,
    run_checks,
)


class BacktestScriptTest(unittest.TestCase):
    def test_srs_bt_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/backtest_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-001 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "BacktestDataSource with 2 launch sources (SystemData, UploadedData)",
            "launched from system data OR uploaded data",
            "fails closed on start > end (BacktestError::InvalidDateRange)",
            "SRS-BT-001 configurable date range",
            "BacktestBar with the 3 fields (symbol, ts, close_minor; integer close_minor)",
            "BacktestResult with the 6 result fields",
            "BacktestError with 11 fail-closed variants (EmptySymbol, InvalidDateRange, UnexpectedSymbol, NonPositivePrice, DuplicateBar, EmptyData, StrategyFailed, Overflow, TooManyBars, DataSourceMismatch, SourceUnavailable)",
            "port trait BarSource with 2 method (source, bars); bars takes a max_bars read bound",
            "deferred user-uploaded Parquet reader seam (the system-catalog reader is landed by store_bar_source::StoreBarSource, SRS-DATA-007)",
            "fallible port trait BacktestStrategy with 1 method (on_bar) returning Result<i64, BacktestError>",
            "deferred Python strategy execution boundary",
            "BacktestEngine::run validates the range",
            "validates data-source provenance (`request.data_source != source.source()` -> `BacktestError::DataSourceMismatch`)",
            "bounds the replay size (`BacktestError::TooManyBars` / MAX_BACKTEST_BARS)",
            "guards the source trust boundary (`BacktestError::UnexpectedSymbol` + `BacktestError::DuplicateBar` + `BacktestError::NonPositivePrice`)",
            "restricts replay to the window (`bars.retain(`)",
            "replays deterministically (`sort_by_key`)",
            "propagates a strategy failure (`strategy.on_bar(bar, position)?`)",
            "money math is integer-only: no f64, 5 i64 minor-unit fields, overflow-safe checked_notional",
            "free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-001 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = backtest_source(self.config)


class DataSourceEnumTest(_Fixture):
    def test_launch_sources_present(self) -> None:
        evidence = check_data_source_enum(self.config, self.src)
        self.assertIn("SystemData, UploadedData", evidence)

    def test_dropped_uploaded_variant_is_caught(self) -> None:
        mutated = self.src.replace("    UploadedData,", "", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_data_source_enum(self.config, mutated)
        self.assertIn("UploadedData", str(ctx.exception))


class DateRangeTest(_Fixture):
    def test_configurable_range_evidence(self) -> None:
        evidence = check_date_range(self.config, self.src)
        self.assertIn("configurable date range", evidence)

    def test_removed_inverted_check_is_caught(self) -> None:
        mutated = self.src.replace("if self.start > self.end {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_date_range(self.config, mutated)
        self.assertIn("self.start > self.end", str(ctx.exception))


class BarStructTest(_Fixture):
    def test_required_fields_present(self) -> None:
        evidence = check_bar_struct(self.config, self.src)
        self.assertIn("symbol, ts, close_minor", evidence)

    def test_dropped_close_field_is_caught(self) -> None:
        mutated = self.src.replace("    pub close_minor: i64,", "", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_bar_struct(self.config, mutated)
        self.assertIn("close_minor", str(ctx.exception))

    def test_leaked_broker_field_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub struct BacktestBar {\n    pub symbol: String,",
            "pub struct BacktestBar {\n    pub broker: String,\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(BacktestCheckError) as ctx:
            check_bar_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))


class ResultStructTest(_Fixture):
    def test_required_fields_present(self) -> None:
        evidence = check_result_struct(self.config, self.src)
        self.assertIn("trade_log, equity_curve, final_equity_minor", evidence)

    def test_dropped_trade_log_field_is_caught(self) -> None:
        mutated = self.src.replace("    pub trade_log: Vec<Fill>,", "", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_result_struct(self.config, mutated)
        self.assertIn("trade_log", str(ctx.exception))

    def test_leaked_vendor_field_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub struct BacktestResult {\n    pub data_source: BacktestDataSource,",
            "pub struct BacktestResult {\n    pub vendor: String,\n    pub data_source: BacktestDataSource,",
            1,
        )
        with self.assertRaises(BacktestCheckError) as ctx:
            check_result_struct(self.config, mutated)
        self.assertIn("vendor", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_fail_closed_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in (
            "EmptySymbol",
            "InvalidDateRange",
            "UnexpectedSymbol",
            "NonPositivePrice",
            "DuplicateBar",
            "EmptyData",
            "StrategyFailed",
            "Overflow",
            "TooManyBars",
            "DataSourceMismatch",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_empty_data_variant_is_caught(self) -> None:
        mutated = self.src.replace("    EmptyData,", "", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("EmptyData", str(ctx.exception))


class BarSourcePortTest(_Fixture):
    def test_bars_method_present(self) -> None:
        evidence = check_bar_source_port(self.config, self.src)
        self.assertIn("bars", evidence)

    def test_renamed_bars_method_is_caught(self) -> None:
        # The trait declaration is the first `fn bars(` in the file.
        mutated = self.src.replace("fn bars(", "fn dropped_bars(", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_bar_source_port(self.config, mutated)
        self.assertIn("bars", str(ctx.exception))

    def test_dropped_source_identity_method_is_caught(self) -> None:
        # The trait's `fn source(` is what ties a source to its catalog identity
        # for the provenance check; dropping it must be caught.
        mutated = self.src.replace("fn source(", "fn dropped_source(", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_bar_source_port(self.config, mutated)
        self.assertIn("source", str(ctx.exception))

    def test_unbounded_bars_read_is_caught(self) -> None:
        # Dropping the max_bars read bound lets a source materialize an unbounded
        # response — the round-6 [high] finding. The trait decl is the first
        # `max_bars: usize`.
        mutated = self.src.replace("max_bars: usize", "capless: usize", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_bar_source_port(self.config, mutated)
        self.assertIn("max_bars", str(ctx.exception))


class StrategyPortTest(_Fixture):
    def test_on_bar_method_present(self) -> None:
        evidence = check_strategy_port(self.config, self.src)
        self.assertIn("on_bar", evidence)

    def test_renamed_on_bar_method_is_caught(self) -> None:
        # The trait declaration is the first `fn on_bar(` in the file.
        mutated = self.src.replace("fn on_bar(", "fn dropped_on_bar(", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_strategy_port(self.config, mutated)
        self.assertIn("on_bar", str(ctx.exception))

    def test_non_fallible_strategy_port_is_caught(self) -> None:
        # Reverting on_bar to a bare i64 return loses the Python-failure channel
        # (the round-2 finding). The trait decl is the only `-> Result<...>;`.
        mutated = self.src.replace("-> Result<i64, BacktestError>;", "-> i64;", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_strategy_port(self.config, mutated)
        self.assertIn("fallible", str(ctx.exception))


class EngineTest(_Fixture):
    def test_engine_evidence(self) -> None:
        evidence = check_engine(self.config, self.src)
        self.assertIn("validates the range", evidence)

    def test_removed_range_restriction_is_caught(self) -> None:
        mutated = self.src.replace("bars.retain(", "bars.no_retain(", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("range_restrict_token", str(ctx.exception))

    def test_removed_strategy_drive_is_caught(self) -> None:
        mutated = self.src.replace("strategy.on_bar(", "strategy.no_call(", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("strategy_drive_token", str(ctx.exception))

    def test_removed_symbol_guard_is_caught(self) -> None:
        # Dropping the foreign-symbol guard is the trust-boundary regression
        # Codex flagged: a mixed-symbol source would silently trade.
        mutated = self.src.replace("return Err(BacktestError::UnexpectedSymbol {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("symbol_guard_token", str(ctx.exception))

    def test_removed_price_guard_is_caught(self) -> None:
        # Dropping the non-positive-price guard lets a negative close fabricate
        # cash via checked_sub — the second [high] Codex finding.
        mutated = self.src.replace("return Err(BacktestError::NonPositivePrice {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("price_guard_token", str(ctx.exception))

    def test_removed_duplicate_guard_is_caught(self) -> None:
        # Dropping the duplicate-timestamp guard double-fills one instant and
        # makes replay order-dependent — round-2 [high] finding.
        mutated = self.src.replace("return Err(BacktestError::DuplicateBar {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("duplicate_guard_token", str(ctx.exception))

    def test_swallowed_strategy_failure_is_caught(self) -> None:
        # Dropping the `?` swallows a Python strategy failure as a 0 delta — the
        # round-2 [high] finding. strategy_drive_token still matches (the call is
        # present); strategy_fallible_token (the propagating `?`) is gone.
        mutated = self.src.replace(
            "strategy.on_bar(bar, position)?", "strategy.on_bar(bar, position)", 1
        )
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("strategy_fallible_token", str(ctx.exception))

    def test_removed_row_limit_guard_is_caught(self) -> None:
        # Dropping the replay-size cap lets a huge upload exhaust memory before
        # any guard fires — round-3 [high] finding.
        mutated = self.src.replace("return Err(BacktestError::TooManyBars {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("row_limit_token", str(ctx.exception))

    def test_removed_row_limit_const_is_caught(self) -> None:
        mutated = self.src.replace("const MAX_BACKTEST_BARS", "const RENAMED_CAP", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("MAX_BACKTEST_BARS", str(ctx.exception))

    def test_removed_provenance_guard_is_caught(self) -> None:
        # Dropping the data-source provenance check lets a run misreport which
        # dataset it ran against — round-5 [high] finding.
        mutated = self.src.replace("if request.data_source != source.source() {", "if false {", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("provenance_check_token", str(ctx.exception))

    def test_engine_not_passing_cap_to_source_is_caught(self) -> None:
        # If the engine stops passing self.max_bars into source.bars, a source can
        # no longer bound its own read — round-6 [high] finding.
        mutated = self.src.replace("&request.range, self.max_bars)", "&request.range)", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_engine(self.config, mutated)
        self.assertIn("source_cap_pass_token", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.src)
        self.assertIn("integer-only", evidence)

    def test_injected_float_in_money_path_is_caught(self) -> None:
        # Switching a minor-unit field to f64 is the exact money-correctness
        # regression the invariant forbids.
        mutated = self.src.replace("pub close_minor: i64,", "pub close_minor: f64,", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_removed_overflow_helper_is_caught(self) -> None:
        mutated = self.src.replace("fn checked_notional", "fn unchecked_notional", 1)
        with self.assertRaises(BacktestCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("checked_notional", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// uses interactive_brokers under the hood\n"
        with self.assertRaises(BacktestCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("interactive_brokers", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable Rust engine must compile where it matters (init.sh)."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("backtest_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("backtest_check.shutil.which", return_value=None):
            with self.assertRaises(BacktestCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_fourteen_items(self) -> None:
        # 13 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 14)

    def test_static_evidence_is_thirteen_items(self) -> None:
        self.assertEqual(len(assert_backtest_static(load_config(), ROOT)), 13)


if __name__ == "__main__":
    unittest.main()
