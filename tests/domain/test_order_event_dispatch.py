"""SRS-SDK-004 / SyRS SYS-7, SYS-85, NFR-P4 — order event callback dispatch.

L7 domain (safety) test. Walks the full SRS-SDK-004 AC end-to-end
against a concrete ``_RefDispatcher`` reference impl whose dispatch
path calls the SDK-shipped ``assert_order_event_payload`` guard
before invoking ``Strategy.on_order_event`` (no shadow comparison).
Locks:

* All four AC-named lifecycle event categories (FILL, PARTIAL_FILL,
  CANCELLED, REJECTED) are deliverable end-to-end through the
  dispatcher, and user strategy code receives them with the
  documented field set populated.
* The dispatcher delegates field-presence enforcement to the SDK
  helper rather than reimplementing it — a regression in production
  drivers would silently drift the contract.
* Malformed events raise ``OrderEventContractError`` at the dispatch
  boundary; the user callback is not invoked on a bad payload.
* The SDK-published latency-budget constants
  ``LIVE_CALLBACK_LATENCY_P95_MS`` and ``PAPER_CALLBACK_LATENCY_P95_MS``
  are read by the dispatcher (single source of truth per NFR-P4),
  and a synthetic in-process timed dispatch fits well under the
  paper budget — a deterministic floor proving the SDK reference
  pattern composes into a measurable latency check.
"""

from __future__ import annotations

import statistics
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_strategy as api  # noqa: E402
import pytest  # noqa: E402
from atp_strategy import (  # noqa: E402
    LIVE_CALLBACK_LATENCY_P95_MS,
    PAPER_CALLBACK_LATENCY_P95_MS,
    OrderEvent,
    OrderEventContractError,
    OrderEventType,
    Strategy,
    assert_order_event_payload,
)

pytestmark = [pytest.mark.domain, pytest.mark.safety]


@dataclass
class _RefStrategy(Strategy):
    """Minimal Strategy subclass recording every delivered event."""

    received: list[OrderEvent] = field(default_factory=list)
    callback_started_at: list[float] = field(default_factory=list)
    callback_ended_at: list[float] = field(default_factory=list)

    def on_order_event(self, context, event: OrderEvent) -> None:  # type: ignore[override]
        # Record arrival timing for the synthetic latency proof.
        self.callback_started_at.append(time.perf_counter())
        self.received.append(event)
        self.callback_ended_at.append(time.perf_counter())


class _RefDispatcher:
    """Reference dispatch path mirroring SRS-EXE-001 / SRS-SIM-001 contract.

    Concrete production dispatchers (live IB execution, internal paper
    simulation) must follow the same shape: validate the payload via
    the SDK-shipped guard, then invoke the user callback. Anything
    else risks silently dropping AC-required fields or skipping the
    structured-error contract.
    """

    def __init__(self, strategy: _RefStrategy, *, context: object = None) -> None:
        self.strategy = strategy
        self.context = context
        self.dispatch_latencies_ms: list[float] = []
        self.rejected_events: list[tuple[OrderEvent, OrderEventContractError]] = []

    def dispatch(self, event: OrderEvent, *, simulated_fill_at: float | None = None) -> None:
        """Validate the event then invoke the strategy callback."""
        # Field-presence guard — production drivers must call this
        # helper rather than reimplementing it.
        try:
            assert_order_event_payload(event)
        except OrderEventContractError as exc:
            self.rejected_events.append((event, exc))
            raise

        # Optional latency observation: caller-supplied `simulated_fill_at`
        # is the t0 the SyRS NFR-P4 budget is measured against. For
        # internal paper simulation this is the simulator's fill stamp;
        # for live IB execution this is the broker fill acknowledgement
        # stamp.
        if simulated_fill_at is not None:
            self.strategy.on_order_event(self.context, event)
            elapsed_ms = (time.perf_counter() - simulated_fill_at) * 1000.0
            self.dispatch_latencies_ms.append(elapsed_ms)
        else:
            self.strategy.on_order_event(self.context, event)


def _fill_event(order_id: str = "ord-1") -> OrderEvent:
    return OrderEvent(
        event_type=OrderEventType.FILL,
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price=100.0,
        fill_quantity=10,
        cumulative_filled=10,
        remaining_quantity=0,
        commission=0.05,
        reason=None,
        timestamp="2026-05-03T13:30:00Z",
    )


