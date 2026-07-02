=== SESSION SRS-EXE-005 ===
Date: 2026-07-02
Feature: SRS-EXE-005 — persist live strategy state for restart recovery and
re-execute the warm-up on restart (docs/SRS.md line 149; SyRS SYS-90, NFR-R3;
StRS SN-2.05 / SN-1.01 / BG-1).
Outcome: SERIALIZED (code merged, passes stays false) — operator-authorized
(AskUserQuestion) integration with the composite/option-recovery scope deferred.

WHY SERIALIZED: the AC's full proof is fault-injection over a real 60s container
restart (a live strategy container with warm-up rebuilding real indicator buffers)
— not runnable solo/in-parallel. The deterministic substrate + fault-injection
demonstrations land; passes stays false pending that operator e2e.

CONTEXT: atp-types `order_lifecycle.rs` (SRS-EXE-008) already built the in-memory
OrderLedger idempotency authority and EXPLICITLY deferred "durable persistence of
the ledger across a process restart" to SRS-EXE-005. So EXE-005 is the durable
snapshot + restart-recovery substrate. It mirrors the SRS-SIM-004 paper analogue
(`paper_state.rs`, also passes:false).

WHAT I DID:
- NEW crates/atp-execution/src/live_state.rs — the durable live-execution-state
  substrate:
  * LiveExecutionState: the full enumerated state — the OrderLedger (pending
    submissions / awaiting acks / order statuses / correlation IDs), broker IDs,
    fill events, open positions (symbol->qty), account equity snapshot, the
    user-accessible JSON state dictionary, AND a live_strategies set (the warm-up
    target, tracked independently of orders). Every `with_*` fails closed on an
    inconsistency (broker id / fill for an unknown order; duplicate fill identity
    (order,sequence); non-canonical/flat position; non-JSON-object user state).
  * LiveStateSnapshot: versioned envelope + a hand-rolled dependency-free codec
    (no serde in the workspace by design), mirroring paper_state — magic header +
    FNV-1a integrity checksum-first + byte-identical (canonical sort) serialize +
    fail-closed deserialize (no partial state). Atomic durable save (unique
    <pid>.<seq> scratch -> fsync -> rename -> parent-dir fsync). load_from_path
    FAILS CLOSED on a missing dir OR a missing snapshot file (a genuine first
    start initializes empty explicitly; recovery never silently restores empty).
  * Restart recovery: recover()/recover_from_path() enforce the NFR-R3 60s restore
    deadline (excluding warm-up) and re-execute the SRS-SDK-005 warm-up (via a
    WarmUpReexecutionPort) for the UNION of registered live_strategies + strategies
    owning a restored order, failing closed if any warm-up fails. The restored
    ledger keeps its (strategy, correlation id) idempotency keys, so a re-submission
    after restart is rejected as a duplicate — the AC's "without duplicate
    submissions".
- atp-types order_lifecycle.rs: added OrderLedger::orders_iter + restore_from
  (fail-closed: duplicate key / key-strategy mismatch / dangling replaces /
  live-cancel-replace-over-non-cancelled-original doubled-exposure) +
  OrderLifecycle::restore (snapshot rehydration, analogue of
  VirtualPosition::from_components). +2 OrderLifecycleError variants.
- Metadata: architecture/runtime_services.json live_state_recovery_contract with an
  honest deferred[] (5 entries incl. the composite deferral).

