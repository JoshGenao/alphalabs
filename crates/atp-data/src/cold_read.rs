//! **SRS-DATA-009** — transparent **cold-read failover to NAS** + a bounded SSD **cold-read cache**
//! (SyRS SYS-68; StRS SN-1.28 / BG-5).
//!
//! # What SRS-DATA-009 asks for
//!
//! The acceptance criterion: *"Requests outside SSD retention are served from NAS and cached on SSD
//! without requiring consumer code changes; cold-read cache entries do not exceed the configurable
//! SSD share defaulting to 20 percent and are evicted before hot runtime data."* (SYS-68: the unified
//! data-access interface serves reads from SSD within the retention window, **falls back to NAS** for
//! historical data outside it, **without the consumer being aware of the storage tier**; NAS results
//! are **cached on SSD**, the cache is capped at a configurable share of SSD capacity — default 20% —
//! and its entries are **evicted before any hot runtime data**.)
//!
//! # The read path this module adds on top of SRS-DATA-008
//!
//! SRS-DATA-008 ([`crate::tiering`]) owns the SSD-primary / NAS-archival WRITE + retention discipline:
//! ingestion is SSD-first then NAS-synced, and [`TieredStore::archive_cold`] drops *cold* (older than
//! the ≥90-day hot window) records off SSD once they are confirmed byte-identical on NAS (NAS keeps
//! them indefinitely). So after archival a cold record lives **only on NAS**. This module is the READ
//! counterpart: a consumer's historical query is served transparently across the two tiers.
//!
//! [`TieredReader::query`] runs the SRS-DATA-007 [`UnifiedHistoricalQuery`] over the tiers in order:
//!
//!   1. **SSD primary** (the hot/runtime tier) — the fast path; every *hot* datum is here by the
//!      SRS-DATA-008 retention invariant, so a purely-hot query never touches NAS.
//!   2. **SSD cold-read cache** — a *separate* store directory on SSD (`<ssd>/cold_read_cache/`)
//!      holding records previously fetched from NAS. A cache hit avoids the slow NAS read.
//!   3. **NAS archival** — consulted only when the query reaches into **cold territory**
//!      (`start_ts < hot_window_start(now_ts)`), i.e. it may request data archived off SSD. Only
//!      records **older than the hot window** (`event_ts < hot_window_start`) are served from NAS (the
//!      legitimate cold fallback) and written back into the cold-read cache (bounded, below). A record
//!      **inside** the hot window that is on NAS but **missing from SSD** is an SRS-DATA-008
//!      hot-retention breach (hot data must live on SSD): the read **fails closed**
//!      ([`ColdReadError::HotRetentionBreach`]) rather than serving it from NAS — which would mask the
//!      breach and, worse, cache hot data into the cold-read cache.
//!
//! The three results are merged, **deduplicated by natural key**, and returned in the SAME
//! deterministic `event_ts`-ascending order (full natural key as tiebreak) as
//! [`MarketDataStore::query_unified`] — so the tiered read is **parity-equal** to querying a single
//! store holding `SSD ∪ NAS`. That parity is exactly what makes the fallback *transparent*: the
//! consumer passes only the SRS-DATA-007 query dimensions (symbol, resolution, `event_ts` range,
//! optional kind) and gets back the same records regardless of which tier served them — there is no
//! tier selector, no cache-miss to handle, no provider to name.
//!
//! A historical record is **immutable by natural key** (a datum is content-addressed by
//! `(kind, symbol, resolution, event_ts, option_contract)`), so the same key MUST resolve to identical
//! content in every tier. The merge ([`merge_record`]) therefore compares the FULL record on any
//! cross-tier duplicate and **fails closed** ([`ColdReadError::CrossTierDivergence`]) if the content
//! disagrees — most importantly when a stale/corrupt cold-read cache entry that still decodes disagrees
//! with the authoritative NAS record. The reader surfaces that corruption rather than silently
//! shadowing one copy with another.
//!
//! # The cold-read cache — bounded, and evicted before hot data
//!
//! The cache is a distinct [`MarketDataStore`] under the SSD tier. Two invariants realise the AC:
//!
//!   * **Cap ≤ a configurable SSD share (default 20%).** [`ColdReadConfig`] carries the SSD capacity
//!     (in the store's record unit — the deterministic, fixture-testable proxy for byte capacity,
//!     whose real 1 TB figure is the NFR-SC2 deployment concern) and a share percent (default
//!     [`DEFAULT_COLD_READ_CACHE_SHARE_PERCENT`] = 20). The cap is `capacity * share / 100` in **integer
//!     arithmetic** ([`ColdReadConfig::cold_cache_capacity`]) — no floating point, matching the tier's
//!     money/precision discipline. Every cache write enforces `entries <= cap`, evicting to fit, so the
//!     cache can never exceed its share.
//!   * **Evicted before any hot runtime data.** Because the cache is a *physically separate* store from
//!     the SSD primary (hot) tier, reclaiming SSD by draining the cache
//!     ([`TieredReader::evict_cold_cache_to`]) NEVER loads or touches the primary — hot data is
//!     structurally untouchable by cache eviction. The SRS-DATA-010 eviction POLICY (the 80% high-water
//!     trigger, the inactivity recency window, never-evict live-strategy data) drives WHEN to reclaim;
//!     this module gives it the cold-read-cache-first primitive.
//!
//! Intra-cache eviction order (which cold entry to drop when over cap) keeps the **most recent by
//! `event_ts`** and drops the oldest — a deterministic default (no clock, no RNG, full natural key as
//! tiebreak). The access-recency (LRU) refinement of SYS-69 — "data accessed within the recency window
//! is not evicted" — is the SRS-DATA-010 policy owner's concern; this module ships the bounded,
//! hot-segregated cache substrate the policy sits on.
//!
//! # Degraded / fail-closed taxonomy (mirrors SRS-DATA-008)
//!
//! A cold read classifies NAS exactly as the write path does, so the two never disagree:
//!   * **NAS unreachable** (the archival directory is absent/unmounted) — a *recoverable* outage: the
//!     read is served from SSD + cache with `nas_reachable = false`; it does NOT error (the consumer
//!     still gets whatever is resident), and nothing is cached. The operator-alert surface is
//!     SRS-MD-006 / SRS-NOTIF-001.
//!   * **NAS aliases SSD** (a `.`/symlink alias — not an independent archive) — skipped as
//!     unreachable-for-read (`nas_reachable = false`) so the primary is never double-counted into the
//!     cache; the cache directory is likewise refused if it would alias NAS ([`ColdReadError::CacheAliasesNas`]).
//!   * **NAS reachable but the store is corrupt/unreadable** — a real integrity failure, surfaced
//!     fail-closed ([`ColdReadError::Nas`]) rather than silently serving a partial cold result.
//!
//! # Determinism + money discipline
//!
//! The hot/cold boundary is a pure function of `(query, now_ts)` via the caller-supplied `now_ts`
//! (production from the system clock, tests/CLI from `--now`); the module reads no wall-clock. The cap
//! is integer arithmetic; there is no floating point; no `serde` / vendor SDK — only `std::fs` and the
//! zero-dependency `store` codec. Running the same query twice (or over reloaded directories) yields an
//! identical record sequence.

