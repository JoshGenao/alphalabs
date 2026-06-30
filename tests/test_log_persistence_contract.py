"""L3 contract test for the SRS-LOG-001 persistent-sink runtime half.

Two layers of evidence:

* :func:`test_check_script_passes` runs ``tools/log_persistence_check.py`` as
  a subprocess so the positive-evidence path stays under CI coverage (the
  17 collectors that exercise the persistent sinks behaviourally).
* the parity assertions cross-check the ``log_persistence_contract`` block
  against the imported ``atp_logging.persistence`` module without a
  subprocess (required exports, module path, deferred downstream halves).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import persistence as persistence_module  # noqa: E402

pytestmark = [pytest.mark.contract]

_CONTRACT = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())[
    "log_persistence_contract"
]


def test_check_script_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "log_persistence_check.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SRS-LOG-001 PERSISTENCE PASS" in result.stdout


def test_required_exports_match() -> None:
    assert sorted(persistence_module.__all__) == sorted(_CONTRACT["required_exports"])


def test_module_path_exists() -> None:
    assert (ROOT / _CONTRACT["module_path"]).exists()


def test_deferred_names_downstream_halves() -> None:
    named = {entry["feature"] for entry in _CONTRACT["deferred"]}
    assert {"SRS-UI-001", "SRS-API-001"} <= named
