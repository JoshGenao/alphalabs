"""Built-in technical indicators for the Python Strategy API (``SRS-SDK-006``).

Each indicator is incremental: it consumes one :class:`atp_strategy.api.Bar`
per :py:meth:`update` call, maintains a rolling buffer of recent bar history
internally, and exposes the latest ``value`` plus an ``is_ready`` readiness
flag. This matches the SRS-SDK-006 acceptance criterion that indicators
"support incremental updates on each new bar".

Backend dispatch — SyRS AC-6 / StRS C-9
---------------------------------------
Every indicator in this module is a thin glue wrapper around either
`TA-Lib <https://ta-lib.org/>`_ or
`pandas-ta <https://github.com/twopirllc/pandas-ta>`_, whichever is the
canonical numerical reference for that particular indicator. SyRS AC-6
prohibits custom reimplementations of indicators available in these
libraries — the wrappers below contain buffering, dtype conversion, and
``NaN -> None`` glue ONLY. No arithmetic.

Backend routing (the cross-language source of truth lives at
``architecture/runtime_services.json#strategy_api_indicators_contract``
under ``primary_backend_per_indicator``):

* ``SMA`` -> ``pandas_ta.sma``
* ``BollingerBands`` -> ``pandas_ta.bbands`` (matype = SMA, population stdev)
* ``EMA`` -> ``talib.EMA`` (alpha = 2 / (period + 1); seeded by SMA at
  index period - 1)
* ``RSI`` -> ``talib.RSI`` (canonical Wilder smoothing on gains/losses)
* ``MACD`` -> ``talib.MACD`` (12/26/9 EMA convention; signal-line EMA
  applied to the MACD line)
* ``ATR`` -> ``talib.ATR`` (canonical Wilder smoothing on true range)

Both libraries are exercised in the runtime path so the indicator library
genuinely wraps both, per SyRS AC-6. The L7 parity test
(``tests/domain/test_indicators_parity.py``) and the contract evidence
script (``tools/strategy_api_indicators_check.py``) cross-check every
indicator against the OTHER library too — so every indicator pins parity
against both backends, not just its primary. Known seed-bar divergences
between the two libraries on RSI / MACD / ATR (different Wilder/EMA
seeding conventions) are documented in the architecture contract under
``pandas_ta_seed_skip_bars_factor`` and ``parity_tolerance_*``.

The TA-Lib C library is a mandatory native dependency of this module
(see ``docker/strategy-python.Dockerfile``, ``docker/jupyter.Dockerfile``,
and ``.github/workflows/ci.yml`` for the install recipe; ``brew install
ta-lib`` for local macOS development). Importing this module before the
C library is on the loader path raises :class:`ImportError`.

All public classes implement the :class:`atp_strategy.api.Indicator`
Protocol. ``MACD`` additionally exposes ``MACDValue`` via ``.reading``;
``BollingerBands`` exposes ``BollingerValue`` via ``.reading``.

Cost model
----------
Each ``update()`` appends one bar to an internal :class:`collections.deque`
and re-invokes the TA-Lib function on the buffer (``O(N)`` where ``N`` is
the buffer size, dominated by the TA-Lib C loop). Buffers are capped per
indicator at a multiple of the period that is large enough for the
TA-Lib output at index ``-1`` to be identical to the unbounded
equivalent (verified by the incremental-vs-batch parity test). The
SRS-SDK-006 AC requires "incremental updates" not "O(1) per update";
``O(period)`` is acceptable.
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

# SRS-SDK-006 / SyRS C-9: pandas-ta pulls in numba transitively. Numba's
# JIT path can fail at import time on edge-case interpreter / LLVM
# combinations and is irrelevant to the SDK's actual code paths (we only
# call pandas_ta.sma and pandas_ta.bbands, both of which are plain
# pandas/numpy under the hood with no numba kernels). Disabling the JIT
# before the first pandas_ta import makes the import deterministic across
# every supported interpreter without any runtime-managed env var. Must
# be set BEFORE pandas_ta / numba is imported anywhere in the process.
os.environ.setdefault("NUMBA_DISABLE_JIT", "1")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pandas_ta  # noqa: E402
import talib  # noqa: E402

if TYPE_CHECKING:
    from .api import Bar


_FLOAT64 = np.float64


class _IndicatorBase:
    """Shared bookkeeping for the built-in indicators."""

    _value: float | None
    _ready: bool

    def __init__(self) -> None:
        self._value = None
        self._ready = False

    @property
    def value(self) -> float | None:
        """Latest indicator value, or ``None`` if the warm-up window is incomplete."""
        return self._value

    @property
    def is_ready(self) -> bool:
        """True once enough bars have been consumed to emit a value."""
        return self._ready


def _nan_to_none(x: float) -> float | None:
    """Convert a TA-Lib trailing-output float to Python ``None`` if NaN."""
    if math.isnan(x):
        return None
    return x


class SMA(_IndicatorBase):
    """Simple moving average wrapping :func:`pandas_ta.sma` (``SRS-SDK-006``).

    Example:
        >>> from atp_strategy import Bar
        >>> sma = SMA(period=3)
        >>> for c in (1.0, 2.0, 3.0):
        ...     _ = sma.update(Bar("X", "t", c, c, c, c, 1))
        >>> round(sma.value, 4)
        2.0
        >>> sma.is_ready
        True
    """

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._closes: deque[float] = deque(maxlen=period)

    def update(self, bar: Bar) -> float | None:
        # SMA is routed through pandas-ta so the indicator library genuinely
        # wraps BOTH pandas-ta and TA-Lib per SyRS AC-6, not just imports one
        # and runs the other. SMA is a trivial mean so pandas-ta and TA-Lib
        # are exact to float epsilon; the L7 test pins both parities.
        self._closes.append(bar.close)
        if len(self._closes) < self.period:
            return None
        ser = pd.Series(self._closes, dtype=_FLOAT64)
        out = pandas_ta.sma(ser, length=self.period)
        v = _nan_to_none(float(out.iloc[-1]))
        if v is None:
            return None
        self._value = v
        self._ready = True
        return v


class EMA(_IndicatorBase):
    """Exponential moving average wrapping :func:`talib.EMA` (``SRS-SDK-006``).

    Example:
        >>> from atp_strategy import Bar
        >>> ema = EMA(period=3)
        >>> for c in (1.0, 2.0, 3.0, 4.0):
        ...     _ = ema.update(Bar("X", "t", c, c, c, c, 1))
        >>> ema.is_ready
        True
    """

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        # 30 * period bars is enough for (1 - alpha)^N decay to fall below
        # 1e-9 vs the full-history batch result: alpha = 2 / (period + 1) so
        # (1 - alpha)^(30 * period) ~ exp(-60 * period / (period + 1)) << 1e-9.
        self._closes: deque[float] = deque(maxlen=period * 30)

    def update(self, bar: Bar) -> float | None:
        self._closes.append(bar.close)
        if len(self._closes) < self.period:
            return None
        arr = np.asarray(self._closes, dtype=_FLOAT64)
        out = talib.EMA(arr, timeperiod=self.period)
        v = _nan_to_none(float(out[-1]))
        if v is None:
            return None
        self._value = v
        self._ready = True
        return v


class RSI(_IndicatorBase):
    """Relative strength index wrapping :func:`talib.RSI` (``SRS-SDK-006``).

    TA-Lib uses Wilder smoothing on gains/losses, which is the de-facto
    canonical RSI convention. pandas-ta's default seeding differs at the
    first ``period`` bars (see contract block parity tolerances).

    Example:
        >>> from atp_strategy import Bar
        >>> rsi = RSI(period=2)
        >>> for c in (1.0, 2.0, 1.5, 2.5):
        ...     _ = rsi.update(Bar("X", "t", c, c, c, c, 1))
        >>> rsi.is_ready
        True
    """

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        # Wilder smoothing decays as (1 - 1/period)^N; 30 * period bars
        # drops the seed contribution well below 1e-6 vs full-history batch.
        self._closes: deque[float] = deque(maxlen=period * 30 + 1)

    def update(self, bar: Bar) -> float | None:
        self._closes.append(bar.close)
        if len(self._closes) < self.period + 1:
            return None
        arr = np.asarray(self._closes, dtype=_FLOAT64)
        out = talib.RSI(arr, timeperiod=self.period)
        v = _nan_to_none(float(out[-1]))
        if v is None:
            return None
        self._value = v
        self._ready = True
        return v


@dataclass(frozen=True, slots=True)
class MACDValue:
    """Three-component MACD reading: line, signal, histogram.

    ``macd`` is the fast-EMA minus slow-EMA value. ``signal`` is the
    EMA of the MACD line over ``signal`` periods. ``histogram`` is
    ``macd - signal`` and is what most crossover strategies trade.
    Returned by ``MACD.reading``; ``MACD.value`` returns just the
    ``macd`` field for compatibility with the ``Indicator`` Protocol.

    Example:
        >>> MACDValue(0.5, 0.3, 0.2).histogram
        0.2
    """

    macd: float
    signal: float
    histogram: float


class MACD:
    """MACD wrapping :func:`talib.MACD` with the standard 12/26/9 defaults.

    Example:
        >>> from atp_strategy import Bar
        >>> macd = MACD()
        >>> for c in range(60):
        ...     _ = macd.update(Bar("X", "t", c, c, c, float(c), 1))
        >>> macd.is_ready
        True
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast < 1 or slow < 1 or signal < 1:
            raise ValueError("MACD periods must be >= 1")
        if fast >= slow:
            raise ValueError("fast period must be smaller than slow period")
        self.fast = fast
        self.slow = slow
        self.signal = signal
        # 30 * slow bars covers the slow-EMA seed decay (alpha = 2/(slow+1));
        # plus 30 * signal bars for the signal-line EMA over the MACD series.
        self._closes: deque[float] = deque(maxlen=slow * 30 + signal * 30)
        self._reading: MACDValue | None = None
        self._ready = False

    @property
    def value(self) -> float | None:
        if self._reading is None:
            return None
        return self._reading.macd

    @property
    def reading(self) -> MACDValue | None:
        """Full ``(macd, signal, histogram)`` tuple, or ``None`` until ready."""
        return self._reading

    @property
    def is_ready(self) -> bool:
        return self._ready

    def update(self, bar: Bar) -> float | None:
        self._closes.append(bar.close)
        if len(self._closes) < self.slow + self.signal - 1:
            return None
        arr = np.asarray(self._closes, dtype=_FLOAT64)
        macd_out, signal_out, hist_out = talib.MACD(
            arr,
            fastperiod=self.fast,
            slowperiod=self.slow,
            signalperiod=self.signal,
        )
        macd_v = float(macd_out[-1])
        signal_v = float(signal_out[-1])
        hist_v = float(hist_out[-1])
        if math.isnan(macd_v) or math.isnan(signal_v) or math.isnan(hist_v):
            return None
        self._reading = MACDValue(macd=macd_v, signal=signal_v, histogram=hist_v)
        self._ready = True
        return macd_v


