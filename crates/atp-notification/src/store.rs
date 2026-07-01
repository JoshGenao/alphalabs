//! Durable notification-event store (SRS-NOTIF-001).
//!
//! SRS-NOTIF-001's acceptance criterion requires the notification **delivery
//! status** to be "stored as a notification event". [`NotificationEventStore`]
//! is that durable audit trail: an append-only log of [`NotificationEvent`]s
//! with a crash-durable, atomically-published, dependency-free file codec —
//! the same persistence discipline as the market-data store
//! (`atp-data::store`) and the backtest store, reused here rather than pulled in
//! (the crate depends only on `atp-types`, so the codec is self-contained).
//!
//! ## Append-only, insertion order
//!
//! Notification events are an audit trail — never mutated, never deduplicated
//! (two independent connectivity losses detected in the same second with the
//! same summary are two real events, not one). The store holds them in
//! **insertion order**, so the serialized form is byte-identical for the same
//! sequence of appends and round-trips losslessly.
//!
//! ## Fail closed on read
//!
//! [`NotificationEventStore::restore`] validates a `MAGIC` header, an FNV-1a
//! integrity checksum over the whole body, the schema version, and every enum
//! tag (an unknown channel / outcome / trigger tag is rejected, never guessed).
//! A corrupt / truncated / checksum-mismatching blob returns an [`Err`] and
//! yields **no** partially-restored store — a persisted delivery record must
//! never be silently lost or fabricated. Reconstruction goes through the same
//! authoritative [`NotificationEvent`] / [`ChannelDelivery`] constructors the
//! dispatcher uses, so a restored event's severity stays consistent with its
//! trigger by construction.

use std::fs;
use std::io;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::event::{
    ChannelDelivery, DeliveryOutcome, NotificationChannel, NotificationEvent, NotificationTrigger,
    TriggerKind, REQUIRED_CHANNELS,
};

/// The magic header prefixing every serialized store, so a foreign / truncated
/// blob is rejected before decode.
pub const MAGIC: &str = "ATP-NOTIFICATION-EVENT-STORE";

/// The current serialized schema version. Bumped only when the on-disk layout
/// changes; [`NotificationEventStore::restore`] accepts
/// `[MIN_SUPPORTED_SCHEMA_VERSION, SCHEMA_VERSION]`.
pub const SCHEMA_VERSION: i64 = 1;

/// The oldest schema version [`NotificationEventStore::restore`] still reads.
pub const MIN_SUPPORTED_SCHEMA_VERSION: i64 = 1;

/// The file an atomic save publishes under the store directory.
pub const STORE_FILENAME: &str = "notification_events.store";

/// Base name of the scratch file an atomic save writes + fsyncs before renaming
/// it onto [`STORE_FILENAME`]. The actual scratch file appends a
/// `<pid>.<seq>` suffix so two writers persisting to the same directory cannot
/// rename over each other's scratch file.
pub const STORE_TMP_FILENAME: &str = "notification_events.store.tmp";

/// The exclusive single-writer lock file (see [`NotificationStoreLock`]).
pub const LOCK_FILENAME: &str = "notification_events.store.lock";

/// Process-local monotonic counter disambiguating concurrent scratch files
/// within one process (combined with the pid for cross-process uniqueness).
/// Affects only the scratch file name, never the published bytes.
static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// Fail-closed errors from notification-event persistence. Carries no
/// broker/vendor identifiers and no secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotificationStoreError {
    /// The serialized blob was malformed: a bad magic header, a missing
    /// newline, a non-integer where an integer was expected, a truncated
    /// length-prefixed string, an unknown enum tag, or trailing data.
    /// `context` names where parsing failed.
    Corrupt { context: &'static str },
    /// The blob's schema version is outside the readable range. Rejected loudly
    /// rather than mis-read.
    UnknownSchemaVersion { found: i64 },
    /// The blob's integrity checksum did not match the body — the bytes were
    /// corrupted or truncated after serialization. Rejected before any state is
    /// built.
    ChecksumMismatch,
    /// A filesystem operation behind [`NotificationEventStore::save_to_path`] /
    /// [`load_from_path`](NotificationEventStore::load_from_path) failed.
    /// `context` names the operation. A *missing* store file is NOT this error —
    /// it restores an empty store; this variant is a real I/O failure that must
    /// fail closed rather than be mistaken for "fresh install".
    Io { context: &'static str },
    /// Another writer holds the [`NotificationStoreLock`] for the store
    /// directory. A concurrent load-append-save is **refused** rather than
    /// proceeding to a last-publish-wins overwrite that would silently drop the
    /// other writer's event — the caller ([`NotificationEventStore::append_durably`])
    /// retries, and the operator clears a stale lock left by a crashed holder.
    Locked,
}

impl core::fmt::Display for NotificationStoreError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::Corrupt { context } => write!(f, "corrupt notification event: {context}"),
            Self::UnknownSchemaVersion { found } => {
                write!(f, "unknown notification store schema version: {found}")
            }
            Self::ChecksumMismatch => write!(f, "notification store checksum mismatch"),
            Self::Io { context } => write!(f, "notification store I/O failure: {context}"),
            Self::Locked => write!(f, "notification store is locked by another writer"),
        }
    }
}

