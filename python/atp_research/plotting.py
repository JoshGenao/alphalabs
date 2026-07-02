"""Plot rendering for the Jupyter research environment (``SRS-RES-002``).

SyRS ``SYS-34b`` requires the research environment to have plotting capabilities
alongside the unified data interface and the indicator library. This module renders
price history (and optional indicator overlays) through matplotlib.

Headless by construction, inline-displayable in Jupyter
-------------------------------------------------------
Rendering goes through matplotlib's **object-oriented** :class:`~matplotlib.figure.Figure`
API (``Figure().add_subplot()``), never ``pyplot`` — so it needs no interactive/GUI
backend and no display, and it never mutates global pyplot state. :func:`plot_ohlc`
returns the :class:`~matplotlib.figure.Figure`: as a notebook cell's last expression
that renders inline (IPython registers a PNG formatter for the ``Figure`` type), and
in a headless CI test the same object writes to an in-memory buffer via ``savefig``.
:func:`bars_to_frame` also exposes the OHLCV history as a :class:`pandas.DataFrame` so
a notebook can drive ``DataFrame.plot`` / ``pandas-ta`` directly.

No-live-order isolation: this module only reads :class:`~atp_strategy.api.Bar`
values a notebook already holds and draws them; it has no order-submission surface.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

import pandas as pd  # type: ignore[import-untyped]
from atp_strategy.api import Bar
from matplotlib.figure import Figure

if TYPE_CHECKING:
    from matplotlib.axes import Axes

__all__ = ["bars_to_frame", "plot_ohlc"]

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def bars_to_frame(bars: Sequence[Bar]) -> pd.DataFrame:
    """Convert ``bars`` into an OHLCV :class:`pandas.DataFrame` indexed by UTC timestamp.

    Columns are ``open, high, low, close, volume`` in that order; the index is the
    parsed (timezone-aware, UTC) bar timestamp and is named ``timestamp``. An empty
    ``bars`` yields an empty frame with the OHLCV columns. This is the plot-ready /
    indicator-ready view notebooks use to drive ``DataFrame.plot`` or ``pandas-ta``
    directly, and the input to :func:`plot_ohlc`.
    """
    frame = pd.DataFrame(
        {
            "open": [bar.open for bar in bars],
            "high": [bar.high for bar in bars],
            "low": [bar.low for bar in bars],
            "close": [bar.close for bar in bars],
            "volume": [bar.volume for bar in bars],
        },
        index=pd.to_datetime([bar.timestamp for bar in bars], utc=True),
        columns=list(_OHLCV_COLUMNS),
    )
    frame.index.name = "timestamp"
    return frame


def plot_ohlc(
    bars: Sequence[Bar],
    *,
    indicators: Mapping[str, Sequence[float | None]] | None = None,
    ax: Axes | None = None,
    title: str | None = None,
) -> Figure:
    """Render the close-price line for ``bars`` with optional indicator overlays.

    Draws ``close`` against the bar timestamps and, for each entry in
    ``indicators``, overlays that value series (a ``None`` reading — e.g. an
    indicator still warming up — is drawn as a gap, not zero). Each overlay series
    must align 1:1 with ``bars`` (same length, same order — the shape
    :func:`atp_research.indicators.compute_series` returns).

    Returns the :class:`~matplotlib.figure.Figure`, so ``ar.plot_ohlc(bars)`` as a
    notebook cell's last expression renders inline (IPython has a PNG formatter for
    ``Figure``), and a headless test can ``fig.savefig(...)``. When ``ax`` is omitted
    a fresh single-axes ``Figure`` is created (object-oriented API — no GUI backend,
    no global pyplot state); pass an existing ``ax`` to draw into a notebook's current
    axes, and the owning ``Figure`` (``ax.figure``) is returned. Reach the drawn axes
    with ``fig.axes[0]`` for further composition.

    Raises:
        ValueError: if ``bars`` is empty, or an overlay series length differs from
            ``len(bars)``.
    """
    bar_list = list(bars)
    if not bar_list:
        raise ValueError("plot_ohlc requires at least one bar to render")
    frame = bars_to_frame(bar_list)

    target: Axes = Figure().add_subplot() if ax is None else ax
    target.plot(frame.index, frame["close"], label="close", color="black", linewidth=1.0)

    if indicators:
        for name, series in indicators.items():
            values = list(series)
            if len(values) != len(bar_list):
                raise ValueError(
                    f"indicator overlay {name!r} has {len(values)} points but there "
                    f"are {len(bar_list)} bars; each overlay must align 1:1 with bars"
                )
            # A None reading (warm-up) becomes NaN so matplotlib leaves a gap
            # instead of drawing a misleading zero.
            plotted = [math.nan if value is None else float(value) for value in values]
            target.plot(frame.index, plotted, label=name, linewidth=1.0)

    target.set_ylabel("price")
    target.set_xlabel("timestamp")
    if title is not None:
        target.set_title(title)
    target.legend(loc="best")
    # A fresh Figure().add_subplot() (and a notebook's plain axes) lives on a top-level
    # Figure; resolve to it (climbing a SubFigure parent) so the inline-displayable
    # Figure is returned.
    owning = target.figure
    return owning if isinstance(owning, Figure) else owning.figure
