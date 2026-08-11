//! # SRS-RESV-004 durable demotion-pending lockout (SyRS SYS-49c (c) and (d))
//!
//! SYS-49c requires that after a demotion liquidation timeout the system "hold the swap in a
//! *demotion-pending* state" and "block the promotion phase **until the operator manually
//! resolves** the unfilled positions". Both clauses are about time the process does not span.
//!
//! [`crate::StrategyOrchestrator::resolve_demotion`] decides ONE attempt: it returns `Err` on a
//! timeout, and a caller that promotes only on `Ok` therefore blocks promotion *for that call*.
//! That is not what SYS-49c asks for. A second attempt — a retry, a restart, a different operator
//! surface — calls the gate again, and if the probe now reports flat the gate has no memory that a
//! previous liquidation was left unfilled. It would promote a candidate while the demoted
//! strategy's IB positions are still open, which is the precise outcome the requirement's last
//! sentence forbids.
//!
//! This module is that memory: a lockout that lives in bytes, is engaged by the timeout branch, and
//! is cleared only by an explicit operator resolution.
//!
//! ## Why the read is three-state, and why only this module collapses it
//!
//! [`load`] answers with the same honest three states the sibling trigger-config store uses:
//!
//! * **No file** → no demotion is pending. `Ok(None)`.
//! * **A readable file** → a demotion IS pending; promotion is blocked. `Ok(Some(record))`.
//! * **An unreadable file** → a lockout exists and this build cannot say what it holds. `Err`.
//!
//! A safety lockout must not leave the third case to caller convention, though. "Unreadable" and
//! "absent" both produce *no record*, and a caller that pattern-matches on the record alone reads a
//! corrupt lockout as a clear one — a false all-clear on the single question this file exists to
//! answer. So [`read_state`] performs the fail-closed collapse **here, once**
//! ([`DemotionPendingState::Unreadable`] blocks exactly like [`DemotionPendingState::Pending`]),
//! and the gate consumes [`DemotionPendingState`] rather than `Result`. There is no arm of the
//! promotion path that can accidentally treat an I/O error as permission to promote.
//!
//! ## Why clearing is not reachable from the gate
//!
//! [`resolve`] is deliberately absent from the [`crate::DemotionPendingLock`] port the gate is
//! given. The gate can ask whether promotion is blocked and it can engage a block; it has no method
//! that unblocks one. "Until the operator manually resolves" is then a property of the type graph
//! rather than of the gate's good behaviour — the same discipline `LiveDesignation` uses to keep a
//! strategy from minting its own live authority (SRS-EXE-001).
//!
//! ## Why an engage never overwrites
//!
//! A second timeout while a lockout is already held would, on a naive save, replace the record the
//! operator still has to act on — the unresolved positions from the FIRST timeout would stop being
//! described anywhere. [`engage`] refuses instead ([`DemotionPendingStoreError::AlreadyPending`]),
//! under the same exclusive guard it reads through, so the check and the write cannot be
//! interleaved by a concurrent surface.

use atp_types::json_scan::{
    json_string_value, parse_strict_i64, top_level_json_field, top_level_json_keys,
};
use atp_types::{SideEffectOutcome, StrategyId};
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};

use crate::trigger_config_store::ExclusiveGuard;

/// Magic marker every persisted lockout carries, so a foreign JSON file pointed at this reader by a
/// misconfiguration is refused before its absence of a `demoting_strategy_id` is read as "clear".
pub const MAGIC: &str = "ATP-HOT-SWAP-DEMOTION-PENDING";

/// The lockout layout this build WRITES (SRS-DATA-015 / SyRS SYS-66).
///
/// **Version history:** v1 = `{magic, schema_version, demoting_strategy_id, candidate_strategy_id,
/// elapsed_seconds, timeout_seconds, observed_at_seconds, liquidation_cancel,
/// liquidation_cancel_reason?, operator_alert, operator_alert_reason?}`.
pub const DEMOTION_PENDING_SCHEMA_VERSION: i64 = 1;

/// The oldest lockout layout this build still READS.
pub const MIN_SUPPORTED_DEMOTION_PENDING_SCHEMA_VERSION: i64 = 1;

