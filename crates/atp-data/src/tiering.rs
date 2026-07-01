//! **SRS-DATA-008** — SSD-primary / NAS-archival **tiered storage** (SyRS SYS-24 / SYS-67,
//! AC-5, NFR-SC2; StRS C-5 / SN-1.26 / SN-1.27).
//!
//! # What SRS-DATA-008 asks for
//!
//! The acceptance criterion: *"All ingestion writes to SSD first; new data is synced to NAS; SSD
//! retains at least 90 days of configured hot data; NAS is used for indefinite retention; storage
//! growth estimates are documented in Section 12.1."*
//!
//! This module is the **tier coordinator** that wraps the directory the [`MarketDataStore`] persists
//! to (the substrate `store` explicitly defers here): it owns TWO store directories — a fast
//! **SSD-primary** tier and an **NAS-archival** tier — and the write/sync/retention discipline that
//! keeps them consistent.
//!
//! # The four invariants (each independently inspectable)
//!
//!   1. **SSD-first write** — [`TieredStore::ingest`] durably persists the batch to the SSD store
//!      (a unique-scratch + fsync + atomic-rename publish via [`MarketDataStore::save_to_path`])
//!      **before** any NAS write is attempted. So a crash, or an unreachable NAS, between the two
//!      can never leave a datum on NAS but not on SSD — the primary tier is the source of truth.
//!   2. **NAS sync (eventual superset)** — after the SSD write, the tier pushes every SSD record
//!      missing from NAS into the NAS store (idempotent [`MarketDataStore::upsert`]), so NAS
//!      **converges to a superset** of SSD. A prior degraded ingest's records are caught up by the
//!      next successful sync (the push is over the whole SSD snapshot, not just the new batch), so
//!      the superset is restored without operator intervention.
//!   3. **SSD ≥ 90-day hot retention** — the hot-retention window is **configurable** but
//!      **floor-enforced at 90 days** ([`MIN_HOT_RETENTION_DAYS`]): a configuration below the floor
//!      is rejected fail-closed at [`TierConfig::new`], so "at least 90 days" is a guaranteed
//!      invariant, not a hope. The tier never archives a record inside the hot window off SSD, and
//!      [`TieredStore::retention_report`] independently verifies that every hot datum known to
//!      either tier is present on SSD.
//!   4. **NAS indefinite retention** — the tier exposes **no NAS delete**. Archival
//!      ([`TieredStore::archive_cold`]) only ever removes a record from **SSD**, and only a *cold*
//!      record that is **confirmed byte-identical on NAS** — so archival frees the SSD tier without
//!      ever losing a datum (NAS keeps it forever) and without ever breaching the hot window.
//!
//! # NAS access taxonomy — degraded vs failed (SRS-MD-006 hook)
//!
//! A single classifier ([`TieredStore::nas_access`]) decides, for every NAS path, whether the
//! archival tier is **reachable**, **unreachable**, or an **alias** of SSD — so the three are never
//! confused. On the *ingest* path the SSD write always commits first, then:
//!   * **unreachable** (the archival directory is absent/unmounted) → [`NasSyncStatus::Degraded`], a
//!     *recoverable* outage caught up by a later sync — the SRS-MD-006 "NAS reachability or
//!     degraded-mode alert" input (the alert *surface* is that feature's + SRS-NOTIF-001's owner);
//!   * **reachable but broken** (a corrupt/checksum-mismatched store, a conflicting record, lock
//!     contention, or NAS aliasing SSD) → [`NasSyncStatus::Failed`], an *integrity* failure that is
//!     surfaced distinctly so it is never mistaken for an offline mount nor reported as `Synced`.
//!
//! A **reachable-but-broken** NAS (an alias, a corrupt/checksum-mismatched store, a conflicting
//! record, or lock contention) ALWAYS fails closed on every NAS-centric operation — ingest reports
//! `Failed`, and [`sync_ssd_to_nas`](TieredStore::sync_ssd_to_nas),
//! [`retention_report`](TieredStore::retention_report), and
//! [`archive_cold`](TieredStore::archive_cold) all return [`TierError::Nas`] — so an integrity
//! failure (an alias in particular) can never let archival delete the only copy. An **unreachable**
//! (offline-mount) NAS is the *recoverable* case, and each operation handles it according to its
//! purpose: `ingest` commits SSD and degrades the archival half (`Degraded`); the explicit
//! `sync_ssd_to_nas` — whose purpose IS to confirm archival — errors; `retention_report` reports it
//! (`nas_reachable = false`); and `archive_cold` — whose purpose is to FREE SSD only when a record is
//! safely on NAS — archives nothing (`archived = 0`, `nas_reachable = false`, everything retained on
//! SSD), a benign no-op rather than a failure.
//!
//! # Honest scope — what is real here vs deferred
//!
//! Real and runnable: the tier coordinator + the four invariants above, demonstrated end-to-end over
//! two real on-disk store directories driven by fixture data (`store::fixture_batch`) — exactly as
//! the verification step permits ("fixture market data, provider mocks, file reads, and persisted
//! output inspection"). The `data008_tier_cli` binary exercises it and the SSD/NAS store files are
//! directly inspectable.
//!
//! **ALL ingestion is SSD-first, then NAS-synced (the AC's cross-cutting clause).** The single
//! validated, tiered market-data write surface is
//! [`DataLayer::ingest_market_records_tiered`](crate::DataLayer::ingest_market_records_tiered): it
//! composes the unchanged ERR-5 validation gate with [`TieredStore::ingest`] (SSD-first durable
//! write, then NAS sync). Every market-data ingestion binary syncs its SSD write to NAS via the tier:
//! `data008_tier_cli` uses the encapsulated surface, while `data016_ingest_cli` and
//! `data005_fundamental_cli` keep their SRS-DATA-016 idempotency + SRS-DATA-017 writer-serialization
//! contract (a `StoreLock`-held SSD load-modify-save) and then call
//! [`sync_ssd_to_nas_best_effort`](TieredStore::sync_ssd_to_nas_best_effort) so the committed SSD
//! snapshot is archived to NAS. The structural guard
//! `tools/data008_tiering_check.py::check_ingestion_routing` sweeps every binary to prove none
//! persists via [`MarketDataStore::save_to_path`] WITHOUT a paired NAS sync — the only exception is
//! `data011_coverage_cli` (corporate-action COVERAGE is an operator trust assertion the tiered surface
//! deliberately refuses, not provider market data), so a new ingest path (or a real provider adapter,
//! when built) cannot regress the "all ingestion" clause without tripping the guard.
//!
//! # Honest scope — what is deferred (none load-bearing for the tiering property)
//!
//! The **real Databento/IB/Sharadar/option-chain network adapters** that FEED the tier are
//! SRS-DATA-001/003/006 (the tier is provider-agnostic — it stores whatever `MarketDataRecord`s it is
//! handed; fixture sources stand in, and the routing guard forces the adapters through the tier when
//! built); a **durable expected-keys manifest/catalog** so [`retention_report`](TieredStore::retention_report)
//! can detect loss of *already-archived* (SSD-evicted) data is SRS-DATA-018 (backup + validated
//! recovery / catalog — the honest scope bound on [`nas_superset_verdict`](RetentionReport::nas_superset_verdict));
//! the **cold-read failover** that serves an archived record back from NAS transparently is
//! SRS-DATA-009; the **eviction *policy*** (the 80 % high-water-mark trigger, the inactivity recency
//! window, never-evict live-strategy data) is SRS-DATA-010 — this module ships only the
//! data-loss-*safe* archival *primitive* the policy will drive; the real 1 TB SSD / 20 TB NAS
//! **capacity** is the NFR-SC2 deployment concern whose growth estimates are documented in SRS §12.1
//! (inspection).
//!
//! # Determinism + money discipline
//!
//! The hot/cold boundary is a pure function of `(event_ts, now_ts, hot_retention_days)` — the caller
//! supplies `now_ts` (production from the system clock, tests/CLI from `--now`), so the module reads
//! no wall-clock and the classification is deterministic and testable. No floating-point anywhere;
//! timestamps are `i64` epoch seconds (the `NaturalKey::event_ts` the store already keys on). No
//! `serde` / external dependency — the tier composes only `std::fs` and the zero-dependency `store`
//! codec.

