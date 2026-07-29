//! # SRS-DATA-018 scheduled backup + validated recovery for NAS-stored data
//!
//! SYS-59: *"The system shall provide a scheduled backup mechanism for all data stored on the NAS
//! (ingested equity data, option chain snapshots, fundamental data, backtest results). Backup shall
//! support at minimum: export to an external storage target (e.g., USB drive, secondary NAS, or
//! cloud archival bucket) on a user-configurable schedule (default: weekly)."*
//!
//! SYS-60: *"The system shall define a recovery point objective (RPO) of 7 days for NAS-stored
//! market data. [...] The system shall validate backup integrity on completion."*
//!
//! This module is the **backup brain** over the existing store substrate. It does not invent a
//! second archive format or a second checksum: a backup unit is a directory holding one of the
//! repo's `MAGIC`-prefixed store blobs, and "validate integrity on completion" means re-reading the
//! **exported** bytes through the very same fail-closed codec that wrote them
//! ([`store::checksum`], [`MarketDataStore::load_from_path`]) and comparing the result against the
//! source. Copying bytes and trusting the copy would prove nothing.
//!
//! ## The three fail-closed stances that make a backup trustworthy
//!
//! 1. **A cadence longer than the RPO is refused at construction.** The schedule and the recovery
//!    point objective are not independent knobs: a 14-day cadence *cannot* satisfy a 7-day RPO, so
//!    [`BackupConfig::new`] rejects it rather than letting an operator configure a policy that is
//!    guaranteed to breach SYS-60. This turns the "documented RPO is no more than 7 days" clause
//!    into an enforced invariant instead of a comment.
//! 2. **A target that is the source is not a backup.** Like the DATA-008 tier check, an alias
//!    (trailing slash, `.`, symlink) is rejected — and, unique to backup, so is a target *nested
//!    inside* the NAS root: a copy that lives on the failing device dies with it, which is the
//!    exact failure SYS-60 exists to survive.
//! 3. **Absence of evidence is never evidence of success.** [`BackupVerdict`] is tri-state and an
//!    unreachable target yields [`Unverified`](BackupVerdict::Unverified) — *never*
//!    [`Verified`](BackupVerdict::Verified). Finding zero units is likewise `Unverified`, not a
//!    vacuous pass: "nothing to back up" and "backed everything up" must not render alike.
//!
//! ## Degraded vs Failed
//!
//! Mirroring the DATA-008 `NasSyncStatus` split, [`TargetStatus`] keeps a *recoverable outage*
//! ([`Degraded`](TargetStatus::Degraded) — the external drive is unplugged, retry next window)
//! distinct from a *reachable-but-broken* target ([`Failed`](TargetStatus::Failed) — the media is
//! there and the copy is wrong, which needs an operator now). Collapsing them would let a silently
//! rotting archive masquerade as an unplugged USB stick.
//!
//! ## Scope boundary (SYS-59's workload-priority clause)
//!
//! SYS-59 also requires the backup job to run *"at the lowest priority in the workload hierarchy
//! (SYS-57)"*. That clause is **not** implemented here, deliberately and consistently with the
//! identical clause in SYS-67: `atp_types::WorkloadPriority` is a closed seven-level hierarchy that
//! ends at research/Jupyter and has no backup rung, and the DATA-008 `tiering` module likewise
//! leaves its SYS-67 priority binding to the orchestrator. Scheduling and resource arbitration are
//! `atp-orchestrator`'s job (SYS-57/SYS-58); this module exposes [`BackupConfig::cadence_days`] and
//! [`due`] so a scheduler can drive it, and the data layer never reaches upward to enforce it.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use crate::store::{self, MarketDataStore, StoreError, StoreLock, STORE_FILENAME};
// Reuse the DATA-008 day constant rather than defining a second one: the cadence, the RPO and the
// tier's hot-retention window must all measure a day identically or the policies would disagree.
use crate::tiering::SECONDS_PER_DAY;

/// The SYS-60 recovery point objective ceiling, in days. A configured cadence may not exceed it.
pub const RPO_MAX_DAYS: u32 = 7;

/// The SYS-59 default backup schedule: weekly.
pub const DEFAULT_CADENCE_DAYS: u32 = 7;

/// Compile-time proof that the shipped default schedule can actually satisfy the shipped recovery
/// point objective. [`BackupConfig::new`] rejects an *operator-supplied* cadence above the ceiling;
/// this catches the same mistake being made in the defaults themselves, where no runtime path would
/// ever exercise the check. Raising [`DEFAULT_CADENCE_DAYS`] past [`RPO_MAX_DAYS`] fails the build.
const _: () = assert!(DEFAULT_CADENCE_DAYS <= RPO_MAX_DAYS);

/// Filename of the durable backup ledger, written under the *target* root so the evidence of what
/// was backed up travels with the archive rather than living only on the device that may fail.
pub const BACKUP_LEDGER_FILENAME: &str = "backup_ledger.log";

/// Scratch filename for the ledger's durable scratch → fsync → rename → parent-fsync publish.
const BACKUP_LEDGER_TMP_FILENAME: &str = "backup_ledger.log.tmp";

/// The backtest-results store filename owned by `atp-simulation::backtest_store`. Duplicated here
/// as a literal ON PURPOSE: `atp-data` is a lower layer and must not depend on `atp-simulation`
/// (that edge would invert the architecture, and `atp-simulation` already depends on this crate).
/// The literals are pinned against their owning definitions by the
/// `tests/test_data018_backup_store_contract.py` drift test, so a rename there fails a check here
/// instead of silently skipping backtest results at backup time.
pub const BACKTEST_STORE_FILENAME: &str = "backtest_results.store";

/// The magic header of the backtest-results blob. See [`BACKTEST_STORE_FILENAME`] for why it is a
/// literal rather than an import.
pub const BACKTEST_STORE_MAGIC: &str = "ATP-BACKTEST-RECORD";

/// Scratch sequence, so two concurrent exports into one target never collide on a scratch name.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// A fail-closed backup error.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BackupError {
    /// The configured cadence is zero — a schedule that never runs cannot meet any RPO.
    CadenceZero,
    /// The configured cadence exceeds the SYS-60 RPO ceiling, so the policy could never satisfy the
    /// recovery point objective even when every run succeeds.
    CadenceExceedsRpo {
        /// The rejected cadence, in days.
        configured: u32,
        /// The SYS-60 ceiling ([`RPO_MAX_DAYS`]).
        ceiling: u32,
    },
    /// Source and target denote the same directory (possibly via `.`/trailing slash/symlink).
    TargetNotDistinct {
        /// The aliased directory.
        dir: PathBuf,
    },
    /// One of the roots is nested inside the other, so the "backup" shares the source's failure
    /// domain — the case SYS-60 exists to survive.
    TargetSharesFailureDomain {
        /// The NAS source root.
        source: PathBuf,
        /// The external target root.
        target: PathBuf,
    },
    /// An I/O failure. `context` names the operation that failed.
    Io {
        /// What was being attempted.
        context: &'static str,
        /// The OS error number behind the failure, when the failure came from a syscall.
        ///
        /// Kept as a raw `i32` rather than an [`io::Error`] so `BackupError` stays `Clone + Eq`.
        /// Recording it is not cosmetic: an export that dies on `write export scratch file` with
        /// the cause discarded is indistinguishable, to the operator holding the failing media,
        /// from a full disk, a permissions problem, or a filesystem that does not implement the
        /// sync barrier at all — three faults with three different remedies.
        errno: Option<i32>,
    },
    /// A store-codec failure surfaced from the underlying blob.
    Store(StoreError),
}

impl fmt::Display for BackupError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::CadenceZero => write!(
                f,
                "backup cadence of 0 days is refused: a schedule that never runs cannot satisfy the \
                 SYS-60 {RPO_MAX_DAYS}-day recovery point objective"
            ),
            Self::CadenceExceedsRpo {
                configured,
                ceiling,
            } => write!(
                f,
                "backup cadence of {configured} days exceeds the SYS-60 recovery point objective of \
                 {ceiling} days: even a perfectly healthy schedule would leave more than {ceiling} \
                 days of data unrecoverable"
            ),
            Self::TargetNotDistinct { dir } => write!(
                f,
                "backup target and NAS source resolve to the same directory ({}): a copy onto \
                 itself is not a backup",
                dir.display()
            ),
            Self::TargetSharesFailureDomain { source, target } => write!(
                f,
                "backup target ({}) is nested within the NAS source ({}) (or vice versa): the copy \
                 would share the source's failure domain and be lost with it",
                target.display(),
                source.display()
            ),
            Self::Io {
                context,
                errno: Some(code),
            } => write!(f, "backup I/O failure: {context} (os error {code})"),
            Self::Io { context, .. } => write!(f, "backup I/O failure: {context}"),
            Self::Store(err) => write!(f, "backup store failure: {err}"),
        }
    }
}

impl Error for BackupError {}

impl From<StoreError> for BackupError {
    fn from(err: StoreError) -> Self {
        Self::Store(err)
    }
}

fn io_error(context: &'static str) -> BackupError {
    BackupError::Io {
        context,
        errno: None,
    }
}

/// An I/O error that carries the OS error number behind it, so the operator sees the cause.
fn os_error(context: &'static str, err: &io::Error) -> BackupError {
    BackupError::Io {
        context,
        errno: err.raw_os_error(),
    }
}

/// Whether the target filesystem can commit a write with the platform's sync barrier.
///
/// This is a property of the *media*, not of the data: it is recorded and reported separately from
/// a unit's [`BackupVerdict`], because the two answer different questions and collapsing them would
/// let one overstate the other.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SyncDurability {
    /// The write was flushed to stable storage with the platform's full sync barrier.
    FullSync,
    /// The target filesystem does not implement the barrier, so the bytes were written and read
    /// back but never explicitly flushed. See [`sync_to_stable_storage`].
    TargetUnsupported,
}

impl SyncDurability {
    /// A stable lowercase label for CLI/report rendering.
    pub fn label(self) -> &'static str {
        match self {
            Self::FullSync => "full-sync",
            Self::TargetUnsupported => "unsupported-by-target",
        }
    }

    /// The weaker of two observations — used to fold a whole run down to one honest answer.
    pub fn weakest(self, other: Self) -> Self {
        match (self, other) {
            (Self::FullSync, Self::FullSync) => Self::FullSync,
            _ => Self::TargetUnsupported,
        }
    }
}

/// Errno values that mean *"this filesystem does not implement the sync barrier"* rather than
/// *"the sync was attempted and failed"*.
///
/// Spelled out numerically per platform because the workspace forbids `unsafe` and takes no
/// external dependencies, so `libc`'s constants are out of reach. POSIX assigns `EINVAL` to
/// "the fildes argument does not refer to a file on which this operation is possible", which is
/// exactly this condition; a genuine fault (`EIO`, `ENOSPC`, `EBADF`) is not in this set and stays
/// fatal.
#[cfg(target_os = "macos")]
const SYNC_UNSUPPORTED_ERRNOS: &[i32] = &[45, 22, 25, 78]; // ENOTSUP, EINVAL, ENOTTY, ENOSYS
#[cfg(target_os = "linux")]
const SYNC_UNSUPPORTED_ERRNOS: &[i32] = &[95, 22, 25, 38]; // EOPNOTSUPP, EINVAL, ENOTTY, ENOSYS
#[cfg(not(any(target_os = "macos", target_os = "linux")))]
const SYNC_UNSUPPORTED_ERRNOS: &[i32] = &[22, 25]; // EINVAL, ENOTTY

