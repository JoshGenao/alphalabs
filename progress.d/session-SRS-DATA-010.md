=== SESSION SRS-DATA-010 ===
Date: 2026-07-26
Feature: SRS-DATA-010 — evict SSD cache data according to the configured storage policy (SyRS SYS-69; P1)
Outcome: serialized (passes stays false) — operator-authorized integrate over a Codex block that
         demands deferred-runtime wiring (see "Adversarial review" + "Resume / next")

What I built (all provable solo with fixtures; verification method is "Test"):
- crates/atp-data/src/eviction.rs — the POLICY brain over the DATA-008 tier + DATA-009 cold-read
  primitives:
    * StoragePolicy (high-water default 80%, recency window default 24h/86400s); target =
      floor(ssd_capacity_records * high_water / 100), INTEGER arithmetic (no float); fail-closed on a
      0/>100 high-water or a negative recency.
    * plan_eviction() — a PURE planner over the whole SSD inventory (cold-cache Tier::Cold + SSD-hot
      Tier::Hot). Pinned (never evicted) = live-strategy symbols (AC-2) ∨ recently-accessed-within-
      window (AC-3) ∨ hot records inside the DATA-008 90-day retention floor. Evictable ordered
      most-removable-first: Cold-before-Hot (SYS-68, via derived Tier Ord), non-listed-before-listed
      (SYS-69), oldest-event_ts-first (SYS-69 "by age"), then key. Selects exactly (usage - target);
      if pinned data blocks the mark it STOPS (reached_target=false) rather than evict pinned (fail-safe).
    * EvictionEngine::enforce() — physically evicts ONLY the cold-read cache (rewrites cold_cache_dir
      under StoreLock); NEVER opens the SSD primary for writing, so live/recent/hot data is
      STRUCTURALLY un-evictable (mirrors DATA-009 evict_cold_cache_to). Hot-tier plan entries are
      reported as hot_pressure_deferred, never removed (physical hot-pressure eviction = declined
      "full hot-pressure" scope).
- crates/atp-data/src/access_journal.rs — the real AC-3 recency PRODUCER: AccessRecorder trait +
  NoopRecorder default + durable append-only AccessJournal. WRITES FAIL OPEN (record→append, discarded
  — a journal-write error never breaks a backtest/factor read); READS FAIL CLOSED (a corrupt complete
  line → AccessJournalError::Corrupt so eviction refuses; a torn tail is tolerated via complete_lines).
  ensure_usable() = a fail-closed writability preflight for a caller that trusts the journal.
- Additive, behavior-preserving instrumentation (existing read fns byte-identical):
    * crates/atp-simulation/src/store_bar_source.rs — RecordingBarSource decorator over StoreBarSource.
    * crates/atp-factor-pipeline/src/store_inputs.rs — assemble_factor_inputs_recorded +
      run_scheduled_factor_job_over_store_recorded (the canonical scheduled factor path made
      recency-recording).
- crates/atp-data/src/bin/data010_eviction_cli.rs — operator CLI: report / plan / enforce. Hand-rolled
  allowlist parser, key:value output. enforce REFUSES without an explicit protection source
  (--protection-inputs / --use-journal / --assume-unprotected), --use-journal fails closed on an
  unusable journal (ensure_usable), and enforce exits NON-ZERO when the mark can't be met without
  evicting pinned/hot data.
- Evidence: architecture/runtime_services.json `storage_eviction_contract` block; tools/
  data010_eviction_check.py (emits "SRS-DATA-010 STORAGE-EVICTION PASS"); tests/
  test_data010_eviction_contract.py (real source passes + 18 mutation-catch spot-checks).

What I tested (per step):
- Step 1: PASS — ./init.sh → "✓ Environment ready".
- Step 2: PASS — data010_eviction_cli report/plan/enforce driven end-to-end over a fixture SSD +
  cold-cache built >80% full (crates/atp-data/tests/srs_data_010_eviction.rs, L4; also manual CLI runs).