use std::error::Error;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use crate::store::{MarketDataRecord, MarketDataStore, StoreError, StoreLock};

/// Seconds in a calendar day — the unit the hot-retention window is measured in.
pub const SECONDS_PER_DAY: i64 = 86_400;

/// The SRS-DATA-008 floor: SSD must retain **at least** 90 days of hot data. A [`TierConfig`]
/// configured below this is rejected fail-closed, so the "at least 90 days" acceptance clause is a
/// guaranteed invariant rather than an operator's good intention.
pub const MIN_HOT_RETENTION_DAYS: u32 = 90;

/// The default hot-retention window when an operator does not configure one — the SRS-DATA-008
/// minimum.
pub const DEFAULT_HOT_RETENTION_DAYS: u32 = 90;

/// A fail-closed error from a tier operation. Wraps the per-tier [`StoreError`] tagged with **which**
/// tier failed, so a primary (SSD) failure — which must abort the ingest — is never confused with an
/// archival (NAS) failure — which degrades to [`NasSyncStatus::Degraded`] instead.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TierError {
    /// The configured hot-retention window is below the SRS-DATA-008 [`MIN_HOT_RETENTION_DAYS`]
    /// floor. Rejected at construction so SSD can never be configured to drop hot data early.
    HotRetentionBelowFloor {
        /// The rejected operator-configured value.
        configured: u32,
        /// The enforced floor ([`MIN_HOT_RETENTION_DAYS`]).
        floor: u32,
    },
    /// The SSD and NAS tiers were configured to the **same** directory — a tier must be two distinct
    /// directories or the "archival" copy would just be the primary, defeating the separation.
    TiersNotDistinct {
        /// The directory both tiers pointed at.
        dir: PathBuf,
    },
    /// A **primary-tier (SSD)** store operation failed. The SSD tier is the source of truth, so this
    /// aborts the operation fail-closed rather than proceeding with a half-written primary.
    Ssd(StoreError),
    /// An **archival-tier (NAS)** store operation failed where the caller demanded it be fatal — an
    /// explicit [`TieredStore::sync_ssd_to_nas`], [`retention_report`](TieredStore::retention_report),
    /// or [`archive_cold`](TieredStore::archive_cold) hitting an unreachable / aliased / corrupt NAS.
    /// On the *ingest* path a NAS problem is NOT fatal — it is reported in the returned
    /// [`NasSyncStatus`] (`Degraded` if unreachable, `Failed` if reachable-but-broken) without losing
    /// the committed SSD write.
    Nas(StoreError),
}

