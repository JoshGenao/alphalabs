"""Jupyter research environment surface for ATP notebooks (``SRS-RES-002``).

SyRS ``SYS-34b`` / SRS-RES-002: the research environment shall give notebooks
access to the system's **historical data** (via the ``SRS-DATA-007`` unified
interface), the **indicator library** (``SRS-SDK-006``), and **plotting**. This
package is the single, curated import a notebook uses for exactly those three
capabilities:

    >>> import atp_research as ar
    >>> data = ar.open_historical_data(store_dir="/mnt/ssd/store")   # doctest: +SKIP
    >>> bars = data.get_bars_range("AAPL", frequency="1d", start=..., end=...)  # doctest: +SKIP
    >>> sma = ar.compute_series(ar.SMA(period=20), bars)             # doctest: +SKIP
    >>> ax = ar.plot_ohlc(bars, indicators={"SMA20": sma})          # doctest: +SKIP

No-live-order isolation (``SRS-RES-002`` acceptance / SyRS ``SYS-34c`` / ``NFR-S6``)
-----------------------------------------------------------------------------------
The public surface below is deliberately read-only research: data queries,
indicator computation, and plotting. It exposes **no** order-submission,
cancellation, position-mutation, or brokerage-credential entry point — a notebook
importing ``atp_research`` can analyse market history but cannot place a live order.
Order submission in ATP requires an orchestrator-wired concrete ``StrategyContext``
driver bound to a live execution path; that driver is never present in a notebook
process, and this package does not expose it. (The complementary *container-level*
sandbox — read-only data mounts, no brokerage credentials, no execution network — is
the separate security control ``SRS-SEC-004``.)

Status
------
``SRS-RES-002`` stays ``passes:false`` (serialized): the data / indicator / plotting
capabilities and the no-live-order isolation are demonstrated here by test, but the
end-to-end "in a live JupyterLab notebook reachable from the dashboard" demonstration
needs the operator-supplied JupyterLab image (see ``docker/jupyter.Dockerfile``) —
which must also bundle/mount the cargo-built ``data007_query_cli`` and point notebooks
at it via ``ATP_DATA_QUERY_BINARY`` (Docker Compose provisioning is ``SRS-ARCH-004``;
see :mod:`atp_research.data`) — and the dashboard embedding (``SRS-RES-001``), none of
which can run in this environment.
"""

from __future__ import annotations

from atp_strategy.api import Bar

from .data import open_historical_data
from .indicators import (
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    BollingerBands,
    BollingerValue,
    Indicator,
    MACDValue,
    compute_series,
)
from .plotting import bars_to_frame, plot_ohlc

__all__ = [
    # data access (read-only, SRS-DATA-007 binding)
    "open_historical_data",
    "Bar",
    # indicators (SRS-SDK-006 wrappers + batch helper)
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
    # plotting (SyRS SYS-34b)
    "bars_to_frame",
    "plot_ohlc",
]
