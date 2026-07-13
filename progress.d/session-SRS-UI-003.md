=== SESSION SRS-UI-003 ===
Date: 2026-07-13
Feature: SRS-UI-003 — display account-level IB status + Reservoir overview in the dashboard (SyRS SYS-43b / SYS-48)
Outcome: serialized — the account + Reservoir panels + their WS channels + REST poll routes are
built and wired end-to-end (in-process + real-socket verified), with the SYS-48 evaluation-window
selector a REAL control; passes stays false because EVERY account/ranking VALUE is an honest
deferred cell (the AC's Demonstration is of the values, and both producers are unbuilt).

CONTEXT: the dashboard is python/atp_dashboard/ mounted on the atp_runtime operator runtime
(SRS-API-001), built serialized across UI-001 (PNL/METRICS/HEARTBEAT + readiness), UI-002
(STRATEGY_STATE inventory) and UI-3 (backtest history). The two channels SRS-UI-003 needs —
ACCOUNT_STATUS + RESERVOIR_RANKING — were pre-declared in atp_ws/channels.py with NO publisher
(exactly the STRATEGY_STATE state UI-002 filled). Investigation (2 explore agents + verification)
confirmed NO solo-runnable producer exists for either panel: account fields come "as reported by
the IB account" via SRS-EXE-006 (crates/atp-adapters; account_status() defaults to not_configured,
real path behind the non-default ib-live-transport feature over a live socket, operator-initiated
per SYS-2e port 4002); the readiness gate's IB-connectivity probe is deferred to SRS-MD-006; and
the SYS-48 ranking engine SRS-RESV-002 is unbuilt (channel + GET /api/v1/reservoir/ranking are
schema-only). So this is an all-deferred panel-pair — the demonstrable deliverable is the plumbing
+ honest "—" cells, exactly like UI-001's trading panels.

WHAT I DID (feat commit; built with the /frontend-design + dataviz skills, self-contained per
SEC-002; mirrors inventory.py exactly; contract.py + ReadinessBackedProvider LEFT UNTOUCHED):
- python/atp_dashboard/account.py (NEW): AccountStatusProvider — opt-in source for ACCOUNT_STATUS.
  account_status_events() = one event, keys exactly the channel payload_fields (real as_of + the
  six fields each deferred_field_named("SRS-EXE-006")); account_snapshot() = GET /dashboard/api/account
  body (ok:true + deferred cells + srs_ref SRS-UI-003). Not a ReadinessBackedProvider subclass.
- python/atp_dashboard/reservoir.py (NEW): ReservoirRankingProvider — opt-in source for
  RESERVOIR_RANKING. Ranking-result fields (eval_window_days/rankings/sharpe/sortino/momentum_score)
  all deferred_field_named("SRS-RESV-002"); rankings is a DEFERRED CELL (value None), never [] (an
  empty list would masquerade as "ranked, zero strategies"). reservoir_snapshot() ALSO carries the
  REAL SYS-48 selector config (allowed_windows=(1,7,15,30,60,90), default_window=30) — a UI input,
  kept clearly separate from the deferred ranking results.
