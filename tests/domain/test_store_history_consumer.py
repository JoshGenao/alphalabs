"""SRS-DATA-007 close — L7 domain test: a real strategy consumer reads the real store.

The acceptance names "strategy code, backtests, factor jobs, and notebooks" as the consumers. This
domain test wires a real ``Strategy`` subclass (standing in for strategy / backtest / factor-job /
notebook code) to the concrete store-backed ``StoreBackedHistoricalData`` over a REAL ingested store,
and asserts it reads bars by symbol / resolution / range with NO provider named — "strategy code
queries the unified historical interface without specifying the original source provider", proven end
to end through an in-process consumer rather than an operator-CLI analogy.

It also pins the two safety properties the adversarial review demanded: the consumer must explicitly
opt into ``NormalizationMode.RAW`` (the default ``SPLIT_ADJUSTED`` fails closed because the store has
no adjusted data yet — SRS-DATA-012 deferred), so a strategy can never silently trade on raw bars it
believes are split-adjusted.

Builds the data CLIs on demand (skips if cargo is unavailable, like the other cargo-driven domain
tests). A second structural assertion confirms the binding is registered in the architecture metadata.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
TOOLS_ROOT = ROOT / "tools"
for path in (PYTHON_ROOT, TOOLS_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from atp_strategy import Bar, NormalizationMode, Strategy  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData  # noqa: E402

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


def test_default_normalization_fails_closed_for_the_consumer() -> None:
    # The safety property: a strategy that omits normalization (Protocol default SPLIT_ADJUSTED) gets a
    # loud failure, never silent raw bars dressed up as adjusted (a splits hazard for live trading).
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    ingest_bin, query_bin = _build_ingest_and_query(cargo)
    with tempfile.TemporaryDirectory() as tmp:
        assert _run(
            str(ingest_bin), "ingest", "--dir", tmp, "--kind", "daily-equity-bar", "--init"
        ).returncode == 0
        history = StoreBackedHistoricalData(store_dir=tmp, query_binary=query_bin)
        with pytest.raises(NotImplementedError):
            history.get_bars("AAPL", lookback=1, frequency="1d")


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
