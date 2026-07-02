"""SRS-RES-002 — L5 integration test: the REAL notebook research path (no injection).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). This is the no-fake-runner
proof of the research data path: it builds the real ``data016_ingest_cli`` +
``data007_query_cli`` binaries, ingests fixtures, then exercises the documented
notebook workflow — ``atp_research.open_historical_data(store_dir=...)`` driving the
REAL query binary (no injected runner, no injected binary path beyond pointing at the
freshly-built one an operator's JupyterLab image would bundle), then computing an
indicator and rendering a plot over the store-sourced bars. It is the operator-runnable
end-to-end demonstration that keeps SRS-RES-002 honest while the live JupyterLab image
(``docker/jupyter.Dockerfile`` / ``SRS-ARCH-004``) is provisioned; the L4 boundary test
covers the same wiring over a fake runner for the fast (parallel) suite.
"""

from __future__ import annotations

import io
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

import atp_research as ar  # noqa: E402
from atp_strategy import Bar, NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreQueryError  # noqa: E402

RAW = NormalizationMode.RAW

pytestmark = pytest.mark.integration


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build(cargo: str) -> tuple[Path, Path]:
    build = _run(
        cargo,
        "build",
        "-q",
        "-p",
        "atp-data",
        "--bin",
        "data016_ingest_cli",
        "--bin",
        "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data007_query_cli"


def test_notebook_reads_real_store_computes_indicator_and_plots() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        assert (
            _run(
                str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init"
            ).returncode
            == 0
        )

        # The documented notebook entry point, driving the REAL query binary (no
        # injected runner). Points at the freshly-built binary an operator's JupyterLab
        # image would bundle/mount (query_binary / ATP_DATA_QUERY_BINARY).
        data = ar.open_historical_data(store_dir=tmp, query_binary=query_bin)

        bars = data.get_bars("AAPL", lookback=10, frequency="1d", normalization=RAW)
        assert len(bars) == 1 and isinstance(bars[0], Bar)
        assert bars[0].close == 100.0  # seed 100 -> 10000 minor / 100

        # Compute an indicator + render a plot over the STORE-sourced bars — the full
        # research workflow, end to end, with no fake anything.
        series = ar.compute_series(ar.SMA(period=1), bars)
        assert series == [100.0]
        fig = ar.plot_ohlc(bars, indicators={"SMA1": series}, title="AAPL daily")
        buffer = io.BytesIO()
        fig.savefig(buffer, format="png")
        assert buffer.getvalue().startswith(b"\x89PNG")

        # An unknown symbol is an empty result, not an error.
        assert data.get_bars("NOSUCH", lookback=5, frequency="1d", normalization=RAW) == []


def test_missing_query_binary_fails_closed_over_the_real_runner() -> None:
    # No injected runner: pointing the notebook at an absent binary fails CLOSED with an
    # actionable StoreQueryError (naming the binary), so an unprovisioned JupyterLab image
    # surfaces a clear error on first query rather than a hang or silent empty result.
    with tempfile.TemporaryDirectory() as tmp:
        data = ar.open_historical_data(
            store_dir=tmp, query_binary=Path(tmp) / "unbuilt-data007_query_cli"
        )
        with pytest.raises(StoreQueryError) as exc:
            data.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
        assert "data007_query_cli" in str(exc.value)
