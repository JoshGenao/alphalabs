//! SRS-DATA-013 / SyRS SYS-77 — alert the operator to quarantined records.
//!
//! SYS-77: records failing validation are quarantined, and the system "shall alert the operator
//! via the dashboard and notification subsystem with the count and nature of quarantined
//! records". The data layer records every ingestion batch as a run in its quarantine store
//! (`atp_data::quarantine_store`); this module turns each run that quarantined something into an
//! SRS-NOTIF-001 operator notification (email + push), stored in the notification audit store.
//!
//! **An alert describes a run's CURRENT state.** A run counts as alerted for its current state
//! when a stored `DATA_QUARANTINE` event's summary is exactly that state's [`describe`] AND at
//! least one of the event's channels got out (delivered, or queued at our relay). So a run whose
//! alert reached no channel is retried, and a run that changes — a `PENDING` run that gains
//! conflicts or finishes — is alerted again with its new summary rather than left with a stale one.
//! Delivery is at-least-once: a crash between the dispatch and the store append, or two passes at
//! once, can send an alert twice; nothing here can drop one that is due.
//!
//! **Settlement is per run.** A run is SETTLED once it is final (not `PENDING`) and either
//! quarantined nothing or its final state was alerted. Settling publishes a marker,
//! `<store>/quarantine_settled/<run_id>.settled`, by atomic rename from a per-call scratch file, so
//! the marker's name alone proves it is whole. Each pass lists run names and marker names and
//! reads only unsettled runs. Nothing is ordered by time: a run published late (its id was minted
//! before a newer run's) or after a clock step is unsettled until it is examined.
//!
//! **What is never settled.** A `PENDING` run: within [`PENDING_GRACE_MILLIS`] it may be an ingest
//! in progress and is left alone; past it, a run that quarantined something is alerted
//! provisionally (outcome unknown), once per state, and re-examined until it finishes. An
//! UNREADABLE run is alerted, once per error, and re-examined — never treated as clean.

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};

use atp_data::{
    parse_run_id, PrimaryWrite, QuarantineRun, QuarantineStore, QuarantineStoreError, RunRead,
};
use atp_notification::{
    DeliveryOutcome, DispatchError, NotificationEvent, NotificationEventStore,
    NotificationStoreError, NotificationTrigger, OperatorNotifier, SharedChannelClient,
    TriggerKind,
};

/// Every data-quarantine summary starts with this, followed by the run id and a colon.
pub const RUN_SUMMARY_PREFIX: &str = "quarantine run ";

/// How long a `PENDING` run may be an ingest still in progress before it is alerted provisionally.
pub const PENDING_GRACE_MILLIS: u64 = 5 * 60 * 1000;

/// The settlement markers' directory, inside the notification store directory.
pub const SETTLED_DIRNAME: &str = "quarantine_settled";
/// A settlement marker's extension; the stem is the run id.
pub const SETTLED_EXTENSION: &str = "settled";
/// A settlement marker's magic header.
pub const SETTLED_MAGIC: &str = "ATP-QUARANTINE-SETTLED";
/// The settlement marker format version this build writes.
pub const SETTLED_SCHEMA_VERSION: i64 = 1;

static SCRATCH_SEQ: AtomicU64 = AtomicU64::new(0);

/// What one pass did with one run it had to act on.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RunAlert {
    /// Dispatched and durably stored. The event carries each channel's outcome.
    Stored {
        run_id: String,
        event: NotificationEvent,
    },
    /// Dispatched, but the audit store refused the event; the next pass will alert it again.
    NotStored {
        run_id: String,
        event: NotificationEvent,
        error: NotificationStoreError,
    },
    /// The dispatcher refused to dispatch (a mis-wired channel set, an impossible clock).
    DispatchFailed {
        run_id: String,
        error: DispatchError,
    },
    /// `PENDING` within the grace period: not yet due.
    InProgress { run_id: String },
}

/// One pass over the quarantine store.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotifyReport {
    /// What happened to each run that had to be acted on, oldest first.
    pub alerts: Vec<RunAlert>,
    /// Unsettled runs examined this pass.
    pub examined: usize,
    /// Runs settled this pass because their final state had already been alerted.
    pub already_alerted: usize,
    /// Runs settled this pass because they finished without quarantining anything.
    pub clean: usize,
    /// Runs settled this pass, of every kind.
    pub settled_now: usize,
    /// Runs left unsettled with nothing to send now (pending, or a provisional / unreadable state
    /// already alerted).
    pub waiting: usize,
    /// Settlement markers that could not be written. Nothing is lost — the run stays unsettled
    /// and its alerted state is not sent again — but the operator should know.
    pub settle_errors: Vec<String>,
    /// Markers that are not valid (see [`read_settlement`]): their runs were re-examined.
    pub invalid_markers: Vec<String>,
}