- Step 3 (AC): PASS (with fixtures) —
    * L1 unit (eviction.rs, access_journal.rs): 26 tests — ranking (cold-before-hot, non-listed-first,
      oldest-first), target math, live/recent/retention pinning, fail-safe residual breach, journal
      round-trip/window/running-filter/torn-tail/corrupt/ensure_usable.
    * L4 boundary (srs_data_010_eviction.rs): 4 CLI e2e tests — usage→≤80%; oldest non-listed evicted
      first; LIVE + 24h-recency (via the real journal) symbols retained; hot store byte-identical;
      fail-safe non-zero when blocked; corrupt-journal fail-closed.
    * L7 domain (tests/domain/test_data010_eviction_safety.py): 7 CLI tests — hot data never evicted +
      breach reported; fail-closed gate; unusable/corrupt journal fail-closed; degenerate high-water
      rejected.
    * L2 property (tests/property/test_data010_eviction_invariants.py): 5 Hypothesis properties over
      the CLI input boundary (well-formed accepted / malformed rejected, for protection files + journals).
    * Instrumentation: RecordingBarSource + assemble_factor_inputs_recorded + the recorded scheduled
      factor run (8,000-universe) all record journal entries AND match the bare path byte-for-byte.
  Full gate: cargo test --workspace + pytest "not integration and not e2e" + cargo fmt --check + cargo
  clippy --workspace -D warnings + run_ci_locally.sh all green. Edits to closed-green files are
  ADDITIVE ONLY (0 deletions vs origin/main, except one reformatted import line).
- Step 4: passes stays FALSE (serialized) — end-to-end AC-2/AC-3 need the deferred runtime (below).

Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (adversarial_review.py, reviewer=codex): BLOCK, twice, on the SAME residual —
    (1) production read paths don't yet populate the journal, so a real running job leaves no recency
        evidence; (2) journal write failures are silently converted into missing protections.
    Round 1 fixes applied (both genuinely in-scope): added run_scheduled_factor_job_over_store_recorded
    (canonical scheduled factor path now records + tested at 8,000 universe) and AccessJournal::
    ensure_usable() wired into enforce --use-journal (fail-closed on an unusable journal).
    Round 2 re-block: the RESIDUAL is deferred-feature work — there are ZERO production callers of the
    factor/backtest read paths today, and every feature that would supply them is passes:false
    (SRS-FAC-001/BT-001 job runtimes; SRS-EXE-001/RESV running-job registry + live-symbols;
    SRS-NOTIF-001 write-failure alerting). The whole recency loop can only close once that runtime
    lands. OPERATOR OVERRIDE (AskUserQuestion 2026-07-26): authorized integrate --mode serialized,
    treating the block as the documented deferred-runtime boundary. Mitigating fact: enforce is
    STRUCTURALLY cache-only, so mis-use evicts only a recoverable cold-cache copy → a transparent
    DATA-009 NAS re-fetch, never data loss or a correctness violation.

Resume / next (what flips passes:true — the named deferred owners):
- AC-2 real live-strategy→symbols feed: SRS-EXE-001 / SRS-RESV-* (today a --live stub; LiveStrategyState
  carries no symbols). The policy consumes an injected live-symbol set (the seam; CLI --protection-inputs).
- AC-3 production recording + running-job scoping: route the PRODUCTION scheduled/backtest run paths
  through run_scheduled_factor_job_over_store_recorded / RecordingBarSource (needs SRS-FAC-001/BT-001 to
  actually run jobs), and feed enforce's running-job set from the atp-orchestrator WorkloadRegistry
  (SRS-EXE-001). The recording SUBSTRATE + entry points are BUILT and tested here.
- Journal write-failure supervision (persistent, transient-loss case): surface a running job's
  recording failures to the operator → SRS-NOTIF-001 + the orchestrator WorkloadRegistry.
- Optional hardening the operator deferred: require --running-jobs for a destructive
  enforce --use-journal (Codex's alternative; defense-in-depth, does not itself close the empty-journal gap).
- Real SSD byte capacity: NFR-SC2 (cap/usage are in the store's record unit, the DATA-009 fixture proxy).
Do NOT rebuild the policy/CLI/journal/instrumentation — wire the producers when the runtime lands.
