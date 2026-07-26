//! # SRS-DATA-010 SSD storage eviction policy
//!
//! SYS-69: *"When SSD usage exceeds a configurable high-water mark (default 80% of SSD capacity), the
//! storage manager shall evict data by age, prioritizing removal of data for securities not on the
//! active-strategy list or minute-bar watchlist. The storage manager shall never evict data for
//! securities with the currently running live strategy container. The eviction policy shall not evict
//! data that has been accessed within a configurable recency window (default 24 hours) by a running
//! backtest or factor pipeline job."* SYS-68 adds: cold-read cache entries are **evicted before any
//! hot runtime data**.
//!
//! This module is the **policy brain** over the DATA-008 / DATA-009 primitives. It does not reinvent
//! the tier substrate — capacity is the DATA-009 [`ColdReadConfig`] record-unit proxy, the hot/cold
//! boundary is the DATA-008 [`TierConfig::hot_window_start`], the cache store is the DATA-009 cold-read
//! cache. It adds: a [`StoragePolicy`] (the 80% mark + 24 h recency window), a [`ProtectionInputs`]
//! model (the never-evict + deprioritise sets), a **pure planner** [`plan_eviction`], and an
//! [`EvictionEngine`] that enforces the plan.
//!
//! ## Structural safety: `enforce` never opens the SSD primary
//! Exactly like the DATA-009 `evict_cold_cache_to` primitive, [`EvictionEngine::enforce`] physically
//! rewrites **only** the cold-read cache store (`<ssd>/cold_read_cache/`); it never opens the SSD
//! primary (hot) store for writing. So hot runtime data — and in particular every live-strategy /
//! recently-accessed / in-retention-window record — is **structurally impossible to physically evict**
//! here. The planner still plans over the *whole* SSD inventory (hot + cold) so the SYS-69 ordering and
//! protections are computed and reported end-to-end, but any hot-tier eviction the plan identifies is
//! surfaced as `hot_pressure_deferred` (the operator resolves SSD-hot pressure via DATA-008 retention
//! archival or added capacity), never silently removed. Physical hot-tier pressure-eviction is the
//! deferred "full hot-pressure" scope.
//!
//! ## Protection tiers (SYS-69 semantics)
//! - **Pinned — never evicted:** live-strategy symbols (AC-2); symbols accessed within the recency
//!   window by a running job (AC-3); and hot records inside the DATA-008 90-day retention floor
//!   (`Retention` — cannot be evicted without breaching SRS-DATA-008).
//! - **Deprioritised — evicted last:** symbols on the active-strategy list or the minute-bar watchlist
//!   (SYS-69 "prioritizing removal of data *not* on" these lists).
//! - **Freely evictable — evicted first, oldest-first:** everything else.
//!
//! If the mark cannot be met without evicting a pinned record, the policy **stops** and reports the
//! residual breach (`reached_target = false`) rather than ever evicting pinned data — fail-safe.
//!
//! ## Determinism
//! Integer arithmetic only (no floating point in the cap), a caller-supplied `now_ts` (no wall-clock),
//! and a total-ordered eviction sequence, so the same inventory + inputs yield an identical plan.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;

use crate::access_journal::normalize_symbol;
use crate::cold_read::TieredReader;
use crate::store::{MarketDataStore, NaturalKey, StoreError, StoreLock};

/// The SYS-69 default high-water mark: eviction is triggered when SSD usage exceeds **80%** of
/// capacity.
pub const DEFAULT_HIGH_WATER_PERCENT: u32 = 80;

/// The maximum permitted high-water mark: a mark above **100%** of capacity is meaningless (usage can
/// never exceed capacity), rejected fail-closed at [`StoragePolicy::new`].
pub const MAX_HIGH_WATER_PERCENT: u32 = 100;

/// The SYS-69 default recency window: data accessed within **24 hours** (86 400 s) by a running
/// backtest / factor job is not evicted.
pub const DEFAULT_RECENCY_WINDOW_SECS: i64 = 86_400;

/// A fail-closed error from an eviction operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EvictionError {
    /// The configured high-water percent is 0 or exceeds [`MAX_HIGH_WATER_PERCENT`]. Rejected at
    /// construction — a 0% mark would evict everything, a >100% mark could never trigger.
    InvalidHighWater {
        /// The rejected value.
        configured: u32,
    },
    /// The configured recency window is negative. Rejected at construction (a window is a
    /// non-negative duration).
    InvalidRecencyWindow {
        /// The rejected value.
        configured: i64,
    },
    /// A read of the SSD primary (hot) store failed while building the inventory. Fail-closed: the
    /// primary is the source of truth, so an unreadable primary aborts the eviction (never evicts
    /// against a partial inventory).
    Ssd(StoreError),
    /// A read/write of the SSD cold-read cache store failed. Fail-closed so a corrupt or locked cache
    /// aborts rather than silently mis-evicting.
    Cache(StoreError),
}

