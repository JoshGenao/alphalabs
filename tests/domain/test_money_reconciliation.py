"""Money-math invariants under float arithmetic.

Prices use IEEE 754 floats (no Decimal). Property tests assert the
reconciliation invariant with explicit relative tolerance:

    abs(sum(fill.price * fill.qty) - executed_notional) < 1e-6 * notional

If this ever tightens, migrate price fields to Decimal first.
"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = [pytest.mark.domain, pytest.mark.property]

    @pytest.mark.skip(reason="pending implementation of fill aggregator")
    def test_fill_aggregation_matches_executed_notional_within_tolerance() -> None:
        raise NotImplementedError(
            "Stub: Hypothesis-generated list of (price, qty) fills; assert "
            "abs(sum(p*q) - aggregator.executed_notional) < 1e-6 * sum(p*q)."
        )
