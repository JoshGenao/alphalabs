"""L2 — Property-based / fuzz tests.

Catches: invariant violations under inputs you didn't think to write
(P&L identities, fill reconciliation, order-state transitions). Use Hypothesis.
Mark with @pytest.mark.property.
"""
