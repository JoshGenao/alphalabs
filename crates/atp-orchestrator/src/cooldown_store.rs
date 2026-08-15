//! SRS-RESV-006 / SyRS SYS-49e — the durable Hot-Swap cool-down window.
//!
//! The persistence half of [`crate::cooldown`]. One entity holds BOTH the configured
//! period and the last swap completion, because classifying an instant needs the two
//! together: split across two files they could be read a moment apart, and a period
//! change landing between the reads would classify against a window that never existed.
//! One file also means one lock and one fsync.
//!
//! ## Three read states, and only two of them are answers
//!
//! * **absent** → `Ok(None)`. No swap has ever completed and no period was configured, so
//!   there is genuinely no window. This inverts the sibling
//!   [`crate::trigger_config_store`], where absent means the SAFE all-disabled default —
//!   and the inversion is deliberate. A cool-down window can only be opened by a swap
//!   COMPLETING; before SRS-RESV-005 exists none ever has. Reading "no history" as "in
//!   cool-down" would leave SRS-RESV-003's automatic triggers permanently dead on a fresh
//!   install, with nothing on any surface to explain why.
//! * **readable** → `Ok(Some(record))`.
//! * **unreadable, corrupt, empty, or foreign** → `Err`, which [`resolve`] turns into
//!   [`CooldownState::Unknown`] and NEVER into "no cool-down". An empty-but-present file
//!   is a torn write, not an absent one — the single case where empty must not read as
//!   never-configured (`CLAUDE.md` rule 3).
//!
//! ## The window only moves forward
//!
//! [`record_completion`] KEEPS a stored completion that is newer than the one offered.
//! A clock that stepped backwards between two swaps would otherwise shorten a live safety
//! window, which is the one direction a cool-down must never move.
//!
//! ## Residual: a failed write leaves no window (owner SRS-RESV-005)
//!
//! If [`record_completion`] returns `Err`, the swap really did complete and no window
//! started — automatic triggers are NOT suppressed. That is a fail-open, and it is not
//! closeable from inside a CLI that then exits: an in-memory poison cannot outlive the
//! process (safety-paths rule 41's own counter-rule says to state the residual rather than
//! imply it survives). The write surface therefore exits non-zero saying in as many words
//! that the window did not start, and the durable close belongs to the production caller
//! in SRS-RESV-005, which can refuse to report the swap complete until the window is.

use crate::cooldown::{CooldownPeriodDays, CooldownState, SwapCompletion};
use crate::trigger_config_store::{ExclusiveGuard, TriggerConfigStoreError};
use atp_types::json_scan::{
    json_string_value, parse_strict_i64, top_level_json_field, top_level_json_keys,
};
use atp_types::StrategyId;
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

/// Magic marker every persisted cool-down window carries, so a foreign JSON file pointed
/// at this reader by a misconfiguration is refused before any timestamp is believed.
pub const MAGIC: &str = "ATP-HOT-SWAP-COOLDOWN";

/// The layout this build WRITES (SRS-DATA-015 / SyRS SYS-66).
///
/// **Version history:** v1 = `{magic, schema_version, cooldown_days,
/// last_completed_at_seconds?, last_demoted_strategy_id?, last_promoted_strategy_id?}`.
///
/// Versioned since its first byte, so there is no unversioned payload to stay compatible
/// with and no legacy floor to read at.
pub const COOLDOWN_SCHEMA_VERSION: i64 = 1;

/// The oldest layout this build still READS.
pub const MIN_SUPPORTED_COOLDOWN_SCHEMA_VERSION: i64 = 1;

const FIELD_MAGIC: &str = "magic";
const FIELD_SCHEMA_VERSION: &str = "schema_version";
const FIELD_COOLDOWN_DAYS: &str = "cooldown_days";
const FIELD_COMPLETED_AT: &str = "last_completed_at_seconds";
const FIELD_DEMOTED: &str = "last_demoted_strategy_id";
const FIELD_PROMOTED: &str = "last_promoted_strategy_id";