const FIELD_MAGIC: &str = "magic";
const FIELD_SCHEMA_VERSION: &str = "schema_version";
const FIELD_DEMOTING: &str = "demoting_strategy_id";
const FIELD_CANDIDATE: &str = "candidate_strategy_id";
const FIELD_ELAPSED: &str = "elapsed_seconds";
const FIELD_TIMEOUT: &str = "timeout_seconds";
const FIELD_OBSERVED_AT: &str = "observed_at_seconds";
const FIELD_LIQUIDATION_CANCEL: &str = "liquidation_cancel";
const FIELD_LIQUIDATION_CANCEL_REASON: &str = "liquidation_cancel_reason";
const FIELD_OPERATOR_ALERT: &str = "operator_alert";
const FIELD_OPERATOR_ALERT_REASON: &str = "operator_alert_reason";

/// The complete set of keys a v1 lockout may declare. Anything else fails the read, for the reason
/// the sibling trigger store spells out: looking up only expected keys is structurally blind to a
/// misspelled or newer one, and here that blindness would silently drop a recovery-critical fact
/// from the record an operator resolves against.
const KNOWN_FIELDS: [&str; 11] = [
    FIELD_MAGIC,
    FIELD_SCHEMA_VERSION,
    FIELD_DEMOTING,
    FIELD_CANDIDATE,
    FIELD_ELAPSED,
    FIELD_TIMEOUT,
    FIELD_OBSERVED_AT,
    FIELD_LIQUIDATION_CANCEL,
    FIELD_LIQUIDATION_CANCEL_REASON,
    FIELD_OPERATOR_ALERT,
    FIELD_OPERATOR_ALERT_REASON,
];

/// Wire spelling of [`SideEffectOutcome::NotAttempted`].
const OUTCOME_NOT_ATTEMPTED: &str = "NOT_ATTEMPTED";
/// Wire spelling of [`SideEffectOutcome::Succeeded`].
const OUTCOME_SUCCEEDED: &str = "SUCCEEDED";
/// Wire spelling of [`SideEffectOutcome::Failed`].
const OUTCOME_FAILED: &str = "FAILED";

const SCRATCH_SUFFIX: &str = "hot-swap-demotion-pending.tmp";

/// Process-local monotonic counter disambiguating concurrent scratch files within one process
/// (combined with the pid for cross-process uniqueness). Not a clock or an RNG, so a save stays
/// reproducible.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// The facts an operator needs to resolve a demotion-pending lockout, persisted alongside it.
///
/// The two side-effect outcomes are carried because they are *recovery-critical and not recoverable
/// from anywhere else*: whether the unfilled liquidation order was actually cancelled decides
/// whether a live order may still be resting at IB, and whether the operator page went out decides
/// whether anyone was told. The `Err` returned by the gate names them too — a record that failed to
/// persist must not take those facts with it (SAFE-002 / ERR-8).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DemotionPendingRecord {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    /// What the liquidation probe reported before the deadline fired.
    pub elapsed_seconds: u64,
    /// The configured timeout that was breached.
    pub timeout_seconds: u64,
    pub observed_at_seconds: u64,
    /// Outcome of the SYS-49c (b) unfilled-liquidation-order cancel.
    pub liquidation_cancel: SideEffectOutcome,
    /// Outcome of the SYS-49c (a) dashboard/email/SMS operator page.
    pub operator_alert: SideEffectOutcome,
}

/// Whether promotion is blocked, and why — the ONLY shape the promotion path consults.
///
/// [`Unreadable`](Self::Unreadable) is a first-class blocking state rather than an error the caller
/// might handle by continuing. See the module docs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DemotionPendingState {
    /// No lockout is held. Promotion is not blocked *by this store*.
    Clear,
    /// A demotion timed out and has not been manually resolved. Promotion is blocked.
    Pending(Box<DemotionPendingRecord>),
    /// A lockout exists but cannot be read. Promotion is blocked (fail closed): an unreadable
    /// lockout is emphatically not an absent one.
    Unreadable { reason: String },
}

impl DemotionPendingState {
    /// The single predicate the promotion path branches on.
    ///
    /// Written as an explicit match rather than `!matches!(self, Self::Clear)` so that adding a
    /// state later forces a decision about it here instead of silently defaulting it to
    /// "promotion allowed".
    pub fn blocks_promotion(&self) -> bool {
        match self {
            Self::Clear => false,
            Self::Pending(_) | Self::Unreadable { .. } => true,
        }
    }