impl fmt::Display for EvictionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidHighWater { configured } => write!(
                f,
                "high-water mark {configured}% must be in 1..={MAX_HIGH_WATER_PERCENT}"
            ),
            Self::InvalidRecencyWindow { configured } => {
                write!(f, "recency window {configured}s must be non-negative")
            }
            Self::Ssd(err) => write!(f, "SSD primary read error: {err}"),
            Self::Cache(err) => write!(f, "SSD cold-read cache error: {err}"),
        }
    }
}

impl Error for EvictionError {}

/// The validated storage-eviction policy: the high-water mark that triggers eviction and the recency
/// window that protects recently-accessed data. Capacity is supplied separately (the DATA-009
/// record-unit proxy), so a policy is capacity-independent and reusable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StoragePolicy {
    high_water_percent: u32,
    recency_window_secs: i64,
}

impl StoragePolicy {
    /// Build a validated policy, failing closed on a 0 / >100 high-water mark or a negative recency
    /// window.
    pub fn new(high_water_percent: u32, recency_window_secs: i64) -> Result<Self, EvictionError> {
        if high_water_percent == 0 || high_water_percent > MAX_HIGH_WATER_PERCENT {
            return Err(EvictionError::InvalidHighWater {
                configured: high_water_percent,
            });
        }
        if recency_window_secs < 0 {
            return Err(EvictionError::InvalidRecencyWindow {
                configured: recency_window_secs,
            });
        }
        Ok(Self {
            high_water_percent,
            recency_window_secs,
        })
    }

    /// The SYS-69 default policy: 80% high-water, 24 h recency window. Infallible (the defaults are
    /// valid).
    pub fn with_defaults() -> Self {
        Self {
            high_water_percent: DEFAULT_HIGH_WATER_PERCENT,
            recency_window_secs: DEFAULT_RECENCY_WINDOW_SECS,
        }
    }

    /// The configured high-water mark, in percent of SSD capacity.
    pub fn high_water_percent(&self) -> u32 {
        self.high_water_percent
    }

    /// The configured recency window, in seconds.
    pub fn recency_window_secs(&self) -> i64 {
        self.recency_window_secs
    }

    /// The target occupancy the policy evicts down to: `floor(capacity_records * high_water / 100)`.
    /// **Integer arithmetic** (saturating multiply then integer divide) — no floating point, so the
    /// target is exact and deterministic. Usage strictly above this triggers eviction; usage at or
    /// below it is within policy.
    pub fn target_records(&self, capacity_records: u64) -> u64 {
        capacity_records.saturating_mul(self.high_water_percent as u64) / 100
    }

    /// The inclusive lower bound of the recency window relative to `now_ts`: a record whose most-recent
    /// access is `>= recency_window_start(now_ts)` is protected.
    pub fn recency_window_start(&self, now_ts: i64) -> i64 {
        now_ts.saturating_sub(self.recency_window_secs.max(0))
    }
}

/// The SSD tier a candidate lives on. **Ordering is load-bearing:** `Cold < Hot`, so the SYS-68
/// "cold-read cache evicted before hot runtime data" ordering falls out of the derived `Ord`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum Tier {
    /// The DATA-009 cold-read cache (`<ssd>/cold_read_cache/`) — evicted first.
    Cold,
    /// The SSD primary (hot) store — evicted after the cache, and never physically by
    /// [`EvictionEngine::enforce`].
    Hot,
}

impl Tier {
    /// The lowercase wire string.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Cold => "cold",
            Self::Hot => "hot",
        }
    }
}

/// Why a candidate is pinned (never evicted). Surfaced for operator-visible evidence.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PinReason {
    /// The symbol is traded by the currently running live strategy (AC-2).
    Live,
    /// The symbol was accessed within the recency window by a running job (AC-3).
    Recent,
    /// A hot record inside the DATA-008 90-day retention floor — evicting it would breach SRS-DATA-008.
    Retention,
}

impl PinReason {
    /// The lowercase wire string.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Live => "live",
            Self::Recent => "recent",
            Self::Retention => "retention",
        }
    }
}

/// The never-evict + deprioritise inputs the policy consumes. The **seam** the real producers fill:
/// `live_symbols` from the live-designation feed (deferred SRS-EXE-001/RESV), `recent_access` from the
/// [`crate::access_journal::AccessJournal`] filtered to running jobs, `active_strategy_symbols` /
/// `minute_bar_watchlist` from the subscription registry / watchlist. All symbols are normalised
/// (trim + uppercase) on insert so comparison against a store natural-key symbol is exact.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct ProtectionInputs {
    live_symbols: BTreeSet<String>,
    active_strategy_symbols: BTreeSet<String>,
    minute_bar_watchlist: BTreeSet<String>,
    recent_access: BTreeMap<String, i64>,
}