/// Why a pass could not run at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotifyError {
    /// The quarantine store could not be read (a missing SSD primary included).
    Quarantine(QuarantineStoreError),
    /// The notification audit store could not be read, so what was alerted is unknown.
    NotificationStore(NotificationStoreError),
    /// The settlement markers could not be listed, so which runs are settled is unknown.
    Settlement { context: &'static str },
}

impl std::fmt::Display for NotifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Quarantine(err) => write!(f, "{err}"),
            Self::NotificationStore(err) => write!(f, "notification store unreadable: {err}"),
            Self::Settlement { context } => {
                write!(f, "quarantine settlement markers unreadable: {context}")
            }
        }
    }
}

/// One pass: alert every due quarantine run beside `ssd_dir` through `channels`, storing each
/// event in the notification store at `store_dir`, and settle every run that is done.
/// `now_millis` is the dispatch instant.
pub fn notify_quarantined_runs(
    ssd_dir: &Path,
    store_dir: &Path,
    notifier: &OperatorNotifier,
    channels: &[SharedChannelClient],
    now_millis: u64,
) -> Result<NotifyReport, NotifyError> {
    let quarantine = QuarantineStore::beside(ssd_dir);
    let ids = quarantine.run_ids().map_err(NotifyError::Quarantine)?;
    let audit = NotificationEventStore::load_from_path(store_dir)
        .map_err(NotifyError::NotificationStore)?;
    let alerted = alerted_states(audit.events());
    let settlement = read_settlement(store_dir)?;
    let settled = &settlement.settled;

    let mut report = NotifyReport {
        alerts: Vec::new(),
        examined: 0,
        already_alerted: 0,
        clean: 0,
        settled_now: 0,
        waiting: 0,
        settle_errors: Vec::new(),
        invalid_markers: settlement.invalid.clone(),
    };
    // Oldest first, so alerts reach the operator in the order the runs happened.
    for run_id in ids.iter().filter(|id| !settled.contains(*id)) {
        report.examined += 1;
        let run = quarantine.read(run_id);
        let settle = match decide(&run, &alerted, now_millis) {
            Due::Settle { clean } => {
                if clean {
                    report.clean += 1;
                } else {
                    report.already_alerted += 1;
                }
                true
            }
            Due::Wait { in_progress } => {
                if in_progress {
                    report.alerts.push(RunAlert::InProgress {
                        run_id: run_id.clone(),
                    });
                } else {
                    report.waiting += 1;
                }
                false
            }
            Due::Alert {
                summary,
                detected_at_millis,
                settles,
            } => {
                let alert = dispatch_and_store(
                    run_id,
                    summary,
                    detected_at_millis,
                    store_dir,
                    notifier,
                    channels,
                    now_millis,
                );
                let got_out = matches!(&alert, RunAlert::Stored { event, .. }
                    if event.deliveries().iter().any(|d| reached_out(d.outcome())));
                report.alerts.push(alert);
                settles && got_out
            }
        };
        if settle {
            match mark_settled(store_dir, run_id) {
                Ok(()) => report.settled_now += 1,
                Err(err) => report.settle_errors.push(format!("{run_id}: {err}")),
            }
        }
    }
    Ok(report)
}

fn dispatch_and_store(
    run_id: &str,
    summary: String,
    detected_at_millis: u64,
    store_dir: &Path,
    notifier: &OperatorNotifier,
    channels: &[SharedChannelClient],
    now_millis: u64,
) -> RunAlert {
    let trigger = NotificationTrigger::data_quarantine(summary, detected_at_millis);
    match notifier.dispatch(&trigger, now_millis, channels) {
        Err(error) => RunAlert::DispatchFailed {
            run_id: run_id.to_string(),
            error,
        },
        Ok(event) => match NotificationEventStore::append_durably(store_dir, event.clone()) {
            Ok(()) => RunAlert::Stored {
                run_id: run_id.to_string(),
                event,
            },
            Err(error) => RunAlert::NotStored {
                run_id: run_id.to_string(),
                event,
                error,
            },
        },
    }
}

