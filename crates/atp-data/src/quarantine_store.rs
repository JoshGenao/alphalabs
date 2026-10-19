//! SRS-DATA-013 / SyRS SYS-77 — the durable quarantine store.
//!
//! SYS-77: records that fail validation "shall be quarantined in a separate store", and the
//! operator is alerted "with the count and nature of quarantined records". The ERR-5 gate already
//! keeps an invalid record out of the primary tables; this module is where it GOES.
//!
//! **One file per ingestion run.** Every batch that reaches either primary-storage write path
//! ([`DataLayer::ingest_market_records_tiered`](crate::DataLayer::ingest_market_records_tiered) or
//! [`DataLayer::ingest_market_records_quarantining`](crate::DataLayer::ingest_market_records_quarantining))
//! records exactly one run — clean or not — under `<ssd>/quarantine/<run_id>.qrun`. A run holds the
//! batch totals, what happened to the primary write, and every quarantined record IN FULL (the
//! SyRS "Data Quarantine Record": the rejected payload, its reason, its source, and when). The
//! counts are derived from those entries, never stored beside them, so they cannot disagree.
//!
//! The store lives beside the SSD primary it guards, so it is located by the tier itself: no
//! caller can reach primary storage without also reaching the quarantine store, and there is no
//! configuration knob to forget. The raw tier write is crate-private, so outside this crate the
//! ONLY ways to write provider market data to the SSD primary are the two recorded paths. The
//! compiler holds that, not a scan:
//!
//! ```compile_fail
//! use atp_data::tiering::{OnConflict, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
//! use atp_data::QuarantineStoreError;
//! let tier = TieredStore::new(TierConfig::new("/ssd", "/nas", DEFAULT_HOT_RETENTION_DAYS).unwrap());
//! // error[E0624]: method `ingest` is private — no unrecorded write to the SSD primary. (The call
//! // is otherwise well-formed, so making `ingest` public makes this compile and the test fail.)
//! let _ = tier.ingest(Vec::new(), OnConflict::RefuseBatch, |_| Ok::<(), QuarantineStoreError>(()));
//! ```
//!
//! while the same tier through a recorded path compiles:
//!
//! ```no_run
//! use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
//! use atp_data::{DataLayer, QuarantineSummarySink, Sys77RecordValidator};
//! let tier = TieredStore::new(TierConfig::new("/ssd", "/nas", DEFAULT_HOT_RETENTION_DAYS).unwrap());
//! let _ = DataLayer.ingest_market_records_tiered(
//!     &tier,
//!     Vec::new(),
//!     &Sys77RecordValidator::new(),
//!     &QuarantineSummarySink::new(),
//!     0,
//! );
//! ```
//!
//! **Durability.** Each run file is written atomically (unique scratch file, `fsync`, `rename`,
//! directory `fsync`) — the same discipline as [`MarketDataStore::save_to_path`](crate::store::MarketDataStore::save_to_path)
//! — and carries the same FNV-1a integrity checksum, so a reader sees a whole run or none of it.
//! A run is written TWICE: once as [`PrimaryWrite::Pending`] before the primary write starts, and
//! again with the outcome. A process that dies in between leaves `Pending` on disk, which reads as
//! "outcome unknown" — never as a write that did or did not happen — and so does a failure that
//! may have followed the primary store's atomic rename. Newly created directories are `fsync`ed
//! into their parents, so a recorded run survives a power loss with its directory.
//!
//! **Every exit is recorded.** Both write paths send every early error through
//! [`QuarantineStore::refuse`], which records the batch — with every reject classified so far — as
//! `REFUSED` before the error is returned. `tools/ingestion_validation_check.py` fails on any other
//! early return in those paths.
//!
//! **Scope.** The store lives on the SSD primary only. It is not archived to NAS and is not a backup
//! unit (the SRS-DATA-018 backup walks NAS catalogs), so losing the SSD loses the quarantine
//! history along with the primary it describes.
//!
//! **Reads are bounded and tri-state.** [`QuarantineStore::read_latest`] parses at most
//! [`MAX_RUNS_READ`] of the newest run files (it lists the directory, which is O(runs) in names
//! but reads no other file). A run file that cannot be read is reported as
//! [`RunRead::Unreadable`] in its place — never skipped, never counted as clean. A store whose SSD
//! directory is missing is an error, not an empty history.

use std::fs;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use atp_types::QuarantineReason;

use crate::ingestion_validation::ALL_QUARANTINE_REASONS;
use crate::store::{
    checksum, decode_record, encode_record, push_count, push_i128, push_line, push_str, Cursor,
    MarketDataRecord, StoreError,
};

/// The quarantine store's directory name, under the SSD primary directory.
pub const QUARANTINE_DIRNAME: &str = "quarantine";
/// Magic header of a run file.
pub const RUN_MAGIC: &str = "ATP-QUARANTINE-RUN";
/// The run-file schema version this build writes and reads.
pub const RUN_SCHEMA_VERSION: i64 = 1;
/// The extension that marks a published run file.
pub const RUN_EXTENSION: &str = "qrun";
/// Scratch files start with this prefix and never end in [`RUN_EXTENSION`], so a reader can never
/// mistake a half-written run for a published one.
const SCRATCH_PREFIX: &str = ".scratch-";
/// The most run files one [`QuarantineStore::read_latest`] call will parse, whatever it is asked.
pub const MAX_RUNS_READ: usize = 200;
/// A run id's sequence part is zero-padded to at least this many digits, and grows past them
/// (sequence 1,000,000 is seven). [`parse_run_id`] accepts every width the writer can emit.
const RUN_SEQ_MIN_WIDTH: usize = 6;