/// Flush `file` to stable storage, distinguishing *"the barrier failed"* from *"this filesystem
/// has no barrier to offer"*.
///
/// [`fs::File::sync_all`] issues `fcntl(F_FULLFSYNC)` on macOS and `fsync` on Linux. The external
/// storage targets SYS-59 names — "USB drive, secondary NAS, or cloud archival bucket" — include
/// network and FUSE-backed mounts that answer `ENOTSUP`: smbfs returns it for `F_FULLFSYNC`
/// unconditionally, so an SMB-mounted secondary NAS could not be backed up **at all** while this
/// was treated as a fatal I/O error. The call being unavailable is not a write failure, and
/// refusing the whole backup over it protects nothing.
///
/// What is deliberately NOT done here is to quietly proceed. The outcome is returned, folded to
/// the weakest observation across the run, and rendered by the operator surface, so a `verified`
/// export on a barrier-less target can never be read as a flushed one. The integrity guarantee is
/// unaffected either way: SYS-60 validation re-reads the exported bytes through the owning codec,
/// which proves the bytes are on the media far more directly than an `fsync` return value does.
fn sync_to_stable_storage(file: &fs::File) -> Result<SyncDurability, io::Error> {
    match file.sync_all() {
        Ok(()) => Ok(SyncDurability::FullSync),
        Err(err) if sync_barrier_is_unavailable(&err) => Ok(SyncDurability::TargetUnsupported),
        Err(err) => Err(err),
    }
}

/// Whether `err` means the sync barrier does not exist on this filesystem, rather than that it was
/// attempted and failed. Split out from [`sync_to_stable_storage`] so the classification can be
/// tested directly — no test can mount a barrier-less filesystem to provoke the real thing.
fn sync_barrier_is_unavailable(err: &io::Error) -> bool {
    err.raw_os_error()
        .is_some_and(|code| SYNC_UNSUPPORTED_ERRNOS.contains(&code))
}

/// Resolve as much of `path` as currently exists, re-appending the components that do not.
///
/// [`Path::canonicalize`] fails outright on a path whose leaf is absent — and a backup target
/// legitimately does not exist until the first run creates it. Canonicalizing only fully-existing
/// paths therefore leaves a hole: a target like `/link/backup`, where `/link` is a symlink *into*
/// the NAS root, resolves to nothing at validation time, passes the failure-domain guard, and is
/// then created inside the very tree it was supposed to survive. Walking up to the deepest existing
/// ancestor and canonicalizing *that* closes the hole, because the symlinked ancestor is what
/// actually determines where the writes land.
fn resolve_best_effort(path: &Path) -> PathBuf {
    let mut prefix = path.to_path_buf();
    let mut suffix: Vec<std::ffi::OsString> = Vec::new();
    loop {
        if let Ok(real) = prefix.canonicalize() {
            let mut out = real;
            for part in suffix.iter().rev() {
                out.push(part);
            }
            return out;
        }
        let Some(name) = prefix.file_name().map(|n| n.to_os_string()) else {
            return path.to_path_buf();
        };
        let Some(parent) = prefix.parent().map(Path::to_path_buf) else {
            return path.to_path_buf();
        };
        suffix.push(name);
        prefix = parent;
    }
}

/// Whether two paths denote the **same directory**, catching a lexical alias (`.`, a trailing
/// slash, a doubled separator — [`Path::components`] normalizes these) and a symlink alias, the
/// latter resolved through [`resolve_best_effort`] so a not-yet-created target is still checked.
///
/// Deliberately a local copy of the DATA-008 `tiering::same_directory` idiom rather than a shared
/// export: the two modules reject aliasing for different reasons and must be free to tighten
/// independently — this one already has, per the target-creation hole described above.
fn same_directory(a: &Path, b: &Path) -> bool {
    if a.components().eq(b.components()) {
        return true;
    }
    resolve_best_effort(a) == resolve_best_effort(b)
}

/// Whether `inner` lies within `outer` (at any depth), checked lexically and again on
/// symlink-resolved paths so a target whose *parent* links into the source tree is caught before
/// anything is written.
fn is_nested_within(inner: &Path, outer: &Path) -> bool {
    fn lexical(inner: &Path, outer: &Path) -> bool {
        let inner: Vec<_> = inner.components().collect();
        let outer: Vec<_> = outer.components().collect();
        inner.len() > outer.len() && inner[..outer.len()] == outer[..]
    }
    lexical(inner, outer) || lexical(&resolve_best_effort(inner), &resolve_best_effort(outer))
}

/// A validator for a blob whose record codec lives OUTSIDE this crate.
///
/// `atp-data` can check any ATP store's envelope (magic + checksum) and can decode its own
/// `market_data.store` fully, but it must not depend on `atp-simulation` to decode a
/// `backtest_results.store` — that edge would invert the architecture. Rather than leave that gap
/// permanently open, a caller that *can* reach the owning codec may inject one here: return `Ok(())`
/// if the blob decodes, or `Err(reason)` if it does not.
///
/// With no validator supplied, a backtest unit is still proven **byte-identical to the NAS source**
/// and envelope-intact, and is reported as [`VerificationDepth::EnvelopeOnly`] so the weaker
/// evidence is never mistaken for a full decode.
pub type ForeignCodecValidator = std::sync::Arc<dyn Fn(&str) -> Result<(), String> + Send + Sync>;

/// Validated configuration of a backup policy: the NAS source root, the external target root, and
/// the schedule cadence.
#[derive(Clone)]
pub struct BackupConfig {
    nas_dir: PathBuf,
    target_dir: PathBuf,
    cadence_days: u32,
    backtest_validator: Option<ForeignCodecValidator>,
}

impl fmt::Debug for BackupConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BackupConfig")
            .field("nas_dir", &self.nas_dir)
            .field("target_dir", &self.target_dir)
            .field("cadence_days", &self.cadence_days)
            .field(
                "backtest_validator",
                &self.backtest_validator.as_ref().map(|_| "<supplied>"),
            )
            .finish()
    }
}

impl PartialEq for BackupConfig {
    /// Compares the policy, not the injected validator (functions have no meaningful equality).
    fn eq(&self, other: &Self) -> bool {
        self.nas_dir == other.nas_dir
            && self.target_dir == other.target_dir
            && self.cadence_days == other.cadence_days
    }
}

impl Eq for BackupConfig {}

impl BackupConfig {
    /// Build a validated backup config, failing closed when the cadence could never satisfy the
    /// SYS-60 RPO, or when the target is the source / shares its failure domain.
    pub fn new(
        nas_dir: impl Into<PathBuf>,
        target_dir: impl Into<PathBuf>,
        cadence_days: u32,
    ) -> Result<Self, BackupError> {
        if cadence_days == 0 {
            return Err(BackupError::CadenceZero);
        }
        if cadence_days > RPO_MAX_DAYS {
            return Err(BackupError::CadenceExceedsRpo {
                configured: cadence_days,
                ceiling: RPO_MAX_DAYS,
            });
        }
        let nas_dir = nas_dir.into();
        let target_dir = target_dir.into();
        if same_directory(&nas_dir, &target_dir) {
            return Err(BackupError::TargetNotDistinct { dir: nas_dir });
        }
        if is_nested_within(&target_dir, &nas_dir) || is_nested_within(&nas_dir, &target_dir) {
            return Err(BackupError::TargetSharesFailureDomain {
                source: nas_dir,
                target: target_dir,
            });
        }
        Ok(Self {
            nas_dir,
            target_dir,
            cadence_days,
            backtest_validator: None,
        })
    }

    /// Build a config at the SYS-59 default (weekly) cadence.
    pub fn with_default_cadence(
        nas_dir: impl Into<PathBuf>,
        target_dir: impl Into<PathBuf>,
    ) -> Result<Self, BackupError> {
        Self::new(nas_dir, target_dir, DEFAULT_CADENCE_DAYS)
    }

    /// Attach a validator for `backtest_results.store` blobs, supplied by a layer that can reach
    /// `atp-simulation`'s codec. With one attached, a backtest unit is decoded like a market-data
    /// unit and reported as [`VerificationDepth::RecordLevel`].
    pub fn with_backtest_validator(mut self, validator: ForeignCodecValidator) -> Self {
        self.backtest_validator = Some(validator);
        self
    }

    /// The attached foreign-codec validator, if any.
    pub fn backtest_validator(&self) -> Option<&ForeignCodecValidator> {
        self.backtest_validator.as_ref()
    }

    /// The NAS source root.
    pub fn nas_dir(&self) -> &Path {
        &self.nas_dir
    }

    /// The external target root.
    pub fn target_dir(&self) -> &Path {
        &self.target_dir
    }

    /// The configured schedule cadence, in days.
    pub fn cadence_days(&self) -> u32 {
        self.cadence_days
    }
}

/// The integrity verdict for an exported unit — **tri-state on purpose**.
///
/// `Unverified` is not a soft failure: it means the check could not run (the target was
/// unreachable, or there was nothing to check), so the archive is *not proven good*. It must never
/// be collapsed into `Verified`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BackupVerdict {
    /// The exported blob was re-read through the owning codec and matches the source.
    Verified,
    /// The exported blob is present but wrong: a bad magic header, a checksum mismatch, a codec
    /// error, or a record set that differs from the source.
    Corrupt,
    /// The check could not be performed — the target was unreachable, or no unit was found.
    Unverified,
}

impl BackupVerdict {
    /// Whether this verdict proves the archive good. Only [`Verified`](Self::Verified) does.
    pub fn is_verified(self) -> bool {
        matches!(self, Self::Verified)
    }

    /// A stable lowercase label for CLI/report rendering.
    pub fn label(self) -> &'static str {
        match self {
            Self::Verified => "verified",
            Self::Corrupt => "corrupt",
            Self::Unverified => "unverified",
        }
    }
}

/// How the *target side* of an export fared, keeping a recoverable outage distinct from a
/// reachable-but-broken archive. Mirrors the DATA-008 `NasSyncStatus` split.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetStatus {
    /// The export was written and published to the target.
    Written,
    /// The target was unreachable (unplugged drive, unmounted share). Recoverable — a later run
    /// catches up, and this is NOT an integrity fault.
    Degraded,
    /// The target was reachable but the export could not be completed or did not verify. Needs
    /// operator attention now; must NOT be mistaken for an offline target.
    Failed,
}

