# SRS-DATA-010 — SSD cache eviction policy

## Context

SRS-DATA-010 (SyRS SYS-69, P1): *"The software shall evict SSD cache data according to the
configured storage policy."* The authoritative AC (SYS-69, richer than `feature_list.json`):

> When SSD usage exceeds a configurable high-water mark (default **80%**), the storage manager
> shall **evict data by age, prioritizing removal of data for securities not on the active-strategy
> list or minute-bar watchlist**. It shall **never evict data for securities with the currently
> running live strategy container**. It shall **not evict data accessed within a configurable
> recency window (default 24h) by a running backtest or factor-pipeline job.** (SYS-68: cold-read
> cache entries are evicted **before any hot runtime data**.)

The tiered-storage substrate already on `main` ships the *primitives* but explicitly defers the
*policy* to this feature:
- `crates/atp-data/src/cold_read.rs` (DATA-009): `TieredReader::evict_cold_cache_to(max_entries)`
  — the hot-safe cold-cache drain primitive; ranks by `event_ts` only. Doc names it *"the
  primitive the SRS-DATA-010 policy drives."* `ColdReadConfig` counts capacity in **record-units**
  (a deterministic fixture proxy for bytes — real 1 TB is an NFR-SC2 deployment concern).
- `crates/atp-data/src/tiering.rs` (DATA-008): `TieredStore::archive_cold(now_ts)` (data-loss-safe
  SSD→NAS archival), `retention_report`, `is_hot`/`hot_window_start`; 90-day hot floor.
- Confirmed **absent** today: any `StoragePolicy`/high-water/eviction-policy struct, any
  last-access/recency metadata, any live-strategy→symbols registry.

**Recorded deps** (`tools/feature_deps.json`): `SRS-DATA-010 → [SRS-DATA-008, SRS-DATA-017]`, both
`passes:true` → the scheduler correctly handed this to me as ready.

## Completeness classification: **serialized** (passes stays false)