/// What one pass does with one unsettled run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Due {
    /// Final, and nothing more to tell the operator: settle it. `clean` when it quarantined
    /// nothing; otherwise its final state was already alerted.
    Settle { clean: bool },
    /// Nothing to send now, and not settled. `in_progress` within the grace period; otherwise a
    /// pending or unreadable run whose current state was already alerted, or a pending run that
    /// has quarantined nothing yet.
    Wait { in_progress: bool },
    /// Send this summary. `settles` when the run is final, so a delivered alert settles it.
    Alert {
        summary: String,
        detected_at_millis: u64,
        settles: bool,
    },
}

/// Decide what to do with an unsettled `run`, given the alerted states and the current instant.
pub fn decide(run: &RunRead, alerted: &BTreeSet<String>, now_millis: u64) -> Due {
    match run {
        RunRead::Readable(run) => {
            let pending = run.primary == PrimaryWrite::Pending;
            if pending && now_millis.saturating_sub(run.recorded_at_millis) < PENDING_GRACE_MILLIS {
                return Due::Wait { in_progress: true };
            }
            if run.quarantined_total() == 0 {
                // A pending run with nothing yet can still gain conflicts: never settle it.
                return if pending {
                    Due::Wait { in_progress: false }
                } else {
                    Due::Settle { clean: true }
                };
            }
            let summary = describe(run);
            if alerted.contains(&summary) {
                return if pending {
                    Due::Wait { in_progress: false }
                } else {
                    Due::Settle { clean: false }
                };
            }
            Due::Alert {
                summary,
                detected_at_millis: run.recorded_at_millis,
                settles: !pending,
            }
        }
        RunRead::Unreadable { run_id, error } => {
            let summary = describe_unreadable(run_id, error);
            if alerted.contains(&summary) {
                return Due::Wait { in_progress: false };
            }
            Due::Alert {
                summary,
                detected_at_millis: recorded_at_from_run_id(run_id).unwrap_or(now_millis),
                settles: false,
            }
        }
    }
}

/// The operator-facing summary of a run: its count and the nature (every reason that occurred,
/// with its count), and what happened to the rest of the batch. Non-secret by construction, and a
/// pure function of the run, so it identifies the state it describes.
pub fn describe(run: &QuarantineRun) -> String {
    let reasons: Vec<String> = run
        .per_reason()
        .into_iter()
        .filter(|(_, count)| *count > 0)
        .map(|(reason, count)| format!("{}={count}", reason.as_str()))
        .collect();
    let primary = match run.primary {
        PrimaryWrite::Written { records } => format!("WRITTEN ({records} valid records stored)"),
        PrimaryWrite::Refused => "REFUSED (nothing stored)".to_string(),
        PrimaryWrite::Failed => "FAILED (nothing stored)".to_string(),
        PrimaryWrite::Pending => "outcome UNKNOWN (the run has not finished)".to_string(),
    };
    format!(
        "{RUN_SUMMARY_PREFIX}{}: {} of {} records quarantined — {}; primary write {primary}",
        run.run_id,
        run.quarantined_total(),
        run.records_in,
        reasons.join(", "),
    )
}

/// The operator-facing summary of a run that cannot be read. Like [`describe`], a pure function of
/// what was observed, so it identifies the state it describes.
pub fn describe_unreadable(run_id: &str, error: &impl std::fmt::Display) -> String {
    format!(
        "{RUN_SUMMARY_PREFIX}{run_id}: UNREADABLE ({error}) — the count and nature of its \
         quarantined records cannot be reported; inspect the quarantine store"
    )
}

/// The run a data-quarantine summary names, if it names one in exactly the minted form.
pub fn run_id_in_summary(summary: &str) -> Option<&str> {
    let rest = summary.strip_prefix(RUN_SUMMARY_PREFIX)?;
    let (candidate, _) = rest.split_once(':')?;
    parse_run_id(candidate)
}

/// The run states a stored event has alerted: the summary of every `DATA_QUARANTINE` event with
/// at least one channel that got out. An event whose every channel failed does not count.
fn alerted_states(events: &[NotificationEvent]) -> BTreeSet<String> {
    events
        .iter()
        .filter(|event| event.trigger_kind() == TriggerKind::DataQuarantine)
        .filter(|event| {
            event
                .deliveries()
                .iter()
                .any(|delivery| reached_out(delivery.outcome()))
        })
        .map(|event| event.summary().to_string())
        .collect()
}

