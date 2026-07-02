"""L1 unit tests for the research plotting surface (``SRS-RES-002`` / SyRS ``SYS-34b``).

Locks that a notebook can turn bars into a plot-ready OHLCV frame and RENDER a plot
headlessly (matplotlib object-oriented Figure API — no GUI backend / display), with
optional indicator overlays that align 1:1 with the bars and warm-up gaps drawn as
NaN rather than a misleading zero.
"""

from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_research as ar  # noqa: E402
from atp_strategy import Bar  # noqa: E402
from matplotlib.axes import Axes  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

pytestmark = pytest.mark.unit


def _bars(closes: list[float]) -> list[Bar]:
    return [
        Bar("X", f"2026-01-{i + 1:02d}T00:00:00+00:00", c, c + 1.0, c - 1.0, c, 100 + i)
        for i, c in enumerate(closes)
    ]


def test_bars_to_frame_has_ohlcv_columns_and_utc_timestamp_index() -> None:
    frame = ar.bars_to_frame(_bars([1.0, 2.0, 3.0]))
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 3
    assert frame.index.name == "timestamp"
    # timezone-aware UTC index (the store keys on UTC event timestamps).
    assert str(frame.index.tz) == "UTC"
    assert list(frame["close"]) == [1.0, 2.0, 3.0]
    assert list(frame["volume"]) == [100, 101, 102]


def test_bars_to_frame_empty_is_empty_frame_with_columns() -> None:
    frame = ar.bars_to_frame([])
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert len(frame) == 0


def test_plot_ohlc_returns_inline_displayable_figure_and_renders_png() -> None:
    bars = _bars([1.0, 2.0, 3.0, 4.0, 5.0])
    fig = ar.plot_ohlc(bars, title="X daily")
    # Returns a Figure — Jupyter inline-displays it (a Figure carries the _repr_html_
    # hook + IPython registers a PNG formatter for the Figure type). An Axes carries
    # neither, so returning the Figure is what makes `ar.plot_ohlc(bars)` show a chart
    # rather than an Axes repr.
    assert isinstance(fig, Figure)
    assert hasattr(fig, "_repr_html_")
    assert not hasattr(Axes, "_repr_html_") and not hasattr(Axes, "_repr_png_")
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    assert buffer.getvalue().startswith(b"\x89PNG")
    ax = fig.axes[0]
    assert ax.get_title() == "X daily"
    labels = [text.get_text() for text in ax.get_legend().get_texts()]
    assert labels == ["close"]


def test_plot_ohlc_overlays_indicator_series() -> None:
    bars = _bars([1.0, 2.0, 3.0, 4.0])
    sma = ar.compute_series(ar.SMA(period=2), bars)
    fig = ar.plot_ohlc(bars, indicators={"SMA2": sma})
    ax = fig.axes[0]
    labels = [text.get_text() for text in ax.get_legend().get_texts()]
    assert labels == ["close", "SMA2"]
    # Two lines drawn: close + the overlay.
    assert len(ax.get_lines()) == 2


def test_plot_ohlc_draws_warmup_none_as_nan_gap() -> None:
    bars = _bars([1.0, 2.0, 3.0])
    sma = ar.compute_series(ar.SMA(period=2), bars)  # [None, 1.5, 2.5]
    fig = ar.plot_ohlc(bars, indicators={"SMA2": sma})
    overlay = fig.axes[0].get_lines()[1]
    ydata = list(overlay.get_ydata())
    assert math.isnan(ydata[0])  # warm-up bar drawn as a gap, not 0.0
    assert ydata[1:] == [1.5, 2.5]


def test_plot_ohlc_rejects_misaligned_overlay() -> None:
    bars = _bars([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="align 1:1"):
        ar.plot_ohlc(bars, indicators={"short": [1.0, 2.0]})


def test_plot_ohlc_rejects_empty_bars() -> None:
    with pytest.raises(ValueError, match="at least one bar"):
        ar.plot_ohlc([])
