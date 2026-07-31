//! # SRS-RESV-003 durable Hot-Swap trigger configuration (SyRS SYS-49a)
//!
//! The acceptance criterion has three clauses — the four triggers are *configurable*, automatic
//! triggers *default to disabled*, and all swap triggers are *logged*. The trigger decision layer in
//! [`crate::StrategyOrchestrator`] and the audit log in `resv003_hot_swap_trigger_cli` own the first
//! and third. This module owns the part that makes "configurable" mean anything across a restart:
//! the configuration has to survive as bytes, and a later reader has to be able to say what it says.
//!
//! ## Why the read is three-state, not two
//!
//! [`HotSwapTriggerConfig`] derives `Default` as all-automatic-disabled, so it is tempting to fold a
//! read failure into that default — "we could not read it, so nothing is enabled, which is the safe
//! side". It is not the safe side. Those are three different facts:
//!
//! * **No file** → nothing has ever been configured. The default genuinely applies, and the operator
//!   surface can say *disabled* truthfully. [`load`] returns `Ok(None)`.
//! * **A readable file** → the operator's configuration. [`load`] returns `Ok(Some(config))`.
//! * **An unreadable file** → the configuration exists and this build cannot say what it is.
//!   [`load`] returns [`Err`].
//!
//! Collapsing the third into the first would let a corrupt, truncated, or newer-format config render
//! on the dashboard as a confident "automatic triggers: disabled" — a false all-clear about whether
//! an automatic demotion or promotion can fire, which is exactly the claim an operator would check
//! this pane to confirm. The caller must surface unknown as unknown.
//!
//! ## Why every unrecognised key is refused
//!
//! Reading only the keys it expects would make this reader structurally blind to the ones it does
//! not: a `top_ranked_promotion_enabld` typo reads as "not enabled", and a flag a newer build made
//! meaningful is dropped while the rest of the record still parses cleanly. Both are silent, and both
//! are wrong in the direction that matters. So the field set is an exact allow-list
//! ([`top_level_json_keys`]) and an unknown key fails the read.

use atp_types::json_scan::{
    json_string_value, parse_strict_bool, parse_strict_i64, top_level_json_field,
    top_level_json_keys,
};
use atp_types::{
    DrawdownDemotionTrigger, DrawdownThresholdBps, HotSwapTriggerConfig, RankingPromotionTrigger,
};
use std::fmt;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

/// Magic marker every persisted trigger configuration carries, so a foreign JSON file pointed at
/// this reader by a misconfiguration is refused before any flag is believed.
pub const MAGIC: &str = "ATP-HOT-SWAP-TRIGGER-CONFIG";

/// The configuration layout this build WRITES (SRS-DATA-015 / SyRS SYS-66).
///
/// **Version history:** v1 = `{magic, schema_version, drawdown_demotion_enabled,
/// drawdown_demotion_threshold_bps?, top_ranked_promotion_enabled,
/// highest_momentum_promotion_enabled}`.
///
/// Unlike the sibling trigger *log*, this format has carried its version since its first byte —
/// there are no pre-versioning configurations in existence, so there is no version-less payload to
/// stay compatible with and no legacy floor to read at.
pub const TRIGGER_CONFIG_SCHEMA_VERSION: i64 = 1;

/// The oldest configuration layout this build still READS.
pub const MIN_SUPPORTED_TRIGGER_CONFIG_SCHEMA_VERSION: i64 = 1;

const FIELD_MAGIC: &str = "magic";
const FIELD_SCHEMA_VERSION: &str = "schema_version";
const FIELD_DRAWDOWN_ENABLED: &str = "drawdown_demotion_enabled";
const FIELD_DRAWDOWN_THRESHOLD: &str = "drawdown_demotion_threshold_bps";
const FIELD_TOP_RANKED: &str = "top_ranked_promotion_enabled";
const FIELD_HIGHEST_MOMENTUM: &str = "highest_momentum_promotion_enabled";