impl std::error::Error for NotificationStoreError {}

fn io_error(context: &'static str, _err: &io::Error) -> NotificationStoreError {
    NotificationStoreError::Io { context }
}

/// An append-only, durable log of [`NotificationEvent`]s (SRS-NOTIF-001).
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct NotificationEventStore {
    events: Vec<NotificationEvent>,
}

impl NotificationEventStore {
    /// A fresh, empty store.
    pub fn new() -> Self {
        Self { events: Vec::new() }
    }

    /// Append a dispatched notification event to the log. Audit-trail semantics:
    /// events are never mutated or deduplicated, and insertion order is
    /// preserved.
    pub fn append(&mut self, event: NotificationEvent) {
        self.events.push(event);
    }

    /// The stored events, oldest first.
    pub fn events(&self) -> &[NotificationEvent] {
        &self.events
    }

    /// Number of stored events.
    pub fn len(&self) -> usize {
        self.events.len()
    }

    pub fn is_empty(&self) -> bool {
        self.events.is_empty()
    }

    /// Serialize the whole store to the deterministic, dependency-free text
    /// form. A [`MAGIC`] header and an FNV-1a checksum over the body let
    /// [`restore`](Self::restore) detect any later byte change.
    pub fn serialize(&self) -> String {
        let mut body = String::new();
        push_i64(&mut body, SCHEMA_VERSION);
        push_count(&mut body, self.events.len());
        for event in &self.events {
            encode_event(&mut body, event);
        }

        let mut out = String::with_capacity(body.len() + MAGIC.len() + 32);
        push_line(&mut out, MAGIC);
        push_u64(&mut out, checksum(body.as_bytes()));
        out.push_str(&body);
        out
    }

    /// Restore a store produced by [`serialize`](Self::serialize), failing
    /// closed on any malformation and building the whole store in a local before
    /// returning — so a corrupt / truncated / checksum-mismatching blob returns
    /// an [`Err`] and yields no partially-restored store.
    pub fn restore(serialized: &str) -> Result<Self, NotificationStoreError> {
        let mut cursor = Cursor::new(serialized);

        let magic = cursor.read_line("magic header")?;
        if magic != MAGIC {
            return Err(NotificationStoreError::Corrupt {
                context: "magic header",
            });
        }
        // Integrity check FIRST: the checksum covers the entire body that follows.
        let stored_checksum = cursor.read_u64("checksum")?;
        let body = cursor.remaining();
        if checksum(body) != stored_checksum {
            return Err(NotificationStoreError::ChecksumMismatch);
        }

        let schema_version = cursor.read_i64("schema version")?;
        if !(MIN_SUPPORTED_SCHEMA_VERSION..=SCHEMA_VERSION).contains(&schema_version) {
            return Err(NotificationStoreError::UnknownSchemaVersion {
                found: schema_version,
            });
        }

        // The count is read from the blob and is NOT trusted to size an allocation: a
        // corrupted (or checksum-recomputed) count must never drive an eager multi-GB
        // reserve. The vector grows incrementally, and a count larger than the remaining
        // data simply exhausts the cursor and fails closed — never an OOM abort.
        let event_count = cursor.read_count("event count")?;
        let mut events: Vec<NotificationEvent> = Vec::new();
        for _ in 0..event_count {
            events.push(decode_event(&mut cursor)?);
        }
        cursor.expect_end()?;
        Ok(Self { events })
    }

