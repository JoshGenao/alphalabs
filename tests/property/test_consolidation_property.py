"""SRS-SDK-007 / SyRS SYS-30a — bar-consolidation property tests (L2).

Generalises the fixed-walk domain proof over varied ascending minute series and every
supported period, pinning three invariants:

1. **Independent-oracle parity** — :func:`atp_strategy.consolidate_bars` equals a pandas
   ``DataFrame.resample(...).agg(OHLC)`` reference (the canonical resample), exactly as the
   indicator suite pins its wrappers against pandas-ta / TA-Lib. Intraday periods resample on
   a UTC index; daily resamples on a US-Eastern index (session-date grouping).
2. **Streaming == batch** — feeding the series through :meth:`TimeBarConsolidator.update`
   plus a final :meth:`~TimeBarConsolidator.flush` yields byte-identical bars to the batch
   call (the live/backtest parity invariant, ``AC-14``).
3. **Aggregation invariants** — total volume conserved, one output bar per non-empty
   bucket, ascending output, and each output extremum equals its members' extremum.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from atp_strategy import Bar, TimeBarConsolidator, consolidate_bars  # noqa: E402
from atp_strategy.resample import SUPPORTED_PERIODS  # noqa: E402

pytestmark = pytest.mark.property

# A fixed base epoch inside May 2026 (UTC). The generated span stays within a few EDT days,
# so oracle and engine share one DST regime — and both resolve the timezone via zoneinfo, so
# they would agree across a transition anyway.
_BASE_EPOCH = 1_777_593_600  # 2026-05-01T00:00:00+00:00

_PRICE = st.floats(min_value=0.01, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_VOLUME = st.integers(min_value=0, max_value=1_000_000)
_PANDAS_RULE = {"5m": "5min", "15m": "15min", "1h": "1h"}
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


@st.composite
def _minute_series(draw: st.DrawFn) -> list[Bar]:
    """An ascending, single-symbol series with per-bar-random OHLCV and 15s–25min gaps.

    Arbitrary gaps (not a strict 1-minute cadence) exercise multi-bar buckets, empty
    buckets, and multi-day spans (daily grouping) in one generator; consolidation requires
    only ascending timestamps, not a fixed cadence.
    """
    n = draw(st.integers(min_value=1, max_value=160))
    gaps = draw(st.lists(st.integers(min_value=15, max_value=1500), min_size=n, max_size=n))
    opens = draw(st.lists(_PRICE, min_size=n, max_size=n))
    highs = draw(st.lists(_PRICE, min_size=n, max_size=n))
    lows = draw(st.lists(_PRICE, min_size=n, max_size=n))
    closes = draw(st.lists(_PRICE, min_size=n, max_size=n))
    volumes = draw(st.lists(_VOLUME, min_size=n, max_size=n))
    bars: list[Bar] = []
    epoch = _BASE_EPOCH
    for i in range(n):
        epoch += gaps[i]  # strictly increasing (gap >= 15s)
        ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
        bars.append(Bar("X", ts, opens[i], highs[i], lows[i], closes[i], volumes[i]))
    return bars


def _oracle(bars: list[Bar], period: str) -> list[Bar]:
    """Independent pandas resample reference for ``bars`` at ``period``."""
    if not bars:
        return []
    frame = pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        },
        index=pd.to_datetime([b.timestamp for b in bars], utc=True),
    )
    if period == "1d":
        resampled = frame.tz_convert("America/New_York").resample("1D").agg(_AGG)
    else:
        resampled = frame.resample(_PANDAS_RULE[period]).agg(_AGG)
    # An empty bucket has NaN open/high/low/close (resample sums empty volume to 0, so
    # how="all" would keep it — the default how="any" drops it on the NaN OHLC).
    resampled = resampled.dropna()
    out: list[Bar] = []
    for ts, row in zip(resampled.index, resampled.itertuples(index=False), strict=True):
        out.append(
            Bar("X", ts.isoformat(), float(row.open), float(row.high), float(row.low),
                float(row.close), int(row.volume))
        )
    return out


@given(bars=_minute_series(), period=st.sampled_from(sorted(SUPPORTED_PERIODS)))
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_batch_matches_pandas_oracle(bars: list[Bar], period: str) -> None:
    assert consolidate_bars(bars, period) == _oracle(bars, period)


@given(bars=_minute_series(), period=st.sampled_from(sorted(SUPPORTED_PERIODS)))
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_streaming_equals_batch(bars: list[Bar], period: str) -> None:
    c = TimeBarConsolidator(period)
    streamed = [e for e in (c.update(b) for b in bars) if e is not None]
    tail = c.flush()
    if tail is not None:
        streamed.append(tail)
    assert streamed == consolidate_bars(bars, period)


@given(bars=_minute_series(), period=st.sampled_from(sorted(SUPPORTED_PERIODS)))
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_aggregation_invariants(bars: list[Bar], period: str) -> None:
    out = consolidate_bars(bars, period)
    # Total volume is conserved (nothing dropped, nothing double-counted).
    assert sum(b.volume for b in out) == sum(b.volume for b in bars)
    # Output is ascending and one bar per distinct occupied bucket.
    ts_list = [b.timestamp for b in out]
    assert ts_list == sorted(ts_list)
    assert len(ts_list) == len(set(ts_list))
    # Per-bar sanity: high is the largest close-or-high seen, low the smallest — checked via
    # the whole-series extrema when everything lands in one bucket.
    if out and len({b.timestamp for b in _oracle(bars, period)}) == 1:
        assert out[0].high == max(b.high for b in bars)
        assert out[0].low == min(b.low for b in bars)
        assert out[0].open == bars[0].open
        assert out[0].close == bars[-1].close