/// The complete set of keys a v1 payload may declare. Anything else fails the read: a key
/// this build does not know is a fact it cannot honour, and honouring the rest of the
/// record while silently dropping it is the fail-open direction.
const KNOWN_FIELDS: [&str; 6] = [
    FIELD_MAGIC,
    FIELD_SCHEMA_VERSION,
    FIELD_COOLDOWN_DAYS,
    FIELD_COMPLETED_AT,
    FIELD_DEMOTED,
    FIELD_PROMOTED,
];

/// The three completion fields are all-present-or-all-absent. A partial triple is a
/// payload that half-remembers a swap, and there is no half of it worth believing.
const COMPLETION_FIELDS: [&str; 3] = [FIELD_COMPLETED_AT, FIELD_DEMOTED, FIELD_PROMOTED];

/// Base name of the scratch file an atomic save writes before renaming it onto the
/// caller's path; the real name appends `<pid>.<seq>` so two writers in one directory
/// cannot rename over each other's scratch.
const SCRATCH_SUFFIX: &str = "hot-swap-cooldown.tmp";

/// Process-local monotonic counter disambiguating concurrent scratch files within one
/// process. Not a clock or an RNG, so a save stays reproducible.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// The persisted cool-down window: the configured period, plus the last swap completion
/// if one has ever been recorded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CooldownRecord {
    pub period: CooldownPeriodDays,
    pub last_completion: Option<SwapCompletion>,
}

impl CooldownRecord {
    /// A record carrying the SYS-49e default period and no swap history.
    pub fn fresh() -> Self {
        Self {
            period: CooldownPeriodDays::default(),
            last_completion: None,
        }
    }
}

/// What [`record_completion`] did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CompletionOutcome {
    /// The offered completion is now the window's start.
    Recorded { previous: Option<SwapCompletion> },
    /// A completion OLDER than the stored one was offered; the stored record was KEPT.
    ///
    /// Not an `Err` — the swap being reported really did happen — but the caller MUST
    /// surface it: the only way to reach this state is a clock that disagrees with
    /// recorded history, and the operator needs to know that before trusting either.
    KeptNewer {
        stored: SwapCompletion,
        offered: SwapCompletion,
    },
}

/// Why a persisted cool-down window could not be read or written.
///
/// [`Malformed`](Self::Malformed) and [`UnsupportedVersion`](Self::UnsupportedVersion) are
/// kept distinct from [`Io`](Self::Io) because they mean different things to an operator:
/// the bytes are there but wrong, versus the bytes could not be reached at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CooldownStoreError {
    Io {
        action: &'static str,
        path: PathBuf,
        detail: String,
    },
    Malformed {
        path: PathBuf,
        reason: String,
    },
    UnsupportedVersion {
        path: PathBuf,
        declared: i64,
    },
    Locked {
        lock_path: PathBuf,
        waited: Duration,
    },
}

impl fmt::Display for CooldownStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io {
                action,
                path,
                detail,
            } => write!(
                formatter,
                "cannot {action} hot-swap cool-down window {}: {detail}",
                path.display()
            ),
            Self::Malformed { path, reason } => write!(
                formatter,
                "hot-swap cool-down window {} is malformed: {reason}",
                path.display()
            ),
            Self::UnsupportedVersion { path, declared } => write!(
                formatter,
                "hot-swap cool-down window {} declares schema version {declared}, outside \
                 this build's supported range {MIN_SUPPORTED_COOLDOWN_SCHEMA_VERSION}..=\
                 {COOLDOWN_SCHEMA_VERSION}",
                path.display()
            ),
            Self::Locked { lock_path, waited } => write!(
                formatter,
                "hot-swap cool-down window is locked by another operation ({} held for \
                 more than {}s)",
                lock_path.display(),
                waited.as_secs()
            ),
        }
    }
}

impl std::error::Error for CooldownStoreError {}

