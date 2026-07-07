# `atp_strategy` — Python Strategy API author guide

This is the user-facing SDK for ATP strategies. The same interface is wired
to live IB execution and to the internal paper simulation engine; user code
contains no execution-mode branches (`SRS-SDK-001`).

SRS trace: `SRS-SDK-001` through `SRS-SDK-009` in `docs/SRS.md` §5.2.

## Getting started

The SDK lives inside the strategy container the orchestrator builds for you
— `pip install atp-strategy` is not needed. Every name in this guide is
imported from the package facade:

```python
from atp_strategy import Strategy, StrategyContext, Bar, SMA, OrderRequest
```

Three things every strategy needs:

1. A subclass of `Strategy` that overrides the callbacks you care about.
2. A `StrategyConfig` (built by the orchestrator from your container
   manifest): `strategy_id`, `tradable_asset_class` (`EQUITY` or `OPTION`),
   and `warmup_bars` (how many historical bars to replay before the first
   executable bar). Calendar / scheduling defaults to `America/New_York`.
3. At least one `ctx.subscribe(...)` call inside `on_start` so the runtime
   knows which market-data streams to fan in.

Verify your environment by running one of the bundled example strategies:

```bash
python -m atp_strategy.examples.hello
python -m atp_strategy.examples.sma_crossover
python -m atp_strategy.examples.dual_asset_analytics
```

Each example is a self-contained `Strategy` subclass plus a small
`__main__` block that walks it through warm-up + a handful of bars + a
sample fill against an in-process stub dispatcher. Clone whichever
example is closest to your idea and edit from there.

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

`warmup_bars` declares how many historical bars to replay before live,
paper, or backtest execution begins. The orchestrator constructs a
`WarmupController` per container which walks the
`PENDING → IN_PROGRESS → COMPLETE` lifecycle: historical bars flow
through `on_bar` first, then `on_warmup_complete` fires exactly once,
then executable bars are allowed through. `assert_warmup_complete(state)`
is the executable-boundary guard production dispatchers call; user
code never instantiates the controller directly.

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

`frequency` accepts `"1m"` and `"1d"` (served from the stored datasets) plus
`"5m"`, `"15m"`, and `"1h"`, which are consolidated on the fly from the stored
minute bars (`SRS-SDK-007`) — no pre-processed higher-timeframe dataset is
required.

## Consolidation (`SRS-SDK-007`)

Time-based bar consolidation turns a minute series into `"5m"`, `"15m"`, `"1h"`,
or `"1d"` bars **without a pre-processed dataset**. OHLCV is aggregated the
standard way: open = first, high = max, low = min, close = last, volume = sum.
Intraday buckets align to the wall-clock period (5-minute / 15-minute / hourly
boundaries); daily buckets group by the US-Eastern session date.

Live, consolidate incrementally inside `on_bar` — `update(bar)` returns a
completed higher-period `Bar` only when a bar opens a new bucket, and `None`
while the bucket is still filling:

```python
from atp_strategy import TimeBarConsolidator

class FiveMinuteMomentum(Strategy):
    def on_start(self, ctx):
        self._five_min = TimeBarConsolidator("5m")

    def on_bar(self, ctx, bar):
        completed = self._five_min.update(bar)  # None until a 5-minute bar closes
        if completed is not None:
            ctx.log(f"5m close {completed.close} vol {completed.volume}")
```

For a historical series (backtest, warm-up, or a research notebook), consolidate
a whole list at once. The streamed and batched bars are identical, so a signal
computed on consolidated bars behaves the same live and in simulation:

```python
from atp_strategy import consolidate_bars

def on_warmup_complete(self, ctx):
    minute = ctx.history.get_bars("AAPL", lookback=390, frequency="1m")
    hourly = consolidate_bars(minute, "1h")
    ctx.log(f"{len(hourly)} hourly bars from {len(minute)} minute bars")
```

`ctx.consolidate(symbol, period)` is the equivalent runtime-managed handle for the
live minute subscription.

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

## Callback reference

Override these `Strategy` methods. The runtime invokes them in this order:
`on_start` once at container init, then warm-up replay (each replayed
historical bar is delivered to `on_bar`), then `on_warmup_complete` once,
then live executable bars (interleaved with `on_order_event` and
`on_schedule`).

| Hook                  | When it fires                                                                                       |
|-----------------------|-----------------------------------------------------------------------------------------------------|
| `on_start`            | Once after the strategy container initializes. Declare subscriptions, register schedules, seed state. |
| `on_bar`              | Once per `Bar`. During warm-up: a replayed historical bar. After warm-up: a live executable bar.     |
| `on_warmup_complete`  | Once, between the last warm-up bar and the first executable bar. Indicator buffers are ready.        |
| `on_order_event`      | Per `OrderEvent` — fill, partial fill, cancellation, rejection, ack, expiry (`SRS-SDK-004`).         |
| `on_schedule`         | Per scheduled trigger fire. `tag` is a runtime-emitted label (e.g. `"market_open"`); the direct callback passed to `ctx.schedule.*` is the typical authoring path (`SRS-SDK-002`). |

