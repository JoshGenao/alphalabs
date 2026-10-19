//! SRS-DATA-013 / SyRS SYS-77 operator binary — alert the operator to quarantined records.
//!
//! ```text
//!   QuarantineStore (<ssd>/quarantine, written by every ingest)
//!     -> notify_quarantined_runs         (which runs are due; the audit store is the cursor)
//!     -> OperatorNotifier                (the REAL dispatcher, required email + push)
//!     -> SmtpEmailChannel / PushChannel   (the REAL IF-10 / IF-11 transports)
//!     -> NotificationEventStore          (the REAL durable audit store)
//! ```
//!
//! Run it after an ingestion job (until the scheduled ingestion jobs of SRS-DATA-001/002 run it
//! themselves). Each run that quarantined records is alerted once, with its count and the nature
//! of what was quarantined; a run whose alert reached no channel is retried on the next pass.
//!
//! What this binary proves and does not: email proves hand-off to the notification relay, push
//! proves ntfy accepted the publish — the same limits as `notif001_operator_alert_cli`, whose help
//! documents the transport environment this binary shares.
//!
//! ## Exit codes
//!
//! `0` when every due run was dispatched with both channels out (delivered, or queued at our
//! relay) and stored — including the case where nothing was due. `1` when any due run failed to
//! get out or to be stored. `2` for a bad invocation, an unusable transport configuration, or a
//! store that cannot be read.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use atp_notification::{DeliveryOutcome, NotificationChannel, NotificationEvent, OperatorNotifier};
use atp_orchestrator::operator_channels::{build_channels, reject_placeholder_secrets};
use atp_orchestrator::quarantine_notification::{notify_quarantined_runs, RunAlert};

const USAGE: &str = "\
data013_quarantine_alert_cli — SRS-DATA-013 operator alert for quarantined records

USAGE:
    data013_quarantine_alert_cli notify --ssd <dir> --store <dir>

OPTIONS:
    --ssd <dir>      the SSD primary directory; its quarantine store is <dir>/quarantine (required)
    --store <dir>    existing directory of the durable notification store (required)

ENVIRONMENT:
    The email + push transports, exactly as for notif001_operator_alert_cli (see its --help).

EXIT CODES:
    0  every due run got out on both channels and was stored (or nothing was due)
    1  a due run's alert did not get out or was not stored, or a settled run's
       marker could not be written
    2  bad invocation / unusable configuration / unreadable store
";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("data013_quarantine_alert_cli: {message}");
            ExitCode::from(2)
        }
    }
}

struct Options {
    ssd: PathBuf,
    store: PathBuf,
}

fn parse(args: &[String]) -> Result<Options, String> {
    let (command, rest) = args
        .split_first()
        .ok_or_else(|| format!("missing command\n\n{USAGE}"))?;
    if command != "notify" {
        return Err(format!("unknown command '{command}'\n\n{USAGE}"));
    }
    let (mut ssd, mut store) = (None, None);
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        let slot = match flag.as_str() {
            "--ssd" => &mut ssd,
            "--store" => &mut store,
            other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        };
        let value = iter
            .next()
            .filter(|value| !value.starts_with("--"))
            .ok_or_else(|| format!("{flag} expects a value"))?;
        *slot = Some(PathBuf::from(value));
    }
    let ssd = ssd.ok_or("--ssd <dir> is required")?;
    let store = store.ok_or("--store <dir> is required")?;
    if !store.is_dir() {
        return Err(format!(
            "notification store directory {} does not exist",
            store.display()
        ));
    }
    Ok(Options { ssd, store })
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    let options = parse(args)?;
    reject_placeholder_secrets(&|key: &str| std::env::var(key).ok())?;
    let channels = build_channels().map_err(|err| {
        format!(
            "transport configuration is unusable: {err}. See notif001_operator_alert_cli --help."
        )
    })?;
    let now_millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "the system clock is before the epoch".to_string())?
        .as_millis() as u64;

    let report = notify_quarantined_runs(
        &options.ssd,
        &options.store,
        &OperatorNotifier::new(),
        &channels,
        now_millis,
    )
    .map_err(|err| err.to_string())?;

    println!("examined_runs:{}", report.examined);
    println!("already_alerted:{}", report.already_alerted);
    println!("clean_runs:{}", report.clean);
    println!("settled_now:{}", report.settled_now);
    println!("waiting_runs:{}", report.waiting);
    println!("due_runs:{}", report.alerts.len());
    let mut all_ok = true;
    for alert in &report.alerts {
        all_ok &= print_alert(alert);
    }
    for error in &report.settle_errors {
        // Nothing is lost (the run stays unsettled and its alerted state is not re-sent).
        println!("settle_error:{error}");
        all_ok = false;
    }
    println!("alert_path_ok:{all_ok}");
    Ok(if all_ok {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

/// Print one run's outcome; `true` when it needs nothing more from the operator.
fn print_alert(alert: &RunAlert) -> bool {
    match alert {
        RunAlert::Stored { run_id, event } => {
            println!("run:{run_id} outcome:stored");
            print_channels(event)
        }
        RunAlert::NotStored {
            run_id,
            event,
            error,
        } => {
            println!("run:{run_id} outcome:not-stored error:{error}");
            print_channels(event);
            false
        }
        RunAlert::DispatchFailed { run_id, error } => {
            println!("run:{run_id} outcome:dispatch-failed error:{error}");
            false
        }
        RunAlert::InProgress { run_id } => {
            println!("run:{run_id} outcome:in-progress");
            true
        }
    }
}

/// Print each channel's outcome; `true` when both got out.
fn print_channels(event: &NotificationEvent) -> bool {
    let mut ok = true;
    for (label, channel) in [
        ("email", NotificationChannel::Email),
        ("push", NotificationChannel::Push),
    ] {
        match event.delivery_for(channel) {
            Some(delivery) => {
                println!(
                    "  {label}:{:?} detail:{}",
                    delivery.outcome(),
                    delivery.detail()
                );
                // EXHAUSTIVE ON PURPOSE — no `_` arm: a new outcome must be decided here.
                ok &= match delivery.outcome() {
                    DeliveryOutcome::Delivered | DeliveryOutcome::Queued => true,
                    DeliveryOutcome::Failed | DeliveryOutcome::Suppressed => false,
                };
            }
            None => {
                println!("  {label}:MISSING");
                ok = false;
            }
        }
    }
    ok
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    #[test]
    fn an_unknown_flag_or_command_is_refused() {
        assert!(parse(&args(&["notify", "--ssd", "/x", "--stor", "/y"])).is_err());
        assert!(parse(&args(&["send", "--ssd", "/x"])).is_err());
        assert!(parse(&args(&[])).is_err());
    }

    #[test]
    fn a_flag_cannot_consume_the_next_flag_as_its_value() {
        assert!(parse(&args(&["notify", "--ssd", "--store", "/tmp"])).is_err());
    }

    #[test]
    fn a_missing_store_directory_is_refused_before_any_send() {
        let missing = std::env::temp_dir().join("atp-data013-alert-no-such-store");
        let _ = std::fs::remove_dir_all(&missing);
        let err = parse(&args(&[
            "notify",
            "--ssd",
            "/x",
            "--store",
            missing.to_str().unwrap(),
        ]))
        .err()
        .unwrap();
        assert!(err.contains("does not exist"), "{err}");
    }
}