static RUN_SEQ: AtomicU64 = AtomicU64::new(0);
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// What happened to the batch's primary (SSD) write, as far as this run record knows.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PrimaryWrite {
    /// Recorded before the primary write began. Still `Pending` on disk means the outcome is
    /// UNKNOWN: the process stopped between the two writes of this run.
    Pending,
    /// The valid subset was committed to the SSD primary. `records` is how many were handed to it.
    Written { records: u64 },
    /// The batch was refused before any primary write (the fail-closed tiered path).
    Refused,
    /// The primary write was attempted and failed; nothing from this batch is in primary storage.
    Failed,
}

impl PrimaryWrite {
    /// The stable wire label (`PENDING` / `WRITTEN` / `REFUSED` / `FAILED`).
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "PENDING",
            Self::Written { .. } => "WRITTEN",
            Self::Refused => "REFUSED",
            Self::Failed => "FAILED",
        }
    }
}

/// One quarantined record: the rejected payload in full, why, from where.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantinedRecord {
    /// The SYS-77 rule it failed.
    pub reason: QuarantineReason,
    /// The ERR-5 source (the dataset-kind tag).
    pub source: String,
    /// The ERR-5 SHA-256 over the normalized record bytes.
    pub record_hash: String,
    /// The rejected record itself.
    pub record: MarketDataRecord,
}

/// One ingestion run as recorded in the quarantine store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineRun {
    /// Sortable, unique run identity; also the file stem.
    pub run_id: String,
    /// Wall-clock instant the run was first recorded (epoch milliseconds).
    pub recorded_at_millis: u64,
    /// The ERR-5 observation instant the caller validated the batch at (epoch seconds).
    pub observed_at_seconds: u64,
    /// Records submitted in the batch.
    pub records_in: u64,
    /// What happened to the primary write.
    pub primary: PrimaryWrite,
    /// Every quarantined record, in batch order.
    pub entries: Vec<QuarantinedRecord>,
}

impl QuarantineRun {
    /// Total records quarantined in this run.
    pub fn quarantined_total(&self) -> u64 {
        self.entries.len() as u64
    }

    /// Records quarantined for `reason` in this run.
    pub fn count(&self, reason: QuarantineReason) -> u64 {
        self.entries.iter().filter(|e| e.reason == reason).count() as u64
    }

    /// Every SYS-77 reason with its count, in the canonical [`ALL_QUARANTINE_REASONS`] order
    /// (zero-count reasons included, so a reader can see every rule was applied).
    pub fn per_reason(&self) -> Vec<(QuarantineReason, u64)> {
        ALL_QUARANTINE_REASONS
            .iter()
            .map(|&reason| (reason, self.count(reason)))
            .collect()
    }
}

/// One run as read back: either the run, or the fact that its file could not be read.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunRead {
    /// The run file parsed and its checksum held.
    Readable(QuarantineRun),
    /// A published run file exists but could not be read. Reported in its place — an unreadable
    /// run is never treated as a clean one.
    Unreadable {
        /// The run id taken from the file name.
        run_id: String,
        /// Why it could not be read.
        error: QuarantineStoreError,
    },
}

/// The newest runs in the store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LatestRuns {
    /// How many published run files the store holds in total.
    pub total_runs: usize,
    /// The newest runs, newest first; at most the requested limit (and [`MAX_RUNS_READ`]).
    pub runs: Vec<RunRead>,
}

/// Fail-closed quarantine-store errors. Carries no broker/vendor identifiers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum QuarantineStoreError {
    /// The SSD primary directory the store lives under does not exist (unmounted or
    /// misconfigured). Not an empty history.
    PrimaryDirectoryMissing,
    /// A filesystem operation failed. `context` names it.
    Io { context: &'static str },
    /// A run file was malformed. `context` names where parsing failed.
    Corrupt { context: &'static str },
    /// A run file's checksum did not match its body.
    ChecksumMismatch,
    /// A run file declares a schema version this build does not read.
    UnknownSchemaVersion { found: i64 },
}

impl std::fmt::Display for QuarantineStoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::PrimaryDirectoryMissing => write!(
                f,
                "quarantine store unavailable: the SSD primary directory is missing"
            ),
            Self::Io { context } => write!(f, "quarantine store I/O failure: {context}"),
            Self::Corrupt { context } => write!(f, "corrupt quarantine run: {context}"),
            Self::ChecksumMismatch => write!(f, "quarantine run checksum mismatch"),
            Self::UnknownSchemaVersion { found } => {
                write!(f, "unknown quarantine run schema version: {found}")
            }
        }
    }
}

impl std::error::Error for QuarantineStoreError {}

impl From<StoreError> for QuarantineStoreError {
    fn from(err: StoreError) -> Self {
        match err {
            StoreError::CorruptRecord { context } => Self::Corrupt { context },
            StoreError::ChecksumMismatch => Self::ChecksumMismatch,
            StoreError::UnknownSchemaVersion { found } => Self::UnknownSchemaVersion { found },
            StoreError::Io { context } => Self::Io { context },
            StoreError::InconsistentField { context } => Self::Corrupt { context },
            StoreError::DuplicateKey { .. } => Self::Corrupt {
                context: "duplicate key",
            },
            StoreError::ConflictingContent { .. } => Self::Corrupt {
                context: "conflicting content",
            },
            StoreError::Locked => Self::Io {
                context: "store locked",
            },
        }
    }
}