    /// Durably persist the whole store to [`STORE_FILENAME`] under `dir`,
    /// creating `dir` if absent.
    ///
    /// The write is **crash-durable and atomically published**: it writes the
    /// blob to a per-call-unique scratch file, `fsync`s the scratch file, then
    /// `rename`s it onto the live store (an atomic replace — a reader never sees
    /// a half-written blob), and finally `fsync`s the parent directory so the
    /// rename itself survives a crash. Every `std::io` failure surfaces as a
    /// fail-closed [`NotificationStoreError::Io`].
    pub fn save_to_path(&self, dir: &Path) -> Result<(), NotificationStoreError> {
        fs::create_dir_all(dir).map_err(|err| io_error("create store directory", &err))?;
        let seq = SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed);
        let tmp_path = dir.join(format!("{STORE_TMP_FILENAME}.{}.{seq}", std::process::id()));
        let final_path = dir.join(STORE_FILENAME);

        // Write the blob to the scratch file and fsync it, so its bytes are durably on disk BEFORE
        // we publish it — otherwise a crash could leave the renamed file referencing unwritten data.
        let mut scratch = fs::File::create(&tmp_path)
            .map_err(|err| io_error("create store scratch file", &err))?;
        use std::io::Write as _;
        if let Err(err) = scratch
            .write_all(self.serialize().as_bytes())
            .and_then(|()| scratch.sync_all())
        {
            let _ = fs::remove_file(&tmp_path);
            return Err(io_error("write store scratch file", &err));
        }
        drop(scratch);

        // Atomic publish: rename replaces the live store in one step.
        fs::rename(&tmp_path, &final_path).map_err(|err| {
            let _ = fs::remove_file(&tmp_path);
            io_error("publish store file", &err)
        })?;

        // fsync the directory so the rename (a directory-entry change) is itself durable.
        let dir_handle =
            fs::File::open(dir).map_err(|err| io_error("open store directory", &err))?;
        dir_handle
            .sync_all()
            .map_err(|err| io_error("sync store directory", &err))?;
        Ok(())
    }

    /// Load a store previously written by [`save_to_path`](Self::save_to_path).
    ///
    /// Fail-closed taxonomy (a persisted audit trail must never be silently
    /// lost):
    /// - `dir` **absent or not a directory** → [`NotificationStoreError::Io`]
    ///   (an unmounted / misconfigured path is a config failure, not an empty
    ///   log — restoring empty here would silently erase the audit trail).
    /// - `dir` exists but holds **no store file** → an empty store (the
    ///   legitimate fresh-install case).
    /// - A **present** file is decoded through the fail-closed
    ///   [`restore`](Self::restore) codec.
    pub fn load_from_path(dir: &Path) -> Result<Self, NotificationStoreError> {
        if !dir.is_dir() {
            return Err(NotificationStoreError::Io {
                context: "store directory is missing or not a directory",
            });
        }
        let final_path = dir.join(STORE_FILENAME);
        match fs::read_to_string(&final_path) {
            Ok(contents) => Self::restore(&contents),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(Self::new()),
            Err(err) => Err(io_error("read store file", &err)),
        }
    }

    /// Durably append one event under an exclusive single-writer lock — the
    /// **concurrent-writer-safe** path.
    ///
    /// A bare [`load_from_path`](Self::load_from_path) → [`append`](Self::append)
    /// → [`save_to_path`](Self::save_to_path) done by two notification sources at
    /// once is last-publish-wins: the later save re-publishes its own snapshot
    /// and silently drops the other source's event. Because notification triggers
    /// arrive from several independent sources (the connectivity gate, kill
    /// switch, Hot-Swap, orchestrator health, critical faults), that race is
    /// realistic and would corrupt the SRS-NOTIF-001 audit trail. This method
    /// closes it: it holds [`NotificationStoreLock`] across the whole
    /// load-append-save, so concurrent appenders **serialize** and every event is
    /// retained.
    ///
    /// `dir` must already exist (a missing directory fails closed as
    /// [`NotificationStoreError::Io`], symmetric with the other store APIs). A
    /// lock held by another in-flight writer is retried with a short bounded
    /// backoff; [`NotificationStoreError::Locked`] is returned only if the lock
    /// stays held for the whole budget (a wedged/crashed holder whose stale lock
    /// file the operator clears).
    pub fn append_durably(
        dir: &Path,
        event: NotificationEvent,
    ) -> Result<(), NotificationStoreError> {
        // Bounded acquisition: retry a held lock with a small backoff so two
        // in-flight appends serialize instead of one being dropped, but never
        // spin forever on a wedged holder.
        const MAX_ATTEMPTS: u32 = 200;
        let mut acquired = None;
        for attempt in 0..MAX_ATTEMPTS {
            match NotificationStoreLock::acquire(dir) {
                Ok(lock) => {
                    acquired = Some(lock);
                    break;
                }
                Err(NotificationStoreError::Locked) => {
                    if attempt + 1 == MAX_ATTEMPTS {
                        return Err(NotificationStoreError::Locked);
                    }
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                Err(other) => return Err(other),
            }
        }
        let _lock = acquired.expect("loop set the lock or returned early");
        let mut store = Self::load_from_path(dir)?;
        store.append(event);
        store.save_to_path(dir)?;
        Ok(())
        // `_lock` drops here → the lock file is removed, releasing the next writer.
    }
}

