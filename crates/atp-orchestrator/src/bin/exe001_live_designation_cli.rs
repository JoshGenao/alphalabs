//! `exe001_live_designation_cli` — SRS-EXE-001 designate the single live strategy,
//! with explicit operator confirmation (SyRS SYS-2a / SYS-2c / SYS-2d, NFR-S2).
//!
//! ```text
//! exe001_live_designation_cli promote --state <file> --strategy <id> --confirm <acknowledgement>
//! exe001_live_designation_cli status  --state <file>
//! ```
//!
//! `promote` writes the shared durable snapshot that the live execution host
//! re-reads on every order and that the Hot-Swap CLI moves
//! (`atp_orchestrator::live_designation_store`). It runs under the same
//! `ExclusiveGuard` as a swap, for the whole read -> designate -> publish sequence.
//!
//! * The confirmation is required and must be non-empty: the operator surface
//!   (`live promote <id> --confirm`, `POST /api/v1/strategies/{id}/promote-live`)
//!   passes the operator's acknowledgement here, and an empty one is refused
//!   (`MissingConfirmation`).
//! * With nothing live, the strategy becomes live. Re-promoting the strategy that
//!   is already live succeeds and changes nothing.
//! * With a DIFFERENT strategy live, `promote` is refused (`AlreadyDesignated`).
//!   Moving the live slot from one strategy to another is a Hot-Swap: it must
//!   liquidate and demote first (SRS-RESV-004 / SRS-RESV-005), which this command
//!   does not do.
//!
//! There is deliberately no `demote` here. Clearing the live slot without the
//! RESV-004 liquidation would leave the strategy's open IB positions with no
//! strategy allowed to manage them.
//!
//! Output is `key:value` lines. Exit codes, each a distinct fact so a caller never
//! parses stderr to tell them apart:
//!
//! * 0 — success (including an idempotent re-promote).
//! * 2 — refused input: bad flags, an empty confirmation, an invalid strategy id.
//! * 3 — published but the directory fsync failed: the designation HAS moved; only
//!   crash-durability is uncertain.
//! * 4 — state error: the snapshot could not be locked, read, or written before
//!   publishing. Nothing changed, and it is not the operator's mistake.
//! * 5 — a different strategy is live; moving the slot is a Hot-Swap. Nothing changed.

use atp_execution::designation::{LiveDesignationConfirmation, LiveDesignationError};
use atp_orchestrator::live_designation_store::{
    load_designation, save_designation, PublishOutcome,
};
use atp_orchestrator::live_host::server;
use atp_orchestrator::trigger_config_store::ExclusiveGuard;
use atp_types::StrategyId;
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "\
exe001_live_designation_cli — SRS-EXE-001 designate the single live strategy

USAGE:
    exe001_live_designation_cli promote --state <file> --strategy <id> --confirm <ack>
    exe001_live_designation_cli status  --state <file>

`promote` refuses when a different strategy is live: use the Hot-Swap
(resv005_hot_swap_promote_cli swap), which demotes before it promotes.
";

const EXIT_REFUSED: u8 = 2;
const EXIT_PUBLISHED_NOT_SYNCED: u8 = 3;
/// The designation state could not be locked, read, or written (nothing changed).
/// Distinct from a refusal: the operator did nothing wrong, and a corrupt snapshot
/// must never be reported as bad input.
const EXIT_STATE_ERROR: u8 = 4;
/// A DIFFERENT strategy is live; moving the slot is a Hot-Swap (nothing changed).
const EXIT_ANOTHER_LIVE: u8 = 5;

enum Failure {
    Refused(String),
    PublishedNotSynced(String),
    StateError(String),
    AnotherLive(String),
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(Failure::Refused(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_REFUSED)
        }
        Err(Failure::PublishedNotSynced(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_PUBLISHED_NOT_SYNCED)
        }
        Err(Failure::StateError(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_STATE_ERROR)
        }
        Err(Failure::AnotherLive(message)) => {
            eprintln!("{message}");
            ExitCode::from(EXIT_ANOTHER_LIVE)
        }
    }
}

/// Parse `--flag value` pairs, allowing only `allowed`, each at most once.
fn parse_flags(args: &[String], allowed: &[&str]) -> Result<Vec<(String, String)>, String> {
    let mut parsed: Vec<(String, String)> = Vec::new();
    let mut iter = args.iter();
    while let Some(flag) = iter.next() {
        if !allowed.contains(&flag.as_str()) {
            return Err(format!("unknown flag `{flag}`\n\n{USAGE}"));
        }
        if parsed.iter().any(|(seen, _)| seen == flag) {
            return Err(format!("`{flag}` was given twice\n\n{USAGE}"));
        }
        let value = iter
            .next()
            .filter(|value| !value.starts_with("--"))
            .ok_or_else(|| format!("`{flag}` needs a value\n\n{USAGE}"))?;
        parsed.push((flag.clone(), value.clone()));
    }
    Ok(parsed)
}

