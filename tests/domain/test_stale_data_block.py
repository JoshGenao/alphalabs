"""Stale-data guard — feed staleness above threshold blocks order submission.

When the latest bar timestamp is older than the configured staleness threshold,
the execution engine must reject new orders rather than trade on stale prices.
"""
from __future__ import annotations

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = [pytest.mark.domain, pytest.mark.safety]

    @pytest.mark.skip(reason="pending implementation of execution-engine staleness guard")
    def test_orders_blocked_when_market_data_is_stale() -> None:
        raise NotImplementedError(
            "Stub: feed bars with a timestamp older than the staleness threshold; "
            "assert submit_order returns STALE_DATA_BLOCKED."
        )