- publisher.py: account/reservoir opt-in on the MAIN ticker (pure builders, no subprocess → no
  isolated thread; a bare publisher keeps channels==OWNED_CHANNELS, preserving the L1 tests). Each
  tick is _guarded (a bad tick can't kill the ticker). Replaced the _run lambda with functools.partial
  (mypy-clean — eliminates the pre-existing publisher.py lambda baseline error).
- server.py: mount_dashboard(..., account=None, reservoir=None) registers GET /dashboard/api/account
  + /dashboard/api/reservoir (404 when not mounted); mount_default_dashboard composes BOTH (no env
  needed), so `python -m atp_dashboard` serves the panels + registers both publishers.
- assets: two new panels — Account (hero equity, sign-aware P&L pair, inline-SVG margin-usage meter
  [deferred=striped track], buying power, IB-connection pill) + Reservoir (panel--wide: SYS-48
  <select> window control, summary, ranked leaderboard with rank medallions + momentum sparkline).
  SUBSCRIBE + PANEL_FRESH gauge:false (opt-in channels off the NFR-P2 pulse); scoped .account/
  .reservoir CSS (dark/light/reduced-motion/ARIA); footer legend names SRS-EXE-006 + SRS-RESV-002.
  UI-001/002/3 panels do not regress.
- contract.py DELIBERATELY UNTOUCHED (mirrors UI-002): ACCOUNT_STATUS is an orphan bucket (no
  workflow); registering the RESERVOIR_RANKING publisher increments its required workflow's
  implemented_operations 0->1 but fully_served stays false (REST route still 501) — the exact merged
  HEARTBEAT precedent (test_operator_interface_runtime.py::test_websocket_obligation_keeps_a_workflow_not_fully_served).
  validate_owners stays green (both owners already in deferred[]). No openapi/asyncapi snapshot change.

WHAT I TESTED (per AC step):
  Step 1 (init): PASS — ./init.sh -> "✓ Environment ready".
  Step 2 (browser + REST/WS): PARTIAL/solo — browser e2e is WRITTEN + gated (ATP_RUN_E2E=1) but NOT
    run (3 siblings leased: SRS-DATA-020/REL-001/SEC-004 — dashboard/e2e forbidden in parallel).
    In-process + REAL-socket smoke ran solo: GET /dashboard 200, /app.js + /styles.css served; GET
    /dashboard/api/account 200 (six deferred:SRS-EXE-006 cells), /dashboard/api/reservoir 200
    (deferred:SRS-RESV-002 rankings + real windows [1,7,15,30,60,90] default 30); bare mount both
    routes 404; POST/PUT/DELETE 404 (read-only); WS SUBSCRIBE -> ACCOUNT_STATUS + RESERVOIR_RANKING
    EVENTs each < 5s; RESERVOIR_RANKING workflow fully_served=False + GET /api/v1/reservoir/ranking
    still 501 owner SRS-RESV-002.
  Step 3 (AC fields): IB equity / daily & cumulative P&L / margin usage / buying power render as
    honest deferred cells (owner SRS-EXE-006); paper-strategy rankings + momentum scores render as
    honest deferred (owner SRS-RESV-002). The SYS-48 window selector is REAL. All VALUES deferred ->
    passes stays false (serialized).
  Step 4 (evidence): passes:false retained; per-step record above; browser Demonstration + live
    evidence deferred to the operator flip.
  Gate: pytest -m "not integration and not e2e" -> 3408 passed, 3 skipped (pre-existing), 0
    regressions; new L1 (12) + L4 (7) + extended L7 + gated L6 collect+pass; ruff check + format clean
    on ALL my files; mypy clean on the 4 new/changed atp_dashboard modules; node --check app.js OK;
    5 contract gates PASS (operator_interface_runtime / websocket_api / operator_workflow_surface /
    network_binding / architecture). NOTE: tools/run_ci_locally.sh is RED on a PRE-EXISTING baseline
    (ruff I001 import-sort in tools/architecture_check.py + tests/domain/test_strategy_container_least_privilege.py,
    both SEC-003 files I did not touch — reproduces on origin/main; integrate does not run that gate).

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py origin/main, reviewer=codex): APPROVE — no findings, converged in 1
    round ("wires deferred account and Reservoir dashboard surfaces without touching lower-layer/
    runtime adapter boundaries; deterministic critic passed"). The plan pre-empted the predictable
    findings (surface-churn / fabrication / workflow-overstate / contract-collision / mutation /
    SEC-002 / missing-domain-test).

Resume / next (what flips SRS-UI-003 passes:true — an operator step, none auto):
  1. Browser evidence (run SOLO — no sibling leases): playwright install chromium + ATP_RUN_E2E=1
     pytest tests/e2e/test_dashboard_refresh.py::test_account_and_reservoir_panels_render_honest_deferred
     (asserts both panels render deferred cells, the SYS-48 selector options 1/7/15/30/60/90 default
     30, and each panel dot reaches fresh <=5s).
  2. Real ACCOUNT_STATUS values: SRS-EXE-006 live IB adapter (account_status behind ib-live-transport,
     operator-initiated port 4002) — then AccountStatusProvider swaps its six deferred cells for the
     adapter's account summary (equity/daily+cumulative pnl/margin/buying power/connection). Provider-
     cell swap, not a rebuild; the panel + WS payload shape + REST route already match the contract.
  3. Real RESERVOIR_RANKING values: SRS-RESV-002 SYS-48 ranking engine (canonical shape
     atp-types::ReservoirRankingSnapshot{evaluation_window_days, ranked:[RankedStrategy{strategy_id,
     rank, risk_adjusted_score, momentum_score}]}) — then ReservoirRankingProvider emits real
     rankings; the client selector drives GET /api/v1/reservoir/ranking?eval_window_days=N (declared,
     not wired now). app.js renderReservoirRow (medallions + momentum sparkline) already handles the
     real shape.
  DEFERRED OWNERS: account = SRS-EXE-006 (live IB); ranking = SRS-RESV-002 (SYS-48 engine, blocked-on
  SRS-RESV-001). Don't rebuild — wire the producers + capture browser evidence, then flip.