impl ProtectionInputs {
    /// An empty input set (no protections). With this, only the DATA-008 retention floor pins hot data;
    /// all cache data is freely evictable — safe, because the cache is a recoverable copy of NAS.
    pub fn new() -> Self {
        Self::default()
    }

    /// Mark `symbol` as traded by the running live strategy (pinned, never evicted).
    pub fn add_live_symbol(&mut self, symbol: &str) -> &mut Self {
        self.live_symbols.insert(normalize_symbol(symbol));
        self
    }

    /// Mark `symbol` as on the active-strategy list (deprioritised — evicted last).
    pub fn add_active_symbol(&mut self, symbol: &str) -> &mut Self {
        self.active_strategy_symbols
            .insert(normalize_symbol(symbol));
        self
    }

    /// Mark `symbol` as on the minute-bar watchlist (deprioritised — evicted last).
    pub fn add_watchlist_symbol(&mut self, symbol: &str) -> &mut Self {
        self.minute_bar_watchlist.insert(normalize_symbol(symbol));
        self
    }

    /// Record that `symbol` was most-recently accessed at `access_ts` by a running job. Keeps the
    /// newest timestamp per symbol. The planner re-applies the recency window, so an out-of-window
    /// entry here is harmless.
    pub fn add_recent_access(&mut self, symbol: &str, access_ts: i64) -> &mut Self {
        self.recent_access
            .entry(normalize_symbol(symbol))
            .and_modify(|ts| {
                if access_ts > *ts {
                    *ts = access_ts;
                }
            })
            .or_insert(access_ts);
        self
    }

    /// Merge a whole recency map (e.g. from [`crate::access_journal::AccessJournal::recent`]).
    pub fn extend_recent_access(
        &mut self,
        accesses: impl IntoIterator<Item = (String, i64)>,
    ) -> &mut Self {
        for (symbol, ts) in accesses {
            self.add_recent_access(&symbol, ts);
        }
        self
    }

    /// The live-strategy symbol set.
    pub fn live_symbols(&self) -> &BTreeSet<String> {
        &self.live_symbols
    }

    /// The active-strategy symbol set.
    pub fn active_strategy_symbols(&self) -> &BTreeSet<String> {
        &self.active_strategy_symbols
    }

    /// The minute-bar watchlist symbol set.
    pub fn minute_bar_watchlist(&self) -> &BTreeSet<String> {
        &self.minute_bar_watchlist
    }

    /// The recency access map (symbol → most-recent access_ts).
    pub fn recent_access(&self) -> &BTreeMap<String, i64> {
        &self.recent_access
    }

    /// Whether any protection is configured (used only for reporting; the CLI enforces the
    /// fail-closed "an explicit source is required" gate, not this).
    pub fn is_empty(&self) -> bool {
        self.live_symbols.is_empty()
            && self.active_strategy_symbols.is_empty()
            && self.minute_bar_watchlist.is_empty()
            && self.recent_access.is_empty()
    }

    /// Whether `symbol` (already normalised) is deprioritised (on the active-strategy list or the
    /// minute-bar watchlist).
    fn is_listed(&self, symbol: &str) -> bool {
        self.active_strategy_symbols.contains(symbol) || self.minute_bar_watchlist.contains(symbol)
    }

    /// The pin reason for a normalised `symbol` on `tier` with `event_ts`, or `None` if evictable.
    /// Order matters: `Live` before `Recent` before `Retention` (most-specific first, for reporting).
    fn pin_reason(
        &self,
        symbol: &str,
        tier: Tier,
        event_ts: i64,
        policy: &StoragePolicy,
        now_ts: i64,
        hot_window_start: i64,
    ) -> Option<PinReason> {
        if self.live_symbols.contains(symbol) {
            return Some(PinReason::Live);
        }
        if let Some(&access_ts) = self.recent_access.get(symbol) {
            if access_ts >= policy.recency_window_start(now_ts) {
                return Some(PinReason::Recent);
            }
        }
        if tier == Tier::Hot && event_ts >= hot_window_start {
            return Some(PinReason::Retention);
        }
        None
    }
}

/// One record eligible for the eviction inventory: its natural key and the tier it lives on. The
/// symbol and age are read from the key, so a candidate is just `(key, tier)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionCandidate {
    /// The record's natural key.
    pub key: NaturalKey,
    /// The tier the record lives on.
    pub tier: Tier,
}

impl EvictionCandidate {
    /// Assemble a candidate.
    pub fn new(key: NaturalKey, tier: Tier) -> Self {
        Self { key, tier }
    }
}

/// One planned eviction, in eviction order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlannedEviction {
    /// The record to evict.
    pub key: NaturalKey,
    /// The tier it lives on (Cold entries are physically evicted; Hot entries are reported as deferred).
    pub tier: Tier,
}

