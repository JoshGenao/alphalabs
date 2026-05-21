"""Canonical fast/slow SMA crossover strategy with 200-bar warm-up.

SRS trace: ``SRS-SDK-009`` (exercises ``SRS-SDK-001`` parity,
``SRS-SDK-002`` scheduling, ``SRS-SDK-004`` order events,
``SRS-SDK-005`` warm-up, ``SRS-SDK-006`` indicators).

Demonstrates the full author workflow end-to-end:

* Declare ``warmup_bars = 200`` so the runtime replays 200 historical
  bars before the first executable bar arrives.
* Construct ``SMA`` indicators in ``on_start`` and update both on
  every bar (historical AND live — same callback).
* React to the warm-up boundary in ``on_warmup_complete``.
* Submit a market ``OrderRequest`` on the crossover, gated on
  ``is_ready`` so signals never fire on warmup-incomplete buffers.
* Schedule a flatten at five minutes before the close via
  ``ctx.schedule.at_market_close(offset_minutes=-5)``.
* Log fill / partial-fill / cancellation / rejection events through
  ``Strategy.on_order_event``.

Run it locally::

    python -m atp_strategy.examples.sma_crossover
"""

from __future__ import annotations

from atp_strategy import (
    SMA,
    AssetClass,
    Bar,
    OrderEvent,
    OrderEventType,
    OrderHandle,
    OrderRequest,
    OrderSide,
    OrderType,
    Strategy,
    StrategyContext,
)
from atp_strategy.examples import _harness


class SmaCrossoverStrategy(Strategy):
    """Buy when fast SMA(10) crosses above slow SMA(50); flatten before close."""

    strategy_id = "sma_crossover"
    warmup_bars = 200

    def __init__(self) -> None:
        self._fast = SMA(period=10)
        self._slow = SMA(period=50)
        self._warmup_complete = False
        self._working_order: OrderHandle | None = None

    def on_start(self, ctx: StrategyContext) -> None:
        ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
        # Initialise persisted state. ctx.get_state survives container
        # restart (SRS-EXE-005), so a strategy that crashed mid-day can
        # rebuild its position counter from disk before warm-up.
        ctx.set_state("position", ctx.get_state("position", default=0))
        # Flatten five minutes before the regular session close so the
        # strategy never carries an overnight position. The bound
        # TradingCalendar resolves the close (16:00 ET regular / 13:00
        # ET early-close days) and DST automatically. The flatten work
        # itself happens directly inside the callback the scheduler
        # holds (self._flatten) — the runtime's direct-callback path
        # is the typical authoring surface for scheduled triggers.
        ctx.schedule.at_market_close(self._flatten, offset_minutes=-5)

    def on_warmup_complete(self, ctx: StrategyContext) -> None:
        self._warmup_complete = True
        ready = self._fast.is_ready and self._slow.is_ready
        ctx.log(f"warm-up complete; indicators ready={ready}")

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        self._fast.update(bar)
        self._slow.update(bar)
        # Strategy.on_bar fires during warm-up replay AND for live
        # executable bars. ctx.order during warm-up raises
        # WarmupNotComplete (SRS-SDK-005), so signals only act after
        # on_warmup_complete has fired.
        if not self._warmup_complete:
            return
        if not (self._fast.is_ready and self._slow.is_ready):
            return
        position = int(ctx.get_state("position", default=0) or 0)
        if self._fast.value > self._slow.value and position == 0:
            handle = ctx.order(
                OrderRequest(
                    symbol=bar.symbol,
                    quantity=10,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    asset_class=AssetClass.EQUITY,
                )
            )
            self._working_order = handle
            ctx.set_state("position", 10)
            ctx.log(f"BUY signal at close={bar.close}")
        elif self._fast.value < self._slow.value and position > 0:
            handle = ctx.order(
                OrderRequest(
                    symbol=bar.symbol,
                    quantity=position,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    asset_class=AssetClass.EQUITY,
                )
            )
            self._working_order = handle
            ctx.set_state("position", 0)
            ctx.log(f"SELL signal at close={bar.close}")

    def on_order_event(self, ctx: StrategyContext, event: OrderEvent) -> None:
        if event.event_type == OrderEventType.FILL:
            self._working_order = None
            ctx.log(
                f"FILL {event.symbol} qty={event.fill_quantity} "
                f"price={event.fill_price} commission={event.commission}"
            )
        elif event.event_type == OrderEventType.REJECTED:
            self._working_order = None
            ctx.log(f"REJECT {event.symbol} reason={event.reason}")

    def _flatten(self, ctx: StrategyContext) -> None:
        # Direct Scheduler callback for `at_market_close(offset=-5)`. If
        # we still have a working buy/sell when the close trigger fires,
        # cancel it; otherwise emit a flatten sell to take the position
        # to zero.
        if self._working_order is not None:
            ctx.cancel(self._working_order)
            self._working_order = None
        position = int(ctx.get_state("position", default=0) or 0)
        if position > 0:
            ctx.order(
                OrderRequest(
                    symbol="AAPL",
                    quantity=position,
                    side=OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    asset_class=AssetClass.EQUITY,
                )
            )
            ctx.set_state("position", 0)
            ctx.log("flatten before close")


def main() -> None:
    ctx = _harness.run(
        SmaCrossoverStrategy(),
        symbol="AAPL",
        history_bars=200,
        executable_bars=12,
    )
    for line in ctx.log_lines:
        print(line)


if __name__ == "__main__":
    main()