    /// A short operator-facing reason, for the rejection message and the dashboard cell.
    pub fn reason(&self) -> String {
        match self {
            Self::Clear => "no demotion is pending".to_string(),
            Self::Pending(record) => format!(
                "a demotion of {demoting} (candidate {candidate}) timed out after {elapsed} s of a \
                 {timeout} s budget and has not been manually resolved",
                demoting = record.demoting_strategy_id.as_str(),
                candidate = record.candidate_strategy_id.as_str(),
                elapsed = record.elapsed_seconds,
                timeout = record.timeout_seconds,
            ),
            Self::Unreadable { reason } => format!(
                "the demotion-pending lockout exists but cannot be read ({reason}) — treated as \
                 pending"
            ),
        }
    }
}

/// Why a persisted lockout could not be read, engaged, or resolved.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DemotionPendingStoreError {
    /// The file could not be read, written, or durably published.
    Io {
        action: &'static str,
        path: PathBuf,
        detail: String,
    },
    /// The file was reachable but is not a lockout this build can interpret.
    Malformed { path: PathBuf, reason: String },
    /// The file declares a layout outside this build's supported range.
    UnsupportedVersion { path: PathBuf, declared: i64 },
    /// [`engage`] found a lockout already held. Refused rather than overwritten, so the record the
    /// operator still has to act on is never replaced by a newer one.
    AlreadyPending {
        path: PathBuf,
        held: Box<DemotionPendingRecord>,
    },
    /// [`resolve`] was asked to clear a lockout that is not held. Reported rather than succeeding
    /// silently: telling an operator "resolved" when nothing was pending would let a *failed*
    /// engage read as a completed resolution.
    NotPending { path: PathBuf },
    /// [`resolve`] was asked to clear a lockout it cannot read. Removing it would discard the only
    /// description of the unresolved positions, so the error names the file for deliberate removal
    /// — the same posture `ExclusiveGuard` takes toward a stale lock.
    UnreadableResolution { path: PathBuf, reason: String },
    /// [`resolve`] was called without an operator acknowledgement.
    MissingAcknowledgement,
    /// The exclusive guard could not be taken.
    Locked { detail: String },
}

impl fmt::Display for DemotionPendingStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io {
                action,
                path,
                detail,
            } => write!(
                formatter,
                "SRS-RESV-004: cannot {action} demotion-pending lockout {}: {detail}",
                path.display()
            ),
            Self::Malformed { path, reason } => write!(
                formatter,
                "SRS-RESV-004: demotion-pending lockout {} is unreadable ({reason})",
                path.display()
            ),
            Self::UnsupportedVersion { path, declared } => write!(
                formatter,
                "SRS-RESV-004: demotion-pending lockout {} declares schema_version {declared}, \
                 outside the supported range [{MIN_SUPPORTED_DEMOTION_PENDING_SCHEMA_VERSION}, \
                 {DEMOTION_PENDING_SCHEMA_VERSION}]",
                path.display()
            ),
            Self::AlreadyPending { path, held } => write!(
                formatter,
                "SRS-RESV-004: demotion-pending lockout {} is already held for {demoting} \
                 (candidate {candidate}) — resolve it before engaging another; the held record \
                 describes positions that are still unresolved",
                path.display(),
                demoting = held.demoting_strategy_id.as_str(),
                candidate = held.candidate_strategy_id.as_str(),
            ),
            Self::NotPending { path } => write!(
                formatter,
                "SRS-RESV-004: no demotion-pending lockout is held at {} — nothing to resolve",
                path.display()
            ),
            Self::UnreadableResolution { path, reason } => write!(
                formatter,
                "SRS-RESV-004: demotion-pending lockout {} cannot be read ({reason}), so it \
                 cannot be resolved on its own terms. It still BLOCKS promotion. Inspect the \
                 unfilled IB positions and remove the file deliberately",
                path.display()
            ),
            Self::MissingAcknowledgement => write!(
                formatter,
                "SRS-RESV-004: resolving a demotion-pending lockout requires an operator \
                 acknowledgement — the unfilled positions are confirmed by a person, not by a flag"
            ),
            Self::Locked { detail } => write!(formatter, "SRS-RESV-004: {detail}"),
        }
    }
}

impl std::error::Error for DemotionPendingStoreError {}

fn io_error(
    action: &'static str,
    path: &Path,
    error: &std::io::Error,
) -> DemotionPendingStoreError {
    DemotionPendingStoreError::Io {
        action,
        path: path.to_path_buf(),
        detail: error.to_string(),
    }
}

fn malformed(path: &Path, reason: impl Into<String>) -> DemotionPendingStoreError {
    DemotionPendingStoreError::Malformed {
        path: path.to_path_buf(),
        reason: reason.into(),
    }
}

