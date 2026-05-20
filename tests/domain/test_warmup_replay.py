"""SRS-SDK-005 / SyRS SYS-8, NFR-R3, StRS SC-18 — warm-up replay.

L7 domain (safety) test. Walks the full SRS-SDK-005 AC end-to-end
against a concrete ``_RefDispatcher`` reference impl whose dispatch
path drives :class:`atp_strategy.warmup.WarmupController` before
delivering the first executable bar. The AC is:

    "A strategy configured with a 200-bar warm-up receives historical
    bars before the first executable bar and begins with indicator
    buffers initialized."

StRS ``SC-18`` is the matching success criterion:

    "A strategy with a 200-bar warm-up period begins generating
    trading signals on the first live/backtest bar with all
    indicators fully initialized."

Locks:

* All 200 historical bars hit :py:meth:`atp_strategy.api.Strategy.on_bar`
  through the SDK-shipped controller before any executable bar is
  delivered (ordering invariant).
* The strategy's :class:`atp_strategy.api.SMA` indicator with
  ``period=200`` is :pyattr:`SMA.is_ready` and exposes a numeric value
  on the first executable bar (indicator-buffers-initialized
  invariant).
* The dispatcher refuses to deliver the first executable bar — and
  refuses to route an order submitted during warm-up — until
  :class:`atp_strategy.warmup.WarmupState` reaches ``COMPLETE``; both
  paths surface :class:`atp_strategy.api.WarmupNotComplete` per SyRS
  ``SYS-64``.
* :py:meth:`atp_strategy.api.Strategy.on_warmup_complete` fires exactly
  once at the warm-up boundary, after the last historical bar and
  before the first executable bar (per the AC ordering).
* The same controller drives the warm-up identically in live and paper
  modes per SRS-SDK-001 ``AC-14`` — the reference dispatcher exercises
  the surface, not a mode-specific code path.
* NFR-R3 restart re-execution: a fresh controller on restart re-walks
  the lifecycle so the executable gate closes again until the second
  warm-up completes; existing state on the restored strategy is
  preserved.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    SMA,
    AssetClass,
    Bar,
    Strategy,
    StrategyConfig,
    WarmupController,
    WarmupNotComplete,
    WarmupState,
    assert_warmup_complete,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


CANONICAL_WARMUP_BARS = 200


def _historical_bars(symbol: str, count: int) -> list[Bar]:
    """Return ``count`` deterministic historical bars in chronological order."""
    return [
        Bar(
            symbol,
            f"2026-04-{(i // 24) + 1:02d}T{i % 24:02d}:00:00Z",
            100.0 + i * 0.1,
            101.0 + i * 0.1,
            99.0 + i * 0.1,
            100.5 + i * 0.1,
            1000 + i,
        )
        for i in range(count)
    ]


def _executable_bars(symbol: str, count: int, *, start_index: int = 1000) -> list[Bar]:
    """Return ``count`` executable bars timestamped after the historical window."""
    return [
        Bar(
            symbol,
            f"2026-05-04T{(start_index + i) % 24:02d}:00:00Z",
            120.0 + i * 0.1,
            121.0 + i * 0.1,
            119.0 + i * 0.1,
            120.5 + i * 0.1,
            5000 + i,
        )
        for i in range(count)
    ]


@dataclass
class _RefStrategy(Strategy):
    """Minimal Strategy subclass recording every bar it sees and SMA state."""

    received_bars: list[Bar] = field(default_factory=list)
    warmup_complete_at_index: int | None = None
    warmup_complete_calls: int = 0
    sma: SMA = field(default_factory=lambda: SMA(period=CANONICAL_WARMUP_BARS))
    indicator_ready_at_index: int | None = None
    indicator_value_at_complete: float | None = None

    def on_bar(self, context, bar: Bar) -> None:  # type: ignore[override]
        self.received_bars.append(bar)
        self.sma.update(bar)
        if self.sma.is_ready and self.indicator_ready_at_index is None:
            self.indicator_ready_at_index = len(self.received_bars)

    def on_warmup_complete(self, context) -> None:  # type: ignore[override]
        self.warmup_complete_calls += 1
        self.warmup_complete_at_index = len(self.received_bars)
        self.indicator_value_at_complete = self.sma.value


@dataclass
class _StubHistoricalData:
    """Deterministic HistoricalData stub keyed by ``(symbol, lookback)``."""

    bars_by_symbol: dict[str, list[Bar]]
    calls: list[tuple[str, int, str, str]] = field(default_factory=list)

    def get_bars(
        self,
        symbol,
        *,
        lookback,
        frequency="1m",
        end=None,
        asset_class=AssetClass.EQUITY,
        normalization=None,
    ):
        self.calls.append((symbol, lookback, frequency, str(asset_class)))
        bars = self.bars_by_symbol.get(symbol, [])
        return list(bars[:lookback])


@dataclass
class _RefContext:
    """Minimal StrategyContext-shaped object the controller hands the strategy."""

    config: StrategyConfig
    submitted_orders: list[str] = field(default_factory=list)
    rejected_order_attempts: list[Exception] = field(default_factory=list)


class _RefDispatcher:
    """Reference dispatcher mirroring SRS-EXE-001 / SRS-SIM-001 / SRS-BT-001 contract.

    Concrete production dispatchers (live IB execution, internal paper
    simulation, backtest engine) must follow this shape: construct the
    SDK-shipped :class:`WarmupController`, invoke :meth:`run` before any
    executable bar is delivered, and gate every executable boundary on
    :func:`assert_warmup_complete`. Anything else risks delivering the
    first executable bar while indicator buffers are still empty.
    """

    def __init__(
        self,
        strategy: Strategy,
        context: _RefContext,
        history: _StubHistoricalData,
        subscriptions: list[tuple[str, AssetClass]],
    ) -> None:
        self.strategy = strategy
        self.context = context
        self.history = history
        self.subscriptions = list(subscriptions)
        self.controller = WarmupController(
            strategy=strategy,
            context=context,
            history=history,
            subscriptions=subscriptions,
        )
        self.delivered_executable_bars: list[Bar] = []

    def run_warmup(self) -> None:
        """Drive the warm-up replay through the SDK-shipped controller."""
        self.controller.run()

    def deliver_executable_bar(self, bar: Bar) -> None:
        """Gate executable delivery on WarmupState.COMPLETE."""
        assert_warmup_complete(self.controller.state)
        self.delivered_executable_bars.append(bar)
        self.strategy.on_bar(self.context, bar)

    def submit_order(self, order_id: str) -> None:
        """Gate user-initiated order submission on WarmupState.COMPLETE."""
        try:
            assert_warmup_complete(self.controller.state)
        except WarmupNotComplete as exc:
            self.context.rejected_order_attempts.append(exc)
            raise
        self.context.submitted_orders.append(order_id)


# --------------------------------------------------------------------------- #
# Helper builders
# --------------------------------------------------------------------------- #


def _make_dispatcher(
    *,
    warmup_bars: int = CANONICAL_WARMUP_BARS,
    symbol: str = "AAPL",
    extra_historical: int = 5,
) -> tuple[_RefStrategy, _RefContext, _StubHistoricalData, _RefDispatcher]:
    config = StrategyConfig(
        strategy_id="s1",
        tradable_asset_class=AssetClass.EQUITY,
        warmup_bars=warmup_bars,
    )
    strategy = _RefStrategy()
    history = _StubHistoricalData(
        bars_by_symbol={symbol: _historical_bars(symbol, warmup_bars + extra_historical)}
    )
    context = _RefContext(config=config)
    dispatcher = _RefDispatcher(
        strategy=strategy,
        context=context,
        history=history,
        subscriptions=[(symbol, AssetClass.EQUITY)],
    )
    return strategy, context, history, dispatcher


# --------------------------------------------------------------------------- #
# AC walks
# --------------------------------------------------------------------------- #


class WarmupOrderingTest(unittest.TestCase):
    """AC half-A: historical bars before first executable bar."""

    def test_200_historical_bars_replayed_before_first_executable_bar(self) -> None:
        strategy, _ctx, _history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        # Exactly 200 historical bars delivered through on_bar.
        self.assertEqual(len(strategy.received_bars), CANONICAL_WARMUP_BARS)
        # Now the first executable bar is allowed through.
        first_exec = _executable_bars("AAPL", 1)[0]
        dispatcher.deliver_executable_bar(first_exec)
        # The executable bar is the 201st bar; the first 200 are historical.
        self.assertEqual(len(strategy.received_bars), CANONICAL_WARMUP_BARS + 1)
        self.assertIs(strategy.received_bars[CANONICAL_WARMUP_BARS], first_exec)
        # The dispatcher's executable-bar log records the executable bar
        # AFTER the historical replay completes.
        self.assertEqual(dispatcher.delivered_executable_bars, [first_exec])

    def test_executable_bar_blocked_until_warmup_complete(self) -> None:
        _strategy, _ctx, _history, dispatcher = _make_dispatcher()
        first_exec = _executable_bars("AAPL", 1)[0]
        # PENDING state — executable delivery must raise.
        with self.assertRaises(WarmupNotComplete):
            dispatcher.deliver_executable_bar(first_exec)
        # After warm-up completes, the same executable bar flows through.
        dispatcher.run_warmup()
        dispatcher.deliver_executable_bar(first_exec)
        self.assertEqual(dispatcher.delivered_executable_bars, [first_exec])

    def test_order_submission_blocked_during_warmup(self) -> None:
        _strategy, context, _history, dispatcher = _make_dispatcher()
        with self.assertRaises(WarmupNotComplete):
            dispatcher.submit_order("ord-during-warmup")
        # The rejected attempt is recorded as a structured error per SyRS SYS-64.
        self.assertEqual(len(context.rejected_order_attempts), 1)
        self.assertIsInstance(context.rejected_order_attempts[0], WarmupNotComplete)
        self.assertEqual(context.submitted_orders, [])
        # After warm-up completes, order submission is allowed.
        dispatcher.run_warmup()
        dispatcher.submit_order("ord-after-warmup")
        self.assertEqual(context.submitted_orders, ["ord-after-warmup"])


class IndicatorBufferReadyTest(unittest.TestCase):
    """AC half-B: indicator buffers initialized at the warm-up boundary."""

    def test_sma200_is_ready_after_200_bar_warmup(self) -> None:
        strategy, _ctx, _history, dispatcher = _make_dispatcher()
        # PENDING: indicator empty.
        self.assertFalse(strategy.sma.is_ready)
        self.assertIsNone(strategy.sma.value)
        # Replay.
        dispatcher.run_warmup()
        # COMPLETE: indicator ready, value populated.
        self.assertTrue(strategy.sma.is_ready)
        self.assertIsNotNone(strategy.sma.value)
        # The indicator was made ready by the historical bars — the
        # "ready at index" recorded by the strategy is at or before the
        # last historical bar, i.e. before the first executable bar
        # can possibly arrive. This is the exact StRS SC-18 claim.
        self.assertIsNotNone(strategy.indicator_ready_at_index)
        assert strategy.indicator_ready_at_index is not None  # narrow for mypy
        self.assertLessEqual(strategy.indicator_ready_at_index, CANONICAL_WARMUP_BARS)
        # The value at the on_warmup_complete boundary is real (numeric).
        self.assertIsNotNone(strategy.indicator_value_at_complete)

    def test_first_executable_bar_sees_ready_indicator(self) -> None:
        strategy, _ctx, _history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        first_exec = _executable_bars("AAPL", 1)[0]
        # The strategy can rely on a ready indicator from the first
        # executable bar onwards — this is the SC-18 guarantee.
        self.assertTrue(strategy.sma.is_ready)
        dispatcher.deliver_executable_bar(first_exec)
        # The indicator updated with the executable bar; still ready.
        self.assertTrue(strategy.sma.is_ready)


class WarmupCompleteCallbackTest(unittest.TestCase):
    """The on_warmup_complete callback fires exactly once at the boundary."""

    def test_on_warmup_complete_fires_exactly_once(self) -> None:
        strategy, _ctx, _history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        self.assertEqual(strategy.warmup_complete_calls, 1)
        self.assertEqual(strategy.warmup_complete_at_index, CANONICAL_WARMUP_BARS)

    def test_on_warmup_complete_does_not_refire_on_more_bars(self) -> None:
        strategy, _ctx, _history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        for bar in _executable_bars("AAPL", 5):
            dispatcher.deliver_executable_bar(bar)
        # The callback still fired exactly once, despite further on_bar invocations.
        self.assertEqual(strategy.warmup_complete_calls, 1)

    def test_controller_run_on_complete_raises(self) -> None:
        _strategy, _ctx, _history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        # A second run on the same controller is a programming error —
        # restart re-execution (NFR-R3) constructs a fresh controller.
        with self.assertRaises(RuntimeError):
            dispatcher.controller.run()


class LiveAndPaperParityTest(unittest.TestCase):
    """SRS-SDK-001 AC-14: the same SDK surface drives live and paper modes."""

    def test_two_dispatchers_share_the_same_controller_contract(self) -> None:
        # Construct two independent dispatchers — modelling one live and
        # one paper container — and verify they walk identical warm-up
        # state through the same SDK surface (no execution-mode branching).
        live_strat, _ctx_l, _hist_l, live_dispatcher = _make_dispatcher()
        paper_strat, _ctx_p, _hist_p, paper_dispatcher = _make_dispatcher()
        live_dispatcher.run_warmup()
        paper_dispatcher.run_warmup()
        self.assertIs(live_dispatcher.controller.state, WarmupState.COMPLETE)
        self.assertIs(paper_dispatcher.controller.state, WarmupState.COMPLETE)
        self.assertEqual(len(live_strat.received_bars), CANONICAL_WARMUP_BARS)
        self.assertEqual(len(paper_strat.received_bars), CANONICAL_WARMUP_BARS)
        # SMA(200) is_ready on both paths after the same 200-bar replay.
        self.assertTrue(live_strat.sma.is_ready)
        self.assertTrue(paper_strat.sma.is_ready)


class RestartReExecutionTest(unittest.TestCase):
    """SyRS NFR-R3: the warm-up mechanism is re-executed on restart."""

    def test_fresh_controller_re_walks_state_on_restart(self) -> None:
        strategy, ctx, history, dispatcher = _make_dispatcher()
        dispatcher.run_warmup()
        self.assertIs(dispatcher.controller.state, WarmupState.COMPLETE)
        bars_pre_restart = len(strategy.received_bars)

        # Simulate restart: a fresh controller is constructed; the
        # existing strategy + history + context are restored from
        # NFR-R3 persistence (modelled here by passing the same objects).
        restart_controller = WarmupController(
            strategy=strategy,
            context=ctx,
            history=history,
            subscriptions=[("AAPL", AssetClass.EQUITY)],
        )
        # Restart controller starts PENDING — the executable gate is
        # closed again while warm-up re-runs.
        self.assertIs(restart_controller.state, WarmupState.PENDING)
        with self.assertRaises(WarmupNotComplete):
            assert_warmup_complete(restart_controller.state)
        # Re-run replays the same 200 historical bars.
        restart_controller.run()
        self.assertIs(restart_controller.state, WarmupState.COMPLETE)
        # The strategy saw another 200 bars from the re-execution
        # (NFR-R3 reconstructs indicator buffers and rolling windows).
        self.assertEqual(len(strategy.received_bars), bars_pre_restart + CANONICAL_WARMUP_BARS)


class ShortHistoryFailClosedTest(unittest.TestCase):
    """A short historical replay must keep the executable gate closed."""

    def test_short_history_raises_and_keeps_gate_closed(self) -> None:
        # Construct the dispatcher exactly as a production live/paper/
        # backtest impl would; then have the stub history return one
        # bar fewer than warmup_bars. The controller must refuse to
        # open the executable gate — the AC requires indicator buffers
        # to be initialised before the first executable bar.
        strategy, _ctx, history, dispatcher = _make_dispatcher(
            warmup_bars=CANONICAL_WARMUP_BARS, extra_historical=0
        )
        # Shorten the history to N-1 bars.
        history.bars_by_symbol["AAPL"] = history.bars_by_symbol["AAPL"][: CANONICAL_WARMUP_BARS - 1]
        with self.assertRaises(WarmupNotComplete):
            dispatcher.run_warmup()
        # The state stays IN_PROGRESS so the executable gate stays closed.
        self.assertIs(dispatcher.controller.state, WarmupState.IN_PROGRESS)
        with self.assertRaises(WarmupNotComplete):
            assert_warmup_complete(dispatcher.controller.state)
        # on_warmup_complete must NOT have fired — production
        # dispatchers gate trading on the callback firing.
        self.assertEqual(strategy.warmup_complete_calls, 0)
        # An executable bar attempt is also refused.
        first_exec = _executable_bars("AAPL", 1)[0]
        with self.assertRaises(WarmupNotComplete):
            dispatcher.deliver_executable_bar(first_exec)
        # An order submission attempt is also refused.
        with self.assertRaises(WarmupNotComplete):
            dispatcher.submit_order("ord-after-short-warmup")

    def test_short_history_on_one_of_many_subscriptions_still_blocks(self) -> None:
        # Two subscriptions: AAPL has full warmup_bars, MSFT is short.
        # The controller must still refuse to open the gate.
        warmup_bars = CANONICAL_WARMUP_BARS
        config = StrategyConfig(
            strategy_id="s1",
            tradable_asset_class=AssetClass.EQUITY,
            warmup_bars=warmup_bars,
        )
        strategy = _RefStrategy()
        history = _StubHistoricalData(
            bars_by_symbol={
                "AAPL": _historical_bars("AAPL", warmup_bars),
                "MSFT": _historical_bars("MSFT", warmup_bars - 1),
            }
        )
        context = _RefContext(config=config)
        dispatcher = _RefDispatcher(
            strategy=strategy,
            context=context,
            history=history,
            subscriptions=[
                ("AAPL", AssetClass.EQUITY),
                ("MSFT", AssetClass.EQUITY),
            ],
        )
        with self.assertRaises(WarmupNotComplete):
            dispatcher.run_warmup()
        self.assertIs(dispatcher.controller.state, WarmupState.IN_PROGRESS)
        self.assertEqual(strategy.warmup_complete_calls, 0)


class ZeroWarmupBarsTest(unittest.TestCase):
    """A strategy declaring no warm-up still walks the lifecycle once."""

    def test_zero_warmup_still_fires_callback_and_opens_gate(self) -> None:
        strategy, _ctx, history, dispatcher = _make_dispatcher(warmup_bars=0, extra_historical=0)
        # Empty historical to make the no-op explicit.
        history.bars_by_symbol["AAPL"] = []
        dispatcher.run_warmup()
        self.assertIs(dispatcher.controller.state, WarmupState.COMPLETE)
        self.assertEqual(strategy.warmup_complete_calls, 1)
        self.assertEqual(strategy.received_bars, [])
        # Executable bars now flow.
        first_exec = _executable_bars("AAPL", 1)[0]
        dispatcher.deliver_executable_bar(first_exec)
        self.assertEqual(strategy.received_bars, [first_exec])


if __name__ == "__main__":
    unittest.main()
