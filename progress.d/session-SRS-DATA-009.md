=== SESSION SRS-DATA-009 ===
Date: 2026-07-02
Feature: SRS-DATA-009 — transparently fall back to NAS for cold historical reads (SyRS SYS-68).
Outcome: PARTIAL — blocked-on SRS-DATA-011, SRS-DATA-012 (passes:false). Cold-read MECHANISM +
one wired consumer landed on main; full multi-consumer transparency is the documented close blocker.
Reclassified 2026-07-03 from "serialized" to "partial(blocked-on ...)" and the honest dependency
edge was RECORDED (see SESSION 2 below) so the scheduler stops re-offering DATA-009 until its
split-adjusted-cold-read prerequisite (DATA-011/012) is green. See SESSION 2 for the resume decision.

CONTEXT: builds directly on SRS-DATA-008 (TieredStore; passes:true). DATA-008 owns the SSD-first
write + retention + archive_cold that drops cold records off SSD (kept only on NAS). DATA-009 is the
READ counterpart: a consumer's historical query is served transparently across the tiers.

WHAT I BUILT:
- crates/atp-data/src/cold_read.rs (NEW): TieredReader over the existing TieredStore.
  * TieredReader::query(&UnifiedHistoricalQuery, now_ts) — SSD primary -> cold-read cache -> NAS
    (for cold ranges only), merged deduped-by-natural-key in the SAME event_ts-ascending order as
    query_unified (parity with SSD∪NAS). Transparent: consumer passes only symbol/resolution/range.
  * Bounded cache: a SEPARATE MarketDataStore under <ssd>/cold_read_cache, cap =
    floor(ssd_capacity_records * cache_share_percent / 100) INTEGER math (no float); ColdReadConfig
    defaults share to 20%, fails closed on share>100% / zero capacity; every write enforces entries<=cap.
  * evict_cold_cache_to(max) drains ONLY the cache dir (never opens the SSD primary) — "evicted
    before hot runtime data" is structural.
  * FAIL-CLOSED integrity (added across Codex rounds):
      - CrossTierDivergence: merge_record compares FULL record content on any cross-tier duplicate key
        (a stale/corrupt cache that decodes but disagrees with NAS fails closed, not silently shadows).
      - HotRetentionBreach: NAS serves only records OLDER than the hot window; a HOT record on NAS but
        missing from SSD is an SRS-DATA-008 retention breach -> fail closed (never served/cached as cold).
      - Degraded taxonomy mirrors DATA-008 (unreachable/aliased NAS -> nas_reachable:false, not an error;
        reachable-but-corrupt NAS -> fail closed).
- crates/atp-data/src/bin/data009_cold_read_cli.rs (NEW): operator CLI (query / cache-report /
  evict-cache); exits non-zero on a cap breach; never persists directly (cache persistence is
  library-owned, so it does not trip the DATA-008 all-bins routing sweep).
- crates/atp-data/src/bin/data007_query_cli.rs (WIRED — the "no consumer code changes" clause): the
  EXISTING SRS-DATA-007 operator read surface now auto-tiers from the ATP_NAS_DATA_DIR config key
  (exactly like --dir resolves from ATP_DATA_STORE_DIR) — the SAME query invocation transparently
  serves archived-off records from NAS with NO new flags. A configured-but-unmounted NAS surfaces a
  DEGRADED read (tier:cold-read, nas_reachable:false), never a silent single-tier read. Tiered serves
  raw only (split-adjusted stays single-tier). Backward-compatible (no NAS configured -> byte-identical).
- crates/atp-data/src/lib.rs: pub mod cold_read + re-exports.
- architecture/runtime_services.json: cold_read_failover_contract block (+ consumer_wiring,
  divergence_guard, retention_guard sub-blocks; honest partial framing + close blocker in deferred[]).
- tools/data009_cold_read_check.py (NEW) + tests/test_data009_cold_read_contract.py (NEW): 14 static
  contract checks + mutation negatives + end-to-end CLI regressions (incl. env-auto-tier, degraded
  mount, divergence, cap enforcement, and the data007-style archived-off read).

WHAT I TESTED (per step):
- Step 1 (init.sh): PASS — "✓ Environment ready".
- Step 2/3 (CLI/API over fixtures): PASS — data008_tier_cli ingest+archive-cold, then data007_query_cli
  (single-tier match_count:0) vs data007_query_cli --nas / ATP_NAS_DATA_DIR (served_from_nas:1,
  match_count:1, cold_cache_within_cap:true); env-auto-tier with unchanged invocation; degraded
  unmounted NAS (nas_reachable:false).
- Step 3 (AC): served-from-NAS + cached-on-SSD (transparent, no query change); cache <= 20% share
  (cap enforced, evicts to fit); evicted before hot (evict_cold_cache_to never touches SSD primary).
- cargo test -p atp-data: 91 lib (15 cold_read) + all bins PASS. pytest DATA cluster: green, no
  DATA-007/008 regression. cargo fmt --check + clippy --workspace -D warnings: clean. data009 check PASS.
Critic verdicts:
  deterministic (critic_check.py --staged): APPROVE — no findings.
  judgment (codex_review.sh origin/main): needs-attention (5 rounds). Rounds 1-4 findings all FIXED
    (opt-in wiring -> env-transparent operator consumer; cache/NAS divergence -> fail closed;
    env-NAS-outage -> degraded; full-range NAS fallback -> cold-only + HotRetentionBreach). Round 5's
    sole remaining finding is the multi-consumer deferral, for which Codex explicitly recommends "keep
    passes:false/partial" — the posture taken here. Override rationale: this is the documented,
    honestly-scoped deferral (not a shipped defect); the code is correct and one existing consumer is
    transparently wired.