/// Convert the shared lock's error into this store's taxonomy.
///
/// A STRUCTURAL match on the public variants, never a re-wrap of its `Display`: the
/// sibling's messages name a *trigger configuration*, and an operator chasing a wedged
/// cool-down must not be sent to the wrong file. The lock is reused
/// ([`ExclusiveGuard`]) precisely so there is one O_EXCL implementation in the tree
/// rather than two that can drift; only its vocabulary is translated here.
fn from_lock_error(error: TriggerConfigStoreError, path: &Path) -> CooldownStoreError {
    match error {
        TriggerConfigStoreError::Locked { lock_path, waited } => {
            CooldownStoreError::Locked { lock_path, waited }
        }
        TriggerConfigStoreError::Io { action, detail, .. } => CooldownStoreError::Io {
            action,
            path: path.to_path_buf(),
            detail,
        },
        TriggerConfigStoreError::Malformed { reason, .. } => malformed(path, reason),
        TriggerConfigStoreError::UnsupportedVersion { declared, .. } => {
            CooldownStoreError::UnsupportedVersion {
                path: path.to_path_buf(),
                declared,
            }
        }
    }
}

fn io_error(action: &'static str, path: &Path, error: &std::io::Error) -> CooldownStoreError {
    CooldownStoreError::Io {
        action,
        path: path.to_path_buf(),
        detail: error.to_string(),
    }
}

fn malformed(path: &Path, reason: impl Into<String>) -> CooldownStoreError {
    CooldownStoreError::Malformed {
        path: path.to_path_buf(),
        reason: reason.into(),
    }
}

/// THE strategy-id rule for this format, applied on WRITE and again on READ.
///
/// One validator shared by both directions, because a writer that can emit what its own
/// reader refuses corrupts the store while reporting success (durable-writes rule 9;
/// adversarial-precheck rule 8 — "writer must satisfy its own reader", the RESV-003 r3
/// class).
///
/// **Why refuse rather than escape.** [`serialize`] hand-builds a single JSON line, so a
/// `"` or `\` in an id would need escaping — but `json_string_value` deliberately returns
/// the RAW, still-escaped inner text (the sibling trigger-config store depends on that to
/// compare key spellings). Escaping on write would therefore round-trip `a"b` back as
/// `a\"b`: a DIFFERENT strategy id, silently. Refusing the two characters keeps the
/// persisted form literal in both directions, and no legitimate strategy id needs them.
/// Control characters go with them for the same reason the trigger log excludes them.
fn validate_strategy_id(value: &str, field: &str) -> Result<(), String> {
    if value.is_empty() {
        return Err(format!("{field} is empty"));
    }
    if let Some(bad) = value.chars().find(|c| c.is_control()) {
        return Err(format!(
            "{field} contains the control character U+{:04X}; the cool-down record is one \
             JSON line and a control character cannot survive it",
            bad as u32
        ));
    }
    if let Some(bad) = value.chars().find(|&c| c == '"' || c == '\\') {
        return Err(format!(
            "{field} contains {bad:?}, which this format refuses: the record is hand-built \
             JSON and its reader returns raw (still-escaped) text, so escaping would \
             round-trip a different id back"
        ));
    }
    Ok(())
}

/// Serialize `record` to the single-line v1 JSON payload this module persists.
///
/// The three completion fields are written together or not at all — [`deserialize`]
/// refuses any partial combination rather than picking which half to believe.
pub fn serialize(record: &CooldownRecord) -> String {
    let mut payload = String::new();
    payload.push('{');
    payload.push_str(&format!("\"{FIELD_MAGIC}\":\"{MAGIC}\","));
    payload.push_str(&format!(
        "\"{FIELD_SCHEMA_VERSION}\":{COOLDOWN_SCHEMA_VERSION},"
    ));
    payload.push_str(&format!(
        "\"{FIELD_COOLDOWN_DAYS}\":{}",
        record.period.get()
    ));
    if let Some(completion) = &record.last_completion {
        payload.push_str(&format!(
            ",\"{FIELD_COMPLETED_AT}\":{}",
            completion.completed_at_seconds
        ));
        payload.push_str(&format!(
            ",\"{FIELD_DEMOTED}\":\"{}\"",
            completion.demoted_strategy_id.as_str()
        ));
        payload.push_str(&format!(
            ",\"{FIELD_PROMOTED}\":\"{}\"",
            completion.promoted_strategy_id.as_str()
        ));
    }
    payload.push('}');
    payload
}