/// Did a channel get the alert out of this system? EXHAUSTIVE ON PURPOSE — no `_` arm, so a new
/// outcome makes the compiler name this decision.
fn reached_out(outcome: DeliveryOutcome) -> bool {
    match outcome {
        DeliveryOutcome::Delivered | DeliveryOutcome::Queued => true,
        DeliveryOutcome::Failed | DeliveryOutcome::Suppressed => false,
    }
}

/// The recording instant a run id carries in its leading 20 digits.
fn recorded_at_from_run_id(run_id: &str) -> Option<u64> {
    parse_run_id(run_id)?.split('-').next()?.parse().ok()
}

// --------------------------------------------------------------------------- //
// Alert status (read-only)
// --------------------------------------------------------------------------- //

/// Where one run stands with its operator alert, as the next [`notify_quarantined_runs`] pass
/// would see it. Read without sending or settling anything.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlertState {
    /// Final, and it quarantined nothing: there is nothing to tell the operator.
    NotNeeded,
    /// Final, and its final state reached the operator on at least one channel.
    Alerted,
    /// Not final (`PENDING` past the grace period, or unreadable), and its CURRENT state reached
    /// the operator. It is re-examined, and alerted again if it changes, until it finishes.
    AlertedProvisional,
    /// `PENDING` within the grace period: possibly an ingest still in progress.
    InProgress,
    /// `PENDING` past the grace period with nothing quarantined yet: nothing to send, not settled.
    AwaitingOutcome,
    /// Its current state has NOT reached the operator on any channel: the next pass sends it.
    Due,
    /// Settled by an earlier pass, but the run can no longer be read, so what it quarantined and
    /// what was sent for it cannot be confirmed. A settled run is never re-examined.
    SettledUnverifiable,
}

impl AlertState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NotNeeded => "NOT_NEEDED",
            Self::Alerted => "ALERTED",
            Self::AlertedProvisional => "ALERTED_PROVISIONAL",
            Self::InProgress => "IN_PROGRESS",
            Self::AwaitingOutcome => "AWAITING_OUTCOME",
            Self::Due => "DUE",
            Self::SettledUnverifiable => "SETTLED_UNVERIFIABLE",
        }
    }
}

/// One run's alert status.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RunAlertStatus {
    pub run_id: String,
    pub state: AlertState,
    /// A valid settlement marker exists for the run.
    pub settled: bool,
    /// The stored alert for the run's current state: for an alerted state, the newest one that
    /// got out; otherwise the newest attempt (a `Due` run whose every channel failed has one).
    /// `None` when nothing for this state is stored.
    pub attempt: Option<NotificationEvent>,
    /// The run exactly as it was read when `state` was decided. A reader that joins this
    /// status to a separate read of the same run must compare the two: the run can change
    /// between them (gain conflicts, finish), and an alert describes one state (review r13).
    pub judged: RunRead,
}

/// The alert status of the newest runs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AlertStatusReport {
    /// Newest first, as [`QuarantineStore::read_latest`] orders them.
    pub runs: Vec<RunAlertStatus>,
    /// Every published run, including those beyond the limit.
    pub total_runs: usize,
    /// Markers that are not valid; their runs are treated as unsettled, as a pass treats them.
    pub invalid_markers: Vec<String>,
}

/// The alert status of the newest `limit` runs beside `ssd_dir`, against the notification store
/// at `store_dir`, at `now_millis`. Uses the same [`decide`] rule and the same settlement markers
/// as [`notify_quarantined_runs`], so it reports what a pass would do, and it sends nothing.
pub fn alert_status(
    ssd_dir: &Path,
    store_dir: &Path,
    now_millis: u64,
    limit: usize,
) -> Result<AlertStatusReport, NotifyError> {
    // THE READ ORDER IS THE CONSISTENCY ARGUMENT (review r12). The three stores are not
    // one snapshot, and a notify pass can run between any two reads. Every write moves
    // forward in a fixed order: a run reaches its final state, THEN its alert event is
    // appended, THEN its settlement marker is published. Reading in the REVERSE order -
    // markers, then events, then runs - means anything a read sees, the later reads see
    // the causes of: a marker read first has its event in the audit read, and its run
    // is already final when the runs are read. The other direction (runs first) could
    // pair a PENDING run with a marker for its final state and report ALERTED with no
    // attempt to show. What this order can still miss is an alert stored after the audit
    // read, which reads as DUE for one poll: the error is toward "not delivered".
    let settlement = read_settlement(store_dir)?;
    let audit = NotificationEventStore::load_from_path(store_dir)
        .map_err(NotifyError::NotificationStore)?;
    let alerted = alerted_states(audit.events());
    let latest = QuarantineStore::beside(ssd_dir)
        .read_latest(limit)
        .map_err(NotifyError::Quarantine)?;

    let runs = latest
        .runs
        .iter()
        .map(|run| {
            let (run_id, summary) = match run {
                RunRead::Readable(run) => (run.run_id.clone(), describe(run)),
                RunRead::Unreadable { run_id, error } => {
                    (run_id.clone(), describe_unreadable(run_id, error))
                }
            };
            let settled = settlement.settled.contains(&run_id);
            let state = classify(run, &summary, settled, &alerted, now_millis);
            let reached = matches!(state, AlertState::Alerted | AlertState::AlertedProvisional);
            let attempt = newest_attempt(audit.events(), &summary, reached).cloned();
            RunAlertStatus {
                run_id,
                state,
                settled,
                attempt,
                judged: run.clone(),
            }
        })
        .collect();
    Ok(AlertStatusReport {
        runs,
        total_runs: latest.total_runs,
        invalid_markers: settlement.invalid,
    })
}

