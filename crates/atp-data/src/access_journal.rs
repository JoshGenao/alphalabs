//! # SRS-DATA-010 access journal — the durable recency producer the eviction policy reads
//!
//! SYS-69 requires the SSD eviction policy to **not evict data accessed within a configurable recency
//! window (default 24 h) by a running backtest or factor-pipeline job**. That clause has no backing
//! data in the tier/cold-read substrate — reads there are pure and record no access. This module ships
//! the missing substrate: an append-only **access journal** that a read path records into, and a
//! recency query the [`crate::eviction`] policy consumes.
//!
//! ## The two producers write, the one consumer reads
//! - **Write side (backtest / factor-pipeline read paths):** an [`AccessRecorder`] is injected into the
//!   instrumented read wrappers (`atp-simulation`'s bar source, `atp-factor-pipeline`'s input
//!   assembler). Each served read records `(symbol, access_ts)` under a [`JobRef`]. The default
//!   [`NoopRecorder`] means an un-instrumented read behaves **byte-identically** — recording is opt-in.
//! - **Read side (eviction policy):** [`AccessJournal::recent`] returns, per symbol, the most-recent
//!   access within the window, optionally filtered to a set of *currently running* jobs.
//!
//! ## Safety asymmetry (deliberate, load-bearing)
//! - **Writes fail open.** A journal-write error must NEVER break a backtest or factor read — the read
//!   result is authoritative, the journal is an optimisation. [`AccessJournal::record`] is infallible
//!   and best-effort: a create/append failure is swallowed (the access simply goes unrecorded, which at
//!   worst under-protects a datum the operator can re-touch). Recording is a pure side effect that never
//!   changes what a read returns, so the DATA-007 point-in-time and closed-green read semantics are
//!   preserved exactly.
//! - **Reads fail closed.** [`AccessJournal::recent`] distinguishes *absent/empty* (benign — no recency
//!   protection, eviction proceeds) from *present-but-corrupt* (a complete line that does not parse →
//!   [`AccessJournalError::Corrupt`]) so the eviction engine **refuses to evict** rather than evict data
//!   it cannot prove was un-accessed. A torn tail (a final line with no terminating newline, e.g. a
//!   crash mid-append) is tolerated: it is split off at the last newline, never mistaken for corruption.
//!   When the *running-job* set is unknown (`None`), every in-window access is treated as protected
//!   (over-protect) — the fail-closed default until the running-job registry is wired.
//!
//! ## Determinism + discipline
//! No `serde`, no vendor SDK — only `std::fs` and a tab-delimited line codec. `now_ts` is caller-supplied
//! (never a wall-clock read here), so a recency query is deterministic over a fixed journal.

use std::collections::{BTreeMap, BTreeSet};
use std::error::Error;
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

/// The subdirectory, under the SSD primary tier, that holds the access-journal log. A subdirectory
/// (not a file directly in the SSD dir) so it never collides with the `market_data.store` blob or the
/// `cold_read_cache/` cache — the access journal is metadata *about* the store, kept beside it.
pub const ACCESS_JOURNAL_SUBDIR: &str = "access_journal";

/// The append-only log filename inside [`ACCESS_JOURNAL_SUBDIR`].
pub const ACCESS_JOURNAL_FILENAME: &str = "access_journal.log";

/// The journal line-schema version this build WRITES (SRS-DATA-015 / SyRS SYS-66). **Version
/// history:** v1 = `v1\t<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>`.
///
/// The version travels on **every line**, not in a file header. An access journal is an append-only
/// `O_APPEND` log written by any number of concurrent readers-of-data, so there is no moment at which
/// a single writer owns "the start of the file" — a header would race, and two racing headers would
/// land mid-file and read as corruption. A per-line tag has no such moment: a file may legitimately
/// mix pre-SRS-DATA-015 (untagged) lines with tagged ones, and each line is self-describing.
pub const ACCESS_JOURNAL_SCHEMA_VERSION: i64 = 1;

/// The oldest journal line-schema version this build still READS.
pub const MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION: i64 = 1;

/// Prefix marking a line's version field, e.g. `v1`. A **legacy** (pre-SRS-DATA-015) line begins with
/// its bare integer `access_ts`, which can never start with this prefix, so the two forms are
/// unambiguous and no probing heuristic is needed.
const VERSION_TAG_PREFIX: char = 'v';