/// The computed eviction plan: the ordered set of records to evict to bring usage to the target,
/// honouring every protection, plus the objective evidence (counts, whether the target is reachable).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionPlan {
    /// The SSD capacity (records) the high-water target is a fraction of.
    pub capacity: u64,
    /// The target occupancy (`floor(capacity * high_water / 100)`).
    pub target: u64,
    /// SSD usage before eviction (cold-cache + hot records).
    pub usage_before: u64,
    /// Cold-read cache records in the inventory.
    pub usage_cold: u64,
    /// SSD-hot records in the inventory.
    pub usage_hot: u64,
    /// The ordered records to evict (Cold-before-Hot, then non-listed, then oldest-first).
    pub evict: Vec<PlannedEviction>,
    /// Projected usage after evicting the plan: `usage_before - evict.len()`.
    pub projected_after: u64,
    /// Whether the plan reaches the target (`projected_after <= target`). `false` means the mark
    /// cannot be met without evicting pinned data — the fail-safe residual breach.
    pub reached_target: bool,
    /// How far `projected_after` still exceeds the target (0 when reached).
    pub over_by: u64,
    /// Pinned by live-strategy membership (never evicted).
    pub pinned_live: usize,
    /// Pinned by recency (never evicted).
    pub pinned_recent: usize,
    /// Pinned by the DATA-008 retention floor (never evicted).
    pub pinned_retention: usize,
}

impl EvictionPlan {
    /// The number of Cold-tier (cache) evictions — the records [`EvictionEngine::enforce`] physically
    /// removes.
    pub fn cold_evictions(&self) -> usize {
        self.evict.iter().filter(|e| e.tier == Tier::Cold).count()
    }

    /// The number of Hot-tier evictions the plan identifies — reported as deferred, never physically
    /// removed by [`EvictionEngine::enforce`].
    pub fn hot_evictions(&self) -> usize {
        self.evict.iter().filter(|e| e.tier == Tier::Hot).count()
    }

    /// Total pinned (never-evicted) records across all three reasons.
    pub fn pinned_total(&self) -> usize {
        self.pinned_live + self.pinned_recent + self.pinned_retention
    }
}

/// **The pure eviction planner.** Given the whole SSD inventory, the policy, the protection inputs, the
/// clock, the DATA-008 hot-window boundary, and the SSD capacity, produce the ordered eviction plan.
///
/// A record is **pinned** (never in the plan) if live, recently-accessed within the window, or a hot
/// record inside the retention floor. Evictable records are ordered most-removable first —
/// `(Cold before Hot)` (SYS-68), then `(not-on-active/watchlist first)` (SYS-69), then
/// `(oldest event_ts first)` (SYS-69 "by age"), then natural key (deterministic) — and the first
/// `usage_before - target` of them are selected. If fewer evictable records exist than the deficit,
/// the plan evicts them all and reports `reached_target = false` (the mark is blocked by pinned data).
pub fn plan_eviction(
    candidates: &[EvictionCandidate],
    policy: &StoragePolicy,
    inputs: &ProtectionInputs,
    now_ts: i64,
    hot_window_start: i64,
    capacity_records: u64,
) -> EvictionPlan {
    let target = policy.target_records(capacity_records);
    let usage_before = candidates.len() as u64;
    let usage_cold = candidates.iter().filter(|c| c.tier == Tier::Cold).count() as u64;
    let usage_hot = usage_before - usage_cold;

    // Classify every candidate; collect the evictable ones and count the pinned by reason.
    let mut pinned_live = 0usize;
    let mut pinned_recent = 0usize;
    let mut pinned_retention = 0usize;
    let mut evictable: Vec<&EvictionCandidate> = Vec::new();
    for candidate in candidates {
        let symbol = normalize_symbol(&candidate.key.symbol);
        match inputs.pin_reason(
            &symbol,
            candidate.tier,
            candidate.key.event_ts,
            policy,
            now_ts,
            hot_window_start,
        ) {
            Some(PinReason::Live) => pinned_live += 1,
            Some(PinReason::Recent) => pinned_recent += 1,
            Some(PinReason::Retention) => pinned_retention += 1,
            None => evictable.push(candidate),
        }
    }

    // Already within policy → evict nothing (even if there is evictable data).
    if usage_before <= target {
        return EvictionPlan {
            capacity: capacity_records,
            target,
            usage_before,
            usage_cold,
            usage_hot,
            evict: Vec::new(),
            projected_after: usage_before,
            reached_target: true,
            over_by: 0,
            pinned_live,
            pinned_recent,
            pinned_retention,
        };
    }

    // Order most-removable first: cold-before-hot, then non-listed, then oldest, then key.
    evictable.sort_by(|a, b| {
        a.tier
            .cmp(&b.tier)
            .then_with(|| {
                let la = inputs.is_listed(&normalize_symbol(&a.key.symbol));
                let lb = inputs.is_listed(&normalize_symbol(&b.key.symbol));
                la.cmp(&lb) // false (not listed) < true (listed) → non-listed evicted first
            })
            .then_with(|| a.key.event_ts.cmp(&b.key.event_ts)) // oldest first
            .then_with(|| a.key.cmp(&b.key)) // deterministic tiebreak
    });

    let need = (usage_before - target) as usize;
    let evict: Vec<PlannedEviction> = evictable
        .iter()
        .take(need)
        .map(|c| PlannedEviction {
            key: c.key.clone(),
            tier: c.tier,
        })
        .collect();

    let projected_after = usage_before - evict.len() as u64;
    let reached_target = projected_after <= target;
    let over_by = projected_after.saturating_sub(target);

    EvictionPlan {
        capacity: capacity_records,
        target,
        usage_before,
        usage_cold,
        usage_hot,
        evict,
        projected_after,
        reached_target,
        over_by,
        pinned_live,
        pinned_recent,
        pinned_retention,
    }
}