use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use crate::query::UnifiedHistoricalQuery;
use crate::store::{MarketDataRecord, MarketDataStore, NaturalKey, StoreError, StoreLock};
use crate::tiering::TieredStore;

/// The subdirectory, under the SSD primary tier, that holds the cold-read cache store. Distinct from
/// the SSD primary store file (`market_data.store` lives directly in the SSD dir), so the cache and the
/// primary never share a store file — the segregation that makes "evicted before hot data" structural.
pub const COLD_READ_CACHE_SUBDIR: &str = "cold_read_cache";

/// The default cold-read cache share of SSD capacity when an operator does not configure one — the
/// SRS-DATA-009 / SYS-68 default of **20 percent**.
pub const DEFAULT_COLD_READ_CACHE_SHARE_PERCENT: u32 = 20;

/// The maximum permitted cache share: a cold-read cache cannot exceed **100%** of SSD capacity. A
/// larger share is rejected fail-closed at [`ColdReadConfig::new`].
pub const MAX_COLD_READ_CACHE_SHARE_PERCENT: u32 = 100;

/// A fail-closed error from a cold-read operation. Tags WHICH tier/store failed so a primary (SSD)
/// read failure, a NAS (archival) failure, and a cold-read-cache failure are never confused, and a
/// configuration error is distinct from any I/O.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ColdReadError {
    /// SSD capacity was configured as zero — a cold-read cache is a *share of SSD capacity*, and a
    /// share of nothing is meaningless. Rejected at construction.
    ZeroSsdCapacity,
    /// The configured cache share percent exceeds [`MAX_COLD_READ_CACHE_SHARE_PERCENT`] (100%) — a
    /// cold-read cache cannot exceed SSD capacity. Rejected at construction.
    CacheShareAboveMax {
        /// The rejected operator-configured share.
        configured: u32,
        /// The enforced maximum ([`MAX_COLD_READ_CACHE_SHARE_PERCENT`]).
        max: u32,
    },
    /// The cold-read cache directory (under SSD) would alias the NAS tier. The cache must live on SSD,
    /// distinct from the archival tier; an alias would let a cache write corrupt/double-count NAS.
    CacheAliasesNas {
        /// The cache directory that resolved to the NAS tier.
        dir: PathBuf,
    },
    /// A read of the **SSD primary** (hot) tier failed. Fatal — the primary is the source of truth.
    Ssd(StoreError),
    /// A read of the **NAS archival** tier failed where it was reachable but the store was
    /// corrupt/unreadable — a real integrity failure, surfaced rather than serving a partial cold read.
    /// (An *unreachable* NAS is NOT this error: it degrades to a resident-only result.)
    Nas(StoreError),
    /// A read/write of the **SSD cold-read cache** store failed (lock contention, corrupt cache, or
    /// I/O). The cache is an acceleration layer, but a failed cache write is surfaced fail-closed so a
    /// silently-unbounded or corrupt cache can never be mistaken for a healthy one.
    Cache(StoreError),
    /// The SAME natural key resolved to **different record content** across the tiers (SSD primary,
    /// cold-read cache, and/or NAS). A historical record is immutable by natural key, so a cross-tier
    /// value divergence is data corruption — most importantly a **stale/corrupt cold-read cache entry
    /// that still decodes** but disagrees with the authoritative NAS record. The read fails closed
    /// (surfacing the divergence) rather than silently returning one copy and hiding the corruption;
    /// the operator can clear the cold-read cache directory to recover.
    CrossTierDivergence {
        /// A human-readable identity of the diverging record's natural key.
        key: String,
    },
    /// A record INSIDE the hot-retention window (`event_ts >= hot_window_start`) was found on NAS but
    /// is **missing from SSD** — an SRS-DATA-008 hot-retention breach (hot data must live on the SSD
    /// primary tier). The cold-read path refuses to serve it from NAS (which would silently mask the
    /// breach and, worse, cache hot data into the cold-read cache); it fails closed so the breach is
    /// surfaced. This is distinct from a legitimate COLD (`event_ts < hot_window_start`) fallback.
    HotRetentionBreach {
        /// A human-readable identity of the hot record's natural key.
        key: String,
        /// The inclusive lower bound of the hot window the record fell inside.
        hot_window_start: i64,
    },
}

impl fmt::Display for ColdReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::ZeroSsdCapacity => write!(
                f,
                "SSD capacity must be > 0 (the cold-read cache is a share of SSD capacity)"
            ),
            Self::CacheShareAboveMax { configured, max } => write!(
                f,
                "cold-read cache share {configured}% exceeds the maximum of {max}% of SSD capacity"
            ),
            Self::CacheAliasesNas { dir } => write!(
                f,
                "cold-read cache directory {} aliases the NAS tier (it must live on SSD, distinct \
                 from the archival tier)",
                dir.display()
            ),
            Self::Ssd(err) => write!(f, "SSD (primary) read error: {err}"),
            Self::Nas(err) => write!(f, "NAS (archival) cold-read error: {err}"),
            Self::Cache(err) => write!(f, "SSD cold-read cache error: {err}"),
            Self::CrossTierDivergence { key } => write!(
                f,
                "cross-tier divergence: the record for {key} differs in content across the SSD / \
                 cold-read cache / NAS tiers (a historical record is immutable by natural key, so a \
                 divergence is corruption — clear the cold-read cache directory to recover)"
            ),
            Self::HotRetentionBreach {
                key,
                hot_window_start,
            } => write!(
                f,
                "SRS-DATA-008 hot-retention breach: the in-window record for {key} \
                 (event_ts >= {hot_window_start}) is on NAS but missing from the SSD primary tier — \
                 hot data must live on SSD; the cold-read path fails closed rather than serving it \
                 from NAS (which would mask the breach and cache hot data as cold)"
            ),
        }
    }
}

impl Error for ColdReadError {}