impl fmt::Display for TierError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::HotRetentionBelowFloor { configured, floor } => write!(
                f,
                "hot-retention window {configured} days is below the SRS-DATA-008 floor of {floor} days"
            ),
            Self::TiersNotDistinct { dir } => write!(
                f,
                "SSD and NAS tiers must be distinct directories (both were {})",
                dir.display()
            ),
            Self::Ssd(err) => write!(f, "SSD (primary) tier error: {err}"),
            Self::Nas(err) => write!(f, "NAS (archival) tier error: {err}"),
        }
    }
}

impl Error for TierError {}

/// The result of the NAS-sync half of an [`TieredStore::ingest`]. The SSD write has already
/// committed by the time this is produced, so **none** of these values means a lost ingest — they
/// distinguish how the *archival* side fared. Critically, a recoverable outage
/// ([`Degraded`](Self::Degraded)) is kept distinct from a reachable-but-broken archive
/// ([`Failed`](Self::Failed)): the former is caught up by a later sync, the latter needs operator
/// attention and must NOT be mistaken for an offline NAS.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NasSyncStatus {
    /// NAS was reachable, distinct from SSD, and now holds a superset of SSD. `records_added` is how
    /// many SSD records were newly written to NAS by this sync (a steady-state re-sync adds 0).
    Synced {
        /// SSD records newly inserted into the NAS store by this sync.
        records_added: usize,
    },
    /// NAS was **unreachable** (the archival directory is absent/unmounted) — a *recoverable* outage.
    /// The SSD write stands; the records are caught up by the next successful sync. This is the
    /// SRS-MD-006 "NAS reachability or degraded-mode alert" input.
    Degraded {
        /// The store error that made NAS unreachable (e.g. `Io { context: "..." }`).
        reason: StoreError,
    },
    /// NAS was **reachable but the archival write failed** — a corrupt/checksum-mismatched NAS store,
    /// a conflicting record (the archive disagrees with the primary), lock contention, an I/O error
    /// mid-write, or the NAS directory ALIASING the SSD directory (not an independent archive). The
    /// SSD write stands, but this is NOT a recoverable outage: it is surfaced distinctly (never as
    /// `Synced`, never folded into `Degraded`) so an integrity failure is not mistaken for an offline
    /// mount.
    Failed {
        /// The store error (or alias condition) that failed the archival write.
        reason: StoreError,
    },
}

impl NasSyncStatus {
    /// Whether NAS was reachable, distinct, and synced (the only success path).
    pub fn is_synced(&self) -> bool {
        matches!(self, Self::Synced { .. })
    }

    /// Whether the ingest ran in degraded (NAS-unreachable) mode — the SRS-MD-006 alert input.
    pub fn is_degraded(&self) -> bool {
        matches!(self, Self::Degraded { .. })
    }

    /// Whether the NAS archive was reachable but the write failed (corruption/conflict/lock/alias) —
    /// an integrity failure that is NOT a recoverable outage.
    pub fn is_failed(&self) -> bool {
        matches!(self, Self::Failed { .. })
    }
}

/// The outcome of an [`TieredStore::ingest`]: the SSD write counts plus the NAS-sync status.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TierIngestOutcome {
    /// Records newly inserted into the SSD store (absent keys).
    pub ssd_inserted: usize,
    /// Records already present on SSD with identical content (idempotent no-ops).
    pub ssd_unchanged: usize,
    /// Whether the NAS archival sync succeeded or degraded.
    pub nas_sync: NasSyncStatus,
}

/// The outcome of an [`TieredStore::archive_cold`]: how many cold records were safely archived off
/// SSD (confirmed on NAS) vs retained on SSD because they were not yet safely on NAS.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchiveOutcome {
    /// Cold SSD records dropped from SSD because they are confirmed byte-identical on NAS.
    pub archived: usize,
    /// Cold SSD records **kept** on SSD because they are absent/different on NAS — never dropped
    /// without a confirmed archival copy (the no-data-loss guard).
    pub retained_unconfirmed: usize,
    /// Whether NAS was reachable; when false, nothing is archived (no copy can be confirmed).
    pub nas_reachable: bool,
}

