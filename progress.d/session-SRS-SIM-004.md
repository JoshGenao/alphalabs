=== SESSION SRS-SIM-004 ===
Date: 2026-07-10
Feature: SRS-SIM-004 — persist paper strategy simulation state (SyRS SYS-89; StRS SN-1.29 / SN-2.05)
Outcome: serialized (code on main, passes:false — the container-restart timing needs the unbuilt
         SRS-EXE-002 orchestrator + SYS-89 container lifecycle, which cannot run in parallel)

Prior state: a June commit (646bb11) landed the IN-MEMORY codec only — PaperStateSnapshot{schema,
  config, book(ledger)}, PersistenceConfig (60s/30s + hard 30s ceiling + mandatory shutdown),
  serialize/deserialize/restore, FNV-1a checksum, fail-closed atomic restore, 9-variant
  PersistenceError — plus L3/L5/L7 + tools/sim_persistence_check.py + the sim_persistence_contract
  block. Nothing reached disk; the metrics/user-state/pending sub-states were reserved empty slots.

Operator decisions this session (AskUserQuestion): (1) BUILD the disk layer + serialize (not
block-on-EXE-002); (2) capture LEDGER + USER-STATE + METRICS (the broad scope).

What I did (SCHEMA_VERSION 1 -> 2):
- paper_state.rs: added the atomic on-disk store (save_to_path/load_from_path: scratch<pid>.<seq> ->
  fsync -> rename -> parent-dir fsync, missing-dir/missing-file fail closed with new PersistenceError::
  Io), the SYS-89 30s restore-deadline enforcement (PersistenceConfig::restore_within_deadline +
  recover_from_path timing the load with Instant, new PersistenceError::RestoreDeadlineExceeded, new
  RecoveryOutcome), and EXTENDED the snapshot to capture metrics (per-strategy PaperMetricsAccumulator)
  + user-state (per-strategy opaque JSON object, is_json_object-validated, JsonValidator ported from
  live_state). capture() stays ledger-only (empty extras, back-compat); capture_full() captures all
  three. deserialize re-validates each sub-state fail-closed; pending-orders stays the one reserved
  slot (UnsupportedSection on non-zero). Reused the backtest_store/live_state atomic recipe verbatim;
  no serde, integer money only.
- paper_metrics.rs: added pub(crate) from_components (fail-closed re-validation of the accumulator's
  construction invariants on restore — positive baseline, monotonic trade log, increasing equity
  curve, coherent last-fill/last-mark cursors, per-fill field invariants; new InconsistentSnapshot
  variant) + last_mark_ts/last_fill_ts accessors; derive PartialEq+Eq so the snapshot stays Eq.
- NEW src/bin/sim004_persist_cli.rs (+ Cargo [[bin]]): persist/restore/roundtrip subcommands over a
  deterministic 2-strategy fixture; roundtrip --inject {missing-dir|corrupt-file|truncated|tampered-
  checksum|deadline-exceeded|non-json-user-state} each fails closed (exit 1, no survival line).
- Tests: extended L5 srs_sim_004_paper_state.rs (disk round-trip, deadline pass/overrun, metrics+
  user-state exact round-trip, non-object user-state fail-closed, determinism, atomic-no-scratch);
  NEW L5 srs_sim_004_persist_cli.rs (cross-process persist->restore survival, every fault fails closed,
  cross-process byte-determinism, parse rejections); 7 from_components unit tests in paper_metrics.rs;
  extended L7 tests/domain/test_paper_state_persistence.py + L3 tests/test_sim_persistence_contract.py.
- Check tool + architecture: 5 new collectors in sim_persistence_check.py (disk/deadline/metrics/
  user_state/cli) + cargo smoke now runs both L5 tests; sim_persistence_contract block updated
  (fields, 11 error variants, SCHEMA_VERSION=2, one reserved slot, new sub-blocks, honest deferred[]);
  repo-wide doc-drift sweep (architecture_check.py summary, lib.rs paper_state/paper_metrics docs,
  metrics.rs BT-004 doc, BT-004 contract "still empty until an accumulator exists" line).

What I tested (per step): Step 1: PASS — ./init.sh -> Environment ready (sim_persistence gate green).
  Step 2 (fault injection): PASS — ran persist/restore/roundtrip happy paths (cross-process survival,
  state-matches-capture:true, restore-elapsed-ms:0) + every --inject fault (all fail closed, exit 1,
  no survival line) + parse rejections. Step 3: PASS — cargo test -p atp-simulation (302 lib + 18
  srs_sim_004_paper_state + 5 srs_sim_004_persist_cli) + cargo test --workspace (0 failed) green;
  pytest -m "not integration and not e2e" 3252 passed / 4 pre-existing skips (incl. L3 60 + L7 22);
  sim_persistence_check.py --require-cargo PASS; architecture_check exit 0; cargo fmt --check +
  clippy --workspace -D warnings clean; run_ci_locally.sh green. Step 4: recorded here; passes:false.

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — 0 findings.
  judgment (adversarial_review.py origin/main, reviewer=codex): APPROVE on round 4 (0 findings).
    The loop converged over 4 rounds, each a real in-scope fail-open/DoS fix (never a scope dispute):
    - R1 BLOCK: metrics-restore trusted persisted cash -> from_components now RECONCILES cash against
      the trade log (-(qty*price)-costs per fill); a checksum-valid cash-inconsistent snapshot fails closed.
    - R2 BLOCK: (a) v1 schema bump stranded old snapshots -> v1 MIGRATION (empty new sub-states);
      (b) invalid user-state could poison the store -> validated at the save_to_path WRITE boundary.
    - R3 BLOCK: (a) untrusted record counts pre-allocated (OOM/panic) -> read-as-you-go, no with_capacity;
      (b) recursive JSON validator could stack-overflow -> MAX_JSON_DEPTH bound. Both fail closed typed.
    - R4 APPROVE.

Resume / next: SRS-SIM-004 stays passes:false (serialized). To FLIP: wire the live 60s persistence
  timer + real container-restart-within-30s into the SRS-EXE-002 orchestrator / SYS-89 container
  lifecycle and capture that e2e evidence (operator / verified-e2e). Remaining deferred sub-state:
  pending simulated orders (no runtime pending-order store — SRS-SIM-001/002). The user-state dict is
  persisted/restored here but the Python WRITER (strategy-API get_state/set_state, SRS-SDK) is deferred.
  Don't rebuild the mechanism — it's done and disk-backed; wire the container lifecycle.
