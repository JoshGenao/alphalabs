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


if __name__ == "__main__":
    unittest.main()
