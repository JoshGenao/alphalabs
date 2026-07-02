"""Technical-indicator access for the Jupyter research environment (``SRS-RES-002``).

SyRS ``SYS-34b`` requires the research environment to reach the indicator library
(``SYS-35`` / ``SRS-SDK-006``). The research surface re-exports the SAME built-in
indicators the strategy SDK ships (thin wrappers around TA-Lib / pandas-ta, no
custom arithmetic per SyRS ``AC-6``) so a notebook computes ``pandas-ta`` /
``TA-Lib`` indicators through one canonical implementation — never a divergent copy.

The SDK indicators are *incremental* (one :class:`~atp_strategy.api.Bar` per
``update``); notebooks usually want the whole series over a history at once, so
:func:`compute_series` drives an indicator across a bar sequence and returns the
per-bar value series. It is pure glue (feed bars in ascending order, collect the
readings) — all numerics stay inside the SDK's TA-Lib / pandas-ta wrappers.
"""

from __future__ import annotations

from collections.abc import Sequence

from atp_strategy.api import Bar, Indicator
from atp_strategy.indicators import (
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    BollingerValue,
    MACDValue,
)

__all__ = [
    "ATR",
    "BollingerBands",
    "BollingerValue",
    "EMA",
    "MACD",
    "MACDValue",
    "RSI",
    "SMA",
    "Indicator",
    "compute_series",
]


def compute_series(indicator: Indicator, bars: Sequence[Bar]) -> list[float | None]:
    """Drive an incremental SDK ``indicator`` across ``bars`` and return the value series.

    Feeds every bar into the indicator in the given (ascending-time) order and
    collects the reading after each bar, so ``result[i]`` is the indicator value
    once ``bars[0..i]`` have been consumed — ``None`` while the warm-up window is
    still filling. The returned list has exactly ``len(bars)`` entries.

    The arithmetic lives entirely in the SDK indicator's TA-Lib / pandas-ta wrapper
    (SyRS ``AC-6`` prohibits reimplementing those indicators); this helper only
    sequences the incremental ``update`` calls a notebook would otherwise write by
    hand. Pass a *fresh* indicator instance — an indicator carries rolling state, so
    reusing one across calls would continue its existing buffer.

    Example:
        >>> from atp_research import SMA, compute_series
        >>> from atp_strategy import Bar
        >>> bars = [Bar("X", str(i), c, c, c, c, 1) for i, c in enumerate((1.0, 2.0, 3.0))]
        >>> compute_series(SMA(period=2), bars)
        [None, 1.5, 2.5]
    """
    return [indicator.update(bar) for bar in bars]