/// The quarantine store beside one SSD primary directory.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct QuarantineStore {
    ssd_dir: PathBuf,
    dir: PathBuf,
}

impl QuarantineStore {
    /// The quarantine store that guards the SSD primary at `ssd_dir` (`<ssd_dir>/quarantine`).
    pub fn beside(ssd_dir: &Path) -> Self {
        Self {
            ssd_dir: ssd_dir.to_path_buf(),
            dir: ssd_dir.join(QUARANTINE_DIRNAME),
        }
    }

    /// The store's directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// Record a new run, published as `primary` (normally [`PrimaryWrite::Pending`], or
    /// [`PrimaryWrite::Refused`] for a batch refused outright). Returns the handle that records the
    /// outcome. Creates the store directory (and the SSD directory above it) exactly as
    /// [`TieredStore::ingest`](crate::tiering::TieredStore::ingest) creates the SSD primary, so the
    /// first batch into a fresh tier is recorded like any other.
    pub(crate) fn begin(
        &self,
        observed_at_seconds: u64,
        records_in: u64,
        entries: Vec<QuarantinedRecord>,
        primary: PrimaryWrite,
    ) -> Result<RunHandle, QuarantineStoreError> {
        crate::store::create_dir_all_durably(&self.dir)
            .map_err(|err| io_err("create quarantine directory", &err))?;
        let recorded_at_millis = now_millis()?;
        let run_id = format!(
            "{recorded_at_millis:020}-{:010}-{:0width$}",
            std::process::id(),
            RUN_SEQ.fetch_add(1, Ordering::Relaxed),
            width = RUN_SEQ_MIN_WIDTH,
        );
        let run = QuarantineRun {
            run_id,
            recorded_at_millis,
            observed_at_seconds,
            records_in,
            primary,
            entries,
        };
        let handle = RunHandle {
            dir: self.dir.clone(),
            run,
        };
        handle.publish()?;
        Ok(handle)
    }

    /// Record a batch refused before any primary write — with every quarantined record collected
    /// up to the refusal — and return the error the caller reports. Every exit from a write path
    /// after its batch arrives goes through here or through [`begin`](Self::begin), so a reject is
    /// never dropped with the error. If the refusal itself cannot be recorded, that is the error.
    pub(crate) fn refuse(
        &self,
        observed_at_seconds: u64,
        records_in: u64,
        entries: Vec<QuarantinedRecord>,
        error: crate::MarketIngestError,
    ) -> crate::MarketIngestError {
        match self.begin(
            observed_at_seconds,
            records_in,
            entries,
            PrimaryWrite::Refused,
        ) {
            Ok(_) => error,
            Err(unrecorded) => crate::MarketIngestError::Quarantine(unrecorded),
        }
    }

    /// The newest `limit` runs (capped at [`MAX_RUNS_READ`]), newest first, plus the total count.
    ///
    /// - SSD primary directory missing → [`QuarantineStoreError::PrimaryDirectoryMissing`].
    /// - SSD present, no quarantine directory yet → no runs (nothing has been ingested here).
    /// - A run file that cannot be read → [`RunRead::Unreadable`] in its place.
    pub fn read_latest(&self, limit: usize) -> Result<LatestRuns, QuarantineStoreError> {
        let mut ids = self.run_ids()?;
        ids.reverse();
        let total_runs = ids.len();
        let runs = ids
            .into_iter()
            .take(limit.min(MAX_RUNS_READ))
            .map(|id| self.read(&id))
            .collect();
        Ok(LatestRuns { total_runs, runs })
    }

    /// Every published run id, OLDEST first. Lists names only (O(runs)); reads no run file.
    ///
    /// Same tri-state as [`read_latest`](Self::read_latest): a missing SSD primary is an error,
    /// an SSD with no quarantine directory yet has no runs.
    pub fn run_ids(&self) -> Result<Vec<String>, QuarantineStoreError> {
        if !self.ssd_dir.is_dir() {
            return Err(QuarantineStoreError::PrimaryDirectoryMissing);
        }
        let listing = match fs::read_dir(&self.dir) {
            Ok(listing) => listing,
            Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(err) => return Err(io_err("list quarantine directory", &err)),
        };
        let mut stems: Vec<String> = Vec::new();
        for entry in listing {
            let entry = entry.map_err(|err| io_err("list quarantine directory", &err))?;
            let name = entry.file_name();
            let Some(name) = name.to_str() else { continue };
            if name.starts_with(SCRATCH_PREFIX) {
                continue;
            }
            if let Some(stem) = name.strip_suffix(&format!(".{RUN_EXTENSION}")) {
                stems.push(stem.to_string());
            }
        }
        // Oldest first, by the id's numbers: name order is not run order once a sequence outgrows
        // its padding ("1000000" sorts before "999999"). A stem that is not a run id sorts last.
        stems.sort_unstable_by(|a, b| {
            let (ka, kb) = (run_id_parts(a), run_id_parts(b));
            (ka.is_none(), ka, a).cmp(&(kb.is_none(), kb, b))
        });
        Ok(stems)
    }