/// Validated configuration of the cold-read cache: the SSD capacity it is a share of, and that share.
///
/// Capacity is expressed in the store's **record unit** — the deterministic, fixture-testable proxy
/// the tier already uses; the real byte capacity (1 TB SSD) is the NFR-SC2 deployment concern. The cap
/// is computed in integer arithmetic ([`cold_cache_capacity`](Self::cold_cache_capacity)), so there is
/// no floating point anywhere in the cache bound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColdReadConfig {
    ssd_capacity_records: u64,
    cache_share_percent: u32,
}

impl ColdReadConfig {
    /// Build a validated config, failing closed if `ssd_capacity_records` is zero or
    /// `cache_share_percent` exceeds [`MAX_COLD_READ_CACHE_SHARE_PERCENT`] (100%).
    pub fn new(ssd_capacity_records: u64, cache_share_percent: u32) -> Result<Self, ColdReadError> {
        if ssd_capacity_records == 0 {
            return Err(ColdReadError::ZeroSsdCapacity);
        }
        if cache_share_percent > MAX_COLD_READ_CACHE_SHARE_PERCENT {
            return Err(ColdReadError::CacheShareAboveMax {
                configured: cache_share_percent,
                max: MAX_COLD_READ_CACHE_SHARE_PERCENT,
            });
        }
        Ok(Self {
            ssd_capacity_records,
            cache_share_percent,
        })
    }

    /// Build a config at the SRS-DATA-009 default share ([`DEFAULT_COLD_READ_CACHE_SHARE_PERCENT`] =
    /// 20%).
    pub fn with_default_share(ssd_capacity_records: u64) -> Result<Self, ColdReadError> {
        Self::new(ssd_capacity_records, DEFAULT_COLD_READ_CACHE_SHARE_PERCENT)
    }

    /// The configured SSD capacity, in records.
    pub fn ssd_capacity_records(&self) -> u64 {
        self.ssd_capacity_records
    }

    /// The configured cold-read cache share of SSD capacity, in percent.
    pub fn cache_share_percent(&self) -> u32 {
        self.cache_share_percent
    }

    /// The cold-read cache capacity in records: `floor(ssd_capacity_records * cache_share_percent /
    /// 100)`. **Integer arithmetic** (saturating multiply, then integer divide) — no floating point,
    /// so the cap is exact and deterministic. A 20% share of a 100-record SSD is 20 records.
    pub fn cold_cache_capacity(&self) -> u64 {
        self.ssd_capacity_records
            .saturating_mul(self.cache_share_percent as u64)
            / 100
    }
}

/// The provenance-annotated result of a [`TieredReader::query`]. Carries the merged records (owned, in
/// the same deterministic `event_ts`-ascending order as [`MarketDataStore::query_unified`]) plus
/// per-tier counts — the objective evidence that the cold-read failover happened, the cache was
/// populated, and the cap was honored. A consumer that only wants the data reads [`records`](Self::records)
/// and ignores the rest: the extra fields are evidence, not required for use (so the fallback stays
/// transparent).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TieredReadResult {
    /// The queried symbol (echoed for a self-describing result).
    pub symbol: String,
    /// The queried resolution (echoed for a self-describing result).
    pub resolution: String,
    /// The merged, deduplicated records in canonical `event_ts`-ascending order — identical to
    /// querying a single store holding `SSD ∪ NAS`.
    pub records: Vec<MarketDataRecord>,
    /// Records served from the SSD primary (hot) tier.
    pub served_from_ssd: usize,
    /// Records served from the SSD cold-read cache (a prior NAS fetch that was still resident).
    pub served_from_cache: usize,
    /// Records served from NAS as a cold-read fallback (absent from SSD + cache) — the transparent
    /// failover. These are the records written back into the cache.
    pub served_from_nas: usize,
    /// Of the NAS-served records, how many are resident in the cache AFTER this read's bounded
    /// write-back (some may be evicted immediately if the cache is at cap with more-recent data).
    pub newly_cached: usize,
    /// Cache entries removed to honor the cap during this read's write-back.
    pub cache_evicted: usize,
    /// Whether NAS was consulted (the query reached cold territory outside SSD retention).
    pub nas_consulted: bool,
    /// Whether NAS was reachable + independent when consulted (false if unreachable/aliased — a
    /// degraded cold read served from SSD + cache only).
    pub nas_reachable: bool,
    /// Cold-read cache entry count after this read (for cap inspection).
    pub cold_cache_entries: usize,
    /// The configured cold-read cache capacity (the cap) in records.
    pub cold_cache_capacity: u64,
}

impl TieredReadResult {
    /// The number of merged records.
    pub fn len(&self) -> usize {
        self.records.len()
    }

    /// Whether the query matched no records (a valid empty result, not an error).
    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    /// The merged records, in canonical `event_ts`-ascending order.
    pub fn records(&self) -> &[MarketDataRecord] {
        &self.records
    }

    /// Whether the cold-read cache is within its configured cap after this read (the SRS-DATA-009
    /// share ceiling). Always true for a correct implementation — surfaced as inspectable evidence.
    pub fn cold_cache_within_cap(&self) -> bool {
        self.cold_cache_entries as u64 <= self.cold_cache_capacity
    }
}

/// An inspection of the cold-read cache's occupancy against its cap — the objective evidence for the
/// "cache entries do not exceed the configurable SSD share" clause.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColdCacheReport {
    /// Cold-read cache entries currently resident.
    pub entries: usize,
    /// The cap: `floor(ssd_capacity_records * share_percent / 100)`.
    pub capacity: u64,
    /// The configured share percent (default 20).
    pub share_percent: u32,
    /// The configured SSD capacity (records) the share is taken of.
    pub ssd_capacity_records: u64,
}

impl ColdCacheReport {
    /// Whether the cache is within its cap (never exceeds the configurable SSD share).
    pub fn within_cap(&self) -> bool {
        self.entries as u64 <= self.capacity
    }
}

/// The **transparent tiered reader** (SRS-DATA-009): a [`TieredStore`] plus a [`ColdReadConfig`],
/// exposing a single historical-query surface that serves from SSD, falls back to NAS for cold data,
/// and caches cold-read results on SSD within the configured share — all without the consumer being
/// aware of the tier. Stateless beyond its config; the durable tiers + cache directory are the state,
/// so two readers over the same directories are interchangeable.
#[derive(Debug, Clone)]
pub struct TieredReader {
    tier: TieredStore,
    cold_read: ColdReadConfig,
}

impl TieredReader {
    /// Wrap a [`TieredStore`] and a validated [`ColdReadConfig`].
    pub fn new(tier: TieredStore, cold_read: ColdReadConfig) -> Self {
        Self { tier, cold_read }
    }

    /// The underlying tier coordinator.
    pub fn tier(&self) -> &TieredStore {
        &self.tier
    }