/// The kind of running job that accessed a datum — the SYS-69 "running backtest or factor pipeline
/// job". Mirrors the orchestrator `WorkloadPriority::{Backtest, FactorPipeline}` running-workload
/// vocabulary (the documented running-job registry seam) without depending on the orchestrator crate.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum JobKind {
    /// A backtest run (`atp-simulation` store-backed bar source).
    Backtest,
    /// A factor-pipeline job (`atp-factor-pipeline` input assembler).
    FactorPipeline,
}

impl JobKind {
    /// The stable lowercase wire tag written into the journal line.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Backtest => "backtest",
            Self::FactorPipeline => "factor-pipeline",
        }
    }

    /// Parse a wire tag back into a [`JobKind`]; `None` for an unknown tag (a corrupt line).
    pub fn from_tag(tag: &str) -> Option<Self> {
        match tag {
            "backtest" => Some(Self::Backtest),
            "factor-pipeline" => Some(Self::FactorPipeline),
            _ => None,
        }
    }
}

/// A validated non-empty job identifier, free of the `\t` / `\n` delimiters the line codec reserves
/// (so a job id can never smuggle extra fields or a line break into the journal).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct JobId(String);

impl JobId {
    /// Build a validated job id, failing closed on an empty (after trim) id or one containing a `\t`
    /// or `\n` (the reserved delimiters).
    pub fn new(id: impl Into<String>) -> Result<Self, AccessJournalError> {
        let id = id.into();
        let trimmed = id.trim();
        if trimmed.is_empty() {
            return Err(AccessJournalError::InvalidField {
                context: "empty job id",
            });
        }
        if trimmed.contains('\t') || trimmed.contains('\n') {
            return Err(AccessJournalError::InvalidField {
                context: "job id contains a reserved delimiter",
            });
        }
        Ok(Self(trimmed.to_string()))
    }

    /// The id string.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for JobId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// A running job's identity: its kind and id. The unit an access is attributed to, and the unit the
/// running-job filter in [`AccessJournal::recent`] scopes recency protection by.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct JobRef {
    /// Whether the job is a backtest or a factor-pipeline job.
    pub kind: JobKind,
    /// The job's stable id (the running-job registry key).
    pub id: JobId,
}

impl JobRef {
    /// Assemble a job reference.
    pub fn new(kind: JobKind, id: JobId) -> Self {
        Self { kind, id }
    }
}

/// Records a data access as a pure side effect. Injected into the instrumented backtest / factor read
/// paths; the default [`NoopRecorder`] records nothing (so an un-instrumented read is byte-identical).
///
/// **Infallible by contract:** a recorder must never surface an error to its caller — a read path that
/// records an access must not fail because recording failed (writes fail open). The durable
/// [`AccessJournal`] honours this by swallowing I/O errors.
pub trait AccessRecorder {
    /// Record that `symbol` was accessed at `access_ts` by `job`. Best-effort; never panics, never
    /// surfaces an error.
    fn record(&self, job: &JobRef, symbol: &str, access_ts: i64);
}

/// The no-op recorder: the default for read paths that are not instrumented. Recording is a no-op, so
/// wiring it costs nothing and changes no observable behaviour.
#[derive(Debug, Clone, Copy, Default)]
pub struct NoopRecorder;

impl AccessRecorder for NoopRecorder {
    fn record(&self, _job: &JobRef, _symbol: &str, _access_ts: i64) {}
}

/// A fail-closed error from an access-journal operation. A `record` never surfaces one (writes fail
/// open); only [`AccessJournal::recent`] and the constructors do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AccessJournalError {
    /// A field supplied to a constructor was invalid (empty, or carrying a reserved delimiter).
    InvalidField {
        /// What was wrong.
        context: &'static str,
    },
    /// A **complete** journal line (one terminated by a newline) failed to parse — a wrong field
    /// count, a non-integer timestamp, or an unknown job kind. This is corruption, not a torn tail, so
    /// the recency read fails closed and the eviction engine refuses to evict.
    Corrupt {
        /// A human-readable reason (never the raw line, which may be large/binary).
        reason: String,
    },
    /// An I/O failure reading an existing journal (a NotFound file / absent directory is NOT this — it
    /// is the benign empty case). Surfaced fail-closed so an unreadable journal never reads as "empty".
    Io {
        /// The operation that failed.
        context: &'static str,
    },
    /// A complete line declared a line-schema version outside
    /// `[MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION, ACCESS_JOURNAL_SCHEMA_VERSION]` — written by a
    /// NEWER build than this one (SRS-DATA-015). Fails closed: a reader that cannot prove it
    /// understands the line's layout must not guess at its fields, because mis-reading an access
    /// record silently under-protects data a running job is using.
    UnsupportedVersion {
        /// The version the line declared.
        found: i64,
    },
}

