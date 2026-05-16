"""SRS-SDK-001 — Python Strategy API parity invariant (L7 domain / safety).

The L3 wrapper at ``tests/test_strategy_api_parity_contract.py`` proves
the AST-level invariants (no mode-discriminator attribute / symbol leakage,
no vendor-SDK imports, full Protocol surface, no execution-mode params,
no ``StrategyConfig`` mode fields). This L7 test proves the *behavioural*
invariant the static checks point at: the **same strategy source** runs
through two ``StrategyContext`` implementations — one labelled "live",
one labelled "paper" — and produces a byte-identical recorded call
sequence, satisfying SyRS AC-14 / SYS-82..SYS-87.

The drivers here are stubs by design; the concrete ``LiveIBStrategyContext``
and ``PaperSimStrategyContext`` land with SRS-EXE-001 / SRS-SIM-*. The
parity invariant exercised here will automatically cover those concrete
drivers once they subclass ``StrategyContext``.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = REPO_ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_strategy import (  # noqa: E402
    AssetClass,
    AssetClassViolation,
    Bar,
    OrderEvent,
    OrderEventType,
    OrderHandle,
    OrderRequest,
    OrderSide,
    OrderType,
    SMA,
    StaticTradingCalendar,
    Strategy,
    StrategyConfig,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


# --------------------------------------------------------------------------- #
# Recording stub drivers — same surface for "live" and "paper".
# --------------------------------------------------------------------------- #


class _Scheduler:
    """Minimal Scheduler stub used by both drivers."""

    def at_market_open(self, callback, *, offset_minutes: int = 0):
        return _Handle()

    def at_market_close(self, callback, *, offset_minutes: int = 0):
        return _Handle()

    def every_n_minutes(self, n, callback, *, only_during_session: bool = True):
        return _Handle()

    def cron(self, expression, callback):
        return _Handle()


class _Handle:
    def cancel(self) -> None:
        return None


class _Consolidator:
    def consolidate(self, source_symbol: str, *, period: str):
        return iter(())


class _History:
    def get_bars(self, symbol, *, lookback, frequency="1m", end=None, asset_class=AssetClass.EQUITY, normalization=None):
        return []


class _RecordingStub:
    """Records every call so live and paper transcripts can be compared.

    Implements every method on the ``StrategyContext`` Protocol. Each call
    appends ``(method_name, sorted_kwarg_names)`` to ``self.events``.
    ``order()`` returns a deterministic ``OrderHandle`` and stamps an
    ``OrderEvent(ACK)`` + ``OrderEvent(FILL)`` pair so a probe strategy
    can be driven end-to-end without a real engine.
    """

    label: str

    def __init__(self, *, label: str, config: StrategyConfig) -> None:
        self.label = label
        self.config = config
        self.schedule = _Scheduler()
        self.calendar = StaticTradingCalendar()
        self.history = _History()
        self.events: list[tuple[str, tuple[str, ...]]] = []
        self._state: dict[str, object] = {}
        self._next_order = 0
        self.emitted_events: list[OrderEvent] = []

    def _record(self, method: str, **kwargs: object) -> None:
        self.events.append((method, tuple(sorted(kwargs.keys()))))

    def subscribe(self, symbol: str, asset_class: AssetClass = AssetClass.EQUITY) -> None:
        self._record("subscribe", symbol=symbol, asset_class=asset_class)

    def order(self, request: OrderRequest) -> OrderHandle:
        if request.asset_class != self.config.tradable_asset_class:
            raise AssetClassViolation(
                f"{self.label}: strategy {self.config.strategy_id} cannot trade {request.asset_class}"
            )
        self._record("order", request=request)
        self._next_order += 1
        oid = f"{self.label}-ord-{self._next_order}"
        handle = OrderHandle(order_id=oid, strategy_id=self.config.strategy_id)
        timestamp = dt.datetime(2026, 5, 15, 13, 30, tzinfo=dt.timezone.utc).isoformat()
        self.emitted_events.append(
            OrderEvent(
                event_type=OrderEventType.ACK,
                order_id=oid,
                client_order_id=request.client_order_id or f"cli-{self._next_order}",
                strategy_id=self.config.strategy_id,
                symbol=request.symbol,
                fill_price=None,
                fill_quantity=None,
                cumulative_filled=0,
                remaining_quantity=request.quantity,
                commission=None,
                reason=None,
                timestamp=timestamp,
            )
        )
        self.emitted_events.append(
            OrderEvent(
                event_type=OrderEventType.FILL,
                order_id=oid,
                client_order_id=request.client_order_id or f"cli-{self._next_order}",
                strategy_id=self.config.strategy_id,
                symbol=request.symbol,
                fill_price=100.0,
                fill_quantity=request.quantity,
                cumulative_filled=request.quantity,
                remaining_quantity=0,
                commission=0.05,
                reason=None,
                timestamp=timestamp,
            )
        )
        return handle

    def cancel(self, handle: OrderHandle) -> None:
        self._record("cancel", handle=handle)

    def log(self, message: str) -> None:
        self._record("log", message=message)

    def get_state(self, key: str, default: object | None = None) -> object | None:
        self._record("get_state", default=default, key=key)
        return self._state.get(key, default)

    def set_state(self, key: str, value: object) -> None:
        self._record("set_state", key=key, value=value)
        self._state[key] = value

    def indicator(self, name: str, **params: object):
        self._record("indicator", name=name, **params)
        if name == "SMA":
            return SMA(period=int(params.get("period", 3)))
        raise ValueError(f"unsupported indicator {name}")

    def consolidate(self, symbol: str, period: str):
        self._record("consolidate", period=period, symbol=symbol)
        return _Consolidator()


class LiveExecutionStub(_RecordingStub):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(label="live", config=config)


class PaperExecutionStub(_RecordingStub):
    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(label="paper", config=config)


# --------------------------------------------------------------------------- #
# Probe strategy used to drive both stubs.
# --------------------------------------------------------------------------- #


class ParityProbeStrategy(Strategy):
    """Exercises every callback shape SRS-SDK-001 requires to be identical."""

    warmup_bars = 3

    def on_start(self, ctx) -> None:
        ctx.subscribe("AAPL", asset_class=AssetClass.EQUITY)
        ctx.set_state("started", True)
        ctx.consolidate("AAPL", period="5m")
        ctx.indicator("SMA", period=3)
        ctx.log("started")

    def on_warmup_complete(self, ctx) -> None:
        ctx.set_state("warm", True)
        ctx.log("warm-up complete")

    def on_bar(self, ctx, bar) -> None:
        handle = ctx.order(
            OrderRequest(
                symbol=bar.symbol,
                quantity=1,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                asset_class=AssetClass.EQUITY,
            )
        )
        ctx.cancel(handle)
        ctx.log(f"bar {bar.close}")

    def on_order_event(self, ctx, event) -> None:
        ctx.log(f"order event {event.event_type}")


# --------------------------------------------------------------------------- #
# Drivers.
# --------------------------------------------------------------------------- #


def _drive(stub: _RecordingStub) -> ParityProbeStrategy:
    strategy = ParityProbeStrategy()
    strategy.on_start(stub)
    strategy.on_warmup_complete(stub)
    for close in (100.0, 100.5):
        strategy.on_bar(stub, Bar("AAPL", "2026-05-15T13:30:00Z", close, close, close, close, 100))
    for event in list(stub.emitted_events):
        strategy.on_order_event(stub, event)
    return strategy


# --------------------------------------------------------------------------- #
# Invariants.
# --------------------------------------------------------------------------- #


def test_recorded_call_sequence_is_identical() -> None:
    config = StrategyConfig(strategy_id="probe", tradable_asset_class=AssetClass.EQUITY, warmup_bars=3)
    live = LiveExecutionStub(config)
    paper = PaperExecutionStub(config)
    _drive(live)
    _drive(paper)
    assert live.events == paper.events, (
        "live and paper call sequences must be byte-identical; AC-14 "
        f"violation. live={live.events!r}\npaper={paper.events!r}"
    )


def test_order_event_payload_shape_is_identical() -> None:
    config = StrategyConfig(strategy_id="probe", tradable_asset_class=AssetClass.EQUITY, warmup_bars=3)
    live = LiveExecutionStub(config)
    paper = PaperExecutionStub(config)
    _drive(live)
    _drive(paper)
    assert len(live.emitted_events) == len(paper.emitted_events) > 0
    for live_evt, paper_evt in zip(live.emitted_events, paper.emitted_events):
        live_populated = {f: getattr(live_evt, f) is not None for f in live_evt.__dataclass_fields__}
        paper_populated = {f: getattr(paper_evt, f) is not None for f in paper_evt.__dataclass_fields__}
        assert live_populated == paper_populated, (
            "OrderEvent populated-field shape must match across live and "
            f"paper. live={live_populated}, paper={paper_populated}"
        )


def test_probe_strategy_source_has_no_mode_branch() -> None:
    source = textwrap.dedent(inspect.getsource(ParityProbeStrategy))
    tree = ast.parse(source)
    forbidden = {"execution_mode", "is_paper", "is_live", "mode"}
    forbidden_symbols = {"ExecutionMode", "StrategyMode"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden, (
                f"ParityProbeStrategy contains forbidden attribute access "
                f"`.{node.attr}` at line {node.lineno} — the probe must "
                "be mode-blind so it actually exercises the parity invariant"
            )
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_symbols, (
                f"ParityProbeStrategy references forbidden symbol "
                f"`{node.id}` at line {node.lineno}"
            )


def test_off_class_order_raises_from_both_stubs() -> None:
    config = StrategyConfig(strategy_id="probe", tradable_asset_class=AssetClass.EQUITY, warmup_bars=0)
    live = LiveExecutionStub(config)
    paper = PaperExecutionStub(config)
    bad = OrderRequest(
        symbol="AAPL",
        quantity=1,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        asset_class=AssetClass.OPTION,
    )
    with pytest.raises(AssetClassViolation):
        live.order(bad)
    with pytest.raises(AssetClassViolation):
        paper.order(bad)