    /// The cold-read configuration.
    pub fn cold_read_config(&self) -> &ColdReadConfig {
        &self.cold_read
    }

    /// The cold-read cache directory: `<ssd>/cold_read_cache/`. On SSD (so the cache consumes SSD
    /// capacity, per "cached on SSD"), and a subdirectory so it never shares a store file with the SSD
    /// primary — the separation that keeps hot data untouchable by cache eviction.
    pub fn cold_cache_dir(&self) -> PathBuf {
        self.tier.config().ssd_dir().join(COLD_READ_CACHE_SUBDIR)
    }

    /// **Transparently serve a historical query across the tiers** (the SRS-DATA-009 read path).
    ///
    /// Reads SSD primary → cold-read cache → (for cold ranges) NAS, merges into the same deterministic
    /// `event_ts`-ascending order as a single-store [`MarketDataStore::query_unified`], and writes
    /// NAS-served records back into the bounded cold-read cache. Returns the merged records plus
    /// per-tier provenance. `now_ts` is the deterministic retention-boundary instant (NOT a tier
    /// selector) — the same as-of instant a point-in-time consumer already supplies.
    ///
    /// NAS is consulted **only** when the query reaches cold territory
    /// (`start_ts < hot_window_start(now_ts)`): a purely-hot query is fully served from SSD by the
    /// SRS-DATA-008 retention invariant, so the fast path never pays the slow NAS read (NFR-SC2).
    pub fn query(
        &self,
        query: &UnifiedHistoricalQuery,
        now_ts: i64,
    ) -> Result<TieredReadResult, ColdReadError> {
        // The cache lives on SSD; refuse a configuration where it would alias NAS (a cache write would
        // then corrupt/double-count the archive). TierConfig already guarantees ssd != nas.
        let cache_dir = self.cold_cache_dir();
        if same_directory(&cache_dir, self.tier.config().nas_dir()) {
            return Err(ColdReadError::CacheAliasesNas { dir: cache_dir });
        }

        // Merge the tiers keyed by natural key. A key that resolves to DIFFERENT content across tiers
        // is corruption (a historical record is immutable by natural key) — most importantly a
        // stale/corrupt cold-read cache entry that still decodes but disagrees with the authoritative
        // NAS record. `merge_record` fails closed on such a divergence rather than letting one copy
        // silently shadow the other, so the reader can never hide historical-data corruption.
        let mut assembled: Vec<MarketDataRecord> = Vec::new();
        let mut index: HashMap<NaturalKey, usize> = HashMap::new();

        // 1. SSD PRIMARY (hot/runtime). Fails closed on a missing SSD directory (never a silent empty).
        let ssd = MarketDataStore::load_from_path(self.tier.config().ssd_dir())
            .map_err(ColdReadError::Ssd)?;
        let mut served_from_ssd = 0;
        for record in ssd.query_unified(query).records() {
            if merge_record(&mut assembled, &mut index, record)? {
                served_from_ssd += 1;
            }
        }

        // 2. SSD COLD-READ CACHE (a prior NAS fetch). Absent cache dir → nothing cached yet. A cache
        // record whose key is already present from SSD must MATCH it (else divergence, fail closed).
        let mut served_from_cache = 0;
        if cache_dir.is_dir() {
            let cache =
                MarketDataStore::load_from_path(&cache_dir).map_err(ColdReadError::Cache)?;
            for record in cache.query_unified(query).records() {
                if merge_record(&mut assembled, &mut index, record)? {
                    served_from_cache += 1;
                }
            }
        }

        // 3. NAS FALLBACK — only when the query reaches cold territory (outside SSD retention). NAS is
        // the archival source of truth: a cache/SSD record that disagrees with the NAS record for the
        // same key trips `merge_record`'s fail-closed divergence guard here (the cache was inserted
        // first, so the NAS record for the same key is compared against it).
        let hot_window_start = self.tier.config().hot_window_start(now_ts);
        let reaches_cold = query.start_ts <= query.end_ts && query.start_ts < hot_window_start;
        let mut served_from_nas = 0;
        let mut nas_reachable = false;
        let mut to_cache: Vec<MarketDataRecord> = Vec::new();
        if reaches_cold {
            match self.classify_nas() {
                NasReadAccess::Ready => {
                    nas_reachable = true;
                    // A reachable-but-corrupt NAS store is a real integrity failure — fail closed
                    // rather than serve a partial cold read.
                    let nas = MarketDataStore::load_from_path(self.tier.config().nas_dir())
                        .map_err(ColdReadError::Nas)?;
                    // NAS serves the COLD portion only. A mixed hot/cold query still consults NAS (it
                    // reaches cold territory), but only records OLDER than the hot window are legitimate
                    // cold-read fallbacks. A record INSIDE the hot window that is on NAS but missing from
                    // SSD is NOT served from NAS — that would mask an SRS-DATA-008 hot-retention breach
                    // (hot data must live on SSD). Such a record fails closed as a distinct
                    // [`ColdReadError::HotRetentionBreach`]. A hot NAS record that IS on SSD is the
                    // normal superset case (already in `index` from step 1 → a `merge_record` dedup).
                    for record in nas.query_unified(query).records() {
                        if index.contains_key(record.key()) {
                            // Present from SSD/cache — verify no cross-tier value divergence.
                            merge_record(&mut assembled, &mut index, record)?;
                        } else if record.key().event_ts < hot_window_start {
                            // A cold record only on NAS — the legitimate transparent fallback.
                            merge_record(&mut assembled, &mut index, record)?;
                            served_from_nas += 1;
                            to_cache.push((*record).clone());
                        } else {
                            // A HOT record on NAS but missing from SSD — an SRS-DATA-008 retention
                            // breach. Fail closed; never serve or cache it as if it were cold.
                            return Err(ColdReadError::HotRetentionBreach {
                                key: describe_key(record.key()),
                                hot_window_start,
                            });
                        }
                    }
                }
                // Unreachable / aliased NAS: a degraded cold read served from SSD + cache only. Not an
                // error — the consumer still gets what is resident; the operator-alert surface is
                // SRS-MD-006 / SRS-NOTIF-001. Nothing is cached.
                NasReadAccess::Unreachable | NasReadAccess::Aliased => {}
            }
        }

        // Cache write-back (bounded): persist NAS-served records into the cold-read cache, evicting to
        // honor the cap. Never touches the SSD primary (hot) store.
        let (newly_cached, cache_evicted, cold_cache_entries) = if to_cache.is_empty() {
            (0, 0, self.cache_entry_count(&cache_dir)?)
        } else {
            self.write_back_cache(&cache_dir, &to_cache)?
        };

        // Merge order: `event_ts` ascending, full natural key as the deterministic tiebreak — identical
        // to query_unified, so the tiered read is parity-equal to querying `SSD ∪ NAS` as one store.
        assembled.sort_by(|a, b| {
            let (ka, kb) = (a.key(), b.key());
            ka.event_ts.cmp(&kb.event_ts).then_with(|| ka.cmp(kb))
        });

        Ok(TieredReadResult {
            symbol: query.symbol.clone(),
            resolution: query.resolution.clone(),
            records: assembled,
            served_from_ssd,
            served_from_cache,
            served_from_nas,
            newly_cached,
            cache_evicted,
            nas_consulted: reaches_cold,
            nas_reachable,
            cold_cache_entries,
            cold_cache_capacity: self.cold_read.cold_cache_capacity(),
        })
    }