impl fmt::Display for AccessJournalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidField { context } => write!(f, "invalid access-journal field: {context}"),
            Self::Corrupt { reason } => write!(f, "corrupt access-journal line: {reason}"),
            Self::Io { context } => write!(f, "access-journal I/O error: {context}"),
            Self::UnsupportedVersion { found } => write!(
                f,
                "access-journal line declares schema version {found}, outside the supported range \
                 [{MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION}, {ACCESS_JOURNAL_SCHEMA_VERSION}]"
            ),
        }
    }
}

impl Error for AccessJournalError {}

/// A durable, append-only access journal under `<ssd>/access_journal/access_journal.log`.
///
/// Stateless beyond its directory (the on-disk log is the state), so two handles to the same directory
/// are interchangeable. Each recorded access is one tab-delimited line
/// `v<schema_version>\t<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>\n`; the log is append-only so a
/// running job's accesses accumulate and the newest wins per symbol at read time. A line written
/// before SRS-DATA-015 carries no `v<N>` field and is read as
/// [`MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION`], so an existing journal stays queryable in place
/// with no migration.
#[derive(Debug, Clone)]
pub struct AccessJournal {
    dir: PathBuf,
}

impl AccessJournal {
    /// Wrap the journal directory directly.
    pub fn new(dir: impl Into<PathBuf>) -> Self {
        Self { dir: dir.into() }
    }

    /// The conventional journal directory under an SSD primary tier: `<ssd>/access_journal/`. The same
    /// resolution the eviction engine uses, so the producer and the consumer agree on one location.
    pub fn under_ssd(ssd_dir: impl AsRef<Path>) -> Self {
        Self::new(ssd_dir.as_ref().join(ACCESS_JOURNAL_SUBDIR))
    }

    /// The journal directory.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    /// The append-only log file path.
    pub fn log_path(&self) -> PathBuf {
        self.dir.join(ACCESS_JOURNAL_FILENAME)
    }

