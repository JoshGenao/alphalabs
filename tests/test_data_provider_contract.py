from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from data_provider_check import (  # noqa: E402
    DataProviderContractError,
    adapter_source,
    assert_data_provider_contract_static,
    check_alternative_methods,
    check_bulk_equity_methods,
    check_capability_traces,
    check_data_provider_base_trait,
    check_fundamental_methods,
    check_options_methods,
    check_provider_bindings,
    check_unified_historical_query,
    check_user_parquet_methods,
    data_provider_contract,
    load_config,
    run_checks,
)


class DataProviderContractScriptTest(unittest.TestCase):
    def test_api_6_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/data_provider_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-6 PASS", result.stdout)
        for needle in (
            "BulkEquityDataProvider declares 3 required methods",
            "FundamentalDataProvider declares 1 required methods",
            "OptionsDataProvider declares 1 required methods",
            "UserParquetDataProvider declares 1 required methods",
            "AlternativeDataProvider declares 1 required methods",
            "DataProviderAdapter base trait shared by 4 providers",
            "provider bindings verified",
            "6 API-6 capabilities trace to",
            "HistoricalDataAdapter::historical_data provides source-neutral historical query",
            "SRS-DATA-007",
        ):
            self.assertIn(needle, result.stdout)


class RequiredMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)

    def test_bulk_equity_methods_present(self) -> None:
        evidence = check_bulk_equity_methods(self.config, self.source)
        for method in (
            "download_full_universe_daily",
            "initial_historical_backfill",
            "incremental_nightly_update",
        ):
            self.assertIn(method, evidence)

    def test_fundamental_methods_present(self) -> None:
        evidence = check_fundamental_methods(self.config, self.source)
        self.assertIn("ingest_fundamentals", evidence)

    def test_options_methods_present(self) -> None:
        evidence = check_options_methods(self.config, self.source)
        self.assertIn("import_options", evidence)

    def test_user_parquet_methods_present(self) -> None:
        evidence = check_user_parquet_methods(self.config, self.source)
        self.assertIn("import_user_parquet", evidence)

    def test_alternative_methods_present(self) -> None:
        evidence = check_alternative_methods(self.config, self.source)
        self.assertIn("fetch_alternative_data", evidence)

    def test_missing_method_is_caught(self) -> None:
        mutated = self.source.replace("fn import_options(", "fn import_xx(", 1)
        with self.assertRaises(DataProviderContractError) as ctx:
            check_options_methods(self.config, mutated)
        self.assertIn("import_options", str(ctx.exception))


class ProviderBindingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)

    def test_data_provider_base_trait_shared_across_providers(self) -> None:
        evidence = check_data_provider_base_trait(self.config, self.source)
        for struct in (
            "DatabentoAdapter",
            "SharadarAdapter",
            "UserParquetAdapter",
            "FutureStubProvider",
        ):
            self.assertIn(struct, evidence)

    def test_per_provider_trait_bindings_resolve(self) -> None:
        evidence = check_provider_bindings(self.config, self.source)
        for struct in (
            "DatabentoAdapter",
            "SharadarAdapter",
            "UserParquetAdapter",
            "FutureStubProvider",
        ):
            self.assertIn(struct, evidence)
        self.assertIn("11 impls", evidence)

    def test_missing_base_trait_binding_is_caught(self) -> None:
        mutated = self.source.replace(
            "impl DataProviderAdapter for SharadarAdapter",
            "impl DataProviderXxx for SharadarAdapter",
            1,
        )
        with self.assertRaises(DataProviderContractError) as ctx:
            check_data_provider_base_trait(self.config, mutated)
        self.assertIn("SharadarAdapter", str(ctx.exception))


class CapabilityTracesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)
        self.contract = data_provider_contract(self.config)

    def test_api_6_description_capabilities_present(self) -> None:
        capabilities = {trace["capability"] for trace in self.contract["capability_traces"]}
        for required in (
            "bulk_equity_download",
            "historical_backfill",
            "incremental_update",
            "fundamentals_ingestion",
            "options_import",
            "user_parquet_import",
        ):
            self.assertIn(required, capabilities)

    def test_each_capability_resolves_to_real_method(self) -> None:
        evidence = check_capability_traces(self.config, self.source)
        self.assertIn("6 API-6 capabilities", evidence)

    def test_unified_historical_query_resolves(self) -> None:
        evidence = check_unified_historical_query(self.config, self.source)
        self.assertIn("HistoricalDataAdapter::historical_data", evidence)
        self.assertIn("SRS-DATA-007", evidence)

    def test_capability_srs_refs_subset_of_contract_srs_refs(self) -> None:
        declared = set(self.contract["srs_refs"])
        for trace in self.contract["capability_traces"]:
            self.assertIn(trace["srs_ref"], declared)


class ContractAggregateTest(unittest.TestCase):
    def test_run_checks_emits_ten_evidence_lines(self) -> None:
        evidence = run_checks()
        self.assertEqual(len(evidence), 10)

    def test_static_assertion_emits_nine_evidence_lines(self) -> None:
        config = load_config()
        evidence = assert_data_provider_contract_static(config, ROOT)
        self.assertEqual(len(evidence), 9)

    def test_runtime_services_block_traces_required_srs(self) -> None:
        contract = data_provider_contract(load_config())
        for ref in (
            "SRS-DATA-001",
            "SRS-DATA-002",
            "SRS-DATA-003",
            "SRS-DATA-004",
            "SRS-DATA-005",
            "SRS-DATA-006",
            "SRS-DATA-007",
        ):
            self.assertIn(ref, contract["srs_refs"])
        self.assertEqual(contract["requirement"], "API-6")


class ArchitectureEvidenceTest(unittest.TestCase):
    def test_architecture_check_includes_data_provider_contract_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/architecture_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "atp-adapters declares 7 data-provider methods across 5 traits",
            result.stdout,
        )
        self.assertIn("DataProviderAdapter base", result.stdout)
        self.assertIn("API-6, SRS-DATA-001..007", result.stdout)


if __name__ == "__main__":
    unittest.main()