    /// Inspect the cold-read cache occupancy against its cap — the objective "cache ≤ configurable SSD
    /// share" evidence. An absent cache directory reports 0 entries.
    pub fn cold_cache_report(&self) -> Result<ColdCacheReport, ColdReadError> {
        let entries = self.cache_entry_count(&self.cold_cache_dir())?;
        Ok(ColdCacheReport {
            entries,
            capacity: self.cold_read.cold_cache_capacity(),
            share_percent: self.cold_read.cache_share_percent(),
            ssd_capacity_records: self.cold_read.ssd_capacity_records(),
        })
    }

    /// **Evict cold-read cache entries down to at most `max_entries`, never touching hot data** — the
    /// SRS-DATA-009 "evicted before hot runtime data" primitive the SRS-DATA-010 policy drives.
    ///
    /// Keeps the `max_entries` most-recent (highest `event_ts`) cache entries and drops the rest. It
    /// loads and rewrites ONLY the cold-read cache store — the SSD primary (hot) store is never opened,
    /// so hot runtime data is structurally impossible to evict here. Returns the number evicted. An
    /// absent cache directory is a benign no-op (`Ok(0)`).
    pub fn evict_cold_cache_to(&self, max_entries: u64) -> Result<usize, ColdReadError> {
        let dir = self.cold_cache_dir();
        if !dir.is_dir() {
            return Ok(0);
        }
        let _lock = StoreLock::acquire(&dir).map_err(ColdReadError::Cache)?;
        let cache = MarketDataStore::load_from_path(&dir).map_err(ColdReadError::Cache)?;
        let before = cache.len();
        if (before as u64) <= max_entries {
            return Ok(0);
        }
        let survivors = keep_most_recent(cache.records(), max_entries);
        let kept = survivors.len();
        store_of(&survivors)
            .save_to_path(&dir)
            .map_err(ColdReadError::Cache)?;
        Ok(before - kept)
    }

    /// Count cold-read cache entries (0 if the directory does not exist yet).
    fn cache_entry_count(&self, dir: &Path) -> Result<usize, ColdReadError> {
        if !dir.is_dir() {
            return Ok(0);
        }
        Ok(MarketDataStore::load_from_path(dir)
            .map_err(ColdReadError::Cache)?
            .len())
    }

    /// Persist `to_cache` into the cold-read cache under its single-writer lock, enforcing the cap by
    /// keeping the most-recent (highest `event_ts`) entries. Returns
    /// `(newly_cached, evicted, final_entries)`. Only the cache store is touched.
    fn write_back_cache(
        &self,
        dir: &Path,
        to_cache: &[MarketDataRecord],
    ) -> Result<(usize, usize, usize), ColdReadError> {
        fs::create_dir_all(dir).map_err(|_| {
            ColdReadError::Cache(StoreError::Io {
                context: "create cold-read cache directory",
            })
        })?;
        let _lock = StoreLock::acquire(dir).map_err(ColdReadError::Cache)?;
        let mut cache = MarketDataStore::load_from_path(dir).map_err(ColdReadError::Cache)?;
        for record in to_cache {
            cache.upsert(record.clone()).map_err(ColdReadError::Cache)?;
        }
        let after_insert = cache.len();

        let cap = self.cold_read.cold_cache_capacity();
        let survivors = keep_most_recent(cache.records(), cap);
        let survivor_keys: HashSet<&NaturalKey> = survivors.iter().map(|r| r.key()).collect();
        let newly_cached = to_cache
            .iter()
            .filter(|r| survivor_keys.contains(r.key()))
            .count();
        let final_entries = survivors.len();
        let evicted = after_insert.saturating_sub(final_entries);

        store_of(&survivors)
            .save_to_path(dir)
            .map_err(ColdReadError::Cache)?;
        Ok((newly_cached, evicted, final_entries))
    }

    /// Classify the NAS archival tier for a cold READ: reachable+independent, unreachable, or an SSD
    /// alias. Mirrors the SRS-DATA-008 write-path classification so read and write never disagree.
    fn classify_nas(&self) -> NasReadAccess {
        let nas = self.tier.config().nas_dir();
        if !nas.is_dir() {
            return NasReadAccess::Unreachable;
        }
        if same_directory(self.tier.config().ssd_dir(), nas) {
            return NasReadAccess::Aliased;
        }
        NasReadAccess::Ready
    }
}

/// How the NAS archival tier classifies for a cold read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NasReadAccess {
    /// A provisioned directory distinct from SSD — safe to read + cache from.
    Ready,
    /// Absent/unmounted — a recoverable outage; the read degrades to a resident-only result.
    Unreachable,
    /// Resolves to the SSD tier (a `.`/symlink alias) — not an independent archive.
    Aliased,
}

/// The `max` records with the highest `event_ts` (full natural key as the deterministic tiebreak),
/// returned in an unspecified order (the caller rebuilds a canonical store via [`store_of`]). Used
/// both for the cache write-back cap and for [`TieredReader::evict_cold_cache_to`]. `max == 0` keeps
/// nothing; `max >= len` keeps everything.
fn keep_most_recent(records: &[MarketDataRecord], max: u64) -> Vec<MarketDataRecord> {
    let keep = max.min(records.len() as u64) as usize;
    if keep == 0 {
        return Vec::new();
    }
    let mut sorted: Vec<MarketDataRecord> = records.to_vec();
    // Descending by event_ts, then descending full key — so `[..keep]` is the most-recent slice.
    sorted.sort_by(|a, b| {
        let (ka, kb) = (a.key(), b.key());
        kb.event_ts.cmp(&ka.event_ts).then_with(|| kb.cmp(ka))
    });
    sorted.truncate(keep);
    sorted
}

