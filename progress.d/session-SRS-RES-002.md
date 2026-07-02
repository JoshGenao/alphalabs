=== SESSION SRS-RES-002 ===
Date: 2026-07-02
Feature: SRS-RES-002 — provide Jupyter access to historical data, indicators, and
plotting (SRS §5.6 line 209; SyRS SYS-34b / SYS-34c / NFR-S6; verification: Test,
demonstration).
Outcome: serialized (code + Test-level evidence landed; passes stays false — the
live-JupyterLab-notebook "demonstration" needs the operator-supplied image).

What I did:
Built python/atp_research/ — the curated notebook research surface. The DATA ACCESS
already existed (SRS-DATA-007 StoreBackedHistoricalData binding, passes:true) and the
INDICATORS already existed (SRS-SDK-006 pandas-ta/TA-Lib wrappers); RES-002's own
scope per architecture/runtime_services.json is the "Jupyter notebook HOST runtime
(kernel / plotting / no-live-order isolation)". So this package composes the three
capabilities into one import and proves the isolation:
  * data.py — open_historical_data(): read-only SRS-DATA-007 handle. Resolves the
    data007_query_cli path from query_binary= or the ATP_DATA_QUERY_BINARY env (the
    operator JupyterLab-image knob), else the repo default; an absent binary fails
    CLOSED with an actionable StoreQueryError, never a hang / silent empty.
  * indicators.py — re-exports the SDK indicators (same objects, SyRS AC-6, no
    reimplementation) + compute_series(indicator, bars): batch-drives an incremental
    indicator over a bar history -> per-bar value series (None during warm-up).
  * plotting.py — bars_to_frame() (OHLCV pandas DataFrame, plot-/pandas-ta-ready) and
    plot_ohlc() (close line + optional indicator overlays, warm-up None drawn as a
    NaN gap). Uses matplotlib's OO Figure API (headless, no GUI backend, no global
    pyplot state) and RETURNS the Figure so a notebook cell inline-displays a chart
    (Figure carries _repr_html_; Axes does not).
  * __init__.py — curated __all__: ONLY read/compute/plot. No order-submission,
    cancellation, position, or credential surface.
Added matplotlib==3.11.0 to requirements.txt (operator-approved this fork over
requirements-dev/defer; SyRS SYS-34b names "plotting capabilities").

Key decisions:
  * Separate atp_research package (not inside atp_strategy): structurally isolates the
    research surface from the strategy SDK and sidesteps the strategy-authoring parity
    / documentation contracts (which target atp_strategy). One-way dep research->SDK.
  * No-live-order isolation is real: ATP's only order-submission entry point is
    StrategyContext.order, and StrategyContext is an uninstantiable typing.Protocol —
    there is no concrete submitter in the SDK a notebook could construct, and the
    orchestrator never wires one into a notebook process. atp_research neither
    re-exports it nor any order request/handle type. The CONTAINER-level sandbox
    (read-only mounts, no brokerage creds, no execution network) is the separate
    SRS-SEC-004 security control — named, not claimed here.
  * The live query path needs the cargo-built data007_query_cli present in the
    operator's JupyterLab image (docker/jupyter.Dockerfile is a Phase-1 stub;
    provisioning is SRS-ARCH-004). Documented + failing-closed + env-configurable;
    proven by a gated L5 integration test rather than faked.

What I tested (per step):
  Step 1: PASS — ./init.sh -> "Environment ready".
  Step 2 (exercise): PASS solo —
    pytest -m "not integration and not e2e" (full suite) green (2711 passed, 4
    pre-existing skips); the new tests:
      L1 tests/unit/test_research_indicators.py — compute_series == manual incremental
        drive; one entry per bar; None warm-up; empty; Protocol duck-typing; re-exports
        are the SAME SDK indicator objects.
      L1 tests/unit/test_research_plotting.py — bars_to_frame columns/UTC-index/empty;
        plot_ohlc renders a real PNG headlessly; returns an inline-displayable Figure
        (has _repr_html_) vs a non-displayable Axes; indicator overlay; warm-up NaN gap;
        rejects misaligned overlay + empty bars.
      L4 tests/boundary/test_research_data_access.py — notebook query by
        symbol/range/resolution with NO provider, over a fake data007 runner; empty is
        a value; failure -> StoreQueryError; the REAL default path fails closed with an
        actionable error when the binary is absent; ATP_DATA_QUERY_BINARY / explicit
        query_binary resolution + precedence.
      L7 tests/domain/test_research_isolation.py — public surface + read handle carry no
        order/credential token; atp_research re-exports no order type; StrategyContext is
        an uninstantiable Protocol; end-to-end data->indicator->plot touches no order path.
  Step 3 (AC): data query + indicator compute + plot render all demonstrated by test;
    no-live-order isolation demonstrated by L7. RAW verbatim path used for the fixture
    (split-adjusted needs a proven coverage frontier, SRS-DATA-011).
  Step 4 (evidence + hold passes false): serialized. The L5 integration test
    tests/integration/test_research_data_access.py (gated ATP_RUN_INTEGRATION=1, NOT run
    in this parallel session) exercises the REAL notebook path over the cargo-built
    data007_query_cli (no injected runner) — ingest -> read -> indicator -> plot — as the
    operator-runnable proof; the live-JupyterLab-in-the-dashboard demonstration
    (SRS-RES-001 reachability + operator JupyterLab image) cannot run here. passes stays
    false.
  Gate: ruff check/format clean; mypy python/ unchanged at the 66-error pre-existing
    baseline (0 added by atp_research); deterministic critic APPROVE.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (tools/codex_review.sh origin/main): APPROVE at round 3. R1 [high]: default
    notebook path relies on an unprovisioned data007_query_cli -> added env/arg
    resolution + fail-closed actionable error + tests. R2 [high] data wiring + [medium]
    inline plot: added the gated L5 real-binary no-injection integration test and made
    plot_ohlc RETURN the Figure (inline-displayable) — R3 APPROVE, no material findings.

Resume / next:
  SRS-RES-002 stays passes:false. To flip it (operator, --mode complete or verified-e2e):
  provision the JupyterLab image (docker/jupyter.Dockerfile production image, SRS-ARCH-004
  Docker Compose stack) with JupyterLab + the cargo-built data007_query_cli (point at it
  via ATP_DATA_QUERY_BINARY) + a mounted store (ATP_DATA_STORE_DIR / the :ro data volumes),
  make it reachable from the dashboard (SRS-RES-001), then demonstrate in a live notebook:
  import atp_research; open_historical_data(...).get_bars_range(...); compute_series(...);
  plot_ohlc(...). Run tests/integration/test_research_data_access.py with
  ATP_RUN_INTEGRATION=1 as the objective end-to-end evidence. Container-level Jupyter
  isolation is SRS-SEC-004.

Also this session (housekeeping, no code): de-churned two dead re-leases of
already-serialized/blocked features so the scheduler stops re-offering them by id —
  * SRS-NOTIF-001 (core dispatcher already done + serialized on main) blocked-on
    SRS-EXE-006 (its real fault-injection/SMTP-SMS e2e rides the operator IB integration);
  * SRS-RES-001 (embed Jupyter IN the dashboard) blocked-on SRS-UI-001 (no dashboard
    exists yet; browser-automation demonstration).
