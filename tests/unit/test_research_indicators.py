"""L1 unit tests for the research indicator surface (``SRS-RES-002``).

Locks that the notebook research package re-exports the SAME built-in indicators the
strategy SDK ships (SyRS ``AC-6`` — one canonical TA-Lib / pandas-ta implementation,
never a divergent copy) and that :func:`atp_research.compute_series` drives them over
a bar history correctly (per-bar series, ``None`` during warm-up, glue only).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_research as ar  # noqa: E402
import atp_strategy as sdk  # noqa: E402
from atp_strategy import Bar  # noqa: E402

pytestmark = pytest.mark.unit


def _bars(closes: list[float]) -> list[Bar]:
    return [
        Bar("X", f"2026-01-{i + 1:02d}T00:00:00+00:00", c, c + 1.0, c - 1.0, c, 100 + i)
        for i, c in enumerate(closes)
    ]


def test_reexported_indicators_are_the_sdk_classes() -> None:
    # Same object identity — the research surface wraps the SDK indicator library,
    # it does not reimplement it (SyRS AC-6).
    for name in ("SMA", "EMA", "RSI", "MACD", "BollingerBands", "ATR"):
        assert getattr(ar, name) is getattr(sdk, name), name


def test_compute_series_matches_manual_incremental_drive() -> None:
    bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0])
    # Reference: drive a fresh indicator by hand.
    reference_ind = ar.SMA(period=2)
    expected = [reference_ind.update(bar) for bar in bars]
    got = ar.compute_series(ar.SMA(period=2), bars)
    assert got == expected


def test_compute_series_has_one_entry_per_bar_with_none_during_warmup() -> None:
    bars = _bars([1.0, 2.0, 3.0, 4.0])
    series = ar.compute_series(ar.SMA(period=3), bars)
    assert len(series) == len(bars)
    # SMA(3) is not ready until the third bar.
    assert series[0] is None and series[1] is None
    assert series[2] == pytest.approx(2.0)
    assert series[3] == pytest.approx(3.0)


def test_compute_series_over_no_bars_is_empty() -> None:
    assert ar.compute_series(ar.SMA(period=3), []) == []


def test_compute_series_respects_the_indicator_protocol() -> None:
    # A minimal Indicator Protocol implementation drives fine — compute_series is
    # pure glue over the incremental update contract, not tied to concrete classes.
    class _CountUp:
        def __init__(self) -> None:
            self._n = 0.0
            self._ready = False

        @property
        def value(self) -> float | None:
            return self._n if self._ready else None

        @property
        def is_ready(self) -> bool:
            return self._ready

        def update(self, bar: Bar) -> float | None:
            self._n += 1.0
            self._ready = True
            return self._n

    assert ar.compute_series(_CountUp(), _bars([1.0, 2.0, 3.0])) == [1.0, 2.0, 3.0]


def test_one_period_moving_averages_return_the_close() -> None:
    """``period=1`` must work — both constructors advertise ``period >= 1``.

    Neither backing library will compute it: ``pandas_ta.sma(length=1)`` raises
    ``ValueError: Length of values (0) does not match length of index (N)`` for every
    input length, and ``talib.EMA(timeperiod=1)`` rejects the call with
    ``TA_BAD_PARAM``. So ``SMA(period=1)`` / ``EMA(period=1)`` were constructible but
    raised on the FIRST update — which is how the SRS-RES-002 notebook path
    (``compute_series(SMA(period=1), bars)``) failed.

    A one-period average is the observation itself, so every reading equals that bar's
    close and the indicator is ready immediately (no warm-up to serve).
    """
    for cls in (sdk.SMA, sdk.EMA):
        indicator = cls(period=1)
        assert ar.compute_series(indicator, _bars([10.0, 20.0, 30.0])) == [10.0, 20.0, 30.0], cls
        assert indicator.is_ready is True, cls
        assert indicator.value == 30.0, cls


def test_multi_period_moving_averages_still_route_through_the_libraries() -> None:
    """The period-1 shortcut must not have swallowed the real arithmetic: period >= 2
    keeps the warm-up ``None``s and the library-computed values."""
    assert ar.compute_series(sdk.SMA(period=2), _bars([1.0, 2.0, 3.0])) == [None, 1.5, 2.5]
    ema = ar.compute_series(sdk.EMA(period=3), _bars([1.0, 2.0, 3.0, 4.0]))
    assert ema[0] is None and ema[1] is None
    assert ema[2] == pytest.approx(2.0) and ema[3] == pytest.approx(3.0)
