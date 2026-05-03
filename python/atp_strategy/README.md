# `atp_strategy` — Python Strategy API author guide

This is the user-facing SDK for ATP strategies. The same interface is wired
to live IB execution and to the internal paper simulation engine; user code
contains no execution-mode branches (`SRS-SDK-001`).

SRS trace: `SRS-SDK-001` through `SRS-SDK-009` in `docs/SRS.md` §5.2.

## Minimal strategy skeleton

Every strategy subclasses `Strategy` and overrides the callbacks it cares
about. The orchestrator instantiates the class, hands it a
`StrategyContext`, replays warm-up bars, then enters the live event loop.

Example:

```python
from atp_strategy import Strategy, StrategyContext, Bar


class HelloStrategy(Strategy):
    warmup_bars = 50

    def on_start(self, ctx: StrategyContext) -> None:
        ctx.subscribe("AAPL")
        ctx.log(f"started with {ctx.config.tradable_asset_class}")

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        ctx.log(f"{bar.symbol} close={bar.close}")
```

## Scheduling (`SRS-SDK-002`)

`ctx.schedule` exposes calendar-aware primitives. Times resolve through the
bound `TradingCalendar`, so NYSE/NASDAQ/CBOE holidays, early closes, and
DST are handled centrally — your code never has to know.

Example:

```python
def on_start(self, ctx):
    ctx.schedule.at_market_open(self._open)
    ctx.schedule.at_market_close(self._close, offset_minutes=-5)
    ctx.schedule.every_n_minutes(15, self._sample, only_during_session=True)
    ctx.schedule.cron("0 12 * * MON-FRI", self._noon)

def on_schedule(self, ctx, tag):
    ctx.log(f"scheduled tick: {tag}")
```

## Subscriptions and asset class (`SRS-SDK-003`)

A strategy is configured for one tradable asset class (`EQUITY` or
`OPTION`). It may subscribe to either or both for analysis, but submitting
an order against the unconfigured class raises `AssetClassViolation`.

Example:

```python
from atp_strategy import AssetClass, OrderRequest, OrderSide, OrderType

def on_start(self, ctx):
    ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
    ctx.subscribe("AAPL", asset_class=AssetClass.OPTION)  # analysis only

def on_bar(self, ctx, bar):
    ctx.order(OrderRequest(
        symbol="AAPL", quantity=10, side=OrderSide.BUY,
        order_type=OrderType.MARKET, asset_class=AssetClass.EQUITY,
    ))
```

## Indicators (`SRS-SDK-006`)

`atp_strategy.indicators` exposes incremental built-ins: `SMA`, `EMA`,
`RSI`, `MACD`, `BollingerBands`, `ATR`. Each accepts a new `Bar` per call
and exposes `value` / `is_ready`.

Example:

```python
from atp_strategy import SMA

class Crossover(Strategy):
    warmup_bars = 50

    def on_start(self, ctx):
        self._fast = SMA(period=10)
        self._slow = SMA(period=50)

    def on_bar(self, ctx, bar):
        self._fast.update(bar)
        self._slow.update(bar)
        if self._fast.is_ready and self._slow.is_ready:
            if self._fast.value > self._slow.value:
                ctx.log("fast above slow")
```

## Warm-up (`SRS-SDK-005`)

`warmup_bars` declares how many historical bars to replay before live
execution. The runtime drives `on_bar` for warm-up bars first, then calls
`on_warmup_complete`, then begins live processing.

Example:

```python
class WarmedUp(Strategy):
    warmup_bars = 200

    def on_warmup_complete(self, ctx):
        ctx.log("indicator buffers initialized; live trading enabled")
```

## Order events (`SRS-SDK-004`)

Every fill, partial fill, cancellation, rejection, ack, and expiry is
delivered as an `OrderEvent`. Fields include `fill_price`, `fill_quantity`,
`commission`, and the broker plus client correlation IDs.

Example:

```python
from atp_strategy import OrderEventType

def on_order_event(self, ctx, event):
    if event.event_type == OrderEventType.FILL:
        ctx.log(f"filled {event.fill_quantity} @ {event.fill_price}")
    elif event.event_type == OrderEventType.REJECTED:
        ctx.log(f"rejected: {event.reason}")
```

## Historical data

`ctx.history.get_bars` returns a list of `Bar` records from the unified
historical-data service.

Example:

```python
def on_warmup_complete(self, ctx):
    bars = ctx.history.get_bars("AAPL", lookback=200, frequency="1m")
    ctx.log(f"loaded {len(bars)} historical bars")
```

## State access

`get_state` / `set_state` persist JSON-serializable values. State survives
container restart per `SRS-EXE-005`.

Example:

```python
def on_bar(self, ctx, bar):
    seen = ctx.get_state("seen", default=0)
    ctx.set_state("seen", seen + 1)
```

## Logging and cancellation

`ctx.log` writes a structured strategy log line. `ctx.cancel(handle)`
cancels a previously-submitted order.

Example:

```python
def on_bar(self, ctx, bar):
    handle = ctx.order(OrderRequest(
        symbol="AAPL", quantity=1, side=OrderSide.BUY,
        order_type=OrderType.LIMIT, limit_price=100.0,
    ))
    ctx.log(f"working order {handle.order_id}")
    ctx.cancel(handle)
```
