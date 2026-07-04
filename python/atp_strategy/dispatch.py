"""Order-event callback delivery seam for Python strategy code (SRS-SDK-004).

SRS-SDK-004 ("The software shall deliver order event callbacks to Python
strategy code") requires the platform to surface FILL / PARTIAL_FILL /
CANCELLED / REJECTED callbacks to a strategy's :meth:`Strategy.on_order_event`,
carrying **fill price, fill quantity, commission, and order identifiers**, with
live callback delivery < 1000 ms p95 from broker fill acknowledgement and paper
callback delivery < 100 ms p95 from simulated fill (SyRS SYS-7, SYS-85, NFR-P4;
StRS SN-1.22 / SN-1.29).

This module is the **reusable Python delivery seam** that the architecture
metadata (``architecture/runtime_services.json`` block
``strategy_api_order_events_contract``) names when it says *"production
dispatchers re-use the SDK helper and budget constants."* Until this module, the
only shared SDK helper was :func:`assert_order_event_payload` (the field-presence
guard) and the only end-to-end dispatch path was the *test-only* ``_RefDispatcher``
in ``tests/domain/test_order_event_dispatch.py``. This module promotes that
reference pattern into a real, importable production helper that both concrete
dispatchers reuse:

  * **Internal paper simulation** (SRS-SIM-001 / SRS-SIM-002): the simulation
    engine produces a fill (``FillDecision`` + ``CostBreakdown`` + virtual-ledger
    mutation, all in integer minor units), emits it as a :class:`SimulatedFill`
    boundary descriptor, and the paper dispatcher calls
    :func:`deliver_simulated_fill`.
  * **Live IB execution** (SRS-EXE-001 / SRS-EXE-006): the IB adapter's order
    events (ACK / execDetails / commissionReport / orderStatus) are assembled
    into an :class:`OrderEvent` and the live dispatcher calls
    :func:`deliver_order_event` (the shared guard→invoke→sample seam).

**Ownership / dependency direction.** This seam is Python (the Strategy API is
Python per AGENTS.md) and lives in a submodule imported explicitly by the host /
dispatcher — like ``atp_strategy.store_history`` it is a runtime binding, not a
strategy-authoring primitive, so it is intentionally NOT re-exported on the
author-facing ``atp_strategy`` surface. It consumes (never rebuilds) the Rust
simulation engine's fill output via the :class:`SimulatedFill` DTO; the Rust core
never imports this SDK (the boundary is a data descriptor, not an FFI call).

**What this module is NOT.** It does not model the *engine-inclusive* end-to-end
paper latency (the simulation engine's own fill-production time is SRS-SIM's), it
does not talk to IB, and it does not itself claim a PTP-disciplined NFR-P4
verification artifact — the p95 measurement over a PTP-disciplined host is the
SRS-PERF-001 substrate's job and is deferred / serialized. What it owns is the
**SDK delivery-seam**: descriptor → :class:`OrderEvent` → fail-closed guard →
:meth:`Strategy.on_order_event`, and a monotonic latency sample for that seam.

Category derivation is NOT re-implemented here. The Rust source-neutral authority
(``atp_types::order_event::OrderEventCategory``) derives which callback category a
lifecycle transition emits, fail-closed against the SRS-EXE-008 state graph; the
engine that owns the transition stamps the resulting :class:`OrderEventType` into
the descriptor. This seam trusts that discriminant and enforces the field-presence
contract via :func:`assert_order_event_payload` before any user code runs — so a
malformed or fabricated payload never reaches a strategy.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .api import (
    OrderEvent,
    OrderEventContractError,
    OrderEventType,
    assert_order_event_payload,
)

__all__ = [
    "MINOR_UNITS_PER_UNIT",
    "SimulatedFill",
    "build_order_event",
    "deliver_order_event",
    "deliver_simulated_fill",
]

# Integer minor units per currency unit — cents per dollar. The Rust simulation
# engine computes every money value in **integer minor units** (see
# ``crates/atp-simulation/src/cost.rs``: "Every cost is computed in integer minor
# units (cents)"; ``IB_TIERED_MIN_PER_ORDER_MINOR = 35`` == $0.35), which keeps
# commission / price math exact. The Python ``OrderEvent`` surface carries
# ``fill_price`` / ``commission`` as ``float`` (the documented SDK schema), so the
# seam converts minor units → units at the boundary using this single scale. The
# canonical *exact* money stays the integer minor units on the descriptor; the
# float is the SDK-facing view (round-trips to the nearest minor unit — see
# :func:`build_order_event`).
MINOR_UNITS_PER_UNIT = 100


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """The internal-simulation-engine → SDK order-event boundary descriptor.

    This is the data the internal simulation engine (SRS-SIM-001 / SRS-SIM-002)
    hands to the paper dispatcher for one order-lifecycle event. It mirrors the
    Rust-side field homes so the boundary is a plain data transfer object, not an
    FFI call (per AGENTS.md the Rust core must not depend on this Python SDK):

      * ``event_type`` — the :class:`OrderEventType` the engine derived for this
        transition via the Rust ``OrderEventCategory`` authority (this seam does
        NOT re-derive it).
      * ``sim_order_id`` — the simulator's order id (``SimulatedOrderReceipt``);
        becomes the strategy-facing ``OrderEvent.order_id``.
      * ``client_order_id`` — the strategy-supplied correlation id.
      * ``fill_price_minor`` / ``commission_minor`` — integer minor units (cents)
        from the fill model (``FillDecision.fill_price_minor``) and cost model
        (``CostBreakdown.commission_minor``). ``None`` only on ACK / EXPIRED.
      * ``fill_quantity`` / ``cumulative_filled`` / ``remaining_quantity`` — share
        counts (already integers on both sides).
      * ``reason`` — engine-supplied reason string (required for CANCELLED /
        REJECTED / EXPIRED).
      * ``simulated_fill_at_ns`` — the engine's monotonic timestamp (``time.
        perf_counter_ns``-domain) at which the simulated fill was produced. This
        is the ``t0`` the SyRS NFR-P4 *paper* budget is measured from.
      * ``timestamp`` — ISO-8601 wall-clock stamp of the event for user code.
    """

    event_type: OrderEventType
    sim_order_id: str
    client_order_id: str
    strategy_id: str
    symbol: str
    fill_price_minor: int | None
    fill_quantity: int | None
    cumulative_filled: int
    remaining_quantity: int
    commission_minor: int | None
    reason: str | None
    simulated_fill_at_ns: int
    timestamp: str


def _minor_to_units(field_name: str, order_id: str, minor: int | None) -> float | None:
    """Convert integer minor units → float currency units, fail-closed on shape.

    ``None`` passes through (permitted on ACK / EXPIRED). A non-``int`` (or a
    ``bool``, which is an ``int`` subclass) minor value is a boundary-schema bug
    and surfaces as a structured :class:`OrderEventContractError` rather than a
    silently-wrong float — the same fail-closed posture the field-presence guard
    takes at the dispatch boundary.

    A **negative** minor value is also rejected here. Fill price and commission are
    both non-negative by construction (a fill price is > 0, a never-filled
    cancel/reject reports 0.0, and commission is a cost floored at >= 0 by the IB
    tiered model). The downstream :func:`assert_order_event_payload` guard rejects a
    negative ``fill_price`` but only checks that ``commission`` is *finite* — so
    without this check a negative ``commission_minor`` descriptor would be delivered
    to user code as a negative fee, corrupting P&L / reconciliation instead of
    failing closed.
    """
    if minor is None:
        return None
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise OrderEventContractError(
            f"SimulatedFill {order_id!r} has invalid {field_name}: expected int "
            f"minor units, got {type(minor).__name__}={minor!r}"
        )
    if minor < 0:
        raise OrderEventContractError(
            f"SimulatedFill {order_id!r} has invalid {field_name}: expected "
            f"non-negative minor units, got {minor}"
        )
    return minor / MINOR_UNITS_PER_UNIT


def build_order_event(fill: SimulatedFill) -> OrderEvent:
    """Assemble the strategy-facing :class:`OrderEvent` from a :class:`SimulatedFill`.

    Maps the boundary descriptor onto the SDK payload: converts money fields from
    integer minor units to float currency units (:data:`MINOR_UNITS_PER_UNIT`),
    carries the engine-derived ``event_type`` through unchanged, and forwards the
    order identifiers and share counts. The float money view round-trips to the
    nearest minor unit: ``round(event.fill_price * MINOR_UNITS_PER_UNIT) ==
    fill.fill_price_minor`` (and likewise for commission).

    This function does NOT validate field presence / lifecycle consistency — that
    is :func:`assert_order_event_payload`'s job, applied by
    :func:`deliver_order_event` immediately before delivery so there is a single
    guard authority. It DOES fail closed on a descriptor whose shape can't be
    mapped at all (not a :class:`SimulatedFill`, a non-:class:`OrderEventType`
    discriminant, or non-int minor money), so a schema-drifted descriptor can
    never silently become a malformed event.

    Raises:
        OrderEventContractError: ``fill`` is not a :class:`SimulatedFill`, carries
            a non-:class:`OrderEventType` ``event_type``, or has non-int minor
            money fields.
    """
    if not isinstance(fill, SimulatedFill):
        raise OrderEventContractError(
            "build_order_event requires a SimulatedFill descriptor (got "
            f"{type(fill).__name__}); the paper dispatcher must construct "
            "SimulatedFill instances rather than dicts or schema-drifted objects"
        )
    order_id = fill.sim_order_id
    if not isinstance(fill.event_type, OrderEventType):
        raise OrderEventContractError(
            f"SimulatedFill {order_id!r} has invalid event_type "
            f"{fill.event_type!r} (expected OrderEventType, got "
            f"{type(fill.event_type).__name__})"
        )
    return OrderEvent(
        event_type=fill.event_type,
        order_id=order_id,
        client_order_id=fill.client_order_id,
        strategy_id=fill.strategy_id,
        symbol=fill.symbol,
        fill_price=_minor_to_units("fill_price_minor", order_id, fill.fill_price_minor),
        fill_quantity=fill.fill_quantity,
        cumulative_filled=fill.cumulative_filled,
        remaining_quantity=fill.remaining_quantity,
        commission=_minor_to_units("commission_minor", order_id, fill.commission_minor),
        reason=fill.reason,
        timestamp=fill.timestamp,
    )


def deliver_order_event(
    strategy: object,
    context: object,
    event: OrderEvent,
    *,
    fill_at_ns: int,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> int:
    """Deliver one :class:`OrderEvent` to ``strategy.on_order_event``, fail-closed.

    The shared delivery seam every concrete dispatcher reuses (live IB execution
    per SRS-EXE-001, internal paper simulation per SRS-SIM-001):

    1. Validate the payload with :func:`assert_order_event_payload` — a malformed
       event (missing / wrong-typed field, lifecycle contradiction, fabricated
       shape) raises :class:`OrderEventContractError` and the user callback is
       **never** invoked. This is the SyRS SYS-64 structured-error contract.
    2. Invoke ``strategy.on_order_event(context, event)``.
    3. Return the delivery-latency sample in nanoseconds: ``clock() - fill_at_ns``,
       where ``fill_at_ns`` is the monotonic (``perf_counter_ns``-domain) stamp of
       the broker fill acknowledgement (live) or simulated fill (paper) — the
       ``t0`` the SyRS NFR-P4 budget is measured from. Sampling is caller-driven so
       this seam never reads a wall clock it hasn't been handed (the same
       discipline as ``atp_types::perf``).

    Args:
        strategy: The strategy sink; must expose an ``on_order_event`` callable.
        context: The :class:`StrategyContext` forwarded to the callback (opaque
            here — the seam never introspects it).
        event: The already-assembled :class:`OrderEvent` to deliver.
        fill_at_ns: Monotonic ns stamp of the originating fill acknowledgement.
        clock: Monotonic ns source for the delivery timestamp (injectable for
            deterministic tests); defaults to :func:`time.perf_counter_ns`.

    Returns:
        The delivery latency in nanoseconds — guaranteed ``>= 0`` (a fill that
        post-dates delivery is rejected, see Raises).

    Raises:
        OrderEventContractError: the payload fails the field-presence guard, the
            sink has no ``on_order_event`` callable, ``fill_at_ns`` is not an
            ``int``, or ``fill_at_ns`` post-dates the start of delivery (a future or
            wrong-clock-domain stamp). On any of these the callback is not invoked.
    """
    # Fetch the callback once; a None / missing / non-callable ``on_order_event``
    # (including ``strategy is None``, since ``getattr(None, ...)`` is ``None``)
    # fails closed here before any payload work.
    callback = getattr(strategy, "on_order_event", None)
    if not callable(callback):
        raise OrderEventContractError(
            "deliver_order_event: strategy sink exposes no callable "
            "on_order_event; cannot deliver the order-event callback"
        )
    if isinstance(fill_at_ns, bool) or not isinstance(fill_at_ns, int):
        raise OrderEventContractError(
            "deliver_order_event: fill_at_ns must be an int monotonic-ns stamp "
            f"(got {type(fill_at_ns).__name__}={fill_at_ns!r})"
        )
    # Fail closed on a fill timestamp that post-dates the start of delivery (a
    # future stamp, or one from a different clock domain than ``clock``) BEFORE any
    # user code runs. Otherwise ``clock() - fill_at_ns`` would be negative, silently
    # understating the NFR-P4 latency and breaking the u64 percentile ingestion in
    # nfr_p95_cli. ``clock`` is monotonic, so ``start_ns <= end_ns`` and the sample
    # below is guaranteed ``>= 0``.
    start_ns = clock()
    if fill_at_ns > start_ns:
        raise OrderEventContractError(
            "deliver_order_event: fill_at_ns post-dates delivery "
            f"(fill_at_ns={fill_at_ns} > now={start_ns}); a fill cannot be stamped "
            "after its own delivery — check the clock domain of fill_at_ns"
        )
    # Fail closed BEFORE invoking user code: a malformed / fabricated payload must
    # never reach the strategy callback.
    assert_order_event_payload(event)
    callback(context, event)
    return clock() - fill_at_ns


def deliver_simulated_fill(
    strategy: object,
    context: object,
    fill: SimulatedFill,
    *,
    clock: Callable[[], int] = time.perf_counter_ns,
) -> int:
    """Paper-path entry: build the :class:`OrderEvent` and deliver it, fail-closed.

    The internal paper simulation dispatcher (SRS-SIM-001) calls this once per
    simulated order-lifecycle event. Composes :func:`build_order_event` (descriptor
    → payload) with :func:`deliver_order_event` (guard → invoke → sample), measuring
    the delivery latency from the descriptor's own ``simulated_fill_at_ns`` stamp
    so the returned sample covers the full SDK delivery seam (assemble + guard +
    callback) — the portion of the SyRS NFR-P4 *paper* budget this SDK owns. The
    engine's fill-production latency (before the descriptor is emitted) is measured
    separately by SRS-SIM-001.

    Returns:
        The delivery latency in nanoseconds.

    Raises:
        OrderEventContractError: the descriptor can't be assembled or the built
            event fails the field-presence guard (callback not invoked).
    """
    event = build_order_event(fill)
    return deliver_order_event(
        strategy,
        context,
        event,
        fill_at_ns=fill.simulated_fill_at_ns,
        clock=clock,
    )
