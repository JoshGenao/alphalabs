"""SRS-SAFE-001 — kill switch must complete within 5 seconds.

Stub adapter simulates 50 open positions; activation must cancel all resting
orders, submit market liquidations, halt paper engines, and disconnect IB
within the 5-second window (NFR-P3).
"""

from __future__ import annotations

try:
    import pytest
except ImportError:  # unittest discovery without pytest — define nothing
    pass
else:
    pytestmark = [pytest.mark.domain, pytest.mark.safety]

    @pytest.mark.skip(reason="pending implementation of crates/atp-execution kill_switch")
    def test_kill_switch_completes_within_5_seconds() -> None:
        raise NotImplementedError(
            "Stub: drive kill-switch via operator CLI against a stub IB adapter "
            "with 50 open positions; assert elapsed wall time <= 5.0s and that "
            "every position has a corresponding liquidation order."
        )
