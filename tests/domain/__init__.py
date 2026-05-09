"""L7 — Trading-domain-specific tests.

Catches: safety-critical behavior that generic test layers cannot reason about.
Each module here covers one trading-system invariant:

- test_kill_switch_latency.py        — SRS-SAFE-001 (<=5s liquidation)
- test_connectivity_blocked.py       — SRS-SAFE-003 (IB unreachable blocks orders)
- test_single_live_invariant.py      — exactly-one-live-strategy invariant
- test_deterministic_replay.py       — bar replay reproducibility
- test_money_reconciliation.py       — float-arithmetic tolerance invariants
- test_stale_data_block.py           — feed staleness blocks order submission

Mark with @pytest.mark.domain. Safety-critical changes MUST land with a
matching diff here (enforced by tools/critic_check.py).
"""
