"""Minimal "hello world" strategy that proves SDK installation.

SRS trace: ``SRS-SDK-009`` (``SRS-SDK-001`` parity surface).

Demonstrates the smallest possible authoring shape: subclass
``Strategy``, override ``on_start`` to declare a market-data
subscription, override ``on_bar`` to react to incoming bars.

Run it locally to verify your environment is wired up::

    python -m atp_strategy.examples.hello
"""

from __future__ import annotations

from atp_strategy import Bar, Strategy, StrategyContext
from atp_strategy.examples import _harness


class HelloStrategy(Strategy):
    """Subscribe to AAPL and log every bar's close."""

    strategy_id = "hello"
    warmup_bars = 0

    def on_start(self, ctx: StrategyContext) -> None:
        ctx.subscribe("AAPL")
        ctx.log("HelloStrategy started; subscribed to AAPL")

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        ctx.log(f"{bar.symbol} close={bar.close}")


def main() -> None:
    ctx = _harness.run(HelloStrategy(), symbol="AAPL", executable_bars=3)
    for line in ctx.log_lines:
        print(line)


if __name__ == "__main__":
    main()
