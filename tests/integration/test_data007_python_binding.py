"""SRS-DATA-007 close — L5 integration test (the end-to-end Python consumer read).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). Ingests fixture batches via the real
``data016_ingest_cli`` writer, then reads them back through the real Python binding
(``StoreBackedHistoricalData`` → ``data007_query_cli``) and asserts a named consumer queries by
symbol / date range / resolution with NO provider named — the SRS-DATA-007 acceptance, end to end:

* the daily (≤ Databento) and minute (≤ IB) fixtures are both queryable through the SAME binding with
  no provider-specific branch and no provider argument (one path serves every provider kind);
* the OHLCV values match the fixtures (close scaled from cents, volume a raw count);
* an unknown symbol is an empty result, not an error;
* the binding exposes no way to pass or read an origin provider (structural source-neutrality).

The binding drives the lock-free read path (``data007_query_cli`` takes no ``StoreLock``), so this also
exercises the read side of the SRS-DATA-017 read-during-write property (whose Python-consumer-vs-held-
writer Load test is the deferred 017 close).
"""

from __future__ import annotations

import inspect
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

RAW = NormalizationMode.RAW

pytestmark = pytest.mark.integration


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


def test_python_binding_reads_ingested_data_with_no_provider() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        # Ingest two providers' worth of records: daily (≤ Databento) + minute (≤ IB).
        first = _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init")
        assert first.returncode == 0, first.stdout + first.stderr
        second = _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "minute-equity-bar")
        assert second.returncode == 0, second.stdout + second.stderr

        binding = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)

        # Fail-closed default: omitting normalization (Protocol default SPLIT_ADJUSTED) must raise —
        # the binding serves RAW only (split-adjusted is deferred as a strategy-facing default pending
        # SRS-DATA-011 corporate-action coverage, so it cannot be raw-as-adjusted).
        with pytest.raises(NotImplementedError):
            binding.get_bars("AAPL", lookback=10, frequency="1d")

        # AAPL daily (seed 100): close = 100*100 minor = 10000 -> 100.0; volume = 100*1000 = 100000.
        aapl_daily = binding.get_bars("AAPL", lookback=10, frequency="1d", normalization=RAW)
        assert len(aapl_daily) == 1
        bar = aapl_daily[0]
        assert bar.symbol == "AAPL"
        assert bar.close == 100.0
        assert bar.open == 99.5 and bar.high == 100.75 and bar.low == 99.1
        assert bar.volume == 100000 and isinstance(bar.volume, int)

        # MSFT daily (seed 101): close = 10100 -> 101.0. Same path, no provider-specific branch.
        msft_daily = binding.get_bars("MSFT", lookback=10, frequency="1d", normalization=RAW)
        assert [b.close for b in msft_daily] == [101.0]

        # AAPL minute (seed 200) via the SAME binding, discriminated only by resolution.
        aapl_minute = binding.get_bars("AAPL", lookback=10, frequency="1m", normalization=RAW)
        assert [b.close for b in aapl_minute] == [200.0]

        # An unknown symbol is an empty result, not an error.
        assert binding.get_bars("NOSUCH", lookback=10, frequency="1d", normalization=RAW) == []

        # Structural source-neutrality: there is no provider/source/vendor parameter to pass, and the
        # Bar carries no origin field a consumer could branch on.
        params = set(inspect.signature(binding.get_bars).parameters)
        assert not (params & {"provider", "source", "vendor", "feed"})
        import dataclasses
        bar_fields = {f.name for f in dataclasses.fields(bar)}
        assert not (bar_fields & {"provider", "source", "vendor", "feed"})


def test_get_bars_range_is_deterministic_across_repeated_reads() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    import datetime as dt

    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init").returncode == 0
        binding = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        start = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
        first = binding.get_bars_range("AAPL", frequency="1d", start=start, end=end, normalization=RAW)
        second = binding.get_bars_range("AAPL", frequency="1d", start=start, end=end, normalization=RAW)
        assert first == second
        assert [b.close for b in first] == [100.0]