    /// **Append one access line, failing open.** Normalises `symbol` (trim + uppercase) to match the
    /// store's natural-key symbol comparison; a symbol that is empty or carries a reserved delimiter is
    /// skipped, and a non-positive `access_ts` is skipped (defensive — a real access is at a positive
    /// instant). Any directory-create / open / write error is swallowed: the access simply goes
    /// unrecorded rather than breaking the caller's read. Uses `O_APPEND` so concurrent short-line
    /// appends do not interleave.
    ///
    /// Every appended line is stamped with [`ACCESS_JOURNAL_SCHEMA_VERSION`] (SRS-DATA-015), so each
    /// persisted record is self-describing.
    ///
    /// Returns `true` if a line was durably written, `false` if the access was skipped or an I/O error
    /// was swallowed — the boolean is for tests/inspection, never an error the caller must handle.
    pub fn append(&self, job: &JobRef, symbol: &str, access_ts: i64) -> bool {
        let normalized = normalize_symbol(symbol);
        if normalized.is_empty() || normalized.contains('\t') || normalized.contains('\n') {
            return false;
        }
        if access_ts <= 0 {
            return false;
        }
        if fs::create_dir_all(&self.dir).is_err() {
            return false;
        }
        let line = format!(
            "{}{}\t{}\t{}\t{}\t{}\n",
            VERSION_TAG_PREFIX,
            ACCESS_JOURNAL_SCHEMA_VERSION,
            access_ts,
            job.kind.as_str(),
            job.id.as_str(),
            normalized
        );
        match fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.log_path())
        {
            Ok(mut file) => file.write_all(line.as_bytes()).is_ok(),
            Err(_) => false,
        }
    }

    /// **Fail-closed usability preflight** for a caller that wants to TRUST the journal as a
    /// recency-protection source (the SRS-DATA-010 eviction policy under `--use-journal`).
    ///
    /// Because [`append`](Self::append) / [`record`](Self::record) fail OPEN — a write error is
    /// swallowed so a running backtest/factor read is never broken — an unwritable journal would
    /// silently accumulate NO recency evidence, and a later [`recent`](Self::recent) would read empty,
    /// letting eviction remove data a running job is using. So a caller that opts into journal-based
    /// protection first asserts the journal is usable: the directory is creatable and writable (proven
    /// by writing and removing a probe file). An unusable journal is surfaced as
    /// [`AccessJournalError::Io`] so the caller fails CLOSED rather than trust an empty read.
    ///
    /// **Scope (honest bound):** this catches a *statically* unwritable journal — a read-only mount,
    /// wrong directory permissions, or a non-directory parent. A *transient* write loss while the
    /// journal is otherwise healthy (a momentary full disk in the recording process) is NOT detectable
    /// here; surfacing a running job's persistent recording failures to supervision is the deferred
    /// owner (the orchestrator workload registry + notification subsystem).
    pub fn ensure_usable(&self) -> Result<(), AccessJournalError> {
        fs::create_dir_all(&self.dir).map_err(|_| AccessJournalError::Io {
            context: "access-journal directory is not creatable",
        })?;
        let probe = self.dir.join(format!(".probe.{}", std::process::id()));
        let wrote = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&probe)
            .and_then(|mut file| file.write_all(b"probe"));
        let _ = fs::remove_file(&probe);
        wrote.map_err(|_| AccessJournalError::Io {
            context: "access-journal directory is not writable",
        })
    }

    /// The **recency read** the eviction policy consumes: per symbol, the most-recent `access_ts` that
    /// is within `[now_ts - window_secs, ..]` (accessed within the window), optionally restricted to
    /// jobs in `running`.
    ///
    /// - **Absent journal** (missing directory or no log file) → `Ok(empty)`: no access recorded, so no
    ///   recency protection — eviction proceeds. This is the one benign "nothing recorded yet" case.
    /// - **Torn tail** (a final line with no terminating newline) → ignored (split off at the last
    ///   newline). A crash mid-append is not corruption.
    /// - **Corrupt complete line** (wrong field count / non-integer ts / unknown kind) →
    ///   [`AccessJournalError::Corrupt`]: the read fails closed so the engine refuses to evict.
    /// - `running = Some(set)` keeps only accesses by a job in `set`; `running = None` keeps **all**
    ///   in-window accesses (fail-closed over-protect, the default until the running-job registry is
    ///   wired).
    ///
    /// `window_secs < 0` is treated as `0` (only accesses exactly at `now_ts` protected); the eviction
    /// policy validates its window `>= 0` so this is defensive.
    pub fn recent(
        &self,
        window_secs: i64,
        now_ts: i64,
        running: Option<&BTreeSet<JobId>>,
    ) -> Result<BTreeMap<String, i64>, AccessJournalError> {
        let path = self.log_path();
        let contents = match fs::read_to_string(&path) {
            Ok(contents) => contents,
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
            Err(_) => {
                return Err(AccessJournalError::Io {
                    context: "read access-journal log",
                })
            }
        };

        let window_start = now_ts.saturating_sub(window_secs.max(0));
        let mut latest: BTreeMap<String, i64> = BTreeMap::new();
        for line in complete_lines(&contents) {
            let entry = parse_line(line)?;
            // Within the recency window (accessed at or after the window start). A future access
            // (access_ts > now_ts) is >= window_start, so it is treated as protected — over-protecting
            // is the safe direction.
            if entry.access_ts < window_start {
                continue;
            }
            if let Some(set) = running {
                if !set.contains(&entry.job_id) {
                    continue;
                }
            }
            latest
                .entry(entry.symbol)
                .and_modify(|ts| {
                    if entry.access_ts > *ts {
                        *ts = entry.access_ts;
                    }
                })
                .or_insert(entry.access_ts);
        }
        Ok(latest)
    }
}

impl AccessRecorder for AccessJournal {
    fn record(&self, job: &JobRef, symbol: &str, access_ts: i64) {
        // Fail-open: ignore the boolean outcome; a failed append must never surface to the read path.
        let _ = self.append(job, symbol, access_ts);
    }
}