/// Escape `value` for a JSON string body, covering **every** C0 control character.
///
/// A hand-rolled escaper that handles only the five obvious characters poisons its own reader the
/// first time a strategy id carries a stray control byte — and this record is written *after* the
/// safety side effects ran, so a corrupt payload suppresses the very evidence of them
/// (SAFE-002 r3 / DATA-020). `StrategyId::new` performs no validation, so nothing upstream
/// guarantees the id is representable.
fn json_escape(value: &str) -> String {
    let mut escaped = String::with_capacity(value.len());
    for character in value.chars() {
        match character {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            '\u{08}' => escaped.push_str("\\b"),
            '\u{0C}' => escaped.push_str("\\f"),
            control if (control as u32) < 0x20 => {
                escaped.push_str(&format!("\\u{:04x}", control as u32));
            }
            other => escaped.push(other),
        }
    }
    escaped
}

/// Decode the body of a JSON string token back into the text [`json_escape`] was given.
///
/// `json_string_value` deliberately returns the contents **still escaped** — for the fixed
/// tokens its other callers compare (a magic marker, an enum label) that is the stronger
/// behaviour, since an escaped spelling is not something this writer emits and should be
/// refused. But the identity and reason fields here carry arbitrary operator-visible text, and
/// reading them raw would hand back `live-mom` as a *literal backslash-u* strategy id:
/// the writer would no longer satisfy its own reader, and the id on the dashboard would not be
/// the id in the system.
///
/// Strict, and fail-closed on anything this writer would never produce: an unknown escape, a
/// truncated or non-hex `\uXXXX`, a surrogate escape (the escaper emits `\u00XX` for C0
/// controls only and passes every other character through as UTF-8, so a surrogate half means a
/// foreign payload), or a raw control character.
fn json_unescape(raw: &str) -> Option<String> {
    let mut decoded = String::with_capacity(raw.len());
    let mut chars = raw.chars();
    while let Some(character) = chars.next() {
        if (character as u32) < 0x20 {
            // The writer escapes every C0 control, so a raw one is corruption.
            return None;
        }
        if character != '\\' {
            decoded.push(character);
            continue;
        }
        match chars.next()? {
            '"' => decoded.push('"'),
            '\\' => decoded.push('\\'),
            '/' => decoded.push('/'),
            'b' => decoded.push('\u{08}'),
            'f' => decoded.push('\u{0C}'),
            'n' => decoded.push('\n'),
            'r' => decoded.push('\r'),
            't' => decoded.push('\t'),
            'u' => {
                let mut hex = String::with_capacity(4);
                for _ in 0..4 {
                    hex.push(chars.next()?);
                }
                let code = u32::from_str_radix(&hex, 16).ok()?;
                // Surrogate halves are unrepresentable on their own and are never emitted
                // here; refuse rather than substitute U+FFFD and silently alter an identity.
                if (0xD800..=0xDFFF).contains(&code) {
                    return None;
                }
                decoded.push(char::from_u32(code)?);
            }
            _ => return None,
        }
    }
    Some(decoded)
}

/// The wire spelling and optional reason of a side-effect outcome.
fn outcome_parts(outcome: &SideEffectOutcome) -> (&'static str, Option<&str>) {
    match outcome {
        SideEffectOutcome::NotAttempted => (OUTCOME_NOT_ATTEMPTED, None),
        SideEffectOutcome::Succeeded => (OUTCOME_SUCCEEDED, None),
        SideEffectOutcome::Failed { reason } => (OUTCOME_FAILED, Some(reason.as_str())),
    }
}