impl TargetStatus {
    /// A stable lowercase label for CLI/report rendering.
    pub fn label(self) -> &'static str {
        match self {
            Self::Written => "written",
            Self::Degraded => "degraded",
            Self::Failed => "failed",
        }
    }
}

/// Which codec owns a discovered backup unit.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum UnitKind {
    /// A `market_data.store` blob — verified at the full record level by this crate's codec.
    MarketData,
    /// A `backtest_results.store` blob — verified at the envelope (magic + checksum) level, since
    /// its record codec lives in the higher `atp-simulation` layer.
    BacktestResults,
}

impl UnitKind {
    /// The store filename this kind is discovered by.
    pub fn filename(self) -> &'static str {
        match self {
            Self::MarketData => STORE_FILENAME,
            Self::BacktestResults => BACKTEST_STORE_FILENAME,
        }
    }

    /// The magic header the blob must begin with.
    pub fn magic(self) -> &'static str {
        match self {
            Self::MarketData => store::MAGIC,
            Self::BacktestResults => BACKTEST_STORE_MAGIC,
        }
    }

    /// A stable lowercase label for CLI/report rendering.
    pub fn label(self) -> &'static str {
        match self {
            Self::MarketData => "market-data",
            Self::BacktestResults => "backtest-results",
        }
    }

    /// Recover the kind from a unit id, which always ends in the owning store's filename (see
    /// [`DiscoveredUnit::id`]).
    ///
    /// Needed where a unit is known only by name — an expected unit that is **absent** from the
    /// archive was never discovered there, so there is no [`DiscoveredUnit`] to read the kind from.
    /// Defaulting those to [`UnitKind::MarketData`] made a missing `backtest_results.store` render
    /// as a market-data unit, which is exactly the "weaker check rendered as the stronger one"
    /// confusion [`VerificationDepth`] exists to prevent.
    pub fn from_unit_id(id: &str) -> Option<Self> {
        let name = id.rsplit('/').next().unwrap_or(id);
        [Self::MarketData, Self::BacktestResults]
            .into_iter()
            .find(|kind| kind.filename() == name)
    }
}

/// **How strong** the evidence behind a [`BackupVerdict::Verified`] actually is.
///
/// Two units can both be "verified" on very different evidence, and rendering them identically
/// overstates the weaker one. A `market_data.store` is checked all the way down to its record set
/// by this crate's own codec; a `backtest_results.store` can only be checked to its magic +
/// checksum envelope, because its record codec lives in `atp-simulation`, a *higher* layer this one
/// must not depend on. Envelope verification still catches corruption, truncation and bit-rot — it
/// cannot catch a structurally-intact blob the owning codec would reject.
///
/// Closing that gap needs a validator hosted where `atp-simulation` is reachable (owner:
/// SRS-BT-009 / the backtest-store layer); until then this field keeps the difference visible
/// instead of letting a weaker check pass for a stronger one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VerificationDepth {
    /// Envelope **and** full record-set decode by the owning codec.
    RecordLevel,
    /// Magic header + checksum only; the record codec belongs to another layer.
    EnvelopeOnly,
    /// No successful verification (the unit is not `Verified`).
    NotVerified,
}

impl VerificationDepth {
    /// A stable lowercase label for CLI/report rendering.
    pub fn label(self) -> &'static str {
        match self {
            Self::RecordLevel => "record-level",
            Self::EnvelopeOnly => "envelope-only",
            Self::NotVerified => "unverified",
        }
    }
}

/// The per-unit outcome of a backup run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnitReport {
    /// The unit's path relative to the NAS root (the same relative path is used under the target).
    pub unit: String,
    /// Which codec owns the blob.
    pub kind: UnitKind,
    /// How the target side fared.
    pub status: TargetStatus,
    /// Whether the *exported* copy was proven to match the source.
    pub verdict: BackupVerdict,
    /// Record count read back from the exported blob, when it could be parsed.
    pub records: Option<usize>,
    /// How strong the evidence behind a `Verified` verdict is — see [`VerificationDepth`].
    pub verification: VerificationDepth,
    /// Human-readable detail — the reason for a non-`Verified` verdict, verbatim.
    pub detail: String,
}

/// The outcome of a whole backup run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BackupReport {
    /// When the run was evaluated (unix seconds), as supplied by the caller.
    pub ran_at: i64,
    /// Per-unit outcomes, ordered by relative path.
    pub units: Vec<UnitReport>,
    /// Whether this target's filesystem could commit the run's writes with a sync barrier, folded
    /// to the **weakest** observation across every unit written. `None` when the run wrote nothing
    /// (an absent target root, or no units to export), because no barrier was ever attempted —
    /// which must not render as either a success or a failure.
    pub durability: Option<SyncDurability>,
}

impl BackupReport {
    /// The aggregate verdict over every unit, computed **fail-closed**:
    ///
    /// - any `Corrupt` unit ⇒ `Corrupt` (one bad blob condemns the run);
    /// - else any `Unverified` unit ⇒ `Unverified`;
    /// - else, **if there is at least one unit**, `Verified`.
    ///
    /// A run that discovered **no units at all** is `Unverified`, never `Verified` — an empty NAS
    /// root and a fully-backed-up NAS root must not render alike, or a misconfigured source path
    /// would report a green backup forever.
    pub fn verdict(&self) -> BackupVerdict {
        if self.units.is_empty() {
            return BackupVerdict::Unverified;
        }
        if self
            .units
            .iter()
            .any(|u| u.verdict == BackupVerdict::Corrupt)
        {
            return BackupVerdict::Corrupt;
        }
        if self
            .units
            .iter()
            .any(|u| u.verdict == BackupVerdict::Unverified)
        {
            return BackupVerdict::Unverified;
        }
        BackupVerdict::Verified
    }

    /// The units that were proven good — the only ones that may advance the RPO clock.
    pub fn verified_units(&self) -> Vec<&UnitReport> {
        self.units
            .iter()
            .filter(|u| u.verdict.is_verified())
            .collect()
    }
}

/// Verify a blob's **envelope**: the magic header line, then the FNV-1a checksum line over the
/// remaining body, recomputed with [`store::checksum`] — the same function that wrote it.
///
/// Returns the body on success so a caller can do a stronger record-level check on top.
fn verify_envelope<'a>(text: &'a str, expected_magic: &str) -> Result<&'a str, String> {
    let Some((magic, rest)) = text.split_once('\n') else {
        return Err("blob has no magic header line".to_string());
    };
    if magic != expected_magic {
        return Err(format!(
            "magic header mismatch: expected '{expected_magic}', found '{magic}'"
        ));
    }
    let Some((checksum_line, body)) = rest.split_once('\n') else {
        return Err("blob has no checksum line".to_string());
    };
    let stored: i128 = checksum_line
        .parse()
        .map_err(|_| format!("checksum line is not an integer: '{checksum_line}'"))?;
    let actual = i128::from(store::checksum(body.as_bytes()));
    if stored != actual {
        return Err(format!(
            "checksum mismatch: blob records {stored}, recomputed {actual} — the exported copy is \
             corrupt or truncated"
        ));
    }
    Ok(body)
}

/// Discover the backup units under `root`: directories holding a recognised store blob.
///
/// Walks depth-first and is tolerant of unreadable subdirectories (they simply yield no unit) but
/// **not** of an unreadable root, which is an [`Err`] — a missing NAS mount must fail closed, not
/// silently report "no data to back up".
/// A discovered backup unit: its durable identity, the directory it lives in, and its codec.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct DiscoveredUnit {
    /// The unit's durable identity — the store file's path relative to the root, e.g.
    /// `equities/market_data.store`.
    ///
    /// **Includes the filename on purpose.** One directory may legitimately hold both a
    /// `market_data.store` and a `backtest_results.store`; keying a unit by its directory alone
    /// would collapse those two distinct blobs into one ledger entry, so a verified market-data
    /// export would silently vouch for a backtest blob that was never backed up.
    pub id: String,
    /// The unit's directory relative to the root (`""` at the root itself).
    pub dir: String,
    /// Which codec owns the blob.
    pub kind: UnitKind,
}

fn discover_units(root: &Path) -> Result<Vec<DiscoveredUnit>, BackupError> {
    if !root.is_dir() {
        return Err(io_error("NAS source root is missing or not a directory"));
    }
    let mut found = Vec::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for kind in [UnitKind::MarketData, UnitKind::BacktestResults] {
            if dir.join(kind.filename()).is_file() {
                let rel = dir
                    .strip_prefix(root)
                    .map(|p| p.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let id = if rel.is_empty() {
                    kind.filename().to_string()
                } else {
                    format!("{rel}/{}", kind.filename())
                };
                found.push(DiscoveredUnit { id, dir: rel, kind });
            }
        }
        // FAIL CLOSED on an unreadable subtree. Skipping it would shrink the discovered unit set,
        // and since the aggregate verdict is computed over *discovered* units the run would report
        // `Verified` — and advance the RPO ledger — while a whole branch of the NAS was never
        // exported. "Back up all NAS data" cannot be satisfied by backing up the readable part.
        let entries = fs::read_dir(&dir).map_err(|_| {
            io_error("NAS subdirectory could not be read; refusing a partial backup")
        })?;
        for entry in entries {
            let entry = entry.map_err(|_| {
                io_error("NAS directory entry could not be read; refusing a partial backup")
            })?;
            let path = entry.path();
            // Do not follow symlinks while walking: a link back into the tree would loop, and a
            // link out of it would silently pull unrelated data into the archive.
            if path.is_dir() && !entry.path().is_symlink() {
                stack.push(path);
            }
        }
    }
    found.sort();
    Ok(found)
}

/// The durable identities of every backup unit currently under `root`.
///
/// This is what [`rpo_report`] and [`due`] must be judged against: the RPO is a statement about the
/// data that exists *now*, not about whatever the ledger happens to remember. Fails closed on an
/// unreadable root or subtree, so a partial listing can never be mistaken for a complete one.
pub fn discover_unit_names(root: &Path) -> Result<Vec<String>, BackupError> {
    Ok(discover_units(root)?.into_iter().map(|u| u.id).collect())
}

