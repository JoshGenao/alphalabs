"""Public Python Strategy API for user-authored ATP strategies.

This package is intentionally limited to user-facing interfaces. Core ATP
runtime services live in Rust crates under ``crates/``. The interfaces here
are identical for live IB execution and internal paper simulation
(``SRS-SDK-001``); the orchestrator selects the execution path at container
start, transparent to user code.

SRS trace
---------
``SRS-SDK-001``..``SRS-SDK-009`` (see ``docs/SRS.md`` §5.2). API-1 in
``feature_list.json`` is the contract-evidence test for this surface.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Iterable, Protocol, runtime_checkable

from .indicators import (
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    BollingerBands,
)


# --------------------------------------------------------------------------- #
# Asset class and configuration  [SRS-SDK-001, SRS-SDK-003, SRS-SDK-005]
# --------------------------------------------------------------------------- #


class AssetClass(StrEnum):
    """Tradable asset class for a strategy.

    A strategy may subscribe to either ``EQUITY`` or ``OPTION`` data for
    analysis, but only one class is tradable per strategy instance. Order
    submission for the unconfigured class raises ``AssetClassViolation``.

    Example:
        >>> AssetClass.EQUITY
        <AssetClass.EQUITY: 'EQUITY'>
    """

    EQUITY = "EQUITY"
    OPTION = "OPTION"


class NormalizationMode(StrEnum):
    """Historical-series normalization mode (``SRS-DATA-012``).

    Selects how splits, dividends, and corporate actions are folded into
    historical and live subscription data. Options strategies can request
    ``RAW`` prices; indicators typically request ``SPLIT_ADJUSTED`` or
    ``FULLY_ADJUSTED`` series; benchmarking workloads request
    ``TOTAL_RETURN``.

    Example:
        >>> NormalizationMode.SPLIT_ADJUSTED
        <NormalizationMode.SPLIT_ADJUSTED: 'SPLIT_ADJUSTED'>
    """

    RAW = "RAW"
    SPLIT_ADJUSTED = "SPLIT_ADJUSTED"
    FULLY_ADJUSTED = "FULLY_ADJUSTED"
    TOTAL_RETURN = "TOTAL_RETURN"


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Container-time configuration handed to a strategy by the orchestrator.

    Identical for live IB execution and internal paper simulation
    (``SRS-SDK-001``). The execution mode is selected by the orchestrator
    and is not visible to user code.

    Attributes:
        strategy_id: Stable identifier for this strategy instance.
        tradable_asset_class: The single asset class this strategy may
            submit orders against (``SRS-SDK-003``).
        warmup_bars: Number of historical bars to replay before the first
            executable bar (``SRS-SDK-005``).
        timezone: IANA zone for scheduling (``SRS-SDK-002`` defaults to
            ``America/New_York``).

    Example:
        >>> StrategyConfig(strategy_id="s1", tradable_asset_class=AssetClass.EQUITY,
        ...                warmup_bars=200).warmup_bars
        200
    """

    strategy_id: str
    tradable_asset_class: AssetClass
    warmup_bars: int = 0
    timezone: str = "America/New_York"


# --------------------------------------------------------------------------- #
# Bars and orders
# --------------------------------------------------------------------------- #


class OrderSide(StrEnum):
    """Direction of an order.

    Example:
        >>> OrderSide.BUY.value
        'BUY'
    """

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Order type supported by the Strategy API.

    Example:
        >>> OrderType.LIMIT.value
        'LIMIT'
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar delivered to ``Strategy.on_bar``.

    Example:
        >>> Bar("AAPL", "2026-05-03T09:30:00-04:00", 1.0, 2.0, 0.5, 1.5, 100).close
        1.5
    """

    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """Order parameters submitted via ``StrategyContext.order``.

    Example:
        >>> OrderRequest("AAPL", 10, OrderSide.BUY, OrderType.MARKET).quantity
        10
    """

    symbol: str
    quantity: int
    side: OrderSide
    order_type: OrderType
    asset_class: AssetClass = AssetClass.EQUITY
    limit_price: float | None = None
    stop_price: float | None = None
    client_order_id: str | None = None


@dataclass(frozen=True, slots=True)
class OrderHandle:
    """Opaque reference to a submitted order.

    Returned by ``StrategyContext.order`` and accepted by
    ``StrategyContext.cancel``.

    Example:
        >>> OrderHandle("ord-1", "s1").order_id
        'ord-1'
    """

    order_id: str
    strategy_id: str


# --------------------------------------------------------------------------- #
# Order events  [SRS-SDK-004]
# --------------------------------------------------------------------------- #


class OrderEventType(StrEnum):
    """Lifecycle event categories delivered to ``Strategy.on_order_event``.

    Covers acknowledgement, fills, partial fills, cancellation, rejection
    and expiry per ``SRS-SDK-004``.

    Example:
        >>> OrderEventType.PARTIAL_FILL.value
        'PARTIAL_FILL'
    """

    ACK = "ACK"
    FILL = "FILL"
    PARTIAL_FILL = "PARTIAL_FILL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Order lifecycle event payload (``SRS-SDK-004``).

    Includes fill price, fill quantity, commission and order identifiers
    required by the SRS-SDK-004 acceptance criteria.

    Example:
        >>> OrderEvent(OrderEventType.FILL, "ord-1", "cli-1", "s1", "AAPL",
        ...            100.0, 10, 10, 0, 0.05, None, "2026-05-03T13:30:00Z").fill_price
        100.0
    """

    event_type: OrderEventType
    order_id: str
    client_order_id: str
    strategy_id: str
    symbol: str
    fill_price: float | None
    fill_quantity: int | None
    cumulative_filled: int
    remaining_quantity: int
    commission: float | None
    reason: str | None
    timestamp: str


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class StrategyAPIError(Exception):
    """Base class for Strategy API runtime errors.

    Example:
        >>> raise StrategyAPIError("boom")
        Traceback (most recent call last):
        ...
        atp_strategy.api.StrategyAPIError: boom
    """


