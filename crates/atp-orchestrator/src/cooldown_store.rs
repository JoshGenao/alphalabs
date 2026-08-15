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
//! ## The window is written TWICE, and that is what closes the fail-open
//!
//! A failed [`record_completion`] used to mean the swap had completed and no window had
//! started — the automatic triggers armed against a strategy that was just promoted, with
//! nothing left to reconcile because the designation had already moved. Ordering could not
//! fix it: the publish and the window are two separate writes, so writing first strands a
//! window for a swap that never happened (review r6) and writing second strands a swap with
//! no window (review r13).
//!
//! So [`begin_provisional`] opens the window BEFORE the demotion, in its own
//! [`CooldownRecord::provisional_completion`] slot and stamped with the attempt instant, and
//! [`record_completion`] confirms it after the durable publish with the real completion
//! instant. A changeover that refuses or fails calls [`abandon_provisional`], so a window
//! still never outlives a swap that did not happen. Every interruption in between now lands
//! on a window that EXISTS: it may expire up to the SYS-49b liquidation timeout early,
//! which is recoverable, rather than being absent, which is not.
//!
//! The residual is stated rather than implied (safety-paths rule 41): an unconfirmed window
//! is visible as provisional on every surface, and clearing a stranded one is the operator
//! CLI's `record-completion` / repair job.

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
/// last_completed_at_seconds?, last_demoted_strategy_id?, last_promoted_strategy_id?,
/// provisional_completed_at_seconds?, provisional_demoted_strategy_id?,
/// provisional_promoted_strategy_id?}` — two INDEPENDENT triples, each
/// all-present-or-all-absent: the swap that completed, and the swap being attempted.
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
/// The IN-FLIGHT swap's triple — phase one written, phase two not yet reached.
///
/// A SEPARATE slot from the confirmed completion above, and adversarial review r15 is
/// why. The first two-phase draft reused the `last_*` triple for both and carried a
/// boolean saying which it meant, so a provisional record OVERWROTE a confirmed one:
/// an acknowledged manual swap inside a running window — which SYS-49a(a) guarantees
/// is allowed — displaced the very window it was running inside, and abandoning that
/// attempt then deleted a cool-down still in force, resuming the automatic triggers
/// days early. "A swap completed" and "a swap is being attempted" are two different
/// facts and cannot share one slot: clearing the second must never disturb the first.
const FIELD_PROVISIONAL_COMPLETED_AT: &str = "provisional_completed_at_seconds";
const FIELD_PROVISIONAL_DEMOTED: &str = "provisional_demoted_strategy_id";
const FIELD_PROVISIONAL_PROMOTED: &str = "provisional_promoted_strategy_id";

/// The complete set of keys a v1 payload may declare. Anything else fails the read: a key
/// this build does not know is a fact it cannot honour, and honouring the rest of the
/// record while silently dropping it is the fail-open direction.
const KNOWN_FIELDS: [&str; 9] = [
    FIELD_MAGIC,
    FIELD_SCHEMA_VERSION,
    FIELD_COOLDOWN_DAYS,
    FIELD_COMPLETED_AT,
    FIELD_DEMOTED,
    FIELD_PROMOTED,
    FIELD_PROVISIONAL_COMPLETED_AT,
    FIELD_PROVISIONAL_DEMOTED,
    FIELD_PROVISIONAL_PROMOTED,
];

/// The three completion fields are all-present-or-all-absent. A partial triple is a
/// payload that half-remembers a swap, and there is no half of it worth believing.
const COMPLETION_FIELDS: [&str; 3] = [FIELD_COMPLETED_AT, FIELD_DEMOTED, FIELD_PROMOTED];

/// Same rule for the in-flight triple, for the same reason.
const PROVISIONAL_FIELDS: [&str; 3] = [
    FIELD_PROVISIONAL_COMPLETED_AT,
    FIELD_PROVISIONAL_DEMOTED,
    FIELD_PROVISIONAL_PROMOTED,
];

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
    /// A swap that has STARTED and not yet been confirmed durable (review r13).
    ///
    /// A swap is not atomic: `execute_hot_swap` designates in memory and the caller
    /// publishes afterwards. Recording only after the publish left a completed swap
    /// whose window-write failed with the automatic triggers armed — unrecoverable,
    /// because the designation had already moved. Recording only before it opened a
    /// seven-day window for swaps that never became durable (r6). So the window is
    /// opened provisionally first and confirmed after, the same shape as
    /// SRS-RESV-004's engage-then-amend lockout (`safety-paths` rule 40).
    ///
    /// It lives in its OWN field rather than flagging `last_completion`, because the
    /// two facts have different lifetimes (review r15). A provisional attempt is
    /// discarded when its swap fails; a completion is never discarded. Sharing one
    /// slot meant an acknowledged manual swap inside a running window overwrote that
    /// window, and failing then erased it — the automatic triggers resuming days
    /// early, which is the fail-open SYS-49e exists to prevent.
    ///
    /// It SUPPRESSES exactly like a confirmed completion: over-suppressing after a
    /// maybe-swap is recoverable, under-suppressing after a real one is not. See
    /// [`resolve`], which classifies against whichever of the two runs later.
    pub provisional_completion: Option<SwapCompletion>,
}