def _partial_fill_event(order_id: str = "ord-2") -> OrderEvent:
    return OrderEvent(
        event_type=OrderEventType.PARTIAL_FILL,
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price=100.0,
        fill_quantity=4,
        cumulative_filled=4,
        remaining_quantity=6,
        commission=0.02,
        reason=None,
        timestamp="2026-05-03T13:30:01Z",
    )


def _cancelled_event(order_id: str = "ord-3") -> OrderEvent:
    # Never-filled cancellation — per SRS-SDK-004 AC the four named
    # callback categories include fill_price / fill_quantity /
    # commission; dispatchers populate explicit zeros on a never-
    # filled cancel.
    return OrderEvent(
        event_type=OrderEventType.CANCELLED,
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price=0.0,
        fill_quantity=0,
        cumulative_filled=0,
        remaining_quantity=10,
        commission=0.0,
        reason="user requested",
        timestamp="2026-05-03T13:30:02Z",
    )


def _partially_filled_cancelled_event(order_id: str = "ord-3p") -> OrderEvent:
    # Partially-filled cancellation — fill_price / fill_quantity /
    # commission report the cumulative state at the terminal event.
    return OrderEvent(
        event_type=OrderEventType.CANCELLED,
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price=99.5,
        fill_quantity=4,
        cumulative_filled=4,
        remaining_quantity=6,
        commission=0.02,
        reason="user requested",
        timestamp="2026-05-03T13:30:02Z",
    )


def _rejected_event(order_id: str = "ord-4") -> OrderEvent:
    return OrderEvent(
        event_type=OrderEventType.REJECTED,
        order_id=order_id,
        client_order_id=f"cli-{order_id}",
        strategy_id="s1",
        symbol="AAPL",
        fill_price=0.0,
        fill_quantity=0,
        cumulative_filled=0,
        remaining_quantity=10,
        commission=0.0,
        reason="insufficient buying power",
        timestamp="2026-05-03T13:30:03Z",
    )