/// Parse a v1 payload, refusing anything this build cannot interpret unambiguously.
fn deserialize(path: &Path, payload: &str) -> Result<CooldownRecord, CooldownStoreError> {
    let keys = top_level_json_keys(payload)
        .map_err(|_| malformed(path, "payload is not a single well-formed JSON object"))?;
    for key in &keys {
        if !KNOWN_FIELDS.contains(key) {
            return Err(malformed(
                path,
                format!("payload declares unknown field '{key}'"),
            ));
        }
    }

    let field = |key: &str| -> Result<Option<&str>, CooldownStoreError> {
        top_level_json_field(payload, key)
            .map_err(|_| malformed(path, "payload is not a single well-formed JSON object"))
    };
    let required = |key: &'static str| -> Result<&str, CooldownStoreError> {
        field(key)?.ok_or_else(|| malformed(path, format!("payload is missing field '{key}'")))
    };

    // Identity before meaning: a file that is not ours has no fields worth reading.
    let magic = json_string_value(required(FIELD_MAGIC)?)
        .ok_or_else(|| malformed(path, format!("field '{FIELD_MAGIC}' is not a JSON string")))?;
    if magic != MAGIC {
        return Err(malformed(
            path,
            format!("payload carries magic {magic:?}, expected {MAGIC:?}"),
        ));
    }

    // Layout before fields: an unsupported version means the field meanings below are not
    // this build's to assume.
    let declared = parse_strict_i64(required(FIELD_SCHEMA_VERSION)?).ok_or_else(|| {
        malformed(
            path,
            format!("field '{FIELD_SCHEMA_VERSION}' is not a JSON integer"),
        )
    })?;
    if !(MIN_SUPPORTED_COOLDOWN_SCHEMA_VERSION..=COOLDOWN_SCHEMA_VERSION).contains(&declared) {
        return Err(CooldownStoreError::UnsupportedVersion {
            path: path.to_path_buf(),
            declared,
        });
    }

    let days_raw = parse_strict_i64(required(FIELD_COOLDOWN_DAYS)?).ok_or_else(|| {
        malformed(
            path,
            format!("field '{FIELD_COOLDOWN_DAYS}' is not a JSON integer"),
        )
    })?;
    let days = u32::try_from(days_raw).map_err(|_| {
        malformed(
            path,
            format!("cool-down period {days_raw} days is out of range"),
        )
    })?;
    // Re-validated through the newtype rather than trusted: the `[1, 365]` bound is the
    // type's invariant, and a hand-edited file is exactly where a 0 — which would silently
    // defeat SYS-49e while looking like configuration — would enter.
    let period =
        CooldownPeriodDays::new(days).map_err(|error| malformed(path, error.to_string()))?;

    let present: Vec<&str> = COMPLETION_FIELDS
        .iter()
        .copied()
        .filter(|key| keys.contains(key))
        .collect();
    let last_completion = match present.len() {
        0 => None,
        3 => {
            let completed_at_raw =
                parse_strict_i64(required(FIELD_COMPLETED_AT)?).ok_or_else(|| {
                    malformed(
                        path,
                        format!("field '{FIELD_COMPLETED_AT}' is not a JSON integer"),
                    )
                })?;
            let completed_at_seconds = u64::try_from(completed_at_raw).map_err(|_| {
                malformed(
                    path,
                    format!(
                        "field '{FIELD_COMPLETED_AT}' ({completed_at_raw}) is negative; a swap \
                         cannot have completed before the Unix epoch"
                    ),
                )
            })?;
            let demoted = json_string_value(required(FIELD_DEMOTED)?).ok_or_else(|| {
                malformed(
                    path,
                    format!("field '{FIELD_DEMOTED}' is not a JSON string"),
                )
            })?;
            let promoted = json_string_value(required(FIELD_PROMOTED)?).ok_or_else(|| {
                malformed(
                    path,
                    format!("field '{FIELD_PROMOTED}' is not a JSON string"),
                )
            })?;
            // Re-validated on READ, not merely on write: a hand-edited file, a record from
            // a build with a laxer writer, or a partially-overwritten line is exactly where
            // an id this format cannot represent would enter. Serving one would hand a
            // caller a strategy id that is not the one that was recorded.
            for (value, field) in [(demoted, FIELD_DEMOTED), (promoted, FIELD_PROMOTED)] {
                validate_strategy_id(value, field).map_err(|reason| malformed(path, reason))?;
            }
            if demoted == promoted {
                return Err(malformed(
                    path,
                    format!(
                        "the recorded completion demotes and promotes the same strategy \
                         ({demoted:?}); that is not a swap"
                    ),
                ));
            }
            Some(SwapCompletion {
                completed_at_seconds,
                demoted_strategy_id: StrategyId::new(demoted),
                promoted_strategy_id: StrategyId::new(promoted),
            })
        }
        _ => {
            let missing: Vec<&str> = COMPLETION_FIELDS
                .iter()
                .copied()
                .filter(|key| !keys.contains(key))
                .collect();
            return Err(malformed(
                path,
                format!(
                    "payload declares {} of the {} completion fields (missing {}); a \
                     half-recorded swap has no half worth believing",
                    present.len(),
                    COMPLETION_FIELDS.len(),
                    missing.join(", "),
                ),
            ));
        }
    };

    Ok(CooldownRecord {
        period,
        last_completion,
    })
}

