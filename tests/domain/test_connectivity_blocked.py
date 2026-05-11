"""SRS-SAFE-003 — order submission is blocked while IB is unreachable.

Property test: with IB connectivity flapping, no `submit_order` call may
succeed against a disconnected adapter. The error must be CONNECTIVITY_BLOCKED.
"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = [pytest.mark.domain, pytest.mark.safety]

    @pytest.mark.skip(reason="pending implementation of execution-engine connectivity guard")
    def test_disconnected_adapter_rejects_all_orders() -> None:
        raise NotImplementedError(
            "Stub: Hypothesis-driven adapter with random connect/disconnect "
            "transitions; assert no submit_order call returns ACCEPTED while "
            "connected=False, and the raised error is CONNECTIVITY_BLOCKED."
        )
