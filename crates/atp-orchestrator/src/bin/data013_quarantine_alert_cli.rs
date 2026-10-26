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
//! `status` is the read-only twin the dashboard's quarantine pane reads: each of the newest runs'
//! alert state, decided by the same rule and the same settlement markers a `notify` pass uses, with
//! the per-channel outcome of the stored alert. It needs no transport configuration and sends
//! nothing.
//!
//! ## Exit codes
//!
//! `notify`: `0` when every due run was dispatched with both channels out (delivered, or queued at
//! our relay) and stored — including the case where nothing was due. `1` when any due run failed
//! to get out or to be stored. `status`: `0` when the status was read. Both: `2` for a bad
//! invocation, an unusable transport configuration, or a store that cannot be read.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{SystemTime, UNIX_EPOCH};

use atp_data::{PrimaryWrite, RunRead, MAX_RUNS_READ};
use atp_notification::{DeliveryOutcome, NotificationChannel, NotificationEvent, OperatorNotifier};
use atp_orchestrator::operator_channels::{build_channels, reject_placeholder_secrets};
use atp_orchestrator::quarantine_notification::{alert_status, notify_quarantined_runs, RunAlert};

/// How many of the newest runs `status` reports when `--limit` is not given (the same default as
/// `data013_ingestion_validation_cli summary`, so the pane's two legs cover the same runs).
const DEFAULT_STATUS_LIMIT: usize = 20;

const USAGE: &str = "\
data013_quarantine_alert_cli — SRS-DATA-013 operator alert for quarantined records

USAGE:
    data013_quarantine_alert_cli notify --ssd <dir> --store <dir>
    data013_quarantine_alert_cli status --ssd <dir> --store <dir> [--limit <n>]

COMMANDS:
    notify   alert the operator to every due quarantine run, and settle finished runs
    status   read-only: the alert state of the newest runs; sends and settles nothing

OPTIONS:
    --ssd <dir>      the SSD primary directory; its quarantine store is <dir>/quarantine (required)
    --store <dir>    existing directory of the durable notification store (required)
    --limit <n>      status only: how many of the newest runs, 1 to 200 (default 20)

ENVIRONMENT:
    notify: the email + push transports, exactly as for notif001_operator_alert_cli (see its
    --help). status reads no environment.

EXIT CODES:
    0  notify: every due run got out on both channels and was stored (or nothing
       was due); status: the status was read
    1  notify: a due run's alert did not get out or was not stored, or a settlement
       marker could not be written or is not valid
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Command {
    Notify,
    Status,
}

#[derive(Debug)]
struct Options {
    command: Command,
    ssd: PathBuf,
    store: PathBuf,
    /// `status` only; `notify` refuses `--limit`.
    limit: usize,
}

fn parse(args: &[String]) -> Result<Options, String> {
    let (command, rest) = args
        .split_first()
        .ok_or_else(|| format!("missing command\n\n{USAGE}"))?;
    let command = match command.as_str() {
        "notify" => Command::Notify,
        "status" => Command::Status,
        other => return Err(format!("unknown command '{other}'\n\n{USAGE}")),
    };
    let (mut ssd, mut store, mut limit) = (None, None, None);
    let mut iter = rest.iter();
    while let Some(flag) = iter.next() {
        let slot = match (flag.as_str(), command) {
            ("--ssd", _) => &mut ssd,
            ("--store", _) => &mut store,
            ("--limit", Command::Status) => &mut limit,
            (other, _) => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
        };
        let value = iter
            .next()
            .filter(|value| !value.starts_with("--"))
            .ok_or_else(|| format!("{flag} expects a value"))?;
        // A repeated flag is refused, not last-wins: the operator could not tell which one
        // took effect.
        if slot.replace(value.clone()).is_some() {
            return Err(format!("{flag} is given more than once"));
        }
    }
    let ssd = PathBuf::from(ssd.ok_or("--ssd <dir> is required")?);
    let store = PathBuf::from(store.ok_or("--store <dir> is required")?);
    if !store.is_dir() {
        return Err(format!(
            "notification store directory {} does not exist",
            store.display()
        ));
    }
    let limit = match limit {
        None => DEFAULT_STATUS_LIMIT,
        Some(raw) => match raw.parse::<usize>() {
            Ok(limit) if (1..=MAX_RUNS_READ).contains(&limit) => limit,
            _ => {
                return Err(format!(
                    "--limit expects an integer from 1 to {MAX_RUNS_READ}, got '{raw}'"
                ))
            }
        },
    };
    Ok(Options {
        command,
        ssd,
        store,
        limit,
    })
}