/// Read the persisted cool-down window at `path`.
///
/// See the module docs for why `Ok(None)` (absent) is the permissive answer here while its
/// sibling store's absent case is the restrictive one.
pub fn load(path: &Path) -> Result<Option<CooldownRecord>, CooldownStoreError> {
    let payload = match fs::read_to_string(path) {
        Ok(payload) => payload,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error("read", path, &error)),
    };
    // A file that exists but holds only whitespace is a torn or truncated write, not an
    // absent window — the one case where "empty" must not read as "never swapped".
    if payload.trim().is_empty() {
        return Err(malformed(path, "file is empty"));
    }
    deserialize(path, payload.trim()).map(Some)
}

/// Durably persist `record` to `path`, creating the parent directory if absent.
///
/// Crash-durable and atomically published, the same four-step discipline as
/// [`crate::trigger_config_store::save`]: write a per-call-unique scratch file, `fsync` it
/// so its bytes reach disk, `rename` it onto the live path (an atomic replace — a reader
/// never sees a half-written window), then `fsync` the parent directory so the rename
/// itself survives a crash. A window that silently reverted after a power loss would let
/// an automatic trigger fire inside a cool-down the operator watched start.
pub fn save(path: &Path, record: &CooldownRecord) -> Result<(), CooldownStoreError> {
    // A relative `cooldown.json` has an EMPTY parent, not an absent one; `.` is that
    // directory. Filtering it away would place the scratch file correctly and then skip
    // the final directory fsync, losing the very durability this contract states.
    let dir = match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    };
    let dir = dir.as_path();
    fs::create_dir_all(dir).map_err(|error| io_error("create directory for", path, &error))?;
    let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
    let scratch_name = format!(
        "{}.{}.{seq}.{SCRATCH_SUFFIX}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("hot-swap-cooldown"),
        std::process::id()
    );
    let scratch_path = dir.join(scratch_name);

    let mut payload = serialize(record);
    payload.push('\n');

    let mut scratch = fs::File::create(&scratch_path)
        .map_err(|error| io_error("create scratch file for", path, &error))?;
    if let Err(error) = scratch
        .write_all(payload.as_bytes())
        .and_then(|()| scratch.flush())
        .and_then(|()| scratch.sync_all())
    {
        let _ = fs::remove_file(&scratch_path);
        return Err(io_error("write scratch file for", path, &error));
    }
    drop(scratch);

    fs::rename(&scratch_path, path).map_err(|error| {
        let _ = fs::remove_file(&scratch_path);
        io_error("publish", path, &error)
    })?;

    let handle =
        fs::File::open(dir).map_err(|error| io_error("open directory of", path, &error))?;
    handle
        .sync_all()
        .map_err(|error| io_error("sync directory of", path, &error))?;
    Ok(())
}