fn required<'a>(flags: &'a [(String, String)], flag: &str) -> Result<&'a str, String> {
    flags
        .iter()
        .find(|(name, _)| name == flag)
        .map(|(_, value)| value.as_str())
        .ok_or_else(|| format!("`{flag}` is required\n\n{USAGE}"))
}

fn run(args: &[String]) -> Result<(), Failure> {
    let refused = Failure::Refused;
    match args.first().map(String::as_str) {
        Some("promote") => promote(&args[1..]),
        Some("status") => status(&args[1..]),
        Some("help" | "--help" | "-h") => {
            print!("{USAGE}");
            Ok(())
        }
        Some(other) => Err(refused(format!("unknown subcommand `{other}`\n\n{USAGE}"))),
        None => Err(refused(format!("missing subcommand\n\n{USAGE}"))),
    }
}

fn status(args: &[String]) -> Result<(), Failure> {
    let flags = parse_flags(args, &["--state"]).map_err(Failure::Refused)?;
    let path = PathBuf::from(required(&flags, "--state").map_err(Failure::Refused)?);
    let _guard = ExclusiveGuard::acquire_creating(&path)
        .map_err(|error| Failure::StateError(error.to_string()))?;
    let designation = load_designation(&path).map_err(Failure::StateError)?;
    println!(
        "designated:{}",
        designation.designated().map_or("none", StrategyId::as_str)
    );
    Ok(())
}

fn promote(args: &[String]) -> Result<(), Failure> {
    let flags =
        parse_flags(args, &["--state", "--strategy", "--confirm"]).map_err(Failure::Refused)?;
    let path = PathBuf::from(required(&flags, "--state").map_err(Failure::Refused)?);
    let strategy = required(&flags, "--strategy").map_err(Failure::Refused)?;
    let acknowledgement = required(&flags, "--confirm").map_err(Failure::Refused)?;
    // The same alphabet the live host serves, so a designated strategy always has a
    // socket it can submit through.
    server::validate_strategy_id(strategy).map_err(|error| Failure::Refused(error.to_string()))?;
    let strategy = StrategyId::new(strategy);

    // Refuse a missing confirmation BEFORE touching the state, so a refused promote
    // takes no lock and reads nothing.
    let confirmation =
        LiveDesignationConfirmation::from_operator(strategy.clone(), acknowledgement)
            .map_err(|error| Failure::Refused(error.to_string()))?;

    let _designation_guard = ExclusiveGuard::acquire_creating(&path)
        .map_err(|error| Failure::StateError(error.to_string()))?;
    let mut designation = load_designation(&path).map_err(Failure::StateError)?;
    let before = designation.designated().cloned();
    designation
        .designate(strategy.clone(), confirmation)
        .map_err(|error| match error {
            LiveDesignationError::AlreadyDesignated { .. } => Failure::AnotherLive(format!(
                "{error}. Moving the live slot is a Hot-Swap (resv005_hot_swap_promote_cli \
                 swap), which liquidates and demotes before it promotes."
            )),
            other => Failure::Refused(other.to_string()),
        })?;

    println!(
        "designation-before:{}",
        before.as_ref().map_or("none", StrategyId::as_str)
    );
    if before.as_ref() == Some(&strategy) {
        println!("designated:{}", strategy.as_str());
        println!("designation-changed:false");
        return Ok(());
    }
    match save_designation(&path, &designation) {
        PublishOutcome::Durable => {
            println!("designated:{}", strategy.as_str());
            println!("designation-changed:true");
            println!("designation-persisted:true");
            Ok(())
        }
        PublishOutcome::FailedBeforePublish(reason) => Err(Failure::StateError(format!(
            "the designation was NOT written (nothing changed): {reason}"
        ))),
        PublishOutcome::PublishedNotSynced(reason) => {
            println!("designated:{}", strategy.as_str());
            println!("designation-changed:true");
            println!("designation-persisted:published-not-synced");
            Err(Failure::PublishedNotSynced(format!(
                "the designation WAS published (the live slot moved) but not fsynced: {reason}"
            )))
        }
    }
}
