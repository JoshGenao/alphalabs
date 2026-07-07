=== SESSION UI-3 ===
Date: 2026-07-07
Feature: UI-3 — dashboard backtest controls + result history (SyRS SYS-42/SYS-43a; traces SRS-UI-004 + SRS-BT-001)
Outcome: serialized (operator-authorized) — code integrates; passes STAYS false. Step 4 mandates
browser evidence, AND the launch (write) leg is genuinely deferred. UI-3 is a KEYSTONE: SRS-BT-001
is blocked-on [SRS-API-001, UI-3]; a serialized landing does NOT auto-flip UI-3, so SRS-BT-001 stays
blocked until the operator flips UI-3 passes:true.

WHAT I DID (feat commit, no prep needed — none of the changed paths match SAFETY_PATH_RE, domain
test added anyway; built with the /frontend-design + dataviz skills, fully self-contained per SEC-002):
- READ leg (REAL): python/atp_dashboard/backtests.py — StoreCliBacktestHistorySource shells the GREEN
  bt009_store_cli `query --format kv --full` (new ADDITIVE machine-output mode on the SRS-BT-009 CLI;
  default `human` output byte-identical + existing bin tests untouched). BacktestHistoryProvider parses
  the indexed `record.<i>.<field>` proof lines FAIL-CLOSED into the seven SRS-UI-004 drill-down
  artifacts (strategy, params, date range, 8 metrics, benchmark comparison, full trade log, full equity
  curve). Undefined metric => null (no fabrication); unreadable store => ok:false, never partial.
- server.py: `backtests=` opt-in on mount_dashboard registers GET /dashboard/api/backtests; AND a NEW
  `mount_default_dashboard(runtime, env)` (used by `python -m atp_dashboard`) ALWAYS composes the
  provider from ATP_BACKTEST_RESULTS_DIR, driving the CLI subprocess env from the passed mapping so it
  is deterministic (a mapping omitting the key cannot leak an ambient store; fails closed to ok:false).
- CONTROLS leg (HONESTLY DEFERRED): assets 6th panel (index.html/styles.css/app.js) — a launch form
  (strategy / date range / parameter overrides / cost model) whose "Run" affordance POSTs to the
  CONTRACT route POST /api/v1/backtests (never a /dashboard path) and renders the runtime's own 501
  HANDLER_DEFERRED verbatim (declared owner SRS-BT-001) — exactly UI-001's kill-switch precedent. Plus a
  REAL result-history table + drill-down: a hand-authored inline-SVG equity-curve chart (hover readout,
  min/max markers, baseline), sign-aware metric tiles, full trade log, SPY/benchmark comparison. Scoped
  .backtest-* CSS + additive tokens only (UI-001/UI-002 panels + tests do not regress).
- Rust: crates/atp-simulation/src/bin/bt009_store_cli.rs gained `query --format {human|kv}` + a kv
  emitter that FAILS CLOSED on any control-char string (the store permits a newline in a --param value,
  which would forge a proof line) — the machine format is unforgeable by construction.

