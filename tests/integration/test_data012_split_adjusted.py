"""SRS-DATA-012 split-adjusted normalization — L5 integration test (public-surface fail-closed).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). The split-adjustment MATH is implemented as
a CRATE-INTERNAL module (``crates/atp-data/src/normalization.rs``), proven by that crate's own unit
suite and NOT re-exported as a public crate API. It is FOUNDATIONAL substrate, exposed on NO public
surface (operator CLI, Python binding, or Rust crate API), because a "split-adjusted" label is only
honest with proven corporate-action COVERAGE and real corporate-action ingestion is deferred
(SRS-DATA-011) -- absent coverage, an empty split set is indistinguishable from missing data, so
emitting split-adjusted output would be raw-as-adjusted.

This test asserts the public surfaces fail closed: the operator CLI ``data007_query_cli`` and the Python
consumer binding ``StoreBackedHistoricalData`` both serve RAW only and refuse split-adjusted.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData  # noqa: E402

pytestmark = pytest.mark.integration

BAR_TS = 1_700_000_000


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build(cargo: str) -> tuple[Path, Path]:
    build = _run(
        cargo, "build", "-q", "-p", "atp-data",
        "--bin", "data016_ingest_cli", "--bin", "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data007_query_cli"


def _ingest_daily(ingest_bin: Path, tmp: str) -> None:
    assert _run(
        str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
        "--event-ts", str(BAR_TS), "--init",
    ).returncode == 0


def test_cli_serves_raw_and_rejects_split_adjusted() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest_daily(ingest_bin, tmp)

        def query(mode: str) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin), "query", "--dir", tmp, "--symbol", "AAPL", "--resolution", "1d",
                "--start", "0", "--end", str(BAR_TS), "--kind", "daily-equity-bar",
                "--normalization", mode,
            )

        # RAW works and echoes normalization:raw.
        raw = query("raw")
        assert raw.returncode == 0, raw.stderr
        assert "normalization:raw" in raw.stdout
        assert "record.0.field.close:10000" in raw.stdout

        # split-adjusted FAILS closed (no raw-as-adjusted), naming the corporate-action coverage owner.
        adj = query("split-adjusted")
        assert adj.returncode != 0
        assert "SRS-DATA-011" in adj.stderr


def test_consumer_binding_refuses_split_adjusted() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest_daily(ingest_bin, tmp)
        binding = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        # The binding serves RAW only: split-adjusted (and the bare default) fail closed.
        with pytest.raises(NotImplementedError):
            binding.get_bars("AAPL", lookback=5, frequency="1d", normalization=NormalizationMode.SPLIT_ADJUSTED)
        with pytest.raises(NotImplementedError):
            binding.get_bars("AAPL", lookback=5, frequency="1d")
        # RAW still works through the binding.
        (raw_bar,) = binding.get_bars("AAPL", lookback=5, frequency="1d", normalization=NormalizationMode.RAW)
        assert raw_bar.close == 100.0
