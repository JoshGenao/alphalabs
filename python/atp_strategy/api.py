"""Strategy author interfaces exposed inside Python strategy containers.

This package is intentionally limited to user-facing interfaces. Core ATP
runtime services live in Rust crates under ``crates/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    quantity: int
    side: OrderSide
    order_type: OrderType
    limit_price: float | None = None
    stop_price: float | None = None


@dataclass(frozen=True, slots=True)
class OrderHandle:
    order_id: str
    strategy_id: str


class StrategyContext(Protocol):
    def subscribe(self, symbol: str) -> None:
        """Subscribe this strategy to market data for ``symbol``."""

    def order(self, request: OrderRequest) -> OrderHandle:
        """Submit an order through the runtime-selected execution path."""

    def log(self, message: str) -> None:
        """Write a strategy log message."""

    def get_state(self, key: str, default: object | None = None) -> object | None:
        """Read a JSON-serializable value from strategy state."""

    def set_state(self, key: str, value: object) -> None:
        """Persist a JSON-serializable value to strategy state."""


class Strategy:
    """Base class for Python-authored ATP strategies."""

    def on_start(self, context: StrategyContext) -> None:
        """Run once after the strategy container has initialized."""

    def on_bar(self, context: StrategyContext, bar: Bar) -> None:
        """Run when a subscribed bar arrives."""

    def on_order_event(self, context: StrategyContext, order: OrderHandle) -> None:
        """Run when an order event is delivered to the strategy."""
