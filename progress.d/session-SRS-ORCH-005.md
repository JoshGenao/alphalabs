=== SESSION SRS-ORCH-005 ===
Date: 2026-07-06
Feature: SRS-ORCH-005 — support rollback to the previous deployed strategy version (SyRS SYS-80 / NFR-S2)
Outcome: serialized — rollback semantics + CLI/REST surfaces landed and solo-verified; the AC's
DASHBOARD leg (and browser-automation evidence) is the deferred SRS-UI-001 control, so passes stays
false until the operator verifies that leg (or UI-001 lands it).

CONTEXT: The CLI `strategy rollback` command and the REST lifecycle route were already fully DECLARED
on the frozen SRS-API-001 contract with transport-level confirmation guards (rest_server.py
action-level 428, cli_dispatch.py --confirm) and tested — but every behavior was a structured 501.
ORCH-004 gave DeployedVersionRegistry only record/lookup (current value; consumed only by test
spies): SYS-80's "retain the previous version" store and the rollback semantics did not exist.
runtime_services operator_workflow deferred[] assigned "target_version_hash resolution +
live-rollback confirmation guard" to this feature.

WHAT I DID:
- crates/atp-orchestrator/src/lib.rs: RetainedDeployedVersionRegistry — a SUPERTRAIT extension of
  the frozen ORCH-004 port (record/lookup unchanged; its pinned check untouched) exposing
  previous(); RetainedVersions{current, previous} (bounded ONE-deep — SYS-80 names "the previous
  version"); concrete in-memory RetainingVersionRegistry (current -> previous on record; a
  same-hash redeploy never becomes its own rollback target; poisoned lock = typed error).
  RollbackConfirmation — the NFR-S2 token, a STRUCTURAL mirror of live promotion's
  LiveDesignationConfirmation (same two private fields, sole from_operator constructor rejecting an
  empty acknowledgement, no Default) — deliberately a DISTINCT type: every workspace crate depends
  only on atp-types, and a shared token would let a rollback confirmation be replayed to designate
  live (cross-workflow replay is now a compile error); the parity is enforced against BOTH sources
  by the new check. StrategyOrchestrator::rollback — fail-closed gate, every guard before the
  single write: target wire-form validate -> lookup (NeverDeployed) -> previous (NoPreviousVersion,
  inert) -> exact-target match (TargetMismatch names the retained hash) -> live/confirmation
  (probe FAILURE refuses: LiveStatusUnavailable — unprovable live status never waives NFR-S2) ->
  record with a FRESH timestamp (a second rollback rolls forward); a record failure PROPAGATES
  (RegistryFailed — unlike launch's best-effort record, the write IS the rollback).
- crates/atp-orchestrator/src/bin/orch005_rollback_cli.rs (+[[bin]]): record/show/rollback over a
  magic-headed state snapshot (scratch write + fsync + atomic rename; tampered/foreign snapshot
  refuses the WHOLE load — never a silent "no previous version"); --acknowledge mints the
  strategy-bound token; --live/--degraded-live-probe are the demonstration probe (honesty-noted).
- python/atp_orchestration/ (NEW top-layer package, the mount_dashboard idiom): rollback_handler.py
  shells the bin (the repo's subprocess->Rust->parse-stdout boundary, store_history pattern);
  re-checks request.confirmed (defense in depth), transcribes the operator's confirm act into the
  strategy- and surface-naming audit acknowledgement, maps typed refusals onto the CLOSED interface
  categories (machine reason in detail: TARGET_MISMATCH 400, NEVER_DEPLOYED/NO_PREVIOUS_VERSION
  404, LIVE_ROLLBACK_UNCONFIRMED 428, LIVE_STATUS_UNAVAILABLE 500); LifecycleActionHandler serves
  ONLY action=="rollback" and delegates start/stop/restart to the honest 501 naming SRS-ORCH-004;
  mount_rollback(runtime, state_path=...) registers CLI "strategy rollback" + the REST lifecycle
  route (opt-in composition; the bare runtime keeps every 501/428 anchor byte-stable — no existing
  test changed EXCEPT one sibling fix below). Default live_strategy_provider reports no live
  strategy (the real source is deferred SRS-EXE-001/RESV; the transport 428 remains the enforced
  control on that path — documented in rollback_contract.deferred[]).
- tools/orchestrator_rollback_check.py: 5 static guards (retention_port, rollback_gate_order incl.
  static write-after-live-check ordering, confirmation_parity checked against BOTH designation.rs
  and lib.rs, rollback_cli, handler_surface) + cargo smoke; wired via the pytest wrapper
  tests/test_orchestrator_rollback_contract.py (ScriptRunTest + 11 non-vacuity mutations) — the
  SAME slot ORCH-004's check uses (runs inside the pytest step in BOTH ci.yml and run_ci_locally,
  so no loop drift).
- architecture/runtime_services.json (surgical splice): new rollback_contract block (sources,
  ports, gate order, refusal taxonomy, 4 deferred owners: dashboard control SRS-UI-001/UI-2; real
  live probe SRS-EXE-001/SRS-RESV-*; durable registry store; mount composition SRS-API-001);
  operator_workflow deferred[] SRS-ORCH-005 entry rewritten (semantics landed; remaining =
  composition + live probe + dashboard) + SRS-UI-001 entry extended to own the dashboard rollback
  control.
- tests/domain/test_strategy_rollback.py (L7, safety-paired): shells both cargo suites; bin e2e
  walk (unconfirmed live rollback refused with state byte-identical -> confirmed rollback ->
  roll-forward); mounted-runtime dispatch (REST 428 pre-handler / 501 for restart naming ORCH-004 /
  confirmed 200 with restored hash / CLI leg / mistarget 400 naming the retained hash) + ONE LIVE
  loopback HTTP request (the "dev server requests" evidence).
- Sibling fix: tests/test_orchestrator_deployment_version_contract.py
  test_bare_unit_record_signature_is_caught now replaces ALL occurrences of the pinned record
  signature — the concrete RetainingVersionRegistry legitimately carries a second textual copy, and
  the first-occurrence-only mutation left it standing (reading the guard as vacuous).

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh -> "✓ Environment ready".
  Step 2 (bash / file reads / dev server requests): manual bin walk (record v1/v2 -> unconfirmed
    live rollback exit 1 naming NFR-S2 -> confirmed rollback rolled-back-to v1@300 was-live:true ->
    show swapped pair); mounted-runtime REST/CLI dispatch incl. a live loopback HTTP POST (domain
    test #3). Browser automation: NOT run — no dashboard control exists (deferred SRS-UI-001).
  Step 3 (AC): CLI + REST legs VERIFIED with confirmation parity (transport 428 everywhere +
    domain-level strategy-bound token for the live path, structurally mirrored to promotion's and
    check-enforced). DASHBOARD leg NOT built -> the honest serialized reason.
  Step 4 (evidence): cargo test --workspace 13 suites ok / 0 failed (orch_5_rollback_contract 8;
    orch_5_cli_fail_closed 6); pytest -m "not integration and not e2e" -> 2962 passed;
    orchestrator_rollback_check.py (incl. cargo smoke) PASS; operator_workflow_surface /
    operator_interface_runtime / cli / rest_api / architecture checks all PASS; clippy
    --workspace --all-targets -D warnings clean; cargo fmt --check clean; ruff clean.
Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (both commits).
  judgment: Codex on usage cooldown AND the adversarial_review.py `claude -p` fallback hit its own
  session limit (its "block" was just the limit banner — no verdict). Ran the sanctioned
  fresh-context SUB-AGENT critic (prompts/critic_prompt.md + independence prompt, read-only, 32
  tool calls, re-ran the cargo/pytest/check evidence itself): WARN with an auditable attack log —
  NO confirmation bypass, NO fail-open (probed: padded-action transport-guard skip is caught by the
  handler's confirmed re-check; unmapped bin stderr -> 500 closed; tampered/foreign/duplicate
  snapshots refuse; route hijack impossible — restart still 501 ORCH-004, STRATEGY_MANAGEMENT stays
  fully_served:false). 2 MEDIUM + 2 LOW, all FIXED in the follow-up commit:
  (1) [medium] fixed scratch path + no parent-dir fsync -> adopted the backtest_store pattern
      (unique <state>.tmp.<pid>.<seq> scratch + parent fsync) + a concurrent-saves bin test;
  (2) [medium] write/read asymmetry (record --strategy "" exit 0 bricked the snapshot) -> empty/
      whitespace ids refused at parse AND at save (write-side superset of the loader) + bin tests;
  (3) [low] handler passes no --observed-at so served-path timestamps share the fixed constant ->
      wording honestied in RollbackOutcome doc + rollback_contract (no gate logic branches on it);
  (4) [low] lifecycle-route implementation count is route-granular -> noted in the contract
      (verified: fully_served stays false, owner still named — no readiness over-claim).
Resume / next: operator (or SRS-UI-001) finishes the dashboard leg: a UI-2 strategy-management
  control (button + UI-4-style confirm modal) POSTing the confirmed lifecycle rollback route that
  mount_rollback serves, then browser-automation evidence -> flip via verified-e2e. Real
  live-designation probe wiring = SRS-EXE-001/SRS-RESV-*; durable registry store deferred (named in
  deployment_version_contract + rollback_contract); mount composition into a shipped main =
  SRS-API-001. Don't rebuild the gate/bin/handler — wire the probe + dashboard onto them.