/// Write `bytes` to a scratch file beside `path` and fsync it, returning the scratch path.
///
/// Split from the publish step on purpose: staging lets a caller verify the bytes that actually
/// landed on the media *before* the rename replaces the live archive, so a failed verification
/// leaves the previous good copy in place.
fn stage_export(
    required_root: Option<&Path>,
    path: &Path,
    bytes: &[u8],
) -> Result<(PathBuf, SyncDurability), BackupError> {
    // `create_dir_all` creates INTERMEDIATE directories, so on a mount that vanished after the
    // run-start guard it would happily recreate the external target root itself on local disk —
    // and the export would then verify against a copy sitting inside the source's failure domain.
    // Require the root to be present immediately before, and again after, the staged write.
    if let Some(root) = required_root {
        if !root.is_dir() {
            return Err(io_error(
                "backup target root is not present; refusing to recreate it on local disk",
            ));
        }
    }
    let dir = path
        .parent()
        .ok_or_else(|| io_error("export path has no parent directory"))?;
    fs::create_dir_all(dir).map_err(|_| io_error("create export directory"))?;
    let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
    let tmp = dir.join(format!(
        "{}.{}.{seq}.tmp",
        path.file_name()
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_else(|| "export".to_string()),
        std::process::id()
    ));
    let mut scratch =
        fs::File::create(&tmp).map_err(|err| os_error("create export scratch file", &err))?;
    if let Err(err) = scratch.write_all(bytes) {
        let _ = fs::remove_file(&tmp);
        return Err(os_error("write export scratch file", &err));
    }
    let durability = match sync_to_stable_storage(&scratch) {
        Ok(durability) => durability,
        Err(err) => {
            let _ = fs::remove_file(&tmp);
            return Err(os_error("sync export scratch file", &err));
        }
    };
    // Re-check after the write: if the mount disappeared mid-operation, `create_dir_all` above may
    // have recreated the tree locally. Tear the staged file down rather than publish it.
    if let Some(root) = required_root {
        if !root.is_dir() {
            let _ = fs::remove_file(&tmp);
            return Err(io_error(
                "backup target root disappeared during the export; refusing a local-disk archive",
            ));
        }
    }
    Ok((tmp, durability))
}

/// Atomically publish a staged file over `final_path`, then fsync the directory so the rename
/// itself is durable. Returns whether that directory sync was actually available on this target.
fn publish_staged(staged: &Path, final_path: &Path) -> Result<SyncDurability, BackupError> {
    fs::rename(staged, final_path).map_err(|err| os_error("publish export file", &err))?;
    let dir = final_path
        .parent()
        .ok_or_else(|| io_error("export path has no parent directory"))?;
    let handle = fs::File::open(dir).map_err(|err| os_error("open export directory", &err))?;
    sync_to_stable_storage(&handle).map_err(|err| os_error("sync export directory", &err))
}

/// Durably publish `bytes` to `path`: stage, fsync, atomic rename, fsync parent. Used where there
/// is nothing to verify between the two halves (the ledger, and restore's output).
fn durable_write(
    required_root: Option<&Path>,
    path: &Path,
    bytes: &[u8],
) -> Result<SyncDurability, BackupError> {
    let (staged, staged_durability) = stage_export(required_root, path, bytes)?;
    match publish_staged(&staged, path) {
        Ok(published_durability) => Ok(staged_durability.weakest(published_durability)),
        Err(err) => {
            let _ = fs::remove_file(&staged);
            Err(err)
        }
    }
}

/// Export every NAS backup unit to the external target and **verify each exported copy**.
///
/// The source is opened read-only and is never written, renamed, or removed — a backup that can
/// damage the thing it protects is worse than no backup. Every unit is independent: one corrupt or
/// unreachable unit degrades its own report entry and does not abort the others, so a single bad
/// blob cannot hide the state of the rest of the archive.
///
/// `now` is injected (unix seconds) rather than read from the clock, so the run is deterministic
/// and testable.
pub fn run_backup(config: &BackupConfig, now: i64) -> Result<BackupReport, BackupError> {
    // Re-check aliasing at run time, not just at construction: a symlink could have been created
    // after the config was built, and by then both roots exist so canonicalize() is meaningful.
    if same_directory(config.nas_dir(), config.target_dir()) {
        return Err(BackupError::TargetNotDistinct {
            dir: config.nas_dir().to_path_buf(),
        });
    }
    if is_nested_within(config.target_dir(), config.nas_dir())
        || is_nested_within(config.nas_dir(), config.target_dir())
    {
        return Err(BackupError::TargetSharesFailureDomain {
            source: config.nas_dir().to_path_buf(),
            target: config.target_dir().to_path_buf(),
        });
    }

    let units = discover_units(config.nas_dir())?;

    // The configured target ROOT must already exist. Creating it on demand is the difference
    // between "wrote to the USB drive" and "wrote a directory named like the USB drive onto the
    // machine whose failure this backup exists to survive": with the mount absent,
    // `create_dir_all` would happily materialise the path locally, the export would verify against
    // itself, and the ledger would certify a backup that lives entirely inside the failure domain.
    // An unplugged drive is a RECOVERABLE outage, so this is Degraded/Unverified per unit rather
    // than an error — the next window catches up — but it is never `Verified` and never advances
    // the RPO clock. Per-unit subdirectories are still created beneath an existing root.
    if !config.target_dir().is_dir() {
        return Ok(BackupReport {
            ran_at: now,
            // Nothing was written, so no sync barrier was attempted: `None`, never `FullSync`.
            durability: None,
            units: units
                .into_iter()
                .map(|unit| UnitReport {
                    unit: unit.id,
                    kind: unit.kind,
                    status: TargetStatus::Degraded,
                    verdict: BackupVerdict::Unverified,
                    records: None,
                    verification: VerificationDepth::NotVerified,
                    detail: format!(
                        "backup target root {} does not exist — refusing to create it, since an \
                         absent external mount would otherwise be backed up onto local disk",
                        config.target_dir().display()
                    ),
                })
                .collect(),
        });
    }

    let mut reports = Vec::with_capacity(units.len());
    let mut durability: Option<SyncDurability> = None;
    for unit in units {
        let (report, observed) = export_unit(config, &unit);
        // Fold to the weakest observation: one unit that could not be flushed makes the whole run
        // no better than that, and a later full-sync unit must not paper over it.
        if let Some(observed) = observed {
            durability = Some(match durability {
                Some(current) => current.weakest(observed),
                None => observed,
            });
        }
        reports.push(report);
    }
    Ok(BackupReport {
        ran_at: now,
        units: reports,
        durability,
    })
}

/// Export and verify one unit, never panicking and never propagating — every failure is folded
/// into the unit's own report entry so the run continues.
///
/// Returns the unit's report plus the sync-barrier observation from **published** writes only: a
/// staged copy that failed verification is deleted rather than archived, so its write says nothing
/// about the durability of the archive the operator ends up holding.
///
/// ## Publish ordering: the previous good archive is never destroyed by a failed run
///
/// The new blob is staged beside the live archive file and **fully verified there** — envelope,
/// record codec, and equality with the source — before the atomic rename that publishes it. An
/// earlier version wrote first and checked afterwards, which meant a codec-invalid source (one that
/// passes the cheap envelope check but that the store itself would reject) could replace a
/// perfectly good previous backup with a corrupt one and merely *report* the failure. Validating
/// before publishing means the worst case of a failed export is that the archive keeps its previous
/// verified copy, which is exactly what a backup is for.
fn export_unit(
    config: &BackupConfig,
    unit: &DiscoveredUnit,
) -> (UnitReport, Option<SyncDurability>) {
    let kind = unit.kind;
    let source_file = config.nas_dir().join(&unit.dir).join(kind.filename());
    let target_file = config.target_dir().join(&unit.dir).join(kind.filename());

    let blank = UnitReport {
        unit: unit.id.clone(),
        kind,
        status: TargetStatus::Failed,
        verdict: BackupVerdict::Corrupt,
        records: None,
        verification: VerificationDepth::NotVerified,
        detail: String::new(),
    };

    // Read the source. A source we cannot read is NOT a target problem — report it as an
    // unverified unit so it is visibly not backed up, rather than a silently skipped one.
    let source_text = match fs::read_to_string(&source_file) {
        Ok(text) => text,
        Err(err) => {
            return (
                UnitReport {
                    verdict: BackupVerdict::Unverified,
                    detail: format!("source unit could not be read: {err}"),
                    ..blank
                },
                None,
            )
        }
    };

    // Validate the SOURCE completely BEFORE anything is written. Copying a blob that is already
    // broken and then "verifying" the copy against it would replicate the corruption faithfully and
    // call it verified — and, worse, publish it over the last good archive.
    let validator = config.backtest_validator();
    let source_check = check_blob(&source_text, kind, blank.clone(), validator);
    if !source_check.verdict.is_verified() {
        return (
            UnitReport {
                status: TargetStatus::Failed,
                detail: format!(
                    "source unit is corrupt, refusing to export it: {}",
                    source_check.detail
                ),
                ..source_check
            },
            None,
        );
    }

    // The root-level failure-domain guard is not enough: a per-unit target subdirectory can itself
    // be a symlink INTO the NAS (`usb/equities -> nas/equities`). The root check passes, and the
    // export would then publish into the source tree — under concurrent ingestion it could even
    // overwrite newer NAS data while reporting a verified backup. Resolve this unit's own parent
    // and require it to stay under the target root and outside the NAS root.
    if let Some(parent) = target_file.parent() {
        let resolved = resolve_best_effort(parent);
        let target_root = resolve_best_effort(config.target_dir());
        let nas_root = resolve_best_effort(config.nas_dir());
        let under_target = resolved == target_root || is_nested_within(&resolved, &target_root);
        let in_source = resolved == nas_root || is_nested_within(&resolved, &nas_root);
        if !under_target || in_source {
            return (
                UnitReport {
                    status: TargetStatus::Failed,
                    verdict: BackupVerdict::Unverified,
                    detail: format!(
                        "target path for this unit resolves to {} — outside the backup target root \
                         or inside the NAS source; refusing to write (a symlinked unit directory \
                         would publish the backup into the source tree)",
                        resolved.display()
                    ),
                    ..blank
                },
                None,
            );
        }
    }

    // Stage the replacement beside the live file; nothing is published yet.
    let (staged, staged_durability) = match stage_export(
        Some(config.target_dir()),
        &target_file,
        source_text.as_bytes(),
    ) {
        Ok(staged) => staged,
        Err(err) => {
            // Classify ONLY — never create. Calling `create_dir_all` here would materialise a
            // vanished external mount on local disk mid-run, after which later units in this same
            // loop would export and verify inside the source's failure domain and be recorded as
            // genuine backups.
            let reachable = config.target_dir().is_dir();
            return (
                UnitReport {
                    status: if reachable {
                        TargetStatus::Failed
                    } else {
                        TargetStatus::Degraded
                    },
                    verdict: BackupVerdict::Unverified,
                    detail: format!("export did not complete: {err}"),
                    ..blank
                },
                None,
            );
        }
    };

    // Validate integrity ON COMPLETION (SYS-60) by re-reading what actually landed on the media —
    // still at the staged path, so a failure here leaves the previous archive untouched.
    let outcome = verify_staged(&staged, &source_text, kind, blank.clone(), validator);
    if !outcome.verdict.is_verified() {
        let _ = fs::remove_file(&staged);
        return (outcome, None);
    }

    // Only now publish, atomically.
    let published_durability = match publish_staged(&staged, &target_file) {
        Ok(durability) => durability,
        Err(err) => {
            let _ = fs::remove_file(&staged);
            return (
                UnitReport {
                    status: TargetStatus::Failed,
                    verdict: BackupVerdict::Unverified,
                    detail: format!("verified export could not be published: {err}"),
                    ..blank
                },
                None,
            );
        }
    };
    (
        outcome,
        Some(staged_durability.weakest(published_durability)),
    )
}

