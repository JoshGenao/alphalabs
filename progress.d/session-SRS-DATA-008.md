=== SESSION SRS-DATA-008 ===
Date: 2026-07-01
Feature: SRS-DATA-008 — implement SSD-primary and NAS-archival tiered storage
Outcome: COMPLETE (flip candidate passes:true) — routed ALL ingestion through the tier + a
structural guard; pending final adversarial verdict + integrate.

CONTEXT: A prior session (2026-06-29) built the TieredStore substrate (crates/atp-data/src/tiering.rs)
through 8 Codex rounds and landed it SERIALIZED (passes:false). Two documented close blockers:
  (1) route ALL production ingestion through TieredStore (the AC's "all ingestion writes to SSD first;
      new data is synced to NAS" clause) — LOAD-BEARING.
  (2) a durable expected-keys MANIFEST/CATALOG (detect loss of already-archived/SSD-evicted data) —
      explicitly owned by SRS-DATA-018 (backup + validated recovery); NOT load-bearing for the AC as
      written (the tier already gives no-NAS-delete + archive-only-when-confirmed + documented §12.1),
      so kept deferred (documented as the honest scope bound on nas_superset_verdict). Codex accepted
      this deferral last session.
Operator (AskUserQuestion) chose the FULL bounded close.

WHAT I DID (this session — the close of blocker #1):
- crates/atp-data/src/lib.rs: DataLayer::ingest_market_records_tiered — the SINGLE validated + tiered
  market-data write surface (composes the UNCHANGED ERR-5 gate with TieredStore::ingest = SSD-first
  durable write, THEN NAS sync; refuses corporate-action COVERAGE via MarketIngestError::UnsupportedKind
  exactly like ingest_market_record; fails closed before any SSD write on quarantine). +
  TieredIngestionOutcome, MarketIngestError::Tier.
- crates/atp-data/src/tiering.rs: TieredStore::sync_ssd_to_nas_best_effort() — a DEGRADE-TOLERANT NAS
  sync returning NasSyncStatus (unreachable->Degraded, alias/broken->Failed, ready->Synced), reads SSD
  lock-free. The counterpart to sync_ssd_to_nas (which errors) for a caller that already committed SSD.
- ROUTED EVERY market-data ingest CLI to sync to NAS (so no SSD-only path):
    * data008_tier_cli ingest -> DataLayer::ingest_market_records_tiered (the encapsulated surface; adds
      the ERR-5 validation the tier's own CLI previously skipped).
    * data016_ingest_cli + data005_fundamental_cli -> KEEP their existing StoreLock-held SSD
      load-modify-save (preserving the SRS-DATA-016 idempotency + SRS-DATA-017 writer-serialization
      contracts, which statically assert on cmd_ingest's exact acquire->load->save text) and THEN call
      sync_ssd_to_nas_best_effort. NAS = --nas / ATP_NAS_DATA_DIR / a <dir>/nas default (a missing NAS
      degrades the sync; the SSD write stands). Backward-compatible: `--dir` unchanged, output format
      unchanged, fail-closed-on-missing-dir preserved -- so the ~12 sibling tests + 3 check tools that
      drive these CLIs as store-population tools stay green.
- tools/data008_tiering_check.py::check_ingestion_routing — the STRUCTURAL guard: (1) the tiered
  surface exists in lib.rs; (2) every market-data ingest bin's cmd_ingest syncs to NAS (marker:
  ingest_market_records_tiered OR sync_ssd_to_nas_best_effort); (3) an all-bins sweep flags any bin
  that persists via .save_to_path( WITHOUT a paired NAS sync -- the ONLY exception is data011_coverage_cli
  (corporate-action COVERAGE is an operator trust assertion the tiered surface refuses, not provider
  ingestion; durable backup of all catalogs incl. coverage = SRS-DATA-018). So a NEW ingest bin (or a
  real provider adapter, when built) that writes SSD-only trips the guard.
- architecture/runtime_services.json: tiered_storage_contract — added ingestion_routing block; updated
  cli_failed_exit_token (outcome.tier.nas_sync); dropped the resolved "route ingestion" deferred[0];
  rewrote the description (routing now enforced end-to-end + guarded).
- Tests: crates/atp-data/tests/srs_data_008_tiered_storage.rs +4 (validated-tiered SSD-first+NAS sync;
  quarantine fails closed BEFORE any SSD write; coverage refused; best-effort sync degrade->sync->fail).
  tests/test_data008_tiering_contract.py +IngestionRoutingTest (guard has teeth: SSD-only bin caught,
  new-rogue-bin sweep caught, missing-surface caught, save+best-effort-sync passes). 30 L3 cases green.
- NO edits to DATA-005/016/017 contracts or tests (Approach 2 preserves cmd_ingest's lock/load/save).

WHAT I TESTED (per AC step):
  Step 1 (init): ./init.sh -> "Environment ready".
  Step 2 (exercise via CLI/fixtures): e2e walk — data016 ingest --dir/--nas --init -> SSD-first + NAS
    synced (both 238B), inspect, reingest (inserted:0, bytes_identical:true); data016 bare --dir --init
    -> auto NAS <dir>/nas synced; data016 ingest --dir <missing> (no --init) -> exit 1 "store directory
    is missing or not a directory"; data005 ingest -> 8 records SSD+NAS synced, factor-input available:true
    earnings_yield/book_to_price; data008 ingest -> validated:2 nas_sync:synced, report ssd_hot_retention:
    satisfied nas_superset:satisfied.
  Step 3 (AC): data008_tiering_check PASS (incl. "ALL ingestion is SSD-first + NAS-synced" evidence);
    cargo test -p atp-data 15/15 tiered-storage cases.
  Step 4 (evidence): recorded above.
  Full gate: cargo test --workspace green; cargo clippy --workspace -D warnings clean; cargo fmt --check
    clean; ruff check/format clean; pytest 'not integration and not e2e' 2615 passed / 4 pre-existing
    skips; DATA-005/016/017 + architecture contract checks PASS. run_ci_locally.sh: green EXCEPT the
    PRE-EXISTING mypy debt (66 errors in 16 python/ modules I did not touch; ci.yml marks mypy
    continue-on-error -- documented).
  Rebased onto latest origin/main (siblings integrated EXE-004/NOTIF-001/MD-007 mid-session); diff vs
    origin/main = my 9 files only, 0 deletions; re-verified green post-rebase.

Critic verdicts:
  deterministic: APPROVE — 0 findings (feat commit + range + post-fix staged).
  judgment: Codex usage-limited (usage cap; retry ~12:54 PM) -> manual fresh-context adversarial review
    per prompts/critic_prompt.md via a subagent. Verdict WARN, 2 findings, BOTH FIXED (re-verified):
      - [warn] contract-description-false-claim: the top-level tiered_storage_contract.description
        overclaimed that all 3 bins use ingest_market_records_tiered (only data008 does; data016/005 use
        ingest_market_record + sync_ssd_to_nas_best_effort) -> FIXED: rewrote the description to match
        the ingestion_routing sub-block + code (Approach 2).
      - [info] guard-whole-file-marker-heuristic: the sweep matched a NAS-sync marker ANYWHERE in a
        bin's file, so a future bin persisting SSD-only in cmd_ingest but naming the marker in an
        unrelated fn (cmd_sync) could evade it -> FIXED: scoped the sweep to require the marker in the
        SAME cmd_ingest body that persists (whole-file only for bins with no cmd_ingest, coverage
        excepted) + an L3 test proving the evader is caught.
    The reviewer independently verified: SSD-first ordering in every path; fail-closed on SSD/config
    errors; degrade-vs-fail NAS taxonomy; best-effort sync re-pushes the whole SSD snapshot; ERR-5 gate
    intact; coverage exemption legitimate; no new broker/vendor deps; §12.1 present; no dishonest flip.

Resume / next: on complete integrate, SRS-DATA-008 flips passes:true and UNBLOCKS DATA-001/002/003/004/
005/006/009/010/017/018 + MD-006. Deferred (none load-bearing for the tiering property, all guard-
or owner-covered): the durable expected-keys MANIFEST/CATALOG (SRS-DATA-018); the real Databento/IB/
Sharadar/option-chain network adapters (SRS-DATA-001/003/006 -- the routing guard forces them through
the tier when built); cold-read failover (SRS-DATA-009); the eviction POLICY (SRS-DATA-010); real
1TB SSD / 20TB NAS capacity + network mount (NFR-SC2 / SRS-ARCH-004). SEPARATE follow-up (unchanged
from prior session, NOT on this branch): the init.sh Homebrew TA-Lib env fix so a fresh worktree's
.venv isn't empty on macOS.
