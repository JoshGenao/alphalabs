"""Equity-tradable strategy that subscribes to both EQUITY and OPTION data.

SRS trace: ``SRS-SDK-009`` (exercises ``SRS-SDK-001`` parity,
``SRS-SDK-003`` single-tradable-asset invariant).

Demonstrates the asymmetry in ``SRS-SDK-003`` between data
subscription and order submission:

* A strategy may ``ctx.subscribe`` to BOTH ``AssetClass.EQUITY`` and
  ``AssetClass.OPTION`` regardless of its configured tradable
  class, for analysis purposes.
* But order submission is gated on the configured tradable class.
  Submitting an ``OrderRequest`` with ``asset_class=OPTION`` from an
  ``EQUITY``-configured strategy raises ``AssetClassViolation``.

The example deliberately attempts the forbidden option order in
``on_warmup_complete`` and catches the violation so authors see what
the error path looks like.

Run it locally::

    python -m atp_strategy.examples.dual_asset_analytics
"""

from __future__ import annotations

from atp_strategy import (
    AssetClass,
    AssetClassViolation,
    Bar,
    OrderRequest,
    OrderSide,
    OrderType,
    Strategy,
    StrategyContext,
)
from atp_strategy.examples import _harness


class DualAssetAnalyticsStrategy(Strategy):
    """Subscribe to AAPL equity + option chain; only equity orders allowed."""

    strategy_id = "dual_asset_analytics"
    warmup_bars = 0

    def __init__(self) -> None:
        self._equity_bars: int = 0
        self._option_bars: int = 0

    def on_start(self, ctx: StrategyContext) -> None:
        ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
        ctx.subscribe("AAPL", asset_class=AssetClass.OPTION)
        ctx.log("subscribed to AAPL equity + option chain for analysis")

    def on_warmup_complete(self, ctx: StrategyContext) -> None:
        # Demonstrate the single-tradable-asset invariant: this option
        # order MUST be rejected even though the option subscription
        # is allowed for analysis (SRS-SDK-003).
        try:
            ctx.order(
                OrderRequest(
                    symbol="AAPL  260620C00200000",
                    quantity=1,
                    side=OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    asset_class=AssetClass.OPTION,
                )
            )
        except AssetClassViolation as exc:
            ctx.log(f"option order correctly rejected: {exc}")

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        if bar.symbol == "AAPL":
            self._equity_bars += 1
        ctx.log(f"bar {bar.symbol} close={bar.close}")


def main() -> None:
    ctx = _harness.run(
        DualAssetAnalyticsStrategy(),
        symbol="AAPL",
        asset_class=AssetClass.EQUITY,
        executable_bars=2,
        deliver_fill_for_first_order=False,
    )
    for line in ctx.log_lines:
        print(line)


if __name__ == "__main__":
    main()