/// The complete set of keys a v1 configuration may declare. Anything else fails the read.
///
/// Keys are compared as the writer's raw (still-escaped) token text, so a key spelled with a
/// `e` escape does not match its plain form and is refused as unknown — the fail-closed
/// direction: an escaped key is not something this writer ever produces.
const KNOWN_FIELDS: [&str; 6] = [
    FIELD_MAGIC,
    FIELD_SCHEMA_VERSION,
    FIELD_DRAWDOWN_ENABLED,
    FIELD_DRAWDOWN_THRESHOLD,
    FIELD_TOP_RANKED,
    FIELD_HIGHEST_MOMENTUM,
];

/// Base name of the scratch file an atomic save writes (and fsyncs) before renaming it onto the
/// caller's path. The real scratch name appends a `<pid>.<seq>` suffix so two writers persisting
/// into the same directory cannot rename over each other's scratch file.
const SCRATCH_SUFFIX: &str = "hot-swap-trigger-config.tmp";

/// Process-local monotonic counter disambiguating concurrent scratch files within one process
/// (combined with the pid for cross-process uniqueness). Not a clock or an RNG, so a save stays
/// reproducible.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// How long [`ExclusiveGuard::acquire`] waits for a contended lock before failing closed.
const LOCK_TIMEOUT: Duration = Duration::from_secs(10);

/// How long to wait between attempts while a lock is held by someone else.
const LOCK_POLL_INTERVAL: Duration = Duration::from_millis(10);

/// A cross-process mutual-exclusion guard over one path, released on drop.
///
/// ## Why this exists
///
/// Two operations here are read-modify-write, and neither is safe to interleave:
///
/// * **Changing one trigger** loads the whole configuration, edits one field, and saves the whole
///   thing back. Two concurrent changes to *different* triggers both read the old configuration and
///   both write a full replacement, so the second silently discards the first — and both callers are
///   told they succeeded. An operator who armed drawdown demotion would be shown a success for a
///   change that no longer exists on disk.
/// * **Firing a trigger** appends one audit record and then counts the log to learn which position
///   it landed at. The append is atomic (`O_APPEND`), but the count is not part of it: a concurrent
///   append lands between the two, and the reported ordinal then addresses somebody else's record.
///   That ordinal is what the REST surface hands back as the trigger's identity, so the audit trail
///   would attribute a fired trigger to the wrong request.
///
/// Both funnel through this binary — the CLI, the REST arm, and the dashboard all shell it — so
/// serialising here covers every arm at once, across threads *and* processes. A `std::sync::Mutex`
/// would not: the threaded REST server and a concurrent operator CLI are different processes.
///
/// ## Mechanism
///
/// A `<path>.lock` sibling created with `create_new` (`O_EXCL`), which the OS makes atomic: exactly
/// one creator wins. Contenders retry until [`LOCK_TIMEOUT`], then fail closed with an explicit
/// error rather than proceeding unserialised — refusing an operation is recoverable, corrupting the
/// configuration or the audit trail is not.
///
/// A crashed holder leaves the lock file behind, and this guard deliberately does NOT break a lock
/// it finds: silently stealing one would reintroduce exactly the race it exists to prevent. The
/// error names the file so an operator can remove it deliberately.
#[derive(Debug)]
pub struct ExclusiveGuard {
    lock_path: PathBuf,
}

impl ExclusiveGuard {
    /// Take the lock for `path`, creating its parent directory if absent.
    ///
    /// For the CONFIGURATION path, where [`save`] creates the directory anyway — the guard must
    /// not turn a first-ever write into a failure.
    pub fn acquire_creating(path: &Path) -> Result<Self, TriggerConfigStoreError> {
        let lock_path = lock_path_for(path);
        if let Some(parent) = lock_path.parent().filter(|p| !p.as_os_str().is_empty()) {
            fs::create_dir_all(parent)
                .map_err(|error| io_error("create directory for", path, &error))?;
        }
        Self::acquire_at(path, lock_path)
    }