/// A point-in-time, cross-tier verification of the SRS-DATA-008 retention invariants — the objective
/// evidence the verification step records. Computed by loading BOTH tiers and comparing them, so it
/// is an independent check, not a restatement of what the writer intended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RetentionReport {
    /// The `now_ts` the hot window was evaluated against.
    pub now_ts: i64,
    /// The configured hot-retention window in days (≥ [`MIN_HOT_RETENTION_DAYS`]).
    pub hot_retention_days: u32,
    /// The inclusive lower bound of the hot window: `now_ts - hot_retention_days * SECONDS_PER_DAY`.
    pub hot_window_start: i64,
    /// Total records on the SSD primary tier.
    pub ssd_total: usize,
    /// Total records on the NAS archival tier (0 and `nas_reachable == false` if NAS is unreachable).
    pub nas_total: usize,
    /// SSD records inside the hot window.
    pub ssd_hot: usize,
    /// SSD records older than the hot window (cold data still resident on SSD, archivable).
    pub ssd_cold: usize,
    /// Hot records known to NAS but **missing from SSD** — a retention breach if non-zero.
    pub hot_missing_from_ssd: usize,
    /// SSD records absent/different on NAS — the un-synced backlog (a degraded-mode residue).
    pub ssd_missing_from_nas: usize,
    /// Whether NAS was reachable when the report was taken.
    pub nas_reachable: bool,
}

/// A cross-tier verification verdict. **Tri-state on purpose**: when NAS is unreachable the
/// cross-tier comparison cannot run, so the verdict is [`Unverified`](Self::Unverified) — NEVER a
/// false-positive [`Satisfied`](Self::Satisfied). A caller (or the CLI) must treat `Unverified` as
/// "not proven", distinct from a proven [`Violated`](Self::Violated).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RetentionVerdict {
    /// The invariant was cross-checked against both tiers and holds.
    Satisfied,
    /// The invariant was cross-checked and is BROKEN.
    Violated,
    /// NAS was unreachable, so the cross-tier check could not run — the invariant is neither proven
    /// nor disproven (treat as not-satisfied for any gating decision).
    Unverified,
}

impl RetentionVerdict {
    /// Whether the invariant is *proven* satisfied (NOT true for an unverified/degraded report).
    pub fn is_satisfied(&self) -> bool {
        matches!(self, Self::Satisfied)
    }

    /// The lowercase wire string (`satisfied` / `violated` / `unverified`).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Satisfied => "satisfied",
            Self::Violated => "violated",
            Self::Unverified => "unverified",
        }
    }
}

impl RetentionReport {
    /// The SRS-DATA-008 "SSD retains at least 90 days of hot data" invariant: every hot datum is on
    /// SSD. The proof is a CROSS-TIER check (a hot record on NAS but missing from SSD is a breach),
    /// so an unreachable NAS yields [`RetentionVerdict::Unverified`] — the report cannot claim the
    /// SSD hot tier is complete when it never loaded NAS to look for hot records SSD is missing.
    pub fn ssd_hot_retention_verdict(&self) -> RetentionVerdict {
        if !self.nas_reachable {
            RetentionVerdict::Unverified
        } else if self.hot_missing_from_ssd > 0 {
            RetentionVerdict::Violated
        } else {
            RetentionVerdict::Satisfied
        }
    }

    /// Verdict that **every record currently RESIDENT on SSD is archived on NAS** (NAS ⊇ resident
    /// SSD) — a NECESSARY condition for "NAS is used for indefinite retention", checked by comparing
    /// the two live stores. An unreachable NAS yields [`RetentionVerdict::Unverified`], never a false
    /// `Satisfied`.
    ///
    /// **Scope (honest bound):** this does NOT detect loss of *already-archived* data — a cold record
    /// that [`archive_cold`](TieredStore::archive_cold) evicted from SSD is no longer in the resident
    /// SSD set, so if NAS later loses it the cross-store comparison cannot notice (there is no record
    /// of what *should* be on NAS). A full archival-integrity proof needs a **durable expected-keys
    /// manifest/catalog** of everything ever ingested/archived — deferred (the SRS-DATA-018 backup +
    /// validated-recovery / catalog owner). Until that lands, treat a `Satisfied` superset verdict as
    /// "no resident SSD datum is un-archived", not "no archived datum was ever lost".
    pub fn nas_superset_verdict(&self) -> RetentionVerdict {
        if !self.nas_reachable {
            RetentionVerdict::Unverified
        } else if self.ssd_missing_from_nas > 0 {
            RetentionVerdict::Violated
        } else {
            RetentionVerdict::Satisfied
        }
    }
}