@dataclass(frozen=True, slots=True)
class BollingerValue:
    """Three-component Bollinger reading: middle, upper, lower bands.

    ``middle`` is the SMA of the close over the indicator's period.
    ``upper`` and ``lower`` are ``middle ± num_std * stdev`` (population
    standard deviation, matching ``talib.BBANDS(matype=0)``). Returned
    by ``BollingerBands.reading``; ``BollingerBands.value`` returns
    ``middle`` alone for the ``Indicator`` Protocol.

    Example:
        >>> BollingerValue(10.0, 12.0, 8.0).upper
        12.0
    """

    middle: float
    upper: float
    lower: float


class BollingerBands:
    """Bollinger bands wrapping :func:`pandas_ta.bbands` (``SRS-SDK-006``).

    pandas-ta's bbands uses an SMA middle band and population standard
    deviation, matching ``talib.BBANDS(matype=0)`` exactly.

    Example:
        >>> from atp_strategy import Bar
        >>> bb = BollingerBands(period=3, num_std=2.0)
        >>> for c in (1.0, 2.0, 3.0):
        ...     _ = bb.update(Bar("X", "t", c, c, c, c, 1))
        >>> bb.is_ready
        True
    """

    def __init__(self, period: int = 20, num_std: float = 2.0) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        self.period = period
        self.num_std = num_std
        self._closes: deque[float] = deque(maxlen=period)
        self._reading: BollingerValue | None = None
        self._ready = False

    @property
    def value(self) -> float | None:
        if self._reading is None:
            return None
        return self._reading.middle

    @property
    def reading(self) -> BollingerValue | None:
        """Full ``(middle, upper, lower)`` tuple, or ``None`` until ready."""
        return self._reading

    @property
    def is_ready(self) -> bool:
        return self._ready

    def update(self, bar: Bar) -> float | None:
        # BollingerBands is routed through pandas-ta so the indicator library
        # genuinely wraps both backends per SyRS AC-6. pandas-ta's bbands uses
        # the same population stdev + SMA middle that talib.BBANDS uses with
        # matype=0; parity vs talib stays within 1e-9 (verified by L7 test).
        self._closes.append(bar.close)
        if len(self._closes) < self.period:
            return None
        ser = pd.Series(self._closes, dtype=_FLOAT64)
        out = pandas_ta.bbands(ser, length=self.period, std=self.num_std)
        if out is None:
            return None
        cols = list(out.columns)
        lower_col = next(c for c in cols if c.startswith("BBL_"))
        middle_col = next(c for c in cols if c.startswith("BBM_"))
        upper_col = next(c for c in cols if c.startswith("BBU_"))
        upper_v = float(out[upper_col].iloc[-1])
        middle_v = float(out[middle_col].iloc[-1])
        lower_v = float(out[lower_col].iloc[-1])
        if math.isnan(upper_v) or math.isnan(middle_v) or math.isnan(lower_v):
            return None
        self._reading = BollingerValue(
            middle=middle_v,
            upper=upper_v,
            lower=lower_v,
        )
        self._ready = True
        return middle_v