    /// Take the lock for `path`, or `Ok(None)` when its parent directory does not exist.
    ///
    /// For the AUDIT-LOG path, and the absent-parent case is deliberately not an error here. This
    /// guard must not bring a directory into being: a `--log` pointed at a mistyped path has to
    /// keep failing at the append, with the sink's own concrete cause, exactly as it did before
    /// locking existed. (An earlier draft called `create_dir_all` unconditionally and quietly
    /// turned three fail-closed tests green by manufacturing the very directory whose absence
    /// they relied on.)
    ///
    /// Skipping the lock is safe precisely in that case: with no parent directory there is no log
    /// file, so no concurrent writer can hold one and there is nothing to serialise — and the
    /// append that follows will fail regardless.
    pub fn acquire_if_parent_exists(path: &Path) -> Result<Option<Self>, TriggerConfigStoreError> {
        let lock_path = lock_path_for(path);
        let parent_missing = lock_path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .is_some_and(|parent| !parent.is_dir());
        if parent_missing {
            return Ok(None);
        }
        Self::acquire_at(path, lock_path).map(Some)
    }

    fn acquire_at(path: &Path, lock_path: PathBuf) -> Result<Self, TriggerConfigStoreError> {
        let deadline = Instant::now() + LOCK_TIMEOUT;
        loop {
            match fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&lock_path)
            {
                Ok(_) => return Ok(Self { lock_path }),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    if Instant::now() >= deadline {
                        return Err(TriggerConfigStoreError::Locked {
                            lock_path,
                            waited: LOCK_TIMEOUT,
                        });
                    }
                    std::thread::sleep(LOCK_POLL_INTERVAL);
                }
                Err(error) => return Err(io_error("lock", path, &error)),
            }
        }
    }
}

impl Drop for ExclusiveGuard {
    fn drop(&mut self) {
        // Best-effort: a failure to unlink leaves the lock held, which the next operator sees as an
        // explicit refusal naming the file — never as a silently unserialised write.
        let _ = fs::remove_file(&self.lock_path);
    }
}

/// The lock sibling for `path` (`<path>.lock`).
fn lock_path_for(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".lock");
    match path.parent().filter(|p| !p.as_os_str().is_empty()) {
        Some(parent) => parent.join(name),
        None => PathBuf::from(name),
    }
}

/// Why a persisted trigger configuration could not be read or written.
///
/// [`Malformed`](Self::Malformed) and [`UnsupportedVersion`](Self::UnsupportedVersion) are kept
/// distinct from [`Io`](Self::Io) because they mean different things to an operator: the bytes are
/// there but wrong, versus the bytes could not be reached at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TriggerConfigStoreError {
    /// The file could not be read, written, or durably published.
    Io {
        /// What was being attempted.
        action: &'static str,
        /// The path involved.
        path: PathBuf,
        /// The underlying `std::io` message.
        detail: String,
    },
    /// The file was reachable but is not a configuration this build can interpret.
    Malformed {
        /// The path involved.
        path: PathBuf,
        /// Why the payload was refused.
        reason: String,
    },
    /// The file declares a layout outside this build's supported range.
    UnsupportedVersion {
        /// The path involved.
        path: PathBuf,
        /// The version the payload declared.
        declared: i64,
    },
    /// Another operation holds the exclusive lock and did not release it in time.
    Locked {
        /// The lock file that is held.
        lock_path: PathBuf,
        /// How long this attempt waited before giving up.
        waited: Duration,
    },
}

