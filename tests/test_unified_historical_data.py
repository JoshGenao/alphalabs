from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from historical_data_check import (  # noqa: E402
    HistoricalDataCheckError,
    adapter_source,
    assert_unified_historical_data_static,
    check_asset_class_enum,
    check_normalization_enum,
    check_python_protocol,
    check_request_struct,
    check_result_envelope,
    check_trait_signature,
    load_config,
    python_protocol_source,
    run_checks,
    unified_block,
)


class HistoricalDataCheckScriptTest(unittest.TestCase):
    def test_api_7_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/historical_data_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-7 PASS", result.stdout)
        for needle in (
            "HistoricalDataRequest declares 6 query fields",
            "asset_class, normalization_mode",
            "HistoricalQueryResult envelope is source-neutral",
            "rejects 5 forbidden vendor fields",
            "AssetClass declares 5 variants",
            "NormalizationMode declares 4 SRS-DATA-012 variants",
            "HistoricalDataAdapter::historical_data returns AdapterResult<HistoricalQueryResult>",
            "SRS-DATA-007 + SRS-DATA-012",
            "atp_strategy.HistoricalData.get_bars accepts 6 parameters",
            "re-exports NormalizationMode",
        ):
            self.assertIn(needle, result.stdout)


class RustSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)

    def test_request_struct_carries_all_query_fields(self) -> None:
        evidence = check_request_struct(self.config, self.source)
        for field in (
            "symbol",
            "start",
            "end",
            "resolution",
            "asset_class",
            "normalization_mode",
        ):
            self.assertIn(field, evidence)

    def test_result_envelope_is_source_neutral(self) -> None:
        evidence = check_result_envelope(self.config, self.source)
        self.assertIn("source-neutral", evidence)
        for field in ("symbol", "asset_class", "normalization_mode", "bars"):
            self.assertIn(field, evidence)

    def test_result_envelope_rejects_forbidden_field(self) -> None:
        # Inject a forbidden vendor field into the envelope and assert the
        # check raises with a message naming the leak.
        mutated = self.source.replace(
            "pub struct HistoricalQueryResult {\n    pub symbol: String,",
            "pub struct HistoricalQueryResult {\n    pub provider: String,\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(HistoricalDataCheckError) as ctx:
            check_result_envelope(self.config, mutated)
        self.assertIn("provider", str(ctx.exception))

    def test_asset_class_enum_lists_phase1_variants(self) -> None:
        evidence = check_asset_class_enum(self.config, self.source)
        for variant in ("Equity", "Option", "Future", "Etf", "Index"):
            self.assertIn(variant, evidence)

    def test_normalization_enum_covers_srs_data_012(self) -> None:
        evidence = check_normalization_enum(self.config, self.source)
        for variant in ("Raw", "SplitAdjusted", "FullyAdjusted", "TotalReturn"):
            self.assertIn(variant, evidence)

    def test_missing_normalization_variant_is_caught(self) -> None:
        mutated = self.source.replace("TotalReturn,", "TotalReturnX,", 1)
        with self.assertRaises(HistoricalDataCheckError) as ctx:
            check_normalization_enum(self.config, mutated)
        self.assertIn("TotalReturn", str(ctx.exception))

    def test_trait_signature_returns_source_neutral_envelope(self) -> None:
        evidence = check_trait_signature(self.config, self.source)
        self.assertIn("HistoricalDataAdapter::historical_data", evidence)
        self.assertIn("AdapterResult<HistoricalQueryResult>", evidence)
        self.assertIn("SRS-DATA-007 + SRS-DATA-012", evidence)


class PythonProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.python_source = python_protocol_source(self.config)

    def test_get_bars_accepts_asset_class_and_normalization(self) -> None:
        evidence = check_python_protocol(self.config, self.python_source)
        for needle in ("asset_class", "normalization", "NormalizationMode"):
            self.assertIn(needle, evidence)

    def test_missing_python_parameter_is_caught(self) -> None:
        mutated = self.python_source.replace(
            "asset_class: AssetClass = AssetClass.EQUITY,", "", 1
        )
        with self.assertRaises(HistoricalDataCheckError) as ctx:
            check_python_protocol(self.config, mutated)
        self.assertIn("asset_class", str(ctx.exception))


class ContractAggregateTest(unittest.TestCase):
    def test_run_checks_emits_seven_evidence_lines(self) -> None:
        evidence = run_checks()
        self.assertEqual(len(evidence), 7)

    def test_static_assertion_emits_six_evidence_lines(self) -> None:
        config = load_config()
        evidence = assert_unified_historical_data_static(config, ROOT)
        self.assertEqual(len(evidence), 6)

    def test_runtime_services_block_traces_required_srs(self) -> None:
        block = unified_block(load_config())
        self.assertEqual(block["requirement"], "API-7")
        for ref in ("SRS-DATA-007", "SRS-DATA-012"):
            self.assertIn(ref, block["srs_refs"])
        self.assertEqual(block["request_struct"], "HistoricalDataRequest")
        self.assertEqual(block["result_struct"], "HistoricalQueryResult")
        for consumer in ("strategy", "backtest", "factor", "research"):
            self.assertIn(consumer, block["consumers"])


class ArchitectureEvidenceTest(unittest.TestCase):
    def test_architecture_check_includes_unified_query_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/architecture_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "atp-adapters unified historical query carries 6 request fields",
            result.stdout,
        )
        self.assertIn(
            "5 asset classes / 4 normalization modes for 4 consumers",
            result.stdout,
        )
        self.assertIn("API-7, SRS-DATA-007 + SRS-DATA-012", result.stdout)


if __name__ == "__main__":
    unittest.main()