impl CooldownRecord {
    /// A record carrying the SYS-49e default period and no swap history.
    pub fn fresh() -> Self {
        Self {
            period: CooldownPeriodDays::default(),
            last_completion: None,
            provisional_completion: None,
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
    for (completion, at, demoted, promoted) in [
        (
            &record.last_completion,
            FIELD_COMPLETED_AT,
            FIELD_DEMOTED,
            FIELD_PROMOTED,
        ),
        (
            &record.provisional_completion,
            FIELD_PROVISIONAL_COMPLETED_AT,
            FIELD_PROVISIONAL_DEMOTED,
            FIELD_PROVISIONAL_PROMOTED,
        ),
    ] {
        if let Some(completion) = completion {
            payload.push_str(&format!(",\"{at}\":{}", completion.completed_at_seconds));
            payload.push_str(&format!(
                ",\"{demoted}\":\"{}\"",
                completion.demoted_strategy_id.as_str()
            ));
            payload.push_str(&format!(
                ",\"{promoted}\":\"{}\"",
                completion.promoted_strategy_id.as_str()
            ));
        }
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

    // ONE triple parser, used for both slots. Two hand-written copies would be two
    // places for the id revalidation (r2) and the same-strategy refusal (r11) to drift
    // apart, and the provisional slot is the one a stranded marker sits in longest.
    let read_triple =
        |fields: [&'static str; 3]| -> Result<Option<SwapCompletion>, CooldownStoreError> {
            let (at_key, demoted_key, promoted_key) = (fields[0], fields[1], fields[2]);
            let present: Vec<&str> = fields
                .iter()
                .copied()
                .filter(|key| keys.contains(key))
                .collect();
            match present.len() {
                0 => Ok(None),
                3 => {
                    let completed_at_raw =
                        parse_strict_i64(required(at_key)?).ok_or_else(|| {
                            malformed(path, format!("field '{at_key}' is not a JSON integer"))
                        })?;
                    let completed_at_seconds = u64::try_from(completed_at_raw).map_err(|_| {
                        malformed(
                            path,
                            format!(
                                "field '{at_key}' ({completed_at_raw}) is negative; a swap \
                             cannot have completed before the Unix epoch"
                            ),
                        )
                    })?;
                    let demoted = json_string_value(required(demoted_key)?).ok_or_else(|| {
                        malformed(path, format!("field '{demoted_key}' is not a JSON string"))
                    })?;
                    let promoted = json_string_value(required(promoted_key)?).ok_or_else(|| {
                        malformed(path, format!("field '{promoted_key}' is not a JSON string"))
                    })?;
                    // Re-validated on READ, not merely on write: a hand-edited file, a record
                    // from a build with a laxer writer, or a partially-overwritten line is
                    // exactly where an id this format cannot represent would enter. Serving one
                    // would hand a caller a strategy id that is not the one that was recorded.
                    for (value, field) in [(demoted, demoted_key), (promoted, promoted_key)] {
                        validate_strategy_id(value, field)
                            .map_err(|reason| malformed(path, reason))?;
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
                    Ok(Some(SwapCompletion {
                        completed_at_seconds,
                        demoted_strategy_id: StrategyId::new(demoted),
                        promoted_strategy_id: StrategyId::new(promoted),
                    }))
                }
                _ => {
                    let missing: Vec<&str> = fields
                        .iter()
                        .copied()
                        .filter(|key| !keys.contains(key))
                        .collect();
                    Err(malformed(
                        path,
                        format!(
                            "payload declares {} of the {} {} fields (missing {}); a \
                         half-recorded swap has no half worth believing",
                            present.len(),
                            fields.len(),
                            if at_key == FIELD_COMPLETED_AT {
                                "completion"
                            } else {
                                "provisional"
                            },
                            missing.join(", "),
                        ),
                    ))
                }
            }
        };

    let last_completion = read_triple(COMPLETION_FIELDS)?;
    let provisional_completion = read_triple(PROVISIONAL_FIELDS)?;

    Ok(CooldownRecord {
        period,
        last_completion,
        provisional_completion,
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
        last_completion: existing.as_ref().and_then(|r| r.last_completion.clone()),
        // A period change must neither confirm nor discard an in-flight swap.
        provisional_completion: existing
            .as_ref()
            .and_then(|r| r.provisional_completion.clone()),
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
    // ONE locked read-modify-write, and adversarial review r20 is why. This used to
    // `load` the period OUTSIDE the lock and then hand it to `set_period`, which
    // reacquires the lock and writes it back — so an operator running
    // `configure --set-days 30` in the gap had their change silently rolled back to
    // whatever the probe had read, and the enforced window quietly reverted to the old
    // period. A pre-flight that proves the store is writable must not be able to
    // CHANGE it: this writes back exactly the record it read, under the same guard.
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|e| from_lock_error(e, path))?;
    let record = load(path)?.unwrap_or_else(CooldownRecord::fresh);
    save(path, &record)
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
    record_completion_inner(path, completion)
}

fn record_completion_inner(
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
    let stored_provisional = existing
        .as_ref()
        .and_then(|record| record.provisional_completion.clone());
    let previous = existing
        .as_ref()
        .and_then(|record| record.last_completion.clone());

    // Monotone against the GOVERNING window, not against one slot — adversarial
    // review r19. Checking only `last_completion` meant a confirmation with an older
    // instant cleared a NEWER provisional marker on its way past, shortening the
    // suppression an interrupted attempt had established. The rule the requirement
    // actually states is about the window in force, and the window in force is
    // whichever slot runs later (see `resolve`), so that is what the rule compares to.
    if let Some(stored) = governing(&existing) {
        if stored.completed_at_seconds > completion.completed_at_seconds {
            return Ok(CompletionOutcome::KeptNewer {
                stored: stored.clone(),
                offered: completion.clone(),
            });
        }
    }

    // Phase two also RETIRES this swap's provisional record: it has been superseded by
    // the confirmed completion being written in the same locked section, so leaving it
    // would strand a marker every surface would report as an unresolved interruption.
    // Another swap's provisional record is left strictly alone.
    let provisional_completion = match &stored_provisional {
        Some(stored) if is_same_swap(stored, completion) => None,
        other => other.clone(),
    };

    save(
        path,
        &CooldownRecord {
            period,
            last_completion: Some(completion.clone()),
            provisional_completion,
        },
    )?;
    Ok(CompletionOutcome::Recorded { previous })
}

/// Two records describe the same swap when they name the same pair of strategies.
///
/// Identity, not equality: phase one stamps the ATTEMPT instant and phase two rewrites
/// it with the completion instant, so the timestamps deliberately differ.
/// The completion the stored window actually runs from — the LATER of the two slots.
///
/// One definition, used by every writer's monotonicity check and mirrored by
/// [`resolve`]'s classification, so "the window in force" cannot mean one thing when
/// it is read and another when it is written (adversarial review r19). A rule that
/// guarded only `last_completion` let a confirmation with an older instant clear a
/// newer provisional marker on its way past, which is a shortening by another name.
fn governing(record: &Option<CooldownRecord>) -> Option<&SwapCompletion> {
    let record = record.as_ref()?;
    match (&record.last_completion, &record.provisional_completion) {
        (Some(confirmed), Some(in_flight)) => Some(
            if in_flight.completed_at_seconds > confirmed.completed_at_seconds {
                in_flight
            } else {
                confirmed
            },
        ),
        (Some(confirmed), None) => Some(confirmed),
        (None, in_flight) => in_flight.as_ref(),
    }
}

fn is_same_swap(left: &SwapCompletion, right: &SwapCompletion) -> bool {
    left.demoted_strategy_id == right.demoted_strategy_id
        && left.promoted_strategy_id == right.promoted_strategy_id
}

/// Open the window PROVISIONALLY, before the swap that owns it is durable.
///
/// Phase one of two (adversarial review r13). It suppresses exactly like a confirmed
/// window — over-suppressing after a maybe-swap is recoverable, under-suppressing
/// after a real one is not — but it is a separate record, so an operator can tell a
/// stranded marker from a genuine cool-down and it can be cleared by
/// [`abandon_provisional`] without disturbing the confirmed window.
///
/// It does NOT go through [`record_completion`]. Adversarial review r15: the first
/// draft did, so opening a provisional window overwrote the confirmed completion —
/// and since an acknowledged manual swap is legal INSIDE a running window
/// (SYS-49a(a)), the attempt displaced the very window it was running inside. When
/// that attempt then failed, abandoning it deleted a cool-down that was still in
/// force. Nothing here touches `last_completion`, which is what makes abandoning safe.
///
/// The monotonicity rule applies within the slot: a provisional record is not replaced
/// by an OLDER one, so a retry with a backwards clock cannot shorten the window an
/// in-flight attempt already opened.
pub fn begin_provisional(
    path: &Path,
    completion: &SwapCompletion,
) -> Result<CompletionOutcome, CooldownStoreError> {
    for (value, field) in [
        (
            completion.demoted_strategy_id.as_str(),
            FIELD_PROVISIONAL_DEMOTED,
        ),
        (
            completion.promoted_strategy_id.as_str(),
            FIELD_PROVISIONAL_PROMOTED,
        ),
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
    let last_completion = existing
        .as_ref()
        .and_then(|record| record.last_completion.clone());
    let previous = existing
        .as_ref()
        .and_then(|record| record.provisional_completion.clone());

    // The same governing rule as phase two, for the same reason: a marker older than
    // the window already in force can only shorten it.
    if let Some(stored) = governing(&existing) {
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
            last_completion,
            provisional_completion: Some(completion.clone()),
        },
    )?;
    Ok(CompletionOutcome::Recorded { previous })
}

/// Clear a provisional window whose swap never became durable.
///
/// Touches ONLY the provisional slot. The confirmed completion is never disturbed —
/// a swap that failed must not retire the cool-down of the last one that succeeded,
/// which is exactly what adversarial review r15 found the shared-slot draft doing.
/// A provisional record belonging to a DIFFERENT swap is also left alone.
pub fn abandon_provisional(
    path: &Path,
    completion: &SwapCompletion,
) -> Result<(), CooldownStoreError> {
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|e| from_lock_error(e, path))?;
    let Some(record) = load(path)? else {
        return Ok(());
    };
    // FULL equality, not just the strategy pair — adversarial review r18. A RETRY of
    // the same swap whose clock stepped backwards is kept out of the slot by
    // `begin_provisional`'s monotonicity rule and writes nothing; matching on identity
    // alone let that retry's failure clear the marker the FIRST attempt wrote, taking
    // with it the only window suppressing the automatic triggers after an interrupted
    // swap. An attempt clears exactly the record it wrote, or nothing.
    let ours = record
        .provisional_completion
        .as_ref()
        .is_some_and(|stored| stored == completion);
    if !ours {
        // Nothing in flight, not ours, or a newer attempt's. Leaving it alone is the
        // safe direction: an over-suppressing marker is resolvable, an absent one is
        // the fail-open.
        return Ok(());
    }
    save(
        path,
        &CooldownRecord {
            period: record.period,
            last_completion: record.last_completion,
            provisional_completion: None,
        },
    )
}

/// Whether the stored completion is still PROVISIONAL — phase one written, phase two
/// never confirmed (adversarial review r13).
///
/// `None` means the question could not be answered: no file, no completion, or an
/// unreadable one. That is deliberately NOT `Some(false)` — "not provisional" is a
/// claim about a window that was read, and a surface that renders an unreadable store
/// as a healthy confirmed swap is the same fail-open in a smaller place (CLAUDE.md
/// rule 3).
///
/// Reported rather than acted upon: a provisional window suppresses exactly like a
/// confirmed one, because an interrupted swap may well have gone live. What an
/// operator needs is to be able to TELL, since only they can find out whether the
/// candidate is actually running and then either let the window stand or clear it.
pub fn completion_is_provisional(path: &Path) -> Option<bool> {
    let record = load(path).ok()??;
    if record.provisional_completion.is_some() {
        // An attempt is in flight, or a stranded marker is left over from one that was
        // interrupted. Either way the window an operator is looking at is not yet a
        // confirmed one, and that is exactly the state they must be told about.
        return Some(true);
    }
    // Not provisional is a claim about a completion that exists. With no completion
    // there is no window to describe, which is a third answer, not `false`.
    record.last_completion.as_ref()?;
    Some(false)
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
            // The LATER of the two slots wins, because both suppress and the longer
            // window is the safe one. A confirmed completion and an in-flight attempt
            // can legitimately coexist — SYS-49a(a) allows an acknowledged manual swap
            // inside a running window — and the pair must never resolve to LESS
            // suppression than either alone. Taking the max is what makes an abandoned
            // attempt a no-op against the window it ran inside (review r15).
            //
            // The SAME `governing` every writer's monotonicity check uses, not a second
            // copy of the rule: r19 found the two had already drifted, and a window that
            // means one thing when read and another when written is the whole defect.
            let record = Some(record);
            let start = governing(&record);
            let period = record.as_ref().expect("just wrapped").period;
            CooldownState::classify(start, period, now_seconds)
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
            provisional_completion: None,
        };
        let payload = serialize(&record);
        let parsed = deserialize(Path::new("t.json"), &payload).expect("round trip");
        assert_eq!(parsed, record);
    }
}