class AssetClassViolation(StrategyAPIError):
    """Raised when a strategy submits an order against a non-configured class.

    See ``SRS-SDK-003``.

    Example:
        >>> raise AssetClassViolation("strategy is EQUITY-only")
        Traceback (most recent call last):
        ...
        atp_strategy.api.AssetClassViolation: strategy is EQUITY-only
    """


class WarmupNotComplete(StrategyAPIError):
    """Raised when user code touches data or orders before warm-up finishes.

    See ``SRS-SDK-005``.

    Example:
        >>> raise WarmupNotComplete("indicator buffers not ready")
        Traceback (most recent call last):
        ...
        atp_strategy.api.WarmupNotComplete: indicator buffers not ready
    """


# --------------------------------------------------------------------------- #
# Scheduling and trading calendar  [SRS-SDK-002]
# --------------------------------------------------------------------------- #


ScheduleCallback = Callable[["StrategyContext"], None]
"""Callable invoked by the runtime when a scheduled trigger fires."""


@runtime_checkable
class ScheduleHandle(Protocol):
    """Cancellation handle returned by every ``Scheduler`` method.

    Example:
        >>> class _H:
        ...     def cancel(self) -> None: ...
        >>> isinstance(_H(), ScheduleHandle)
        True
    """

    def cancel(self) -> None:
        """Cancel this scheduled trigger; idempotent."""


@runtime_checkable
class Scheduler(Protocol):
    """Trading-calendar-aware scheduling primitives (``SRS-SDK-002``).

    Resolves NYSE / NASDAQ / CBOE holidays, early closes, pre-market and
    after-hours session boundaries, US Eastern time, and DST transitions
    via the bound ``TradingCalendar``.

    Example:
        >>> class _S:
        ...     def at_market_open(self, callback, *, offset_minutes=0): ...
        ...     def at_market_close(self, callback, *, offset_minutes=0): ...
        ...     def every_n_minutes(self, n, callback, *, only_during_session=True): ...
        ...     def cron(self, expression, callback): ...
        >>> isinstance(_S(), Scheduler)
        True
    """

    def at_market_open(
        self, callback: ScheduleCallback, *, offset_minutes: int = 0
    ) -> ScheduleHandle:
        """Fire ``callback`` at the regular session open, plus an optional offset."""

    def at_market_close(
        self, callback: ScheduleCallback, *, offset_minutes: int = 0
    ) -> ScheduleHandle:
        """Fire ``callback`` at the regular session close, plus an optional offset."""

    def every_n_minutes(
        self,
        n: int,
        callback: ScheduleCallback,
        *,
        only_during_session: bool = True,
    ) -> ScheduleHandle:
        """Fire ``callback`` every ``n`` minutes, optionally only during a session."""

    def cron(self, expression: str, callback: ScheduleCallback) -> ScheduleHandle:
        """Fire ``callback`` on a cron-like schedule expression."""


@runtime_checkable
class TradingCalendar(Protocol):
    """Read-only trading-calendar Protocol (``SRS-SDK-002``).

    Example:
        >>> cal = StaticTradingCalendar()
        >>> cal.name
        'NYSE'
    """

    name: str

    def is_session(self, date: _dt.date) -> bool:
        """Return True if ``date`` is a regular trading session."""

    def session_open(self, date: _dt.date) -> _dt.datetime:
        """Return the regular session open for ``date`` in US Eastern time."""

    def session_close(self, date: _dt.date) -> _dt.datetime:
        """Return the regular session close for ``date`` in US Eastern time."""

    def is_early_close(self, date: _dt.date) -> bool:
        """Return True if ``date`` has an early close."""