/// A parsed journal line.
struct ParsedAccess {
    access_ts: i64,
    job_id: JobId,
    symbol: String,
}

/// The **complete** lines of the log: every newline-terminated line. A trailing fragment with no
/// terminating newline (a torn tail from a crash mid-append) is dropped — it is not a complete record,
/// and dropping it is the benign, non-corruption path. Empty lines are skipped by the parser.
fn complete_lines(contents: &str) -> impl Iterator<Item = &str> {
    let mut end = contents.len();
    // If the content does not end in '\n', trim the final fragment (the torn tail) off.
    if !contents.ends_with('\n') {
        end = contents.rfind('\n').map(|i| i + 1).unwrap_or(0);
    }
    contents[..end].lines().filter(|l| !l.is_empty())
}

/// Parse one complete line, failing closed on any structural defect (a corrupt complete line makes the
/// whole recency read fail closed).
///
/// Two forms are accepted (SRS-DATA-015 / SyRS SYS-66):
/// - **versioned** `v<N>\t<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>` — the form this build writes;
/// - **legacy** `<access_ts>\t<job_kind>\t<job_id>\t<SYMBOL>` — written before the version field
///   existed, read as [`MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION`]. This is what keeps an existing
///   journal queryable **in place**, with no rewrite and no bulk migration.
///
/// The two are told apart by the `v` prefix alone: a legacy line's first field is a bare decimal
/// timestamp, which never begins with `v`. A version outside the supported range fails closed with
/// [`AccessJournalError::UnsupportedVersion`] — never a best-effort parse of a layout this build has
/// not seen.
fn parse_line(line: &str) -> Result<ParsedAccess, AccessJournalError> {
    let (line, declared_version) = match line.strip_prefix(VERSION_TAG_PREFIX) {
        // A `v`-prefixed first field: the line is self-describing.
        Some(rest) => {
            let (version_str, remainder) =
                rest.split_once('\t')
                    .ok_or_else(|| AccessJournalError::Corrupt {
                        reason: "version-tagged line has no fields after its version".to_string(),
                    })?;
            let version: i64 = version_str
                .parse()
                .map_err(|_| AccessJournalError::Corrupt {
                    reason: "non-integer line schema version".to_string(),
                })?;
            (remainder, version)
        }
        // No version field: a pre-SRS-DATA-015 line, read at the supported floor.
        None => (line, MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION),
    };
    if !(MIN_SUPPORTED_ACCESS_JOURNAL_SCHEMA_VERSION..=ACCESS_JOURNAL_SCHEMA_VERSION)
        .contains(&declared_version)
    {
        return Err(AccessJournalError::UnsupportedVersion {
            found: declared_version,
        });
    }
    let mut parts = line.split('\t');
    let ts_str = parts.next();
    let kind_str = parts.next();
    let id_str = parts.next();
    let symbol_str = parts.next();
    // Exactly four fields — a fifth means an unescaped delimiter leaked in (corruption).
    if parts.next().is_some() {
        return Err(AccessJournalError::Corrupt {
            reason: "more than four tab-delimited fields".to_string(),
        });
    }
    let (ts_str, kind_str, id_str, symbol_str) = match (ts_str, kind_str, id_str, symbol_str) {
        (Some(a), Some(b), Some(c), Some(d)) => (a, b, c, d),
        _ => {
            return Err(AccessJournalError::Corrupt {
                reason: "fewer than four tab-delimited fields".to_string(),
            })
        }
    };
    let access_ts: i64 = ts_str.parse().map_err(|_| AccessJournalError::Corrupt {
        reason: "non-integer access timestamp".to_string(),
    })?;
    if JobKind::from_tag(kind_str).is_none() {
        return Err(AccessJournalError::Corrupt {
            reason: "unknown job kind".to_string(),
        });
    }
    let job_id = JobId::new(id_str).map_err(|_| AccessJournalError::Corrupt {
        reason: "empty job id".to_string(),
    })?;
    let symbol = normalize_symbol(symbol_str);
    if symbol.is_empty() {
        return Err(AccessJournalError::Corrupt {
            reason: "empty symbol".to_string(),
        });
    }
    Ok(ParsedAccess {
        access_ts,
        job_id,
        symbol,
    })
}

