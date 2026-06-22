"""Contract tests for the SRS-DATA-007 close: the Python store-history binding.

SRS-DATA-007 / SyRS SYS-27, SYS-53 — strategy code, backtests, factor jobs, and notebooks query by
symbol, date range, and resolution WITHOUT specifying the original source provider. This slice ships
the FIRST in-process consumer binding (``atp_strategy.store_history.StoreBackedHistoricalData``) over
the source-neutral query engine + ``data007_query_cli`` operator surface.

Mirrors ``tests/test_unified_query_contract.py``: shells out to ``tools/store_history_check.py``, then
exercises each per-check function in-process — including negative spot-checks that mutate the binding
source in memory and assert the contract actually catches the regression (an injected provider
parameter, a dropped adjusted-mode raise, a lossy ``volume`` scaling, a ``shell=True`` invocation, a
parsed origin key). A pure-Python invariant test then asserts the binding conforms to the
``HistoricalData`` Protocol and never accepts/reads an origin field.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"
PYTHON_ROOT = ROOT / "python"
for path in (TOOLS_ROOT, PYTHON_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from store_history_check import (  # noqa: E402
    StoreHistoryCheckError,
    assert_store_history_static,
    check_count_validated,
    check_echo_validated,
    check_empty_match_is_value,
    check_kind_narrowed,
    check_list_argv_no_shell,
    check_module_and_class,
    check_money_scale,
    check_no_origin_field_read,
    check_normalization_honesty,
    check_range_and_order,
    check_round_trip,
    check_source_neutral_signature,
    check_subprocess_timeout,
    load_config,
    module_source,
    run_checks,
)


class StoreHistoryScriptTest(unittest.TestCase):
    def test_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/store_history_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-007 STORE-HISTORY BINDING PASS", result.stdout)
        for needle in (
            "concrete HistoricalData binding over the durable store",
            "carry no provider/vendor/source/feed/adapter parameter",
            "reads no origin-provider field off the result",
            "normalization honesty",
            "money math",
            "list argv and shell=False",
            "empty is a value, never an error",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = module_source(self.config)


class ModuleAndClassTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("HistoricalData binding", check_module_and_class(self.config, self.src))

    def test_renamed_class_is_caught(self) -> None:
        mutated = self.src.replace("class StoreBackedHistoricalData", "class Renamed", 1)
        with self.assertRaises(StoreHistoryCheckError) as ctx:
            check_module_and_class(self.config, mutated)
        self.assertIn("StoreBackedHistoricalData", str(ctx.exception))


class SourceNeutralSignatureTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no", check_source_neutral_signature(self.config, self.src))

    def test_injected_provider_parameter_is_caught(self) -> None:
        # A provider parameter would let a consumer specify the source — the exact thing DATA-007
        # forbids ("query ... without specifying the original source provider").
        mutated = self.src.replace(
            "        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,\n    ) -> list[Bar]:\n        \"\"\"Return the last",
            "        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,\n        provider: str = \"ib\",\n    ) -> list[Bar]:\n        \"\"\"Return the last",
            1,
        )
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError) as ctx:
            check_source_neutral_signature(self.config, mutated)
        self.assertIn("provider", str(ctx.exception).lower())


class NoOriginFieldReadTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no origin-provider field", check_no_origin_field_read(self.config, self.src))

    def test_parsed_origin_key_is_caught(self) -> None:
        mutated = self.src.replace(
            "        event_ts = record.get(\"event_ts\")\n",
            "        event_ts = record.get(\"event_ts\")\n        _origin = record[\"provider\"]\n",
            1,
        )
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError) as ctx:
            check_no_origin_field_read(self.config, mutated)
        self.assertIn("origin", str(ctx.exception).lower())


class NormalizationHonestyTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("RAW", check_normalization_honesty(self.config, self.src))

    def test_dropped_adjusted_raise_is_caught(self) -> None:
        # Remove EVERY raise (the normalization guard and the asset-class guard) so the token is
        # genuinely absent — a raise remaining anywhere would still satisfy the presence check.
        mutated = self.src.replace("raise NotImplementedError", "return None  # silent")
        with self.assertRaises(StoreHistoryCheckError):
            check_normalization_honesty(self.config, mutated)

    def test_reverting_default_to_raw_is_caught(self) -> None:
        # A RAW default would silently serve raw bars where the Protocol promises adjusted — the
        # exact silent-mismatch hazard the SPLIT_ADJUSTED default is there to prevent.
        mutated = self.src.replace(
            "normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED",
            "normalization: NormalizationMode = NormalizationMode.RAW",
        )
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_normalization_honesty(self.config, mutated)


class SubprocessTimeoutTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no hang", check_subprocess_timeout(self.config, self.src))

    def test_removed_timeout_arg_is_caught(self) -> None:
        mutated = self.src.replace("timeout=timeout", "", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_subprocess_timeout(self.config, mutated)

    def test_removed_timeout_handler_is_caught(self) -> None:
        mutated = self.src.replace("subprocess.TimeoutExpired", "ValueError")
        with self.assertRaises(StoreHistoryCheckError):
            check_subprocess_timeout(self.config, mutated)


class MoneyScaleTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("money math", check_money_scale(self.config, self.src))

    def test_scaled_volume_is_caught(self) -> None:
        # Dividing volume by the price scale would corrupt the count — only OHLC prices are scaled.
        mutated = self.src.replace(
            "volume = int(fields[_VOLUME_FIELD])",
            "volume = fields[_VOLUME_FIELD] / _PRICE_MINOR_SCALE",
            1,
        )
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_money_scale(self.config, mutated)

    def test_removed_scale_constant_is_caught(self) -> None:
        mutated = self.src.replace("_PRICE_MINOR_SCALE = 100", "PRICE = 100", 1)
        with self.assertRaises(StoreHistoryCheckError):
            check_money_scale(self.config, mutated)


class ListArgvNoShellTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("list argv", check_list_argv_no_shell(self.config, self.src))

    def test_shell_true_is_caught(self) -> None:
        mutated = self.src.replace(
            "subprocess.run(argv, check=False", "subprocess.run(argv, shell=True, check=False", 1
        )
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError) as ctx:
            check_list_argv_no_shell(self.config, mutated)
        self.assertIn("shell", str(ctx.exception).lower())


class EmptyMatchTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("empty is a value", check_empty_match_is_value(self.config, self.src))

    def test_removed_empty_guard_is_caught(self) -> None:
        mutated = self.src.replace("if match_count == 0:", "if match_count == -1:", 1)
        with self.assertRaises(StoreHistoryCheckError):
            check_empty_match_is_value(self.config, mutated)


class CountValidatedTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("count integrity", check_count_validated(self.config, self.src))

    def test_removed_count_validation_is_caught(self) -> None:
        mutated = self.src.replace("set(records) != expected", "False", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_count_validated(self.config, mutated)


class EchoValidatedTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("envelope integrity", check_echo_validated(self.config, self.src))

    def test_removed_echo_validation_is_caught(self) -> None:
        mutated = self.src.replace("echoed_symbol != symbol or echoed_resolution != resolution", "False", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_echo_validated(self.config, mutated)


class RangeAndOrderTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("range + order integrity", check_range_and_order(self.config, self.src))

    def test_removed_range_check_is_caught(self) -> None:
        mutated = self.src.replace("event_ts < start_ts or event_ts > end_ts", "False", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_range_and_order(self.config, mutated)

    def test_removed_ordering_check_is_caught(self) -> None:
        mutated = self.src.replace("event_ts < previous_ts", "False", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_range_and_order(self.config, mutated)


class KindNarrowedTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("kind narrowing", check_kind_narrowed(self.config, self.src))

    def test_removed_kind_arg_is_caught(self) -> None:
        mutated = self.src.replace('"--kind",\n            kind,', "", 1)
        self.assertNotEqual(mutated, self.src, "mutation did not apply")
        with self.assertRaises(StoreHistoryCheckError):
            check_kind_narrowed(self.config, mutated)


class AggregateEvidenceTest(unittest.TestCase):
    def test_static_evidence_is_twelve_items(self) -> None:
        self.assertEqual(len(assert_store_history_static(load_config(), ROOT)), 12)

    def test_run_checks_emits_thirteen_items(self) -> None:
        # 12 static + 1 round-trip (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 13)


class RoundTripCargoGateTest(unittest.TestCase):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("store_history_check.shutil.which", return_value=None):
            evidence = check_round_trip(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("store_history_check.shutil.which", return_value=None):
            with self.assertRaises(StoreHistoryCheckError) as ctx:
                check_round_trip(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class ProtocolConformanceTest(unittest.TestCase):
    def test_binding_is_a_historical_data(self) -> None:
        from atp_strategy import HistoricalData
        from atp_strategy.store_history import StoreBackedHistoricalData

        binding = StoreBackedHistoricalData(store_dir="/tmp/x")
        self.assertIsInstance(binding, HistoricalData)

    def test_binding_reads_only_event_ts_and_value_fields(self) -> None:
        # With a fake runner returning a source-neutral response, the binding produces bars and never
        # depends on an origin field — there is no provider parameter to pass, by construction.
        import datetime as _dt
        import subprocess as _sp

        from atp_strategy import NormalizationMode
        from atp_strategy.store_history import StoreBackedHistoricalData

        def runner(argv: list[str], *, timeout: float) -> _sp.CompletedProcess[str]:
            # Echo the requested symbol/resolution/start/end (like the real CLI) so the envelope is
            # valid; event_ts sits inside the requested range.
            sym = argv[argv.index("--symbol") + 1]
            res = argv[argv.index("--resolution") + 1]
            start = argv[argv.index("--start") + 1]
            end = argv[argv.index("--end") + 1]
            stdout = (
                f"symbol:{sym}\nresolution:{res}\nstart:{start}\nend:{end}\nkind:any\nmatch_count:1\n"
                "record.0.event_ts:1700000000\nrecord.0.option_contract:-\n"
                "record.0.field.open:9950\nrecord.0.field.high:10075\nrecord.0.field.low:9910\n"
                "record.0.field.close:10000\nrecord.0.field.volume:100000\n"
            )
            return _sp.CompletedProcess(argv, 0, stdout, "")

        # A fixed clock so end_ts (and the echo) are deterministic and the event_ts is in range.
        binding = StoreBackedHistoricalData(
            store_dir="/tmp/x", runner=runner, clock=lambda: _dt.datetime(2024, 1, 1, tzinfo=_dt.timezone.utc)
        )
        (bar,) = binding.get_bars(
            "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.RAW
        )
        self.assertEqual(bar.close, 100.0)
        self.assertEqual(bar.volume, 100000)


if __name__ == "__main__":
    unittest.main()