/// The outcome of [`EvictionEngine::enforce`] — the objective evidence of what was physically evicted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EvictionOutcome {
    /// The SSD capacity (records).
    pub capacity: u64,
    /// The high-water target.
    pub target: u64,
    /// SSD usage before enforcement.
    pub usage_before: u64,
    /// Cold-read cache records physically evicted.
    pub cold_evicted: usize,
    /// Hot-tier records the plan identified but that were **not** physically evicted (enforce never
    /// touches the hot store). The operator resolves SSD-hot pressure via DATA-008 retention archival.
    pub hot_pressure_deferred: usize,
    /// SSD usage after enforcement (`usage_before - cold_evicted`; the hot store is unchanged).
    pub usage_after: u64,
    /// Whether usage is now at or below the target.
    pub reached_target: bool,
    /// Total pinned (never-evicted) records.
    pub pinned_retained: usize,
    /// Pinned by live-strategy membership.
    pub pinned_live: usize,
    /// Pinned by recency.
    pub pinned_recent: usize,
    /// Pinned by the retention floor.
    pub pinned_retention: usize,
}

/// Drives the eviction policy against the live tier directories behind a [`TieredReader`]. Stateless
/// beyond the reader; the durable stores are the state.
#[derive(Debug)]
pub struct EvictionEngine<'a> {
    reader: &'a TieredReader,
}

impl<'a> EvictionEngine<'a> {
    /// Wrap a tiered reader (the DATA-008 tier + DATA-009 cold-read config).
    pub fn new(reader: &'a TieredReader) -> Self {
        Self { reader }
    }

    /// Build the read-only SSD inventory: the cold-read cache records (Tier::Cold) and the SSD-primary
    /// records (Tier::Hot). The cache lives in a subdirectory of the SSD dir with its own store file,
    /// so the two loads are disjoint (no double counting). Returns the candidates, the SSD capacity,
    /// and the hot-window boundary at `now_ts`.
    fn inventory(&self, now_ts: i64) -> Result<(Vec<EvictionCandidate>, u64, i64), EvictionError> {
        let capacity = self.reader.cold_read_config().ssd_capacity_records();
        let hot_window_start = self.reader.tier().config().hot_window_start(now_ts);

        let mut candidates: Vec<EvictionCandidate> = Vec::new();

        // SSD primary (hot). Fail-closed on a missing/corrupt primary (never evict against a partial
        // inventory).
        let ssd_dir = self.reader.tier().config().ssd_dir();
        let hot = MarketDataStore::load_from_path(ssd_dir).map_err(EvictionError::Ssd)?;
        for record in hot.records() {
            candidates.push(EvictionCandidate::new(record.key().clone(), Tier::Hot));
        }

        // Cold-read cache (a subdirectory of SSD). An absent cache is a benign empty (fresh install).
        let cache_dir = self.reader.cold_cache_dir();
        if cache_dir.is_dir() {
            let cache =
                MarketDataStore::load_from_path(&cache_dir).map_err(EvictionError::Cache)?;
            for record in cache.records() {
                candidates.push(EvictionCandidate::new(record.key().clone(), Tier::Cold));
            }
        }

        Ok((candidates, capacity, hot_window_start))
    }

    /// Compute the eviction plan (dry — no mutation).
    pub fn plan(
        &self,
        policy: &StoragePolicy,
        inputs: &ProtectionInputs,
        now_ts: i64,
    ) -> Result<EvictionPlan, EvictionError> {
        let (candidates, capacity, hot_window_start) = self.inventory(now_ts)?;
        Ok(plan_eviction(
            &candidates,
            policy,
            inputs,
            now_ts,
            hot_window_start,
            capacity,
        ))
    }