    /// One run by id: the run, or the fact that it could not be read.
    pub fn read(&self, run_id: &str) -> RunRead {
        match self.read_run(run_id) {
            Ok(run) => RunRead::Readable(run),
            Err(error) => RunRead::Unreadable {
                run_id: run_id.to_string(),
                error,
            },
        }
    }

    fn read_run(&self, stem: &str) -> Result<QuarantineRun, QuarantineStoreError> {
        let path = self.dir.join(format!("{stem}.{RUN_EXTENSION}"));
        let text = fs::read_to_string(&path).map_err(|err| io_err("read quarantine run", &err))?;
        let run = deserialize(&text)?;
        // The file name is the run's identity; a body that names another run is not this run.
        if run.run_id != stem {
            return Err(QuarantineStoreError::Corrupt {
                context: "run id does not match its file name",
            });
        }
        Ok(run)
    }
}

/// `text` if it is exactly a run id this store mints — `<20-digit millis>-<10-digit pid>-<seq>`,
/// the seq zero-padded to at least six digits — else `None`. Consumers that carry a run id in free
/// text (the operator notification's summary) parse it back through this, so the format has one
/// owner.
pub fn parse_run_id(text: &str) -> Option<&str> {
    run_id_parts(text).map(|_| text)
}

/// A run id's `(millis, pid, seq)`, if `text` is exactly one the writer can emit: each part in
/// range for its type, and one spelling per number (a seq wider than its padding has no leading
/// zero).
fn run_id_parts(text: &str) -> Option<(u64, u32, u64)> {
    fn digits(part: &str) -> Option<&str> {
        (!part.is_empty() && part.bytes().all(|b| b.is_ascii_digit())).then_some(part)
    }
    let mut parts = text.split('-');
    let millis = digits(parts.next()?).filter(|p| p.len() == 20)?;
    let pid = digits(parts.next()?).filter(|p| p.len() == 10)?;
    let seq = digits(parts.next()?).filter(|p| {
        p.len() == RUN_SEQ_MIN_WIDTH || (p.len() > RUN_SEQ_MIN_WIDTH && !p.starts_with('0'))
    })?;
    if parts.next().is_some() {
        return None;
    }
    Some((millis.parse().ok()?, pid.parse().ok()?, seq.parse().ok()?))
}

/// Which quarantine run recorded an ingestion batch, and whether its outcome reached disk.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunReceipt {
    /// The run's id (its file stem under the quarantine directory).
    pub run_id: String,
    /// `false` when the primary write finished but re-publishing the run with that outcome failed:
    /// the run stays on disk as `PENDING` (outcome unknown) and the caller must say so.
    pub finalized: bool,
}

/// An event sink that remembers the reason of the last event it saw, then forwards the event.
///
/// The ERR-5 gate emits exactly one event per quarantined record and returns an error that names
/// the record but not the rule. Wrapping the caller's sink in this capture is how a write path
/// learns WHY each record was quarantined without a second validation pass (which a stateful
/// validator would answer differently) and without changing the gate.
pub(crate) struct ReasonCapture<'a, S> {
    inner: &'a S,
    last: std::cell::Cell<Option<QuarantineReason>>,
}

impl<'a, S: crate::IngestionValidationEventSink> ReasonCapture<'a, S> {
    pub(crate) fn new(inner: &'a S) -> Self {
        Self {
            inner,
            last: std::cell::Cell::new(None),
        }
    }

    /// The reason of the event emitted since the last call, if any.
    pub(crate) fn take(&self) -> Option<QuarantineReason> {
        self.last.take()
    }
}

impl<S: crate::IngestionValidationEventSink> crate::IngestionValidationEventSink
    for ReasonCapture<'_, S>
{
    fn record(&self, event: atp_types::IngestionValidationEvent) {
        self.last.set(Some(event.reason));
        self.inner.record(event);
    }
}

/// A recorded run whose outcome is still to be written.
#[derive(Debug)]
pub(crate) struct RunHandle {
    dir: PathBuf,
    run: QuarantineRun,
}

impl RunHandle {
    /// The run as currently recorded.
    #[cfg(test)]
    pub(crate) fn run(&self) -> &QuarantineRun {
        &self.run
    }

    /// Re-publish the run with its final primary-write outcome. On failure the run stays on disk
    /// as previously published (normally `Pending`: outcome unknown).
    pub(crate) fn finish(
        mut self,
        primary: PrimaryWrite,
    ) -> Result<QuarantineRun, QuarantineStoreError> {
        self.run.primary = primary;
        self.publish()?;
        Ok(self.run)
    }

    /// Record the conflicting re-ingests the tier found under its writer lock: each joins this run
    /// as a `DUPLICATE_RECORD` entry with its full payload, one ERR-5 event is emitted per conflict
    /// (so a counts sink agrees with the run), and the run is re-published — still `PENDING` —
    /// before the tier saves anything. The tier writes nothing if this fails.
    pub(crate) fn record_conflicts<S: crate::IngestionValidationEventSink>(
        &mut self,
        conflicts: &[MarketDataRecord],
        events: &S,
    ) -> Result<(), QuarantineStoreError> {
        let reason = QuarantineReason::DuplicateRecord;
        for record in conflicts {
            let submission = record.ingestion_submission();
            events.record(atp_types::IngestionValidationEvent {
                state: atp_types::RecordValidationOutcome::Quarantined(reason),
                reason,
                source: submission.source.clone(),
                record_hash: submission.record_hash.clone(),
                observed_at_seconds: self.run.observed_at_seconds,
            });
            self.run.entries.push(QuarantinedRecord {
                reason,
                source: submission.source,
                record_hash: submission.record_hash,
                record: record.clone(),
            });
        }
        self.publish()
    }

