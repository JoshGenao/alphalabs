"""SRS-DATA-012 split-adjusted normalization — L5 integration test (uncovered-store fail-closed).

Gated by ``ATP_RUN_INTEGRATION=1`` (see tests/conftest.py). The raw split-adjustment MATH is a
CRATE-INTERNAL module (``crates/atp-data/src/normalization.rs``), proven by that crate's own unit suite
and NOT re-exported as a public crate API. Split-adjusted output is served on the operator CLI ONLY
through the SRS-DATA-011 coverage-enforcing gate (``MarketDataStore::query_split_adjusted``): over a
store with no coverage record, an empty/incomplete split set is indistinguishable from missing data, so
the gate FAILS CLOSED rather than emitting raw-as-adjusted output.

This test ingests a daily bar with NO coverage record and asserts the uncovered-store fail-closed
behaviour: the operator CLI ``data007_query_cli`` fails closed on split-adjusted (naming SRS-DATA-011),
and the Python consumer binding ``StoreBackedHistoricalData`` — which now serves gated split-adjusted as
its Protocol default — also fails closed over this uncovered store, raising ``CoverageNotProvenError``
(naming SRS-DATA-011), never raw-as-adjusted. The COVERED (served) path is
proven by the SRS-DATA-011 coverage tests (tools/coverage_manifest_check.py +
tests/domain/test_coverage_gate_domain) and the SRS-DATA-007 consumer close
(tests/domain/test_store_history_consumer).
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
from atp_strategy.store_history import (  # noqa: E402
    CoverageNotProvenError,
    StoreBackedHistoricalData,
)

pytestmark = pytest.mark.integration

BAR_TS = 1_700_000_000


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


def _ingest_daily(ingest_bin: Path, tmp: str) -> None:
    assert (
        _run(
            str(ingest_bin),
            "ingest",
            "--dir",
            tmp,
            "--kind",
            "daily-equity-bar",
            "--event-ts",
            str(BAR_TS),
            "--init",
        ).returncode
        == 0
    )


def test_cli_serves_raw_and_fails_closed_split_adjusted_without_coverage() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest_daily(ingest_bin, tmp)

        def query(mode: str) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin),
                "query",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--resolution",
                "1d",
                "--start",
                "0",
                "--end",
                str(BAR_TS),
                "--kind",
                "daily-equity-bar",
                "--normalization",
                mode,
            )

        # RAW works and echoes normalization:raw.
        raw = query("raw")
        assert raw.returncode == 0, raw.stderr
        assert "normalization:raw" in raw.stdout
        assert "record.0.field.close:10000" in raw.stdout

        # Over this UNCOVERED store (no coverage record ingested), every adjusted mode FAILS closed at
        # the coverage gate (no raw-as-adjusted), naming the corporate-action coverage owner. This
        # includes total-return (the SRS-DATA-012 close), which routes through the SAME gate.
        for adjusted_mode in ("split-adjusted", "fully-adjusted", "total-return"):
            adj = query(adjusted_mode)
            assert adj.returncode != 0, adjusted_mode
            assert "SRS-DATA-011" in adj.stderr, adjusted_mode


def test_cli_serves_raw_option_chain_but_refuses_adjusted_on_options() -> None:
    # SRS-DATA-012 "options strategies can request raw prices" -- the normalization-MODE half, served at
    # the DATA LAYER / operator CLI. The RAW mode serves option-chain records verbatim (query_unified is
    # kind-agnostic), while every adjusted mode REFUSES a non-equity kind (UnsupportedQueryKind) -- so an
    # options query structurally resolves to raw and can never get a meaningless adjusted option price.
    # The strategy-facing option DATA path (ctx.history) is NOT closed by DATA-012 (deferred, owner
    # SRS-DATA-006) -- see tests/boundary/test_store_history_binding.py::test_option_asset_class_raises.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert (
            _run(
                str(ingest_bin),
                "ingest",
                "--dir",
                tmp,
                "--kind",
                "option-chain",
                "--event-ts",
                str(BAR_TS),
                "--init",
            ).returncode
            == 0
        )

        def option_query(mode: str) -> subprocess.CompletedProcess[str]:
            return _run(
                str(query_bin),
                "query",
                "--dir",
                tmp,
                "--symbol",
                "AAPL",
                "--resolution",
                "chain",
                "--start",
                "0",
                "--end",
                str(BAR_TS),
                "--kind",
                "option-chain",
                "--normalization",
                mode,
            )

        # RAW serves the option-chain records verbatim (an options strategy CAN request raw prices).
        raw = option_query("raw")
        assert raw.returncode == 0, raw.stderr
        assert "normalization:raw" in raw.stdout
        assert "match_count:2" in raw.stdout
        assert "record.0.field.bid:" in raw.stdout
        # Every adjusted mode is REFUSED on a non-equity kind (so options can only get raw).
        for adjusted_mode in ("split-adjusted", "fully-adjusted", "total-return"):
            adj = option_query(adjusted_mode)
            assert adj.returncode != 0, adjusted_mode
            assert "equity-bar" in adj.stderr, adjusted_mode


def test_consumer_binding_fails_closed_split_adjusted_without_coverage() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        _ingest_daily(ingest_bin, tmp)
        binding = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        # Over this UNCOVERED store the binding routes split-adjusted (and the bare default) through the
        # coverage gate and fails closed with CoverageNotProvenError (naming SRS-DATA-011), never raw.
        with pytest.raises(CoverageNotProvenError) as exc:
            binding.get_bars(
                "AAPL", lookback=5, frequency="1d", normalization=NormalizationMode.SPLIT_ADJUSTED
            )
        assert "SRS-DATA-011" in str(exc.value)
        with pytest.raises(CoverageNotProvenError):
            binding.get_bars("AAPL", lookback=5, frequency="1d")
        # FULLY_ADJUSTED and TOTAL_RETURN are served through the SAME gate now, so over the uncovered
        # store they fail closed at the gate too (CoverageNotProvenError, not NotImplementedError).
        for adjusted_mode in (
            NormalizationMode.FULLY_ADJUSTED,
            NormalizationMode.TOTAL_RETURN,
        ):
            with pytest.raises(CoverageNotProvenError):
                binding.get_bars("AAPL", lookback=5, frequency="1d", normalization=adjusted_mode)
        # RAW still works through the binding.
        (raw_bar,) = binding.get_bars(
            "AAPL", lookback=5, frequency="1d", normalization=NormalizationMode.RAW
        )
        assert raw_bar.close == 100.0