/// One run's [`AlertState`]. `summary` is the run's current-state summary ([`describe`] or
/// [`describe_unreadable`]); `settled` is whether it has a valid marker.
fn classify(
    run: &RunRead,
    summary: &str,
    settled: bool,
    alerted: &BTreeSet<String>,
    now_millis: u64,
) -> AlertState {
    if settled {
        // A pass never re-examines a settled run, so neither does this.
        return match run {
            RunRead::Readable(run) if run.quarantined_total() == 0 => AlertState::NotNeeded,
            RunRead::Readable(_) => AlertState::Alerted,
            RunRead::Unreadable { .. } => AlertState::SettledUnverifiable,
        };
    }
    match decide(run, alerted, now_millis) {
        Due::Settle { clean: true } => AlertState::NotNeeded,
        Due::Settle { clean: false } => AlertState::Alerted,
        Due::Wait { in_progress: true } => AlertState::InProgress,
        Due::Wait { in_progress: false } if alerted.contains(summary) => {
            AlertState::AlertedProvisional
        }
        Due::Wait { in_progress: false } => AlertState::AwaitingOutcome,
        Due::Alert { .. } => AlertState::Due,
    }
}

/// The newest stored `DATA_QUARANTINE` event with exactly this summary; when `reached`, only one
/// with at least one channel that got out.
fn newest_attempt<'a>(
    events: &'a [NotificationEvent],
    summary: &str,
    reached: bool,
) -> Option<&'a NotificationEvent> {
    events
        .iter()
        .filter(|event| event.trigger_kind() == TriggerKind::DataQuarantine)
        .filter(|event| event.summary() == summary)
        .filter(|event| {
            !reached
                || event
                    .deliveries()
                    .iter()
                    .any(|delivery| reached_out(delivery.outcome()))
        })
        .max_by_key(|event| event.dispatch_began_at_millis())
}

// --------------------------------------------------------------------------- //
// Settlement markers
// --------------------------------------------------------------------------- //

/// The settlement markers: the runs they validly settle, and every marker that is not valid.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Settlement {
    /// Runs with a valid marker.
    pub settled: BTreeSet<String>,
    /// Marker names that are not a valid marker for the run they name (not a regular file,
    /// unreadable, wrong magic or version, or a body naming another run). Their runs are NOT
    /// settled — they are re-examined, and their alerted state is not re-sent — and the pass
    /// reports them.
    pub invalid: Vec<String>,
}

/// Read every settlement marker. A marker settles its run only when it is a regular file whose
/// body is exactly the [`SETTLED_MAGIC`] / [`SETTLED_SCHEMA_VERSION`] record naming that run: a
/// name proves publication, not that the state behind it is valid (a damaged or restored file, a
/// directory, an unsupported version). No markers directory yet means none settled; a directory
/// that cannot be listed is an error.
pub fn read_settlement(store_dir: &Path) -> Result<Settlement, NotifyError> {
    let listing = match std::fs::read_dir(store_dir.join(SETTLED_DIRNAME)) {
        Ok(listing) => listing,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Settlement::default()),
        Err(_) => {
            return Err(NotifyError::Settlement {
                context: "the markers directory could not be listed",
            })
        }
    };
    let mut settlement = Settlement::default();
    for entry in listing {
        let entry = entry.map_err(|_| NotifyError::Settlement {
            context: "a markers directory entry could not be read",
        })?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let Some(stem) = name.strip_suffix(&format!(".{SETTLED_EXTENSION}")) else {
            continue; // scratch files and anything else are not markers
        };
        let valid = parse_run_id(stem).is_some()
            && std::fs::symlink_metadata(entry.path()).is_ok_and(|meta| meta.is_file())
            && std::fs::read_to_string(entry.path()).is_ok_and(|body| {
                body == format!("{SETTLED_MAGIC}\n{SETTLED_SCHEMA_VERSION}\n{stem}\n")
            });
        if valid {
            settlement.settled.insert(stem.to_string());
        } else {
            settlement.invalid.push(name.to_string());
        }
    }
    Ok(settlement)
}