    /// Finish a run whose batch the tier refused for conflicting re-ingests (already recorded by
    /// [`record_conflicts`](Self::record_conflicts)) as `REFUSED`. Returns the error the caller
    /// reports — the store's conflict error for the first conflict — or the quarantine error if the
    /// run cannot be finished.
    pub(crate) fn refuse_conflicts(
        self,
        conflicts: &[MarketDataRecord],
    ) -> crate::MarketIngestError {
        let error = match conflicts.first() {
            Some(first) => crate::store::MarketDataStore::conflict_error(first),
            None => StoreError::InconsistentField {
                context: "the tier refused a batch without naming a conflict",
            },
        };
        match self.finish(PrimaryWrite::Refused) {
            Ok(_) => crate::MarketIngestError::Tier(crate::tiering::TierError::Ssd(error)),
            Err(unrecorded) => crate::MarketIngestError::Quarantine(unrecorded),
        }
    }

    /// Record a failed primary write. `FAILED` means nothing from the batch is in primary storage,
    /// so it is recorded only when the error proves that; an error that may have followed the
    /// store's atomic rename leaves the run `PENDING` — outcome unknown, never a false "failed".
    pub(crate) fn primary_write_failed(self, error: &crate::tiering::TierError) {
        if !error.may_have_published() {
            let _ = self.finish(PrimaryWrite::Failed);
        }
    }

    /// [`finish`](Self::finish) for a write path that has already committed (or failed) its primary
    /// write and must report that outcome whatever happens here: the receipt says whether the run
    /// on disk carries it.
    pub(crate) fn finish_receipt(self, primary: PrimaryWrite) -> RunReceipt {
        let run_id = self.run.run_id.clone();
        let finalized = self.finish(primary).is_ok();
        RunReceipt { run_id, finalized }
    }

    /// Atomically (re)write the run file: unique scratch, fsync, rename, directory fsync.
    fn publish(&self) -> Result<(), QuarantineStoreError> {
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let scratch_path = self
            .dir
            .join(format!("{SCRATCH_PREFIX}{}-{seq}", std::process::id()));
        let final_path = self
            .dir
            .join(format!("{}.{RUN_EXTENSION}", self.run.run_id));
        let mut scratch = fs::File::create(&scratch_path)
            .map_err(|err| io_err("create quarantine scratch file", &err))?;
        if let Err(err) = scratch
            .write_all(serialize(&self.run).as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&scratch_path);
            return Err(io_err("write quarantine scratch file", &err));
        }
        drop(scratch);
        fs::rename(&scratch_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&scratch_path);
            io_err("publish quarantine run", &err)
        })?;
        fs::File::open(&self.dir)
            .and_then(|dir| dir.sync_all())
            .map_err(|err| io_err("sync quarantine directory", &err))
    }
}

fn now_millis() -> Result<u64, QuarantineStoreError> {
    let elapsed =
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| QuarantineStoreError::Io {
                context: "system clock is before the epoch",
            })?;
    u64::try_from(elapsed.as_millis()).map_err(|_| QuarantineStoreError::Io {
        context: "system clock out of range",
    })
}

fn io_err(context: &'static str, _err: &io::Error) -> QuarantineStoreError {
    QuarantineStoreError::Io { context }
}

// --------------------------------------------------------------------------- //
// Codec — the market-data store's dependency-free text codec, with its own magic.
// --------------------------------------------------------------------------- //

fn serialize(run: &QuarantineRun) -> String {
    let mut body = String::new();
    push_i128(&mut body, i128::from(RUN_SCHEMA_VERSION));
    push_str(&mut body, &run.run_id);
    push_i128(&mut body, i128::from(run.recorded_at_millis));
    push_i128(&mut body, i128::from(run.observed_at_seconds));
    push_i128(&mut body, i128::from(run.records_in));
    push_line(&mut body, run.primary.as_str());
    if let PrimaryWrite::Written { records } = run.primary {
        push_i128(&mut body, i128::from(records));
    }
    push_count(&mut body, run.entries.len());
    for entry in &run.entries {
        push_str(&mut body, entry.reason.as_str());
        push_str(&mut body, &entry.source);
        push_str(&mut body, &entry.record_hash);
        encode_record(&mut body, &entry.record);
    }
    let mut out = String::with_capacity(body.len() + RUN_MAGIC.len() + 32);
    push_line(&mut out, RUN_MAGIC);
    push_i128(&mut out, i128::from(checksum(body.as_bytes())));
    out.push_str(&body);
    out
}

