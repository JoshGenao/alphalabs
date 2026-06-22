"""Public Python Strategy API for user-authored ATP strategies.

The contract surface here is the SDK exposed inside Python strategy
containers. SRS trace: ``SRS-SDK-001``..``SRS-SDK-009`` (see
``docs/SRS.md`` §5.2 and ``feature_list.json`` entry ``API-1``).
"""

from .api import (
    ATR,
    EMA,
    LIVE_CALLBACK_LATENCY_P95_MS,
    MACD,
    PAPER_CALLBACK_LATENCY_P95_MS,
    RSI,
    SMA,
    AssetClass,
    AssetClassViolation,
    Bar,
    BarConsolidator,
    BollingerBands,
    BollingerValue,
    CalendarHorizonExceeded,
    HistoricalData,
    Indicator,
    MACDValue,
    NormalizationMode,
    NotATradingSession,
    OrderEvent,
    OrderEventContractError,
    OrderEventType,
    OrderHandle,
    OrderRequest,
    OrderSide,
    OrderType,
    RangeBarBuilder,
    RenkoBuilder,
    ScheduleCallback,
    ScheduleHandle,
    Scheduler,
    StaticTradingCalendar,
    Strategy,
    StrategyAPIError,
    StrategyConfig,
    StrategyContext,
    TradingCalendar,
    WarmupNotComplete,
    assert_asset_class,
    assert_order_event_payload,
)
from .calendar import UsEquityTradingCalendar
from .scheduler import InMemoryScheduler
from .warmup import WarmupController, WarmupState, assert_warmup_complete

# NOTE: ``StoreBackedHistoricalData`` (the host/runtime binding that PROVIDES a HistoricalData over the
# durable store, SRS-DATA-007) is intentionally NOT re-exported here. It is not a strategy-AUTHORING
# primitive — a strategy consumes the ``HistoricalData`` Protocol via ``ctx.history`` and never
# constructs the concrete binding; the host/notebook imports it explicitly from
# ``atp_strategy.store_history`` so the documented author surface (__all__) stays focused.

__all__ = [
    "ATR",
    "AssetClass",
    "AssetClassViolation",
    "assert_asset_class",
    "assert_order_event_payload",
    "Bar",
    "BarConsolidator",
    "BollingerBands",
    "BollingerValue",
    "CalendarHorizonExceeded",
    "EMA",
    "HistoricalData",
    "InMemoryScheduler",
    "Indicator",
    "LIVE_CALLBACK_LATENCY_P95_MS",
    "MACD",
    "MACDValue",
    "NormalizationMode",
    "NotATradingSession",
    "OrderEvent",
    "OrderEventContractError",
    "OrderEventType",
    "OrderHandle",
    "OrderRequest",
    "OrderSide",
    "OrderType",
    "PAPER_CALLBACK_LATENCY_P95_MS",
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
    "UsEquityTradingCalendar",
    "WarmupController",
    "WarmupNotComplete",
    "WarmupState",
    "assert_warmup_complete",
]