_EASTERN = _dt.timezone(_dt.timedelta(hours=-5), name="US/Eastern")


@dataclass(frozen=True, slots=True)
class StaticTradingCalendar:
    """Minimal NYSE-shaped trading calendar usable without external deps.

    Sessions: weekdays, 09:30–16:00 in a fixed UTC-5 offset. Holidays and
    DST handling are intentionally omitted; the full holiday/DST calendar
    is delivered by a sibling feature (see SRS-SDK-002 trace in
    ``docs/SRS.md``).

    Example:
        >>> import datetime as dt
        >>> cal = StaticTradingCalendar()
        >>> cal.is_session(dt.date(2026, 5, 4))   # Monday
        True
        >>> cal.is_session(dt.date(2026, 5, 3))   # Sunday
        False
    """

    name: str = "NYSE"

    def is_session(self, date: _dt.date) -> bool:
        return date.weekday() < 5

    def session_open(self, date: _dt.date) -> _dt.datetime:
        return _dt.datetime(date.year, date.month, date.day, 9, 30, tzinfo=_EASTERN)

    def session_close(self, date: _dt.date) -> _dt.datetime:
        return _dt.datetime(date.year, date.month, date.day, 16, 0, tzinfo=_EASTERN)

    def is_early_close(self, date: _dt.date) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Historical data, indicators, bar consolidation
# --------------------------------------------------------------------------- #


@runtime_checkable
class HistoricalData(Protocol):
    """Historical-bar query interface exposed to strategies.

    The runtime fans queries to the unified historical-data service
    (Databento / Sharadar / IB / user Parquet) without exposing vendor
    details to user code.

    Example:
        >>> class _H:
        ...     def get_bars(self, symbol, *, lookback, frequency="1m", end=None):
        ...         return []
        >>> isinstance(_H(), HistoricalData)
        True
    """

    def get_bars(
        self,
        symbol: str,
        *,
        lookback: int,
        frequency: str = "1m",
        end: _dt.datetime | None = None,
        asset_class: AssetClass = AssetClass.EQUITY,
        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,
    ) -> list[Bar]:
        """Return ``lookback`` bars at ``frequency`` ending at ``end`` (default: now).

        ``asset_class`` and ``normalization`` route through the unified
        historical-data interface (``API-7``, ``SRS-DATA-007`` +
        ``SRS-DATA-012``) so strategies, backtests, factor jobs, and
        notebooks can request raw, split-adjusted, fully adjusted, or
        total-return series for equities, options, futures, ETFs, or
        indices without binding to a specific data provider.
        """


@runtime_checkable
class Indicator(Protocol):
    """Incremental technical indicator (``SRS-SDK-006``).

    Implementations update on each new bar and expose the latest value
    plus a readiness flag. See ``atp_strategy.indicators`` for built-ins.

    Example:
        >>> from atp_strategy.indicators import SMA
        >>> ind = SMA(period=2)
        >>> ind.is_ready
        False
    """

    @property
    def value(self) -> float | None:
        """Latest indicator value, or ``None`` if not yet ready."""

    @property
    def is_ready(self) -> bool:
        """True once the indicator has consumed enough bars to emit a value."""

    def update(self, bar: Bar) -> float | None:
        """Consume a new bar; return the latest value or ``None``."""


@runtime_checkable
class BarConsolidator(Protocol):
    """Time-based bar consolidator (``SRS-SDK-007``).

    Consolidates minute-resolution input into 5-minute, 15-minute, hourly,
    and daily output bars without pre-processed datasets.

    Example:
        >>> class _C:
        ...     def consolidate(self, source_symbol, *, period):
        ...         return iter(())
        >>> isinstance(_C(), BarConsolidator)
        True
    """

    def consolidate(self, source_symbol: str, *, period: str) -> Iterable[Bar]:
        """Yield consolidated bars for ``source_symbol`` at ``period`` (e.g. ``"5m"``)."""


@runtime_checkable
class RenkoBuilder(Protocol):
    """Renko-bar generator (``SRS-SDK-008``, P3).

    Example:
        >>> class _R:
        ...     def __init__(self, brick_size): self.brick_size = brick_size
        ...     def update(self, bar): return None
        >>> isinstance(_R(0.5), RenkoBuilder)
        True
    """

    brick_size: float

    def update(self, bar: Bar) -> Bar | None:
        """Consume a tick/bar; emit a completed Renko bar or ``None``."""