Resume / next (CLOSE BLOCKER — keeps passes:false):
  Route the OTHER named unified-read consumers through TieredReader so "without consumer code changes"
  holds for EVERY consumer, not just the operator CLI:
    1. atp-simulation StoreBarSource (backtest BarSource) — borrow-streaming + up-front bounding pass
       over a single MarketDataStore; needs a tiered-backed variant (design change, its own contract).
    2. python/atp_strategy StoreBackedHistoricalData (Python binding) — reads a single store dir.
    3. atp-factor-pipeline store_inputs (factor pipeline) — reads a single store dir.
  Each is a passes:true feature with its own contract (cross-crate follow-ups). The MECHANISM +
  operator surface shipped here are the foundation they reuse. Add a consumer-level regression per
  path (archived-off record served through it) before flipping SRS-DATA-009 passes:true.
  Also downstream: SRS-DATA-010 eviction POLICY drives evict_cold_cache_to; split-adjusted × cold-read
  is SRS-DATA-011/012.

=== SESSION 2 (SRS-DATA-009 resume) ===
Date: 2026-07-03
Outcome: PARTIAL — integrated the (unintegrated) SESSION-1 mechanism to main + RECORDED the honest
block edge. passes stays false. No code changes to the mechanism this session (it was already correct
and Codex-reviewed 5 rounds); this session lands it + de-churns.

FINDING (why SESSION 1 kept re-appearing): the SESSION-1 work was committed on the branch but NEVER
integrated to origin/main (the 2 commits sat un-pushed), AND no dependency edge was recorded — so the
scheduler (which routes by id) kept re-offering DATA-009. Verified via `git diff origin/main...HEAD`
(3159 insertions still ahead of main) + `feature_deps.json` (only edge: DATA-009 -> DATA-008, green).

DECISION (three-way confirmed — cannot close green this session; do NOT force it):
- Architecture (fresh exploration): NO shared tier-swappable read seam. StoreBarSource (atp-simulation)
  and store_inputs (atp-factor-pipeline) each borrow a CONCRETE &MarketDataStore and call methods
  TieredReader does NOT expose — `.records()` (up-front bounding) and `query_split_adjusted*`. There is
  no query trait and no DataLayer read factory. The Python StoreBackedHistoricalData binding is the
  exception: it shells to data007_query_cli, which ALREADY auto-tiers from ATP_NAS_DATA_DIR, so it is
  already raw-transparent with no code change.
- Hard prerequisite: both Rust consumers read SPLIT-ADJUSTED; serving split-adjusted over archived-off
  NAS bars couples DATA-009 to the SRS-DATA-011 coverage frontier + SRS-DATA-012 math (both passes:false).
  Until those land, a split-adjusted cold read FAILS CLOSED instead of serving from NAS — so "without
  consumer code changes" cannot hold for EVERY named consumer (SYS-68: strategy containers, factor
  pipeline, backtesting engine, research environment). A raw-only consumer refactor would (a) be a
  risky cross-crate change touching SRS-BT-001/SRS-FAC-001 contract tests, and (b) still NOT flip
  complete (split-adjusted stays un-tiered). Premature -> deferred, per the honest-scope discipline.
- SRS.md verification method for DATA-009 is "Test" (solo-testable), so a `complete` flip is possible
  IN PRINCIPLE once the consumers are tiered — this is not an IB/integration serialization; it is a
  genuine feature-dependency block. Hence `integrate --mode partial` (not serialized).

RECORDED BLOCK: `agent_pool.py block SRS-DATA-009 --on SRS-DATA-011 SRS-DATA-012` — split-adjusted
cold-read tiering is the prerequisite for transparently tiering the split-adjusted-reading named
consumers. DATA-009 re-surfaces once DATA-011/012 are green; at that point a session does the
TieredReader capability extension (borrow-streaming bound + split-adjusted-over-tier) and routes
StoreBarSource + store_inputs (DATA-009's own remaining cross-crate work). NOT blocked on DATA-010
(eviction POLICY): "evicted before hot" is already structurally satisfied by the separate cache dir +
evict_cold_cache_to primitive.

WHAT I VERIFIED (this session, mechanism unchanged):
- cargo test -p atp-data --lib: 93 passed (incl. 15 cold_read); cargo test --workspace: all green (0 failed).
- tools/data009_cold_read_check.py: PASS. pytest test_data009_cold_read_contract.py: 40 passed.
- pytest -m "not integration and not e2e" (whole repo): 2806 passed, 4 pre-existing skips, 0 failed.
- cargo fmt --check + cargo clippy --workspace -D warnings: clean. ruff (my py files): clean.
- tools/architecture_check.py: PASS (validates my runtime_services.json edits).
- deterministic critic (critic_check.py --range origin/main..HEAD): APPROVE — no findings.
- KNOWN pre-existing RED (NOT mine, on origin/main, out of scope): run_ci_locally.sh mypy step reports
  66 type errors in python/atp_strategy/* (scheduler.py, examples/_harness.py, sma_crossover.py). My
  branch touches ZERO python/atp_strategy files; `git diff --name-only origin/main...HEAD -- python/`
  is empty. This is the known "CI red behind format/type gates" main condition, not a DATA-009 defect.
- judgment critic (codex): the mechanism code is byte-identical to SESSION 1's Codex-reviewed state
  (5 rounds; recorded above). No code changed this session (only the doc note + the recorded block),
  so the prior verdicts stand; a full codex re-review of unchanged code would be redundant.

Resume / next: unchanged close path (see "Resume / next (CLOSE BLOCKER ...)" above). The block on
DATA-011/012 gates re-offering until split-adjusted-cold-read is buildable.