    /// **Enforce the plan, physically evicting only the cold-read cache.** Cold-tier planned keys are
    /// removed by rewriting the cache store under its single-writer [`StoreLock`] (load → drop keys →
    /// atomic save); the SSD primary is never opened for writing, so hot/pinned data is structurally
    /// safe. Hot-tier plan entries are reported as `hot_pressure_deferred`.
    pub fn enforce(
        &self,
        policy: &StoragePolicy,
        inputs: &ProtectionInputs,
        now_ts: i64,
    ) -> Result<EvictionOutcome, EvictionError> {
        let plan = self.plan(policy, inputs, now_ts)?;

        // The Cold-tier keys the plan selected (planner already excludes every pinned record).
        let cold_keys: BTreeSet<NaturalKey> = plan
            .evict
            .iter()
            .filter(|e| e.tier == Tier::Cold)
            .map(|e| e.key.clone())
            .collect();

        let cold_evicted = if cold_keys.is_empty() {
            0
        } else {
            self.evict_cache_keys(&cold_keys)?
        };

        let hot_pressure_deferred = plan.hot_evictions();
        let usage_after = plan.usage_before - cold_evicted as u64;
        let reached_target = usage_after <= plan.target;

        Ok(EvictionOutcome {
            capacity: plan.capacity,
            target: plan.target,
            usage_before: plan.usage_before,
            cold_evicted,
            hot_pressure_deferred,
            usage_after,
            reached_target,
            pinned_retained: plan.pinned_total(),
            pinned_live: plan.pinned_live,
            pinned_recent: plan.pinned_recent,
            pinned_retention: plan.pinned_retention,
        })
    }

    /// Rewrite the cold-read cache store, dropping every key in `keys`, under the single-writer lock.
    /// Returns the number of records removed. An absent cache directory is a benign no-op.
    fn evict_cache_keys(&self, keys: &BTreeSet<NaturalKey>) -> Result<usize, EvictionError> {
        let dir = self.reader.cold_cache_dir();
        if !dir.is_dir() {
            return Ok(0);
        }
        let _lock = StoreLock::acquire(&dir).map_err(EvictionError::Cache)?;
        let cache = MarketDataStore::load_from_path(&dir).map_err(EvictionError::Cache)?;
        let before = cache.len();
        let mut retained = MarketDataStore::new();
        for record in cache.records() {
            if !keys.contains(record.key()) {
                retained
                    .upsert(record.clone())
                    .map_err(EvictionError::Cache)?;
            }
        }
        let removed = before - retained.len();
        if removed > 0 {
            retained.save_to_path(&dir).map_err(EvictionError::Cache)?;
        }
        Ok(removed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{DatasetKind, MarketDataRecord, MarketField};

    fn key(symbol: &str, event_ts: i64) -> NaturalKey {
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        }
    }

    fn cold(symbol: &str, event_ts: i64) -> EvictionCandidate {
        EvictionCandidate::new(key(symbol, event_ts), Tier::Cold)
    }

    fn hot(symbol: &str, event_ts: i64) -> EvictionCandidate {
        EvictionCandidate::new(key(symbol, event_ts), Tier::Hot)
    }

    // A far-future hot window so every hot candidate here is "cold-resident" (outside the floor) and
    // thus evictable — the retention pin is exercised separately.
    const HWS_ALL_EVICTABLE: i64 = i64::MIN / 2;

    #[test]
    fn policy_rejects_degenerate_config() {
        assert!(StoragePolicy::new(0, 0).is_err());
        assert!(StoragePolicy::new(101, 0).is_err());
        assert!(StoragePolicy::new(80, -1).is_err());
        assert!(StoragePolicy::new(80, 0).is_ok());
        assert!(StoragePolicy::new(100, 86_400).is_ok());
    }

    #[test]
    fn target_is_integer_floor_of_capacity() {
        let p = StoragePolicy::with_defaults();
        assert_eq!(p.target_records(100), 80);
        assert_eq!(p.target_records(10), 8);
        assert_eq!(p.target_records(9), 7); // floor(9*80/100) = floor(7.2) = 7
    }

    #[test]
    fn under_target_evicts_nothing() {
        let candidates = vec![cold("AAPL", 1), cold("MSFT", 2)];
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &ProtectionInputs::new(),
            1_000,
            HWS_ALL_EVICTABLE,
            10, // target 8; usage 2 → nothing to do
        );
        assert!(plan.evict.is_empty());
        assert!(plan.reached_target);
    }