/// Whether two tier paths denote the **same directory** — used to reject a config (and, defensively,
/// an archival run) where the SSD primary and the NAS archive are aliases of one location. That
/// aliasing is dangerous: [`TieredStore::archive_cold`] drops a cold record from SSD once it is
/// "confirmed on NAS", so if NAS *is* SSD the confirmation is trivially true and archival would
/// delete the ONLY copy — breaching the NAS indefinite-retention invariant.
///
/// Catches both a **lexical** alias (`ssd` vs `ssd/.`, a trailing slash, a redundant separator — via
/// [`Path::components`], which normalizes away `.` and repeated separators) and, when both paths
/// already exist on disk, a **symlink / hardlink** alias (via [`Path::canonicalize`], which resolves
/// links to the real path). A not-yet-provisioned path cannot be canonicalized, so an alias created
/// after construction is re-checked at archival time (where both tiers necessarily exist).
fn same_directory(a: &Path, b: &Path) -> bool {
    if a.components().eq(b.components()) {
        return true;
    }
    matches!((a.canonicalize(), b.canonicalize()), (Ok(ra), Ok(rb)) if ra == rb)
}

/// Validated configuration of a two-tier store: an SSD primary directory, an NAS archival directory,
/// and the floor-enforced hot-retention window.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TierConfig {
    ssd_dir: PathBuf,
    nas_dir: PathBuf,
    hot_retention_days: u32,
}

impl TierConfig {
    /// Build a validated tier config, failing closed if `hot_retention_days` is below the
    /// SRS-DATA-008 floor ([`MIN_HOT_RETENTION_DAYS`]) or the two tiers are the same directory.
    pub fn new(
        ssd_dir: impl Into<PathBuf>,
        nas_dir: impl Into<PathBuf>,
        hot_retention_days: u32,
    ) -> Result<Self, TierError> {
        if hot_retention_days < MIN_HOT_RETENTION_DAYS {
            return Err(TierError::HotRetentionBelowFloor {
                configured: hot_retention_days,
                floor: MIN_HOT_RETENTION_DAYS,
            });
        }
        let ssd_dir = ssd_dir.into();
        let nas_dir = nas_dir.into();
        // Reject not just an exactly-equal path but any ALIAS of one directory (a `.`/trailing-slash
        // lexical alias, or an already-existing symlink): two tiers that share a store file would let
        // archive_cold delete the only copy. A post-construction alias is re-caught in archive_cold.
        if same_directory(&ssd_dir, &nas_dir) {
            return Err(TierError::TiersNotDistinct { dir: ssd_dir });
        }
        Ok(Self {
            ssd_dir,
            nas_dir,
            hot_retention_days,
        })
    }

    /// Build a config at the default ([`DEFAULT_HOT_RETENTION_DAYS`]) hot-retention window.
    pub fn with_default_retention(
        ssd_dir: impl Into<PathBuf>,
        nas_dir: impl Into<PathBuf>,
    ) -> Result<Self, TierError> {
        Self::new(ssd_dir, nas_dir, DEFAULT_HOT_RETENTION_DAYS)
    }

    /// The SSD primary directory.
    pub fn ssd_dir(&self) -> &Path {
        &self.ssd_dir
    }

    /// The NAS archival directory.
    pub fn nas_dir(&self) -> &Path {
        &self.nas_dir
    }

    /// The configured hot-retention window in days.
    pub fn hot_retention_days(&self) -> u32 {
        self.hot_retention_days
    }

    /// The inclusive lower bound of the hot window relative to `now_ts`. A record with
    /// `event_ts >= hot_window_start(now_ts)` is **hot** (must live on SSD); older is **cold**
    /// (archivable to NAS).
    pub fn hot_window_start(&self, now_ts: i64) -> i64 {
        now_ts.saturating_sub((self.hot_retention_days as i64).saturating_mul(SECONDS_PER_DAY))
    }

    /// Whether a record with `event_ts` is hot at `now_ts`.
    pub fn is_hot(&self, event_ts: i64, now_ts: i64) -> bool {
        event_ts >= self.hot_window_start(now_ts)
    }
}

/// The SSD-primary / NAS-archival tiered store (SRS-DATA-008). Stateless beyond its [`TierConfig`];
/// every operation is a fail-closed load-modify-save against the on-disk tiers, so two `TieredStore`
/// handles to the same directories are interchangeable and the durable files are the only state.
#[derive(Debug, Clone)]
pub struct TieredStore {
    config: TierConfig,
}

impl TieredStore {
    /// Wrap a validated [`TierConfig`].
    pub fn new(config: TierConfig) -> Self {
        Self { config }
    }

    /// The tier configuration.
    pub fn config(&self) -> &TierConfig {
        &self.config
    }

