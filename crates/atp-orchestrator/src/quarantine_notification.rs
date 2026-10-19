//! SRS-DATA-013 / SyRS SYS-77 — alert the operator to quarantined records.
//!
//! SYS-77: records failing validation are quarantined, and the system "shall alert the operator
//! via the dashboard and notification subsystem with the count and nature of quarantined
//! records". The data layer records every ingestion batch as a run in its quarantine store
//! (`atp_data::quarantine_store`); this module turns each run that quarantined something into one
//! SRS-NOTIF-001 operator notification (email + push), and stores it in the notification audit
//! store.
//!
//! **The notification store is the cursor.** A run counts as alerted when a stored
//! `DATA_QUARANTINE` event names it AND at least one of that event's channels got out (delivered,
//! or queued at our relay). So a run whose alert reached no channel is retried on the next pass
//! rather than marked done, and there is no second state file to keep consistent with the audit
//! trail. Delivery is therefore at-least-once: a crash between the dispatch and the store append,
//! or two passes running at once, can alert a run twice; nothing here can alert it zero times
//! once it is due.
//!
//! **Which runs are due.** A readable run that quarantined at least one record. A run still
//! `PENDING` within [`PENDING_GRACE_MILLIS`] of being recorded may be an ingest in progress (its
//! conflicts are added under the writer lock), so it waits; past the grace period it is alerted
//! with its primary-write outcome reported as unknown, so a crashed ingest cannot block its own
//! alert. An UNREADABLE run is due too, saying so — never treated as clean.

use std::collections::BTreeSet;
use std::path::Path;

use atp_data::{
    parse_run_id, PrimaryWrite, QuarantineRun, QuarantineStore, QuarantineStoreError, RunRead,
    MAX_RUNS_READ,
};
use atp_notification::{
    DeliveryOutcome, DispatchError, NotificationEvent, NotificationEventStore,
    NotificationStoreError, NotificationTrigger, OperatorNotifier, SharedChannelClient,
    TriggerKind,
};

/// Every data-quarantine summary starts with this, followed by the run id and a colon. It is how
/// a stored event names its run.
pub const RUN_SUMMARY_PREFIX: &str = "quarantine run ";

/// How long a `PENDING` run may be an ingest still in progress before it is alerted anyway.
pub const PENDING_GRACE_MILLIS: u64 = 5 * 60 * 1000;

/// What one pass did with one due run.
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
    /// What happened to each run that was due or waiting, oldest first.
    pub alerts: Vec<RunAlert>,
    /// Runs in the window that a stored event already alerted.
    pub already_alerted: usize,
    /// Runs in the window that quarantined nothing.
    pub clean: usize,
    /// Runs older than the newest [`MAX_RUNS_READ`]: not examined by this pass.
    pub beyond_window: usize,
}

/// Why a pass could not run at all.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotifyError {
    /// The quarantine store could not be read (a missing SSD primary included).
    Quarantine(QuarantineStoreError),
    /// The notification audit store could not be read, so which runs were alerted is unknown.
    NotificationStore(NotificationStoreError),
}

impl std::fmt::Display for NotifyError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Quarantine(err) => write!(f, "{err}"),
            Self::NotificationStore(err) => write!(f, "notification store unreadable: {err}"),
        }
    }
}

/// One pass: alert every due quarantine run beside `ssd_dir` through `channels`, storing each
/// event in the notification store at `store_dir`. `now_millis` is the dispatch instant.
pub fn notify_quarantined_runs(
    ssd_dir: &Path,
    store_dir: &Path,
    notifier: &OperatorNotifier,
    channels: &[SharedChannelClient],
    now_millis: u64,
) -> Result<NotifyReport, NotifyError> {
    let latest = QuarantineStore::beside(ssd_dir)
        .read_latest(MAX_RUNS_READ)
        .map_err(NotifyError::Quarantine)?;
    let audit = NotificationEventStore::load_from_path(store_dir)
        .map_err(NotifyError::NotificationStore)?;
    let alerted = alerted_run_ids(audit.events());

    let mut report = NotifyReport {
        alerts: Vec::new(),
        already_alerted: 0,
        clean: 0,
        beyond_window: latest.total_runs.saturating_sub(latest.runs.len()),
    };
    // Oldest first, so alerts reach the operator in the order the runs happened.
    for run in latest.runs.iter().rev() {
        let run_id = match run {
            RunRead::Readable(run) => run.run_id.as_str(),
            RunRead::Unreadable { run_id, .. } => run_id.as_str(),
        };
        let (summary, detected_at_millis) = match decide(run, &alerted, now_millis) {
            Due::AlreadyAlerted => {
                report.already_alerted += 1;
                continue;
            }
            Due::Clean => {
                report.clean += 1;
                continue;
            }
            Due::InProgress => {
                report.alerts.push(RunAlert::InProgress {
                    run_id: run_id.to_string(),
                });
                continue;
            }
            Due::Alert {
                summary,
                detected_at_millis,
            } => (summary, detected_at_millis),
        };
        let trigger = NotificationTrigger::data_quarantine(summary, detected_at_millis);
        let alert = match notifier.dispatch(&trigger, now_millis, channels) {
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
        };
        report.alerts.push(alert);
    }
    Ok(report)
}

/// What one pass does with one run.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Due {
    /// A stored event already alerted it.
    AlreadyAlerted,
    /// It quarantined nothing.
    Clean,
    /// `PENDING` within the grace period: an ingest may still be adding to it.
    InProgress,
    /// Alert it with this summary, detected at this instant.
    Alert {
        summary: String,
        detected_at_millis: u64,
    },
}

