"""L5 — Integration tests with real containers.

Catches: bugs that only surface against real I/O (Postgres/Parquet round-trips,
IB Gateway paper, Databento sandbox). Gated by ATP_RUN_INTEGRATION=1 — these
tests do not run by default. Mark with @pytest.mark.integration.
"""
