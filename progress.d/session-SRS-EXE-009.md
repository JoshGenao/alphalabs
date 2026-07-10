=== SESSION SRS-EXE-009 ===
Date: 2026-07-09
Feature: SRS-EXE-009 — durably commit live order intents to a write-ahead OUTBOX
before IB submission, and use the durable record to reconcile broker state on
restart (SyRS SYS-90 / NFR-R3 / NFR-R4; StRS SN-2.05).
Outcome: serialized (code on main, passes stays false pending real-IB e2e)

CONTEXT / scope boundary (pinned by the codebase):
SRS-EXE-005 (crates/atp-execution/src/live_state.rs) snapshots already-known live
state (broker IDs, fills, positions, equity) and EXPLICITLY defers the crash
window between the durable intent commit and the IB submission to EXE-009
(live_state.rs:34-37,66-67; runtime_services.json order_lifecycle + live_state
`deferred[]` both name "the SRS-EXE-009 durable outbox — write-ahead intent commit
+ acknowledged-broker-ID reconciliation for the submit-crash window"). EXE-009 is
also a foundational prerequisite: SRS-EXE-008 (order-lifecycle idempotency) is
blocked-on EXE-009. The atp-types order-lifecycle primitives EXE-009 reuses
(OrderKey, OrderState/is_terminal, OrderLifecycle, ClientCorrelationId) already
exist on main, so no dependency was blocked.

WHAT I DID:
- crates/atp-execution/src/outbox.rs (new, the deliverable):
  * OrderOutbox: commit_intent (write-ahead — durable PENDING_SUBMIT record BEFORE
    submission, idempotent DuplicateClientCorrelationId rejection), bind_ack
    (PENDING_SUBMIT->ACKED + records broker id; fail-closed on unknown/blank/
    conflicting id), observe_state (graph-validated lifecycle transitions),
    prune_terminal (retention: entries kept until FILLED/CANCELLED/REJECTED/EXPIRED).
  * OutboxSnapshot durable codec mirroring live_state.rs: own MAGIC
    (ATP-ORDER-OUTBOX-V1) + schema version, FNV-1a checksum, length-prefixed
    framing, fsync -> atomic rename -> parent-dir fsync save, fail-closed load, and
    a write<->read broker-binding-consistency guard (a FILLED intent with no id, or
    a PENDING_SUBMIT carrying one, fails closed).
  * reconcile(&OrderOutbox, &BrokerOpenOrderSnapshot) -> ReconciliationPlan — PURE,
    fail-closed. Bound intent -> skip_bound (NEVER resubmit; a different broker id ->
    unresolved BrokerIdMismatch). Unacked + broker-has-it -> adopt_ack. Unacked +
    absent + OpenAndRecentlyCompleted -> resubmit. Unacked + absent + OpenOnly ->
    unresolved UnverifiableSubmitWindow (never auto-resubmitted on a partial view).
    A legally-reachable broker terminal state -> mark_terminal. Port
    BrokerOpenOrderSource (concrete IB impl deferred) defined at the execution layer.
  * DUPLICATE-BROKER-ROWS guard: reconcile groups broker rows by key (not collect())
    so two rows for one correlation key are surfaced as DuplicateBrokerRows, never
    collapsed into a single adopt/skip that could mask a second live order.
  * Plan buckets are MUTUALLY EXCLUSIVE: classify_terminal_sync decides before
    committing to a bucket, so a StateDisagreement goes ONLY to unresolved (never
    also skip_bound/adopt_ack).
  * BrokerReconcileError is a TYPED enum (ConnectivityBlocked / StaleData / Timeout /
    MalformedSnapshot / Unavailable) with a category() wire string.
- crates/atp-execution/src/lib.rs: ExecutionEngine::submit_live_order_durably — the
  durable-submit SEAM (write-ahead commit + persist BEFORE broker.submit_order; bind
  the ack; mark a synchronous rejection REJECTED so it is never resubmitted; a crash
  between commit and ack leaves PENDING_SUBMIT for reconcile). Does NOT modify the
  pinned submit_live_order. + DurableSubmitError {Rejected|Persistence|Outbox}.
  Proven by srs_exe_009_durable_submit.rs incl. an ORDERING test (the broker stub
  asserts the outbox snapshot exists on disk before it is called).
- crates/atp-execution/src/bin/exe009_outbox_reconcile_cli.rs (+ [[bin]]): operator
  + fault-injection harness (persist -> reload = restart) with subcommands
  write-ahead / restart-skip-bound / restart-adopt / restart-resubmit / retention /
  broker-error and --inject duplicate-replay|id-conflict|partial-coverage proving
  the fail-closed properties. Allowlist arg parser, kv proofs, ExitCode.
- architecture/runtime_services.json: new outbox_reconciliation_contract block +
  tools/outbox_reconciliation_check.py (word-boundary decl matching) wired into
  tools/architecture_check.py, deferred[] naming EXE-006/EXE-001/EXE-008.
- Design decision: did NOT touch the pinned submit_live_order/route_order ERR-1/2/3
  signatures (their sole-entry rework is deferred to EXE-006/ORCH). EXE-009 ships the
  substrate + integration seam the deferred owners call.
- NOTE (follow-up): extending SAFETY_PATH_RE in tools/critic_check.py to recognize
  the outbox as a safety path was PREPARED then DROPPED — the adversarial reviewer
  policy auto-blocks any critic-gate self-modification. Route that one-line regex
  change through separate human review of the critic gate (it is defense-in-depth
  only; this feature already lands with its paired tests/domain/ test).

WHAT I TESTED (per step):
- Step 1 (./init.sh): PASS — "Environment ready" (background build exit 0).
- Step 2 (fault-injection w/ mocked IB): PASS — CLI subcommands + --inject faults
  all fail-closed; `exe009_outbox_reconcile_cli restart-skip-bound` -> resubmit:0,
  `--inject partial-coverage` -> no-resubmit-on-partial-view:true, etc.
- Step 3 (AC verification): PASS solo —
  * bullet 1 (durable before submit): srs_exe_009_write_ahead_intent_is_durable...
  * bullet 2/3 (bound not resubmitted / adopt): srs_exe_009_bound_intent_not_...,
    _unacked_intent_adopted_..., _id_conflict_never_resubmits.
  * bullet 4 (retention): srs_exe_009_retained_until_terminal.
  * fail-closed: corrupt/missing snapshot, open-only ambiguity.
  Rust: cargo test -p atp-execution — 17 lib (incl. a 3000-case seeded property test:
  "a bound intent is never resubmitted" + durable round-trip identity), 10
  fault-injection integration, 10 CLI. Python: tests/domain/test_outbox_reconciliation.py
  (12) + tests/test_outbox_reconciliation_contract.py (23, w/ negative spot-checks).
- Full gate: cargo fmt --check clean; cargo clippy --workspace -D warnings clean;
  cargo test --workspace green; pytest "not integration and not e2e" -> 3062 passed,
  3 pre-existing skips; tools/architecture_check.py PASS (outbox evidence emitted).
- Step 4: passes stays FALSE (serialized) per the feature's own step 4.

Critic verdicts:
  deterministic (critic_check.py --range origin/main..HEAD): APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex): converged over rounds —
    R0: claude-fallback TOOLING FAILURE (codex-unparseable + API ConnectionRefused,
        verdict dropped fail-open) — NOT a real finding; re-ran.
    R1: codex BLOCK meta:critic-self-modification — resolved by DROPPING the
        SAFETY_PATH_RE prep commit (routed to separate human review), not overridden.
    R2: codex BLOCK high "duplicate broker rows overwritten" — FIXED (DuplicateBrokerRows
        conflict + tests).
    R3: codex BLOCK — (1 critical) live path bypasses the outbox; (1 high) untyped
        broker error; (1 medium) contradictory plan buckets. FIXED the high+medium
        (typed BrokerReconcileError; mutually-exclusive buckets via classify_terminal_sync)
        AND built the durable-submit SEAM + ordering test.
    R4: codex BLOCK high "post-broker persist failure looks like pre-broker" — FIXED:
        split DurableSubmitError into WriteAheadPersistence / AckNotDurable{receipt} /
        RejectionCleanupFailed + staged-clone transactional writes + fault-injection tests.
    R5: codex BLOCK high "durable API trusts caller-supplied mode" — FIXED: made
        submit_live_order_durably pub(crate) + no mode param; added authority-gated pub
        route_order_durably (derives from self.designation), mirroring route_composite_order.
    R6: codex BLOCK critical "invalid durable submissions poison restart recovery"
        (a persisted invalid order fails the fail-closed restore, bricking the whole
        outbox) — FIXED: validate() BEFORE commit_intent/persist; regression test proves
        a valid entry still recovers after an invalid order is rejected.
    R7 (final confirmation): codex BLOCK with ONLY the critical "production live orders
        can still reach IB without an outbox record" — the poison-recovery critical is
        RESOLVED; all in-scope findings are fixed. The sole residual is the deferred
        boundary below.
    RESIDUAL (all rounds): the PRODUCTION route_order still delegates to the non-durable
        submit_live_order (does not route through route_order_durably). This is the
        deferred EXE-001 pinned-contract rework (blocked on EXE-006); codex itself
        recommends "keep serialized." OPERATOR AUTHORIZED integrate --mode serialized
        past this deferred-boundary block (2026-07-09) — NOT a faked approval.
  ENV NOTE: after rebasing onto origin/main, `pytest` collection needs `cryptography`
    (a sibling SEC vault dep not in the worktree venv — init.sh skips requirements-dev);
    `pip install cryptography` fixes it. Not caused by this feature.

