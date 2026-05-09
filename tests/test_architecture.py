from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTest(unittest.TestCase):
    def test_srs_arch_001_language_boundary(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/architecture_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-001 PASS", result.stdout)
        self.assertIn("SRS-ARCH-002 enforced dependency flow", result.stdout)
        self.assertIn("SRS-ARCH-004 Phase 1 deployment", result.stdout)
        self.assertIn("SRS-ARCH-005 configuration system", result.stdout)
        self.assertIn("atp_ws covers 8 channels", result.stdout)
        self.assertIn("/ws/v1", result.stdout)
        self.assertIn("atp_cli covers 6 groups", result.stdout)
        self.assertIn("local-shell", result.stdout)
        self.assertIn(
            "atp-adapters declares 6 required trait methods",
            result.stdout,
        )
        self.assertIn(
            "InteractiveBrokersAdapter documents IB TWS API version 10.45",
            result.stdout,
        )
        self.assertIn(
            "atp-adapters declares 7 data-provider methods across 5 traits",
            result.stdout,
        )
        self.assertIn("DataProviderAdapter base", result.stdout)
        self.assertIn(
            "atp-adapters unified historical query carries 6 request fields",
            result.stdout,
        )
        self.assertIn(
            "5 asset classes / 4 normalization modes for 4 consumers",
            result.stdout,
        )
        self.assertIn("API-7, SRS-DATA-007 + SRS-DATA-012", result.stdout)

    def test_srs_arch_002_dependency_boundary(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/dependency_boundary_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-002 PASS", result.stdout)
        self.assertIn("Cargo graph checked", result.stdout)
        self.assertIn("source scan found no dashboard, orchestrator, or vendor imports", result.stdout)

    def test_srs_arch_002_rejects_orchestrator_import_from_lower_layer(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/dependency_boundary_check.py",
                "--fixture",
                "lower-layer-orchestrator-import",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-002 FAIL", result.stderr)
        self.assertIn("atp_orchestrator", result.stderr)

    def test_srs_arch_002_rejects_vendor_adapter_import_from_lower_layer(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/dependency_boundary_check.py",
                "--fixture",
                "lower-layer-vendor-adapter-import",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-002 FAIL", result.stderr)
        self.assertIn("ib_gateway", result.stderr)

    def test_srs_arch_002_rejects_dashboard_import_from_lower_layer(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/dependency_boundary_check.py",
                "--fixture",
                "lower-layer-dashboard-import",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-002 FAIL", result.stderr)
        self.assertIn("atp_dashboard", result.stderr)

    def test_srs_arch_003_adapter_isolation(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/adapter_isolation_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-003 PASS", result.stdout)
        self.assertIn("public traits", result.stdout)
        self.assertIn("fictional alternative-data adapter compiled", result.stdout)

    def test_srs_arch_003_rejects_ib_import_from_core(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/adapter_isolation_check.py",
                "--fixture",
                "core-imports-ib",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-003 FAIL", result.stderr)
        self.assertIn("interactive_brokers", result.stderr)

    def test_srs_arch_003_rejects_databento_import_from_core(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/adapter_isolation_check.py",
                "--fixture",
                "core-imports-databento",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-003 FAIL", result.stderr)
        self.assertIn("databento", result.stderr)

    def test_srs_arch_003_rejects_sharadar_import_from_core(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/adapter_isolation_check.py",
                "--fixture",
                "core-imports-sharadar",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-003 FAIL", result.stderr)
        self.assertIn("sharadar", result.stderr)

    def test_srs_arch_004_deployment_compose(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/deployment_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-004 PASS", result.stdout)
        self.assertIn("phase1", result.stdout)
        self.assertIn("SSD primary tier and NAS archive tier", result.stdout)
        self.assertIn("cloud VPS as future target", result.stdout)
        self.assertIn("127.0.0.1", result.stdout)

    def test_srs_arch_004_rejects_missing_jupyter(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/deployment_check.py",
                "--fixture",
                "missing-jupyter",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-004 FAIL", result.stderr)
        self.assertIn("phase1-jupyter", result.stderr)

    def test_srs_arch_004_rejects_missing_ssd_volume(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/deployment_check.py",
                "--fixture",
                "missing-ssd",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-004 FAIL", result.stderr)
        self.assertIn("ATP_SSD_DATA_DIR", result.stderr)

    def test_srs_arch_004_rejects_missing_portability_doc(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/deployment_check.py",
                "--fixture",
                "missing-portability-doc",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-004 FAIL", result.stderr)
        self.assertIn("portability", result.stderr)

    def test_srs_arch_005_configuration_pass(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/config_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-005 PASS", result.stdout)
        self.assertIn("16 keys catalogued across 6 categories", result.stdout)
        for category in (
            "credentials",
            "storage_paths",
            "ib_account",
            "market_data_limits",
            "resource_limits",
            "notification_channels",
        ):
            self.assertIn(category, result.stdout)

    def test_srs_arch_005_rejects_missing_credential(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/config_check.py",
                "--fixture",
                "missing-credential",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-005 FAIL", result.stderr)
        self.assertIn("DATABENTO_API_KEY", result.stderr)

    def test_srs_arch_005_rejects_placeholder_secret_in_production(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/config_check.py",
                "--fixture",
                "placeholder-secret-in-production",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-005 FAIL", result.stderr)
        self.assertIn("production", result.stderr)
        self.assertIn("placeholder-set-in-environment", result.stderr)

    def test_srs_arch_005_rejects_invalid_line_limit(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/config_check.py",
                "--fixture",
                "invalid-line-limit",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-005 FAIL", result.stderr)
        self.assertIn("ATP_MARKET_DATA_LINE_LIMIT", result.stderr)

    def test_srs_arch_005_rejects_missing_resource_limit(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/config_check.py",
                "--fixture",
                "missing-resource-limit",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ARCH-005 FAIL", result.stderr)
        self.assertIn("ATP_LIVE_STRATEGY_MEM_MB", result.stderr)


if __name__ == "__main__":
    unittest.main()
