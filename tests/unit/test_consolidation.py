"""L1 unit tests for time-based bar consolidation (``SRS-SDK-007`` / SyRS ``SYS-30a``).

Pins the pure consolidation engine (:func:`atp_strategy.consolidate_bars` and
:class:`atp_strategy.TimeBarConsolidator`) that turns a minute series into 5-minute,
15-minute, hourly, and daily bars **without pre-processed datasets** (``SC-16``):

* correct OHLCV aggregation (open=first, high=max, low=min, close=last, volume=sum);
* correct bucket alignment (intraday epoch-floored; daily by US-Eastern calendar date);
* the streaming ``update()`` emits only on bucket close, ``flush()`` the trailing partial;
* streaming and batch agree; and
* fail-closed on a naive timestamp, unknown period, mixed symbols, or out-of-order input.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import Bar, TimeBarConsolidator, consolidate_bars  # noqa: E402
from atp_strategy.resample import SUPPORTED_PERIODS  # noqa: E402

pytestmark = pytest.mark.unit

_UTC = "+00:00"


def _bar(
    ts: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: int,
    symbol: str = "AAPL",
) -> Bar:
    return Bar(symbol, ts, open_, high, low, close, volume)


def _minute_walk(start_hh_mm: str, count: int, *, date: str = "2026-05-04") -> list[Bar]:
    """`count` consecutive 1-minute bars from ``date T start_hh_mm`` UTC, distinct OHLCV per bar."""
    hh, mm = (int(x) for x in start_hh_mm.split(":"))
    bars: list[Bar] = []
    for i in range(count):
        total = hh * 60 + mm + i
        ts = f"{date}T{total // 60:02d}:{total % 60:02d}:00{_UTC}"
        # open=100+i, high=110+i, low=90+i, close=105+i, volume=10+i — all distinct so
        # first/last/max/min/sum are individually observable.
        bars.append(_bar(ts, 100.0 + i, 110.0 + i, 90.0 + i, 105.0 + i, 10 + i))
    return bars


# --------------------------------------------------------------------------- #
# OHLCV aggregation + bucket alignment
# --------------------------------------------------------------------------- #


def test_five_minute_bucket_ohlcv_and_label() -> None:
    bars = _minute_walk("13:30", 5)  # 13:30..13:34 → one 5m bucket
    out = consolidate_bars(bars, "5m")
    assert len(out) == 1
    (b,) = out
    assert b.symbol == "AAPL"
    assert b.timestamp == "2026-05-04T13:30:00+00:00"  # bucket left edge, UTC
    assert b.open == 100.0  # first bar's open
    assert b.high == 114.0  # max high (110 + 4)
    assert b.low == 90.0  # min low (90 + 0)
    assert b.close == 109.0  # last bar's close (105 + 4)
    assert b.volume == sum(range(10, 15))  # 10+11+12+13+14 = 60
    assert isinstance(b.volume, int)


def test_five_minute_splits_into_aligned_buckets() -> None:
    bars = _minute_walk("13:30", 12)  # 13:30..13:41 → buckets [30,35) [35,40) [40,45)
    out = consolidate_bars(bars, "5m")
    assert [b.timestamp for b in out] == [
        "2026-05-04T13:30:00+00:00",
        "2026-05-04T13:35:00+00:00",
        "2026-05-04T13:40:00+00:00",
    ]
    # First bucket = 5 bars, second = 5 bars, third = 2 bars (13:40, 13:41).
    assert [b.volume for b in out] == [sum(range(10, 15)), sum(range(15, 20)), sum(range(20, 22))]


def test_fifteen_minute_and_hourly_alignment() -> None:
    bars = _minute_walk("13:30", 40)  # 13:30..14:09
    out15 = consolidate_bars(bars, "15m")
    # 15m buckets align to :00/:15/:30/:45 → [13:30,13:45) [13:45,14:00) [14:00,14:15)
    assert [b.timestamp for b in out15] == [
        "2026-05-04T13:30:00+00:00",
        "2026-05-04T13:45:00+00:00",
        "2026-05-04T14:00:00+00:00",
    ]
    out1h = consolidate_bars(bars, "1h")
    # 1h buckets align to the hour → [13:00,14:00) has 13:30..13:59 (30 bars); [14:00,15:00) has 10.
    assert [b.timestamp for b in out1h] == [
        "2026-05-04T13:00:00+00:00",
        "2026-05-04T14:00:00+00:00",
    ]
    assert [b.volume for b in out1h] == [sum(range(10, 40)), sum(range(40, 50))]


def test_hourly_bucket_is_wall_clock_not_session_anchored() -> None:
    # 09:30 ET (13:30 UTC) falls in the 13:00 UTC hour bucket, not a 13:30-anchored one.
    bars = _minute_walk("13:30", 1)
    (b,) = consolidate_bars(bars, "1h")
    assert b.timestamp == "2026-05-04T13:00:00+00:00"


# --------------------------------------------------------------------------- #
# Daily = US-Eastern calendar date (not naive UTC date)
# --------------------------------------------------------------------------- #


def test_daily_groups_by_eastern_calendar_date() -> None:
    # 23:00 UTC May 4 = 19:00 EDT May 4; 03:00 UTC May 5 = 23:00 EDT May 4 — SAME ET date,
    # different UTC dates. A naive UTC-date grouping would wrongly split them.
    bars = [
        _bar("2026-05-04T14:00:00+00:00", 10.0, 11.0, 9.0, 10.5, 100),  # 10:00 ET May 4
        _bar("2026-05-04T23:00:00+00:00", 10.5, 12.0, 10.0, 11.0, 200),  # 19:00 ET May 4
        _bar("2026-05-05T03:00:00+00:00", 11.0, 13.0, 8.0, 12.0, 300),  # 23:00 ET May 4
        _bar("2026-05-05T14:00:00+00:00", 12.0, 12.5, 11.5, 12.2, 400),  # 10:00 ET May 5
    ]
    out = consolidate_bars(bars, "1d")
    assert len(out) == 2
    may4, may5 = out
    assert may4.timestamp == "2026-05-04T00:00:00-04:00"  # ET midnight (EDT)
    assert may4.open == 10.0 and may4.high == 13.0 and may4.low == 8.0 and may4.close == 12.0
    assert may4.volume == 600
    assert may5.timestamp == "2026-05-05T00:00:00-04:00"
    assert may5.volume == 400


# --------------------------------------------------------------------------- #
# Streaming update()/flush()
# --------------------------------------------------------------------------- #


def test_streaming_emits_only_on_bucket_close() -> None:
    bars = _minute_walk("13:30", 12)  # three 5m buckets (5,5,2)
    c = TimeBarConsolidator("5m")
    emitted = [c.update(b) for b in bars]
    # A completed bar is emitted on the FIRST bar of each new bucket: at index 5 (13:35 opens
    # bucket 2, closing bucket 1) and index 10 (13:40 opens bucket 3, closing bucket 2).
    non_none = [i for i, e in enumerate(emitted) if e is not None]
    assert non_none == [5, 10]
    assert emitted[5].timestamp == "2026-05-04T13:30:00+00:00"
    assert emitted[10].timestamp == "2026-05-04T13:35:00+00:00"
    tail = c.flush()
    assert tail is not None and tail.timestamp == "2026-05-04T13:40:00+00:00"
    assert c.flush() is None  # idempotent: nothing left


def test_streaming_matches_batch() -> None:
    for period in ("5m", "15m", "1h", "1d"):
        bars = _minute_walk("13:30", 200)
        c = TimeBarConsolidator(period)
        streamed = [e for e in (c.update(b) for b in bars) if e is not None]
        final = c.flush()
        if final is not None:
            streamed.append(final)
        assert streamed == consolidate_bars(bars, period), period


def test_period_property_and_supported_set() -> None:
    assert TimeBarConsolidator("15m").period == "15m"
    assert SUPPORTED_PERIODS == frozenset({"5m", "15m", "1h", "1d"})


# --------------------------------------------------------------------------- #
# BarConsolidator Protocol pull surface
# --------------------------------------------------------------------------- #


def test_consolidate_protocol_method_over_bound_source() -> None:
    bars = _minute_walk("13:30", 10)
    other = [_bar(b.timestamp, 1.0, 1.0, 1.0, 1.0, 1, symbol="MSFT") for b in bars]
    c = TimeBarConsolidator("5m", source=sorted(bars + other, key=lambda b: b.timestamp))
    out = list(c.consolidate("AAPL", period="5m"))
    assert out == consolidate_bars(bars, "5m")  # filtered to the requested symbol only


def test_consolidate_rejects_period_mismatch_and_missing_source() -> None:
    with pytest.raises(ValueError, match="bound to period"):
        list(TimeBarConsolidator("5m", source=[]).consolidate("AAPL", period="15m"))
    with pytest.raises(ValueError, match="requires a bound bar source"):
        list(TimeBarConsolidator("5m").consolidate("AAPL", period="5m"))


# --------------------------------------------------------------------------- #
# Fail-closed + edge cases
# --------------------------------------------------------------------------- #


def test_empty_input_yields_empty() -> None:
    assert consolidate_bars([], "5m") == []
    assert TimeBarConsolidator("1d").flush() is None


def test_unknown_period_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported consolidation period"):
        consolidate_bars(_minute_walk("13:30", 3), "30m")
    with pytest.raises(ValueError, match="unsupported consolidation period"):
        TimeBarConsolidator("2h")


def test_naive_timestamp_fails_closed() -> None:
    naive = [_bar("2026-05-04T13:30:00", 1.0, 1.0, 1.0, 1.0, 1)]  # no offset
    with pytest.raises(ValueError, match="timezone-aware"):
        consolidate_bars(naive, "5m")
    with pytest.raises(ValueError, match="timezone-aware"):
        TimeBarConsolidator("5m").update(naive[0])


def test_mixed_symbols_fail_closed() -> None:
    bars = [
        _bar("2026-05-04T13:30:00+00:00", 1.0, 1.0, 1.0, 1.0, 1, symbol="AAPL"),
        _bar("2026-05-04T13:31:00+00:00", 1.0, 1.0, 1.0, 1.0, 1, symbol="MSFT"),
    ]
    with pytest.raises(ValueError, match="single symbol"):
        consolidate_bars(bars, "5m")
    c = TimeBarConsolidator("5m")
    c.update(bars[0])
    with pytest.raises(ValueError, match="bound to symbol"):
        c.update(bars[1])


def test_out_of_order_timestamps_fail_closed() -> None:
    bars = [
        _bar("2026-05-04T13:31:00+00:00", 1.0, 1.0, 1.0, 1.0, 1),
        _bar("2026-05-04T13:30:00+00:00", 1.0, 1.0, 1.0, 1.0, 1),
    ]
    with pytest.raises(ValueError, match="ascending"):
        consolidate_bars(bars, "5m")
    c = TimeBarConsolidator("5m")
    c.update(bars[0])
    with pytest.raises(ValueError, match="ascending"):
        c.update(bars[1])