impl fmt::Display for TriggerConfigStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io {
                action,
                path,
                detail,
            } => write!(
                formatter,
                "SRS-RESV-003: cannot {action} trigger configuration {}: {detail}",
                path.display()
            ),
            Self::Malformed { path, reason } => write!(
                formatter,
                "SRS-RESV-003: trigger configuration {} is unreadable ({reason})",
                path.display()
            ),
            Self::UnsupportedVersion { path, declared } => write!(
                formatter,
                "SRS-RESV-003: trigger configuration {} declares schema_version {declared}, \
                 outside the supported range [{MIN_SUPPORTED_TRIGGER_CONFIG_SCHEMA_VERSION}, \
                 {TRIGGER_CONFIG_SCHEMA_VERSION}]",
                path.display()
            ),
            Self::Locked { lock_path, waited } => write!(
                formatter,
                "SRS-RESV-003: another Hot-Swap trigger operation holds {} (waited {:.1}s). \
                 Retry; if no operation is running, a previous one crashed — remove the file \
                 deliberately rather than letting two writers interleave",
                lock_path.display(),
                waited.as_secs_f64()
            ),
        }
    }
}

impl std::error::Error for TriggerConfigStoreError {}

fn io_error(action: &'static str, path: &Path, error: &std::io::Error) -> TriggerConfigStoreError {
    TriggerConfigStoreError::Io {
        action,
        path: path.to_path_buf(),
        detail: error.to_string(),
    }
}

fn malformed(path: &Path, reason: impl Into<String>) -> TriggerConfigStoreError {
    TriggerConfigStoreError::Malformed {
        path: path.to_path_buf(),
        reason: reason.into(),
    }
}

/// Serialize `config` to the single-line v1 JSON payload this module persists.
///
/// The drawdown threshold is written **iff** the drawdown trigger is enabled. A threshold left
/// alongside a disabled trigger would be a second, contradictory statement about the same fact, and
/// [`load`] refuses that combination rather than picking which half to believe.
pub fn serialize(config: &HotSwapTriggerConfig) -> String {
    let mut payload = String::new();
    payload.push('{');
    payload.push_str(&format!("\"{FIELD_MAGIC}\":\"{MAGIC}\","));
    payload.push_str(&format!(
        "\"{FIELD_SCHEMA_VERSION}\":{TRIGGER_CONFIG_SCHEMA_VERSION},"
    ));
    payload.push_str(&format!(
        "\"{FIELD_DRAWDOWN_ENABLED}\":{},",
        config.drawdown_demotion.is_enabled()
    ));
    if let Some(threshold) = config.drawdown_demotion.threshold() {
        payload.push_str(&format!(
            "\"{FIELD_DRAWDOWN_THRESHOLD}\":{},",
            threshold.get()
        ));
    }
    payload.push_str(&format!(
        "\"{FIELD_TOP_RANKED}\":{},",
        config.top_ranked_promotion.is_enabled()
    ));
    payload.push_str(&format!(
        "\"{FIELD_HIGHEST_MOMENTUM}\":{}",
        config.highest_momentum_promotion.is_enabled()
    ));
    payload.push('}');
    payload
}