/// Serialize `record` to the single-line v1 JSON payload this module persists.
///
/// A `*_reason` is written **iff** its outcome is `FAILED`, and [`load`] refuses the two
/// contradictory combinations rather than picking which half to believe — the same
/// present-iff-meaningful discipline the trigger store applies to the drawdown threshold.
pub fn serialize(record: &DemotionPendingRecord) -> String {
    let (cancel, cancel_reason) = outcome_parts(&record.liquidation_cancel);
    let (alert, alert_reason) = outcome_parts(&record.operator_alert);
    let mut payload = String::new();
    payload.push('{');
    payload.push_str(&format!("\"{FIELD_MAGIC}\":\"{MAGIC}\","));
    payload.push_str(&format!(
        "\"{FIELD_SCHEMA_VERSION}\":{DEMOTION_PENDING_SCHEMA_VERSION},"
    ));
    payload.push_str(&format!(
        "\"{FIELD_DEMOTING}\":\"{}\",",
        json_escape(record.demoting_strategy_id.as_str())
    ));
    payload.push_str(&format!(
        "\"{FIELD_CANDIDATE}\":\"{}\",",
        json_escape(record.candidate_strategy_id.as_str())
    ));
    payload.push_str(&format!("\"{FIELD_ELAPSED}\":{},", record.elapsed_seconds));
    payload.push_str(&format!("\"{FIELD_TIMEOUT}\":{},", record.timeout_seconds));
    payload.push_str(&format!(
        "\"{FIELD_OBSERVED_AT}\":{},",
        record.observed_at_seconds
    ));
    payload.push_str(&format!("\"{FIELD_LIQUIDATION_CANCEL}\":\"{cancel}\","));
    if let Some(reason) = cancel_reason {
        payload.push_str(&format!(
            "\"{FIELD_LIQUIDATION_CANCEL_REASON}\":\"{}\",",
            json_escape(reason)
        ));
    }
    payload.push_str(&format!("\"{FIELD_OPERATOR_ALERT}\":\"{alert}\""));
    if let Some(reason) = alert_reason {
        payload.push_str(&format!(
            ",\"{FIELD_OPERATOR_ALERT_REASON}\":\"{}\"",
            json_escape(reason)
        ));
    }
    payload.push('}');
    payload
}

/// Parse a v1 payload, refusing anything this build cannot interpret unambiguously.
fn deserialize(
    path: &Path,
    payload: &str,
) -> Result<DemotionPendingRecord, DemotionPendingStoreError> {
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

    let field = |key: &str| -> Result<Option<&str>, DemotionPendingStoreError> {
        top_level_json_field(payload, key)
            .map_err(|_| malformed(path, "payload is not a single well-formed JSON object"))
    };
    let required = |key: &'static str| -> Result<&str, DemotionPendingStoreError> {
        field(key)?.ok_or_else(|| malformed(path, format!("payload is missing field '{key}'")))
    };

    // Identity before meaning: a file that is not ours has no fields worth reading.
    let magic_raw = required(FIELD_MAGIC)?;
    let magic = json_string_value(magic_raw)
        .ok_or_else(|| malformed(path, format!("field '{FIELD_MAGIC}' is not a JSON string")))?;
    if magic != MAGIC {
        return Err(malformed(
            path,
            format!("payload carries magic {magic:?}, expected {MAGIC:?}"),
        ));
    }

    // Layout before fields: an unsupported version means the field meanings below are not this
    // build's to assume.
    let version_raw = required(FIELD_SCHEMA_VERSION)?;
    let declared = parse_strict_i64(version_raw).ok_or_else(|| {
        malformed(
            path,
            format!("field '{FIELD_SCHEMA_VERSION}' is not a JSON integer"),
        )
    })?;
    if !(MIN_SUPPORTED_DEMOTION_PENDING_SCHEMA_VERSION..=DEMOTION_PENDING_SCHEMA_VERSION)
        .contains(&declared)
    {
        return Err(DemotionPendingStoreError::UnsupportedVersion {
            path: path.to_path_buf(),
            declared,
        });
    }

    // A blank strategy id names nobody. The record exists so an operator can act on a specific
    // strategy's unfilled positions; one that cannot say whose is not a usable lockout, and
    // accepting it would put an empty string on the dashboard where a strategy id belongs.
    let text = |key: &'static str| -> Result<String, DemotionPendingStoreError> {
        let raw = required(key)?;
        let escaped = json_string_value(raw)
            .ok_or_else(|| malformed(path, format!("field '{key}' is not a JSON string")))?;
        json_unescape(escaped).ok_or_else(|| {
            malformed(
                path,
                format!("field '{key}' carries an escape sequence this build cannot decode"),
            )
        })
    };

    let identity = |key: &'static str| -> Result<StrategyId, DemotionPendingStoreError> {
        let value = text(key)?;
        if value.trim().is_empty() {
            return Err(malformed(path, format!("field '{key}' is blank")));
        }
        Ok(StrategyId::new(value))
    };

    let count = |key: &'static str| -> Result<u64, DemotionPendingStoreError> {
        let raw = required(key)?;
        let value = parse_strict_i64(raw)
            .ok_or_else(|| malformed(path, format!("field '{key}' is not a JSON integer")))?;
        u64::try_from(value)
            .map_err(|_| malformed(path, format!("field '{key}' ({value}) is negative")))
    };

    let outcome = |key: &'static str,
                   reason_key: &'static str|
     -> Result<SideEffectOutcome, DemotionPendingStoreError> {
        let raw = required(key)?;
        // Compared as the writer's RAW token: this is a closed vocabulary, and an escaped
        // spelling of `SUCCEEDED` is not something this writer emits, so it is refused as
        // unknown rather than decoded into a match.
        let label = json_string_value(raw)
            .ok_or_else(|| malformed(path, format!("field '{key}' is not a JSON string")))?;
        // The reason, by contrast, is free text and must round-trip.
        let reason = match field(reason_key)? {
            Some(_) => Some(text(reason_key)?),
            None => None,
        };
        match (label, reason) {
            (OUTCOME_FAILED, Some(reason)) => Ok(SideEffectOutcome::Failed { reason }),
            (OUTCOME_FAILED, None) => Err(malformed(
                path,
                format!(
                    "'{key}' is {OUTCOME_FAILED} but '{reason_key}' is absent — a recorded failure \
                     with no reason cannot tell an operator what to recover"
                ),
            )),
            (OUTCOME_NOT_ATTEMPTED | OUTCOME_SUCCEEDED, Some(_)) => Err(malformed(
                path,
                format!(
                    "'{key}' is not {OUTCOME_FAILED} but '{reason_key}' is present — the payload \
                     states the outcome twice, and disagrees"
                ),
            )),
            (OUTCOME_NOT_ATTEMPTED, None) => Ok(SideEffectOutcome::NotAttempted),
            (OUTCOME_SUCCEEDED, None) => Ok(SideEffectOutcome::Succeeded),
            (other, _) => Err(malformed(
                path,
                format!(
                    "field '{key}' declares unknown outcome {other:?} (expected \
                     {OUTCOME_NOT_ATTEMPTED}, {OUTCOME_SUCCEEDED}, or {OUTCOME_FAILED})"
                ),
            )),
        }
    };

    Ok(DemotionPendingRecord {
        demoting_strategy_id: identity(FIELD_DEMOTING)?,
        candidate_strategy_id: identity(FIELD_CANDIDATE)?,
        elapsed_seconds: count(FIELD_ELAPSED)?,
        timeout_seconds: count(FIELD_TIMEOUT)?,
        observed_at_seconds: count(FIELD_OBSERVED_AT)?,
        liquidation_cancel: outcome(FIELD_LIQUIDATION_CANCEL, FIELD_LIQUIDATION_CANCEL_REASON)?,
        operator_alert: outcome(FIELD_OPERATOR_ALERT, FIELD_OPERATOR_ALERT_REASON)?,
    })
}

