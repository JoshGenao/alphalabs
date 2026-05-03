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


if __name__ == "__main__":
    unittest.main()