Strategy authors should gate order submission on warm-up completion. The
production dispatchers raise `WarmupNotComplete` if `ctx.order` is called
before `on_warmup_complete` has fired; track a `self._warmup_complete`
flag in your strategy and check it in `on_bar` (see
`atp_strategy.examples.sma_crossover` for the canonical pattern).

## StrategyContext reference

Every callback receives a `StrategyContext`. Attributes give read-only
access to runtime services; methods drive actions.

| Member                              | Purpose                                                                                       |
|-------------------------------------|-----------------------------------------------------------------------------------------------|
| `ctx.config`                        | The `StrategyConfig` for this strategy instance.                                              |
| `ctx.schedule`                      | `Scheduler` with `at_market_open` / `at_market_close` / `every_n_minutes` / `cron`.           |
| `ctx.calendar`                      | `TradingCalendar` (sessions, holidays, early closes, pre/post-market hours).                  |
| `ctx.history`                       | `HistoricalData.get_bars(symbol, lookback=..., frequency="1m", normalization=...)`.            |
| `ctx.subscribe(sym, asset_class)`   | Add a market-data subscription. Both `EQUITY` and `OPTION` may be subscribed for analysis.    |
| `ctx.order(request)`                | Submit an `OrderRequest`; returns an `OrderHandle`. Async events arrive on `on_order_event`.  |
| `ctx.cancel(handle)`                | Cancel a previously submitted order; idempotent.                                              |
| `ctx.log(message)`                  | Write a structured strategy log line.                                                         |
| `ctx.get_state(key, default=None)`  | Read a JSON-serializable value from persisted strategy state (survives restart).               |
| `ctx.set_state(key, value)`         | Persist a JSON-serializable value.                                                            |
| `ctx.indicator(name, **params)`     | Construct a built-in indicator (equivalent to importing the class directly).                  |
| `ctx.consolidate(symbol, period)`   | Open a time-based bar consolidator (`SRS-SDK-007`).                                           |

## Errors and assertions

Every exception a strategy can encounter from the SDK inherits from
`StrategyAPIError` so user code can catch one base class for the
structured-error surface (`SyRS SYS-64`).

| Exception                    | Raised when                                                                                       |
|------------------------------|---------------------------------------------------------------------------------------------------|
| `StrategyAPIError`           | Base class — catch this to handle any SDK runtime error generically.                              |
| `AssetClassViolation`        | `ctx.order` called with `asset_class` other than `config.tradable_asset_class` (`SRS-SDK-003`).    |
| `WarmupNotComplete`          | Executable action attempted before `on_warmup_complete` has fired (`SRS-SDK-005`).                |
| `OrderEventContractError`    | A dispatcher delivered an `OrderEvent` missing AC-required fields (`SRS-SDK-004`).                |
| `CalendarHorizonExceeded`    | Calendar query targets a date past the bundled calendar horizon (`SRS-SDK-002` / `SYS-50`).       |
| `NotATradingSession`         | Calendar boundary query (open / close / pre / post) targets a weekend or holiday.                 |

Two assertion helpers a strategy author may call directly:

- `assert_asset_class(config, request)` — the reference enforcement for
  `SRS-SDK-003`. Raises `AssetClassViolation`. Concrete dispatchers call
  this before routing an order; user code rarely needs to.
- `assert_warmup_complete(state)` — the executable-bar / order-submission
  gate. Raises `WarmupNotComplete` until the controller reaches
  `WarmupState.COMPLETE`. Useful inside `on_bar` if you want a typed
  failure instead of a silent gate.

## Configuration reference

`StrategyConfig` is constructed by the orchestrator from your container
manifest and handed to every callback through `ctx.config`. Strategy
authors do not construct it directly; the fields below are documented for
reference so you know what is available on `ctx.config`.

| Field                     | Type           | Default              | Meaning                                                                                    |
|---------------------------|----------------|----------------------|--------------------------------------------------------------------------------------------|
| `strategy_id`             | `str`          | required             | Stable identifier for this strategy instance.                                              |
| `tradable_asset_class`    | `AssetClass`   | required             | The single asset class this strategy may submit orders against (`EQUITY` or `OPTION`).      |
| `warmup_bars`             | `int`          | `0`                  | Number of historical bars to replay before the first executable bar (`SRS-SDK-005`).        |
| `timezone`                | `str`          | `"America/New_York"` | IANA zone for scheduling resolution (`SRS-SDK-002`).                                       |

The `Strategy` class attribute `warmup_bars` mirrors the config field and
is the developer-friendly default when no orchestrator config is wired in
(useful for local tests and the bundled example strategies).
