=== SESSION SRS-ORCH-005 ===
Date: 2026-07-31
Feature: SRS-ORCH-005 — support rollback to the previous deployed strategy version (SyRS SYS-80 / NFR-S2)
Outcome: complete — the DASHBOARD arm (the one deferred surface) is built, composed, and
browser-verified; all three AC surfaces are now real.

AC: "Rollback is available through dashboard, CLI, and REST API; rollback of the live strategy
requires the same confirmation control as live promotion."

CONTEXT: the 2026-07-06 session landed the rollback semantics (RetainedDeployedVersionRegistry
supertrait, the fail-closed StrategyOrchestrator::rollback gate, the NFR-S2 RollbackConfirmation
mirror of live promotion's LiveDesignationConfirmation), the `orch005_rollback_cli` operator bin,
and `python/atp_orchestration.mount_rollback` serving the CLI + REST arms. It classified
serialized because the AC's DASHBOARD leg was the deferred SRS-UI-001 control. SRS-UI-001 has
since flipped passes:true, so this session built that leg and closed the feature.

## What I did (2026-07-31)

1. **Composition** (`python/atp_dashboard/server.py`): `mount_default_dashboard` now calls
   `atp_orchestration.mount_rollback(runtime, state_path=deployment_state)` inside the existing
   `ATP_DEPLOYMENT_STATE` branch. ONE knob composes both arms off ONE snapshot — the file the
   SRS-UI-002 inventory READS is the file the control WRITES, so the surface can never be
   half-composed (a readable inventory with an inert control, or a live control over a snapshot
   nothing displays). Unset, NEITHER arm composes and the operation keeps its honest 501.
   This is what makes rollback genuinely "available through the dashboard" in the shipped
   `python -m atp_dashboard` entrypoint. Operator-authorized (AskUserQuestion 2026-07-31) over
   the alternative of leaving composition to SRS-API-001, which would have left the dashboard
   arm inert and forced another serialized outcome.
   No layering violation: `atp_dashboard` and `atp_orchestration` are peer top-layer operator
   surfaces (killswitch.py already imports `atp_safety`); `dependency_boundary_check.py` and
   `architecture_check.py` are both green.

2. **The control** (`assets/app.js`, `index.html`, `styles.css`): a per-row ROLLBACK button that
   is deliberately the SAME two-step arm-then-confirm affordance as UI-2's promote-live — same
   5 s arm window, same in-flight serialization, and the operator's confirm act rides the SAME
   `confirm` token on the contract route. That literalness IS the NFR-S2 parity claim.
   - **Mutually exclusive with promote**: both stage a live-state mutation, so arming either
     disarms the other and `controlsBusy()` makes every control inert while either is in flight.
     Two staged mutations at once would leave the operator unable to say which control a
     response belongs to. The other control's caption is reset ONLY if something was genuinely
     staged there — a prior REFUSED/confirmed outcome is still true and must not be wiped.
   - **Target = the retained PREVIOUS version**, read from the inventory's
     `previous_version_identifier` (already served by SRS-UI-002). A strategy with none renders
     a DISABLED control (SYS-80 is inert before a second deployment) rather than posting a
     request the gate would refuse NO_PREVIOUS_VERSION.
   - **Fail-closed success**: a rollback reads as done only when the response names the confirmed
     strategy AND carries `lifecycle_state: "rolled-back"` AND an evidenced
     `deployment_version_hash`. A bare 2xx, a differently-named strategy, or a missing hash all
     render UNRESOLVED. Refusals render verbatim with their machine reason.
   - The button is `.rollback__btn`, NOT `.manage__btn`: that class is the promote control's
     identity in 21 existing UI-2 selectors, several of them strict-mode `.click()` locators that
     a second match would break. The two share every style rule instead — parity is in the
     behaviour and the styling, not the class name.

3. **Contract sweep** (`architecture/runtime_services.json`): dropped the dashboard-control entry
   from `rollback_contract.deferred[]` (4 → 3), narrowed the composition entry to "runtime mains
   OTHER than the dashboard", added a `dashboard_control` block recording what landed, and
   retagged both `operator_workflow` entries (the SRS-ORCH-005 one and the SRS-UI-001 one that
   claimed ownership of the dashboard rollback control). Fixed the now-stale `rollback_handler.py`
   module docstring. Repo-wide grep confirms no stale ownership refs remain.

4. **Capability gate** (adversarial R1+R2 — see Critic verdicts): the control is actionable only
   when the server proves it serves the rollback ACTION. `mount_dashboard` binds
   `rollback_is_served(runtime)`, which resolves the bound lifecycle handler and requires its
   `serves_rollback` marker — route registration alone is NOT proof, because that route is shared
   with SRS-ORCH-004's start/stop/restart. The provider carries `rollback_available` on the REST
   snapshot and the WS summary; the client fails closed on anything but an explicit boolean true,
   and drops it on an unreadable/malformed inventory. A dashboard without the handler renders the
   control disabled and names why.

5. **New static guard** (`tools/orchestrator_rollback_check.py`): a sixth guard, `dashboard_arm`,
   pinning both halves (composition + control semantics: confirm token, action, target hash,
   inert state, fail-closed success, mutual exclusion) so the arm cannot silently regress to the
   read-only surface it used to be. Reached via the existing pytest wrapper, which already runs
   in BOTH ci.yml and run_ci_locally.sh — no new CI wiring needed. Two pinned guard-count
   assertions (contract wrapper + domain test) updated 5 → 6; five new non-vacuity mutations.

## What I tested (per step)

- Step 1: PASS — `./init.sh` → `✓ Environment ready` (worktree-local venv/target).
- Step 2: PASS — exercised all three AC surfaces as an operator would:
  - CLI: `orch005_rollback_cli record/show/rollback` (round-trip, roll-forward, tampered-snapshot
    refusal) via the domain suite.
  - REST: `runtime.dispatch_rest` → 428 unconfirmed, 200 confirmed with the restored hash,
    400 TARGET_MISMATCH, 501 for sibling lifecycle actions.
  - DASHBOARD: real Chromium against the production composition.
- Step 3 (acceptance criteria): PASS
  - `ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py -k orch_005` → **4 passed**
    (inert-without-previous-version; confirmation parity incl. exactly ONE POST to
    `/lifecycle?confirm=true` and the REAL handler restoring v1; refusals 428/500 plus three
    unevidenced-2xx shapes; promote↔rollback mutual exclusion both directions).
  - Full dashboard e2e (regression — the composition changed for every UI-1/UI-2 test):
    `ATP_RUN_E2E=1 pytest tests/e2e/test_dashboard_refresh.py` → **57 passed** in 181 s.
  - `pytest tests/domain/test_strategy_rollback.py` → **5 passed**, incl. the new
    `test_production_dashboard_composition_serves_the_rollback_surface`.
  - `pytest tests/test_orchestrator_rollback_contract.py` → **19 passed**.
  - `tools/orchestrator_rollback_check.py --skip-cargo` → SRS-ORCH-005 PASS, 6 guards.
  - `pytest -m "not integration and not e2e"` → **4413 passed**, 5 skipped.
- Step 4: PASS — evidence recorded; classified complete.

### First-run defect found (this feature's e2e had never been run)
The control originally sent `{"confirmed": true}` in the request body. The runtime's action-level
guard reads `confirm` (`_body_confirmed` / `?confirm=`), so every dashboard rollback 428'd. Fixed
the IMPLEMENTATION, not the assertion: the confirm token now rides `?confirm=true` on the route —
exactly where promote-live puts it, which strengthens the parity claim rather than working around
it. A second, test-only defect: the Playwright route glob needed a trailing `*` to absorb that
query string, otherwise the interception silently never fired and the real handler answered.

## Critic verdicts
  deterministic: APPROVE — no findings (every commit)
  judgment (adversarial_review.py, reviewer=codex): APPROVE after 3 blocking rounds.
    Both code findings were REAL and are fixed; neither was reachable by the
    deterministic critic or by any test I had written.

  R1 [high] "Rollback button is actionable without proving the handler is mounted."
    The control was rendered actionable from `previous_version_identifier` alone. But
    `mount_dashboard(..., inventory=...)` is a PUBLIC composition path that can serve rows with
    retained previous versions on a runtime that never mounted the handler — an operator-visible
    live-state control posting into the bare 501. My "can never be half-composed" claim only held
    for `mount_default_dashboard`. FIX: `mount_dashboard` (the one path EVERY composition goes
    through) binds a capability probe; the provider reports `rollback_available` on the REST
    snapshot and WS summary; the control needs BOTH a served route and a retained previous
    version. Absent a probe → False (an unproven capability is not a capability).

  R2 [high] "Rollback control can enable against a non-rollback lifecycle handler."
    The probe read `registry.is_registered(REST_LIFECYCLE_OPERATION)` — but that key is the
    SHARED lifecycle route. SRS-ORCH-004's start/stop/restart handler, when it lands, would have
    flipped the capability true and enabled the control against a handler that still 501s on
    `action: rollback`. FIX: the capability is now ACTION-level —
    `LifecycleActionHandler.serves_rollback = True` plus
    `atp_orchestration.rollback_is_served(runtime)`, which resolves the BOUND handler and
    requires that marker to be exactly True. The question now lives in the owning package, so the
    dashboard never re-derives which handler serves which action.

  R3 [high] "Unrelated global test-harness change mixed into the feature diff."
    The branch opened with a prep commit adding an autouse `sys.modules`/`sys.path` sandbox to
    tests/conftest.py. Legitimate under the prompt's prep rule, but repo-wide, not load-bearing
    for this feature, and it contaminated the evidence (a green suite would partly depend on it).
    FIX: dropped from this branch (`rebase --onto origin/main`) and the full suite re-verified
    WITHOUT it. The fixture remains uncommitted in the primary checkout for its own session.

## Resume / next
SRS-ORCH-005's AC is met on all three surfaces; the feature closes. Two items stay honestly
deferred in `rollback_contract.deferred[]` and NEITHER is required by this AC:
  1. The REAL live-designation probe (SRS-EXE-001 / SRS-RESV-001..006). Until it lands the
     handler's default `live_strategy_provider` reports no live strategy — but the transport
     guard 428s EVERY unconfirmed rollback, which is strictly STRONGER than the AC's live-only
     requirement, and `live_strategy_provider` is injectable so the live path is exercised by the
     gate/bin tests. When EXE-001 lands, inject the real provider — do NOT rebuild the control.
  2. The durable deployed-version registry store (`RetainingVersionRegistry` is in-memory; the
     bin's fsync'd magic-headed snapshot is its demonstration port).
Composition of `mount_rollback` into runtime mains other than the dashboard remains SRS-API-001's.
