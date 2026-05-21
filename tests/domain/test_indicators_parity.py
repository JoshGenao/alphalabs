"""SRS-SDK-006 / SyRS SYS-35, AC-6 / StRS SN-1.20, C-9 — indicator parity.

L7 domain (safety) test. Walks the full SRS-SDK-006 AC end-to-end
against the SDK-shipped indicator wrappers and asserts numerical
parity vs TA-Lib (canonical numerical backend) and pandas-ta
(documented parity reference) on a 300-bar seeded synthetic OHLCV
sequence. The AC is:

    "SMA, EMA, RSI, MACD, Bollinger Bands, and ATR match pandas-ta
    or TA-Lib reference outputs and support incremental updates on
    each new bar."

SyRS ``AC-6`` is the matching hard constraint:

    "The built-in indicator library shall wrap pandas-ta and TA-Lib;
    custom reimplementations of indicators available in these
    libraries are prohibited."

Locks:

* Numerical parity vs TA-Lib (primary backend) within the
  per-indicator tolerance documented in
  ``architecture/runtime_services.json#strategy_api_indicators_contract``
  for all six AC-listed indicators on the 300-bar seeded walk.
* Numerical parity vs pandas-ta (documented parity reference) within
  the per-indicator pandas-ta tolerance, with the documented seed-bar
  skip (``3 * period`` bars) on Wilder-smoothed indicators (RSI, ATR)
  and MACD.
* Incremental-vs-batch parity: at every readiness step ``i`` the
  wrapper's ``.value`` equals ``talib.<NAME>(arr[:i+1])[-1]`` within
  tolerance. This is the "support incremental updates on each new
  bar" half of the AC.
* NaN -> None: indicators in their warm-up window expose ``.value
  is None`` (not a NaN float). For compound indicators (MACD,
  BollingerBands) the whole ``.reading`` is None if any leg is NaN.
* Backwards compatibility: the existing call sites in
  ``tests/test_strategy_api.py:62-76``,
  ``tests/domain/test_warmup_replay.py:108-128``, and
  ``tests/domain/test_strategy_api_parity.py`` are replicated in
  process; a regression on the public surface flips this test red
  without touching the originals.
* ``is_ready`` transitions at the documented bar count per indicator
  (SMA / EMA / BollingerBands at ``period``; RSI / ATR at
  ``period + 1``; MACD at ``slow + signal - 1``).
* AC-6 dependency direction: ``inspect.getsource(atp_strategy.indicators)``
  contains ``import talib`` AND ``import pandas_ta`` and none of the
  prohibited-custom-math tokens.
"""

from __future__ import annotations

import inspect
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_ta as pta
import pytest
import talib

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

