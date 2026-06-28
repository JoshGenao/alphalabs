"""Exactly one strategy may be in live IB execution mode at any time (AGENTS.md).

Property test: across any random sequence of `live promote` / `stop` /
`rollback` calls, the orchestrator never reports two strategies in live mode.
"""

from __future__ import annotations

try:
    import pytest
except ImportError:
    pass
else:
    pytestmark = [pytest.mark.domain, pytest.mark.safety]

    @pytest.mark.skip(reason="pending Hot-Swap per SRS-RESV-001..006 (promote/demote/rollback)")
    def test_at_most_one_live_strategy_under_random_operator_sequences() -> None:
        raise NotImplementedError(
            "Stub: Hypothesis-driven sequence of operator commands; after each "
            "command, assert sum(s.mode == LIVE for s in orchestrator.strategies) <= 1."
        )