class FourCategoryDeliveryTest(unittest.TestCase):
    """SRS-SDK-004 AC: FILL, PARTIAL_FILL, CANCELLED, REJECTED all deliver."""

    def test_fill_event_reaches_user_callback_with_required_fields(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        dispatcher.dispatch(_fill_event())
        self.assertEqual(len(strat.received), 1)
        delivered = strat.received[0]
        self.assertIs(delivered.event_type, OrderEventType.FILL)
        self.assertEqual(delivered.fill_price, 100.0)
        self.assertEqual(delivered.fill_quantity, 10)
        self.assertEqual(delivered.commission, 0.05)
        self.assertEqual(delivered.order_id, "ord-1")
        self.assertEqual(delivered.client_order_id, "cli-ord-1")

    def test_partial_fill_event_reaches_user_callback(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        dispatcher.dispatch(_partial_fill_event())
        self.assertEqual(len(strat.received), 1)
        delivered = strat.received[0]
        self.assertIs(delivered.event_type, OrderEventType.PARTIAL_FILL)
        self.assertEqual(delivered.fill_quantity, 4)
        self.assertEqual(delivered.remaining_quantity, 6)
        self.assertEqual(delivered.commission, 0.02)

    def test_cancelled_event_reaches_user_callback_with_required_fields(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        dispatcher.dispatch(_cancelled_event())
        self.assertEqual(len(strat.received), 1)
        delivered = strat.received[0]
        self.assertIs(delivered.event_type, OrderEventType.CANCELLED)
        self.assertEqual(delivered.reason, "user requested")
        # Per the AC, CANCELLED events include fill_price /
        # fill_quantity / commission. On a never-filled cancel
        # the dispatcher reports explicit zeros.
        self.assertEqual(delivered.fill_price, 0.0)
        self.assertEqual(delivered.fill_quantity, 0)
        self.assertEqual(delivered.commission, 0.0)
        self.assertEqual(delivered.order_id, "ord-3")

    def test_partially_filled_cancellation_reports_cumulative_state(self) -> None:
        # Partially-filled cancel — fill_price / fill_quantity /
        # commission carry the cumulative average / total at the
        # terminal event so user code reconciles position without
        # out-of-band lookups (AC-named field-presence + SyRS SYS-7
        # delivery).
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        dispatcher.dispatch(_partially_filled_cancelled_event())
        self.assertEqual(len(strat.received), 1)
        delivered = strat.received[0]
        self.assertIs(delivered.event_type, OrderEventType.CANCELLED)
        self.assertEqual(delivered.fill_price, 99.5)
        self.assertEqual(delivered.fill_quantity, 4)
        self.assertEqual(delivered.cumulative_filled, 4)
        self.assertEqual(delivered.remaining_quantity, 6)
        self.assertEqual(delivered.commission, 0.02)

    def test_rejected_event_reaches_user_callback_with_required_fields(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        dispatcher.dispatch(_rejected_event())
        self.assertEqual(len(strat.received), 1)
        delivered = strat.received[0]
        self.assertIs(delivered.event_type, OrderEventType.REJECTED)
        self.assertEqual(delivered.reason, "insufficient buying power")
        # Per the AC, REJECTED events include fill_price / fill_quantity
        # / commission. On a rejected (never-filled) order the
        # dispatcher reports zeros.
        self.assertEqual(delivered.fill_price, 0.0)
        self.assertEqual(delivered.fill_quantity, 0)
        self.assertEqual(delivered.commission, 0.0)

    def test_full_lifecycle_sequence_is_ordered(self) -> None:
        """A representative ack -> partial-fill -> fill sequence preserves order."""
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        partial = _partial_fill_event(order_id="ord-9")
        complete = OrderEvent(
            event_type=OrderEventType.FILL,
            order_id="ord-9",
            client_order_id="cli-ord-9",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=100.1,
            fill_quantity=6,
            cumulative_filled=10,
            remaining_quantity=0,
            commission=0.03,
            reason=None,
            timestamp="2026-05-03T13:30:05Z",
        )
        dispatcher.dispatch(partial)
        dispatcher.dispatch(complete)
        self.assertEqual(
            [ev.event_type for ev in strat.received],
            [OrderEventType.PARTIAL_FILL, OrderEventType.FILL],
        )
        self.assertEqual(strat.received[1].cumulative_filled, 10)
        self.assertEqual(strat.received[1].remaining_quantity, 0)


class GuardEnforcedAtDispatchTest(unittest.TestCase):
    """Production dispatchers must call the SDK guard before user code."""

    def test_malformed_fill_does_not_reach_user_callback(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.FILL,
            order_id="ord-5",
            client_order_id="cli-5",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=None,  # missing per SRS-SDK-004 AC
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission=0.05,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            dispatcher.dispatch(bad)
        self.assertEqual(strat.received, [], "user callback reached on a malformed FILL")
        self.assertEqual(len(dispatcher.rejected_events), 1)

    def test_malformed_cancellation_does_not_reach_user_callback(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.CANCELLED,
            order_id="ord-6",
            client_order_id="cli-6",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=0.0,
            fill_quantity=0,
            cumulative_filled=0,
            remaining_quantity=10,
            commission=0.0,
            reason=None,  # missing per SRS-SDK-004 AC
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError):
            dispatcher.dispatch(bad)
        self.assertEqual(strat.received, [], "user callback reached on a malformed CANCELLED")

    def test_cancellation_with_none_fill_price_does_not_reach_user_callback(self) -> None:
        # Per the AC, CANCELLED includes fill_price; dispatchers must
        # populate 0.0 on a never-filled cancel, not None.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.CANCELLED,
            order_id="ord-6b",
            client_order_id="cli-6b",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=None,  # violates AC for CANCELLED
            fill_quantity=0,
            cumulative_filled=0,
            remaining_quantity=10,
            commission=0.0,
            reason="user requested",
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("fill_price", str(ctx.exception))
        self.assertIn("CANCELLED", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_none_payload_does_not_reach_user_callback(self) -> None:
        # A None payload from the Rust/Python boundary must surface a
        # structured OrderEventContractError, not AttributeError.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(None)  # type: ignore[arg-type]
        self.assertIn("payload is not an OrderEvent", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_dict_payload_does_not_reach_user_callback(self) -> None:
        # Unconverted JSON / dict at the boundary — also a structured
        # error, not AttributeError.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(  # type: ignore[arg-type]
                {"event_type": "FILL", "order_id": "ord-1"}
            )
        self.assertIn("payload is not an OrderEvent", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_invalid_event_type_string_does_not_reach_user_callback(self) -> None:
        # OrderEventType is a StrEnum, so a bare-string event_type
        # like "FILL" would equality-match in the AC branches and
        # crash on .value access. The dispatch boundary must reject
        # it with a structured OrderEventContractError.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type="FILL",  # type: ignore[arg-type]
            order_id="ord-bare",
            client_order_id="cli-bare",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=100.0,
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission=0.05,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("invalid event_type", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_partial_fill_with_zero_remaining_does_not_reach_user_callback(self) -> None:
        # Lifecycle consistency: a PARTIAL_FILL that leaves nothing
        # working is actually a final FILL. The mislabel would
        # corrupt user order-state machines if it slipped past.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.PARTIAL_FILL,
            order_id="ord-lc1",
            client_order_id="cli-lc1",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=100.0,
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,  # contradicts PARTIAL_FILL semantics
            commission=0.05,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("PARTIAL_FILL", str(ctx.exception))
        self.assertIn("remaining_quantity=0", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_terminal_with_inconsistent_cumulative_state_does_not_reach_user(self) -> None:
        # Terminal cumulative-state invariant: CANCELLED.fill_quantity
        # must equal CANCELLED.cumulative_filled. A mismatch would
        # silently corrupt user P&L reconciliation if it slipped past
        # the dispatch boundary.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.CANCELLED,
            order_id="ord-lc3",
            client_order_id="cli-lc3",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=99.5,
            fill_quantity=4,  # claims 4 filled this event
            cumulative_filled=0,  # but cumulative says 0 — contradiction
            remaining_quantity=10,
            commission=0.02,
            reason="user requested",
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("inconsistent cumulative state", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_fill_with_remaining_does_not_reach_user_callback(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.FILL,
            order_id="ord-lc2",
            client_order_id="cli-lc2",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=100.0,
            fill_quantity=4,
            cumulative_filled=4,
            remaining_quantity=6,  # contradicts FILL (terminal) semantics
            commission=0.02,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("FILL event", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_unknown_event_type_does_not_silently_reach_user_callback(self) -> None:
        # An unknown string event_type would fall through every `in`
        # branch silently without the discriminant check — the worst
        # case where bad payload reaches user code with no signal.
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type="UNKNOWN",  # type: ignore[arg-type]
            order_id="ord-unk",
            client_order_id="cli-unk",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=None,
            fill_quantity=None,
            cumulative_filled=0,
            remaining_quantity=0,
            commission=None,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("UNKNOWN", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_rejection_with_none_commission_does_not_reach_user_callback(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.REJECTED,
            order_id="ord-6c",
            client_order_id="cli-6c",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=0.0,
            fill_quantity=0,
            cumulative_filled=0,
            remaining_quantity=10,
            commission=None,  # violates AC for REJECTED
            reason="insufficient buying power",
            timestamp="2026-05-03T13:30:00Z",
        )
        with self.assertRaises(OrderEventContractError) as ctx:
            dispatcher.dispatch(bad)
        self.assertIn("commission", str(ctx.exception))
        self.assertEqual(strat.received, [])

    def test_dispatcher_delegates_to_shipped_guard(self) -> None:
        """Replaces the SDK guard with a sentinel and asserts the dispatcher
        invokes it — locks the rule that production drivers must
        delegate to the SDK helper, not reimplement the comparison.
        """

        sentinel_calls: list[OrderEvent] = []

        def sentinel_guard(event: OrderEvent) -> None:
            sentinel_calls.append(event)
            # No raise — sentinel approves everything so we can confirm
            # the call site is reached even on the well-formed path.

        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        # The dispatcher reads the guard via its module-level import in
        # this file, so patch _this_ module's attribute (not the api
        # module's) — that's the call site the reference impl uses.
        with mock.patch(f"{_RefDispatcher.__module__}.assert_order_event_payload", sentinel_guard):
            dispatcher.dispatch(_fill_event())

        self.assertEqual(len(sentinel_calls), 1)
        self.assertIs(sentinel_calls[0].event_type, OrderEventType.FILL)


class LatencyBudgetReferenceTest(unittest.TestCase):
    """SyRS NFR-P4: SDK budgets are the single source of truth.

    The end-to-end live and paper latency proofs (against IB Gateway
    and the internal simulation engine respectively) are owned by
    SRS-EXE-001 / SRS-SIM-001. What we lock here is the SDK reference
    pattern: dispatchers read ``LIVE_/PAPER_CALLBACK_LATENCY_P95_MS``
    from the SDK, and a synthetic in-process dispatch fits well under
    the paper budget so the published number is internally consistent.
    """

    def test_published_budgets_match_nfr_p4(self) -> None:
        self.assertEqual(LIVE_CALLBACK_LATENCY_P95_MS, 1000)
        self.assertEqual(PAPER_CALLBACK_LATENCY_P95_MS, 100)
        # Live must be looser than paper — broker round-trip dominates.
        self.assertGreater(LIVE_CALLBACK_LATENCY_P95_MS, PAPER_CALLBACK_LATENCY_P95_MS)

    def test_synthetic_dispatch_fits_under_paper_budget(self) -> None:
        """A deterministic floor on the SDK reference pattern: dispatching
        a series of well-formed FILL events through ``_RefDispatcher``
        in-process and computing the p95 callback latency must come in
        well under ``PAPER_CALLBACK_LATENCY_P95_MS``. This is not the
        SRS-SIM-001 end-to-end proof (which requires a real simulation
        engine) — it is a sanity test that the SDK-shipped budget
        constant is consistent with the SDK-shipped dispatch shape on
        present-day hardware. A real simulation engine adds at most a
        few-ms scheduler hop on top of this floor.
        """
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        sample_size = 200
        for i in range(sample_size):
            t0 = time.perf_counter()
            dispatcher.dispatch(_fill_event(order_id=f"ord-{i}"), simulated_fill_at=t0)

        self.assertEqual(len(dispatcher.dispatch_latencies_ms), sample_size)
        latencies = sorted(dispatcher.dispatch_latencies_ms)
        p95_idx = max(0, int(round(0.95 * (sample_size - 1))))
        p95_ms = latencies[p95_idx]
        median_ms = statistics.median(latencies)
        # An in-process dispatch is well under 1 ms on present-day
        # hardware; comparing against PAPER_CALLBACK_LATENCY_P95_MS
        # (100 ms) gives plenty of headroom and avoids brittleness
        # under loaded CI runners. We are intentionally NOT proving
        # the SyRS NFR-P4 paper p95 here — that proof requires
        # SRS-SIM-001's simulation engine and is gated by
        # ATP_RUN_INTEGRATION per repo convention.
        self.assertLess(
            p95_ms,
            PAPER_CALLBACK_LATENCY_P95_MS,
            f"in-process p95={p95_ms:.3f} ms exceeded paper budget "
            f"{PAPER_CALLBACK_LATENCY_P95_MS} ms; SDK reference pattern "
            "is no longer consistent with the published budget — "
            f"median was {median_ms:.3f} ms",
        )


class StructuredErrorContractTest(unittest.TestCase):
    """SyRS SYS-64: violation reaches user code through StrategyAPIError."""

    def test_violation_is_catchable_via_strategy_api_error(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        bad = OrderEvent(
            event_type=OrderEventType.FILL,
            order_id="ord-7",
            client_order_id="cli-7",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=None,
            fill_quantity=10,
            cumulative_filled=10,
            remaining_quantity=0,
            commission=0.05,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        # User code may catch the base class and route on the structured
        # message — SyRS SYS-64.
        with self.assertRaises(api.StrategyAPIError):
            dispatcher.dispatch(bad)

    def test_violation_message_names_event_type_and_field(self) -> None:
        strat = _RefStrategy()
        dispatcher = _RefDispatcher(strat)
        # Fill the AC-required fields with zeros so the helper reaches
        # the reason-presence check; the missing reason is what we lock
        # the structured-error contract on here.
        bad = OrderEvent(
            event_type=OrderEventType.REJECTED,
            order_id="alpha-momentum-7",
            client_order_id="cli-8",
            strategy_id="s1",
            symbol="AAPL",
            fill_price=0.0,
            fill_quantity=0,
            cumulative_filled=0,
            remaining_quantity=10,
            commission=0.0,
            reason=None,
            timestamp="2026-05-03T13:30:00Z",
        )
        try:
            dispatcher.dispatch(bad)
        except OrderEventContractError as exc:
            message = str(exc)
            self.assertIn("alpha-momentum-7", message)
            self.assertIn("REJECTED", message)
            self.assertIn("reason", message)
        else:
            self.fail("OrderEventContractError was not raised")


if __name__ == "__main__":
    unittest.main()