    /// **Ingest a batch SSD-first, then sync to NAS** (the SRS-DATA-008 write path).
    ///
    /// Order is load-bearing: the SSD store is loaded under its single-writer [`StoreLock`],
    /// upserted, and **durably persisted** before any NAS write. Only then is the full SSD snapshot
    /// pushed to NAS. So the primary tier is committed first and a NAS failure can never leave a
    /// datum on NAS but absent from SSD.
    ///
    /// An SSD failure aborts fail-closed ([`TierError::Ssd`]). A NAS failure does **not** abort — it
    /// surfaces as [`NasSyncStatus::Degraded`] (the SSD write stands), to be reconciled by a later
    /// sync. The SSD lock is released after the SSD save; the NAS push reads a consistent SSD
    /// snapshot (the atomic-publish file), so it needs no SSD lock and cannot deadlock against it.
    pub fn ingest(
        &self,
        records: impl IntoIterator<Item = MarketDataRecord>,
    ) -> Result<TierIngestOutcome, TierError> {
        let batch: Vec<MarketDataRecord> = records.into_iter().collect();

        // --- SSD-FIRST: durable primary write under the single-writer lock. ------------------- //
        fs::create_dir_all(&self.config.ssd_dir).map_err(|_| {
            TierError::Ssd(StoreError::Io {
                context: "create SSD tier directory",
            })
        })?;
        let ssd_snapshot = {
            let _lock = StoreLock::acquire(&self.config.ssd_dir).map_err(TierError::Ssd)?;
            let mut ssd =
                MarketDataStore::load_from_path(&self.config.ssd_dir).map_err(TierError::Ssd)?;
            let mut inserted = 0;
            let mut unchanged = 0;
            for record in batch {
                match ssd.upsert(record).map_err(TierError::Ssd)? {
                    crate::store::UpsertOutcome::Inserted => inserted += 1,
                    crate::store::UpsertOutcome::UnchangedDuplicate => unchanged += 1,
                }
            }
            // SSD is durable on disk HERE (unique scratch + fsync + atomic rename + dir fsync),
            // before a single byte is written to the archival tier.
            ssd.save_to_path(&self.config.ssd_dir)
                .map_err(TierError::Ssd)?;
            (ssd, inserted, unchanged)
            // _lock released on scope exit — NAS push below holds no SSD lock.
        };
        let (ssd, ssd_inserted, ssd_unchanged) = ssd_snapshot;

        // --- NAS sync: classify the archival tier, then push only into a Ready (reachable +
        // distinct) NAS. Each classification maps to a DISTINCT outcome — a recoverable outage
        // (Unreachable) degrades, but an alias or a reachable-but-failed write surfaces as Failed,
        // never as Synced and never folded into Degraded.
        let nas_sync = match self.nas_access() {
            NasAccess::Unreachable => NasSyncStatus::Degraded {
                reason: nas_unreachable_error(),
            },
            NasAccess::Aliased => NasSyncStatus::Failed {
                reason: nas_alias_error(),
            },
            NasAccess::Ready => match self.push_to_ready_nas(ssd.records()) {
                Ok(records_added) => NasSyncStatus::Synced { records_added },
                Err(reason) => NasSyncStatus::Failed { reason },
            },
        };

        Ok(TierIngestOutcome {
            ssd_inserted,
            ssd_unchanged,
            nas_sync,
        })
    }

    /// Explicitly reconcile NAS to a superset of SSD — the recovery path after a degraded ingest.
    /// Unlike the ingest's best-effort sync, **every** non-success here is **fatal**
    /// ([`TierError::Nas`]): the operator asked to confirm archival, so an unreachable NAS, an
    /// SSD/NAS alias, or a corrupt/conflicting NAS store must all be reported, not swallowed.
    /// Returns the number of SSD records newly written to NAS.
    pub fn sync_ssd_to_nas(&self) -> Result<usize, TierError> {
        match self.nas_access() {
            NasAccess::Unreachable => Err(TierError::Nas(nas_unreachable_error())),
            NasAccess::Aliased => Err(TierError::Nas(nas_alias_error())),
            NasAccess::Ready => {
                // Read SSD without the writer lock: the atomic-publish file is a consistent snapshot.
                let ssd = MarketDataStore::load_from_path(&self.config.ssd_dir)
                    .map_err(TierError::Ssd)?;
                self.push_to_ready_nas(ssd.records())
                    .map_err(TierError::Nas)
            }
        }
    }

    /// **Best-effort** sync of the SSD snapshot to NAS — the degrade-tolerant counterpart to
    /// [`sync_ssd_to_nas`](Self::sync_ssd_to_nas), returning the archival [`NasSyncStatus`] instead of
    /// erroring.
    ///
    /// This is what an operator ingest path calls **after** it has already committed its SSD write
    /// (SSD-first): it must sync the new data to NAS without failing the whole ingest on a *recoverable*
    /// NAS outage. The taxonomy matches [`ingest`](Self::ingest): an **unreachable** NAS →
    /// [`NasSyncStatus::Degraded`] (the SSD write stands; a later [`sync_ssd_to_nas`](Self::sync_ssd_to_nas)
    /// reconciles); an **alias** or a **reachable-but-broken** NAS (corrupt store, conflict, lock) →
    /// [`NasSyncStatus::Failed`] (an integrity failure, never mistaken for an offline mount); a
    /// **Ready** NAS → [`NasSyncStatus::Synced`]. The SSD store is read lock-free (the atomic-publish
    /// snapshot), so this never blocks an active reader and needs no SSD lock.
    pub fn sync_ssd_to_nas_best_effort(&self) -> NasSyncStatus {
        match self.nas_access() {
            NasAccess::Unreachable => NasSyncStatus::Degraded {
                reason: nas_unreachable_error(),
            },
            NasAccess::Aliased => NasSyncStatus::Failed {
                reason: nas_alias_error(),
            },
            NasAccess::Ready => match MarketDataStore::load_from_path(&self.config.ssd_dir) {
                Ok(ssd) => match self.push_to_ready_nas(ssd.records()) {
                    Ok(records_added) => NasSyncStatus::Synced { records_added },
                    Err(reason) => NasSyncStatus::Failed { reason },
                },
                Err(reason) => NasSyncStatus::Failed { reason },
            },
        }
    }

