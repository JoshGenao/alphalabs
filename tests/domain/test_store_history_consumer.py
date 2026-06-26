"""SRS-DATA-007 close — L7 domain test: real strategy & notebook consumers read the real store.

The acceptance names "strategy code, backtests, factor jobs, and notebooks" as the consumers. This
domain test wires a real ``Strategy`` subclass (the strategy-code consumer) AND a notebook-style direct
binding call (the notebook / research consumer) to the concrete store-backed ``StoreBackedHistoricalData``
over a REAL ingested store, and asserts each reads bars by symbol / resolution / date range with NO
provider named — "code queries the unified historical interface without specifying the original source
provider", proven end to end through in-process consumers rather than an operator-CLI analogy. SRS-DATA-007
STAYS passes:false (foundational): the BACKTEST consumer is now genuinely wired as a real Rust engine
(``atp_simulation::store_bar_source::StoreBarSource`` consumes the store in ``BacktestEngine::run``; see that
crate's ``srs_data_007_store_bar_source`` test) and strategy + notebook read via this binding; deferred --
the factor-job EXECUTION path (``atp_factor_pipeline::store_inputs`` is a shipped market-input loader, not yet
invoked by ``run_factor_job``, and a complete run needs Sharadar fundamentals, SRS-DATA-005) and the Jupyter
notebook HOST runtime (SRS-RES-002).

It also pins the safety property the adversarial review demanded. The binding's default
``NormalizationMode.SPLIT_ADJUSTED`` (the HistoricalData Protocol default) is served ONLY through the
SRS-DATA-011 coverage-enforcing gate:

  * COVERED — over a store with a pre-split bar, a split, AND an asserted coverage frontier reaching the
    query end, the consumer's default (SPLIT_ADJUSTED) query returns the re-quoted (split-adjusted)
    series — a 10000 close under a 4-for-1 split reads 2500 ($25.00), volume 100000 reads 400000;
  * UNCOVERED — over a store with no coverage record, the consumer's default query FAILS CLOSED with
    ``CoverageNotProvenError`` (naming SRS-DATA-011), never silent raw bars dressed up as adjusted, so a
    strategy can never trade on bars it believes are adjusted. ``FULLY_ADJUSTED`` / ``TOTAL_RETURN`` need
    dividend data (SRS-DATA-012) and fail closed before any query. ``RAW`` is always available verbatim.

Builds the data CLIs on demand (skips if cargo is unavailable, like the other cargo-driven domain
tests). A structural assertion confirms the binding is registered in the architecture metadata.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
TOOLS_ROOT = ROOT / "tools"
for path in (PYTHON_ROOT, TOOLS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from atp_strategy import Bar, NormalizationMode, Strategy  # noqa: E402
from atp_strategy.store_history import (  # noqa: E402
    CoverageNotProvenError,
    StoreBackedHistoricalData,
)

pytestmark = pytest.mark.domain

SEED_TS = 1_700_000_000
DAY = 86_400
RAW = NormalizationMode.RAW


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build_ingest_and_query(cargo: str) -> tuple[Path, Path]:
    build = _run(
        cargo, "build", "-q", "-p", "atp-data",
        "--bin", "data016_ingest_cli", "--bin", "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return debug / "data016_ingest_cli", debug / "data007_query_cli"


def _build_with_coverage(cargo: str) -> tuple[Path, Path, Path]:
    build = _run(
        cargo, "build", "-q", "-p", "atp-data",
        "--bin", "data016_ingest_cli", "--bin", "data011_coverage_cli", "--bin", "data007_query_cli",
    )
    assert build.returncode == 0, build.stdout + build.stderr
    debug = ROOT / "target" / "debug"
    return (
        debug / "data016_ingest_cli",
        debug / "data011_coverage_cli",
        debug / "data007_query_cli",
    )


class _ResearchStrategy(Strategy):
    """A strategy that pulls its own warm-up history from the unified interface.

    Stands in for strategy / backtest / factor-job / notebook code: it queries ``history`` by symbol /
    resolution / lookback with no provider named, explicitly acknowledging RAW (un-adjusted) data.
    """

    warmup_bars = 3

    def __init__(self) -> None:
        self.history_bars: list[Bar] = []

    def load_history(self, history) -> None:
        self.history_bars = history.get_bars(
            "AAPL", lookback=self.warmup_bars, frequency="1d", normalization=RAW
        )

    def load_split_adjusted(self, history, *, start: datetime, end: datetime) -> None:
        # Queries the unified interface by symbol / date range / resolution with NO provider and the
        # Protocol DEFAULT (SPLIT_ADJUSTED) normalization — the path a strategy / backtest / factor job /
        # notebook uses by default; the binding routes it through the SRS-DATA-011 coverage gate.
        self.history_bars = history.get_bars_range("AAPL", frequency="1d", start=start, end=end)


def test_strategy_reads_store_sourced_bars_without_a_provider() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build_ingest_and_query(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        # Ingest 3 AAPL daily bars across distinct event timestamps (each ingest writes AAPL + MSFT).
        for i in range(3):
            init = ["--init"] if i == 0 else []
            res = _run(
                str(ingest_bin), "ingest", "--dir", tmp,
                "--kind", "daily-equity-bar", "--event-ts", str(SEED_TS + i * DAY), *init,
            )
            assert res.returncode == 0, res.stdout + res.stderr

        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        strategy = _ResearchStrategy()
        strategy.load_history(history)

        # The strategy received real store records (AAPL daily seed 100 -> close 100.0), ascending,
        # carrying no origin field a strategy could branch on.
        assert len(strategy.history_bars) == 3
        assert {b.symbol for b in strategy.history_bars} == {"AAPL"}
        assert [b.close for b in strategy.history_bars] == [100.0, 100.0, 100.0]
        timestamps = [b.timestamp for b in strategy.history_bars]
        assert timestamps == sorted(timestamps)
        bar_fields = {f.name for f in dataclasses.fields(strategy.history_bars[0])}
        assert not (bar_fields & {"provider", "source", "vendor", "feed"})


def test_notebook_reads_store_via_binding_without_a_provider() -> None:
    # The "notebooks" consumer the acceptance names: the literal Jupyter / research idiom -- import the
    # binding and ask for bars by symbol / date range / resolution with NO provider named. There is no
    # Strategy wrapper and no orchestrator here, exactly as a notebook cell or a factor-research script
    # runs. (The Jupyter HOST itself -- the kernel, plotting, and no-live-order isolation -- is the
    # separate deferred SRS-RES-002 feature; this proves the notebook DATA ACCESS goes through the
    # unified, source-neutral interface.)
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build_ingest_and_query(cargo)

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(3):
            init = ["--init"] if i == 0 else []
            res = _run(
                str(ingest_bin), "ingest", "--dir", tmp,
                "--kind", "daily-equity-bar", "--event-ts", str(SEED_TS + i * DAY), *init,
            )
            assert res.returncode == 0, res.stdout + res.stderr

        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        bars = history.get_bars_range(
            "AAPL",
            frequency="1d",
            start=datetime.fromtimestamp(SEED_TS, tz=timezone.utc),
            end=datetime.fromtimestamp(SEED_TS + 2 * DAY, tz=timezone.utc),
            normalization=RAW,
        )
        assert len(bars) == 3
        assert {b.symbol for b in bars} == {"AAPL"}
        assert [b.close for b in bars] == [100.0, 100.0, 100.0]
        timestamps = [b.timestamp for b in bars]
        assert timestamps == sorted(timestamps)
        # Source-neutral: the Bar carries no origin field a notebook could branch on.
        bar_fields = {f.name for f in dataclasses.fields(bars[0])}
        assert not (bar_fields & {"provider", "source", "vendor", "feed"})


def test_consumer_reads_split_adjusted_over_covered_store() -> None:
    # The close: over a store with a pre-split bar, a 4-for-1 split, AND an asserted coverage frontier
    # reaching the query end, the consumer's DEFAULT (SPLIT_ADJUSTED) query returns the re-quoted series
    # by symbol / date range / resolution with NO provider named -- a strategy / backtest / factor job /
    # notebook reads an honest adjusted series through the unified interface.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, coverage_bin, query_bin = _build_with_coverage(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
                    "--event-ts", "100", "--init").returncode == 0
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "corporate-action-split",
                    "--event-ts", "200").returncode == 0
        assert _run(str(coverage_bin), "assert-coverage", "--dir", tmp,
                    "--symbol", "AAPL", "--through", "200").returncode == 0

        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        strategy = _ResearchStrategy()
        strategy.load_split_adjusted(
            history,
            start=datetime(1970, 1, 1, tzinfo=timezone.utc),
            end=datetime.fromtimestamp(100, tz=timezone.utc),
        )
        # The pre-split bar is re-quoted onto the split-comparable basis: close 10000 / 4 = 2500 -> $25.00,
        # volume 100000 * 4 = 400000. The consumer received a real, honest adjusted series.
        assert len(strategy.history_bars) == 1
        bar = strategy.history_bars[0]
        assert bar.symbol == "AAPL"
        assert bar.close == 25.0
        assert bar.volume == 400000
        # Source-neutral: the Bar carries no origin field a consumer could branch on.
        bar_fields = {f.name for f in dataclasses.fields(bar)}
        assert not (bar_fields & {"provider", "source", "vendor", "feed"})


def test_consumer_split_adjusted_uncovered_fails_closed() -> None:
    # The safety property: over a store with bars but NO coverage record, the consumer's DEFAULT
    # (SPLIT_ADJUSTED) query fails closed with CoverageNotProvenError (naming SRS-DATA-011), never silent
    # raw bars dressed up as adjusted -- a strategy can never trade on bars it believes are adjusted.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build_ingest_and_query(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar",
                    "--event-ts", "100", "--init").returncode == 0
        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        strategy = _ResearchStrategy()
        with pytest.raises(CoverageNotProvenError) as exc:
            strategy.load_split_adjusted(
                history,
                start=datetime(1970, 1, 1, tzinfo=timezone.utc),
                end=datetime.fromtimestamp(100, tz=timezone.utc),
            )
        assert "SRS-DATA-011" in str(exc.value)


def test_consumer_normalization_safety_over_uncovered_store() -> None:
    # The bare default (no normalization arg, the path WarmupController uses) and an explicit
    # SPLIT_ADJUSTED both fail closed at the coverage gate over an uncovered store; the dividend modes
    # additionally need dividend data (SRS-DATA-012) and fail closed before any query.
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build_ingest_and_query(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(
            str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init"
        ).returncode == 0
        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        # Coverage-gated modes: both the bare default and explicit SPLIT_ADJUSTED fail closed naming 011.
        for call in (
            lambda: history.get_bars("AAPL", lookback=1, frequency="1d"),
            lambda: history.get_bars(
                "AAPL", lookback=1, frequency="1d", normalization=NormalizationMode.SPLIT_ADJUSTED
            ),
        ):
            with pytest.raises(CoverageNotProvenError) as exc:
                call()
            assert "SRS-DATA-011" in str(exc.value)
        # Dividend modes fail closed before any query (no dividend data, SRS-DATA-012).
        for mode in (NormalizationMode.FULLY_ADJUSTED, NormalizationMode.TOTAL_RETURN):
            with pytest.raises(NotImplementedError):
                history.get_bars("AAPL", lookback=1, frequency="1d", normalization=mode)


def test_store_history_binding_is_registered_in_architecture_metadata() -> None:
    # Structural: the binding contract block + check are wired so CI/init.sh gate on them.
    from store_history_check import assert_store_history_static, contract_block, load_config

    config = load_config()
    block = contract_block(config)
    assert block["module"]["class"] == "StoreBackedHistoricalData"
    assert block["protocol"] == "HistoricalData"
    assert set(block["consumers"]) == {"strategy code", "backtests", "factor jobs", "notebooks"}
    # The static contract guards all pass against the real binding source.
    assert len(assert_store_history_static(config, ROOT)) == 12