COMPLETENESS: serialized. Solo-verified with a mocked broker: outbox durability,
write-ahead commit, reconciliation decision logic, retention, every fail-closed edge,
AND the durable-submit seam (submit_live_order_durably) with an ordering test.
DEFERRED (real IB / blocked features): the concrete BrokerOpenOrderSource querying
IB open+completed orders (EXE-006, serialized); wiring the durable-submit SEAM into
the PRODUCTION route_order call site so every live submission consults the outbox
(EXE-001, blocked — it re-architects the pinned single-live authority path);
event-driven transitions + full lifecycle idempotency (EXE-008, blocked-on this
feature); the real-IB restart reconciliation e2e within the NFR-R3 60s window.

RESUME / NEXT: Do NOT rebuild outbox.rs. To flip passes:true, an operator runs the
real-IB restart reconciliation e2e (a live execution engine restarted mid-submit
reconciling against a live IB open+completed-orders query) — the verified-e2e path.
EXE-009 unblocks EXE-008 (order-lifecycle idempotency); EXE-008 should wire bind_ack/
observe_state to real broker events and commit_intent into the live submit path.
Optional hardening: the dropped SAFETY_PATH_RE outbox extension (needs separate
human review of the critic gate).

--- DE-CHURN ADDENDUM (2026-07-10) ---
This feature was RE-OFFERED by the scheduler despite being code-complete+serialized
on main. Root cause: (1) tools/feature_deps.json had NO blocked_on entry for
SRS-EXE-009, so it read as a ready/claimable feature; (2) serialized_notes() reads
the MAIN-REPO checkout (ROOT/progress.d/), which lagged origin/main and so could not
see this very note → EXE-009 was not parked in the awaiting_verification bucket.
passes:false + no dep + invisible note ⇒ re-offered forever.
ACTION: recorded `block SRS-EXE-009 --on SRS-EXE-001 SRS-EXE-006` (cycle-safe;
mirrors the EXE-008→[EXE-001,EXE-006,EXE-009] edge and the DEFERRED owners named
above). Landed via `integrate --mode partial`; passes stays FALSE.
The code is UNCHANGED — do NOT rebuild outbox.rs / the CLI / the durable-submit seam.
When EXE-001 (production route_order wiring) and EXE-006 (concrete IB
BrokerOpenOrderSource) are passes:true, EXE-009 re-enters the pool; flip it only via
the operator real-IB restart-reconciliation e2e (verified-e2e), never solo.
SYSTEMIC (operator): the lagging main-repo checkout also hides SEC-002/SDK-007/
DATA-012 notes → same churn on those; a `git -C <main-repo> pull --ff-only` (or having
integrate refresh the main checkout) prevents this class globally.