/// Read back and fully check a staged export against its source.
fn verify_staged(
    staged: &Path,
    source_text: &str,
    kind: UnitKind,
    blank: UnitReport,
    validator: Option<&ForeignCodecValidator>,
) -> UnitReport {
    let exported = match fs::read_to_string(staged) {
        Ok(text) => text,
        Err(err) => {
            return UnitReport {
                status: TargetStatus::Failed,
                verdict: BackupVerdict::Unverified,
                detail: format!("staged export could not be re-read for verification: {err}"),
                ..blank
            }
        }
    };

    // The SAME verifier `verify_archive` and `restore` use, so the three paths can never drift to
    // different strictness — and so verification-depth attribution is produced in one place.
    let checked = check_blob(&exported, kind, blank.clone(), validator);
    if !checked.verdict.is_verified() {
        return checked;
    }

    // Verifying the export in isolation is not enough for ANY codec: a well-formed blob that is
    // not the bytes we read from the NAS would still pass. Since the export writes the source bytes
    // verbatim, byte equality is the universal proof that this is a faithful copy — and it is the
    // only check available for backtest-results, whose record codec lives in a higher layer.
    if exported != source_text {
        return UnitReport {
            status: TargetStatus::Written,
            detail: "exported bytes differ from the source bytes".to_string(),
            ..blank
        };
    }

    // For this crate's own format, additionally compare at the record level, so a difference is
    // reported in terms of records rather than opaque bytes.
    if kind == UnitKind::MarketData {
        let (Ok(source_store), Ok(target_store)) = (
            MarketDataStore::restore(source_text),
            MarketDataStore::restore(&exported),
        ) else {
            return UnitReport {
                status: TargetStatus::Written,
                detail: "record-set comparison could not be performed".to_string(),
                ..blank
            };
        };
        if source_store.serialize() != target_store.serialize() {
            return UnitReport {
                status: TargetStatus::Written,
                detail: format!(
                    "exported record set differs from the source ({} source records vs {} exported)",
                    source_store.len(),
                    target_store.len()
                ),
                ..blank
            };
        }
    }
    checked
}

/// Run a backup and record its verified units **under an exclusive lock on the target**, returning
/// the report and the updated ledger.
///
/// This is the entry point callers should use. `run_backup` + `BackupLedger::record` as two
/// separate steps leaves a window in which two runs interleave: an older run can publish its
/// (staler) bytes over a newer archive while the newer run's ledger timestamp survives, producing a
/// confident within-RPO status describing data that is no longer there. Export, verification and
/// ledger advancement have to be one atomic unit with respect to the archive, so they are — via the
/// crate's existing single-writer [`StoreLock`], acquired on the target root for the whole
/// operation and released on drop.
///
/// A concurrent holder fails closed with [`StoreError::Locked`] rather than proceeding.
pub fn run_backup_locked(
    config: &BackupConfig,
    now: i64,
) -> Result<(BackupReport, BackupLedger), BackupError> {
    // An absent target root cannot be locked. Report it the same way `run_backup` does — a
    // recoverable Degraded outage — instead of surfacing a confusing lock error.
    if !config.target_dir().is_dir() {
        let report = run_backup(config, now)?;
        return Ok((report, BackupLedger::new()));
    }
    let _guard = StoreLock::acquire(config.target_dir()).map_err(BackupError::Store)?;
    let mut ledger = BackupLedger::load(config.target_dir())?;
    let report = run_backup(config, now)?;
    ledger.record(&report, config.target_dir())?;
    Ok((report, ledger))
}

/// One durable ledger record: a unit that was **proven** backed up, and when.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LedgerEntry {
    /// The unit's path relative to the NAS root.
    pub unit: String,
    /// Unix seconds at which the unit was verified.
    pub verified_at: i64,
}

/// The durable record of verified backups, used to answer "is a backup due?" and "what is our
/// RPO?" across process restarts.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct BackupLedger {
    entries: Vec<LedgerEntry>,
}

impl BackupLedger {
    /// An empty ledger — the state of a system that has never completed a verified backup.
    pub fn new() -> Self {
        Self::default()
    }

    /// Every recorded entry, in file order.
    pub fn entries(&self) -> &[LedgerEntry] {
        &self.entries
    }

    /// The newest verified timestamp per unit.
    pub fn newest_per_unit(&self) -> BTreeMap<&str, i64> {
        let mut out: BTreeMap<&str, i64> = BTreeMap::new();
        for entry in &self.entries {
            let slot = out.entry(entry.unit.as_str()).or_insert(entry.verified_at);
            if entry.verified_at > *slot {
                *slot = entry.verified_at;
            }
        }
        out
    }

    /// The oldest of the per-unit newest timestamps *the ledger knows about*. `None` when empty.
    ///
    /// **This is not an RPO answer on its own.** A ledger can only speak about units it has seen,
    /// so a NAS unit that has *never* been backed up successfully contributes nothing here and is
    /// invisible to this method — a run where one unit verified and another was corrupt would
    /// otherwise look as fresh as a fully successful one. Use [`rpo_report`] / [`due`], which take
    /// the units currently present on the NAS and treat an unbacked one as a breach.
    pub fn effective_verified_at(&self) -> Option<i64> {
        self.newest_per_unit().values().copied().min()
    }

    /// The oldest verified timestamp across `current_units`, or `None` if **any** of them has no
    /// entry at all — an unbacked unit has no recovery point, so there is no timestamp to report.
    pub fn effective_verified_at_for(&self, current_units: &[String]) -> Option<i64> {
        if current_units.is_empty() {
            return None;
        }
        let newest = self.newest_per_unit();
        let mut oldest = i64::MAX;
        for unit in current_units {
            let ts = *newest.get(unit.as_str())?;
            oldest = oldest.min(ts);
        }
        Some(oldest)
    }

    /// The units in `current_units` with no verified backup on record — the ones whose data would
    /// be lost outright if the NAS failed now.
    pub fn unbacked_units(&self, current_units: &[String]) -> Vec<String> {
        let newest = self.newest_per_unit();
        current_units
            .iter()
            .filter(|unit| !newest.contains_key(unit.as_str()))
            .cloned()
            .collect()
    }

    /// Parse a ledger from its serialized text, **fail-closed on a torn tail**.
    ///
    /// A crash mid-append can leave a partial final line. That trailing fragment is discarded (the
    /// text is split at the last newline), because a half-written record is not evidence of a
    /// verified backup. A malformed line *before* the tail is a real corruption and is an error —
    /// silently skipping it would understate how stale the archive is, which is precisely the
    /// direction an RPO check must never err in.
    pub fn parse(text: &str) -> Result<Self, BackupError> {
        let complete = match text.rfind('\n') {
            Some(idx) => &text[..idx],
            None => "",
        };
        let mut entries = Vec::new();
        for line in complete.lines() {
            if line.trim().is_empty() {
                continue;
            }
            let Some((unit, ts)) = line.rsplit_once('\t') else {
                return Err(io_error("backup ledger line is missing its separator"));
            };
            let Ok(verified_at) = ts.parse::<i64>() else {
                return Err(io_error("backup ledger timestamp is not an integer"));
            };
            entries.push(LedgerEntry {
                unit: unit.to_string(),
                verified_at,
            });
        }
        Ok(Self { entries })
    }

    /// Serialize to the on-disk form: one `unit<TAB>timestamp` record per line.
    pub fn serialize(&self) -> String {
        let mut out = String::new();
        for entry in &self.entries {
            out.push_str(&entry.unit);
            out.push('\t');
            out.push_str(&entry.verified_at.to_string());
            out.push('\n');
        }
        out
    }

    /// Load the ledger stored under `target_dir`. An absent ledger is an **empty** ledger (a target
    /// that has never been backed up to), but an unreadable or malformed one is an error.
    pub fn load(target_dir: &Path) -> Result<Self, BackupError> {
        let path = target_dir.join(BACKUP_LEDGER_FILENAME);
        match fs::read_to_string(&path) {
            Ok(text) => Self::parse(&text),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(Self::new()),
            Err(_) => Err(io_error("read backup ledger")),
        }
    }

    /// Record the verified units of `report` and durably publish the ledger under `target_dir`.
    ///
    /// Only units whose verdict is [`BackupVerdict::Verified`] are recorded — an unverified or
    /// corrupt unit must not advance the RPO clock, or the ledger would certify a backup that was
    /// never proven.
    pub fn record(&mut self, report: &BackupReport, target_dir: &Path) -> Result<(), BackupError> {
        // Nothing proven ⇒ nothing to record, and critically ⇒ nothing to WRITE. Publishing the
        // ledger would call through to `create_dir_all`, which on an absent external mount would
        // materialise the target root on local disk — re-introducing, via the evidence file, the
        // exact "backup that lives inside the failure domain" that `run_backup` just refused.
        if report.verified_units().is_empty() {
            return Ok(());
        }
        for unit in report.verified_units() {
            self.entries.push(LedgerEntry {
                unit: unit.unit.clone(),
                verified_at: report.ran_at,
            });
        }
        let path = target_dir.join(BACKUP_LEDGER_FILENAME);
        let scratch_hint = target_dir.join(BACKUP_LEDGER_TMP_FILENAME);
        debug_assert_ne!(path, scratch_hint);
        // The ledger's own sync-barrier outcome is deliberately dropped rather than surfaced: it
        // lives on the same target as the units whose durability the run already reports, so a
        // second copy of the same fact could only ever agree — or drift and contradict it.
        durable_write(Some(target_dir), &path, self.serialize().as_bytes()).map(|_| ())
    }
}

/// The SYS-60 recovery-point-objective assessment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RpoReport {
    /// The effective verified timestamp (the stalest *currently-present* unit), when every unit has
    /// one. `None` whenever some unit has never been verified — an unbacked unit has no recovery
    /// point to report.
    pub verified_at: Option<i64>,
    /// Whole days between `verified_at` and `now`, when computable.
    pub age_days: Option<i64>,
    /// The SYS-60 ceiling this was judged against.
    pub ceiling_days: u32,
    /// Units present on the NAS with **no** verified backup on record. Non-empty ⇒ not within RPO,
    /// and these are the units whose data would be lost outright if the NAS failed now.
    pub unbacked_units: Vec<String>,
    /// Whether the objective is **proven** met.
    pub within_rpo: bool,
}