class ATR(_IndicatorBase):
    """Average true range wrapping :func:`talib.ATR` (``SRS-SDK-006``).

    TA-Lib ATR uses Wilder smoothing seeded with the SMA of the first
    ``period`` true-range values, the canonical convention.

    Example:
        >>> from atp_strategy import Bar
        >>> atr = ATR(period=2)
        >>> for h, l, c in ((2.0, 1.0, 1.5), (3.0, 1.5, 2.5), (3.5, 2.0, 3.0)):
        ...     _ = atr.update(Bar("X", "t", l, h, l, c, 1))
        >>> atr.is_ready
        True
    """

    def __init__(self, period: int) -> None:
        super().__init__()
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        # Wilder smoothing seed decay; 30 * period bars covers it under 1e-6.
        self._highs: deque[float] = deque(maxlen=period * 30 + 1)
        self._lows: deque[float] = deque(maxlen=period * 30 + 1)
        self._closes: deque[float] = deque(maxlen=period * 30 + 1)

    def update(self, bar: Bar) -> float | None:
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        self._closes.append(bar.close)
        if len(self._closes) < self.period + 1:
            return None
        high = np.asarray(self._highs, dtype=_FLOAT64)
        low = np.asarray(self._lows, dtype=_FLOAT64)
        close = np.asarray(self._closes, dtype=_FLOAT64)
        out = talib.ATR(high, low, close, timeperiod=self.period)
        v = _nan_to_none(float(out[-1]))
        if v is None:
            return None
        self._value = v
        self._ready = True
        return v


__all__ = [
    "ATR",
    "BollingerBands",
    "BollingerValue",
    "EMA",
    "MACD",
    "MACDValue",
    "RSI",
    "SMA",
]
