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


if __name__ == "__main__":
    unittest.main()
