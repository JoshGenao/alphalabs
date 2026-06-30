=== SESSION SRS-DATA-008 ===
Date: 2026-06-29
Feature: SRS-DATA-008 — implement SSD-primary and NAS-archival tiered storage
Outcome: SERIALIZED (passes:false). The tier SUBSTRATE is built + demonstrated solo
with fixtures + file inspection, but Codex R6 correctly showed the AC's cross-cutting
"all ingestion writes to SSD first; new data is synced to NAS" clause is NOT met end
to end — a raw ingest path (data016_ingest_cli) still writes a single store dir with
no NAS sync, and the real provider adapters are stubs. Per Step 4 ("leave passes
false until evidence proves the requirement end to end") this is foundational
substrate the DATA cluster composes, not a closed feature. (Initially classified
complete; reclassified serialized after the adversarial review.)

Session shape (self-scheduling): the scheduler first claimed ERR-7, then ERR-8,
then ERR-9 — all three are already-SDK-surface-sliced verification stubs whose
remainder needs unbuilt runtime + their own Step-4 says "leave passes:false until
evidence captured". Parked each with accurate self-learned dep edges:
  - ERR-7 blocked-on SRS-RESV-004, SRS-NOTIF-001, SRS-LOG-001 (Hot-Swap demotion
    runtime + the operator-alert + evidence-log channels).
  - ERR-8 blocked-on SRS-SAFE-001, SRS-EXE-006, SRS-NOTIF-001, SRS-LOG-001.
  - ERR-9 blocked-on SRS-MD-006, SRS-LOG-001, SRS-UI-001, SRS-API-001.
The next auto-pick was SRS-DATA-007 (a 6-session tar pit that unblocks 0 features).
Surfaced the conflict (scheduler routes by id; operator directive ranks by
downstream-unblock) via AskUserQuestion; operator chose SRS-DATA-008 (unblocks 10
DATA features: DATA-001/002/003/004/005/006/009/010/017/018). Released DATA-007,
acquired DATA-008 collision-safely by reusing agent_pool's own locked primitives.

What I did:
- New crate module crates/atp-data/src/tiering.rs — TieredStore over two
  MarketDataStore directories (the SRS-DATA-016 store doc explicitly defers tiering
  here). Invariants: (1) SSD-first durable write before any NAS write; (2) NAS
  converges to a superset (idempotent full-snapshot push, self-healing after a
  degraded ingest); (3) hot-retention floor-enforced at 90 days
  (MIN_HOT_RETENTION_DAYS, fail-closed below); (4) NAS indefinite (no NAS delete;
  archive_cold drops an SSD record only when cold AND confirmed byte-identical on
  NAS).
- A single nas_access classifier (Ready / Unreachable / Aliased) drives every NAS
  path so the three are never confused: Unreachable -> NasSyncStatus::Degraded
  (recoverable outage, the SRS-MD-006 hook); reachable-but-broken (corrupt store /
  conflict / lock / SSD alias) -> NasSyncStatus::Failed (integrity failure, never
  Synced, never folded into Degraded); sync/report/archive fail closed (TierError::
  Nas) on any non-Ready.
- Tier-alias guard (no-data-loss): same_directory rejects an SSD/NAS alias (a
  ./trailing-slash lexical alias or a symlink) at TierConfig::new AND in nas_access
  — so a post-construction symlink alias can never make archival delete the only
  copy.
- Operator CLI data008_tier_cli (ingest/report/archive-cold/sync); exits non-zero
  on a NasSyncStatus::Failed archival integrity failure.
- Contract block tiered_storage_contract + tools/data008_tiering_check.py
  (registered in architecture_check.py; NOT the ci.yml/run_ci_locally for-loops —
  architecture runs it transitively). init.sh smoke block.
- Tests: L7/L5 Rust crates/atp-data/tests/srs_data_008_tiered_storage.rs (11 cases:
  SSD-first+superset, degraded+recover, hot/cold retention, data-loss-safe archival,
  symlink-alias-fails-every-NAS-path, reachable-corrupt-NAS-fails-not-degrades, CLI
  exit codes, idempotent re-ingest, config floor/distinct) + L3 Python
  tests/test_data008_tiering_contract.py (25 cases incl. negative spot-checks).
- ENV FIX (NOT on this branch — see below): init.sh leaves .venv EMPTY on
  macOS/Apple Silicon because TA-Lib==0.6.8's wheel build can't find the Homebrew C
  library (/opt/homebrew, outside the build's default /usr/local search), and pip
  builds every wheel before installing any, so the failure aborts the whole
  `pip install`. Worked around locally by exporting TA_INCLUDE_PATH/TA_LIBRARY_PATH
  from `brew --prefix` before pip install, then re-running pip. RECOMMENDED FIX (for
  a SEPARATE change — Codex R5 required the feature branch be atomic, so I removed it
  from agent/SRS-DATA-008): add, before init.sh's `pip install`:
    if command -v brew >/dev/null 2>&1; then
      _BREW_PREFIX="$(brew --prefix 2>/dev/null)"
      if [[ -n "$_BREW_PREFIX" ]]; then
        export TA_INCLUDE_PATH="${TA_INCLUDE_PATH:-${_BREW_PREFIX}/include}"
        export TA_LIBRARY_PATH="${TA_LIBRARY_PATH:-${_BREW_PREFIX}/lib}"
      fi
    fi
  (brew-gated, env-respecting; no-op on Linux/CI). Until it lands, a fresh worktree's
  init.sh produces an empty venv → architecture_check/pytest can't run.

What I tested (per step):
  Step 1 (init): ./init.sh -> "✓ Environment ready" (after fixing the empty-venv
    via the TA-Lib path export + pip install).
  Step 2 (exercise via CLI/fixtures): data008_tier_cli ingest(hot,cold)/report/
    archive-cold/report over worktree-local --ssd/--nas dirs -> PASS; persisted
    SSD/NAS store files inspected (SSD=239B hot-only after archive, NAS=431B all).
  Step 3 (AC): cargo test -p atp-data --test srs_data_008_tiered_storage -> 11/11;
    report shows ssd_hot_retention_satisfied + nas_superset_satisfied; archive keeps
    SSD hot-only while NAS retains all.
  Step 4 (evidence): tools/data008_tiering_check.py -> SRS-DATA-008 TIERED-STORAGE
    PASS (15 evidence items); architecture_check.py -> SRS-ARCH-001 PASS.
  Full gate: cargo test --workspace green; cargo clippy --workspace -- -D warnings
    clean; cargo fmt --check clean; ruff check/format clean repo-wide; pytest 'not
    integration and not e2e' 2444 passed / 4 pre-existing skips. (run_ci_locally.sh
    mypy step fails on PRE-EXISTING python/atp_strategy debt — ci.yml marks mypy
    continue-on-error; not my files, not fixed.)

Critic verdicts:
  deterministic: APPROVE — 0 findings (both commits, staged + pre-commit hook).
  judgment (codex_review.sh origin/main): converged over 4 rounds.
    R1 [high] FIXED: tier dirs compared lexically -> an SSD/NAS alias (ssd/. or a
      symlink) let archive_cold delete the only copy. Added same_directory
      (lexical components + canonicalize) at TierConfig::new + an archive guard.
    R2 [high]x2 FIXED: (a) the alias was only guarded in archive_cold -> push/report
      still treated an aliased NAS as a real archive; centralized into nas_access so
      EVERY NAS path fails closed; (b) every push error was downgraded to Degraded ->
      added NasSyncStatus::Failed to distinguish reachable-but-broken from offline.
    R3 [high]+[medium] FIXED: (a) the CLI printed nas_sync:failed then exited 0 ->
      now exits non-zero on Failed + a CLI-level exit-code test; (b) the init.sh
      TA-Lib fix was mixed into the feature commit -> split into its own commit.
    R4 [high] FIXED: archive_cold returned Ok on an unreachable NAS while the
      module doc/contract claimed "fail closed on any non-Ready" -> made the docs +
      contract accurate (reachable-but-broken ALWAYS fails closed; an UNREACHABLE NAS
      is the recoverable case each op handles per its purpose: sync errors, report
      flags nas_reachable=false, archive_cold no-ops, ingest degrades). Per-op
      behavior was already correct + tested; the bug was the over-broad doc claim.
    R5 [high] FIXED: the branch DIFF (not just the commit) still carried the unrelated
      init.sh TA-Lib fix -> removed it from agent/SRS-DATA-008 entirely; the branch is
      now atomic (DATA-008 only). The env fix is recorded above for a separate change.
    R6 [high]x2: (a) retention_report false-positive — ssd_hot_retention_satisfied()
      returned true when NAS was unreachable (the cross-tier check never ran) -> FIXED:
      replaced the bool methods with TRI-STATE RetentionVerdict (Satisfied/Violated/
      Unverified); an unreachable NAS now yields Unverified, CLI prints it. (b) ingestion
      bypass — data016_ingest_cli writes a single store dir with NO NAS sync, so "all
      ingestion synced to NAS" is not met end to end -> SCOPED honestly as the deferred
      close blocker (route production ingestion through TieredStore) + RECLASSIFIED
      SRS-DATA-008 to passes:false. (Hit the data-heavy adversarial-loop pattern:
      fixed the fail-open bug, scoped the rest, then sought human authorization to land
      serialized rather than chase indefinitely.)
    R7 [high] FIXED (process, not a DATA-008 defect): origin/main moved mid-session
      (a sibling integrated SRS-BT-001), and my `reset --soft origin/main` left
      runtime_services.json based on OLD main -> the branch diff silently REVERTED
      BT-001's contract edits. Rebuilt runtime_services.json = current-main + my block
      (append-only); branch is now 2292 insertions / 0 deletions, atomic to DATA-008.
    R8 [high] SCOPED (deferred): retention_report proves NAS superset of RESIDENT SSD,
      but cannot detect loss of ALREADY-ARCHIVED (SSD-evicted) data without a durable
      expected-keys manifest. Documented the scope bound on nas_superset_verdict +
      added the manifest/catalog deferred owner (SRS-DATA-018). Did NOT build the
      manifest (substantial; out of scope) -> reinforces serialized.
  Verdicts recorded honestly; NO faked APPROVE. Stopped the loop after 8 rounds:
  every concrete bug fixed; the 2 remaining findings are deferred-completeness items,
  scoped + pinned. Operator AUTHORIZED the serialized landing (AskUserQuestion).

Resume / next: DATA-008 integrates SERIALIZED (passes:false) — the TieredStore
substrate is on main but the feature is NOT closed; it does NOT yet unblock the 10
DATA features (they stay blocked-on DATA-008 being passes:true). THE CLOSE BLOCKERS
(both in tiered_storage_contract.deferred[], pinned):
  1. Route ALL production ingestion through TieredStore so 'all ingestion writes to
     SSD first; new data is synced to NAS' is demonstrated end to end — wire
     data016_ingest_cli + the SRS-DATA-001/003/005/006 adapters through the tier (or
     fold the raw single-dir ingest into it). This is the load-bearing close.
  2. A durable expected-keys MANIFEST/CATALOG so retention_report can detect loss of
     already-archived (SSD-evicted) data — owner SRS-DATA-018 (backup + validated
     recovery) / a catalog. Until then nas_superset_verdict proves only NAS ⊇
     resident-SSD (necessary, not sufficient for indefinite retention).
When both land, flip DATA-008 -> passes:true (it then unblocks DATA-001/002/003/004/
005/006/009/010/017/018). DATA-009 (cold-read failover) + DATA-010 (eviction policy)
compose the tier boundary (TieredStore + ArchiveOutcome) this slice lays down.
The parked ERR-7/8/9 stay blocked on their recorded runtime deps.
SEPARATE follow-up (NOT on this branch, per Codex R5): the init.sh Homebrew TA-Lib
env fix recorded above — land it so a fresh worktree's .venv isn't empty on macOS.