/// Read the persisted lockout at `path`, honestly three-state.
///
/// Prefer [`read_state`] on any path that decides whether promotion may proceed: this function
/// hands the fail-closed decision to its caller, and there is exactly one correct answer.
pub fn load(path: &Path) -> Result<Option<DemotionPendingRecord>, DemotionPendingStoreError> {
    let payload = match fs::read_to_string(path) {
        Ok(payload) => payload,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error("read", path, &error)),
    };
    // A file that exists but holds only whitespace is a torn or truncated write, not an absent
    // lockout — the one case where "empty" must not read as "nothing is pending".
    if payload.trim().is_empty() {
        return Err(malformed(path, "file is empty"));
    }
    deserialize(path, payload.trim()).map(Some)
}

/// The promotion path's view of the lockout, with the fail-closed collapse applied **here**.
///
/// An unreadable lockout becomes [`DemotionPendingState::Unreadable`], which
/// [`DemotionPendingState::blocks_promotion`] treats exactly like a held one. This is the single
/// place that decision is made; see the module docs for why it is not the caller's.
pub fn read_state(path: &Path) -> DemotionPendingState {
    match load(path) {
        Ok(None) => DemotionPendingState::Clear,
        Ok(Some(record)) => DemotionPendingState::Pending(Box::new(record)),
        Err(error) => DemotionPendingState::Unreadable {
            reason: error.to_string(),
        },
    }
}

/// Engage the lockout: persist `record` at `path`, refusing if one is already held.
///
/// Crash-durable and atomically published — scratch file → `fsync` → `rename` → parent `fsync` —
/// for the reason the whole module exists: a lockout that evaporated on power loss would let the
/// next promotion proceed over positions nobody resolved.
///
/// The existing-lockout check and the write are both taken under one [`ExclusiveGuard`], so a
/// concurrent surface cannot slip a second engage between them.
pub fn engage(
    path: &Path,
    record: &DemotionPendingRecord,
) -> Result<(), DemotionPendingStoreError> {
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|error| {
        DemotionPendingStoreError::Locked {
            detail: error.to_string(),
        }
    })?;
    // Read THROUGH the guard, and read fail-closed: an unreadable lockout is still a lockout, so
    // an engage must not overwrite one it merely could not parse.
    match load(path) {
        Ok(None) => {}
        Ok(Some(held)) => {
            return Err(DemotionPendingStoreError::AlreadyPending {
                path: path.to_path_buf(),
                held: Box::new(held),
            });
        }
        Err(error) => return Err(error),
    }
    write_atomically(path, record)
}