/// An exclusive **single-writer lock** over a notification-store directory,
/// mirroring `atp-data`'s `StoreLock`. Held across a whole load-append-save so
/// two concurrent notification sources cannot each load the old log and have the
/// later save erase the earlier event (SRS-NOTIF-001 audit-trail no-loss).
///
/// Acquisition is an atomic exclusive file create (`create_new`, i.e. `O_EXCL`):
/// a second writer that finds the lock present is **refused** with
/// [`NotificationStoreError::Locked`] rather than proceeding to a last-publish-
/// wins overwrite. The lock releases on [`Drop`] (the file is removed).
///
/// Scope (honest bound): this serializes writers on **one host/filesystem** —
/// the realistic case for the single-user, local-only baseline. A crashed holder
/// leaves a stale lock file an operator removes before retrying; richer liveness
/// detection (pid-liveness, lease expiry) and cross-host coordination are out of
/// the baseline.
#[derive(Debug)]
pub struct NotificationStoreLock {
    path: std::path::PathBuf,
}

impl NotificationStoreLock {
    /// Acquire the exclusive single-writer lock for `dir`. The directory must
    /// already exist (a missing directory fails closed as
    /// [`NotificationStoreError::Io`], symmetric with
    /// [`NotificationEventStore::load_from_path`]). If another writer already
    /// holds the lock, returns [`NotificationStoreError::Locked`].
    pub fn acquire(dir: &Path) -> Result<NotificationStoreLock, NotificationStoreError> {
        if !dir.is_dir() {
            return Err(NotificationStoreError::Io {
                context: "store directory is missing or not a directory",
            });
        }
        let path = dir.join(LOCK_FILENAME);
        match fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(mut file) => {
                // Best-effort holder pid for operator debugging (advisory only —
                // not used for liveness); a write failure does not invalidate the
                // acquired lock.
                use std::io::Write as _;
                let _ = writeln!(file, "{}", std::process::id());
                Ok(NotificationStoreLock { path })
            }
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                Err(NotificationStoreError::Locked)
            }
            Err(err) => Err(io_error("acquire store lock", &err)),
        }
    }
}

impl Drop for NotificationStoreLock {
    fn drop(&mut self) {
        // Release the lock on scope exit. Best-effort: a failed removal leaves a
        // stale lock the operator clears before retrying, never a corrupted store.
        let _ = fs::remove_file(&self.path);
    }
}

// --------------------------------------------------------------------------- //
// Codec (self-contained, std-only)
// --------------------------------------------------------------------------- //

fn encode_event(out: &mut String, event: &NotificationEvent) {
    push_line(out, trigger_tag(event.trigger_kind()));
    push_str(out, event.summary());
    push_u64(out, event.detected_at_millis());
    push_u64(out, event.dispatch_began_at_millis());
    push_count(out, event.deliveries().len());
    for delivery in event.deliveries() {
        push_line(out, channel_tag(delivery.channel()));
        push_line(out, outcome_tag(delivery.outcome()));
        push_str(out, delivery.detail());
    }
}

