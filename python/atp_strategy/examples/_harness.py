"""In-process harness for running example strategies locally.

This module is **not** part of the documented Strategy API. Strategy
authors never import it in production code; the live runtime delivers
the same callback surface from real IB Gateway / paper simulation
paths per ``SRS-SDK-001`` ``AC-14``. The harness exists so the
example modules under :mod:`atp_strategy.examples` can be run
end-to-end (``python -m atp_strategy.examples.<name>``) for local
verification without spinning up the orchestrator.

The harness uses ONLY public ``atp_strategy`` exports. It implements
the ``StrategyContext`` Protocol with simple in-memory state, a
``HistoricalData`` stub that returns synthetic OHLCV bars, the
``StaticTradingCalendar`` for scheduling, and a no-op
``InMemoryScheduler`` instance. ``run`` walks the standard runtime
sequence (``on_start`` → warm-up replay via
:class:`atp_strategy.WarmupController` → executable ``on_bar`` ticks
→ a synthesized ``OrderEvent``) and returns the captured log lines
for inspection.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from atp_strategy import (
    AssetClass,
    AssetClassViolation,
    Bar,
    InMemoryScheduler,
    NormalizationMode,
    OrderEvent,
    OrderEventType,
    OrderHandle,
    OrderRequest,
    StaticTradingCalendar,
    Strategy,
    StrategyConfig,
    WarmupController,
    WarmupState,
    assert_warmup_complete,
)


@dataclass
class _StubHistory:
    """Synthetic ``HistoricalData`` source emitting deterministic ramp bars."""

    bars_per_symbol: int = 250

    def get_bars(
        self,
        symbol: str,
        *,
        lookback: int,
        frequency: str = "1m",
        end: Any = None,
        asset_class: AssetClass = AssetClass.EQUITY,
        normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED,
    ) -> list[Bar]:
        n = min(lookback, self.bars_per_symbol)
        return [
            Bar(
                symbol=symbol,
                timestamp=f"2026-04-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=1000 + i,
            )
            for i in range(n)
        ]


@dataclass
class _HarnessContext:
    """Minimal ``StrategyContext`` implementation backed by in-memory state."""

    config: StrategyConfig
    schedule: InMemoryScheduler
    calendar: StaticTradingCalendar
    history: _StubHistory
    subscriptions: list[tuple[str, AssetClass]] = field(default_factory=list)
    orders: list[OrderRequest] = field(default_factory=list)
    cancellations: list[OrderHandle] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    _warmup_state: WarmupState = WarmupState.PENDING
    _order_counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    @property
    def warmup_state(self) -> WarmupState:
        return self._warmup_state

    def subscribe(self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY) -> None:
        self.subscriptions.append((symbol, asset_class))

    def order(self, request: OrderRequest) -> OrderHandle:
        # Mirror the production dispatchers: refuse order submission
        # until warm-up reaches WarmupState.COMPLETE so author errors
        # (ordering during warm-up replay) surface here rather than
        # only in the live runtime (SRS-SDK-005 AC).
        assert_warmup_complete(self._warmup_state)
        if request.asset_class != self.config.tradable_asset_class:
            raise AssetClassViolation(
                f"strategy {self.config.strategy_id} configured for "
                f"{self.config.tradable_asset_class.value} cannot trade "
                f"{request.asset_class.value}"
            )
        self.orders.append(request)
        order_id = f"ord-{next(self._order_counter)}"
        return OrderHandle(order_id=order_id, strategy_id=self.config.strategy_id)

    def cancel(self, handle: OrderHandle) -> None:
        self.cancellations.append(handle)

    def log(self, message: str) -> None:
        self.log_lines.append(message)

    def get_state(self, key: str, default: Any = None) -> Any:
        return self.state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self.state[key] = value

    def indicator(self, name: str, **params: Any) -> Any:  # noqa: ARG002
        raise NotImplementedError(
            "harness does not lazily-construct indicators; strategy authors "
            "import the indicator class directly from atp_strategy"
        )

    def consolidate(self, symbol: str, period: str) -> Any:  # noqa: ARG002
        raise NotImplementedError("harness does not implement consolidation")


def build_context(
    strategy_id: str,
    *,
    asset_class: AssetClass = AssetClass.EQUITY,
    warmup_bars: int = 0,
    history_bars: int = 250,
) -> _HarnessContext:
    """Construct a fresh harness context with sensible defaults."""
    calendar = StaticTradingCalendar()
    return _HarnessContext(
        config=StrategyConfig(
            strategy_id=strategy_id,
            tradable_asset_class=asset_class,
            warmup_bars=warmup_bars,
        ),
        schedule=InMemoryScheduler(calendar=calendar),
        calendar=calendar,
        history=_StubHistory(bars_per_symbol=history_bars),
    )


def _make_bar(symbol: str, index: int) -> Bar:
    """Build a single deterministic ramp bar (offset 500 to mark live)."""
    return Bar(
        symbol=symbol,
        timestamp=f"2026-05-{(index // 24) + 1:02d}T{index % 24:02d}:00:00Z",
        open=600.0 + index,
        high=601.0 + index,
        low=599.0 + index,
        close=600.5 + index,
        volume=2000 + index,
    )


def _make_fill_event(
    request: OrderRequest, handle: OrderHandle, *, sequence: int = 0
) -> OrderEvent:
    """Build a synthetic FILL event for ``request`` to drive ``on_order_event``."""
    return OrderEvent(
        event_type=OrderEventType.FILL,
        order_id=handle.order_id,
        client_order_id=request.client_order_id or f"cli-{sequence}",
        strategy_id=handle.strategy_id,
        symbol=request.symbol,
        fill_price=600.5,
        fill_quantity=request.quantity,
        cumulative_filled=request.quantity,
        remaining_quantity=0,
        commission=0.05,
        reason=None,
        timestamp=f"2026-05-04T13:30:{sequence:02d}Z",
    )


def run(
    strategy: Strategy,
    *,
    symbol: str = "AAPL",
    asset_class: AssetClass = AssetClass.EQUITY,
    history_bars: int = 250,
    executable_bars: int = 10,
    deliver_fill_for_first_order: bool = True,
) -> _HarnessContext:
    """Drive ``strategy`` through warm-up + executable bars + a sample fill.

    Returns the populated ``_HarnessContext`` so callers (or the
    ``__main__`` block of an example module) can inspect the captured
    log lines, recorded subscriptions, and submitted orders.
    """
    warmup_bars = int(getattr(strategy, "warmup_bars", 0) or 0)
    ctx = build_context(
        strategy_id=getattr(strategy, "strategy_id", "demo"),
        asset_class=asset_class,
        warmup_bars=warmup_bars,
        history_bars=max(history_bars, warmup_bars),
    )
    strategy.on_start(ctx)
    subscriptions = list(ctx.subscriptions) or [(symbol, asset_class)]
    warmup_subscriptions = [(sym, cls) for sym, cls in subscriptions if cls == asset_class] or [
        (symbol, asset_class)
    ]
    # Drive warm-up via the SDK WarmupController so the example surface
    # exercises the same class production dispatchers wire in. The
    # harness mirrors the production state-flip-before-callback order
    # (controller flips its private _state to COMPLETE before invoking
    # on_warmup_complete; the harness mirrors that on ctx._warmup_state
    # via two callbacks wrapped around controller.run()).
    controller = WarmupController(
        strategy=strategy,
        context=ctx,
        history=ctx.history,
        subscriptions=warmup_subscriptions,
    )
    # WarmupController fires on_warmup_complete on the strategy, so
    # ctx._warmup_state must reach COMPLETE before that callback runs.
    # We monkey-patch on_warmup_complete to flip the gate first, then
    # call the user's callback. This keeps the harness aligned with
    # the production dispatcher contract without re-implementing the
    # controller's replay loop.
    user_on_warmup_complete = strategy.on_warmup_complete

    def _gated_on_warmup_complete(context: Any) -> None:
        ctx._warmup_state = WarmupState.COMPLETE
        user_on_warmup_complete(context)

    strategy.on_warmup_complete = _gated_on_warmup_complete  # type: ignore[method-assign]
    try:
        ctx._warmup_state = WarmupState.IN_PROGRESS
        controller.run()
    finally:
        strategy.on_warmup_complete = user_on_warmup_complete  # type: ignore[method-assign]
    pre_order_count = len(ctx.orders)
    for index in range(executable_bars):
        for sym, _cls in subscriptions:
            strategy.on_bar(ctx, _make_bar(sym, index))
    if deliver_fill_for_first_order and len(ctx.orders) > pre_order_count:
        first_new = ctx.orders[pre_order_count]
        # The harness assigned ord-<n> sequentially in ctx.order().
        order_id = f"ord-{pre_order_count + 1}"
        handle = OrderHandle(order_id=order_id, strategy_id=ctx.config.strategy_id)
        strategy.on_order_event(ctx, _make_fill_event(first_new, handle))
    return ctx