/// Update the record of a lockout that is ALREADY held, keeping the block continuous.
///
/// The two-phase write exists because of a crash window. The gate used to cancel the unfilled
/// order and page the operator and only THEN engage, so the record could carry their outcomes —
/// but a process that died in between left no lockout at all, and the next process read `Clear`
/// and could promote over positions nobody resolved. `(found by /codex:adversarial-review,
/// SRS-RESV-004 r4 [high])`
///
/// So the gate engages FIRST, with both outcomes `NotAttempted`, which blocks promotion from
/// that instant; then it runs the side effects; then it amends. A crash anywhere after phase one
/// leaves a lockout that blocks — with a record that understates what was attempted, which is
/// the safe direction to be wrong in.
///
/// Amending REQUIRES an existing record for the same strategies: this is not a second engage,
/// and it must never bring a lockout into being that the gate believes it already created.
pub fn amend(path: &Path, record: &DemotionPendingRecord) -> Result<(), DemotionPendingStoreError> {
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|error| {
        DemotionPendingStoreError::Locked {
            detail: error.to_string(),
        }
    })?;
    let held = match load(path) {
        Ok(Some(held)) => held,
        Ok(None) => {
            return Err(DemotionPendingStoreError::NotPending {
                path: path.to_path_buf(),
            });
        }
        Err(error) => return Err(error),
    };
    if held.demoting_strategy_id != record.demoting_strategy_id
        || held.candidate_strategy_id != record.candidate_strategy_id
    {
        return Err(DemotionPendingStoreError::AlreadyPending {
            path: path.to_path_buf(),
            held: Box::new(held),
        });
    }
    write_atomically(path, record)
}

/// The manual resolution SYS-49c (d) requires: clear the lockout so promotion may proceed again.
///
/// `acknowledgement` is the operator's statement that they have inspected and resolved the unfilled
/// IB positions. It is required and must be non-blank — this is the control that stands between an
/// automated retry and a live position nobody looked at, so it cannot be a boolean an automation
/// sets. Returns the record that was cleared, so the caller can record what was resolved.
///
/// Deliberately NOT reachable through the [`crate::DemotionPendingLock`] port: see the module docs.
pub fn resolve(
    path: &Path,
    acknowledgement: &str,
) -> Result<DemotionPendingRecord, DemotionPendingStoreError> {
    if acknowledgement.trim().is_empty() {
        return Err(DemotionPendingStoreError::MissingAcknowledgement);
    }
    let _guard = ExclusiveGuard::acquire_creating(path).map_err(|error| {
        DemotionPendingStoreError::Locked {
            detail: error.to_string(),
        }
    })?;
    let held = match load(path) {
        Ok(Some(record)) => record,
        Ok(None) => {
            return Err(DemotionPendingStoreError::NotPending {
                path: path.to_path_buf(),
            });
        }
        // Removing a lockout this build cannot read would discard the only description of the
        // unresolved positions. Refuse, and keep blocking, naming the file for deliberate removal.
        Err(error) => {
            return Err(DemotionPendingStoreError::UnreadableResolution {
                path: path.to_path_buf(),
                reason: error.to_string(),
            });
        }
    };
    fs::remove_file(path).map_err(|error| io_error("clear", path, &error))?;
    // fsync the directory so the unlink is itself durable: a lockout that reappeared after a power
    // loss would block a promotion the operator already cleared.
    let dir = parent_dir(path);
    let handle =
        fs::File::open(&dir).map_err(|error| io_error("open directory of", path, &error))?;
    handle
        .sync_all()
        .map_err(|error| io_error("sync directory of", path, &error))?;
    Ok(held)
}

/// The production [`crate::DemotionPendingLock`]: the gate's view of a lockout that lives on disk
/// at `path`.
///
/// The fail-closed collapse is [`read_state`]'s, not this type's — there is one place an
/// unreadable lockout becomes a blocking one, and this adapter does not get to have an opinion.
/// It carries no `resolve`, because the port it implements has none: clearing a lockout is
/// [`resolve`], reached only from the operator CLI.
#[derive(Debug, Clone)]
pub struct FileDemotionPendingLock {
    path: PathBuf,
    /// Set when an `engage` FAILED. See [`state`](Self::state).
    poison: Arc<Mutex<Option<String>>>,
}