fn deserialize(text: &str) -> Result<QuarantineRun, QuarantineStoreError> {
    let mut cursor = Cursor::new(text);
    if cursor.read_line("magic")? != RUN_MAGIC {
        return Err(QuarantineStoreError::Corrupt {
            context: "bad magic header",
        });
    }
    let stored = cursor.read_u64("checksum")?;
    if checksum(cursor.remaining()) != stored {
        return Err(QuarantineStoreError::ChecksumMismatch);
    }
    let version = cursor.read_i64("schema version")?;
    if version != RUN_SCHEMA_VERSION {
        return Err(QuarantineStoreError::UnknownSchemaVersion { found: version });
    }
    let run_id = cursor.read_str("run id")?;
    let recorded_at_millis = cursor.read_u64("recorded_at_millis")?;
    let observed_at_seconds = cursor.read_u64("observed_at_seconds")?;
    let records_in = cursor.read_u64("records_in")?;
    let primary = match cursor.read_line("primary write")? {
        "PENDING" => PrimaryWrite::Pending,
        "WRITTEN" => PrimaryWrite::Written {
            records: cursor.read_u64("written records")?,
        },
        "REFUSED" => PrimaryWrite::Refused,
        "FAILED" => PrimaryWrite::Failed,
        _ => {
            return Err(QuarantineStoreError::Corrupt {
                context: "unknown primary write outcome",
            })
        }
    };
    // The count is untrusted: grow incrementally so a corrupt count exhausts the cursor rather
    // than driving an allocation.
    let entry_count = cursor.read_count("entry count")?;
    let mut entries = Vec::new();
    for _ in 0..entry_count {
        let reason_label = cursor.read_str("reason")?;
        let reason = ALL_QUARANTINE_REASONS
            .iter()
            .copied()
            .find(|r| r.as_str() == reason_label)
            .ok_or(QuarantineStoreError::Corrupt {
                context: "unknown quarantine reason",
            })?;
        let source = cursor.read_str("source")?;
        let record_hash = cursor.read_str("record hash")?;
        let record = decode_record(&mut cursor)?;
        entries.push(QuarantinedRecord {
            reason,
            source,
            record_hash,
            record,
        });
    }
    cursor.expect_end()?;
    if entries.len() as u64 > records_in {
        return Err(QuarantineStoreError::Corrupt {
            context: "more quarantined records than records in",
        });
    }
    Ok(QuarantineRun {
        run_id,
        recorded_at_millis,
        observed_at_seconds,
        records_in,
        primary,
        entries,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ingestion_validation::mixed_validation_fixture;

    fn scratch_dir(tag: &str) -> PathBuf {
        // Unique per CALL: tests run in parallel, and two that share a tag must not share a
        // directory (line!() here would be this function's line, the same for every caller).
        static NEXT: AtomicU64 = AtomicU64::new(0);
        let dir = std::env::temp_dir().join(format!(
            "atp-qstore-{tag}-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn entry(reason: QuarantineReason) -> QuarantinedRecord {
        let record = mixed_validation_fixture(1_700_000_000).remove(0);
        let submission = record.ingestion_submission();
        QuarantinedRecord {
            reason,
            source: submission.source,
            record_hash: submission.record_hash,
            record,
        }
    }

    #[test]
    fn a_run_round_trips_with_its_outcome() {
        let ssd = scratch_dir("roundtrip");
        let store = QuarantineStore::beside(&ssd);
        let entries = vec![
            entry(QuarantineReason::RangeViolation),
            entry(QuarantineReason::RangeViolation),
            entry(QuarantineReason::DuplicateRecord),
        ];
        let handle = store
            .begin(7, 10, entries.clone(), PrimaryWrite::Pending)
            .unwrap();
        let pending = store.read_latest(5).unwrap();
        assert_eq!(pending.total_runs, 1);
        match &pending.runs[0] {
            RunRead::Readable(run) => assert_eq!(run.primary, PrimaryWrite::Pending),
            other => panic!("expected a readable pending run, got {other:?}"),
        }
        let finished = handle.finish(PrimaryWrite::Written { records: 7 }).unwrap();
        let latest = store.read_latest(5).unwrap();
        assert_eq!(latest.runs, vec![RunRead::Readable(finished.clone())]);
        assert_eq!(finished.entries, entries);
        assert_eq!(finished.quarantined_total(), 3);
        assert_eq!(finished.count(QuarantineReason::RangeViolation), 2);
        assert_eq!(finished.count(QuarantineReason::NegativeVolume), 0);
        assert_eq!(finished.per_reason().len(), 6);
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn newest_first_and_bounded_by_the_limit() {
        let ssd = scratch_dir("order");
        let store = QuarantineStore::beside(&ssd);
        let ids: Vec<String> = (0..3)
            .map(|i| {
                store
                    .begin(i, 1, Vec::new(), PrimaryWrite::Refused)
                    .unwrap()
                    .run()
                    .run_id
                    .clone()
            })
            .collect();
        let latest = store.read_latest(2).unwrap();
        assert_eq!(latest.total_runs, 3);
        let got: Vec<&str> = latest
            .runs
            .iter()
            .map(|r| match r {
                RunRead::Readable(run) => run.run_id.as_str(),
                RunRead::Unreadable { run_id, .. } => run_id.as_str(),
            })
            .collect();
        assert_eq!(got, vec![ids[2].as_str(), ids[1].as_str()]);
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn an_unreadable_run_is_reported_in_place_never_skipped() {
        let ssd = scratch_dir("corrupt");
        let store = QuarantineStore::beside(&ssd);
        let run_id = store
            .begin(
                1,
                2,
                vec![entry(QuarantineReason::NegativeVolume)],
                PrimaryWrite::Refused,
            )
            .unwrap()
            .run()
            .run_id
            .clone();
        let path = store.dir().join(format!("{run_id}.{RUN_EXTENSION}"));
        let mut text = fs::read_to_string(&path).unwrap();
        text = text.replacen("NEGATIVE_VOLUME", "NEGATIVE_VOLUMF", 1);
        fs::write(&path, text).unwrap();
        let latest = store.read_latest(5).unwrap();
        assert_eq!(latest.total_runs, 1);
        assert_eq!(
            latest.runs,
            vec![RunRead::Unreadable {
                run_id,
                error: QuarantineStoreError::ChecksumMismatch
            }]
        );
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn a_run_renamed_to_another_id_is_not_that_run() {
        let ssd = scratch_dir("rename");
        let store = QuarantineStore::beside(&ssd);
        let run_id = store
            .begin(1, 1, Vec::new(), PrimaryWrite::Refused)
            .unwrap()
            .run()
            .run_id
            .clone();
        let from = store.dir().join(format!("{run_id}.{RUN_EXTENSION}"));
        let to = store
            .dir()
            .join(format!("99999999999999999999-x.{RUN_EXTENSION}"));
        fs::rename(from, to).unwrap();
        let latest = store.read_latest(5).unwrap();
        assert!(matches!(
            &latest.runs[0],
            RunRead::Unreadable {
                error: QuarantineStoreError::Corrupt { .. },
                ..
            }
        ));
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn reading_a_missing_primary_is_an_error_and_a_fresh_primary_has_no_runs() {
        let ssd = scratch_dir("fresh");
        let store = QuarantineStore::beside(&ssd);
        assert_eq!(
            store.read_latest(5).unwrap(),
            LatestRuns {
                total_runs: 0,
                runs: Vec::new()
            }
        );
        let gone = QuarantineStore::beside(&ssd.join("not-mounted"));
        assert_eq!(
            gone.read_latest(5),
            Err(QuarantineStoreError::PrimaryDirectoryMissing)
        );
        // A write creates the store the way the tier creates the SSD primary on first ingest.
        gone.begin(1, 1, Vec::new(), PrimaryWrite::Refused).unwrap();
        assert_eq!(gone.read_latest(5).unwrap().total_runs, 1);
        let _ = fs::remove_dir_all(&ssd);
    }

    fn primary_after(error: crate::tiering::TierError) -> PrimaryWrite {
        let ssd = scratch_dir("after");
        let store = QuarantineStore::beside(&ssd);
        store
            .begin(1, 1, Vec::new(), PrimaryWrite::Pending)
            .unwrap()
            .primary_write_failed(&error);
        let latest = store.read_latest(1).unwrap();
        let _ = fs::remove_dir_all(&ssd);
        match &latest.runs[0] {
            RunRead::Readable(run) => run.primary,
            other => panic!("expected a readable run, got {other:?}"),
        }
    }

    #[test]
    fn a_failure_that_may_follow_the_rename_stays_pending_never_failed() {
        use crate::store::{CTX_OPEN_STORE_DIR, CTX_SYNC_STORE_DIR};
        use crate::tiering::TierError;
        for context in [CTX_OPEN_STORE_DIR, CTX_SYNC_STORE_DIR] {
            let error = TierError::Ssd(StoreError::Io { context });
            assert_eq!(primary_after(error), PrimaryWrite::Pending, "{context}");
        }
        // NAS work only starts after the SSD primary committed.
        let nas = TierError::Nas(StoreError::Io {
            context: "anything",
        });
        assert_eq!(primary_after(nas), PrimaryWrite::Pending);
    }

    #[test]
    fn a_failure_before_the_rename_is_recorded_failed() {
        use crate::tiering::TierError;
        for error in [
            StoreError::Locked,
            StoreError::ChecksumMismatch,
            StoreError::Io {
                context: "publish store file",
            },
            StoreError::Io {
                context: "write store scratch file",
            },
        ] {
            assert_eq!(
                primary_after(TierError::Ssd(error.clone())),
                PrimaryWrite::Failed,
                "{error}"
            );
        }
    }

    #[test]
    fn the_store_directory_is_created_with_its_missing_parents() {
        let base = scratch_dir("nested");
        let ssd = base.join("a").join("b");
        let store = QuarantineStore::beside(&ssd);
        store
            .begin(1, 1, Vec::new(), PrimaryWrite::Refused)
            .unwrap();
        assert!(store.dir().is_dir());
        assert_eq!(store.read_latest(1).unwrap().total_runs, 1);
        // A second run into the existing directory is just another run.
        store
            .begin(1, 1, Vec::new(), PrimaryWrite::Refused)
            .unwrap();
        assert_eq!(store.read_latest(5).unwrap().total_runs, 2);
        let _ = fs::remove_dir_all(&base);
    }

    #[test]
    fn every_ancestor_is_synced_on_every_call_even_for_a_relative_path() {
        use crate::store::{ancestors_to_sync, create_dir_all_durably};
        // Relative to the test's working directory (the crate root): the parent of a fresh
        // relative directory must be a real directory, never the empty path.
        let relative = PathBuf::from(format!("target/atp-qstore-rel-{}", std::process::id()));
        let _ = fs::remove_dir_all(&relative);
        let nested = relative.join("ssd").join(QUARANTINE_DIRNAME);
        create_dir_all_durably(&nested).unwrap();
        let synced = ancestors_to_sync(&nested).unwrap();
        let cwd = std::env::current_dir().unwrap().canonicalize().unwrap();
        assert!(synced
            .iter()
            .all(|p| p.is_absolute() && !p.as_os_str().is_empty()));
        assert_eq!(synced[0], nested.canonicalize().unwrap().parent().unwrap());
        assert!(
            synced.contains(&cwd),
            "the working directory holds the new top-level entry"
        );
        assert!(
            synced.contains(&PathBuf::from("/")),
            "the chain reaches the root"
        );
        // A retry against the now-existing directory re-syncs the SAME chain: existence is not
        // taken as proof that an earlier call's parent fsync succeeded.
        create_dir_all_durably(&nested).unwrap();
        assert_eq!(ancestors_to_sync(&nested).unwrap(), synced);
        let _ = fs::remove_dir_all(&relative);
    }

    #[test]
    fn a_minted_run_id_parses_and_nothing_else_does() {
        let ssd = scratch_dir("ids");
        let store = QuarantineStore::beside(&ssd);
        let handle = store
            .begin(1, 1, Vec::new(), PrimaryWrite::Refused)
            .unwrap();
        let id = handle.run().run_id.clone();
        assert_eq!(parse_run_id(&id), Some(id.as_str()));
        for bad in [
            "",
            "00000001792383193180-0000008791",
            "00000001792383193180-0000008791-000000-1",
            "0000000179238319318x-0000008791-000000",
            "1792383193180-8791-0",
            " 00000001792383193180-0000008791-000000",
            // Review L2 r4: one spelling per number, and every part in range for its type.
            "00000001792383193180-0000008791-0999999",
            "00000001792383193180-0000008791-00000",
            "00000001792383193180-0000008791-",
            "00000001792383193180-0000008791-18446744073709551616",
            "00000001792383193180-4294967296-000000",
            "99999999999999999999-0000008791-000000",
        ] {
            assert_eq!(parse_run_id(bad), None, "{bad:?}");
        }
        for good in [
            "00000001792383193180-0000008791-999999",
            "00000001792383193180-0000008791-1000000",
            "00000001792383193180-0000008791-18446744073709551615",
            "18446744073709551615-4294967295-000000",
        ] {
            assert_eq!(parse_run_id(good), Some(good), "{good:?}");
        }
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn a_sequence_past_its_padding_is_minted_parsed_and_read() {
        // Review L2 r4: the writer's `{:06}` is a MINIMUM width. Push the shared sequence past it
        // and mint through the real writer: the ids must still parse and their runs still read.
        RUN_SEQ.fetch_max(999_999, Ordering::Relaxed);
        let ssd = scratch_dir("wide-seq");
        let store = QuarantineStore::beside(&ssd);
        let ids: Vec<String> = (0..2)
            .map(|_| {
                let handle = store
                    .begin(1, 1, Vec::new(), PrimaryWrite::Refused)
                    .unwrap();
                handle.run().run_id.clone()
            })
            .collect();
        let seq = ids[1].rsplit('-').next().unwrap();
        assert!(seq.len() > RUN_SEQ_MIN_WIDTH, "{}", ids[1]);
        for id in &ids {
            assert_eq!(parse_run_id(id), Some(id.as_str()));
            assert!(matches!(store.read(id), RunRead::Readable(_)), "{id}");
        }
        assert_eq!(store.run_ids().unwrap(), ids);
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn run_ids_are_in_run_order_past_the_sequence_padding() {
        // Name order is not run order once a sequence outgrows its padding.
        let ssd = scratch_dir("seq-order");
        let dir = ssd.join(QUARANTINE_DIRNAME);
        fs::create_dir_all(&dir).unwrap();
        let names = [
            "00000001792383193180-0000008791-1000000",
            "00000001792383193180-0000008791-999999",
            "00000001792383193179-0000008791-1000001",
            "not-a-run",
        ];
        for name in names {
            fs::write(dir.join(format!("{name}.{RUN_EXTENSION}")), "").unwrap();
        }
        let store = QuarantineStore::beside(&ssd);
        assert_eq!(
            store.run_ids().unwrap(),
            vec![names[2], names[1], names[0], names[3]]
        );
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn scratch_files_are_never_read_as_runs() {
        let ssd = scratch_dir("scratch");
        let store = QuarantineStore::beside(&ssd);
        fs::create_dir_all(store.dir()).unwrap();
        fs::write(store.dir().join(format!("{SCRATCH_PREFIX}1-1")), "partial").unwrap();
        assert_eq!(store.read_latest(5).unwrap().total_runs, 0);
        let _ = fs::remove_dir_all(&ssd);
    }

    #[test]
    fn a_corrupt_entry_count_cannot_claim_more_rejects_than_records_in() {
        let ssd = scratch_dir("count");
        let store = QuarantineStore::beside(&ssd);
        let mut handle = store
            .begin(
                1,
                1,
                vec![entry(QuarantineReason::RangeViolation)],
                PrimaryWrite::Refused,
            )
            .unwrap();
        handle
            .run
            .entries
            .push(entry(QuarantineReason::RangeViolation));
        handle.publish().unwrap();
        let latest = store.read_latest(1).unwrap();
        assert!(matches!(
            &latest.runs[0],
            RunRead::Unreadable {
                error: QuarantineStoreError::Corrupt { .. },
                ..
            }
        ));
        let _ = fs::remove_dir_all(&ssd);
    }
}
