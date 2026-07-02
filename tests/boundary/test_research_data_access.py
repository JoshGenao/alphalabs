"""L4 boundary test for research data access (``SRS-RES-002`` / ``SRS-DATA-007``).

Exercises the notebook read path — ``atp_research.open_historical_data`` over a FAKE
``data007_query_cli`` runner (no cargo needed) — proving a notebook queries the
unified data interface by ``(symbol, resolution, date range)`` with **no provider
named**, through the same source-neutral binding every other consumer uses, and gets
back read-only bars. The exhaustive parser/honesty invariants of the underlying
binding are pinned by ``tests/boundary/test_store_history_binding.py``; this test
pins that the research factory wires them up for notebooks and stays read-only.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_research as ar  # noqa: E402
from atp_strategy import Bar, NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData, StoreQueryError  # noqa: E402

pytestmark = pytest.mark.boundary

RAW = NormalizationMode.RAW
_OHLCV = {"open": 9950, "high": 10075, "low": 9910, "close": 10000, "volume": 100000}


def _render(
    symbol: str,
    resolution: str,
    start: str,
    end: str,
    records: list[tuple[int, dict[str, int]]],
    normalization: str = "raw",
) -> str:
    lines = [
        f"symbol:{symbol}",
        f"resolution:{resolution}",
        f"start:{start}",
        f"end:{end}",
        "kind:any",
        f"normalization:{normalization}",
        f"match_count:{len(records)}",
    ]
    for i, (event_ts, fields) in enumerate(records):
        lines.append(f"record.{i}.event_ts:{event_ts}")
        lines.append(f"record.{i}.option_contract:-")
        for name, value in fields.items():
            lines.append(f"record.{i}.field.{name}:{value}")
    return "\n".join(lines) + "\n"


class _FakeRunner:
    """A fake data007_query_cli echoing the requested query (like the real CLI)."""

    def __init__(
        self,
        *,
        records: list[tuple[int, dict[str, int]]] | None = None,
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self._records = records
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.returncode != 0:
            return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)
        symbol = argv[argv.index("--symbol") + 1]
        resolution = argv[argv.index("--resolution") + 1]
        start = argv[argv.index("--start") + 1]
        end = argv[argv.index("--end") + 1]
        normalization = argv[argv.index("--normalization") + 1]
        out = _render(symbol, resolution, start, end, self._records or [], normalization)
        return subprocess.CompletedProcess(argv, 0, out, "")


def _open(runner: _FakeRunner) -> StoreBackedHistoricalData:
    return ar.open_historical_data(
        store_dir="/tmp/does-not-matter",
        query_binary="/tmp/fake-data007_query_cli",
        runner=runner,
    )


def test_open_historical_data_returns_the_readonly_binding() -> None:
    handle = _open(_FakeRunner(records=[]))
    assert isinstance(handle, StoreBackedHistoricalData)


def test_notebook_queries_by_symbol_range_resolution_with_no_provider() -> None:
    runner = _FakeRunner(records=[(1_700_000_000, _OHLCV), (1_700_086_400, _OHLCV)])
    handle = _open(runner)
    bars = handle.get_bars_range(
        "AAPL",
        frequency="1d",
        start=datetime(2023, 11, 14, tzinfo=timezone.utc),
        end=datetime(2023, 11, 16, tzinfo=timezone.utc),
        normalization=RAW,
    )
    assert [bar.close for bar in bars] == [100.0, 100.0]  # minor units / 100
    assert all(isinstance(bar, Bar) for bar in bars)
    # source-neutral: the notebook never passes, and the binding never emits, a provider flag.
    (argv,) = runner.calls
    assert argv[argv.index("--symbol") + 1] == "AAPL"
    assert not any(flag in argv for flag in ("--provider", "--source", "--vendor", "--feed"))


def test_notebook_query_empty_result_is_a_value_not_an_error() -> None:
    handle = _open(_FakeRunner(records=[]))
    assert handle.get_bars("AAPL", lookback=5, frequency="1d", normalization=RAW) == []


def test_notebook_query_failure_surfaces_structured_error() -> None:
    handle = _open(_FakeRunner(returncode=1, stderr="store directory missing"))
    with pytest.raises(StoreQueryError):
        handle.get_bars("AAPL", lookback=5, frequency="1d", normalization=RAW)


def test_open_historical_data_fails_closed_without_store_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATP_DATA_STORE_DIR", raising=False)
    with pytest.raises(ValueError, match="store directory"):
        ar.open_historical_data()


def test_default_notebook_path_without_the_query_binary_fails_closed(
    tmp_path: Path,
) -> None:
    # The DEFAULT notebook path (NO injected runner) drives the real subprocess
    # runner. In the operator's JupyterLab image the built data007_query_cli must be
    # provisioned (SRS-ARCH-004); if it is absent the first query fails CLOSED with
    # an actionable StoreQueryError naming the binary — never a hang or silent empty.
    handle = ar.open_historical_data(
        store_dir=str(tmp_path),
        query_binary=str(tmp_path / "not-built-data007_query_cli"),
    )
    with pytest.raises(StoreQueryError) as exc:
        handle.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    assert "data007_query_cli" in str(exc.value)


def test_env_var_configures_the_query_binary_for_notebooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An operator points notebooks at the bundled binary via ATP_DATA_QUERY_BINARY,
    # with no notebook code change; the resolved path is what the runner is invoked with.
    monkeypatch.setenv("ATP_DATA_QUERY_BINARY", "/opt/atp/bin/data007_query_cli")
    runner = _FakeRunner(records=[])
    handle = ar.open_historical_data(store_dir="/tmp/does-not-matter", runner=runner)
    handle.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    (argv,) = runner.calls
    assert argv[0] == "/opt/atp/bin/data007_query_cli"


def test_explicit_query_binary_overrides_the_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATP_DATA_QUERY_BINARY", "/opt/atp/bin/from-env")
    runner = _FakeRunner(records=[])
    handle = ar.open_historical_data(
        store_dir="/tmp/does-not-matter",
        query_binary="/explicit/data007_query_cli",
        runner=runner,
    )
    handle.get_bars("AAPL", lookback=1, frequency="1d", normalization=RAW)
    (argv,) = runner.calls
    assert argv[0] == "/explicit/data007_query_cli"