WHAT I TESTED (per step):
- Step 1: PASS — ./init.sh -> "✓ Environment ready".
- Step 2/3 (deterministic fault injection): PASS
  * atp-execution lib live_state: 31 tests (round-trip fidelity + byte-identical
    determinism; no-dup-after-restart; corrupt magic / checksum / truncation /
    trailing garbage / unknown schema / warm-up-disabled / zero-deadline fail
    closed; restored submission validation blank-symbol / non-positive qty /
    non-positive price; huge-length no-panic; duplicate-fill; non-JSON user state;
    JSON validator accept/reject; missing-file fail-closed; warm-up-for-registered-
    order-less-strategy).
  * atp-execution --test srs_exe_005_live_state_recovery: 13 integration tests
    (public API): round-trip / determinism / no-dup-after-restart / warm-up
    re-exec / deadline / warm-up-failure-aborts / corrupt / tamper / end-to-end
    disk restart no-dup / missing-snapshot fail-closed / warm-up for order-less
    registered strategy / duplicate-fill / user-state-json-object.
  * atp-types --test srs_exe_005_ledger_restore: 7 tests (orders_iter, restore_from
    fail-closed cases + rebuild-preserves-idempotency).
  * L7 domain tests/domain/test_live_state_recovery.py: 13 tests (shell out to the
    safety-relevant Rust subset).
- Step 4 (evidence; passes stays false): PASS. Full gate: tools/run_ci_locally.sh
  green; cargo test --workspace (0 failures); pytest "not integration and not e2e"
  2699 passed / 4 skipped; cargo fmt --check + clippy -D warnings clean;
  architecture_check + order_lifecycle_check exit 0.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings (safety paths
    order_lifecycle.rs + live_state.rs paired with tests/domain/).
  judgment (tools/codex_review.sh origin/main):
    R1 -> needs-attention [high]x2: (a) read_str length arithmetic could panic on a
      crafted huge length; (b) restore reconstructed OrderSubmission without
      OrderSubmission::validate. FIXED: checked_add + newline-bound before slice;
      submission.validate() on restore (blank symbol / non-positive qty / options
      fail closed) + regression tests.
    R2 -> needs-attention [high]x2: (a) warm-up skipped when recovered state has no
      orders; (b) missing snapshot file silently restored empty. FIXED: explicit
      persisted live_strategies set (warm-up = union with order-derived); load_from_path
      fails closed on a missing file (first start inits empty explicitly) +
      regression tests.
    R3 -> needs-attention [high] duplicate fill events restored as distinct
      executions; [medium] user state not validated as JSON. FIXED: (order,sequence)
      fill-identity uniqueness; with_user_state_json validates a JSON object via a
      compact dependency-free recursive-descent validator + regression tests.
    R4 -> needs-attention [high] the snapshot cannot represent live composite option
      orders (SRS-EXE-004) / per-contract option positions. DISPOSITION: genuine
      DEFERRED DEPENDENCY, not a current fail-open loss — SRS-EXE-004 is passes:false
      with a pending combo wire, the composite path (route_composite_order) is NOT
      ledger-tracked, and single-leg option orders fail closed at
      OrderSubmission::validate (re-checked on restore), so no live composite/option
      state exists to recover today. Scoped honestly (module docs + metadata
      deferred[], owners SRS-EXE-004 + SRS-EXE-008; SCHEMA_VERSION bumps when EXE-004
      goes live). Operator (AskUserQuestion) authorized the serialized integration
      with this deferral. Recorded honestly; no faked APPROVE.

Resume / next:
- SRS-EXE-005 stays passes:false. To flip: (1) operator runs the real 60s
  container-restart fault-injection e2e (a live strategy container restarted with
  warm-up reconstructing real indicator buffers); AND (2) the capture loop is wired
  into the live execution engine / orchestrator (periodic + on-shutdown); AND (3)
  producers of broker IDs / fills / positions / equity land (SRS-EXE-006 IB adapter
  + a live account sync). Then verified-e2e.
- Composite/option recovery is deferred to SRS-EXE-004 (live combo path) +
  SRS-EXE-008 (composite lifecycle tracking): when EXE-004 goes live, extend the
  schema (SCHEMA_VERSION bump) to carry CompositeOrderSubmission records + option
  positions keyed by OptionContractIdentity::canonical_key.
- SRS-EXE-009 durable outbox (write-ahead intent + broker reconciliation) remains a
  separate, stronger guarantee.
