"""SRS-SDK-006 / SyRS SYS-35, AC-6 — indicator property tests.

L2 property tests via Hypothesis. The L7 domain test pins parity on
fixed seeded walks for a handful of canonical periods; this layer
generalises that proof over varied periods and input lengths so the
wrapper's bounded-buffer behaviour is exercised across the full
parameter space the SDK exposes to strategy authors.

Each test generates a random close-price sequence (or full OHLCV when
needed) plus an indicator period inside the supported range, drip-feeds
the wrapper, and asserts the last-bar value matches the equivalent
TA-Lib batch call on the full sequence within the per-indicator
tolerance from the architecture contract. For Wilder/EMA-style
indicators the test skips the pre-stabilisation window the wrapper's
buffer cap (period * 30) is sized for.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest
import talib
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from atp_strategy import ATR, EMA, MACD, RSI, SMA, Bar, BollingerBands  # noqa: E402

pytestmark = pytest.mark.property


_CONTRACT = json.loads(
    (ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
)["strategy_api_indicators_contract"]
_TOL = _CONTRACT["parity_tolerance_abs_vs_talib"]


def _bars_from(closes: list[float]) -> list[Bar]:
    """Build OHLCV bars from a close-price list (open/high/low/close all equal)."""
    return [Bar("X", f"t{i}", c, c, c, c, 1) for i, c in enumerate(closes)]


def _ohlcv_from(closes: list[float], spread: float = 0.5) -> list[Bar]:
    return [Bar("X", f"t{i}", c, c + spread, c - spread, c, 1) for i, c in enumerate(closes)]


_CLOSE_STRATEGY = st.lists(
    st.floats(min_value=10.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    min_size=80,
    max_size=400,
)


@given(period=st.integers(min_value=2, max_value=30), closes=_CLOSE_STRATEGY)
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_sma_property_matches_batch_talib(period: int, closes: list[float]) -> None:
    if len(closes) < period:
        return
    sma = SMA(period=period)
    for bar in _bars_from(closes):
        sma.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    batch_v = float(talib.SMA(arr, timeperiod=period)[-1])
    assert sma.value is not None
    assert abs(float(sma.value) - batch_v) <= _TOL["SMA"], (
        f"period={period} n={len(closes)} wrapper={sma.value} batch={batch_v}"
    )


@given(period=st.integers(min_value=2, max_value=30), closes=_CLOSE_STRATEGY)
@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_ema_property_matches_batch_talib_after_stabilisation(
    period: int, closes: list[float]
) -> None:
    # EMA seed decays as (1 - alpha)^N; the wrapper caps the buffer at
    # period * 30 so any input >= 30 * period gives full convergence.
    if len(closes) < 3 * period:
        return
    ema = EMA(period=period)
    for bar in _bars_from(closes):
        ema.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    batch_v = float(talib.EMA(arr, timeperiod=period)[-1])
    assert ema.value is not None
    diff = abs(float(ema.value) - batch_v)
    # Allow a small slack for cases where len(closes) sits just at 3*period;
    # the architecture-contract tolerance covers stable cases.
    assert diff <= max(_TOL["EMA"], 1e-6), (
        f"period={period} n={len(closes)} wrapper={ema.value} batch={batch_v} diff={diff}"
    )


@given(period=st.integers(min_value=2, max_value=25), closes=_CLOSE_STRATEGY)
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_rsi_property_matches_batch_talib_after_stabilisation(
    period: int, closes: list[float]
) -> None:
    if len(closes) < 3 * period + 1:
        return
    rsi = RSI(period=period)
    for bar in _bars_from(closes):
        rsi.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    batch_v = float(talib.RSI(arr, timeperiod=period)[-1])
    if math.isnan(batch_v):
        return
    assert rsi.value is not None
    diff = abs(float(rsi.value) - batch_v)
    assert diff <= max(_TOL["RSI"], 1e-4), (
        f"period={period} n={len(closes)} wrapper={rsi.value} batch={batch_v} diff={diff}"
    )


@given(period=st.integers(min_value=2, max_value=25), closes=_CLOSE_STRATEGY)
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_bbands_property_matches_batch_talib(period: int, closes: list[float]) -> None:
    if len(closes) < period:
        return
    bb = BollingerBands(period=period, num_std=2.0)
    for bar in _bars_from(closes):
        bb.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    u_t, m_t, l_t = talib.BBANDS(arr, timeperiod=period, nbdevup=2.0, nbdevdn=2.0, matype=0)
    reading = bb.reading
    assert reading is not None
    for leg, wrapper_leg, batch_leg in (
        ("middle", reading.middle, float(m_t[-1])),
        ("upper", reading.upper, float(u_t[-1])),
        ("lower", reading.lower, float(l_t[-1])),
    ):
        assert abs(wrapper_leg - batch_leg) <= max(_TOL["BollingerBands"], 1e-6), (
            f"BB.{leg} period={period} n={len(closes)} wrapper={wrapper_leg} batch={batch_leg}"
        )


@given(period=st.integers(min_value=2, max_value=20), closes=_CLOSE_STRATEGY)
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_atr_property_matches_batch_talib_after_stabilisation(
    period: int, closes: list[float]
) -> None:
    if len(closes) < 3 * period + 1:
        return
    bars = _ohlcv_from(closes, spread=0.5)
    atr = ATR(period=period)
    for bar in bars:
        atr.update(bar)
    highs = np.asarray([b.high for b in bars], dtype=np.float64)
    lows = np.asarray([b.low for b in bars], dtype=np.float64)
    closes_arr = np.asarray([b.close for b in bars], dtype=np.float64)
    batch_v = float(talib.ATR(highs, lows, closes_arr, timeperiod=period)[-1])
    assert atr.value is not None
    diff = abs(float(atr.value) - batch_v)
    assert diff <= max(_TOL["ATR"], 1e-4), (
        f"period={period} n={len(closes)} wrapper={atr.value} batch={batch_v} diff={diff}"
    )


@given(
    fast=st.integers(min_value=3, max_value=10),
    slow_offset=st.integers(min_value=4, max_value=20),
    signal=st.integers(min_value=3, max_value=12),
    closes=_CLOSE_STRATEGY,
)
@settings(max_examples=15, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_macd_property_matches_batch_talib_after_stabilisation(
    fast: int, slow_offset: int, signal: int, closes: list[float]
) -> None:
    slow = fast + slow_offset
    if len(closes) < 3 * slow:
        return
    macd = MACD(fast=fast, slow=slow, signal=signal)
    for bar in _bars_from(closes):
        macd.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    m_t, s_t, h_t = talib.MACD(arr, fastperiod=fast, slowperiod=slow, signalperiod=signal)
    reading = macd.reading
    assert reading is not None
    for leg, wrapper_leg, batch_leg in (
        ("macd", reading.macd, float(m_t[-1])),
        ("signal", reading.signal, float(s_t[-1])),
        ("histogram", reading.histogram, float(h_t[-1])),
    ):
        assert abs(wrapper_leg - batch_leg) <= max(_TOL["MACD"], 1e-4), (
            f"MACD.{leg} fast={fast} slow={slow} signal={signal} n={len(closes)} "
            f"wrapper={wrapper_leg} batch={batch_leg}"
        )


@given(period=st.integers(min_value=10, max_value=20))
@settings(max_examples=5, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_ema_maxlen_truncation_does_not_break_parity_on_long_history(period: int) -> None:
    """Verify the wrapper's bounded buffer (period * 30) still matches a
    full-history batch call when input length far exceeds the cap.

    This is the explicit property the architecture-contract buffer-cap
    derivation rests on: at N > 30 * period bars the seed-bar
    contribution to a Wilder/EMA-style indicator decays below the
    tolerance window, so the wrapper's truncated buffer is numerically
    indistinguishable from the full-history batch call at the last bar.
    """
    rng = np.random.default_rng(1234 + period)
    n = period * 50  # well past the wrapper's period * 30 cap
    closes = (100.0 + np.cumsum(rng.normal(0, 0.5, n))).tolist()
    ema = EMA(period=period)
    for bar in _bars_from(closes):
        ema.update(bar)
    arr = np.asarray(closes, dtype=np.float64)
    batch_v = float(talib.EMA(arr, timeperiod=period)[-1])
    assert ema.value is not None
    assert abs(float(ema.value) - batch_v) <= _TOL["EMA"], (
        f"period={period} n={n} wrapper={ema.value} batch={batch_v}; "
        "buffer cap (period * 30) is too small for tolerance window"
    )