/// Set the configured cool-down period, preserving any recorded completion.
///
/// Read-modify-write under the exclusive guard: without it two concurrent operator
/// changes would both read the old record and both write a full replacement, so one
/// change would vanish while its caller was told it succeeded.
pub fn set_period(
    path: &Path,
    period: CooldownPeriodDays,
) -> Result<CooldownRecord, CooldownStoreError> {
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|e| from_lock_error(e, path))?;
    // Every fallible READ happens before the write, under the same guard: a window this
    // build cannot read must refuse the change rather than be overwritten by it
    // (adversarial-precheck rule 9 — order of operations around durable writes).
    let existing = load(path)?;
    let record = CooldownRecord {
        period,
        last_completion: existing.and_then(|record| record.last_completion),
    };
    save(path, &record)?;
    Ok(record)
}

/// Prove this window can be RECORDED, before anything irreversible depends on it.
///
/// A swap that completes and then cannot record its completion is a fail-open: the
/// designation has moved, the book is flat, and the automatic triggers the window was
/// supposed to suppress are still armed. It cannot be undone afterwards — reverting a
/// live designation over an unwritable file leaves the demoted strategy live with an
/// emptied book, which is strictly worse — so the honest place to catch it is BEFORE
/// the swap runs, where refusing costs nothing (`safety-paths` rule 41: a failed
/// durable write must leave a fail-closed STATE, not just a truthful error;
/// adversarial review r4 named the residual this closes).
///
/// The probe is a real read-modify-write through [`set_period`], not a permissions
/// guess: it takes the same lock, walks the same scratch → fsync → rename path, and
/// therefore fails for every reason the real write would. It is idempotent in
/// MEANING — the period is written back as it was read, and an absent file becomes a
/// present one carrying the default period, which classifies identically
/// ([`CooldownState::NeverSwapped`]). Any recorded completion is preserved, because
/// `set_period` preserves it.
///
/// This narrows the fail-open to a genuine race (a disk that fills between this call
/// and the completion write). That residual is stated rather than implied — see
/// `hot_swap_cooldown_contract.deferred`.
pub fn probe_recordable(
    path: &Path,
    demoted: &StrategyId,
    promoted: &StrategyId,
) -> Result<(), CooldownStoreError> {
    // The IDs, by the SAME rule `record_completion` will apply. Proving the FILE is
    // writable is not proving THIS completion can be written: `serialize` hand-builds
    // one JSON line, so an id carrying `"` or `\` is refused on the write — and if
    // that refusal arrives after the swap has completed, the designation has moved and
    // no window opened. Adversarial review r11 found exactly that gap between r2's
    // escaping rule and r4's writability probe.
    validate_strategy_id(demoted.as_str(), FIELD_DEMOTED).map_err(|r| malformed(path, r))?;
    validate_strategy_id(promoted.as_str(), FIELD_PROMOTED).map_err(|r| malformed(path, r))?;
    if demoted.as_str() == promoted.as_str() {
        return Err(malformed(
            path,
            "a swap completion names the same strategy on both sides",
        ));
    }
    probe_writable(path)
}

fn probe_writable(path: &Path) -> Result<(), CooldownStoreError> {
    let period = match load(path)? {
        Some(record) => record.period,
        None => CooldownPeriodDays::default(),
    };
    set_period(path, period).map(|_| ())
}