/// Publish `run_id`'s settlement marker: a per-call unique scratch file (exclusive create),
/// fsync, atomic rename, directory fsync. Safe to call concurrently for the same run.
pub fn mark_settled(store_dir: &Path, run_id: &str) -> std::io::Result<()> {
    use std::io::Write;
    let dir = store_dir.join(SETTLED_DIRNAME);
    if !dir.is_dir() {
        std::fs::create_dir_all(&dir)?;
        std::fs::File::open(store_dir)?.sync_all()?;
    }
    let scratch = dir.join(format!(
        ".scratch-{}-{}",
        std::process::id(),
        SCRATCH_SEQ.fetch_add(1, Ordering::Relaxed)
    ));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&scratch)?;
        file.write_all(
            format!("{SETTLED_MAGIC}\n{SETTLED_SCHEMA_VERSION}\n{run_id}\n").as_bytes(),
        )?;
        file.sync_all()?;
        drop(file);
        std::fs::rename(&scratch, dir.join(format!("{run_id}.{SETTLED_EXTENSION}")))?;
        std::fs::File::open(&dir)?.sync_all()
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(&scratch);
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_summary_names_its_run_only_in_the_minted_form() {
        let id = "00000001792383193180-0000008791-000000";
        assert_eq!(
            run_id_in_summary(&format!("{RUN_SUMMARY_PREFIX}{id}: 6 of 10 ...")),
            Some(id)
        );
        for bad in [
            format!("{RUN_SUMMARY_PREFIX}{id} 6 of 10"),
            format!("quarantine  run {id}: x"),
            format!("{RUN_SUMMARY_PREFIX}{id}x: x"),
            "IB Gateway unreachable".to_string(),
        ] {
            assert_eq!(run_id_in_summary(&bad), None, "{bad:?}");
        }
        assert_eq!(recorded_at_from_run_id(id), Some(1_792_383_193_180));
    }

    #[test]
    fn a_sequence_past_its_padding_settles_and_names_its_run() {
        // Review L2 r4: the writer pads the sequence to six digits and grows past them. A reader
        // that wanted exactly six read every marker from run 1,000,000 on as invalid.
        let dir = std::env::temp_dir().join(format!("atp-qwide-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let ids = [
            "00000001792383193180-0000008791-999999",
            "00000001792383193180-0000008791-1000000",
        ];
        for id in ids {
            mark_settled(&dir, id).unwrap();
            let summary = format!("{RUN_SUMMARY_PREFIX}{id}: 1 of 1 records quarantined");
            assert_eq!(run_id_in_summary(&summary), Some(id));
            assert_eq!(recorded_at_from_run_id(id), Some(1_792_383_193_180));
        }
        let settlement = read_settlement(&dir).unwrap();
        assert!(settlement.invalid.is_empty(), "{:?}", settlement.invalid);
        assert_eq!(settlement.settled, BTreeSet::from(ids.map(str::to_string)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    fn run(primary: PrimaryWrite, recorded_at_millis: u64, quarantined: usize) -> QuarantineRun {
        let record =
            atp_data::ingestion_validation::mixed_validation_fixture(1_700_000_000).remove(0);
        let submission = record.ingestion_submission();
        let entry = atp_data::QuarantinedRecord {
            reason: atp_types::QuarantineReason::NegativeVolume,
            source: submission.source,
            record_hash: submission.record_hash,
            record,
        };
        QuarantineRun {
            run_id: format!("{recorded_at_millis:020}-0000000001-000000"),
            recorded_at_millis,
            observed_at_seconds: 1,
            records_in: 10,
            primary,
            entries: vec![entry; quarantined],
        }
    }

    const T: u64 = 1_000_000;
    const LATE: u64 = T + PENDING_GRACE_MILLIS;

    #[test]
    fn a_pending_run_waits_out_the_grace_period_then_alerts_provisionally() {
        let none = BTreeSet::new();
        let young = RunRead::Readable(run(PrimaryWrite::Pending, T, 2));
        assert_eq!(
            decide(&young, &none, LATE - 1),
            Due::Wait { in_progress: true }
        );
        let Due::Alert {
            summary, settles, ..
        } = decide(&young, &none, LATE)
        else {
            panic!("a pending run past the grace period is alerted");
        };
        assert!(!settles, "a pending run is never settled");
        assert!(summary.contains("outcome UNKNOWN"), "{summary}");
        assert!(summary.contains("2 of 10 records quarantined"), "{summary}");
        // Its provisional state, once alerted, waits — re-examined, not re-sent.
        let alerted: BTreeSet<String> = [summary].into();
        assert_eq!(
            decide(&young, &alerted, LATE),
            Due::Wait { in_progress: false }
        );
    }

    #[test]
    fn a_run_that_changes_after_a_provisional_alert_is_alerted_again() {
        // Review L2 r2: conflicts can arrive after the grace period; the final state must still
        // reach the operator even though a provisional alert went out.
        let provisional = describe(&run(PrimaryWrite::Pending, T, 1));
        let alerted: BTreeSet<String> = [provisional].into();
        let grown = RunRead::Readable(run(PrimaryWrite::Pending, T, 3));
        assert!(matches!(
            decide(&grown, &alerted, LATE),
            Due::Alert { settles: false, .. }
        ));
        let finished = RunRead::Readable(run(PrimaryWrite::Written { records: 7 }, T, 3));
        let Due::Alert {
            summary, settles, ..
        } = decide(&finished, &alerted, LATE)
        else {
            panic!("the final state is alerted");
        };
        assert!(settles);
        assert!(
            summary.contains("WRITTEN (7 valid records stored)"),
            "{summary}"
        );
    }

    /// `classify` over a readable run, with its own current summary.
    fn state_of(
        run: &QuarantineRun,
        settled: bool,
        alerted: &BTreeSet<String>,
        now: u64,
    ) -> AlertState {
        classify(
            &RunRead::Readable(run.clone()),
            &describe(run),
            settled,
            alerted,
            now,
        )
    }

    #[test]
    fn every_alert_state_is_reached_and_matches_what_a_pass_would_do() {
        let none = BTreeSet::new();
        let pending = run(PrimaryWrite::Pending, T, 2);
        assert_eq!(
            state_of(&pending, false, &none, LATE - 1),
            AlertState::InProgress
        );
        assert_eq!(state_of(&pending, false, &none, LATE), AlertState::Due);
        let provisional: BTreeSet<String> = [describe(&pending)].into();
        assert_eq!(
            state_of(&pending, false, &provisional, LATE),
            AlertState::AlertedProvisional
        );
        let empty_pending = run(PrimaryWrite::Pending, T, 0);
        assert_eq!(
            state_of(&empty_pending, false, &none, LATE),
            AlertState::AwaitingOutcome
        );

        let finished = run(PrimaryWrite::Written { records: 8 }, T, 2);
        assert_eq!(state_of(&finished, false, &none, T), AlertState::Due);
        let alerted: BTreeSet<String> = [describe(&finished)].into();
        assert_eq!(state_of(&finished, false, &alerted, T), AlertState::Alerted);
        assert_eq!(state_of(&finished, true, &none, T), AlertState::Alerted);
        // A provisional alert is NOT the final state's alert: the final state is due.
        assert_eq!(state_of(&finished, false, &provisional, T), AlertState::Due);

        let clean = run(PrimaryWrite::Refused, T, 0);
        assert_eq!(state_of(&clean, false, &none, T), AlertState::NotNeeded);
        assert_eq!(state_of(&clean, true, &none, T), AlertState::NotNeeded);
    }

    #[test]
    fn an_unreadable_run_is_due_then_provisional_and_never_clean() {
        let run_id = "00000000000001000000-0000000001-000000";
        let error = QuarantineStoreError::PrimaryDirectoryMissing;
        let summary = describe_unreadable(run_id, &error);
        let unreadable = RunRead::Unreadable {
            run_id: run_id.to_string(),
            error,
        };
        let none = BTreeSet::new();
        assert_eq!(
            classify(&unreadable, &summary, false, &none, T),
            AlertState::Due
        );
        let alerted: BTreeSet<String> = [summary.clone()].into();
        assert_eq!(
            classify(&unreadable, &summary, false, &alerted, T),
            AlertState::AlertedProvisional
        );
        // Settled earlier and unreadable now: what it held cannot be confirmed.
        assert_eq!(
            classify(&unreadable, &summary, true, &none, T),
            AlertState::SettledUnverifiable
        );
    }

    #[test]
    fn a_pending_run_with_nothing_quarantined_is_never_settled() {
        let none = BTreeSet::new();
        let empty = RunRead::Readable(run(PrimaryWrite::Pending, T, 0));
        assert_eq!(
            decide(&empty, &none, T + 1),
            Due::Wait { in_progress: true }
        );
        assert_eq!(
            decide(&empty, &none, LATE),
            Due::Wait { in_progress: false }
        );
    }

    #[test]
    fn a_final_run_alerts_once_and_settles_and_a_clean_one_settles_at_once() {
        let finished = run(PrimaryWrite::Written { records: 8 }, T, 2);
        let summary = describe(&finished);
        let finished = RunRead::Readable(finished);
        assert!(matches!(
            decide(&finished, &BTreeSet::new(), T),
            Due::Alert { settles: true, detected_at_millis, .. } if detected_at_millis == T
        ));
        let alerted: BTreeSet<String> = [summary].into();
        assert_eq!(decide(&finished, &alerted, T), Due::Settle { clean: false });
        let clean = RunRead::Readable(run(PrimaryWrite::Refused, T, 0));
        assert_eq!(
            decide(&clean, &BTreeSet::new(), T),
            Due::Settle { clean: true }
        );
    }

    #[test]
    fn concurrent_settlements_of_one_run_publish_one_whole_marker() {
        // Review L2 r2: two saves sharing one scratch name could interleave and corrupt the file.
        let dir = std::env::temp_dir().join(format!("atp-qsettle-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let id = "00000001792383193180-0000008791-000000";
        std::thread::scope(|scope| {
            for _ in 0..16 {
                scope.spawn(|| mark_settled(&dir, id).unwrap());
            }
        });
        let settlement = read_settlement(&dir).unwrap();
        assert_eq!(settlement.settled, BTreeSet::from([id.to_string()]));
        assert!(settlement.invalid.is_empty());
        let markers = dir.join(SETTLED_DIRNAME);
        let names: Vec<String> = std::fs::read_dir(&markers)
            .unwrap()
            .map(|e| e.unwrap().file_name().into_string().unwrap())
            .collect();
        assert_eq!(
            names,
            vec![format!("{id}.{SETTLED_EXTENSION}")],
            "no scratch left"
        );
        let text = std::fs::read_to_string(markers.join(&names[0])).unwrap();
        assert_eq!(
            text,
            format!("{SETTLED_MAGIC}\n{SETTLED_SCHEMA_VERSION}\n{id}\n")
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_marker_settles_only_its_own_run_in_the_exact_format() {
        // Review L2 r3: a name proves publication, not a valid settlement.
        let dir = std::env::temp_dir().join(format!("atp-qmarkers-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let markers = dir.join(SETTLED_DIRNAME);
        std::fs::create_dir_all(&markers).unwrap();
        let ids = [
            "00000001792383193180-0000008791-000000",
            "00000001792383193181-0000008791-000000",
            "00000001792383193182-0000008791-000000",
            "00000001792383193183-0000008791-000000",
        ];
        mark_settled(&dir, ids[0]).unwrap();
        // A body naming another run, an unsupported version, and a directory with a marker name.
        let marker = |id: &str| markers.join(format!("{id}.{SETTLED_EXTENSION}"));
        std::fs::write(
            marker(ids[1]),
            format!("{SETTLED_MAGIC}\n{SETTLED_SCHEMA_VERSION}\n{}\n", ids[0]),
        )
        .unwrap();
        std::fs::write(marker(ids[2]), format!("{SETTLED_MAGIC}\n2\n{}\n", ids[2])).unwrap();
        std::fs::create_dir(marker(ids[3])).unwrap();
        let settlement = read_settlement(&dir).unwrap();
        assert_eq!(settlement.settled, BTreeSet::from([ids[0].to_string()]));
        let mut invalid = settlement.invalid.clone();
        invalid.sort();
        assert_eq!(
            invalid,
            ids[1..]
                .iter()
                .map(|id| format!("{id}.{SETTLED_EXTENSION}"))
                .collect::<Vec<_>>()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn only_a_channel_that_got_out_counts() {
        assert!(reached_out(DeliveryOutcome::Delivered));
        assert!(reached_out(DeliveryOutcome::Queued));
        assert!(!reached_out(DeliveryOutcome::Failed));
        assert!(!reached_out(DeliveryOutcome::Suppressed));
    }
}
