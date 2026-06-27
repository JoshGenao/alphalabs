"""Contract tests for SRS-DATA-007 (provide a unified historical data access interface).

SRS-DATA-007 / SyRS SYS-27, SYS-53 / StRS SN-1.28, SN-3.03, BG-5 -- strategy code, backtests, factor
jobs, and notebooks query by symbol, date range, and resolution WITHOUT specifying the original source
provider. This slice ships the runnable READ path over the SRS-DATA-016 storage substrate -- a
source-neutral query engine in ``crates/atp-data`` (module ``query``) plus the ``data007_query_cli``
operator surface.

Mirrors ``tests/test_ingestion_idempotency_contract.py``: shells out to
``tools/unified_query_check.py``, then exercises each per-check function in-process, including negative
spot-checks that mutate the Rust source / lib.rs / Cargo.toml / CLI in memory and assert the contract
actually catches the regression (an injected provider field, a dropped query dimension, a broken
inclusive range, a query that returns a Result instead of a value, an injected re-sort, a leaked
provider output line, an injected writer lock, an injected broker dependency, a leaked vendor token). A
behavioral subprocess test then ingests fixtures via ``data016_ingest_cli`` and queries them back via
``data007_query_cli`` over a temp directory, asserting the output is source-neutral end to end.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from unified_query_check import (  # noqa: E402
    UnifiedQueryCheckError,
    assert_unified_query_static,
    cargo_source,
    check_cargo_test_smoke,
    check_cli_no_writer_lock,
    check_cli_registered,
    check_cli_source_neutral,
    check_module_reexport,
    check_no_broker_dependency,
    check_query_filter_dimensions,
    check_query_method,
    check_query_struct,
    check_query_struct_no_provider,
    check_result_source_neutral,
    check_result_struct,
    check_vendor_isolation,
    cli_source,
    lib_source,
    load_config,
    query_source,
    run_checks,
)


class UnifiedQueryScriptTest(unittest.TestCase):
    def test_srs_data_007_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/unified_query_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-007 UNIFIED-QUERY PASS", result.stdout)
        for needle in (
            "declares UnifiedHistoricalQuery carrying the acceptance query dimensions",
            "names NO origin provider",
            "declares the source-neutral UnifiedHistoricalResult",
            "UnifiedHistoricalResult is source-neutral",
            "MarketDataStore::query_unified returns the source-neutral UnifiedHistoricalResult",
            "covers exactly the three acceptance dimensions",
            "lib.rs re-exports `pub mod query;`",
            "exposes `fn cmd_query`",
            "prints a source-neutral report",
            "takes no single-writer StoreLock",
            "Cargo.toml declares no dependency on the broker/execution path",
            "query path is free of all 5 forbidden vendor SDK tokens",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = query_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.cli_src = cli_source(self.config)


class QueryStructTest(_Fixture):
    def test_query_struct_evidence(self) -> None:
        self.assertIn("acceptance query dimensions", check_query_struct(self.config, self.src))

    def test_dropped_query_dimension_is_caught(self) -> None:
        mutated = self.src.replace("    pub symbol: String,\n", "", 1)
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_struct(self.config, mutated)
        self.assertIn("symbol", str(ctx.exception))

    def test_injected_provider_field_is_caught(self) -> None:
        # A provider field on the QUERY would let a consumer specify the source -- the exact thing
        # SRS-DATA-007 forbids ("query ... without specifying the original source provider").
        mutated = self.src.replace(
            "pub struct UnifiedHistoricalQuery {\n",
            "pub struct UnifiedHistoricalQuery {\n    pub provider: String,\n",
            1,
        )
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_struct_no_provider(self.config, mutated)
        self.assertIn("provider", str(ctx.exception).lower())


class ResultStructTest(_Fixture):
    def test_result_struct_evidence(self) -> None:
        self.assertIn("source-neutral", check_result_struct(self.config, self.src))

    def test_injected_source_field_is_caught(self) -> None:
        # A source/provider field on the RESULT would let a consumer branch on a record's origin.
        mutated = self.src.replace(
            "pub struct UnifiedHistoricalResult<'a> {\n",
            "pub struct UnifiedHistoricalResult<'a> {\n    pub source: String,\n",
            1,
        )
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_result_source_neutral(self.config, mutated)
        self.assertIn("source", str(ctx.exception).lower())


class QueryMethodTest(_Fixture):
    def test_query_method_evidence(self) -> None:
        self.assertIn("never an error", check_query_method(self.config, self.src))

    def test_query_returning_result_is_caught(self) -> None:
        # Returning a Result instead of a value would turn an empty match into an error path.
        mutated = self.src.replace(
            "    ) -> UnifiedHistoricalResult<'a> {",
            "    ) -> Result<UnifiedHistoricalResult<'a>, ()> {",
            1,
        )
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_method(self.config, mutated)
        self.assertIn("Result", str(ctx.exception))


class QueryFilterTest(_Fixture):
    def test_filter_evidence(self) -> None:
        self.assertIn("three acceptance dimensions", check_query_filter_dimensions(self.config, self.src))

    def test_broken_inclusive_upper_bound_is_caught(self) -> None:
        # Making the upper bound exclusive would silently drop the end-of-range record.
        mutated = self.src.replace("key.event_ts <= self.end_ts", "key.event_ts < self.end_ts", 1)
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_filter_dimensions(self.config, mutated)
        self.assertIn("upper", str(ctx.exception).lower())

    def test_dropped_symbol_filter_is_caught(self) -> None:
        mutated = self.src.replace("key.symbol == self.symbol", "true", 1)
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_filter_dimensions(self.config, mutated)
        self.assertIn("symbol", str(ctx.exception).lower())

    def test_dropped_event_ts_sort_is_caught(self) -> None:
        # Removing the explicit event_ts ordering (reverting to the store's kind-first natural-key
        # order) would let a kind-agnostic cross-kind match return out of event_ts order.
        mutated = self.src.replace("ka.event_ts.cmp(&kb.event_ts)", "ka.cmp(kb)", 1)
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_query_filter_dimensions(self.config, mutated)
        self.assertIn("event_ts", str(ctx.exception).lower())


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        self.assertIn("pub mod query;", check_module_reexport(self.config, self.lib_src))

    def test_dropped_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod query;", "mod query;", 1)
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("pub mod query;", str(ctx.exception))


class CliRegisteredTest(_Fixture):
    def test_cli_evidence(self) -> None:
        self.assertIn("fn cmd_query", check_cli_registered(self.config, self.cli_src))

    def test_dropped_flag_is_caught(self) -> None:
        # Remove EVERY occurrence (the match arm, the USAGE line, the error message) so the flag
        # token is genuinely absent -- a flag mentioned only in help text would still satisfy the
        # static presence check, so the mutation must erase it entirely.
        mutated = self.cli_src.replace("--symbol", "--ticker")
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_cli_registered(self.config, mutated)
        self.assertIn("--symbol", str(ctx.exception))


class CliSourceNeutralTest(_Fixture):
    def test_cli_source_neutral_evidence(self) -> None:
        self.assertIn("source-neutral report", check_cli_source_neutral(self.config, self.cli_src))

    def test_injected_provider_line_is_caught(self) -> None:
        # Printing a provider line would leak the origin into the operator output.
        mutated = self.cli_src.replace(
            'println!("match_count:{}", records.len());',
            'println!("match_count:{}", records.len());\n    println!("provider:ib");',
            1,
        )
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_cli_source_neutral(self.config, mutated)
        self.assertIn("provider:", str(ctx.exception))


class CliNoWriterLockTest(_Fixture):
    def test_no_writer_lock_evidence(self) -> None:
        self.assertIn("read-only snapshot load", check_cli_no_writer_lock(self.config, self.cli_src))

    def test_injected_writer_lock_is_caught(self) -> None:
        # A read taking the single-writer lock would serialize against ingestion writers needlessly
        # (read-while-write coordination is the deferred SRS-DATA-017, not a writer lock here).
        mutated = self.cli_src.replace(
            "let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
            "let _lock = StoreLock::acquire(&dir);\n    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;",
            1,
        )
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_cli_no_writer_lock(self.config, mutated)
        self.assertIn("StoreLock::acquire", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// served straight from databento under the hood\n"
        with self.assertRaises(UnifiedQueryCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("databento", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("unified_query_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("unified_query_check.shutil.which", return_value=None):
            with self.assertRaises(UnifiedQueryCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_static_evidence_is_twelve_items(self) -> None:
        self.assertEqual(len(assert_unified_query_static(load_config(), ROOT)), 12)

    def test_run_checks_emits_thirteen_items(self) -> None:
        # 12 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 13)


class BehavioralIngestQueryTest(unittest.TestCase):
    """End-to-end: ingest fixtures via data016_ingest_cli, query them back via data007_query_cli, and
    assert the operator output is source-neutral (no provider/source/vendor line)."""

    @staticmethod
    def _cargo() -> str | None:
        return shutil.which("cargo")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_ingest_then_query_is_source_neutral(self) -> None:
        cargo = self._cargo()
        if cargo is None:
            self.skipTest("cargo not on PATH")
        build = self._run(
            cargo, "build", "-q", "-p", "atp-data",
            "--bin", "data016_ingest_cli", "--bin", "data007_query_cli",
        )
        self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
        ingest_bin = ROOT / "target" / "debug" / "data016_ingest_cli"
        query_bin = ROOT / "target" / "debug" / "data007_query_cli"

        with tempfile.TemporaryDirectory() as tmp:
            # Ingest two providers' worth of records: daily (≤ Databento) + minute (≤ IB).
            first = self._run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            second = self._run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "minute-equity-bar")
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            # Query by symbol + resolution + range -- no provider argument exists.
            queried = self._run(
                str(query_bin), "query", "--dir", tmp,
                "--symbol", "AAPL", "--resolution", "1d", "--start", "0", "--end", "9999999999",
            )
            self.assertEqual(queried.returncode, 0, queried.stdout + queried.stderr)
            lines = queried.stdout.splitlines()
            self.assertIn("symbol:AAPL", lines)
            self.assertIn("resolution:1d", lines)
            match_lines = [ln for ln in lines if ln.startswith("match_count:")]
            self.assertTrue(match_lines and int(match_lines[0].split(":", 1)[1]) > 0)
            self.assertTrue(any(ln.startswith("record.0.event_ts:") for ln in lines))
            # The output names NO provider/source/vendor anywhere.
            for ln in lines:
                self.assertFalse(
                    ln.lower().startswith(("provider:", "source:", "vendor:", "feed:")),
                    f"source-neutral output leaked an origin line: {ln!r}",
                )

            # An unknown symbol is an empty result, exit 0 (not an error).
            empty = self._run(
                str(query_bin), "query", "--dir", tmp,
                "--symbol", "NOSUCH", "--resolution", "1d", "--start", "0", "--end", "9999999999",
            )
            self.assertEqual(empty.returncode, 0, empty.stdout + empty.stderr)
            self.assertIn("match_count:0", empty.stdout.splitlines())


if __name__ == "__main__":
    unittest.main()