    #[test]
    fn cold_evicted_before_hot() {
        // capacity 4, target floor(4*80/100)=3; usage 4 → evict exactly 1, and it must be the cold one.
        let candidates = vec![
            hot("AAPL", 5),
            hot("MSFT", 6),
            cold("GOOG", 7),
            hot("AMZN", 8),
        ];
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &ProtectionInputs::new(),
            1_000,
            HWS_ALL_EVICTABLE,
            4,
        );
        assert_eq!(plan.evict.len(), 1);
        assert_eq!(plan.evict[0].tier, Tier::Cold);
        assert_eq!(plan.evict[0].key.symbol, "GOOG");
    }

    #[test]
    fn oldest_non_listed_evicted_first() {
        // All cold; capacity 4 target 3; usage 5 → evict 2, oldest non-listed first.
        let candidates = vec![
            cold("AAPL", 100),
            cold("MSFT", 50),
            cold("GOOG", 200),
            cold("AMZN", 10),
            cold("TSLA", 300),
        ];
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &ProtectionInputs::new(),
            10_000,
            HWS_ALL_EVICTABLE,
            4,
        );
        assert_eq!(plan.evict.len(), 2);
        assert_eq!(plan.evict[0].key.symbol, "AMZN"); // ts 10
        assert_eq!(plan.evict[1].key.symbol, "MSFT"); // ts 50
    }

    #[test]
    fn listed_symbols_are_deprioritized() {
        // TSLA is oldest but on the watchlist; the older-but-non-listed AAPL is evicted first.
        let candidates = vec![cold("TSLA", 1), cold("AAPL", 100), cold("MSFT", 200)];
        let mut inputs = ProtectionInputs::new();
        inputs.add_watchlist_symbol("tsla");
        // capacity 3 target 2; usage 3 → evict 1: must skip listed TSLA and take AAPL (next-oldest).
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &inputs,
            10_000,
            HWS_ALL_EVICTABLE,
            3,
        );
        assert_eq!(plan.evict.len(), 1);
        assert_eq!(plan.evict[0].key.symbol, "AAPL");
    }

    #[test]
    fn live_symbols_are_never_evicted_even_under_pressure() {
        // Every record belongs to the live strategy; the mark can't be met without evicting them.
        let candidates = vec![
            cold("AAPL", 1),
            cold("AAPL", 2),
            cold("AAPL", 3),
            cold("AAPL", 4),
        ];
        let mut inputs = ProtectionInputs::new();
        inputs.add_live_symbol("AAPL");
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &inputs,
            10_000,
            HWS_ALL_EVICTABLE,
            4,
        );
        assert!(
            plan.evict.is_empty(),
            "no live-strategy record may be evicted"
        );
        assert!(
            !plan.reached_target,
            "mark left breached rather than evicting live data"
        );
        assert_eq!(plan.pinned_live, 4);
        assert_eq!(plan.over_by, plan.usage_before - plan.target);
    }

    #[test]
    fn recently_accessed_symbols_are_never_evicted() {
        let candidates = vec![
            cold("AAPL", 1),
            cold("MSFT", 2),
            cold("GOOG", 3),
            cold("AMZN", 4),
        ];
        let mut inputs = ProtectionInputs::new();
        // Accessed 100s ago, window 24h → protected.
        inputs.add_recent_access("aapl", 9_900);
        inputs.add_recent_access("msft", 9_950);
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &inputs,
            10_000,
            HWS_ALL_EVICTABLE,
            4,
        );
        // target 3, usage 4 → evict 1; AAPL/MSFT pinned, so evict oldest of GOOG/AMZN = GOOG.
        assert_eq!(plan.evict.len(), 1);
        assert_eq!(plan.evict[0].key.symbol, "GOOG");
        assert_eq!(plan.pinned_recent, 2);
    }

    #[test]
    fn stale_access_outside_window_is_not_protected() {
        let candidates = vec![cold("AAPL", 1), cold("MSFT", 2)];
        let mut inputs = ProtectionInputs::new();
        // Accessed at ts 100 but now is 100000 with a 24h window → out of window, not protected.
        inputs.add_recent_access("aapl", 100);
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &inputs,
            100_000,
            HWS_ALL_EVICTABLE,
            2,
        );
        // target 1, usage 2 → evict 1; AAPL is NOT protected (stale), so oldest AAPL evicted.
        assert_eq!(plan.evict.len(), 1);
        assert_eq!(plan.pinned_recent, 0);
        assert_eq!(plan.evict[0].key.symbol, "AAPL");
    }

    #[test]
    fn hot_data_inside_retention_floor_is_pinned() {
        // now=10_000, hot_window_start=9_000. A hot record at ts 9_500 is inside the floor → pinned.
        let candidates = vec![hot("AAPL", 9_500), cold("MSFT", 1), cold("GOOG", 2)];
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &ProtectionInputs::new(),
            10_000,
            9_000,
            3,
        );
        // target 2, usage 3 → evict 1; hot AAPL is retention-pinned, so evict a cold one (oldest MSFT).
        assert_eq!(plan.pinned_retention, 1);
        assert_eq!(plan.evict.len(), 1);
        assert_eq!(plan.evict[0].tier, Tier::Cold);
        assert_eq!(plan.evict[0].key.symbol, "MSFT");
    }

    #[test]
    fn plan_never_over_evicts_below_target() {
        // usage 10, target 8 → evict exactly 2, never more.
        let candidates: Vec<EvictionCandidate> =
            (0..10).map(|i| cold(&format!("S{i:02}"), i)).collect();
        let plan = plan_eviction(
            &candidates,
            &StoragePolicy::with_defaults(),
            &ProtectionInputs::new(),
            10_000,
            HWS_ALL_EVICTABLE,
            10,
        );
        assert_eq!(plan.evict.len(), 2);
        assert_eq!(plan.projected_after, 8);
        assert!(plan.reached_target);
    }

    #[test]
    fn no_pinned_symbol_ever_appears_in_plan_invariant() {
        // A broad deterministic sweep standing in for a property check.
        for seed in 0..64u64 {
            let mut candidates = Vec::new();
            for i in 0..12u64 {
                let s = format!("S{}", (seed.wrapping_mul(31).wrapping_add(i)) % 5);
                let ts = ((seed.wrapping_add(i)) % 7) as i64 + 1;
                let tier = if (seed + i) % 2 == 0 {
                    Tier::Cold
                } else {
                    Tier::Hot
                };
                candidates.push(EvictionCandidate::new(key(&s, ts), tier));
            }
            let mut inputs = ProtectionInputs::new();
            inputs.add_live_symbol("S0");
            inputs.add_recent_access("S1", 10_000);
            let plan = plan_eviction(
                &candidates,
                &StoragePolicy::with_defaults(),
                &inputs,
                10_000,
                HWS_ALL_EVICTABLE,
                8,
            );
            for e in &plan.evict {
                assert_ne!(e.key.symbol, "S0", "live symbol evicted (seed {seed})");
                assert_ne!(e.key.symbol, "S1", "recent symbol evicted (seed {seed})");
            }
            assert!(plan.projected_after <= plan.usage_before);
        }
    }

    // A record we can actually persist, for the engine end-to-end test below.
    fn record(symbol: &str, event_ts: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            key(symbol, event_ts),
            [MarketField {
                name: "close".to_string(),
                value_minor: 100,
            }],
        )
        .unwrap()
    }

    #[test]
    fn enforce_physically_evicts_cache_and_never_touches_hot() {
        use crate::cold_read::ColdReadConfig;
        use crate::tiering::{TierConfig, TieredStore};

        let base = tempdir();
        let ssd = base.join("ssd");
        let nas = base.join("nas");
        std::fs::create_dir_all(&ssd).unwrap();
        std::fs::create_dir_all(&nas).unwrap();

        // Hot store: 2 records for a live symbol (must never be touched).
        let mut hot_store = MarketDataStore::new();
        hot_store.upsert(record("LIVE", 500)).unwrap();
        hot_store.upsert(record("LIVE", 600)).unwrap();
        hot_store.save_to_path(&ssd).unwrap();
        let hot_bytes_before = std::fs::read(ssd.join("market_data.store")).unwrap();

        // Cold cache: 3 evictable records (old, non-listed).
        let cache_dir = ssd.join(crate::cold_read::COLD_READ_CACHE_SUBDIR);
        std::fs::create_dir_all(&cache_dir).unwrap();
        let mut cache = MarketDataStore::new();
        cache.upsert(record("OLD1", 1)).unwrap();
        cache.upsert(record("OLD2", 2)).unwrap();
        cache.upsert(record("OLD3", 3)).unwrap();
        cache.save_to_path(&cache_dir).unwrap();

        let tier = TieredStore::new(TierConfig::new(&ssd, &nas, 90).unwrap());
        // capacity 5, target floor(5*80/100)=4; usage 5 (2 hot + 3 cold) → evict 1 cold.
        let reader = TieredReader::new(tier, ColdReadConfig::new(5, 20).unwrap());
        let engine = EvictionEngine::new(&reader);

        let mut inputs = ProtectionInputs::new();
        inputs.add_live_symbol("LIVE");

        let outcome = engine
            .enforce(&StoragePolicy::with_defaults(), &inputs, 10_000)
            .unwrap();
        assert_eq!(outcome.cold_evicted, 1);
        assert_eq!(outcome.usage_after, 4);
        assert!(outcome.reached_target);
        assert_eq!(outcome.pinned_live, 2);

        // Hot store byte-identical — enforce never opened the SSD primary.
        let hot_bytes_after = std::fs::read(ssd.join("market_data.store")).unwrap();
        assert_eq!(
            hot_bytes_before, hot_bytes_after,
            "hot store must be untouched"
        );

        // The evicted cache record is the oldest (OLD1, ts 1).
        let cache_after = MarketDataStore::load_from_path(&cache_dir).unwrap();
        assert_eq!(cache_after.len(), 2);
        assert!(cache_after
            .records()
            .iter()
            .all(|r| r.key().symbol != "OLD1"));
    }

    // A minimal, dependency-free temp dir (the crate has no `tempfile` dependency).
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);

    fn tempdir() -> std::path::PathBuf {
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let base =
            std::env::temp_dir().join(format!("atp-eviction-{}-{}", std::process::id(), seq));
        std::fs::create_dir_all(&base).unwrap();
        base
    }
}