fn now_millis() -> Result<u64, String> {
    Ok(SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "the system clock is before the epoch".to_string())?
        .as_millis() as u64)
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    let options = parse(args)?;
    match options.command {
        Command::Notify => notify(&options),
        Command::Status => status(&options),
    }
}

/// Read-only: print each of the newest runs' alert state. Needs no transport configuration and
/// sends nothing.
fn status(options: &Options) -> Result<ExitCode, String> {
    let report = alert_status(&options.ssd, &options.store, now_millis()?, options.limit)
        .map_err(|err| err.to_string())?;
    println!("notification_store:{}", options.store.display());
    println!("total_runs:{}", report.total_runs);
    println!("returned:{}", report.runs.len());
    println!("invalid_markers:{}", report.invalid_markers.len());
    for (i, marker) in report.invalid_markers.iter().enumerate() {
        println!("invalid_marker_{i}:{marker}");
    }
    for (i, run) in report.runs.iter().enumerate() {
        println!("run_{i}_id:{}", run.run_id);
        println!("run_{i}_alert:{}", run.state.as_str());
        println!("run_{i}_settled:{}", run.settled);
        // The state the alert state was judged against, in the summary command's grammar,
        // so a reader joining the two can tell when the run changed between its reads.
        match &run.judged {
            RunRead::Readable(judged) => {
                println!("run_{i}_status:readable");
                println!("run_{i}_records_in:{}", judged.records_in);
                println!("run_{i}_primary:{}", judged.primary.as_str());
                if let PrimaryWrite::Written { records } = judged.primary {
                    println!("run_{i}_written:{records}");
                }
                println!("run_{i}_quarantined_total:{}", judged.quarantined_total());
            }
            RunRead::Unreadable { .. } => println!("run_{i}_status:unreadable"),
        }
        match &run.attempt {
            None => println!("run_{i}_attempt:none"),
            Some(event) => {
                println!("run_{i}_attempt:stored");
                println!(
                    "run_{i}_attempt_at_millis:{}",
                    event.dispatch_began_at_millis()
                );
                for delivery in event.deliveries() {
                    println!(
                        "run_{i}_channel_{}:{}",
                        delivery.channel().as_str(),
                        delivery.outcome().as_str()
                    );
                }
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

fn notify(options: &Options) -> Result<ExitCode, String> {
    reject_placeholder_secrets(&|key: &str| std::env::var(key).ok())?;
    let channels = build_channels().map_err(|err| {
        format!(
            "transport configuration is unusable: {err}. See notif001_operator_alert_cli --help."
        )
    })?;
    let report = notify_quarantined_runs(
        &options.ssd,
        &options.store,
        &OperatorNotifier::new(),
        &channels,
        now_millis()?,
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
    for marker in &report.invalid_markers {
        // Its run was re-examined rather than trusted as settled; the marker needs attention.
        println!("invalid_marker:{marker}");
        all_ok = false;
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
    fn a_repeated_flag_is_refused_rather_than_last_wins() {
        let tmp = std::env::temp_dir();
        let tmp = tmp.to_str().unwrap();
        for command in ["notify", "status"] {
            let err = parse(&args(&[
                command, "--ssd", "/a", "--ssd", "/b", "--store", tmp,
            ]))
            .err()
            .unwrap();
            assert!(err.contains("more than once"), "{command}: {err}");
        }
    }

    #[test]
    fn status_takes_a_bounded_limit_and_notify_takes_none() {
        let tmp = std::env::temp_dir();
        let tmp = tmp.to_str().unwrap();
        let options = parse(&args(&["status", "--ssd", "/a", "--store", tmp])).unwrap();
        assert_eq!(
            (options.command, options.limit),
            (Command::Status, DEFAULT_STATUS_LIMIT)
        );
        let options = parse(&args(&[
            "status", "--ssd", "/a", "--store", tmp, "--limit", "200",
        ]))
        .unwrap();
        assert_eq!(options.limit, 200);
        for bad in ["0", "201", "-1", "ten", ""] {
            assert!(
                parse(&args(&[
                    "status", "--ssd", "/a", "--store", tmp, "--limit", bad
                ]))
                .is_err(),
                "--limit {bad:?} was accepted"
            );
        }
        let err = parse(&args(&[
            "notify", "--ssd", "/a", "--store", tmp, "--limit", "5",
        ]))
        .err()
        .unwrap();
        assert!(err.contains("unknown flag '--limit'"), "{err}");
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