@runtime_checkable
class RangeBarBuilder(Protocol):
    """Range-bar generator (``SRS-SDK-008``, P3).

    Example:
        >>> class _R:
        ...     def __init__(self, range_size): self.range_size = range_size
        ...     def update(self, bar): return None
        >>> isinstance(_R(0.5), RangeBarBuilder)
        True
    """

    range_size: float

    def update(self, bar: Bar) -> Bar | None:
        """Consume a tick/bar; emit a completed range bar or ``None``."""


# --------------------------------------------------------------------------- #
# Strategy context and base class
# --------------------------------------------------------------------------- #


@runtime_checkable
class StrategyContext(Protocol):
    """Runtime services exposed to a strategy (``SRS-SDK-001``).

    The same context surface is delivered in live IB execution and internal
    paper simulation; user code must contain no execution-mode branches.
    The orchestrator wires either a live IB-backed driver or a paper
    simulation driver behind this protocol.

    Example:
        Strategy authors interact with ``StrategyContext`` only via the
        ``context`` parameter on each callback:

        >>> def handle(ctx):
        ...     ctx.subscribe("AAPL")
        ...     ctx.log("ready")
    """

    config: StrategyConfig
    schedule: Scheduler
    calendar: TradingCalendar
    history: HistoricalData

    def subscribe(
        self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY
    ) -> None:
        """Subscribe to market data for ``symbol`` in ``asset_class``.

        A strategy may subscribe to both equities and options for analysis
        regardless of its tradable asset class (``SRS-SDK-003``).
        """

    def order(self, request: OrderRequest) -> OrderHandle:
        """Submit an order through the runtime-selected execution path.

        Raises ``AssetClassViolation`` if ``request.asset_class`` does not
        match ``config.tradable_asset_class``.
        """

    def cancel(self, handle: OrderHandle) -> None:
        """Cancel a previously submitted order."""

    def log(self, message: str) -> None:
        """Write a strategy log message."""

    def get_state(self, key: str, default: object | None = None) -> object | None:
        """Read a JSON-serializable value from strategy state."""

    def set_state(self, key: str, value: object) -> None:
        """Persist a JSON-serializable value to strategy state."""

    def indicator(self, name: str, **params: object) -> Indicator:
        """Construct a built-in technical indicator (``SRS-SDK-006``)."""

    def consolidate(self, symbol: str, period: str) -> BarConsolidator:
        """Open a time-based consolidator for ``symbol`` at ``period`` (``SRS-SDK-007``)."""


class Strategy:
    """Base class for Python-authored ATP strategies.

    Subclasses override the ``on_*`` callbacks. The runtime invokes
    callbacks in this order: ``on_start`` → warm-up replay →
    ``on_warmup_complete`` → ``on_bar`` (live) interleaved with
    ``on_schedule`` and ``on_order_event``.

    Class attribute ``warmup_bars`` (mirrors ``StrategyConfig.warmup_bars``)
    is the SRS-SDK-005 declaration when not configured externally.

    Example:
        >>> class MyStrategy(Strategy):
        ...     warmup_bars = 200
        ...     def on_bar(self, ctx, bar):
        ...         ctx.log(bar.symbol)
        >>> MyStrategy().warmup_bars
        200
    """

    warmup_bars: int = 0

    def on_start(self, context: StrategyContext) -> None:
        """Run once after the strategy container has initialized."""

    def on_warmup_complete(self, context: StrategyContext) -> None:
        """Run once after the warm-up replay completes (``SRS-SDK-005``)."""

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        """Run when a subscribed bar arrives."""

    def on_order_event(self, context: StrategyContext, event: OrderEvent) -> None:
        """Run when an order lifecycle event is delivered (``SRS-SDK-004``)."""

    def on_schedule(self, context: StrategyContext, tag: str) -> None:
        """Run when a scheduled trigger fires (``SRS-SDK-002``)."""


__all__ = [
    "ATR",
    "AssetClass",
    "AssetClassViolation",
    "Bar",
    "BarConsolidator",
    "BollingerBands",
    "EMA",
    "HistoricalData",
    "Indicator",
    "MACD",
    "OrderEvent",
    "OrderEventType",
    "OrderHandle",
    "OrderRequest",
    "OrderSide",
    "OrderType",
    "RSI",
    "RangeBarBuilder",
    "RenkoBuilder",
    "SMA",
    "ScheduleCallback",
    "ScheduleHandle",
    "Scheduler",
    "StaticTradingCalendar",
    "Strategy",
    "StrategyAPIError",
    "StrategyConfig",
    "StrategyContext",
    "TradingCalendar",
    "WarmupNotComplete",
]
