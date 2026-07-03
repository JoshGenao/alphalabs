"""SRS-SDK-004 / SyRS NFR-P4 — order-event money-conversion property tests.

L2 property tests (Hypothesis) over the integer-minor-unit → float conversion the
paper delivery seam (``atp_strategy.dispatch.build_order_event``) performs when it
maps a :class:`SimulatedFill` descriptor onto the strategy-facing
:class:`OrderEvent`. The L7 domain test pins the round-trip on a handful of fixed
representative values; this layer generalises the money-math invariants over the
generated value space.

The canonical *exact* money on the boundary is the integer minor units (cents) the
Rust simulation engine computes; the float is the SDK-facing view. The invariant
that keeps paper / live P&L reconciling is that the float round-trips to the
**nearest minor unit**. Generated minor values are bounded to a realistic money
domain (0 .. $100,000,000 in cents) — far inside float64's 53-bit exact-integer
range — so the round-trip is exact; outside that range the descriptor's integer
minor units remain the source of truth (documented on ``MINOR_UNITS_PER_UNIT``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "python") not in sys.path:
    sys.path.insert(0, str(ROOT / "python"))

from atp_strategy.api import OrderEventType  # noqa: E402
from atp_strategy.dispatch import (  # noqa: E402
    MINOR_UNITS_PER_UNIT,
    SimulatedFill,
    build_order_event,
)

pytestmark = pytest.mark.property

# Realistic money domain in integer minor units (cents): 0 .. $100,000,000.
_MINOR = st.integers(min_value=0, max_value=10_000_000_000)
# Strictly positive prices for FILL/PARTIAL_FILL (a fill at zero price is invalid).
_POSITIVE_MINOR = st.integers(min_value=1, max_value=10_000_000_000)


def _fill_descriptor(price_minor: int, commission_minor: int, qty: int) -> SimulatedFill:
    return SimulatedFill(
        event_type=OrderEventType.FILL,
        sim_order_id="ord-prop",
        client_order_id="cli-prop",
        strategy_id="s1",
        symbol="AAPL",
        fill_price_minor=price_minor,
        fill_quantity=qty,
        cumulative_filled=qty,
        remaining_quantity=0,
        commission_minor=commission_minor,
        reason=None,
        simulated_fill_at_ns=0,
        timestamp="2026-07-03T13:30:00Z",
    )


@given(price_minor=_POSITIVE_MINOR, commission_minor=_MINOR, qty=st.integers(1, 1_000_000))
def test_money_round_trips_to_nearest_minor_unit(
    price_minor: int, commission_minor: int, qty: int
) -> None:
    event = build_order_event(_fill_descriptor(price_minor, commission_minor, qty))
    assert event.fill_price is not None and event.commission is not None
    assert round(event.fill_price * MINOR_UNITS_PER_UNIT) == price_minor
    assert round(event.commission * MINOR_UNITS_PER_UNIT) == commission_minor


@given(a=_POSITIVE_MINOR, b=_POSITIVE_MINOR)
def test_conversion_is_monotonic(a: int, b: int) -> None:
    """Larger minor value never converts to a smaller float price — order is
    preserved, so downstream P&L comparisons on the float view stay faithful to
    the integer minor units."""
    price_a = build_order_event(_fill_descriptor(a, 0, 10)).fill_price
    price_b = build_order_event(_fill_descriptor(b, 0, 10)).fill_price
    assert price_a is not None and price_b is not None
    if a < b:
        assert price_a < price_b
    elif a > b:
        assert price_a > price_b
    else:
        assert price_a == price_b


@given(commission_minor=_MINOR)
def test_conversion_is_non_negative(commission_minor: int) -> None:
    event = build_order_event(_fill_descriptor(1_000, commission_minor, 10))
    assert event.commission is not None
    assert event.commission >= 0.0