    /// **Archive cold data off SSD, data-loss-safely** (the SRS-DATA-008 tier boundary).
    ///
    /// For each SSD record older than the hot window: if it is **confirmed byte-identical on NAS**,
    /// drop it from SSD (NAS keeps it indefinitely); otherwise **keep** it on SSD — a cold record is
    /// never dropped without a confirmed archival copy, and a hot record is never touched.
    ///
    /// NAS handling matches this op's purpose (free SSD only when a record is safely archived):
    /// an **unreachable** NAS is a benign no-op — `Ok(`[`ArchiveOutcome`]` { archived: 0,
    /// nas_reachable: false, .. })`, everything retained on SSD (the eviction policy reads
    /// `nas_reachable` and can alert/skip) — whereas a **reachable-but-broken** NAS (an SSD alias, or
    /// a corrupt/unreadable store) **fails closed** ([`TierError::Nas`]): dropping a "confirmed on
    /// NAS" record when NAS *is* SSD (or trusting an unreadable archive) would risk the only copy.
    /// This is the safe archival *primitive*; the eviction *policy* that decides WHEN to call it (the
    /// 80 % high-water mark, inactivity recency, never-evict live-strategy data) is SRS-DATA-010.
    pub fn archive_cold(&self, now_ts: i64) -> Result<ArchiveOutcome, TierError> {
        let hot_window_start = self.config.hot_window_start(now_ts);

        let _lock = StoreLock::acquire(&self.config.ssd_dir).map_err(TierError::Ssd)?;
        let ssd = MarketDataStore::load_from_path(&self.config.ssd_dir).map_err(TierError::Ssd)?;

        // Classify NAS before touching any record. An ALIAS fails closed (dropping a "cold but
        // confirmed on NAS" record when NAS *is* SSD would delete the only copy — the SRS-DATA-008
        // NAS indefinite-retention invariant); an UNREACHABLE NAS archives nothing (no copy can be
        // confirmed). Only a Ready (reachable + distinct) NAS can confirm an archival copy.
        let nas = match self.nas_access() {
            NasAccess::Aliased => return Err(TierError::Nas(nas_alias_error())),
            NasAccess::Unreachable => {
                return Ok(ArchiveOutcome {
                    archived: 0,
                    retained_unconfirmed: ssd
                        .records()
                        .iter()
                        .filter(|r| r.key().event_ts < hot_window_start)
                        .count(),
                    nas_reachable: false,
                });
            }
            // A corrupt/unreadable NAS store is a real integrity failure — fail closed rather than
            // treat an unconfirmable archive as "nothing to archive".
            NasAccess::Ready => {
                MarketDataStore::load_from_path(&self.config.nas_dir).map_err(TierError::Nas)?
            }
        };

        let mut retained = MarketDataStore::new();
        let mut archived = 0;
        let mut retained_unconfirmed = 0;
        for record in ssd.records() {
            let is_cold = record.key().event_ts < hot_window_start;
            let confirmed_on_nas = nas.get(record.key()) == Some(record);
            if is_cold && confirmed_on_nas {
                archived += 1; // drop from SSD — NAS retains it indefinitely.
            } else {
                if is_cold {
                    retained_unconfirmed += 1; // cold but not safely on NAS → keep on SSD.
                }
                retained.upsert(record.clone()).map_err(TierError::Ssd)?;
            }
        }

        // Only republish SSD when something actually moved (avoid a no-op rewrite).
        if archived > 0 {
            retained
                .save_to_path(&self.config.ssd_dir)
                .map_err(TierError::Ssd)?;
        }

        Ok(ArchiveOutcome {
            archived,
            retained_unconfirmed,
            nas_reachable: true,
        })
    }

