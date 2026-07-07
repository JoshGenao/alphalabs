"""SRS-SDK-007 — L4 boundary test: the store binding serves 5m/15m/1h by consolidating 1m.

Exercises ``StoreBackedHistoricalData`` over a FAKE ``data007_query_cli`` runner (no cargo)
to pin the consolidation wiring at the binding boundary:

* a ``5m`` / ``15m`` / ``1h`` query fetches the underlying ``1m`` dataset ONCE (``--resolution 1m
  --kind minute-equity-bar``) over the same range, then folds it into the requested period —
  no ``5m`` dataset is ever requested;
* the folded OHLCV is correct on the cents→dollars-converted minute bars;
* **only buckets whose FULL period lies within the request are returned** — a mid-bucket
  ``start``/``end`` drops the range-truncated edge bucket (never a partial bar mislabelled
  outside the range), and a real-clock ``get_bars`` drops the still-open trailing bucket;
* ``1m`` / ``1d`` stay native (unconsolidated); an unsupported resolution still fails closed;
* the coverage-gated ``SPLIT_ADJUSTED`` mode is honoured on the underlying 1m fetch.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from atp_strategy import NormalizationMode  # noqa: E402
from atp_strategy.store_history import StoreBackedHistoricalData  # noqa: E402

pytestmark = pytest.mark.boundary

RAW = NormalizationMode.RAW
SPLIT = NormalizationMode.SPLIT_ADJUSTED

# A base epoch on a 5m / 15m / hour / UTC-day boundary → predictable consolidated labels.
_BASE = 1_777_593_600  # 2026-05-01T00:00:00+00:00


def _render(argv: list[str], records: list[tuple[int, dict[str, int]]]) -> str:
    """Render a data007_query_cli response, echoing the query and returning only IN-RANGE records.

    The real CLI only emits records whose ``event_ts`` falls in the inclusive ``[--start, --end]``
    window; the binding's parser fails closed on an out-of-range record, so the fake must filter
    exactly as the CLI does.
    """
    symbol = argv[argv.index("--symbol") + 1]
    resolution = argv[argv.index("--resolution") + 1]
    start = int(argv[argv.index("--start") + 1])
    end = int(argv[argv.index("--end") + 1])
    normalization = argv[argv.index("--normalization") + 1]
    in_range = [(ts, fields) for ts, fields in records if start <= ts <= end]
    lines = [
        f"symbol:{symbol}",
        f"resolution:{resolution}",
        f"start:{start}",
        f"end:{end}",
        "kind:any",
        f"normalization:{normalization}",
    ]
    if normalization in ("split-adjusted", "fully-adjusted"):
        lines.append(f"coverage_through:{end}")
    lines.append(f"match_count:{len(in_range)}")
    for i, (event_ts, fields) in enumerate(in_range):
        lines.append(f"record.{i}.event_ts:{event_ts}")
        lines.append(f"record.{i}.option_contract:-")
        for name, value in fields.items():
            lines.append(f"record.{i}.field.{name}:{value}")
    return "\n".join(lines) + "\n"


class _FakeRunner:
    def __init__(self, records: list[tuple[int, dict[str, int]]]) -> None:
        self._records = records
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, _render(argv, self._records), "")


def _binding(runner: _FakeRunner, *, clock_ts: int) -> StoreBackedHistoricalData:
    clock = datetime.fromtimestamp(clock_ts, tz=timezone.utc)
    return StoreBackedHistoricalData(
        store_dir="/tmp/does-not-matter",
        query_binary="/tmp/fake-cli",
        runner=runner,
        clock=lambda: clock,
    )


def _arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def _minute_cents(count: int) -> list[tuple[int, dict[str, int]]]:
    """`count` consecutive 1-minute records (cents), distinct per bar so each aggregate is observable."""
    return [
        (
            _BASE + i * 60,
            {"open": 10000 + i, "high": 10100 + i, "low": 9900 - i, "close": 10050 + i,
             "volume": 1000 + i},
        )
        for i in range(count)
    ]


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# --------------------------------------------------------------------------- #


def test_five_minute_range_fetches_one_minute_and_consolidates() -> None:
    runner = _FakeRunner(_minute_cents(10))  # 00:00..00:09 → two full 5m buckets
    out = _binding(runner, clock_ts=_BASE + 10_000).get_bars_range(
        "AAPL", frequency="5m", start=_dt(_BASE), end=_dt(_BASE + 600), normalization=RAW
    )
    # Fetched the MINUTE dataset once — never a 5m dataset.
    assert len(runner.calls) == 1
    assert _arg(runner.calls[0], "--resolution") == "1m"
    assert _arg(runner.calls[0], "--kind") == "minute-equity-bar"

    assert [b.timestamp for b in out] == [
        "2026-05-01T00:00:00+00:00",
        "2026-05-01T00:05:00+00:00",
    ]
    first = out[0]  # bars i=0..4 (cents → dollars)
    assert first.open == 100.00  # bar0 open 10000/100
    assert first.high == 101.04  # max high (10100+4)/100
    assert first.low == 98.96  # min low (9900-4)=9896/100
    assert first.close == 100.54  # bar4 close (10050+4)/100
    assert first.volume == sum(range(1000, 1005))  # 5010, unscaled


def test_mid_bucket_start_drops_truncated_leading_bucket() -> None:
    # start = 00:02 falls inside the [00:00,00:05) bucket; that bucket was only partially fetched
    # (00:02-00:04), so it must NOT be emitted (it would be mislabelled 00:00, outside the range).
    runner = _FakeRunner(_minute_cents(10))
    out = _binding(runner, clock_ts=_BASE + 10_000).get_bars_range(
        "AAPL", frequency="5m", start=_dt(_BASE + 120), end=_dt(_BASE + 600), normalization=RAW
    )
    assert [b.timestamp for b in out] == ["2026-05-01T00:05:00+00:00"]  # only the complete bucket


def test_mid_bucket_end_drops_truncated_trailing_bucket() -> None:
    # end = 00:07 falls inside the [00:05,00:10) bucket; it was only partially fetched (00:05-00:07),
    # so only the complete [00:00,00:05) bucket is returned.
    runner = _FakeRunner(_minute_cents(10))
    out = _binding(runner, clock_ts=_BASE + 10_000).get_bars_range(
        "AAPL", frequency="5m", start=_dt(_BASE), end=_dt(_BASE + 420), normalization=RAW
    )
    assert [b.timestamp for b in out] == ["2026-05-01T00:00:00+00:00"]


def test_lookback_returns_last_complete_bucket_not_in_progress() -> None:
    # Data runs 00:00..00:11; "now" is 00:12, mid the [00:10,00:15) bucket. get_bars must return the
    # last CLOSED 5m bucket ([00:05,00:10)), never the still-open one — matching the streaming path.
    runner = _FakeRunner(_minute_cents(12))
    out = _binding(runner, clock_ts=_BASE + 720).get_bars(
        "AAPL", lookback=1, frequency="5m", normalization=RAW
    )
    assert [b.timestamp for b in out] == ["2026-05-01T00:05:00+00:00"]


def test_fifteen_minute_folds_all_into_one_bucket() -> None:
    runner = _FakeRunner(_minute_cents(15))  # 00:00..00:14 → one full 15m bucket
    out = _binding(runner, clock_ts=_BASE + 10_000).get_bars_range(
        "AAPL", frequency="15m", start=_dt(_BASE), end=_dt(_BASE + 900), normalization=RAW
    )
    assert len(out) == 1
    assert out[0].timestamp == "2026-05-01T00:00:00+00:00"
    assert out[0].volume == sum(range(1000, 1015))
    assert _arg(runner.calls[0], "--resolution") == "1m"


def test_daily_stays_native_no_consolidation() -> None:
    runner = _FakeRunner([(_BASE, {"open": 10000, "high": 10100, "low": 9900, "close": 10050,
                                   "volume": 1000})])
    _binding(runner, clock_ts=_BASE + 10_000).get_bars(
        "AAPL", lookback=1, frequency="1d", normalization=RAW
    )
    assert _arg(runner.calls[0], "--resolution") == "1d"
    assert _arg(runner.calls[0], "--kind") == "daily-equity-bar"


def test_minute_stays_native() -> None:
    runner = _FakeRunner(_minute_cents(3))
    _binding(runner, clock_ts=_BASE + 10_000).get_bars(
        "AAPL", lookback=3, frequency="1m", normalization=RAW
    )
    assert _arg(runner.calls[0], "--resolution") == "1m"
    assert len(runner.calls) == 1


def test_unsupported_resolution_still_fails_closed() -> None:
    runner = _FakeRunner(_minute_cents(3))
    with pytest.raises(NotImplementedError):
        _binding(runner, clock_ts=_BASE + 10_000).get_bars(
            "AAPL", lookback=1, frequency="30m", normalization=RAW
        )


def test_split_adjusted_consolidation_goes_through_the_gate() -> None:
    # The underlying 1m fetch is split-adjusted (fake stamps coverage_through, as the gate does),
    # then consolidated — a consolidated adjusted read is never raw dressed as adjusted.
    runner = _FakeRunner(_minute_cents(5))
    out = _binding(runner, clock_ts=_BASE + 10_000).get_bars_range(
        "AAPL", frequency="5m", start=_dt(_BASE), end=_dt(_BASE + 300), normalization=SPLIT
    )
    assert len(out) == 1
    assert _arg(runner.calls[0], "--normalization") == "split-adjusted"
