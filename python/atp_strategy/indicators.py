"""Built-in technical indicators for the Python Strategy API (``SRS-SDK-006``).

Each indicator is incremental: it consumes one ``Bar`` per ``update()`` call,
maintains rolling state internally, and exposes the latest ``value`` and an
``is_ready`` readiness flag. This matches the SRS-SDK-006 acceptance criterion
that indicators "support incremental updates on each new bar".

Backend dispatch
----------------
The implementations here are pure-Python and stand alone with no external
dependencies. SRS-SDK-006 also references ``pandas-ta`` and ``TA-Lib`` as
reference outputs; numerical parity with those libraries is delivered by a
sibling feature once the dependencies are introduced. Code that wants the
optional backend should import ``pandas_ta`` / ``talib`` directly inside the
strategy until the dispatcher lands.

All public classes implement the :class:`atp_strategy.api.Indicator` Protocol.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import Bar


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


class SMA(_IndicatorBase):
    """Simple moving average over the last ``period`` closes (``SRS-SDK-006``).

    Example:
        >>> from atp_strategy.api import Bar
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
        self._window: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, bar: Bar) -> float | None:
        if len(self._window) == self.period:
            self._sum -= self._window[0]
        self._window.append(bar.close)
        self._sum += bar.close
        if len(self._window) == self.period:
            self._value = self._sum / self.period
            self._ready = True
        return self._value


class EMA(_IndicatorBase):
    """Exponential moving average with smoothing factor ``2 / (period + 1)``.

    Example:
        >>> from atp_strategy.api import Bar
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
        self._alpha = 2.0 / (period + 1.0)
        self._count = 0
        self._sum = 0.0

    def update(self, bar: Bar) -> float | None:
        self._count += 1
        if self._count < self.period:
            self._sum += bar.close
            return None
        if self._count == self.period:
            self._sum += bar.close
            self._value = self._sum / self.period
            self._ready = True
            return self._value
        assert self._value is not None
        self._value = (bar.close - self._value) * self._alpha + self._value
        return self._value


class RSI(_IndicatorBase):
    """Relative strength index over ``period`` closes using Wilder smoothing.

    Example:
        >>> from atp_strategy.api import Bar
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
        self._prev_close: float | None = None
        self._gains: list[float] = []
        self._losses: list[float] = []
        self._avg_gain = 0.0
        self._avg_loss = 0.0

    def update(self, bar: Bar) -> float | None:
        close = bar.close
        if self._prev_close is None:
            self._prev_close = close
            return None
        change = close - self._prev_close
        self._prev_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        if not self._ready:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self.period:
                return None
            self._avg_gain = sum(self._gains) / self.period
            self._avg_loss = sum(self._losses) / self.period
            self._ready = True
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0.0:
            self._value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self._value = 100.0 - (100.0 / (1.0 + rs))
        return self._value


@dataclass(frozen=True, slots=True)
class MACDValue:
    """Three-component MACD reading: line, signal, histogram.

    Example:
        >>> MACDValue(0.5, 0.3, 0.2).histogram
        0.2
    """

    macd: float
    signal: float
    histogram: float


class MACD:
    """MACD = EMA(fast) - EMA(slow), with signal-line EMA and histogram.

    Default parameters follow the standard 12/26/9 convention.

    Example:
        >>> from atp_strategy.api import Bar
        >>> macd = MACD()
        >>> for c in range(40):
        ...     _ = macd.update(Bar("X", "t", c, c, c, float(c), 1))
        >>> macd.is_ready
        True
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast < 1 or slow < 1 or signal < 1:
            raise ValueError("MACD periods must be >= 1")
        if fast >= slow:
            raise ValueError("fast period must be smaller than slow period")
        self._fast = EMA(fast)
        self._slow = EMA(slow)
        self._signal_ema = EMA(signal)
        self._value: MACDValue | None = None
        self._ready = False

    @property
    def value(self) -> float | None:
        if self._value is None:
            return None
        return self._value.macd

    @property
    def reading(self) -> MACDValue | None:
        """Full ``(macd, signal, histogram)`` tuple, or ``None`` until ready."""
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._ready

    def update(self, bar: Bar) -> float | None:
        self._fast.update(bar)
        self._slow.update(bar)
        if not (self._fast.is_ready and self._slow.is_ready):
            return None
        macd = self._fast.value - self._slow.value  # type: ignore[operator]
        synth = type(bar)(
            bar.symbol, bar.timestamp, bar.open, bar.high, bar.low, macd, bar.volume
        )
        self._signal_ema.update(synth)
        if not self._signal_ema.is_ready:
            return None
        signal = self._signal_ema.value
        assert signal is not None
        self._value = MACDValue(macd=macd, signal=signal, histogram=macd - signal)
        self._ready = True
        return macd


@dataclass(frozen=True, slots=True)
class BollingerValue:
    """Three-component Bollinger reading: middle, upper, lower bands.

    Example:
        >>> BollingerValue(10.0, 12.0, 8.0).upper
        12.0
    """

    middle: float
    upper: float
    lower: float


class BollingerBands:
    """Middle SMA plus ``num_std`` upper/lower bands on rolling close stdev.

    Example:
        >>> from atp_strategy.api import Bar
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
        self._window: deque[float] = deque(maxlen=period)
        self._value: BollingerValue | None = None
        self._ready = False

    @property
    def value(self) -> float | None:
        if self._value is None:
            return None
        return self._value.middle

    @property
    def reading(self) -> BollingerValue | None:
        """Full ``(middle, upper, lower)`` tuple, or ``None`` until ready."""
        return self._value

    @property
    def is_ready(self) -> bool:
        return self._ready

    def update(self, bar: Bar) -> float | None:
        self._window.append(bar.close)
        if len(self._window) < self.period:
            return None
        mean = sum(self._window) / self.period
        var = sum((x - mean) ** 2 for x in self._window) / self.period
        std = var**0.5
        self._value = BollingerValue(
            middle=mean,
            upper=mean + self.num_std * std,
            lower=mean - self.num_std * std,
        )
        self._ready = True
        return mean


class ATR(_IndicatorBase):
    """Average true range over ``period`` bars using Wilder smoothing.

    Example:
        >>> from atp_strategy.api import Bar
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
        self._prev_close: float | None = None
        self._trs: list[float] = []

    def update(self, bar: Bar) -> float | None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close

        if not self._ready:
            self._trs.append(tr)
            if len(self._trs) < self.period:
                return None
            self._value = sum(self._trs) / self.period
            self._ready = True
        else:
            assert self._value is not None
            self._value = (self._value * (self.period - 1) + tr) / self.period
        return self._value


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