/// Merge `record` into the `assembled` result set, deduplicated by natural key with a **fail-closed
/// cross-tier value check**. Returns `Ok(true)` if the key was new (inserted), `Ok(false)` if it was
/// already present with **byte-identical** content (a legitimate dedup — e.g. a cache hit that matches
/// NAS), or [`ColdReadError::CrossTierDivergence`] if the key was present with DIFFERENT content (a
/// stale/corrupt cache or a corrupt tier). This is what stops a divergent cache entry from silently
/// shadowing the authoritative NAS record.
fn merge_record(
    assembled: &mut Vec<MarketDataRecord>,
    index: &mut HashMap<NaturalKey, usize>,
    record: &MarketDataRecord,
) -> Result<bool, ColdReadError> {
    match index.get(record.key()) {
        None => {
            index.insert(record.key().clone(), assembled.len());
            assembled.push(record.clone());
            Ok(true)
        }
        // `MarketDataRecord: PartialEq` compares key + all value fields; the keys are equal here, so
        // this is a full value comparison. Equal => dedup; different => corruption, fail closed.
        Some(&i) if &assembled[i] == record => Ok(false),
        Some(_) => Err(ColdReadError::CrossTierDivergence {
            key: describe_key(record.key()),
        }),
    }
}

/// A stable, human-readable identity of a natural key for a divergence error message.
fn describe_key(key: &NaturalKey) -> String {
    format!(
        "{}/{}/{}@{}{}",
        key.kind.as_str(),
        key.symbol,
        key.resolution,
        key.event_ts,
        key.option_contract
            .as_deref()
            .map(|c| format!(" [{c}]"))
            .unwrap_or_default()
    )
}

/// Build a canonical [`MarketDataStore`] from a record slice (restores natural-key order + validation).
fn store_of(records: &[MarketDataRecord]) -> MarketDataStore {
    let mut store = MarketDataStore::new();
    for record in records {
        // Records already passed `MarketDataRecord::new` validation; upsert re-canonicalizes order.
        // A conflicting duplicate is impossible here (all keys are distinct in the source store).
        let _ = store.upsert(record.clone());
    }
    store
}

