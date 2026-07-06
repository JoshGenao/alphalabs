=== SESSION SRS-SAFE-001 ===
Date: 2026-07-05
Feature: SRS-SAFE-001 — kill switch from dashboard, CLI, and REST API following the
  QuantConnect Liquidate sequence (SyRS SYS-44a; NFR-P3; NFR-SC1; StRS SN-1.11).
Outcome: serialized (passes stays false) — the FULL activation runtime is built and
  evidenced over the mocked-IB fixture transport the feature's own Step 2 prescribes;
  the LIVE path is deferred to named owners.

CONTEXT (what already existed on main): the sealed per-engine halt gate
(crates/atp-simulation/src/halt.rs, prior SAFE-001 sub-component session), the SAFE-002
timeout gate + IbLiquidationCleanup ports (atp-execution), LiveExecutionState (EXE-005
schema: order ledger / broker-id bindings / open positions), the LOG-001 durable
JsonlLogStore (+ Source.KILL_SWITCH), confirmation-guarded CLI/REST contract stubs
(428 / exit 3 live; handlers DeferredHandler-501), a skipped 5s NFR-P3 domain-test stub,
and the PERF-001 percentile engine. This session built the genuinely missing ACTIVATION
runtime and its operator surfaces.

What I did (5 commits):
1. Rust core: atp-types activation vocabulary (KillSwitchActivationRequest/Report/Event,
   RestingOrderCancel(+Outcome), LiquidationSubmission, PaperHaltSummary, timings;
   budgets KILL_SWITCH_ACTIVATION_BUDGET_MS=5000 / KILL_SWITCH_HALT_OBSERVABILITY_
   BUDGET_MS=1000; reuses SideEffectOutcome). atp-execution kill_switch.rs:
   ExecutionEngine::activate_kill_switch over 4 ports (KillSwitchClock /
   KillSwitchBrokerageControl {cancel_resting_order, submit_market_liquidation,
   disconnect} / PaperHaltFanout / KillSwitchActivationEventSink). PHASE ORDER: halt
   paper engines FIRST (the 1s SRS-LOG-001 observability budget cannot sit behind up to
   5s of lawful brokerage I/O), then cancel every resting (non-terminal, sorted)
   live-strategy order (broker binding or honest None), then one validated
   opposite-direction MARKET liquidation per open position (long->SELL |net|,
   short->BUY |net|), disconnect LAST (the AC's one explicit ordering).
   CONTINUE-TO-SAFETY: signature returns the report (no Result, no early return, no ?);
   every failure recorded as SideEffectOutcome::Failed, later phases always attempted;
   best-effort event sink. atp-simulation halt_fleet.rs: PaperEngineFleet over sealed
   HaltablePaperEngines (halt_all visits EVERY engine; transitioned + already_halted ==
   engines_total; blank/dup registration fails closed; no engine reference leaks).
2. Orchestrator composition (the one crate allowed to see both layers):
   kill_switch_activation.rs (FleetHaltPort over the REAL fleet; FixtureBrokerageControl
   = the deterministic mocked-IB transport w/ per-step fault injection + injectable
   latency; run_fixture_activation drives the REAL gate over a REAL LiveExecutionState)
   + safe001_kill_switch_cli (allowlist fail-closed parser; activate -> report:{json},
   exit 0 clean / 1 ran-with-failures / 2 unrunnable; perf -> nearest-rank
   p50/p95/p99/p99.9 via atp-types perf.rs + verdict on max liquidations_submitted_ms
   <= 5000; no LatencyNfr catalog extension — NFR-P3 is a one-shot deadline).
3. LOG-001 extension: EVENT_TYPES_BY_SOURCE[KILL_SWITCH] = ("ACTIVATION", "HALTED")
   (+ contract JSON + mutation-literal update) — HALTED is a first-class queryable
   SYSTEM event per the AC.
4. python/atp_safety: RustCliKillSwitchBackend (subprocess bridge, fail-closed: missing
   binary / exit 2 / bad report / id mismatch -> KILL_SWITCH_BACKEND_UNAVAILABLE;
   TimeoutExpired -> TimeoutError -> 504 -> CLI exit TIMEOUT); handlers for
   POST /api/v1/kill-switch + kill-switch activate/status (replay guard consulted
   BEFORE the backend, armed BEFORE the audit writes — repeat activate replays, never
   re-liquidates; durable ACTIVATION+HALTED writes w/ measured latency vs the 1s budget;
   response = exactly the SDK-pinned fields; status honest-empty); durable state record
   (O_EXCL scratch+fsync+rename+dir-fsync; corrupt state fails CLOSED). Ownership
   re-point kill-switch -> SRS-SAFE-001 (contract.py + operator_workflow_surface
   deferred[] + registry docstrings + owner literal). tools/kill_switch_check.py (9
   structural checks) + tests/test_kill_switch_activation_contract.py (14, incl. 11
   mutation non-vacuity).
5. Dashboard minimal affordance (SYS-44a "accessible from the dashboard"): two-step
   arm-then-fire control POSTing to the CONTRACT route, rendering the runtime's response
   verbatim (refusals shown as their error type); no dashboard-namespaced mutation
   (read-only safety tests still green); UI-4 keeps the rich status-feedback control.
   runtime_services.json: NEW kill_switch_activation_contract (ports, phase-order pins,
   budgets, fleet, CLI, operator surface, deferred[]) + reconciled paper_halt_contract /
   kill_switch_timeout_contract / operator_workflow_surface_contract deferred[] and the
   halt.rs / atp-execution lib.rs / sim_halt_check.py doc prose.

What I tested (per AC step):
  Step 1 (init): ./init.sh -> "Environment ready" (after clearing a stale orphaned
    placeholder http.server holding port 3000 from a July-2 session).
  Step 2 (exercise w/ mocked IB + CLI/API + logs): operator-driven e2e — CLI
    kill-switch activate (exit 3 unconfirmed; --confirm exit 0 w/ frozen-field JSON),
    kill-switch status (honest-empty then populated w/ liquidation outcomes +
    audit latency), REST 428 unconfirmed / 200 confirmed / 501 on a bare runtime;
    safe001_kill_switch_cli activate clean (exit 0) + fault-injected (exit 1, failures
    surfaced in-report, later phases still ran) + usage (exit 2, no report).
  Step 3 (acceptance): NFR-P3 — tests/domain/test_kill_switch_latency.py (previously
    a skipped stub, now implemented): 50 positions / 50 resting / 30 engines through
    the operator CLI + real Rust gate, wall-clock <= 5.0s, every position liquidated
    opposite-direction; perf 20 iterations p50 ~0.5ms, verdict:PASS; negative control:
    600ms/call injected transport latency -> mark 6078ms -> verdict:FAIL exit 1 (the
    measurement has teeth). HALTED w/ no further on_fill — srs_safe_001_halt_fleet.rs
    (30 engines halted, every post-halt fill refused -> no SimulatedFill exists to
    drive a callback; idempotent; counts invariant). 1s observability —
    test_kill_switch_halted_observability.py: durable ACTIVATION+HALTED records
    (correlation_id = activation_id) through the REAL wired stack, measured <= 1.0s.
    Disconnect-after-liquidation — spy-port ordering test (disconnect strictly after
    the LAST liquidation).
  Step 4 (record + passes false): this note; passes stays false (serialized), pinned
    by test_paper_halt_lifecycle.py::test_safe_001_stays_unflipped + kill_switch_check.
  Suites: cargo test (atp-types/simulation/execution/orchestrator) 900+ green incl.
    new suites (9 activation + 7 fleet + 3 composition); pytest solo suite green
    (runtime/workflow/log/dashboard/kill-switch: 162 + 43 + 81 focused runs; full
    gate run below); ruff + format clean; atp_safety mypy strict-clean.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE on every commit — no findings;
    critic_check.py --range origin/main..HEAD: APPROVE (post-rebase).
  judgment (adversarial_review.py origin/main, reviewer=claude-fallback — Codex
    rate-limited at review time; verdict carried NON-empty findings, not the
    distrusted empty-summary shape): WARN, 3 findings, resolved as follows:
    1. FIXED (doc-drift): the operator-interface-runtime deferred[] narrative still
       attributed the kill-switch handler to SRS-EXE-001 — re-pointed to SRS-SAFE-001.
    2. FIXED (hardening, was info): a failed durable audit write left audit_recorded
       false forever on replay — the replay path now RETRIES the pending
       ACTIVATION+HALTED writes (nothing was written before, so no duplication;
       still-failing keeps surfacing KILL_SWITCH_AUDIT_WRITE_FAILED; the sequence
       never re-fires). New paired domain + boundary tests
       (test_failed_audit_write_* in tests/domain/test_kill_switch_halted_observability.py
       and tests/boundary/test_kill_switch_wiring.py).
    3. OVERRIDE (warn, one line): the OpenAPI 200 schema types the kill-switch
       response fields as placeholder strings while the handler returns concrete
       int/bool/arrays — accepted: the route's own description declares "Concrete
       request and response schemas land with the downstream feature that owns the
       handler", i.e. the spec marks its own types provisional; re-typing the
       snapshot belongs to the atp_api SDK surface and is deliberately not churned
       here (UI-001 precedent). Documented in python/atp_safety/README.md.
  judgment ROUND 2 (2026-07-06): the dispatcher's two re-runs both failed on
    INFRASTRUCTURE (one non-final "awaiting investigations" output; one API
    connection-drop the dispatcher fail-closed normalized to "block" — a
    transport failure, not a judgment; Codex rate-limited through the session).
    Per the established failover, a REAL fresh-context sub-agent critic ran
    prompts/critic_prompt.md over the full branch diff (reviewer=
    claude-subagent-fresh; it independently re-ran kill_switch_check,
    critic --range, the Rust suites and 163 Python tests): verdict WARN,
    4 substantive findings, resolved:
    a. FIXED (warn, async-race): the replay guard was check-then-act on a
       threading REST server — two concurrent confirmed POSTs could both fire
       the sequence. A per-handler threading.Lock now serializes activations;
       the loser replays the winner's response (domain test:
       test_concurrent_confirmed_activations_fire_the_sequence_exactly_once).
    b. FIXED (warn, overstated guarantee): persist_last_activation can fail
       AFTER the sequence ran, leaving the guard unarmed. Now surfaced as its
       own KILL_SWITCH_REPLAY_GUARD_UNARMED error (explicitly warning a blind
       retry WOULD re-fire) after a best-effort audit write so one durable
       trace exists; the module docstring no longer claims an invariant the
       code cannot hold (boundary test:
       test_unpersistable_replay_guard_is_surfaced_never_silent).
    c. RE-ACCEPTED (warn, conditional): the OpenAPI staleness override stands,
       on the reviewer's stated condition that the snapshot is re-typed when
       the atp_api surface next churns.
    d. FIXED (info): the dashboard affordance now counts FAILED per-order
       outcomes and renders "WITH FAILURES: n ... inspect kill-switch status"
       in error tone instead of clean-looking counts; full per-phase status
       feedback remains UI-4.

