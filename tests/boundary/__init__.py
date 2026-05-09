"""L4 — Service-boundary tests.

Catches: wiring mistakes between in-process modules (Strategy <-> Execution
<-> DataLayer) using real classes but stub adapters. Mark with
@pytest.mark.boundary.
"""