WHAT I TESTED (per step):
  Step 1 (init): PASS — ./init.sh -> "✓ Environment ready" (dev-deps installed manually; init.sh skips
    requirements-dev.txt).
  Step 2 (exercise): PARTIAL/solo — browser e2e is WRITTEN + gated (ATP_RUN_E2E=1) but NOT run (4
    siblings leased; dashboard/e2e forbidden in parallel). In-process + REST smoke ran solo: GET
    /dashboard->200; mounted GET /dashboard/api/backtests->200 with REAL records (newest-first); bare
    mount->404 (honest "not mounted"); POST /dashboard/api/backtests->404 (read-only); POST
    /api/v1/backtests->501 HANDLER_DEFERRED owner=SRS-BT-001; bt009_store_cli persist --init seeds a real
    demo pair, query --format kv --full round-trips through the Python parser.
  Step 3 (AC): controls form shipped (strategy/date/params/cost); "inspect completed backtest details"
    is REAL (history list + drill-down over the real store). "initiate backtests" is HONESTLY DEFERRED
    (contract-route affordance renders the 501). So the read/inspect half is demonstrable; the
    initiate/write half is deferred -> passes:false.
  Step 4: passes:false retained; UI behavior traces to SRS-BT-001 (launch) + SRS-UI-004 (history)
    confirmed; browser evidence deferred to the operator flip.
  Gate: cargo test --workspace ok; pytest -m "not integration and not e2e" 3051 passed, 0 regressions;
    cargo fmt/clippy clean; ruff clean; mypy adds 0 new errors (pre-existing publisher.py:176 baseline);
    18 architecture/contract checks PASS. NOTE: tools/run_ci_locally.sh is RED on a PRE-EXISTING baseline
    (ruff format on tests/domain/test_coverage_gate_domain.py — unmodified by me, already fails on
    origin/main; the toolchain-pin fix is a separate PR) — not a UI-3 regression; integrate does not run
    that gate.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py origin/main, reviewer=codex): 4 rounds; converged.
    R1 BLOCK: (a) launch deferred [scope]; (b) production entrypoint never mounts history provider.
      -> FIXED (b): mount_default_dashboard always composes the provider.
    R2 BLOCK: (a) launch deferred [scope]; (b NEW) kv format forgeable via a newline-bearing param value.
      -> FIXED (b): Rust kv emitter + Python parser both fail closed on control chars / duplicate fields;
         Rust bin test persists a \n param and asserts kv fails closed.
    R3 BLOCK (launch-scope DROPPED by codex): (1) history rows not reordered after a poll; (2) "vs SPY"
      column hardcoded though records carry benchmark_symbol; (3) mount_default_dashboard could leak an
      ambient store when env omits the key. -> ALL FIXED: reorder pass (newest-first) each poll;
      neutral "Excess" column (drill-down shows the real benchmark); subprocess env driven by the passed
      mapping + a determinism boundary test (ambient set, passed env omits -> ok:false).
    R4 BLOCK: ONLY "launch path still deferred" — codex's own resolution: "integrate as explicitly
      serialized/partial history-only slice without claiming UI-3 complete." That IS the classification.
      **Operator-authorized override (AskUserQuestion, 2026-07-07): land --mode serialized; passes stays
      false.** Never faked an APPROVE. The deferred launch handler is L7-pinned
      (tests/domain/test_dashboard_safety.py: the affordance uses ONLY the contract route -> 501,
      owner SRS-BT-001) and cannot reach live paths.

Resume / next (what flips UI-3 passes:true — an operator step, none auto):
  1. Browser evidence: `playwright install chromium` + ATP_RUN_E2E=1 pytest
     tests/e2e/test_dashboard_refresh.py::test_backtest_panel_renders_real_history_and_honest_deferred_launch
     (run SOLO — no sibling leases). It seeds a real store via bt009_store_cli persist --init and asserts
     the history table, drill-down chart, trade log, and the honest 501 launch outcome.
  2. Live launch handler behind POST /api/v1/backtests (currently DeferredHandler 501): owned by
     SRS-API-001 (REST handler registration; BLOCKED) + the deferred Python strategy host (operator scope
     decision from the SRS-BT-001 note) + param-override/cost-model plumbing (bt001 hardcodes
     CostConfig::default, no strategy selection). When those land, the app.js `submitBacktest` success
     branch already renders {backtest_id, queued_at} — it is a handler wiring, not a UI rebuild.
  DEFERRED OWNERS: launch handler = SRS-API-001; strategy host + param/cost = SRS-BT-001 runtime;
  orchestrated launch->persist so real runs appear in history = SRS-BT-009/orchestrator. Don't rebuild
  the panel or the read leg — wire the handler + capture browser evidence, then flip.
  ASYMMETRY NOTE: mount_default_dashboard composes the BACKTEST provider but NOT the SRS-UI-002 inventory
  (still opt-in-only) — wiring inventory into the default composition is a UI-002 follow-up, out of UI-3 scope.