Gate run (2026-07-05, post-rebase onto e8cc4bc):
  ruff check . clean; cargo fmt --check clean workspace-wide; cargo clippy
  --workspace -D warnings clean; pytest -m "not integration and not e2e" 2992
  passed / 0 failed; cargo test --workspace 1437 passed / 0 failed; all 18
  architecture/contract check tools PASS. run_ci_locally.sh cannot exit 0 on
  current main regardless of this branch: tests/domain/test_coverage_gate_domain.py
  (byte-identical to origin/main) fails the ruff-format gate and the repo-wide
  mypy baseline is red — both PRE-EXISTING on main (documented; integrate skips
  mypy; the sibling file is not this branch's to reformat).

Resume / next:
- Classification serialized. What flips passes:true (kill_switch_activation_contract
  .deferred[]): SRS-EXE-006 real IB transport behind KillSwitchBrokerageControl (+ a
  disconnect seam on the adapter transport), SRS-EXE-001/EXE-005 live state producers,
  SRS-EXE-002 hosting real paper strategies on fleet-registered gates, SRS-NOTIF-001
  email/SMS, UI-4 rich control. Operator verifies the live path (or the fault-injection
  scenario battery over the live scaffold) and flips via verified evidence.
- ERR-8 and SRS-API-001 are blocked-on SRS-SAFE-001 — they unblock at flip, not at
  this serialized merge.
- Do NOT rebuild: the gate/fleet/CLI/atp_safety are done; wiring a live backend is a
  composer swap (wire_kill_switch(backend=<live>)), not a rebuild.