    /// Cross-tier verification of the SRS-DATA-008 retention invariants at `now_ts` — the inspectable
    /// evidence. Loads both tiers and compares them; never mutates either.
    pub fn retention_report(&self, now_ts: i64) -> Result<RetentionReport, TierError> {
        let hot_window_start = self.config.hot_window_start(now_ts);
        let ssd = MarketDataStore::load_from_path(&self.config.ssd_dir).map_err(TierError::Ssd)?;
        // An alias fails closed (a report over NAS==SSD would falsely claim the superset invariant);
        // an unreachable NAS reports `nas_reachable: false` (not a corrupt one); a corrupt NAS store
        // is a fatal load error, not a silent "unreachable".
        let (nas, nas_reachable) = match self.nas_access() {
            NasAccess::Aliased => return Err(TierError::Nas(nas_alias_error())),
            NasAccess::Unreachable => (MarketDataStore::new(), false),
            NasAccess::Ready => (
                MarketDataStore::load_from_path(&self.config.nas_dir).map_err(TierError::Nas)?,
                true,
            ),
        };

        let mut ssd_hot = 0;
        let mut ssd_cold = 0;
        let mut ssd_missing_from_nas = 0;
        for record in ssd.records() {
            if record.key().event_ts >= hot_window_start {
                ssd_hot += 1;
            } else {
                ssd_cold += 1;
            }
            if !nas_reachable || nas.get(record.key()) != Some(record) {
                ssd_missing_from_nas += 1;
            }
        }

        // A hot datum known to NAS but absent from SSD is a retention breach. (SSD's own hot records
        // are present by construction; the cross-check is over NAS-only hot keys.)
        let mut hot_missing_from_ssd = 0;
        if nas_reachable {
            for record in nas.records() {
                if record.key().event_ts >= hot_window_start && ssd.get(record.key()).is_none() {
                    hot_missing_from_ssd += 1;
                }
            }
        }

        Ok(RetentionReport {
            now_ts,
            hot_retention_days: self.config.hot_retention_days,
            hot_window_start,
            ssd_total: ssd.len(),
            nas_total: nas.len(),
            ssd_hot,
            ssd_cold,
            hot_missing_from_ssd,
            ssd_missing_from_nas,
            nas_reachable,
        })
    }

    /// Classify the NAS archival tier as an **independent** archive — the single place the
    /// reachability + distinctness policy lives, so every NAS path (ingest sync, explicit sync,
    /// archival, the report) treats an absent mount, an SSD alias, and a healthy archive identically.
    ///
    ///   * [`Unreachable`](NasAccess::Unreachable) — the archival directory is absent/unmounted (a
    ///     recoverable outage).
    ///   * [`Aliased`](NasAccess::Aliased) — the directory exists but resolves to the SSD tier (a
    ///     `.`/symlink alias). It is NOT an independent archive; reading or writing it would
    ///     double-count the primary and let archival delete the only copy, so callers fail closed.
    ///   * [`Ready`](NasAccess::Ready) — a provisioned directory distinct from SSD, safe to archive
    ///     into.
    ///
    /// The NAS directory is **not** auto-created: an absent archival directory means
    /// "unmounted/unreachable", which must surface rather than be silently provisioned.
    fn nas_access(&self) -> NasAccess {
        if !self.config.nas_dir.is_dir() {
            return NasAccess::Unreachable;
        }
        if same_directory(&self.config.ssd_dir, &self.config.nas_dir) {
            return NasAccess::Aliased;
        }
        NasAccess::Ready
    }

    /// Push `records` into a **Ready** (caller-classified reachable + distinct) NAS store, returning
    /// the count newly inserted. Any store error (a corrupt/checksum-mismatched store, a conflicting
    /// record, lock contention, or an I/O failure mid-write) is surfaced fail-closed — these are NOT
    /// a recoverable outage and the caller must not mask them as degraded.
    fn push_to_ready_nas(&self, records: &[MarketDataRecord]) -> Result<usize, StoreError> {
        let _lock = StoreLock::acquire(&self.config.nas_dir)?;
        let mut nas = MarketDataStore::load_from_path(&self.config.nas_dir)?;
        let mut added = 0;
        for record in records {
            match nas.upsert(record.clone())? {
                crate::store::UpsertOutcome::Inserted => added += 1,
                crate::store::UpsertOutcome::UnchangedDuplicate => {}
            }
        }
        if added > 0 {
            nas.save_to_path(&self.config.nas_dir)?;
        }
        Ok(added)
    }
}

/// How the NAS archival tier classifies relative to the SSD primary (see [`TieredStore::nas_access`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum NasAccess {
    /// A provisioned directory distinct from SSD — safe to archive into.
    Ready,
    /// The archival directory is absent/unmounted — a recoverable outage.
    Unreachable,
    /// The directory resolves to the SSD tier (a `.`/symlink alias) — not an independent archive.
    Aliased,
}

/// The fail-closed store error for an unreachable (absent/unmounted) NAS archival directory.
fn nas_unreachable_error() -> StoreError {
    StoreError::Io {
        context: "NAS archival directory missing or unreachable",
    }
}

/// The fail-closed store error for an SSD/NAS directory ALIAS (not an independent archive).
fn nas_alias_error() -> StoreError {
    StoreError::Io {
        context: "NAS archival directory aliases the SSD tier (not an independent archive)",
    }
}