/// Assess the RPO for the units **currently present on the NAS**, fail-closed.
///
/// `current_units` is what [`discover_unit_names`] found on the source right now; the ledger alone
/// is not enough to answer this. A ledger can only speak about units it has already seen, so
/// judging on ledger contents lets two distinct false greens through:
///
/// - a run where unit A verified and unit B was corrupt records A, and the archive then looks as
///   fresh as a fully successful run even though B was never exported;
/// - a *complete* run at t1 over `{A}` stays "fresh" after a new unit B appears on the NAS and
///   every later run fails on it, because B never reaches the ledger to drag the timestamp down.
///
/// Both are the same bug: absence from the ledger reads as absence of a problem. Taking the current
/// unit set makes an unbacked unit an explicit breach ([`RpoReport::unbacked_units`]) instead.
///
/// `within_rpo` is therefore true only when there is at least one unit, **every** current unit has
/// a verified backup, and the stalest of those is within [`RPO_MAX_DAYS`]. A future-dated timestamp
/// (a clock jump — not evidence of freshness) also yields `false`.
pub fn rpo_report(ledger: &BackupLedger, current_units: &[String], now: i64) -> RpoReport {
    let unbacked = ledger.unbacked_units(current_units);
    let base = RpoReport {
        verified_at: None,
        age_days: None,
        ceiling_days: RPO_MAX_DAYS,
        unbacked_units: unbacked.clone(),
        within_rpo: false,
    };
    if !unbacked.is_empty() || current_units.is_empty() {
        return base;
    }
    let Some(verified_at) = ledger.effective_verified_at_for(current_units) else {
        return base;
    };
    if verified_at > now {
        return RpoReport {
            verified_at: Some(verified_at),
            ..base
        };
    }
    // Compare RAW SECONDS, not floored days. Integer-dividing first would call a backup that is
    // seven days and one second old "7 days" and pass it, holding a green status for almost a full
    // day after the objective was actually breached. `age_days` is retained for display only.
    let elapsed = now - verified_at;
    RpoReport {
        verified_at: Some(verified_at),
        age_days: Some(elapsed / SECONDS_PER_DAY),
        within_rpo: elapsed <= i64::from(RPO_MAX_DAYS) * SECONDS_PER_DAY,
        ..base
    }
}

/// Whether a backup is due under the configured cadence, judged against the units currently on the
/// NAS.
///
/// Due whenever any current unit has **no** verified backup (including the never-backed-up system,
/// and the mixed run where one unit failed) — deferring is never correct while some unit is
/// unprotected. A future-dated entry is due too, so a clock jump cannot suppress backups
/// indefinitely.
pub fn due(
    ledger: &BackupLedger,
    config: &BackupConfig,
    current_units: &[String],
    now: i64,
) -> bool {
    if !ledger.unbacked_units(current_units).is_empty() {
        return true;
    }
    match ledger.effective_verified_at_for(current_units) {
        None => true,
        Some(verified_at) if verified_at > now => true,
        Some(verified_at) => {
            (now - verified_at) >= i64::from(config.cadence_days()) * SECONDS_PER_DAY
        }
    }
}

/// The outcome of a recovery.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RestoreReport {
    /// Per-unit outcomes, ordered by relative path.
    pub units: Vec<UnitReport>,
}

impl RestoreReport {
    /// The aggregate verdict, computed with the same fail-closed rules as [`BackupReport::verdict`]
    /// — including that restoring **zero** units is `Unverified`, never a vacuous success.
    pub fn verdict(&self) -> BackupVerdict {
        BackupReport {
            ran_at: 0,
            units: self.units.clone(),
            // A restore/verify writes nothing to the archive, so there is no barrier to report.
            durability: None,
        }
        .verdict()
    }
}

/// Re-verify an existing archive **without writing anything**, against the units that are expected
/// to be in it.
///
/// A ledger entry is a record that a unit verified *once*. It is not evidence about the archive
/// **now**: the external drive can be wiped, a blob deleted, or bit-rot can set in between runs. A
/// scheduler that skips a not-yet-due backup while the archive is silently empty has an RPO of
/// infinity and a green status, so the not-due path must re-read the media rather than trust the
/// ledger.
///
/// `expected_units` names what should be present (normally the units currently on the NAS); an
/// expected unit missing from the archive is reported [`Unverified`](BackupVerdict::Unverified),
/// which keeps it out of a `Verified` aggregate.
pub fn verify_archive(
    target_dir: &Path,
    expected_units: &[String],
    backtest_validator: Option<&ForeignCodecValidator>,
) -> Result<RestoreReport, BackupError> {
    if !target_dir.is_dir() {
        return Err(io_error(
            "backup target is missing or not a directory; the archive cannot be verified",
        ));
    }
    let present: BTreeMap<String, DiscoveredUnit> = discover_units(target_dir)?
        .into_iter()
        .map(|u| (u.id.clone(), u))
        .collect();

    // Check the UNION of what is expected and what is actually there, not just the expected list.
    // `restore` discovers and restores every archived unit, so a stale or corrupt blob left behind
    // in the target — one no longer on the NAS, and therefore absent from `expected_units` — would
    // be invisible to a status check and then fail during the recovery that status just blessed.
    let mut to_check: Vec<String> = expected_units.to_vec();
    for id in present.keys() {
        if !expected_units.iter().any(|e| e == id) {
            to_check.push(id.clone());
        }
    }
    to_check.sort();
    to_check.dedup();

    let mut reports = Vec::new();
    for unit in &to_check {
        let base = UnitReport {
            unit: unit.clone(),
            // Derived from the id, not defaulted: an absent unit has no `DiscoveredUnit` to read
            // the kind from, and naming the wrong codec in the operator's report is a lie about
            // what is missing. A present unit overwrites this from `found.kind` below.
            kind: UnitKind::from_unit_id(unit).unwrap_or(UnitKind::MarketData),
            status: TargetStatus::Failed,
            verdict: BackupVerdict::Corrupt,
            records: None,
            verification: VerificationDepth::NotVerified,
            detail: String::new(),
        };
        let Some(found) = present.get(unit) else {
            reports.push(UnitReport {
                status: TargetStatus::Degraded,
                verdict: BackupVerdict::Unverified,
                detail: "expected unit is absent from the archive".to_string(),
                ..base
            });
            continue;
        };
        let kind = found.kind;
        let base = UnitReport { kind, ..base };
        let path = target_dir.join(&found.dir).join(kind.filename());
        // `is_file()` and `read_to_string` both FOLLOW symlinks, so an archive entry that is really
        // a link back into the NAS (`equities/market_data.store -> /nas/equities/...`) would read
        // and verify perfectly — while containing no recoverable bytes of its own. If the NAS is
        // lost, so is the "backup". Require a regular file via `symlink_metadata`, which does not
        // follow the link.
        match fs::symlink_metadata(&path) {
            Ok(meta) if meta.file_type().is_file() => {}
            Ok(_) => {
                reports.push(UnitReport {
                    verdict: BackupVerdict::Unverified,
                    detail:
                        "archived unit is a symlink, not a real copy: it holds no bytes of its \
                             own and would be lost with whatever it points at"
                            .to_string(),
                    ..base
                });
                continue;
            }
            Err(_) => {
                reports.push(UnitReport {
                    verdict: BackupVerdict::Unverified,
                    detail: "archived unit could not be inspected".to_string(),
                    ..base
                });
                continue;
            }
        }
        let Ok(text) = fs::read_to_string(&path) else {
            reports.push(UnitReport {
                verdict: BackupVerdict::Unverified,
                detail: "archived unit could not be read".to_string(),
                ..base
            });
            continue;
        };
        reports.push(check_blob(&text, kind, base, backtest_validator));
    }
    reports.sort_by(|a, b| a.unit.cmp(&b.unit));
    Ok(RestoreReport { units: reports })
}

/// Verify one archived blob's bytes: envelope first, then the record codec where this crate owns
/// it. Shared by [`verify_archive`] and [`restore`] so the two can never drift apart in strictness.
fn check_blob(
    text: &str,
    kind: UnitKind,
    base: UnitReport,
    validator: Option<&ForeignCodecValidator>,
) -> UnitReport {
    if let Err(reason) = verify_envelope(text, kind.magic()) {
        return UnitReport {
            status: TargetStatus::Written,
            detail: reason,
            ..base
        };
    }
    match kind {
        UnitKind::MarketData => match MarketDataStore::restore(text) {
            Ok(store) => UnitReport {
                status: TargetStatus::Written,
                verdict: BackupVerdict::Verified,
                records: Some(store.len()),
                verification: VerificationDepth::RecordLevel,
                detail: String::new(),
                ..base
            },
            Err(err) => UnitReport {
                status: TargetStatus::Written,
                detail: format!(
                    "unit passed the envelope check but failed the record codec: {err}"
                ),
                ..base
            },
        },
        UnitKind::BacktestResults => match validator {
            // A caller that can reach the owning codec supplied one: decode for real.
            Some(validate) => match validate(text) {
                Ok(()) => UnitReport {
                    status: TargetStatus::Written,
                    verdict: BackupVerdict::Verified,
                    records: None,
                    verification: VerificationDepth::RecordLevel,
                    detail: String::new(),
                    ..base
                },
                Err(reason) => UnitReport {
                    status: TargetStatus::Written,
                    detail: format!(
                        "unit passed the envelope check but the owning backtest codec rejected \
                         it: {reason}"
                    ),
                    ..base
                },
            },
            // No validator wired: FAIL CLOSED. Envelope integrity and byte-identity with the NAS
            // source are real evidence, but they do not prove the blob is RESTORABLE — the owning
            // codec rejects schema drift, duplicate run ids and malformed records that a checksum
            // cannot see. Reporting `Verified` here would let a backtest archive the simulation
            // layer cannot load advance the RPO ledger and exit a `restore` successfully. So the
            // verdict is `Unverified` ("could not be checked"), never `Corrupt` (which would
            // wrongly accuse an intact blob), and the detail says exactly what was and was not
            // proven. Attach a `ForeignCodecValidator` from a layer that can reach
            // `atp-simulation` to turn this into a real `Verified`.
            None => UnitReport {
                status: TargetStatus::Written,
                verdict: BackupVerdict::Unverified,
                records: None,
                verification: VerificationDepth::EnvelopeOnly,
                detail: "envelope intact and byte-identical to the source, but restorability is \
                         UNPROVEN: the backtest record codec lives in atp-simulation and no \
                         ForeignCodecValidator was supplied"
                    .to_string(),
                ..base
            },
        },
    }
}