/// Parse a v1 payload, refusing anything this build cannot interpret unambiguously.
fn deserialize(
    path: &Path,
    payload: &str,
) -> Result<HotSwapTriggerConfig, TriggerConfigStoreError> {
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

    let field = |key: &str| -> Result<Option<&str>, TriggerConfigStoreError> {
        top_level_json_field(payload, key)
            .map_err(|_| malformed(path, "payload is not a single well-formed JSON object"))
    };
    let required = |key: &'static str| -> Result<&str, TriggerConfigStoreError> {
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
    // build's to assume. Absence is NOT a legacy floor here — this format has always been versioned,
    // so a payload with no version is malformed, not old.
    let version_raw = required(FIELD_SCHEMA_VERSION)?;
    let declared = parse_strict_i64(version_raw).ok_or_else(|| {
        malformed(
            path,
            format!("field '{FIELD_SCHEMA_VERSION}' is not a JSON integer"),
        )
    })?;
    if !(MIN_SUPPORTED_TRIGGER_CONFIG_SCHEMA_VERSION..=TRIGGER_CONFIG_SCHEMA_VERSION)
        .contains(&declared)
    {
        return Err(TriggerConfigStoreError::UnsupportedVersion {
            path: path.to_path_buf(),
            declared,
        });
    }

    let flag = |key: &'static str| -> Result<bool, TriggerConfigStoreError> {
        let raw = required(key)?;
        parse_strict_bool(raw)
            .ok_or_else(|| malformed(path, format!("field '{key}' is not a JSON boolean")))
    };

    let drawdown_enabled = flag(FIELD_DRAWDOWN_ENABLED)?;
    let threshold_raw = field(FIELD_DRAWDOWN_THRESHOLD)?;
    let drawdown_demotion = match (drawdown_enabled, threshold_raw) {
        (true, Some(raw)) => {
            let bps = parse_strict_i64(raw).ok_or_else(|| {
                malformed(
                    path,
                    format!("field '{FIELD_DRAWDOWN_THRESHOLD}' is not a JSON integer"),
                )
            })?;
            let bps = u32::try_from(bps).map_err(|_| {
                malformed(
                    path,
                    format!("field '{FIELD_DRAWDOWN_THRESHOLD}' ({bps}) is out of range"),
                )
            })?;
            // Re-validate through the newtype rather than trusting the persisted number: the
            // `[1, 10_000]` bps bound is the type's invariant, and a hand-edited file is exactly
            // where an out-of-range threshold would enter.
            let threshold = DrawdownThresholdBps::new(bps)
                .map_err(|error| malformed(path, error.to_string()))?;
            DrawdownDemotionTrigger::Enabled { threshold }
        }
        (true, None) => {
            return Err(malformed(
                path,
                format!(
                    "'{FIELD_DRAWDOWN_ENABLED}' is true but '{FIELD_DRAWDOWN_THRESHOLD}' is absent \
                     — an enabled drawdown trigger has no threshold to fire on"
                ),
            ))
        }
        (false, Some(_)) => {
            return Err(malformed(
                path,
                format!(
                    "'{FIELD_DRAWDOWN_ENABLED}' is false but '{FIELD_DRAWDOWN_THRESHOLD}' is \
                     present — the payload states the trigger's arming twice, and disagrees"
                ),
            ))
        }
        (false, None) => DrawdownDemotionTrigger::Disabled,
    };

    let ranking = |enabled: bool| {
        if enabled {
            RankingPromotionTrigger::Enabled
        } else {
            RankingPromotionTrigger::Disabled
        }
    };

    Ok(HotSwapTriggerConfig {
        drawdown_demotion,
        top_ranked_promotion: ranking(flag(FIELD_TOP_RANKED)?),
        highest_momentum_promotion: ranking(flag(FIELD_HIGHEST_MOMENTUM)?),
    })
}

/// Read the persisted trigger configuration at `path`.
///
/// * `Ok(None)` — no file: nothing has been configured, so [`HotSwapTriggerConfig::all_disabled`]
///   is the truthful answer and the caller may state it as such.
/// * `Ok(Some(config))` — the operator's persisted configuration.
/// * `Err(_)` — the file exists but this build cannot say what it configures. The caller must
///   surface this as unknown; see the module docs for why it must not become "disabled".
pub fn load(path: &Path) -> Result<Option<HotSwapTriggerConfig>, TriggerConfigStoreError> {
    let payload = match fs::read_to_string(path) {
        Ok(payload) => payload,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(io_error("read", path, &error)),
    };
    // A file that exists but holds only whitespace is a torn or truncated write, not an absent
    // configuration — the one case where "empty" must not read as "never configured".
    if payload.trim().is_empty() {
        return Err(malformed(path, "file is empty"));
    }
    deserialize(path, payload.trim()).map(Some)
}