fn decode_event(cursor: &mut Cursor<'_>) -> Result<NotificationEvent, NotificationStoreError> {
    let kind = trigger_from_tag(cursor.read_line("trigger kind")?)?;
    let summary = cursor.read_str("summary")?;
    let detected_at_millis = cursor.read_u64("detected_at_millis")?;
    let dispatch_began_at_millis = cursor.read_u64("dispatch_began_at_millis")?;
    // Read↔write validation SYMMETRY: re-apply the dispatch-time invariants so a
    // checksum-valid blob written by a buggy / clock-skewed producer cannot
    // restore semantically-impossible SLA evidence. A `dispatch_began` earlier
    // than `detected` would let `within_dispatch_sla()` saturate to a false pass,
    // so reject it fail-closed rather than trusting the persisted bytes.
    if dispatch_began_at_millis < detected_at_millis {
        return Err(NotificationStoreError::Corrupt {
            context: "restored dispatch instant precedes detection instant",
        });
    }
    // Reconstruct the trigger through the authoritative constructor so the
    // restored event's severity stays consistent with its kind by construction.
    let trigger = match kind {
        TriggerKind::ConnectivityLoss => {
            NotificationTrigger::connectivity_loss(summary, detected_at_millis)
        }
        TriggerKind::CriticalFailure => {
            NotificationTrigger::critical_failure(summary, detected_at_millis)
        }
    };

    // Untrusted count — grow incrementally, never pre-reserve (see restore()).
    let delivery_count = cursor.read_count("delivery count")?;
    let mut deliveries: Vec<ChannelDelivery> = Vec::new();
    for _ in 0..delivery_count {
        let channel = channel_from_tag(cursor.read_line("delivery channel")?)?;
        let outcome = outcome_from_tag(cursor.read_line("delivery outcome")?)?;
        let detail = cursor.read_str("delivery detail")?;
        deliveries.push(ChannelDelivery::new(channel, outcome, detail));
    }
    // Symmetry with the dispatcher's required-channel fan-out contract: every
    // stored event must carry each SRS-NOTIF-001 required channel (email + SMS)
    // exactly once. A restored event missing or duplicating one is a corrupt blob
    // (a real dispatch could never produce it), so fail closed.
    for &required in REQUIRED_CHANNELS {
        let count = deliveries
            .iter()
            .filter(|delivery| delivery.channel() == required)
            .count();
        if count != 1 {
            return Err(NotificationStoreError::Corrupt {
                context: "restored event missing or duplicating a required channel delivery",
            });
        }
    }
    // Symmetry with the dispatcher's suppression invariants. A CRITICAL failure
    // is NEVER suppressed (the SYS-75 fail-safe), so a restored critical-failure
    // event carrying a suppressed delivery is impossible provenance — restoring it
    // would make the audit trail falsely claim the system chose not to notify on a
    // critical failure. And any suppressed dispatch suppresses EVERY required
    // channel (all-or-nothing), so a mix of suppressed and non-suppressed
    // deliveries is likewise impossible. Fail closed on both.
    let suppressed = deliveries
        .iter()
        .filter(|delivery| delivery.outcome() == DeliveryOutcome::Suppressed)
        .count();
    if matches!(kind, TriggerKind::CriticalFailure) && suppressed != 0 {
        return Err(NotificationStoreError::Corrupt {
            context: "restored critical-failure event has suppressed deliveries (never produced)",
        });
    }
    if suppressed != 0 && suppressed != deliveries.len() {
        return Err(NotificationStoreError::Corrupt {
            context: "restored event mixes suppressed and non-suppressed deliveries",
        });
    }

    Ok(NotificationEvent::new(
        &trigger,
        dispatch_began_at_millis,
        deliveries,
    ))
}

const fn trigger_tag(kind: TriggerKind) -> &'static str {
    match kind {
        TriggerKind::ConnectivityLoss => "C",
        TriggerKind::CriticalFailure => "F",
    }
}

fn trigger_from_tag(tag: &str) -> Result<TriggerKind, NotificationStoreError> {
    match tag {
        "C" => Ok(TriggerKind::ConnectivityLoss),
        "F" => Ok(TriggerKind::CriticalFailure),
        _ => Err(NotificationStoreError::Corrupt {
            context: "unknown trigger tag",
        }),
    }
}

const fn channel_tag(channel: NotificationChannel) -> &'static str {
    match channel {
        NotificationChannel::Email => "E",
        NotificationChannel::Sms => "S",
    }
}

fn channel_from_tag(tag: &str) -> Result<NotificationChannel, NotificationStoreError> {
    match tag {
        "E" => Ok(NotificationChannel::Email),
        "S" => Ok(NotificationChannel::Sms),
        _ => Err(NotificationStoreError::Corrupt {
            context: "unknown channel tag",
        }),
    }
}