/// Decide what to do with `run`, given the runs already alerted and the current instant.
pub fn decide(run: &RunRead, alerted: &BTreeSet<String>, now_millis: u64) -> Due {
    match run {
        RunRead::Readable(run) => {
            if alerted.contains(&run.run_id) {
                Due::AlreadyAlerted
            } else if run.quarantined_total() == 0 {
                Due::Clean
            } else if run.primary == PrimaryWrite::Pending
                && now_millis.saturating_sub(run.recorded_at_millis) < PENDING_GRACE_MILLIS
            {
                Due::InProgress
            } else {
                Due::Alert {
                    summary: describe(run),
                    detected_at_millis: run.recorded_at_millis,
                }
            }
        }
        RunRead::Unreadable { run_id, error } => {
            if alerted.contains(run_id) {
                return Due::AlreadyAlerted;
            }
            Due::Alert {
                summary: format!(
                    "{RUN_SUMMARY_PREFIX}{run_id}: UNREADABLE ({error}) — the count and nature of \
                     its quarantined records cannot be reported; inspect the quarantine store"
                ),
                detected_at_millis: recorded_at_from_run_id(run_id).unwrap_or(now_millis),
            }
        }
    }
}

/// The operator-facing summary of a run: its count and the nature (every reason that occurred,
/// with its count), and what happened to the rest of the batch. Non-secret by construction.
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
        PrimaryWrite::Pending => "outcome UNKNOWN (the run never finished)".to_string(),
    };
    format!(
        "{RUN_SUMMARY_PREFIX}{}: {} of {} records quarantined — {}; primary write {primary}",
        run.run_id,
        run.quarantined_total(),
        run.records_in,
        reasons.join(", "),
    )
}

/// The run a data-quarantine summary names, if it names one in exactly the minted form.
pub fn run_id_in_summary(summary: &str) -> Option<&str> {
    let rest = summary.strip_prefix(RUN_SUMMARY_PREFIX)?;
    let (candidate, _) = rest.split_once(':')?;
    parse_run_id(candidate)
}

/// Runs a stored event has alerted: a `DATA_QUARANTINE` event naming the run, with at least one
/// channel that got out. An event whose every channel failed does not count — the run is retried.
fn alerted_run_ids(events: &[NotificationEvent]) -> BTreeSet<String> {
    events
        .iter()
        .filter(|event| event.trigger_kind() == TriggerKind::DataQuarantine)
        .filter(|event| {
            event
                .deliveries()
                .iter()
                .any(|delivery| reached_out(delivery.outcome()))
        })
        .filter_map(|event| run_id_in_summary(event.summary()).map(str::to_string))
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

    fn run(primary: PrimaryWrite, recorded_at_millis: u64, quarantined: usize) -> RunRead {
        let record =
            atp_data::ingestion_validation::mixed_validation_fixture(1_700_000_000).remove(0);
        let submission = record.ingestion_submission();
        let entry = atp_data::QuarantinedRecord {
            reason: atp_types::QuarantineReason::NegativeVolume,
            source: submission.source,
            record_hash: submission.record_hash,
            record,
        };
        RunRead::Readable(QuarantineRun {
            run_id: format!("{recorded_at_millis:020}-0000000001-000000"),
            recorded_at_millis,
            observed_at_seconds: 1,
            records_in: 10,
            primary,
            entries: vec![entry; quarantined],
        })
    }

    #[test]
    fn a_pending_run_waits_out_the_grace_period_then_alerts_as_unknown() {
        let recorded = 1_000_000;
        let none = BTreeSet::new();
        let young = run(PrimaryWrite::Pending, recorded, 2);
        assert_eq!(
            decide(&young, &none, recorded + PENDING_GRACE_MILLIS - 1),
            Due::InProgress
        );
        let Due::Alert { summary, .. } = decide(&young, &none, recorded + PENDING_GRACE_MILLIS)
        else {
            panic!("a pending run past the grace period is alerted");
        };
        assert!(summary.contains("outcome UNKNOWN"), "{summary}");
        assert!(summary.contains("2 of 10 records quarantined"), "{summary}");
        assert!(summary.contains("NEGATIVE_VOLUME=2"), "{summary}");
    }

    #[test]
    fn a_finished_run_alerts_at_once_and_a_clean_or_alerted_one_never() {
        let recorded = 1_000_000;
        let finished = run(PrimaryWrite::Written { records: 8 }, recorded, 2);
        assert!(matches!(
            decide(&finished, &BTreeSet::new(), recorded),
            Due::Alert { detected_at_millis, .. } if detected_at_millis == recorded
        ));
        let clean = run(PrimaryWrite::Written { records: 10 }, recorded, 0);
        assert_eq!(decide(&clean, &BTreeSet::new(), recorded), Due::Clean);
        let RunRead::Readable(r) = &finished else {
            unreachable!()
        };
        let alerted: BTreeSet<String> = [r.run_id.clone()].into();
        assert_eq!(decide(&finished, &alerted, recorded), Due::AlreadyAlerted);
    }

    #[test]
    fn only_a_channel_that_got_out_counts() {
        assert!(reached_out(DeliveryOutcome::Delivered));
        assert!(reached_out(DeliveryOutcome::Queued));
        assert!(!reached_out(DeliveryOutcome::Failed));
        assert!(!reached_out(DeliveryOutcome::Suppressed));
    }
}