impl FileDemotionPendingLock {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            poison: Arc::new(Mutex::new(None)),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// Why this lock is refusing everything, if it is.
    pub fn poisoned(&self) -> Option<String> {
        self.poison
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }
}

impl crate::DemotionPendingLock for FileDemotionPendingLock {
    /// The lockout state — or a BLOCKING state if a previous engage could not persist.
    ///
    /// The failure this closes: on timeout the gate engages a lockout, and if that write fails
    /// it returns `Err` with `promotion_block_is_durable = false`. But the store is then still
    /// EMPTY, so the very next attempt reads `Clear`, and a probe reporting flat is accepted —
    /// promotion proceeds over positions no operator resolved, which is precisely what SyRS
    /// SYS-49c (d) forbids. Saying "not durable" on the way out is a description of the hole,
    /// not a closure of it. `(found by /codex:adversarial-review, SRS-RESV-004 r3 [high])`
    ///
    /// So a failed engage POISONS this lock: every later `state()` reports the blocking
    /// `Unreadable` variant — a demotion is pending and this build cannot evidence it — until
    /// the operator resolves. The poison is shared across clones (`Arc`), so a composition that
    /// hands the lock out by value cannot lose it.
    ///
    /// **Residual, stated rather than implied:** the poison lives in memory, so a process
    /// RESTART over a store that is still unwritable begins clear again. Closing that needs a
    /// second durable location to fail over to, which is the deferred SRS-LOG-001 /
    /// SRS-ARCH-005 startup-readiness leg (`hot_swap_demotion_contract.deferred[]`). The
    /// operator has been paged by then — the alert dispatch precedes the engage — and the
    /// rejection says the block is not durable.
    fn state(&self) -> DemotionPendingState {
        if let Some(reason) = self.poisoned() {
            return DemotionPendingState::Unreadable { reason };
        }
        read_state(&self.path)
    }

    fn engage(&self, record: DemotionPendingRecord) -> Result<(), crate::HotSwapSideEffectError> {
        match engage(&self.path, &record) {
            Ok(()) => Ok(()),
            // A lockout is ALREADY held: the block this engage wanted exists, and the store
            // enforces it on its own. Poisoning here would outlive the operator's resolution
            // and wedge the swap path permanently — a fail-closed that never reopens is its
            // own outage, and it would be indistinguishable from the real persistence fault.
            Err(DemotionPendingStoreError::AlreadyPending { path, held }) => {
                Err(crate::HotSwapSideEffectError::new(
                    DemotionPendingStoreError::AlreadyPending { path, held }.to_string(),
                ))
            }
            Err(error) => {
                let reason = format!(
                    "a demotion of {demoting} (candidate {candidate}) timed out and its                      demotion-pending lockout could NOT be persisted ({error}); promotion stays                      blocked in this process until an operator resolves the open positions",
                    demoting = record.demoting_strategy_id.as_str(),
                    candidate = record.candidate_strategy_id.as_str(),
                );
                *self
                    .poison
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(reason);
                Err(crate::HotSwapSideEffectError::new(error.to_string()))
            }
        }
    }

    /// Amend the record this lock already engaged. Never poisons: the block is already held,
    /// so a failure here costs detail on the record, not the block itself.
    fn amend(&self, record: DemotionPendingRecord) -> Result<(), crate::HotSwapSideEffectError> {
        amend(&self.path, &record)
            .map_err(|error| crate::HotSwapSideEffectError::new(error.to_string()))
    }
}

/// The directory a path lives in. A relative `demotion-pending.json` has an EMPTY parent, not an
/// absent one, and skipping the fsync of `.` would leave the publish non-durable.
fn parent_dir(path: &Path) -> PathBuf {
    match path.parent() {
        Some(parent) if !parent.as_os_str().is_empty() => parent.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

fn write_atomically(
    path: &Path,
    record: &DemotionPendingRecord,
) -> Result<(), DemotionPendingStoreError> {
    let dir = parent_dir(path);
    let dir = dir.as_path();
    fs::create_dir_all(dir).map_err(|error| io_error("create directory for", path, &error))?;
    let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
    let scratch_name = format!(
        "{}.{}.{seq}.{SCRATCH_SUFFIX}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("hot-swap-demotion-pending"),
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