/// Restore from an archive at `target_dir` into `dest_dir`, verifying every restored unit.
///
/// This is the *validated* half of "validated recovery support": each unit is re-read from `dest`
/// after being written and checked against the archived bytes, so a recovery that silently dropped
/// or mangled a blob cannot report success. `dest_dir` must not be the archive itself.
pub fn restore(
    target_dir: &Path,
    dest_dir: &Path,
    backtest_validator: Option<&ForeignCodecValidator>,
) -> Result<RestoreReport, BackupError> {
    if same_directory(target_dir, dest_dir) {
        return Err(BackupError::TargetNotDistinct {
            dir: target_dir.to_path_buf(),
        });
    }
    // A destination *inside* the archive would have recovery write into the very tree it is reading
    // from: the restored copies then become discoverable "archive contents" on the next pass, and a
    // later verify would be checking the archive against itself. Mirror the backup guard.
    if is_nested_within(dest_dir, target_dir) || is_nested_within(target_dir, dest_dir) {
        return Err(BackupError::TargetSharesFailureDomain {
            source: target_dir.to_path_buf(),
            target: dest_dir.to_path_buf(),
        });
    }
    let units = discover_units(target_dir)?;
    let mut reports = Vec::with_capacity(units.len());
    for unit in units {
        let kind = unit.kind;
        let archived = target_dir.join(&unit.dir).join(kind.filename());
        let restored = dest_dir.join(&unit.dir).join(kind.filename());
        let base = UnitReport {
            unit: unit.id.clone(),
            kind,
            status: TargetStatus::Failed,
            verdict: BackupVerdict::Corrupt,
            records: None,
            verification: VerificationDepth::NotVerified,
            detail: String::new(),
        };
        // A symlinked archive entry is not a recoverable copy (see verify_archive).
        if !matches!(fs::symlink_metadata(&archived), Ok(meta) if meta.file_type().is_file()) {
            reports.push(UnitReport {
                verdict: BackupVerdict::Unverified,
                detail: "archived unit is a symlink or unreadable, not a real copy".to_string(),
                ..base
            });
            continue;
        }
        let Ok(archived_text) = fs::read_to_string(&archived) else {
            reports.push(UnitReport {
                verdict: BackupVerdict::Unverified,
                detail: "archived unit could not be read".to_string(),
                ..base
            });
            continue;
        };
        // Validate the ARCHIVE fully — envelope AND record codec — BEFORE writing anything. The
        // envelope alone is not enough: a blob can carry a valid magic + checksum and still be
        // rejected by the store codec, and publishing it first would overwrite a good previously
        // recovered file with bytes we are about to reject. Same publish-ordering rule as
        // `export_unit`: never destroy a good copy on the strength of an unvalidated one.
        let archive_check = check_blob(&archived_text, kind, base.clone(), backtest_validator);
        if !archive_check.verdict.is_verified() {
            reports.push(UnitReport {
                detail: format!(
                    "archived unit is corrupt, refusing to restore it: {}",
                    archive_check.detail
                ),
                ..archive_check
            });
            continue;
        }

        // Stage into the destination, verify what landed, and only then publish. The sync-barrier
        // outcome is not reported here: it would describe the RECOVERY destination, not the
        // archive, and a restore proves its own bytes by re-reading them below regardless.
        let staged = match stage_export(None, &restored, archived_text.as_bytes()) {
            Ok((path, _durability)) => path,
            Err(err) => {
                reports.push(UnitReport {
                    verdict: BackupVerdict::Unverified,
                    detail: format!("restore did not complete: {err}"),
                    ..base
                });
                continue;
            }
        };
        let Ok(restored_text) = fs::read_to_string(&staged) else {
            let _ = fs::remove_file(&staged);
            reports.push(UnitReport {
                verdict: BackupVerdict::Unverified,
                detail: "restored unit could not be re-read for verification".to_string(),
                ..base
            });
            continue;
        };
        if restored_text != archived_text {
            let _ = fs::remove_file(&staged);
            reports.push(UnitReport {
                status: TargetStatus::Written,
                detail: "restored bytes differ from the archived bytes".to_string(),
                ..base
            });
            continue;
        }
        let checked = check_blob(&restored_text, kind, base.clone(), backtest_validator);
        if !checked.verdict.is_verified() {
            let _ = fs::remove_file(&staged);
            reports.push(checked);
            continue;
        }
        if let Err(err) = publish_staged(&staged, &restored) {
            let _ = fs::remove_file(&staged);
            reports.push(UnitReport {
                status: TargetStatus::Failed,
                verdict: BackupVerdict::Unverified,
                detail: format!("verified restore could not be published: {err}"),
                ..base
            });
            continue;
        }
        reports.push(checked);
    }
    reports.sort_by(|a, b| a.unit.cmp(&b.unit));
    Ok(RestoreReport { units: reports })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unit(name: &str, verdict: BackupVerdict) -> UnitReport {
        UnitReport {
            unit: name.to_string(),
            kind: UnitKind::MarketData,
            status: TargetStatus::Written,
            verdict,
            records: Some(1),
            verification: VerificationDepth::RecordLevel,
            detail: String::new(),
        }
    }

    fn report(units: Vec<UnitReport>) -> BackupReport {
        BackupReport {
            ran_at: 0,
            units,
            durability: None,
        }
    }

    // ----------------------------------------------------------------- //
    // Config: the cadence/RPO coupling and the failure-domain rules
    // ----------------------------------------------------------------- //

    #[test]
    fn cadence_longer_than_the_rpo_is_refused() {
        let err = BackupConfig::new("/nas", "/usb", RPO_MAX_DAYS + 1).unwrap_err();
        assert_eq!(
            err,
            BackupError::CadenceExceedsRpo {
                configured: RPO_MAX_DAYS + 1,
                ceiling: RPO_MAX_DAYS,
            }
        );
    }

    #[test]
    fn cadence_exactly_at_the_rpo_ceiling_is_accepted() {
        assert!(BackupConfig::new("/nas", "/usb", RPO_MAX_DAYS).is_ok());
    }

    #[test]
    fn zero_cadence_is_refused() {
        assert_eq!(
            BackupConfig::new("/nas", "/usb", 0).unwrap_err(),
            BackupError::CadenceZero
        );
    }

    #[test]
    fn default_cadence_is_weekly_and_within_the_rpo() {
        // The default-vs-ceiling invariant itself is proven at COMPILE time by the `const _` above;
        // this asserts the SYS-59 value and that the default constructor actually accepts it.
        assert_eq!(DEFAULT_CADENCE_DAYS, 7);
        let config = BackupConfig::with_default_cadence("/nas", "/usb").unwrap();
        assert_eq!(config.cadence_days(), DEFAULT_CADENCE_DAYS);
    }

    #[test]
    fn target_aliasing_the_source_is_refused() {
        assert!(matches!(
            BackupConfig::new("/nas", "/nas/.", 7).unwrap_err(),
            BackupError::TargetNotDistinct { .. }
        ));
    }

    #[test]
    fn target_inside_the_source_shares_its_failure_domain_and_is_refused() {
        assert!(matches!(
            BackupConfig::new("/nas", "/nas/backups", 7).unwrap_err(),
            BackupError::TargetSharesFailureDomain { .. }
        ));
        // ...and the reverse nesting is equally unusable.
        assert!(matches!(
            BackupConfig::new("/nas/data", "/nas", 7).unwrap_err(),
            BackupError::TargetSharesFailureDomain { .. }
        ));
    }

    #[test]
    fn a_sibling_target_is_accepted() {
        assert!(BackupConfig::new("/mnt/nas", "/mnt/usb", 7).is_ok());
    }

    // ----------------------------------------------------------------- //
    // Aggregate verdict: absence of evidence is never evidence
    // ----------------------------------------------------------------- //

    #[test]
    fn a_run_with_no_units_is_unverified_not_a_vacuous_pass() {
        assert_eq!(report(vec![]).verdict(), BackupVerdict::Unverified);
    }

    #[test]
    fn one_corrupt_unit_condemns_the_whole_run() {
        let r = report(vec![
            unit("a", BackupVerdict::Verified),
            unit("b", BackupVerdict::Corrupt),
            unit("c", BackupVerdict::Verified),
        ]);
        assert_eq!(r.verdict(), BackupVerdict::Corrupt);
    }

    #[test]
    fn corrupt_outranks_unverified() {
        let r = report(vec![
            unit("a", BackupVerdict::Unverified),
            unit("b", BackupVerdict::Corrupt),
        ]);
        assert_eq!(r.verdict(), BackupVerdict::Corrupt);
    }

    #[test]
    fn one_unverified_unit_prevents_a_verified_run() {
        let r = report(vec![
            unit("a", BackupVerdict::Verified),
            unit("b", BackupVerdict::Unverified),
        ]);
        assert_eq!(r.verdict(), BackupVerdict::Unverified);
        assert_eq!(r.verified_units().len(), 1);
    }

    #[test]
    fn all_verified_is_verified() {
        let r = report(vec![
            unit("a", BackupVerdict::Verified),
            unit("b", BackupVerdict::Verified),
        ]);
        assert_eq!(r.verdict(), BackupVerdict::Verified);
        assert!(r.verdict().is_verified());
    }

    // ----------------------------------------------------------------- //
    // Ledger: fail-closed parsing, stalest-unit semantics
    // ----------------------------------------------------------------- //

    #[test]
    fn ledger_round_trips() {
        let mut ledger = BackupLedger::new();
        ledger.entries.push(LedgerEntry {
            unit: "equities".to_string(),
            verified_at: 100,
        });
        let parsed = BackupLedger::parse(&ledger.serialize()).unwrap();
        assert_eq!(parsed, ledger);
    }

    #[test]
    fn a_torn_final_line_is_discarded_not_trusted() {
        // A crash mid-append leaves a partial record. It must not count as a verified backup.
        let text = "equities\t100\noptions\t2";
        let parsed = BackupLedger::parse(text).unwrap();
        assert_eq!(parsed.entries().len(), 1);
        assert_eq!(parsed.entries()[0].unit, "equities");
    }

    #[test]
    fn a_malformed_complete_line_is_an_error_not_a_skip() {
        // Skipping it would UNDERSTATE staleness — the one direction an RPO check must never err.
        assert!(BackupLedger::parse("equities\tnot-a-number\nx\t1\n").is_err());
        assert!(BackupLedger::parse("no-separator-here\nx\t1\n").is_err());
    }

    #[test]
    fn effective_timestamp_is_the_stalest_unit() {
        // The archive is only as fresh as its oldest unit.
        let ledger = BackupLedger::parse("equities\t500\noptions\t100\nequities\t900\n").unwrap();
        assert_eq!(ledger.newest_per_unit().get("equities"), Some(&900));
        assert_eq!(ledger.effective_verified_at(), Some(100));
    }

    #[test]
    fn an_empty_ledger_has_no_effective_timestamp() {
        assert_eq!(BackupLedger::new().effective_verified_at(), None);
    }

    // ----------------------------------------------------------------- //
    // RPO + cadence arithmetic
    // ----------------------------------------------------------------- //

    /// The unit set a single-unit fixture ledger describes.
    fn one_unit() -> Vec<String> {
        vec!["u".to_string()]
    }

    #[test]
    fn no_verified_backup_is_never_within_rpo() {
        let rpo = rpo_report(&BackupLedger::new(), &one_unit(), 1_000_000);
        assert!(!rpo.within_rpo);
        assert_eq!(rpo.verified_at, None);
        assert_eq!(rpo.age_days, None);
        assert_eq!(rpo.ceiling_days, RPO_MAX_DAYS);
        assert_eq!(rpo.unbacked_units, vec!["u".to_string()]);
    }

    #[test]
    fn a_unit_with_no_ledger_entry_breaches_the_rpo_however_fresh_its_siblings_are() {
        // The mixed-run false green: unit `a` verified just now, unit `b` never did. Judging on the
        // ledger alone would read as fresh, because `b` simply is not in it.
        let now = 100 * SECONDS_PER_DAY;
        let ledger = BackupLedger::parse(&format!("a\t{now}\n")).unwrap();
        let units = vec!["a".to_string(), "b".to_string()];
        let rpo = rpo_report(&ledger, &units, now);
        assert!(
            !rpo.within_rpo,
            "an unbacked unit is a breach, not a rounding error"
        );
        assert_eq!(rpo.unbacked_units, vec!["b".to_string()]);
        assert_eq!(
            rpo.verified_at, None,
            "there is no recovery point for `b` to report"
        );

        let config = BackupConfig::new("/nas", "/usb", 7).unwrap();
        assert!(
            due(&ledger, &config, &units, now),
            "a backup is due while `b` is unprotected"
        );
    }

    #[test]
    fn a_newly_appeared_unit_drops_a_previously_green_archive_out_of_rpo() {
        // The second false green: a COMPLETE run over {a} at t1, then unit `b` appears on the NAS.
        // Gating the ledger on whole-run success would not catch this; judging against the current
        // unit set does.
        let t1 = 100 * SECONDS_PER_DAY;
        let ledger = BackupLedger::parse(&format!("a\t{t1}\n")).unwrap();
        assert!(rpo_report(&ledger, &["a".to_string()], t1).within_rpo);

        let with_new = vec!["a".to_string(), "b".to_string()];
        let one_day_later = t1 + SECONDS_PER_DAY;
        assert!(!rpo_report(&ledger, &with_new, one_day_later).within_rpo);
    }

    #[test]
    fn an_empty_current_unit_set_is_never_within_rpo() {
        let ledger = BackupLedger::parse("a\t100\n").unwrap();
        assert!(!rpo_report(&ledger, &[], 100).within_rpo);
    }

    #[test]
    fn the_stalest_current_unit_sets_the_recovery_point() {
        let now = 100 * SECONDS_PER_DAY;
        let ledger = BackupLedger::parse(&format!(
            "a\t{}\nb\t{}\n",
            now - SECONDS_PER_DAY,
            now - 5 * SECONDS_PER_DAY
        ))
        .unwrap();
        let units = vec!["a".to_string(), "b".to_string()];
        assert_eq!(rpo_report(&ledger, &units, now).age_days, Some(5));
    }

    #[test]
    fn rpo_breaches_one_second_past_seven_days_not_a_day_later() {
        // Flooring elapsed time to whole days would hold a green status for almost 24 more hours
        // after the objective was actually breached.
        let now = 100 * SECONDS_PER_DAY;
        let exactly = BackupLedger::parse(&format!("u\t{}\n", now - 7 * SECONDS_PER_DAY)).unwrap();
        assert!(rpo_report(&exactly, &one_unit(), now).within_rpo);

        let one_second_late =
            BackupLedger::parse(&format!("u\t{}\n", now - 7 * SECONDS_PER_DAY - 1)).unwrap();
        let rpo = rpo_report(&one_second_late, &one_unit(), now);
        assert!(
            !rpo.within_rpo,
            "7 days + 1 second must breach the 7-day objective"
        );
        assert_eq!(rpo.age_days, Some(7), "the DISPLAY value still floors to 7");
    }

    #[test]
    fn rpo_boundary_is_inclusive_at_seven_days_and_fails_past_it() {
        let now = 100 * SECONDS_PER_DAY;
        let at_ceiling =
            BackupLedger::parse(&format!("u\t{}\n", now - 7 * SECONDS_PER_DAY)).unwrap();
        assert!(rpo_report(&at_ceiling, &one_unit(), now).within_rpo);
        assert_eq!(rpo_report(&at_ceiling, &one_unit(), now).age_days, Some(7));

        let past = BackupLedger::parse(&format!("u\t{}\n", now - 8 * SECONDS_PER_DAY)).unwrap();
        assert!(!rpo_report(&past, &one_unit(), now).within_rpo);
        assert_eq!(rpo_report(&past, &one_unit(), now).age_days, Some(8));
    }

    #[test]
    fn a_future_timestamp_is_not_treated_as_freshness() {
        // A clock jump must not be able to certify compliance.
        let ledger = BackupLedger::parse("u\t9000000\n").unwrap();
        let rpo = rpo_report(&ledger, &one_unit(), 1_000);
        assert!(!rpo.within_rpo);
        assert_eq!(rpo.age_days, None);
    }

    #[test]
    fn a_system_that_never_backed_up_is_always_due() {
        let config = BackupConfig::new("/nas", "/usb", 7).unwrap();
        assert!(due(&BackupLedger::new(), &config, &one_unit(), 0));
    }

    #[test]
    fn due_fires_exactly_at_the_cadence_boundary() {
        let config = BackupConfig::new("/nas", "/usb", 7).unwrap();
        let now = 100 * SECONDS_PER_DAY;
        let just_under =
            BackupLedger::parse(&format!("u\t{}\n", now - 7 * SECONDS_PER_DAY + 1)).unwrap();
        assert!(!due(&just_under, &config, &one_unit(), now));
        let exactly = BackupLedger::parse(&format!("u\t{}\n", now - 7 * SECONDS_PER_DAY)).unwrap();
        assert!(due(&exactly, &config, &one_unit(), now));
    }

    #[test]
    fn a_future_timestamp_cannot_suppress_backups() {
        let config = BackupConfig::new("/nas", "/usb", 7).unwrap();
        let ledger = BackupLedger::parse("u\t9000000\n").unwrap();
        assert!(due(&ledger, &config, &one_unit(), 1_000));
    }

    // ----------------------------------------------------------------- //
    // Envelope verification
    // ----------------------------------------------------------------- //

    fn envelope(magic: &str, body: &str) -> String {
        format!(
            "{magic}\n{}\n{body}",
            i128::from(store::checksum(body.as_bytes()))
        )
    }

    #[test]
    fn a_well_formed_envelope_verifies_and_yields_its_body() {
        let text = envelope(store::MAGIC, "payload\n");
        assert_eq!(verify_envelope(&text, store::MAGIC).unwrap(), "payload\n");
    }

    #[test]
    fn a_foreign_magic_is_rejected() {
        let text = envelope(BACKTEST_STORE_MAGIC, "payload\n");
        assert!(verify_envelope(&text, store::MAGIC).is_err());
    }

    #[test]
    fn a_single_flipped_byte_fails_the_checksum() {
        let text = envelope(store::MAGIC, "payload\n");
        let tampered = text.replace("payload", "paylOad");
        assert_ne!(text, tampered);
        assert!(verify_envelope(&tampered, store::MAGIC).is_err());
    }

    #[test]
    fn a_truncated_blob_is_rejected_rather_than_read_short() {
        assert!(verify_envelope(store::MAGIC, store::MAGIC).is_err());
        assert!(verify_envelope(&format!("{}\n", store::MAGIC), store::MAGIC).is_err());
    }

    #[test]
    fn a_non_integer_checksum_line_is_rejected() {
        let text = format!("{}\nnot-a-number\nbody\n", store::MAGIC);
        assert!(verify_envelope(&text, store::MAGIC).is_err());
    }

    // ----------------------------------------------------------------- //
    // Kind vocabulary
    // ----------------------------------------------------------------- //

    #[test]
    fn unit_kinds_carry_distinct_filenames_and_magics() {
        assert_eq!(UnitKind::MarketData.filename(), STORE_FILENAME);
        assert_eq!(UnitKind::MarketData.magic(), store::MAGIC);
        assert_eq!(
            UnitKind::BacktestResults.filename(),
            BACKTEST_STORE_FILENAME
        );
        assert_eq!(UnitKind::BacktestResults.magic(), BACKTEST_STORE_MAGIC);
        assert_ne!(
            UnitKind::MarketData.filename(),
            UnitKind::BacktestResults.filename()
        );
        assert_ne!(
            UnitKind::MarketData.magic(),
            UnitKind::BacktestResults.magic()
        );
    }

    #[test]
    fn a_unit_kind_is_recoverable_from_its_id_alone() {
        // An expected-but-absent unit is known only by id, so this is the only way to name its
        // codec honestly. Both the bare and the nested form must resolve.
        assert_eq!(
            UnitKind::from_unit_id(STORE_FILENAME),
            Some(UnitKind::MarketData)
        );
        assert_eq!(
            UnitKind::from_unit_id(BACKTEST_STORE_FILENAME),
            Some(UnitKind::BacktestResults)
        );
        assert_eq!(
            UnitKind::from_unit_id(&format!("equities/{STORE_FILENAME}")),
            Some(UnitKind::MarketData)
        );
        assert_eq!(
            UnitKind::from_unit_id(&format!("backtests/{BACKTEST_STORE_FILENAME}")),
            Some(UnitKind::BacktestResults)
        );
        assert_eq!(UnitKind::from_unit_id("equities/notes.txt"), None);
        assert_eq!(UnitKind::from_unit_id(""), None);
    }

    #[test]
    fn a_missing_sync_barrier_is_classified_apart_from_a_failed_one() {
        // The whole point of the split: "this filesystem has no barrier" must not read like
        // "the flush failed", or an SMB/NFS/cloud target can never be backed up at all.
        for code in SYNC_UNSUPPORTED_ERRNOS {
            assert!(
                sync_barrier_is_unavailable(&io::Error::from_raw_os_error(*code)),
                "errno {code} should classify as an absent barrier"
            );
        }
        // Real faults stay fatal. EIO (5) and ENOSPC (28/28) are genuine write failures on both
        // supported platforms, and EBADF (9) means we handed the syscall a broken descriptor.
        for code in [5, 9, 28] {
            assert!(
                !sync_barrier_is_unavailable(&io::Error::from_raw_os_error(code)),
                "errno {code} is a real fault and must stay fatal"
            );
        }
        // An error with no OS number behind it cannot be a "this filesystem lacks the call" answer.
        assert!(!sync_barrier_is_unavailable(&io::Error::other("synthetic")));
    }

    #[test]
    fn durability_folds_to_the_weakest_observation() {
        use SyncDurability::{FullSync, TargetUnsupported};
        assert_eq!(FullSync.weakest(FullSync), FullSync);
        // One unflushed unit makes the whole run unflushed; order must not change the answer.
        assert_eq!(FullSync.weakest(TargetUnsupported), TargetUnsupported);
        assert_eq!(TargetUnsupported.weakest(FullSync), TargetUnsupported);
        assert_eq!(
            TargetUnsupported.weakest(TargetUnsupported),
            TargetUnsupported
        );
        assert_eq!(FullSync.label(), "full-sync");
        assert_eq!(TargetUnsupported.label(), "unsupported-by-target");
    }

    #[test]
    fn an_io_error_reports_the_os_cause_when_it_has_one() {
        let with_cause = os_error(
            "write export scratch file",
            &io::Error::from_raw_os_error(45),
        );
        assert_eq!(
            with_cause.to_string(),
            "backup I/O failure: write export scratch file (os error 45)"
        );
        // A context-only error must not grow a misleading "(os error …)" tail.
        assert_eq!(
            io_error("read backup ledger").to_string(),
            "backup I/O failure: read backup ledger"
        );
    }
}