const fn outcome_tag(outcome: DeliveryOutcome) -> &'static str {
    match outcome {
        DeliveryOutcome::Delivered => "D",
        DeliveryOutcome::Failed => "F",
        DeliveryOutcome::Suppressed => "X",
    }
}

fn outcome_from_tag(tag: &str) -> Result<DeliveryOutcome, NotificationStoreError> {
    match tag {
        "D" => Ok(DeliveryOutcome::Delivered),
        "F" => Ok(DeliveryOutcome::Failed),
        "X" => Ok(DeliveryOutcome::Suppressed),
        _ => Err(NotificationStoreError::Corrupt {
            context: "unknown outcome tag",
        }),
    }
}

/// Append `value` as its own line.
fn push_line(out: &mut String, value: &str) {
    out.push_str(value);
    out.push('\n');
}

/// Append a signed decimal integer as its own line.
fn push_i64(out: &mut String, value: i64) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append an unsigned decimal integer as its own line.
fn push_u64(out: &mut String, value: u64) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a non-negative count as its own line.
fn push_count(out: &mut String, value: usize) {
    out.push_str(&value.to_string());
    out.push('\n');
}

/// Append a length-prefixed string: the byte length on one line, then the bytes
/// followed by a newline — so any byte (including a newline inside a summary or
/// detail) round-trips without escaping.
fn push_str(out: &mut String, value: &str) {
    out.push_str(&value.len().to_string());
    out.push('\n');
    out.push_str(value);
    out.push('\n');
}

/// A 64-bit FNV-1a integrity checksum over the serialized body. Non-cryptographic:
/// it detects *accidental* corruption (bit flips, truncation) so a damaged blob
/// fails closed rather than restoring fabricated records. Not a security MAC.
fn checksum(bytes: &[u8]) -> u64 {
    const OFFSET_BASIS: u64 = 0xcbf29ce484222325;
    const PRIME: u64 = 0x0000_0100_0000_01b3;
    let mut hash = OFFSET_BASIS;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(PRIME);
    }
    hash
}

struct Cursor<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(serialized: &'a str) -> Self {
        Self {
            bytes: serialized.as_bytes(),
            pos: 0,
        }
    }

    fn remaining(&self) -> &'a [u8] {
        &self.bytes[self.pos..]
    }

    fn read_line(&mut self, context: &'static str) -> Result<&'a str, NotificationStoreError> {
        let start = self.pos;
        while self.pos < self.bytes.len() && self.bytes[self.pos] != b'\n' {
            self.pos += 1;
        }
        if self.pos >= self.bytes.len() {
            return Err(NotificationStoreError::Corrupt { context });
        }
        let line = &self.bytes[start..self.pos];
        self.pos += 1; // consume the '\n'
        std::str::from_utf8(line).map_err(|_| NotificationStoreError::Corrupt { context })
    }

    fn read_i64(&mut self, context: &'static str) -> Result<i64, NotificationStoreError> {
        self.read_line(context)?
            .parse::<i64>()
            .map_err(|_| NotificationStoreError::Corrupt { context })
    }

    fn read_u64(&mut self, context: &'static str) -> Result<u64, NotificationStoreError> {
        self.read_line(context)?
            .parse::<u64>()
            .map_err(|_| NotificationStoreError::Corrupt { context })
    }

    fn read_count(&mut self, context: &'static str) -> Result<usize, NotificationStoreError> {
        self.read_line(context)?
            .parse::<usize>()
            .map_err(|_| NotificationStoreError::Corrupt { context })
    }

    fn read_str(&mut self, context: &'static str) -> Result<String, NotificationStoreError> {
        let len = self.read_count(context)?;
        let end = self
            .pos
            .checked_add(len)
            .ok_or(NotificationStoreError::Corrupt { context })?;
        if end >= self.bytes.len() || self.bytes[end] != b'\n' {
            return Err(NotificationStoreError::Corrupt { context });
        }
        let value = std::str::from_utf8(&self.bytes[self.pos..end])
            .map_err(|_| NotificationStoreError::Corrupt { context })?
            .to_string();
        self.pos = end + 1; // consume the trailing '\n'
        Ok(value)
    }

    fn expect_end(&self) -> Result<(), NotificationStoreError> {
        if self.pos == self.bytes.len() {
            Ok(())
        } else {
            Err(NotificationStoreError::Corrupt {
                context: "trailing data",
            })
        }
    }
}