/// Normalise a symbol the way the eviction planner and the store's natural key compare — trim
/// surrounding whitespace and uppercase. Kept in one place so the journal writer, the journal reader,
/// and the eviction policy all agree.
pub fn normalize_symbol(symbol: &str) -> String {
    symbol.trim().to_uppercase()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn journal(dir: &Path) -> AccessJournal {
        AccessJournal::new(dir.join("access_journal"))
    }

    fn job(kind: JobKind, id: &str) -> JobRef {
        JobRef::new(kind, JobId::new(id).unwrap())
    }

    #[test]
    fn append_then_recent_returns_the_symbol_within_window() {
        let tmp = tempdir();
        let j = journal(&tmp);
        assert!(j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000));
        let recent = j.recent(100, 1_050, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
    }

    #[test]
    fn access_outside_the_window_is_not_protected() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        // now_ts - window = 2_000 - 500 = 1_500 > 1_000 → out of window.
        let recent = j.recent(500, 2_000, None).unwrap();
        assert!(recent.is_empty(), "an old access must not be protected");
    }

    #[test]
    fn newest_access_per_symbol_wins() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        j.append(&job(JobKind::FactorPipeline, "fp-9"), "aapl", 1_400);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_200);
        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_400));
    }

    #[test]
    fn running_job_filter_scopes_protection() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "running"), "aapl", 1_000);
        j.append(&job(JobKind::Backtest, "finished"), "msft", 1_000);
        let mut running = BTreeSet::new();
        running.insert(JobId::new("running").unwrap());
        let recent = j.recent(1_000, 1_050, Some(&running)).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
        assert!(
            !recent.contains_key("MSFT"),
            "an access by a NON-running job must not protect"
        );
    }

    #[test]
    fn none_running_over_protects_all_in_window() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "a"), "aapl", 1_000);
        j.append(&job(JobKind::Backtest, "b"), "msft", 1_000);
        let recent = j.recent(1_000, 1_050, None).unwrap();
        assert_eq!(
            recent.len(),
            2,
            "None running → every in-window access protected"
        );
    }

    #[test]
    fn absent_journal_is_benign_empty() {
        let tmp = tempdir();
        let j = journal(&tmp);
        let recent = j.recent(1_000, 1_050, None).unwrap();
        assert!(recent.is_empty());
    }

    #[test]
    fn torn_tail_is_tolerated_not_corruption() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        // Simulate a crash mid-append: a final line with no terminating newline.
        {
            let mut f = fs::OpenOptions::new()
                .append(true)
                .open(j.log_path())
                .unwrap();
            f.write_all(b"1400\tbacktest\tbt-2\tMS").unwrap();
        }
        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
        assert!(
            !recent.contains_key("MS") && !recent.contains_key("MSFT"),
            "the torn tail must be ignored, never parsed"
        );
    }

    #[test]
    fn corrupt_complete_line_fails_closed() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        {
            let mut f = fs::OpenOptions::new()
                .append(true)
                .open(j.log_path())
                .unwrap();
            // A COMPLETE (newline-terminated) but malformed line.
            f.write_all(b"not-a-timestamp\tbacktest\tbt-2\tMSFT\n")
                .unwrap();
        }
        let err = j.recent(1_000, 1_500, None).unwrap_err();
        assert!(matches!(err, AccessJournalError::Corrupt { .. }));
    }

    #[test]
    fn unknown_job_kind_is_corruption() {
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        fs::write(j.log_path(), b"1000\tlive-trade\tid\tAAPL\n").unwrap();
        let err = j.recent(1_000, 1_050, None).unwrap_err();
        assert!(matches!(err, AccessJournalError::Corrupt { .. }));
    }

    #[test]
    fn empty_symbol_and_reserved_delimiters_are_skipped_on_append() {
        let tmp = tempdir();
        let j = journal(&tmp);
        assert!(!j.append(&job(JobKind::Backtest, "bt-1"), "   ", 1_000));
        assert!(!j.append(&job(JobKind::Backtest, "bt-1"), "a\tb", 1_000));
        assert!(!j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 0));
        let recent = j.recent(10_000, 2_000, None).unwrap();
        assert!(recent.is_empty());
    }

    // -- SRS-DATA-015 line-schema versioning ------------------------------------------------ //

    #[test]
    fn every_appended_line_records_its_schema_version() {
        // AC clause 1: each persisted entity records a schema version. For an append-only log the
        // persisted unit is the LINE, so every line must be self-describing.
        let tmp = tempdir();
        let j = journal(&tmp);
        assert!(j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000));
        assert!(j.append(&job(JobKind::FactorPipeline, "fp-1"), "msft", 1_100));
        let contents = fs::read_to_string(j.log_path()).unwrap();
        for line in contents.lines() {
            assert!(
                line.starts_with(&format!(
                    "{VERSION_TAG_PREFIX}{ACCESS_JOURNAL_SCHEMA_VERSION}\t"
                )),
                "every written line carries its version: {line}"
            );
        }
        // ...and the versioned line round-trips through the reader.
        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
        assert_eq!(recent.get("MSFT"), Some(&1_100));
    }

    #[test]
    fn a_legacy_version_less_line_stays_queryable_in_place() {
        // AC clause 2: data written under an older schema remains queryable WITHOUT bulk migration.
        // A journal written before SRS-DATA-015 has no version field; it must read exactly as before,
        // and the file must not be rewritten to get there.
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        let legacy = b"1000\tbacktest\tbt-legacy\tAAPL\n1100\tfactor-pipeline\tfp-legacy\tMSFT\n";
        fs::write(j.log_path(), legacy).unwrap();

        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
        assert_eq!(recent.get("MSFT"), Some(&1_100));

        let after = fs::read(j.log_path()).unwrap();
        assert_eq!(
            after, legacy,
            "reading a legacy journal must not rewrite it — no bulk migration"
        );
    }

    #[test]
    fn a_journal_mixing_legacy_and_versioned_lines_reads_both() {
        // The realistic upgrade state: an existing journal that a post-upgrade job appends to. An
        // append-only log cannot be partitioned by version, so both forms must coexist per file.
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        fs::write(j.log_path(), b"1000\tbacktest\tbt-legacy\tAAPL\n").unwrap();
        assert!(j.append(&job(JobKind::FactorPipeline, "fp-new"), "msft", 1_100));

        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000), "legacy line still read");
        assert_eq!(recent.get("MSFT"), Some(&1_100), "versioned line read");
    }

    #[test]
    fn an_unknown_future_line_version_fails_closed() {
        // A line written by a NEWER build may have a layout this one cannot parse. Guessing at its
        // fields would silently mis-attribute an access and under-protect live data, so the read
        // fails closed exactly like corruption does.
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        let future = ACCESS_JOURNAL_SCHEMA_VERSION + 1;
        fs::write(
            j.log_path(),
            format!("v{future}\t1000\tbacktest\tbt-1\tAAPL\textra-field\n").as_bytes(),
        )
        .unwrap();
        let err = j.recent(1_000, 1_500, None).unwrap_err();
        assert_eq!(
            err,
            AccessJournalError::UnsupportedVersion { found: future }
        );
    }

    #[test]
    fn a_malformed_version_tag_is_corruption() {
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        for bad in [
            "vX\t1000\tbacktest\tbt-1\tAAPL\n", // non-integer version
            "v1\n",                             // version with no fields after it
        ] {
            fs::write(j.log_path(), bad.as_bytes()).unwrap();
            let err = j.recent(1_000, 1_500, None).unwrap_err();
            assert!(
                matches!(err, AccessJournalError::Corrupt { .. }),
                "{bad:?} must be corruption, got {err:?}"
            );
        }
    }

    #[test]
    fn a_zero_or_negative_line_version_fails_closed() {
        // Defensive: a version below the supported floor is as unreadable as one above the ceiling.
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        for bad in [0_i64, -1] {
            fs::write(
                j.log_path(),
                format!("v{bad}\t1000\tbacktest\tbt-1\tAAPL\n").as_bytes(),
            )
            .unwrap();
            let err = j.recent(1_000, 1_500, None).unwrap_err();
            assert_eq!(err, AccessJournalError::UnsupportedVersion { found: bad });
        }
    }

    #[test]
    fn a_versioned_line_with_a_bad_body_is_still_corruption() {
        // The version gate must not become an escape hatch: a correctly-versioned line whose body is
        // malformed fails closed on the body, exactly as a legacy line does.
        let tmp = tempdir();
        let j = journal(&tmp);
        fs::create_dir_all(j.dir()).unwrap();
        fs::write(
            j.log_path(),
            format!("v{ACCESS_JOURNAL_SCHEMA_VERSION}\tnot-a-ts\tbacktest\tbt-1\tAAPL\n")
                .as_bytes(),
        )
        .unwrap();
        let err = j.recent(1_000, 1_500, None).unwrap_err();
        assert!(matches!(err, AccessJournalError::Corrupt { .. }));
    }

    #[test]
    fn a_versioned_torn_tail_is_still_tolerated() {
        let tmp = tempdir();
        let j = journal(&tmp);
        j.append(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        {
            let mut f = fs::OpenOptions::new()
                .append(true)
                .open(j.log_path())
                .unwrap();
            // A crash mid-append leaves a versioned line with no terminating newline.
            f.write_all(
                format!("v{ACCESS_JOURNAL_SCHEMA_VERSION}\t1400\tbacktest\tbt-2\tMS").as_bytes(),
            )
            .unwrap();
        }
        let recent = j.recent(1_000, 1_500, None).unwrap();
        assert_eq!(recent.get("AAPL"), Some(&1_000));
        assert!(!recent.contains_key("MS"));
    }

    #[test]
    fn job_id_rejects_empty_and_delimiters() {
        assert!(JobId::new("  ").is_err());
        assert!(JobId::new("a\tb").is_err());
        assert!(JobId::new("a\nb").is_err());
        assert_eq!(JobId::new(" bt-1 ").unwrap().as_str(), "bt-1");
    }

    #[test]
    fn ensure_usable_ok_on_a_writable_journal() {
        let tmp = tempdir();
        let j = journal(&tmp);
        assert!(j.ensure_usable().is_ok());
        // A probe file must not linger.
        let leftovers: Vec<_> = fs::read_dir(j.dir())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.file_name().to_string_lossy().starts_with(".probe"))
            .collect();
        assert!(
            leftovers.is_empty(),
            "the writability probe must be removed"
        );
    }

    #[test]
    fn ensure_usable_fails_closed_when_the_journal_dir_is_not_creatable() {
        // Point the journal at a path whose PARENT is a FILE, so create_dir_all fails regardless of
        // the running user (a portable stand-in for an unwritable/read-only journal location).
        let tmp = tempdir();
        let blocker = tmp.join("blocker");
        fs::write(&blocker, b"i am a file, not a directory").unwrap();
        let j = AccessJournal::new(blocker.join("access_journal"));
        let err = j.ensure_usable().unwrap_err();
        assert!(matches!(err, AccessJournalError::Io { .. }));
    }

    #[test]
    fn noop_recorder_records_nothing() {
        let tmp = tempdir();
        let rec = NoopRecorder;
        rec.record(&job(JobKind::Backtest, "bt-1"), "aapl", 1_000);
        // No journal directory should have been created by a no-op recorder.
        assert!(!tmp.join("access_journal").exists());
    }

    // --- a minimal, dependency-free temp dir (the crate is serde/dep-free; no `tempfile`) ---
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);

    fn tempdir() -> PathBuf {
        let seq = SEQ.fetch_add(1, Ordering::Relaxed);
        let base =
            std::env::temp_dir().join(format!("atp-access-journal-{}-{}", std::process::id(), seq));
        // CLEAR IT FIRST. This path is keyed on the PID, macOS recycles PIDs
        // within ~99k, and nothing here ever removes the directory — so a fresh
        // process routinely inherits a POPULATED one. The failures then land on
        // whichever tests assert absence or a specific corruption
        // (`absent_journal_is_benign_empty`, `torn_tail_is_tolerated_not_corruption`,
        // `corrupt_complete_line_fails_closed`), pass in isolation, and move
        // between runs — a phantom that reads exactly like a real regression in
        // a crate the diff never touched. Observed live at 17,241 leaked
        // directories. Safe: a PID is unique among LIVE processes, and `seq` is
        // unique within this one, so nothing running owns this path.
        let _ = fs::remove_dir_all(&base);
        fs::create_dir_all(&base).unwrap();
        base
    }
}