/// Whether two directory paths denote the **same directory** — a lexical alias (`.`/trailing-slash via
/// [`Path::components`]) or, when both exist, a symlink/hardlink alias (via [`Path::canonicalize`]).
/// Local to this module (the tier's own equivalent is private) so the cold-read path can refuse a
/// cache/NAS alias without reaching into [`crate::tiering`].
fn same_directory(a: &Path, b: &Path) -> bool {
    if a.components().eq(b.components()) {
        return true;
    }
    matches!((a.canonicalize(), b.canonicalize()), (Ok(ra), Ok(rb)) if ra == rb)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{DatasetKind, MarketField, NaturalKey};
    use crate::tiering::{TierConfig, DEFAULT_HOT_RETENTION_DAYS};
    use std::sync::atomic::{AtomicU64, Ordering};

    // A fixed "now" well past the epoch so a 90-day hot window has room below it for cold data.
    // 2023-11-14T22:13:20Z.
    const NOW: i64 = 1_700_000_000;
    const DAY: i64 = 86_400;

    // Unique temp dirs per test (no wall-clock / RNG — a process-local monotonic counter keeps
    // parallel tests isolated). Cleaned up on drop.
    static SEQ: AtomicU64 = AtomicU64::new(0);

    struct TempTree {
        root: PathBuf,
    }

    impl TempTree {
        fn new(tag: &str) -> Self {
            let seq = SEQ.fetch_add(1, Ordering::Relaxed);
            let root = std::env::temp_dir()
                .join(format!("atp-data009-{tag}-{}-{seq}", std::process::id()));
            let _ = fs::remove_dir_all(&root);
            fs::create_dir_all(&root).expect("create temp root");
            Self { root }
        }
        fn dir(&self, name: &str) -> PathBuf {
            let p = self.root.join(name);
            fs::create_dir_all(&p).expect("create subdir");
            p
        }
    }

    impl Drop for TempTree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    fn daily(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [MarketField {
                name: "close".to_string(),
                value_minor: close,
            }],
        )
        .expect("well-formed daily fixture")
    }

    /// Persist `records` into a store directory (the SSD or NAS tier) directly.
    fn seed_store(dir: &Path, records: &[MarketDataRecord]) {
        let mut store = MarketDataStore::new();
        for r in records {
            store.upsert(r.clone()).expect("seed upsert");
        }
        store.save_to_path(dir).expect("seed save");
    }

    fn reader(ssd: &Path, nas: &Path, capacity: u64, share: u32) -> TieredReader {
        let config = TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).expect("tier config");
        TieredReader::new(
            TieredStore::new(config),
            ColdReadConfig::new(capacity, share).expect("cold-read config"),
        )
    }

    fn cold_ts(days_before_window: i64) -> i64 {
        // hot_window_start ≈ NOW - 90d; place a record safely before it.
        NOW - (DEFAULT_HOT_RETENTION_DAYS as i64 + days_before_window) * DAY
    }

    // ---- config / cap arithmetic ---------------------------------------------------------------

    #[test]
    fn cap_is_integer_share_of_capacity() {
        assert_eq!(
            ColdReadConfig::new(100, 20).unwrap().cold_cache_capacity(),
            20
        );
        assert_eq!(
            ColdReadConfig::new(10, 20).unwrap().cold_cache_capacity(),
            2
        );
        // floor: 7 * 20 / 100 = 1
        assert_eq!(ColdReadConfig::new(7, 20).unwrap().cold_cache_capacity(), 1);
        // default share is 20%
        assert_eq!(
            ColdReadConfig::with_default_share(50)
                .unwrap()
                .cache_share_percent(),
            DEFAULT_COLD_READ_CACHE_SHARE_PERCENT
        );
    }

    #[test]
    fn config_fails_closed_on_bad_values() {
        assert_eq!(
            ColdReadConfig::new(0, 20),
            Err(ColdReadError::ZeroSsdCapacity)
        );
        assert_eq!(
            ColdReadConfig::new(100, 101),
            Err(ColdReadError::CacheShareAboveMax {
                configured: 101,
                max: 100
            })
        );
    }

    // ---- transparent hot read (no NAS) ---------------------------------------------------------

    #[test]
    fn hot_query_served_from_ssd_without_consulting_nas() {
        let t = TempTree::new("hot");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let hot = daily("AAPL", NOW - DAY, 100); // inside the hot window
        seed_store(&ssd, std::slice::from_ref(&hot));
        // NAS empty; a hot query must not need it.
        seed_store(&nas, &[]);

        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", NOW - 2 * DAY, NOW);
        let out = r.query(&q, NOW).unwrap();

        assert_eq!(out.len(), 1);
        assert_eq!(out.served_from_ssd, 1);
        assert_eq!(out.served_from_nas, 0);
        assert!(!out.nas_consulted, "hot-only query must not consult NAS");
        assert_eq!(out.records()[0].key().event_ts, NOW - DAY);
    }

    // ---- cold read: transparent NAS fallback + cache populate ----------------------------------

    #[test]
    fn cold_query_falls_back_to_nas_and_caches_on_ssd() {
        let t = TempTree::new("cold");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let cold = daily("AAPL", cold_ts(10), 90); // archived off SSD, only on NAS
        seed_store(&ssd, &[]); // SSD has none of it (post-archive)
        seed_store(&nas, std::slice::from_ref(&cold));

        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();

        assert_eq!(out.len(), 1, "the cold record is served transparently");
        assert_eq!(out.served_from_ssd, 0);
        assert_eq!(out.served_from_nas, 1);
        assert!(out.nas_consulted && out.nas_reachable);
        assert_eq!(out.newly_cached, 1);
        assert_eq!(out.records()[0].key().event_ts, cold_ts(10));

        // Second identical read is now a CACHE hit — no NAS fetch needed for that record.
        let out2 = r.query(&q, NOW).unwrap();
        assert_eq!(out2.len(), 1);
        assert_eq!(out2.served_from_cache, 1);
        assert_eq!(out2.served_from_nas, 0);
    }

    // ---- transparency parity: tiered read == query over SSD ∪ NAS ------------------------------

    #[test]
    fn tiered_read_is_parity_equal_to_union_query() {
        let t = TempTree::new("parity");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let hot = daily("AAPL", NOW - DAY, 111);
        let cold1 = daily("AAPL", cold_ts(5), 50);
        let cold2 = daily("AAPL", cold_ts(15), 40);
        // SSD holds the hot record; NAS is the superset (all three).
        seed_store(&ssd, std::slice::from_ref(&hot));
        seed_store(&nas, &[hot.clone(), cold1.clone(), cold2.clone()]);

        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(30), NOW);
        let out = r.query(&q, NOW).unwrap();

        // Expected: query_unified over a single store holding SSD ∪ NAS.
        let mut union = MarketDataStore::new();
        for rec in [&hot, &cold1, &cold2] {
            union.upsert(rec.clone()).unwrap();
        }
        let expected: Vec<i64> = union
            .query_unified(&q)
            .records()
            .iter()
            .map(|r| r.key().event_ts)
            .collect();
        let got: Vec<i64> = out.records().iter().map(|r| r.key().event_ts).collect();
        assert_eq!(got, expected, "tiered read parity with SSD ∪ NAS query");
        assert_eq!(
            out.served_from_ssd + out.served_from_cache + out.served_from_nas,
            out.len(),
            "every served record has exactly one provenance"
        );
    }

    // ---- cap: cache never exceeds the configurable share ---------------------------------------

    #[test]
    fn cold_cache_never_exceeds_cap_and_evicts_oldest() {
        let t = TempTree::new("cap");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        // capacity 10, 20% share => cap 2. Five cold records only on NAS.
        let colds: Vec<MarketDataRecord> = (0..5)
            .map(|i| daily("AAPL", cold_ts(50 - i), 10 + i))
            .collect();
        seed_store(&ssd, &[]);
        seed_store(&nas, &colds);

        let r = reader(&ssd, &nas, 10, 20);
        assert_eq!(r.cold_read_config().cold_cache_capacity(), 2);

        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(60), NOW);
        let out = r.query(&q, NOW).unwrap();

        // All five served transparently from NAS…
        assert_eq!(out.served_from_nas, 5);
        assert_eq!(out.len(), 5);
        // …but the cache is capped at 2 and never exceeds it.
        assert!(out.cold_cache_within_cap());
        assert_eq!(out.cold_cache_entries, 2);
        assert!(out.cache_evicted >= 3);

        let report = r.cold_cache_report().unwrap();
        assert_eq!(report.entries, 2);
        assert_eq!(report.capacity, 2);
        assert!(report.within_cap());

        // The survivors are the most-recent (highest event_ts). cold_ts decreases with its argument,
        // so the newest two records are cold_ts(46) and cold_ts(47) (i=4, i=3 => 50-i).
        let cache_dir = r.cold_cache_dir();
        let cache = MarketDataStore::load_from_path(&cache_dir).unwrap();
        let mut ts: Vec<i64> = cache.records().iter().map(|r| r.key().event_ts).collect();
        ts.sort_unstable();
        assert_eq!(ts, vec![cold_ts(47), cold_ts(46)]);
    }

    #[test]
    fn zero_share_caches_nothing_but_still_serves() {
        let t = TempTree::new("zero");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let cold = daily("AAPL", cold_ts(10), 90);
        seed_store(&ssd, &[]);
        seed_store(&nas, &[cold]);

        let r = reader(&ssd, &nas, 100, 0);
        assert_eq!(r.cold_read_config().cold_cache_capacity(), 0);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();
        assert_eq!(out.served_from_nas, 1, "still served transparently");
        assert_eq!(out.newly_cached, 0, "0% share caches nothing");
        assert_eq!(out.cold_cache_entries, 0);
        assert!(out.cold_cache_within_cap());
    }

    // ---- eviction never touches hot data -------------------------------------------------------

    #[test]
    fn evict_cold_cache_never_touches_ssd_primary() {
        let t = TempTree::new("evict");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let hot = daily("AAPL", NOW - DAY, 111);
        seed_store(&ssd, std::slice::from_ref(&hot));
        let colds: Vec<MarketDataRecord> = (0..4)
            .map(|i| daily("AAPL", cold_ts(30 - i), 10 + i))
            .collect();
        seed_store(&nas, &colds);

        // Big cap so all 4 cold records cache.
        let r = reader(&ssd, &nas, 1000, 100);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(40), NOW);
        let out = r.query(&q, NOW).unwrap();
        assert_eq!(out.cold_cache_entries, 4);

        // Snapshot the SSD primary bytes before eviction.
        let ssd_before = MarketDataStore::load_from_path(&ssd).unwrap();

        let evicted = r.evict_cold_cache_to(1).unwrap();
        assert_eq!(evicted, 3, "drained cache down to 1 entry");
        assert_eq!(r.cold_cache_report().unwrap().entries, 1);

        // The SSD primary (hot) store is byte-for-byte unchanged — hot data is never evicted.
        let ssd_after = MarketDataStore::load_from_path(&ssd).unwrap();
        assert_eq!(
            ssd_before, ssd_after,
            "hot data untouched by cache eviction"
        );
        // The cold query spanned up to NOW, so the hot record (in range, on SSD) was served from SSD
        // and NOT cached — only the 4 NAS-only cold records populate the cache.
        assert_eq!(out.served_from_ssd, 1);
        // The hot record is still fully served after eviction.
        let hotq = UnifiedHistoricalQuery::new("AAPL", "1d", NOW - 2 * DAY, NOW);
        assert_eq!(r.query(&hotq, NOW).unwrap().len(), 1);
    }

    // ---- degraded: NAS unreachable during a cold read ------------------------------------------

    #[test]
    fn cold_read_degrades_when_nas_unreachable() {
        let t = TempTree::new("degraded");
        let ssd = t.dir("ssd");
        let nas = t.root.join("nas-absent"); // never created → unreachable
        let resident = daily("AAPL", cold_ts(10), 90); // still on SSD (not yet archived)
        seed_store(&ssd, std::slice::from_ref(&resident));

        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();

        assert!(out.nas_consulted, "the query reaches cold territory");
        assert!(!out.nas_reachable, "NAS is unreachable → degraded");
        assert_eq!(out.served_from_ssd, 1, "resident data still served");
        assert_eq!(out.served_from_nas, 0);
        assert_eq!(out.newly_cached, 0, "nothing cached in degraded mode");
    }

    #[test]
    fn empty_result_is_a_value_not_an_error() {
        let t = TempTree::new("empty");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        seed_store(&ssd, &[]);
        seed_store(&nas, &[]);
        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("NOSUCH", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();
        assert!(out.is_empty());
        assert_eq!(out.len(), 0);
    }

    #[test]
    fn divergent_cache_record_fails_closed() {
        // A stale/corrupt cold-read cache entry that still decodes but disagrees with the
        // authoritative NAS record must NOT silently shadow it — the read fails closed so historical
        // data corruption is surfaced, not hidden.
        let t = TempTree::new("divergent");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let authoritative = daily("AAPL", cold_ts(10), 90);
        seed_store(&ssd, &[]);
        seed_store(&nas, std::slice::from_ref(&authoritative));

        let r = reader(&ssd, &nas, 100, 20);
        // Plant a DIVERGENT cache record: same natural key (AAPL 1d @ cold_ts(10)) but a different
        // close value than NAS.
        let cache_dir = r.cold_cache_dir();
        fs::create_dir_all(&cache_dir).unwrap();
        let divergent = daily("AAPL", cold_ts(10), 999);
        seed_store(&cache_dir, std::slice::from_ref(&divergent));

        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let err = r.query(&q, NOW).unwrap_err();
        assert!(
            matches!(err, ColdReadError::CrossTierDivergence { .. }),
            "expected a fail-closed divergence, got {err:?}"
        );
    }

    #[test]
    fn matching_cache_and_nas_is_a_clean_dedup() {
        // A cache record that MATCHES the NAS record (the normal cache-hit case) is a clean dedup, not
        // a divergence — served from cache, NAS not re-added.
        let t = TempTree::new("match");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let rec = daily("AAPL", cold_ts(10), 90);
        seed_store(&ssd, &[]);
        seed_store(&nas, std::slice::from_ref(&rec));
        let r = reader(&ssd, &nas, 100, 20);
        let cache_dir = r.cold_cache_dir();
        fs::create_dir_all(&cache_dir).unwrap();
        seed_store(&cache_dir, std::slice::from_ref(&rec)); // identical to NAS

        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();
        assert_eq!(out.len(), 1);
        assert_eq!(out.served_from_cache, 1);
        assert_eq!(
            out.served_from_nas, 0,
            "identical NAS record is a clean dedup"
        );
    }

    #[test]
    fn mixed_range_hot_record_only_on_nas_fails_closed() {
        // A query spanning cold + hot time reaches cold territory (so NAS is consulted), but a HOT
        // record present on NAS and MISSING from SSD is an SRS-DATA-008 retention breach — it must NOT
        // be served (or cached) from NAS; the read fails closed and surfaces the breach.
        let t = TempTree::new("retention");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let cold = daily("AAPL", cold_ts(10), 90);
        let hot_only_on_nas = daily("AAPL", NOW - DAY, 111); // inside the hot window
        seed_store(&ssd, &[]); // SSD is MISSING the hot record (the breach)
        seed_store(&nas, &[cold, hot_only_on_nas]);

        let r = reader(&ssd, &nas, 100, 20);
        // Range spans cold_ts(20) .. NOW (cold + hot).
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let err = r.query(&q, NOW).unwrap_err();
        assert!(
            matches!(err, ColdReadError::HotRetentionBreach { .. }),
            "a hot record only on NAS must fail closed, got {err:?}"
        );
    }

    #[test]
    fn mixed_range_hot_on_ssd_cold_on_nas_serves_both() {
        // The healthy mixed-range case: the hot record IS on SSD (retention intact) and a cold record
        // is only on NAS — both are served, the hot one from SSD, the cold one from NAS.
        let t = TempTree::new("mixed-ok");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let hot = daily("AAPL", NOW - DAY, 111);
        let cold = daily("AAPL", cold_ts(10), 90);
        seed_store(&ssd, std::slice::from_ref(&hot)); // hot data resident on SSD (no breach)
        seed_store(&nas, &[hot.clone(), cold.clone()]); // NAS is the superset

        let r = reader(&ssd, &nas, 100, 20);
        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        let out = r.query(&q, NOW).unwrap();
        assert_eq!(out.len(), 2);
        assert_eq!(out.served_from_ssd, 1, "hot record from SSD");
        assert_eq!(out.served_from_nas, 1, "cold record from NAS");
        // Only the cold record is cached — the hot record (already on SSD) is never cached.
        assert_eq!(out.newly_cached, 1);
        let cache = MarketDataStore::load_from_path(&r.cold_cache_dir()).unwrap();
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.records()[0].key().event_ts, cold_ts(10));
    }

    #[test]
    fn cache_survives_reload_by_a_fresh_reader() {
        let t = TempTree::new("reload");
        let (ssd, nas) = (t.dir("ssd"), t.dir("nas"));
        let cold = daily("AAPL", cold_ts(10), 90);
        seed_store(&ssd, &[]);
        seed_store(&nas, &[cold]);

        let q = UnifiedHistoricalQuery::new("AAPL", "1d", cold_ts(20), NOW);
        reader(&ssd, &nas, 100, 20).query(&q, NOW).unwrap();

        // A brand-new reader over the same dirs sees the durable cache (survives process restart).
        let fresh = reader(&ssd, &nas, 100, 20);
        assert_eq!(fresh.cold_cache_report().unwrap().entries, 1);
    }
}