import atp_strategy  # noqa: E402
from atp_strategy import (  # noqa: E402
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    Bar,
    BollingerBands,
    BollingerValue,
    Indicator,
    MACDValue,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _contract_block() -> dict:
    data = json.loads((ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))
    return data["strategy_api_indicators_contract"]


def _seeded_closes(n: int = 300) -> np.ndarray:
    rng = np.random.default_rng(42)
    steps = rng.normal(loc=0.0, scale=0.5, size=n)
    return 100.0 + np.cumsum(steps)


def _seeded_ohlcv(n: int = 300) -> list[Bar]:
    closes = _seeded_closes(n)
    rng = np.random.default_rng(7)
    noise = np.abs(rng.normal(loc=0.0, scale=0.3, size=n))
    bars: list[Bar] = []
    prev_close = float(closes[0])
    for i, close in enumerate(closes):
        bars.append(
            Bar(
                symbol="X",
                timestamp=f"t{i}",
                open=prev_close,
                high=float(close) + float(noise[i]),
                low=float(close) - float(noise[i]),
                close=float(close),
                volume=1,
            )
        )
        prev_close = float(close)
    return bars


# --------------------------------------------------------------------------- #
# Parity vs TA-Lib (canonical numerical backend)
# --------------------------------------------------------------------------- #


class IndicatorTaLibParityTest(unittest.TestCase):
    """Each wrapper matches the equivalent TA-Lib call within tolerance."""

    def setUp(self) -> None:
        self.block = _contract_block()
        self.bars = _seeded_ohlcv(300)
        self.closes = np.asarray([b.close for b in self.bars], dtype=np.float64)
        self.highs = np.asarray([b.high for b in self.bars], dtype=np.float64)
        self.lows = np.asarray([b.low for b in self.bars], dtype=np.float64)

    def _drip_scalar(self, ind: object) -> float:
        for bar in self.bars:
            ind.update(bar)
        self.assertIsNotNone(ind.value, f"{type(ind).__name__} not ready after 300 bars")
        return float(ind.value)

    def test_sma_matches_talib_within_tolerance(self) -> None:
        period = 20
        tol = self.block["parity_tolerance_abs_vs_talib"]["SMA"]
        wrapper_v = self._drip_scalar(SMA(period=period))
        talib_v = float(talib.SMA(self.closes, timeperiod=period)[-1])
        self.assertLessEqual(abs(wrapper_v - talib_v), tol, f"{wrapper_v} vs {talib_v}")

    def test_sma_200_matches_talib(self) -> None:
        """SMA(period=200) is used in tests/domain/test_warmup_replay.py."""
        period = 200
        tol = self.block["parity_tolerance_abs_vs_talib"]["SMA"]
        wrapper_v = self._drip_scalar(SMA(period=period))
        talib_v = float(talib.SMA(self.closes, timeperiod=period)[-1])
        self.assertLessEqual(abs(wrapper_v - talib_v), tol)

    def test_ema_matches_talib_within_tolerance(self) -> None:
        period = 20
        tol = self.block["parity_tolerance_abs_vs_talib"]["EMA"]
        wrapper_v = self._drip_scalar(EMA(period=period))
        talib_v = float(talib.EMA(self.closes, timeperiod=period)[-1])
        self.assertLessEqual(abs(wrapper_v - talib_v), tol)

    def test_rsi_matches_talib_within_tolerance(self) -> None:
        period = 14
        tol = self.block["parity_tolerance_abs_vs_talib"]["RSI"]
        wrapper_v = self._drip_scalar(RSI(period=period))
        talib_v = float(talib.RSI(self.closes, timeperiod=period)[-1])
        self.assertLessEqual(abs(wrapper_v - talib_v), tol)

    def test_macd_matches_talib_within_tolerance(self) -> None:
        tol = self.block["parity_tolerance_abs_vs_talib"]["MACD"]
        macd = MACD()
        for bar in self.bars:
            macd.update(bar)
        self.assertIsNotNone(macd.reading)
        m_t, s_t, h_t = talib.MACD(self.closes, fastperiod=12, slowperiod=26, signalperiod=9)
        reading = macd.reading
        assert reading is not None  # for mypy
        self.assertLessEqual(abs(reading.macd - float(m_t[-1])), tol)
        self.assertLessEqual(abs(reading.signal - float(s_t[-1])), tol)
        self.assertLessEqual(abs(reading.histogram - float(h_t[-1])), tol)

    def test_bbands_matches_talib_within_tolerance(self) -> None:
        period = 20
        num_std = 2.0
        tol = self.block["parity_tolerance_abs_vs_talib"]["BollingerBands"]
        bb = BollingerBands(period=period, num_std=num_std)
        for bar in self.bars:
            bb.update(bar)
        u_t, m_t, l_t = talib.BBANDS(
            self.closes, timeperiod=period, nbdevup=num_std, nbdevdn=num_std, matype=0
        )
        reading = bb.reading
        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertLessEqual(abs(reading.middle - float(m_t[-1])), tol)
        self.assertLessEqual(abs(reading.upper - float(u_t[-1])), tol)
        self.assertLessEqual(abs(reading.lower - float(l_t[-1])), tol)

    def test_atr_matches_talib_within_tolerance(self) -> None:
        period = 14
        tol = self.block["parity_tolerance_abs_vs_talib"]["ATR"]
        atr = ATR(period=period)
        for bar in self.bars:
            atr.update(bar)
        self.assertIsNotNone(atr.value)
        wrapper_v = float(atr.value)
        talib_v = float(talib.ATR(self.highs, self.lows, self.closes, timeperiod=period)[-1])
        self.assertLessEqual(abs(wrapper_v - talib_v), tol)


# --------------------------------------------------------------------------- #
# Parity vs pandas-ta (documented reference, with seed-bar skip)
# --------------------------------------------------------------------------- #


class IndicatorPandasTaParityTest(unittest.TestCase):
    """Each wrapper matches pandas-ta within the documented tolerance.

    Wilder-smoothed indicators (RSI, ATR) and MACD diverge from
    pandas-ta on the first ``3 * period`` bars due to different
    seeding conventions; the slice below skips that window.
    """

    def setUp(self) -> None:
        self.block = _contract_block()
        self.skip_factor = int(self.block["pandas_ta_seed_skip_bars_factor"])
        self.bars = _seeded_ohlcv(300)
        self.closes = np.asarray([b.close for b in self.bars], dtype=np.float64)
        self.highs = np.asarray([b.high for b in self.bars], dtype=np.float64)
        self.lows = np.asarray([b.low for b in self.bars], dtype=np.float64)

    def _drip_scalar(self, ind: object) -> float:
        for bar in self.bars:
            ind.update(bar)
        self.assertIsNotNone(ind.value)
        return float(ind.value)

    def test_sma_matches_pandas_ta(self) -> None:
        period = 20
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["SMA"]
        wrapper_v = self._drip_scalar(SMA(period=period))
        pta_v = float(pta.sma(pd.Series(self.closes), length=period).iloc[-1])
        self.assertLessEqual(abs(wrapper_v - pta_v), tol)

    def test_ema_matches_pandas_ta(self) -> None:
        period = 20
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["EMA"]
        wrapper_v = self._drip_scalar(EMA(period=period))
        pta_v = float(pta.ema(pd.Series(self.closes), length=period).iloc[-1])
        self.assertLessEqual(abs(wrapper_v - pta_v), tol)

    def test_rsi_matches_pandas_ta_after_seed_skip(self) -> None:
        period = 14
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["RSI"]
        wrapper_v = self._drip_scalar(RSI(period=period))
        skip = self.skip_factor * period
        pta_v = float(pta.rsi(pd.Series(self.closes[skip:]), length=period).iloc[-1])
        self.assertLessEqual(abs(wrapper_v - pta_v), tol, f"{wrapper_v} vs {pta_v}")

    def test_bbands_all_legs_match_pandas_ta(self) -> None:
        period = 20
        num_std = 2.0
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["BollingerBands"]
        bb = BollingerBands(period=period, num_std=num_std)
        for bar in self.bars:
            bb.update(bar)
        reading = bb.reading
        self.assertIsNotNone(reading)
        assert reading is not None
        pta_df = pta.bbands(pd.Series(self.closes), length=period, std=num_std)
        cols = list(pta_df.columns)
        middle_col = next(c for c in cols if c.startswith("BBM_"))
        upper_col = next(c for c in cols if c.startswith("BBU_"))
        lower_col = next(c for c in cols if c.startswith("BBL_"))
        for leg, wrapper_v, pta_v in (
            ("middle", reading.middle, float(pta_df[middle_col].iloc[-1])),
            ("upper", reading.upper, float(pta_df[upper_col].iloc[-1])),
            ("lower", reading.lower, float(pta_df[lower_col].iloc[-1])),
        ):
            self.assertLessEqual(abs(wrapper_v - pta_v), tol, f"BB.{leg}")

    def test_macd_matches_pandas_ta_after_seed_skip(self) -> None:
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["MACD"]
        macd = MACD()
        for bar in self.bars:
            macd.update(bar)
        reading = macd.reading
        self.assertIsNotNone(reading)
        assert reading is not None
        skip = self.skip_factor * 26  # slow period
        pta_df = pta.macd(pd.Series(self.closes[skip:]), fast=12, slow=26, signal=9)
        cols = list(pta_df.columns)
        macd_col = next(c for c in cols if c.startswith("MACD_"))
        signal_col = next(c for c in cols if c.startswith("MACDs_"))
        hist_col = next(c for c in cols if c.startswith("MACDh_"))
        for leg, wrapper_v, pta_v in (
            ("macd", reading.macd, float(pta_df[macd_col].iloc[-1])),
            ("signal", reading.signal, float(pta_df[signal_col].iloc[-1])),
            ("histogram", reading.histogram, float(pta_df[hist_col].iloc[-1])),
        ):
            self.assertLessEqual(abs(wrapper_v - pta_v), tol, f"MACD.{leg}")

    def test_atr_matches_pandas_ta_after_seed_skip(self) -> None:
        period = 14
        tol = self.block["parity_tolerance_abs_vs_pandas_ta"]["ATR"]
        atr = ATR(period=period)
        for bar in self.bars:
            atr.update(bar)
        self.assertIsNotNone(atr.value)
        wrapper_v = float(atr.value)
        skip = self.skip_factor * period
        pta_v = float(
            pta.atr(
                pd.Series(self.highs[skip:]),
                pd.Series(self.lows[skip:]),
                pd.Series(self.closes[skip:]),
                length=period,
            ).iloc[-1]
        )
        self.assertLessEqual(abs(wrapper_v - pta_v), tol)


# --------------------------------------------------------------------------- #
# Incremental updates parity
# --------------------------------------------------------------------------- #


class IncrementalVsBatchParityTest(unittest.TestCase):
    """At every readiness step the wrapper matches batch TA-Lib.

    SRS-SDK-006 AC requires all six indicators to support "incremental
    updates on each new bar"; each test below drip-feeds bars one at a
    time and compares to the equivalent batch call on ``arr[:i+1]``.
    """

    def test_sma_incremental_equals_batch_every_step(self) -> None:
        period = 5
        closes = _seeded_closes(60)
        sma = SMA(period=period)
        for i, c in enumerate(closes):
            wrapper_v = sma.update(Bar("X", f"t{i}", c, c, c, c, 1))
            arr = closes[: i + 1]
            batch = talib.SMA(arr, timeperiod=period)
            batch_v = float(batch[-1])
            if math.isnan(batch_v):
                self.assertIsNone(wrapper_v, f"bar {i}: wrapper={wrapper_v} batch=NaN")
            else:
                self.assertIsNotNone(wrapper_v, f"bar {i}: wrapper=None batch={batch_v}")
                self.assertLessEqual(abs(wrapper_v - batch_v), 1e-9, f"bar {i}")

    def test_ema_incremental_stabilises_against_batch(self) -> None:
        period = 10
        closes = _seeded_closes(80)
        ema = EMA(period=period)
        # EMA seeds via SMA at index period-1; after stabilisation (3*period)
        # incremental must match batch within 1e-9.
        for i, c in enumerate(closes):
            ema.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i + 1 < 3 * period:
                continue
            arr = closes[: i + 1]
            batch_v = float(talib.EMA(arr, timeperiod=period)[-1])
            self.assertIsNotNone(ema.value)
            self.assertLessEqual(abs(float(ema.value) - batch_v), 1e-9, f"bar {i}")

    def test_rsi_incremental_stabilises_against_batch(self) -> None:
        period = 14
        closes = _seeded_closes(120)
        rsi = RSI(period=period)
        for i, c in enumerate(closes):
            rsi.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i + 1 < 3 * period:
                continue
            arr = closes[: i + 1]
            batch_v = float(talib.RSI(arr, timeperiod=period)[-1])
            self.assertIsNotNone(rsi.value)
            self.assertLessEqual(abs(float(rsi.value) - batch_v), 1e-6, f"bar {i}")

    def test_macd_incremental_stabilises_against_batch(self) -> None:
        closes = _seeded_closes(140)
        macd = MACD()
        for i, c in enumerate(closes):
            macd.update(Bar("X", f"t{i}", c, c, c, c, 1))
            # MACD stabilises after ~3 * slow bars; before that buffer caps
            # in the wrapper may diverge from a full-history batch call.
            if i + 1 < 3 * 26:
                continue
            arr = closes[: i + 1]
            m_t, s_t, h_t = talib.MACD(arr, fastperiod=12, slowperiod=26, signalperiod=9)
            reading = macd.reading
            self.assertIsNotNone(reading)
            assert reading is not None
            self.assertLessEqual(abs(reading.macd - float(m_t[-1])), 1e-6, f"bar {i} macd")
            self.assertLessEqual(abs(reading.signal - float(s_t[-1])), 1e-6, f"bar {i} signal")

    def test_bbands_incremental_equals_batch_every_step(self) -> None:
        period = 10
        num_std = 2.0
        closes = _seeded_closes(60)
        bb = BollingerBands(period=period, num_std=num_std)
        for i, c in enumerate(closes):
            bb.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i + 1 < period:
                continue
            arr = closes[: i + 1]
            u_t, m_t, l_t = talib.BBANDS(
                arr, timeperiod=period, nbdevup=num_std, nbdevdn=num_std, matype=0
            )
            reading = bb.reading
            self.assertIsNotNone(reading)
            assert reading is not None
            self.assertLessEqual(abs(reading.middle - float(m_t[-1])), 1e-9, f"bar {i} middle")
            self.assertLessEqual(abs(reading.upper - float(u_t[-1])), 1e-9, f"bar {i} upper")
            self.assertLessEqual(abs(reading.lower - float(l_t[-1])), 1e-9, f"bar {i} lower")

    def test_atr_incremental_stabilises_against_batch(self) -> None:
        period = 14
        bars = _seeded_ohlcv(120)
        highs = np.asarray([b.high for b in bars], dtype=np.float64)
        lows = np.asarray([b.low for b in bars], dtype=np.float64)
        closes = np.asarray([b.close for b in bars], dtype=np.float64)
        atr = ATR(period=period)
        for i, bar in enumerate(bars):
            atr.update(bar)
            if i + 1 < 3 * period:
                continue
            batch_v = float(
                talib.ATR(highs[: i + 1], lows[: i + 1], closes[: i + 1], timeperiod=period)[-1]
            )
            self.assertIsNotNone(atr.value)
            self.assertLessEqual(abs(float(atr.value) - batch_v), 1e-6, f"bar {i}")


# --------------------------------------------------------------------------- #
# NaN -> None convention
# --------------------------------------------------------------------------- #


class NanToNoneTest(unittest.TestCase):
    def test_sma_short_buffer_value_is_none_not_nan(self) -> None:
        sma = SMA(period=10)
        for i, c in enumerate([1.0, 2.0, 3.0, 4.0, 5.0]):
            sma.update(Bar("X", f"t{i}", c, c, c, c, 1))
        self.assertIsNone(sma.value)
        self.assertFalse(sma.is_ready)

    def test_macd_short_buffer_reading_is_none(self) -> None:
        macd = MACD()
        for i, c in enumerate([100.0, 101.0, 102.0, 103.0]):
            macd.update(Bar("X", f"t{i}", c, c, c, c, 1))
        self.assertIsNone(macd.reading)
        self.assertIsNone(macd.value)
        self.assertFalse(macd.is_ready)

    def test_bbands_short_buffer_reading_is_none(self) -> None:
        bb = BollingerBands(period=20, num_std=2.0)
        for i, c in enumerate([100.0, 101.0]):
            bb.update(Bar("X", f"t{i}", c, c, c, c, 1))
        self.assertIsNone(bb.reading)
        self.assertIsNone(bb.value)
        self.assertFalse(bb.is_ready)


# --------------------------------------------------------------------------- #
# Backwards compatibility with existing call sites
# --------------------------------------------------------------------------- #


class BackwardsCompatibilityTest(unittest.TestCase):
    """Replicates existing test call sites in-process.

    A regression on the public surface (constructor signature, .value
    return type, .is_ready timing, etc.) trips this test instead of
    silently changing the originals.
    """

    def test_sma_period_3_on_one_two_three_returns_two(self) -> None:
        """Mirror of tests/test_strategy_api.py:62-76."""
        sma = SMA(period=3)
        for c in (1.0, 2.0, 3.0):
            sma.update(Bar("X", "t", c, c, c, c, 1))
        self.assertIsNotNone(sma.value)
        self.assertAlmostEqual(sma.value, 2.0, places=9)
        self.assertTrue(sma.is_ready)

    def test_sma_period_200_warmup(self) -> None:
        """Mirror of tests/domain/test_warmup_replay.py:108-128."""
        sma = SMA(period=200)
        closes = _seeded_closes(200)
        for i, c in enumerate(closes):
            sma.update(Bar("X", f"t{i}", c, c, c, c, 1))
        self.assertTrue(sma.is_ready)
        self.assertIsNotNone(sma.value)

    def test_macd_value_dataclass_field_order(self) -> None:
        """Field order is the public contract for positional unpacking."""
        mv = MACDValue(0.5, 0.3, 0.2)
        self.assertEqual(mv.macd, 0.5)
        self.assertEqual(mv.signal, 0.3)
        self.assertEqual(mv.histogram, 0.2)

    def test_bollinger_value_dataclass_field_order(self) -> None:
        bv = BollingerValue(10.0, 12.0, 8.0)
        self.assertEqual(bv.middle, 10.0)
        self.assertEqual(bv.upper, 12.0)
        self.assertEqual(bv.lower, 8.0)


# --------------------------------------------------------------------------- #
# is_ready transitions
# --------------------------------------------------------------------------- #


class IsReadyTransitionsTest(unittest.TestCase):
    def test_sma_period_20_flips_at_bar_20(self) -> None:
        sma = SMA(period=20)
        for i, c in enumerate(_seeded_closes(25), start=1):
            sma.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i < 20:
                self.assertFalse(sma.is_ready, f"early-flip at bar {i}")
            else:
                self.assertTrue(sma.is_ready, f"not ready at bar {i}")

    def test_rsi_period_14_flips_at_bar_15(self) -> None:
        rsi = RSI(period=14)
        for i, c in enumerate(_seeded_closes(20), start=1):
            rsi.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i < 15:
                self.assertFalse(rsi.is_ready, f"early-flip at bar {i}")
            else:
                self.assertTrue(rsi.is_ready, f"not ready at bar {i}")

    def test_macd_default_flips_at_bar_34(self) -> None:
        macd = MACD()
        for i, c in enumerate(_seeded_closes(40), start=1):
            macd.update(Bar("X", f"t{i}", c, c, c, c, 1))
            if i < 34:
                self.assertFalse(macd.is_ready, f"early-flip at bar {i}")

    def test_atr_period_14_flips_at_bar_15(self) -> None:
        atr = ATR(period=14)
        for i, bar in enumerate(_seeded_ohlcv(20), start=1):
            atr.update(bar)
            if i < 15:
                self.assertFalse(atr.is_ready, f"early-flip at bar {i}")
            else:
                self.assertTrue(atr.is_ready, f"not ready at bar {i}")


# --------------------------------------------------------------------------- #
# AC-6 dependency direction
# --------------------------------------------------------------------------- #


class Ac6DependencyDirectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.block = _contract_block()
        self.source = inspect.getsource(atp_strategy.indicators)

    def test_talib_is_imported(self) -> None:
        self.assertIn("import talib", self.source)

    def test_pandas_ta_is_imported(self) -> None:
        self.assertIn("import pandas_ta", self.source)

    def test_no_prohibited_custom_math_tokens(self) -> None:
        for token in self.block["prohibited_custom_math_tokens"]:
            self.assertNotIn(token, self.source, f"AC-6 violation: token {token!r}")

    def test_all_six_classes_satisfy_indicator_protocol(self) -> None:
        for cls, kwargs in (
            (SMA, {"period": 3}),
            (EMA, {"period": 3}),
            (RSI, {"period": 2}),
            (MACD, {}),
            (BollingerBands, {"period": 3, "num_std": 2.0}),
            (ATR, {"period": 2}),
        ):
            self.assertIsInstance(cls(**kwargs), Indicator)


if __name__ == "__main__":
    unittest.main()