/// Record a swap completion, starting the SYS-49e window at its timestamp.
///
/// **Only a PROMOTION may call this.** SRS-RESV-004's demotion can finish without any
/// promotion (the SYS-49b demotion-pending timeout); that is a failed changeover, not a
/// swap, and starting a week-long window from it would suppress the automatic triggers
/// because something went wrong.
///
/// Read-modify-write under the exclusive guard, and monotone: an offered completion older
/// than the stored one is REFUSED into [`CompletionOutcome::KeptNewer`] rather than
/// shortening a window that is already running.
pub fn record_completion(
    path: &Path,
    completion: &SwapCompletion,
) -> Result<CompletionOutcome, CooldownStoreError> {
    // Validated BEFORE the guard and the write: a completion this build's own reader would
    // refuse must never reach the file (durable-writes rule 4). `StrategyId::new` accepts
    // ANY string, so this is the only place between an arbitrary caller — including the
    // deferred SRS-RESV-005 producer — and the durable line.
    for (value, field) in [
        (completion.demoted_strategy_id.as_str(), FIELD_DEMOTED),
        (completion.promoted_strategy_id.as_str(), FIELD_PROMOTED),
    ] {
        validate_strategy_id(value, field).map_err(|reason| malformed(path, reason))?;
    }
    if completion.demoted_strategy_id.as_str() == completion.promoted_strategy_id.as_str() {
        return Err(malformed(
            path,
            format!(
                "the completion demotes and promotes the same strategy ({:?}); that is not \
                 a swap and must not start a cool-down",
                completion.demoted_strategy_id.as_str()
            ),
        ));
    }

    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|e| from_lock_error(e, path))?;
    let existing = load(path)?;
    let period = existing
        .as_ref()
        .map(|record| record.period)
        .unwrap_or_default();
    let previous = existing.and_then(|record| record.last_completion);

    if let Some(stored) = &previous {
        if stored.completed_at_seconds > completion.completed_at_seconds {
            return Ok(CompletionOutcome::KeptNewer {
                stored: stored.clone(),
                offered: completion.clone(),
            });
        }
    }

    save(
        path,
        &CooldownRecord {
            period,
            last_completion: Some(completion.clone()),
        },
    )?;
    Ok(CompletionOutcome::Recorded { previous })
}

/// **The** production producer of a [`CooldownState`] — the one place a store failure
/// becomes a cool-down state.
///
/// Keeping this mapping in a single function is what lets the gate stay a pure value
/// consumer: there is exactly one `Err → Unknown` edge to get right, rather than one per
/// call site. `tools/hot_swap_cooldown_check.py` asserts that both operator subcommands
/// obtain their state from here and construct no `CooldownState` themselves.
///
/// `path: None` is NOT permissive: a surface that was never told where the window lives
/// cannot say whether a swap is inside one, and saying "no cool-down" on its behalf is the
/// fail-open this feature exists to prevent.
pub fn resolve(path: Option<&Path>, now_seconds: u64) -> CooldownState {
    let Some(path) = path else {
        return CooldownState::unknown(
            "no cool-down state path configured (--cooldown-state); this build cannot say \
             whether a Hot-Swap cool-down is in effect",
        );
    };
    match load(path) {
        Ok(Some(record)) => {
            CooldownState::classify(record.last_completion.as_ref(), record.period, now_seconds)
        }
        Ok(None) => CooldownState::classify(None, CooldownPeriodDays::default(), now_seconds),
        Err(error) => CooldownState::unknown(error.to_string()),
    }
}

#[cfg(test)]
mod resv006_cooldown_store_unit_tests {
    //! Serializer round-trips only. Every BEHAVIOURAL test lives in
    //! `crates/atp-orchestrator/tests/resv_6_cooldown_store.rs` — `mutation_verify`
    //! reverts a source file wholesale, so a test beside its subject vanishes with the
    //! code it guards and the run goes vacuously green (test-integrity rule 23).

    use super::*;

    #[test]
    fn a_record_with_no_completion_round_trips() {
        let record = CooldownRecord::fresh();
        let payload = serialize(&record);
        let parsed = deserialize(Path::new("t.json"), &payload).expect("round trip");
        assert_eq!(parsed, record);
    }

    #[test]
    fn a_record_with_a_completion_round_trips() {
        let record = CooldownRecord {
            period: CooldownPeriodDays::new(30).unwrap(),
            last_completion: Some(SwapCompletion {
                completed_at_seconds: 1_715_000_000,
                demoted_strategy_id: StrategyId::new("alpha"),
                promoted_strategy_id: StrategyId::new("beta"),
            }),
        };
        let payload = serialize(&record);
        let parsed = deserialize(Path::new("t.json"), &payload).expect("round trip");
        assert_eq!(parsed, record);
    }
}
