from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"
ADAPTER_SOURCE = ROOT / "crates" / "atp-adapters" / "src" / "lib.rs"
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from adapter_check import (  # noqa: E402
    AdapterContractError,
    adapter_contract,
    adapter_source,
    assert_adapter_contract_static,
    check_brokerage_methods,
    check_historical_methods,
    check_interactive_brokers_version,
    check_market_data_methods,
    check_version_default_method,
    check_version_struct,
    load_config,
    run_checks,
)


class AdapterContractScriptTest(unittest.TestCase):
    def test_api_5_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/adapter_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-5 PASS", result.stdout)
        for needle in (
            "BrokerageAdapter declares 4 required methods",
            "MarketDataAdapter declares 1 required methods",
            "HistoricalDataAdapter declares 1 required methods",
            "AdapterVersion declares 3 fields",
            "AdapterBoundary exposes default `fn version(&self) -> AdapterVersion`",
            "InteractiveBrokersAdapter overrides `version()`",
            "IB TWS API version 10.19.4",
        ):
            self.assertIn(needle, result.stdout)


class RequiredMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)
        self.contract = adapter_contract(self.config)

    def test_brokerage_methods_present(self) -> None:
        evidence = check_brokerage_methods(self.config, self.source)
        for method in ("submit_order", "cancel_order", "account_status", "positions"):
            self.assertIn(method, evidence)

    def test_market_data_methods_present(self) -> None:
        evidence = check_market_data_methods(self.config, self.source)
        self.assertIn("subscribe_market_data", evidence)

    def test_historical_methods_present(self) -> None:
        evidence = check_historical_methods(self.config, self.source)
        self.assertIn("historical_data", evidence)

    def test_missing_method_is_caught(self) -> None:
        mutated = self.source.replace("fn cancel_order(", "fn cancel_xx(", 1)
        with self.assertRaises(AdapterContractError) as ctx:
            check_brokerage_methods(self.config, mutated)
        self.assertIn("cancel_order", str(ctx.exception))


class VersionedDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.source = adapter_source(self.config)

    def test_version_struct_declared(self) -> None:
        evidence = check_version_struct(self.config, self.source)
        for field in ("adapter_version", "protocol_version", "protocol_label"):
            self.assertIn(field, evidence)

    def test_version_method_on_adapter_boundary(self) -> None:
        evidence = check_version_default_method(self.config, self.source)
        self.assertIn("AdapterBoundary", evidence)
        self.assertIn("version", evidence)

    def test_interactive_brokers_documents_tws_api_version(self) -> None:
        evidence = check_interactive_brokers_version(self.config, self.source)
        self.assertIn("IB TWS API", evidence)
        self.assertIn("10.19.4", evidence)
        self.assertIn("INTERACTIVE_BROKERS_TWS_API_VERSION", evidence)

    def test_protocol_version_constant_must_match_config(self) -> None:
        mutated = self.source.replace(
            'pub const INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "10.19.4"',
            'pub const INTERACTIVE_BROKERS_TWS_API_VERSION: &str = "9.99"',
        )
        with self.assertRaises(AdapterContractError) as ctx:
            check_interactive_brokers_version(self.config, mutated)
        self.assertIn("does not match", str(ctx.exception))


class ContractAggregateTest(unittest.TestCase):
    def test_run_checks_emits_seven_evidence_lines(self) -> None:
        evidence = run_checks()
        self.assertEqual(len(evidence), 7)

    def test_static_assertion_emits_six_evidence_lines(self) -> None:
        config = load_config()
        evidence = assert_adapter_contract_static(config, ROOT)
        self.assertEqual(len(evidence), 6)

    def test_runtime_services_block_traces_required_srs(self) -> None:
        contract = adapter_contract(load_config())
        for ref in ("SRS-EXE-006", "SRS-EXE-007"):
            self.assertIn(ref, contract["srs_refs"])
        self.assertEqual(contract["requirement"], "API-5")


class ArchitectureEvidenceTest(unittest.TestCase):
    def test_architecture_check_includes_adapter_contract_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/architecture_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "atp-adapters declares 6 required trait methods",
            result.stdout,
        )
        self.assertIn(
            "InteractiveBrokersAdapter documents IB TWS API version 10.19.4",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