Scope decision (operator, AskUserQuestion): **Deeper — also wire a real AC-3 producer.** Build the
contained policy engine **and** a durable access-journal substrate + additive instrumentation of the
backtest/factor read paths, so AC-3's "recently accessed by a running job" is backed by *real*
recorded accesses rather than only a fixture file. Still **serialized** because:
- **AC-2 (never evict live-strategy data):** the live-strategy→symbols feed is deferred to
  SRS-EXE-001 / SRS-RESV-* (today only a `--live <id>` demonstration stub; `LiveStrategyState`
  carries no symbols; `ConsolidatedSubscriptionRegistry` is in-memory and doesn't split live/paper).
- **AC-3 running-job scoping:** the *running-job registry* (WorkloadRegistry in atp-orchestrator) is
  the authoritative "is this job still running" source; wiring it live is deferred → I expose it as
  an injected seam (fail-closed: without it, over-protect all recently-accessed symbols).
- True end-to-end AC-2/AC-3 needs a real live strategy + real running jobs = integration/live.

→ Integrate **`--mode serialized`** (code lands on `main`, `passes:false`), operator/`verified-e2e`
finishes later. Mirrors the DATA-009 landing. **Not `block`** — the code needs no unbuilt feature to
compile/run; only live verification is deferred.

### Safety asymmetry (key design invariant)
- **Access-journal WRITE fails open:** a journal-write error must NEVER break a backtest/factor read
  (log-and-continue). Recording is a pure side-effect that cannot change what a read returns (keeps
  DATA-007 point-in-time + closed-green read semantics byte-identical).
- **Access-journal READ (by eviction) fails closed:** distinguish *absent/empty* (benign → no
  recency protection, proceed) from *present-but-corrupt/truncated* (→ refuse to evict). Without the
  running-job set, treat all in-window journal symbols as protected (over-protect).

## Design — all new code; ZERO edits to closed-green cold_read.rs/tiering.rs/store.rs

### New module `crates/atp-data/src/eviction.rs`

**Config**
- `StoragePolicy { high_water_percent: u32 (default 80), recency_window_secs: i64 (default 86_400) }`
  with fail-closed `new()` (reject high-water 0 or >100; reject negative recency). Consts
  `DEFAULT_HIGH_WATER_PERCENT=80`, `DEFAULT_RECENCY_WINDOW_SECS=86_400`.
- Reuse `ColdReadConfig::ssd_capacity_records` for capacity (record-units); target usage =
  `floor(capacity * high_water_percent / 100)`.

**Protection inputs** — `ProtectionInputs`:
- `live_symbols: BTreeSet<String>` — **pinned, never evict** (AC-2). Seam: live-designation feed.
- `recent_access: BTreeMap<String, i64>` — symbol → most-recent access_ts; **pinned** when
  `now_ts - access_ts <= recency_window_secs` (AC-3). **Sourced from the real `AccessJournal`**
  (below), filtered to running jobs; the CLI can also merge a fixture file.
- `active_strategy_symbols` + `minute_bar_watchlist: BTreeSet<String>` — **deprioritized**
  (evicted last, only if unavoidable) (SYS-69 "prioritize removal of data *not* on these lists").
- Symbols normalized to match `NaturalKey.symbol` (trim+uppercase, per `SecurityKey`).

### New durable substrate `crates/atp-data/src/access_journal.rs` (the real AC-3 producer)
- `AccessRecorder` trait: `fn record(&self, job: &JobRef, symbol: &str, access_ts: i64)`. Default
  `NoopRecorder` (existing read paths behave byte-identically unless a recorder is wired).
- `AccessJournal` — durable, append-only log under `<ssd>/access_journal/` (torn-tail-safe: parse to
  the last `\n`; append via O_APPEND; **write fails open**, logging and continuing).
  - `record(job, symbol, ts)` — one line `<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>`.
  - `recent(window_secs, now_ts, running: Option<&BTreeSet<JobId>>) -> Result<BTreeMap<String,i64>>`
    — latest access_ts per symbol within window; if `running` given, keep only those jobs;
    if `None`, keep all in-window (fail-closed over-protect). **Read fails closed** on a
    corrupt (non-tail) line: return `AccessJournalError::Corrupt` so the engine refuses to evict.
- `JobRef { kind: JobKind(Backtest|FactorPipeline), id: JobId(String) }`. `JobKind` maps to the
  orchestrator `WorkloadPriority::{Backtest,FactorPipeline}` (documented running-job seam).

### Additive, behavior-preserving instrumentation of the read paths
- `crates/atp-simulation/src/store_bar_source.rs`: add `StoreBarSource::with_access_recorder(
  recorder: &dyn AccessRecorder, job: JobRef)` (or a wrapping newtype). The existing `new(...)` and
  read methods stay **byte-identical**; the recording variant records `(symbol, bar_ts)` on each
  served read. No change to what bars are returned.
- `crates/atp-factor-pipeline/src/store_inputs.rs`: add `assemble_factor_inputs_recorded(store,
  securities, start_ts, end_ts, basis, recorder, job)` delegating to the existing
  `assemble_factor_inputs` and recording each touched `SecurityKey.symbol()`. Existing fn unchanged.
- Both crates already depend on `atp-data` → importing `AccessRecorder` is no new dep / no
  direction violation. A test in each asserts the un-instrumented path is unchanged and the
  instrumented path writes the expected journal lines.

**Pure planner** `plan_eviction(inventory, policy, inputs, now_ts, capacity) -> EvictionPlan`:
- `inventory`: `Vec<EvictionCandidate { key: NaturalKey, tier: Tier(Cold|Hot) }>` built from the
  cold-cache store + the SSD-hot store (1 record = 1 unit).
- Partition every candidate into `Pinned` (live ∨ recently-accessed) vs `Evictable`.
- Order `Evictable` **most-removable first**: `(tier: Cold<Hot)`, then `(listed: not-on-active/
  watchlist first)`, then `(event_ts ascending — oldest first)`, then `key` (deterministic).
- Walk the ordered list, adding to the plan until `usage - evicted <= target`.
- `EvictionPlan { evict: Vec<(NaturalKey,Tier)>, target, usage_before, projected_after,
  reached_target: bool, pinned_blocking: usize }`. `reached_target=false` + `pinned_blocking>0`
  means the mark can't be met without evicting pinned data → **fail-safe: stop, never evict pinned.**

**Engine** `EvictionEngine::enforce(reader: &TieredReader, policy, inputs, now_ts) -> EvictionOutcome`:
1. Build inventory (read-only) from `cold_cache_dir()` store + `tier().ssd_dir()` store.
2. `plan_eviction(...)`.
3. **Enforce cold-cache evictions concretely**: `StoreLock::acquire(cold_cache_dir)` → load →
   rebuild a `MarketDataStore` of (records − planned-cold-keys) via `new()`+`upsert()` → `save_to_path`.
   (Same lock/atomic-save discipline as `evict_cold_cache_to`, but **symbol/protection-aware**.)
4. **Hot evictions**: drive the existing safe `TieredStore::archive_cold(now_ts)` for age-eligible
   hot data (SSD→NAS, keeps data on NAS); **report** any planned hot evictions not yet age-eligible
   as `hot_pressure_deferred` rather than inventing new hot-store removal (protects DATA-008
   closed-green). Report residual over-mark.
5. `EvictionOutcome { usage_before, cold_evicted, hot_archived, usage_after, reached_target,
   pinned_retained, hot_pressure_deferred }`.

**Fail-closed:** `enforce` requires an explicit `ProtectionInputs` source. The CLI refuses a
destructive run without `--protection-inputs <file>` unless `--assume-unprotected` is passed
explicitly (so a production run can never silently treat "no live feed wired" as "evict everything").

### New binary `crates/atp-data/src/bin/data010_eviction_cli.rs`
Hand-rolled allowlist parser (no clap), `key:value` line output, non-zero exit on invariant breach —
matching `data009_cold_read_cli.rs`. Flags: `--ssd/--nas` (else `ATP_SSD_DATA_DIR`/`ATP_NAS_DATA_DIR`),
`--ssd-capacity`, `--cache-share` (default 20), `--high-water` (default 80), `--recency-secs`
(default 86400), `--now` (default 1_700_000_000), `--protection-inputs <file>`, `--assume-unprotected`.
Subcommands:
- `report` — SSD usage vs high-water, pinned/evictable counts (dry, read-only).
- `plan` — print the ordered eviction plan + `reached_target`/`pinned_blocking` (dry-run).
- `enforce` — apply cold-cache eviction + drive archival; print outcome; **exit non-zero** if the
  mark can't be met without evicting pinned data (operator-visible fail-safe).
- Protection-inputs file: line format `live <SYM>` / `active <SYM>` / `watchlist <SYM>` /
  `access <SYM> <TS>` (hand-parsed, serde-free — matches the crate's discipline).

### `crates/atp-data/src/lib.rs`
`pub mod eviction; pub mod access_journal;` + re-export `StoragePolicy`, `ProtectionInputs`,
`EvictionPlan`, `EvictionEngine`, `EvictionOutcome`, `Tier`, `AccessJournal`, `AccessRecorder`,
`NoopRecorder`, `JobRef`, `JobKind`, consts.

### Evidence (mirrors DATA-008/009)
- `architecture/runtime_services.json`: new `eviction_policy_contract` block pinning struct/fn names
  + required/forbidden tokens (e.g. the cold-before-hot ordering; "pinned never evicted").
- `tools/data010_eviction_check.py`: static-source contract check emitting
  `SRS-DATA-010 STORAGE-EVICTION PASS`; encodes "planner never emits a pinned symbol" and
  "cold-before-hot" as source assertions.
- `tests/test_data010_eviction_contract.py`: pytest — real source passes every check **and** a
  deliberately mutated source (e.g. a planner that skips the pinned filter) is **caught**.

## Tests (per Step 5.5 layers)
- **L1 unit** (inline `#[cfg(test)] mod tests`): planner ranking (cold-before-hot,
  non-listed-before-listed, oldest-first), target math, boundary; `AccessJournal` round-trip,
  window filtering, running-job filter, torn-tail read, **corrupt-line → fail-closed error**.
- **L2 property** (Rust proptest inline / `tests/property/`): over random inventories —
  (i) no pinned symbol ever in the plan; (ii) no unnecessary over-eviction below target;
  (iii) projected_after ≤ usage_before; (iv) journal `recent()` never omits an in-window running-job
  access.
- **L4 boundary** (`crates/atp-data/tests/srs_data_010_eviction.rs`): end-to-end over a fixture SSD +
  cold-cache — build >80% usage, run `enforce`, reload stores, assert usage ≤ target, live & recency
  symbols byte-present, oldest non-listed evicted first, SSD-hot untouched when cold-cache suffices.
- **Instrumentation tests** (in atp-simulation + atp-factor-pipeline): un-instrumented read path
  byte-identical; instrumented path writes expected journal lines; a write failure does not fail the
  read (fail-open).
- **L7 domain** (`tests/domain/test_data010_eviction_safety.py`, paired regardless of SAFETY_PATH_RE):
  **live-strategy data NEVER evicted under max pressure**; **recency-protected NEVER evicted**;
  **`enforce` fails closed (non-zero) on missing protection source or a corrupt journal**; mark left
  breached rather than evicting pinned.

## Files
Add: `crates/atp-data/src/eviction.rs`, `crates/atp-data/src/access_journal.rs`,
`crates/atp-data/src/bin/data010_eviction_cli.rs`, `crates/atp-data/tests/srs_data_010_eviction.rs`,
`tools/data010_eviction_check.py`, `tests/test_data010_eviction_contract.py`,
`tests/domain/test_data010_eviction_safety.py`, `progress.d/plan-SRS-DATA-010.md`,
`progress.d/session-SRS-DATA-010.md`.
Edit (additive only): `crates/atp-data/src/lib.rs`, `architecture/runtime_services.json`,
`crates/atp-simulation/src/store_bar_source.rs`, `crates/atp-factor-pipeline/src/store_inputs.rs`
(new wrapper entry points only — existing fns/bodies unchanged).
**No edits** to `cold_read.rs` / `tiering.rs` / `store.rs`.

## Verification — maps to the feature's 4 steps
- **Step 1:** `./init.sh` → "✓ Environment ready".
- **Step 2:** exercise via `data010_eviction_cli report|plan|enforce` over a fixture SSD/cold-cache
  built >80% full (record output).
- **Step 3 (AC):** the L1/L2/L4/L7 tests + a CLI `enforce` run demonstrating: usage → ≤80%;
  oldest non-listed evicted first; live-strategy symbols retained; 24h-recency symbols retained;
  cold-before-hot.
- **Step 4:** record per-step PASS/FAIL evidence; **passes stays false** (serialized) — end-to-end
  AC-2 needs the deferred live-symbol feed (SRS-EXE-001/RESV) and AC-3's running-job scoping needs
  the atp-orchestrator `WorkloadRegistry` wiring; both require a live strategy + running jobs.

## Gate & integrate
`source .venv/bin/activate && tools/run_ci_locally.sh` + `cargo test --workspace` +
`pytest -m "not integration and not e2e"` + deterministic critic (`critic_check.py --staged`) +
judgment critic (`adversarial_review.py origin/main`). Then
`python3 tools/agent_pool.py integrate SRS-DATA-010 --mode serialized`. Session note records what's
now real vs still deferred:
- **Built real:** eviction policy engine + CLI + durable `AccessJournal` + backtest/factor read-path
  instrumentation (AC-3's access producer).
- **Deferred (why passes stays false):** **AC-2 live-symbols feed → SRS-EXE-001 / SRS-RESV-***
  (only a stub today); **AC-3 running-job registry → atp-orchestrator `WorkloadRegistry`** wiring
  (injected seam today, fail-closed over-protect without it); true end-to-end AC-2/AC-3 needs a live
  strategy + running jobs (integration/live).
