"""L7 domain — time-based bar consolidation is correct and live/backtest-identical (SRS-SDK-007).

Locks the SRS-SDK-007 / SyRS ``SYS-30a`` / StRS ``SN-1.21`` acceptance criterion and its
stakeholder success criterion ``SC-16``:

    "A strategy can subscribe to minute-resolution data and receive consolidated 5-minute,
     15-minute, and hourly bars via the strategy API **without pre-processed datasets**."

Two trading-domain invariants:

1. **Correctness on real minute data (SC-16).** From ONE minute series — the only input
   constructed in this test — the engine derives correct 5-minute, 15-minute, hourly, and
   daily OHLCV bars, checked against hand-computed literals (an oracle independent of the
   implementation). No 5m/15m/1h/1d dataset is ever supplied.

2. **Paper/live parity (AC-14).** The streaming path a LIVE strategy drives from ``on_bar``
   (``update()`` per bar + a final ``flush()``) produces bars byte-identical to the batch
   path a BACKTEST / warm-up drives (``consolidate_bars`` over the replayed history). A
   consolidated bar must not depend on execution mode or a wall clock — it is a pure
   function of the minute bars, so a signal computed on consolidated bars fires identically
   in simulation and in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from atp_strategy import Bar, TimeBarConsolidator, consolidate_bars  # noqa: E402

pytestmark = [pytest.mark.domain, pytest.mark.safety]


# A hand-built 10-minute micro session (09:30–09:39 ET = 13:30–13:39 UTC, 2026-05-04, EDT).
# The ONLY dataset this test constructs is minute bars — everything else is derived.
_MINUTE = [
    #    timestamp (UTC)                 open  high  low  close  volume
    Bar("AAPL", "2026-05-04T13:30:00+00:00", 100.0, 105.0, 99.0, 102.0, 10),
    Bar("AAPL", "2026-05-04T13:31:00+00:00", 102.0, 108.0, 101.0, 107.0, 20),
    Bar("AAPL", "2026-05-04T13:32:00+00:00", 107.0, 110.0, 106.0, 109.0, 30),
    Bar("AAPL", "2026-05-04T13:33:00+00:00", 109.0, 111.0, 104.0, 105.0, 40),
    Bar("AAPL", "2026-05-04T13:34:00+00:00", 105.0, 106.0, 100.0, 101.0, 50),
    Bar("AAPL", "2026-05-04T13:35:00+00:00", 101.0, 103.0, 98.0, 99.0, 60),
    Bar("AAPL", "2026-05-04T13:36:00+00:00", 99.0, 100.0, 95.0, 96.0, 70),
    Bar("AAPL", "2026-05-04T13:37:00+00:00", 96.0, 97.0, 90.0, 92.0, 80),
    Bar("AAPL", "2026-05-04T13:38:00+00:00", 92.0, 94.0, 91.0, 93.0, 90),
    Bar("AAPL", "2026-05-04T13:39:00+00:00", 93.0, 120.0, 88.0, 115.0, 100),
]


def _tuple(bar: Bar) -> tuple[str, float, float, float, float, int]:
    return (bar.timestamp, bar.open, bar.high, bar.low, bar.close, bar.volume)


def test_sc16_five_minute_bars_from_minute_data() -> None:
    out = consolidate_bars(_MINUTE, "5m")
    assert [_tuple(b) for b in out] == [
        # [13:30,13:35): bars 0–4 → o=100 h=max(105..106)=111 l=min(99..100)=99 c=101 v=150
        ("2026-05-04T13:30:00+00:00", 100.0, 111.0, 99.0, 101.0, 150),
        # [13:35,13:40): bars 5–9 → o=101 h=120 l=88 c=115 v=400
        ("2026-05-04T13:35:00+00:00", 101.0, 120.0, 88.0, 115.0, 400),
    ]


def test_sc16_fifteen_minute_bar_from_minute_data() -> None:
    out = consolidate_bars(_MINUTE, "15m")
    # All ten bars fall in [13:30,13:45): o=100 h=120 l=88 c=115 v=550.
    assert [_tuple(b) for b in out] == [
        ("2026-05-04T13:30:00+00:00", 100.0, 120.0, 88.0, 115.0, 550),
    ]


def test_sc16_hourly_bar_from_minute_data() -> None:
    out = consolidate_bars(_MINUTE, "1h")
    # All ten bars fall in the 13:00 UTC hour bucket.
    assert [_tuple(b) for b in out] == [
        ("2026-05-04T13:00:00+00:00", 100.0, 120.0, 88.0, 115.0, 550),
    ]


def test_daily_bar_groups_the_whole_eastern_session() -> None:
    out = consolidate_bars(_MINUTE, "1d")
    # One ET-May-4 session → one daily bar, labelled at ET midnight (EDT −04:00).
    assert [_tuple(b) for b in out] == [
        ("2026-05-04T00:00:00-04:00", 100.0, 120.0, 88.0, 115.0, 550),
    ]


def _rth_session(date: str, *, tz_offset: str, minutes: int = 390) -> list[Bar]:
    """A synthetic regular-session minute series (09:30 ET open) with a deterministic shape."""
    hh, mm = 13, 30  # 09:30 ET == 13:30 UTC during EDT
    bars: list[Bar] = []
    for i in range(minutes):
        total = hh * 60 + mm + i
        ts = f"{date}T{total // 60:02d}:{total % 60:02d}:00{tz_offset}"
        # A gentle saw-tooth so highs/lows/opens/closes all differ per bar.
        base = 100.0 + (i % 37) * 0.25
        bars.append(Bar("SPY", ts, base, base + 1.5, base - 1.5, base + 0.5, 1000 + i))
    return bars


@pytest.mark.parametrize("period", ["5m", "15m", "1h", "1d"])
def test_ac14_streaming_equals_batch_over_a_full_session(period: str) -> None:
    # A live strategy drives update() per on_bar; a backtest drives consolidate_bars over the
    # replayed history. The two MUST agree bar-for-bar (paper/live parity).
    bars = _rth_session("2026-05-04", tz_offset="+00:00")

    consolidator = TimeBarConsolidator(period)
    streamed: list[Bar] = []
    for bar in bars:
        completed = consolidator.update(bar)
        if completed is not None:
            streamed.append(completed)
    tail = consolidator.flush()
    if tail is not None:
        streamed.append(tail)

    batched = consolidate_bars(bars, period)
    assert streamed == batched
    assert len(batched) >= 1


def test_ac14_parity_holds_across_a_multi_day_span() -> None:
    # Two ET sessions back to back: daily consolidation yields exactly two bars, and streaming
    # still equals batch across the day boundary.
    bars = _rth_session("2026-05-04", tz_offset="+00:00") + _rth_session(
        "2026-05-05", tz_offset="+00:00"
    )
    for period in ("5m", "15m", "1h", "1d"):
        consolidator = TimeBarConsolidator(period)
        streamed = [c for c in (consolidator.update(b) for b in bars) if c is not None]
        tail = consolidator.flush()
        if tail is not None:
            streamed.append(tail)
        assert streamed == consolidate_bars(bars, period), period
    assert len(consolidate_bars(bars, "1d")) == 2


@pytest.mark.parametrize("period", ["5m", "15m", "1h"])
def test_final_session_bucket_is_delivered_by_flush_at_close(period: str) -> None:
    # The reviewer's live-parity hazard: update() emits a bucket only when a LATER bar opens the
    # next one. At market close there is no next bar, so the CLOSING bucket is delivered only by
    # flush() (what a live strategy wires to ctx.schedule.at_market_close, and what the runtime does
    # for ctx.consolidate). Prove: update-only DROPS the final bucket; update + a single flush at
    # close delivers it and matches the backtest exactly — no next-period bar required.
    bars = _rth_session("2026-05-04", tz_offset="+00:00")  # 09:30–15:59 ET, 390 minute bars
    backtest = consolidate_bars(bars, period)  # includes the closing bucket implicitly

    consolidator = TimeBarConsolidator(period)
    live_updates = [c for c in (consolidator.update(b) for b in bars) if c is not None]

    # update() alone never emitted the last bucket (no bar after the session close opened a new one).
    assert live_updates == backtest[:-1]
    assert live_updates != backtest

    final = consolidator.flush()  # the at_market_close / runtime flush
    assert final is not None
    assert final == backtest[-1]  # the exact closing bucket a backtest produced
    assert live_updates + [final] == backtest  # live (with close flush) == backtest, bar-for-bar


def test_consolidation_is_deterministic_no_wall_clock() -> None:
    # Pure function of the input: repeated calls are identical (no now()/random), the property
    # that makes a backtest reproducible and a live signal match its simulation.
    a = consolidate_bars(_MINUTE, "5m")
    b = consolidate_bars(_MINUTE, "5m")
    assert a == b