/// Durably persist `config` to `path`, creating the parent directory if absent.
///
/// Crash-durable and atomically published, the same discipline as the SRS-BT-009 backtest store:
/// write the payload to a per-call-unique scratch file, `fsync` it so its bytes reach disk, `rename`
/// it onto the live path (an atomic replace — a reader never sees a half-written configuration),
/// then `fsync` the parent directory so the rename itself survives a crash. A configuration that
/// silently reverted to its previous value after a power loss would leave an automatic trigger armed
/// that the operator believes they disabled.
pub fn save(path: &Path, config: &HotSwapTriggerConfig) -> Result<(), TriggerConfigStoreError> {
    // A relative `triggers.json` has an EMPTY parent, not an absent one. Filtering it away
    // meant the scratch file was placed correctly but the final directory fsync was skipped,
    // so the rename that publishes a just-confirmed configuration was not itself crash-
    // durable — the very guarantee this function's contract states. `.` is that directory.
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
            .unwrap_or("hot-swap-trigger-config"),
        std::process::id()
    );
    let scratch_path = dir.join(scratch_name);

    let mut payload = serialize(config);
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

    // fsync the directory so the rename (a directory-entry change) is itself durable.
    let handle =
        fs::File::open(dir).map_err(|error| io_error("open directory of", path, &error))?;
    handle
        .sync_all()
        .map_err(|error| io_error("sync directory of", path, &error))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn enabled_config() -> HotSwapTriggerConfig {
        HotSwapTriggerConfig {
            drawdown_demotion: DrawdownDemotionTrigger::Enabled {
                threshold: DrawdownThresholdBps::new(250).unwrap(),
            },
            top_ranked_promotion: RankingPromotionTrigger::Enabled,
            highest_momentum_promotion: RankingPromotionTrigger::Disabled,
        }
    }

    fn parse(payload: &str) -> Result<HotSwapTriggerConfig, TriggerConfigStoreError> {
        deserialize(Path::new("/fixture/config.json"), payload)
    }

    #[test]
    fn round_trips_every_trigger_combination() {
        for drawdown in [
            DrawdownDemotionTrigger::Disabled,
            DrawdownDemotionTrigger::Enabled {
                threshold: DrawdownThresholdBps::new(1).unwrap(),
            },
            DrawdownDemotionTrigger::Enabled {
                threshold: DrawdownThresholdBps::new(10_000).unwrap(),
            },
        ] {
            for top in [
                RankingPromotionTrigger::Disabled,
                RankingPromotionTrigger::Enabled,
            ] {
                for momentum in [
                    RankingPromotionTrigger::Disabled,
                    RankingPromotionTrigger::Enabled,
                ] {
                    let config = HotSwapTriggerConfig {
                        drawdown_demotion: drawdown,
                        top_ranked_promotion: top,
                        highest_momentum_promotion: momentum,
                    };
                    assert_eq!(parse(&serialize(&config)).unwrap(), config);
                }
            }
        }
    }

    #[test]
    fn serialization_is_deterministic() {
        assert_eq!(serialize(&enabled_config()), serialize(&enabled_config()));
    }

    #[test]
    fn declares_the_magic_and_version_it_registers() {
        let payload = serialize(&HotSwapTriggerConfig::all_disabled());
        assert!(payload.contains(&format!("\"{FIELD_MAGIC}\":\"{MAGIC}\"")));
        assert!(payload.contains(&format!(
            "\"{FIELD_SCHEMA_VERSION}\":{TRIGGER_CONFIG_SCHEMA_VERSION}"
        )));
    }

    #[test]
    fn refuses_an_unknown_field() {
        // The fail-open this guards: a reader that looks up only what it expects would parse this
        // cleanly and silently drop whatever the extra flag was meant to arm.
        let payload = serialize(&HotSwapTriggerConfig::all_disabled())
            .replace('}', ",\"manual_promotion_enabled\":true}");
        let error = parse(&payload).unwrap_err();
        assert!(
            matches!(&error, TriggerConfigStoreError::Malformed { reason, .. }
                if reason.contains("unknown field")),
            "{error}"
        );
    }

    #[test]
    fn refuses_a_misspelled_flag_rather_than_reading_it_as_disabled() {
        let payload = serialize(&HotSwapTriggerConfig::all_disabled())
            .replace(FIELD_TOP_RANKED, "top_ranked_promotion_enabld");
        assert!(parse(&payload).is_err());
    }

    #[test]
    fn refuses_a_foreign_magic() {
        let payload = serialize(&enabled_config()).replace(MAGIC, "ATP-SOMETHING-ELSE");
        let error = parse(&payload).unwrap_err();
        assert!(
            matches!(&error, TriggerConfigStoreError::Malformed { reason, .. }
                if reason.contains("magic")),
            "{error}"
        );
    }

    #[test]
    fn refuses_a_payload_with_no_version() {
        let payload = serialize(&enabled_config()).replace(
            &format!("\"{FIELD_SCHEMA_VERSION}\":{TRIGGER_CONFIG_SCHEMA_VERSION},"),
            "",
        );
        // Absence is malformed, not a legacy floor: this format has always been versioned.
        assert!(parse(&payload).is_err());
    }

    #[test]
    fn refuses_a_future_version() {
        let payload = serialize(&enabled_config()).replace(
            &format!("\"{FIELD_SCHEMA_VERSION}\":{TRIGGER_CONFIG_SCHEMA_VERSION}"),
            &format!(
                "\"{FIELD_SCHEMA_VERSION}\":{}",
                TRIGGER_CONFIG_SCHEMA_VERSION + 1
            ),
        );
        assert!(matches!(
            parse(&payload),
            Err(TriggerConfigStoreError::UnsupportedVersion { declared, .. })
                if declared == TRIGGER_CONFIG_SCHEMA_VERSION + 1
        ));
    }

    #[test]
    fn refuses_a_coerced_boolean() {
        // `"true"` is a string, not a flag. Coercing it would arm a trigger off a value the writer
        // never wrote as a boolean.
        let payload = serialize(&enabled_config()).replace(
            &format!("\"{FIELD_TOP_RANKED}\":true"),
            &format!("\"{FIELD_TOP_RANKED}\":\"true\""),
        );
        assert!(parse(&payload).is_err());
        let numeric = serialize(&enabled_config()).replace(
            &format!("\"{FIELD_TOP_RANKED}\":true"),
            &format!("\"{FIELD_TOP_RANKED}\":1"),
        );
        assert!(parse(&numeric).is_err());
    }

    #[test]
    fn refuses_a_duplicated_key() {
        let payload = serialize(&HotSwapTriggerConfig::all_disabled())
            .replace('}', &format!(",\"{FIELD_TOP_RANKED}\":true}}"));
        assert!(parse(&payload).is_err());
    }

    #[test]
    fn refuses_an_enabled_drawdown_with_no_threshold() {
        let payload = serialize(&enabled_config())
            .replace(&format!("\"{FIELD_DRAWDOWN_THRESHOLD}\":250,"), "");
        assert!(parse(&payload).is_err());
    }

    #[test]
    fn refuses_a_threshold_alongside_a_disabled_drawdown() {
        let payload = serialize(&HotSwapTriggerConfig::all_disabled()).replace(
            &format!("\"{FIELD_DRAWDOWN_ENABLED}\":false,"),
            &format!("\"{FIELD_DRAWDOWN_ENABLED}\":false,\"{FIELD_DRAWDOWN_THRESHOLD}\":250,"),
        );
        assert!(parse(&payload).is_err());
    }

    #[test]
    fn refuses_an_out_of_range_threshold() {
        for bad in ["0", "10001", "-250"] {
            let payload = serialize(&enabled_config()).replace(
                &format!("\"{FIELD_DRAWDOWN_THRESHOLD}\":250"),
                &format!("\"{FIELD_DRAWDOWN_THRESHOLD}\":{bad}"),
            );
            assert!(parse(&payload).is_err(), "{bad} was accepted");
        }
    }

    #[test]
    fn refuses_trailing_bytes_after_the_object() {
        let payload = format!("{} trailing", serialize(&enabled_config()));
        assert!(parse(&payload).is_err());
    }
}
