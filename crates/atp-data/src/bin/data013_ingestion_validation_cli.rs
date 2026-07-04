//! SRS-DATA-013 / SyRS SYS-77 / ERR-5 ingestion-validation operator CLI.
//!
//! Demonstrates the data layer validating ingested market and options records before the primary
//! write, driven by the deterministic mixed fixture (well-formed records plus one deliberately
//! malformed record per SYS-77 rule) — exactly as the verification step permits ("CLI/API workflows
//! with fixture market data, provider mocks, file reads, and persisted output inspection"):
//!
//! - `ingest  --ssd S --nas N [--event-ts T]` — run the mixed fixture through the quarantine-and-
//!   continue path (`DataLayer::ingest_market_records_quarantining` = the ERR-5 gate + the real
//!   `Sys77RecordValidator` + a counts-aggregating `QuarantineSummarySink`). Records failing a
//!   structural / range / duplicate / required-field check are quarantined and NOT written; the valid
//!   subset is written SSD-first + NAS-synced through the SRS-DATA-008 tier. Prints the count and
//!   nature of quarantined records (`quarantined_total` + `count_<REASON>` per rule) and the tier
//!   status — the operator-facing "count and nature" surface the SyRS SYS-77 alert clause requires.
//! - `inspect --ssd S` — load the primary (SSD) store and list every record present, so an operator
//!   (or a test) can confirm the quarantined records are ABSENT from the primary tables while the
//!   valid ones are present.
//!
//! Directories resolve fail-closed: explicit `--ssd` / `--nas` win, else the `ATP_SSD_DATA_DIR` /
//! `ATP_NAS_DATA_DIR` config keys (read here as environment variables). `--event-ts` defaults to a
//! fixed instant (NOT a clock read) so the demonstration is deterministic and re-runnable.
//!
//! Scope: the durable quarantine STORE that persists rejected payloads is SRS-DATA-014 / SRS-DATA-015;
//! the dashboard alert pane and email/SMS reason summaries are SRS-UI-001 / SRS-NOTIF-001. This CLI
//! produces the structured counts-and-reasons those surfaces will consume.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::ingestion_validation::mixed_validation_fixture;
use atp_data::store::{DatasetKind, MarketDataStore, STORE_FILENAME};
use atp_data::tiering::{NasSyncStatus, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, QuarantineSummarySink, Sys77RecordValidator};

/// A fixed default event timestamp (NOT a clock read — keeps the demo deterministic).
/// 2023-11-14T22:13:20Z.
const DEFAULT_TS: i64 = 1_700_000_000;

const USAGE: &str = "\
data013_ingestion_validation_cli — SRS-DATA-013 / SyRS SYS-77 ingestion-validation workflow

USAGE:
    data013_ingestion_validation_cli ingest  --ssd <path> --nas <path> [--event-ts <ts>]
    data013_ingestion_validation_cli inspect --ssd <path>

`ingest` runs a deterministic mixed fixture (valid records + one malformed record per SYS-77 rule)
through the quarantine-and-continue path: invalid records are quarantined (never written to primary),
the valid subset is written SSD-first + NAS-synced, and the count and nature of quarantined records
is printed. `inspect` lists the records present in the primary (SSD) store so absence of the
quarantined records is verifiable.

Directories come from --ssd/--nas, else ATP_SSD_DATA_DIR / ATP_NAS_DATA_DIR.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data013_ingestion_validation_cli: {err}");
            ExitCode::FAILURE
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let (command, rest) = match args.split_first() {
        Some(parts) => parts,
        None => return Err(format!("missing subcommand\n\n{USAGE}")),
    };
    match command.as_str() {
        "ingest" => cmd_ingest(rest),
        "inspect" => cmd_inspect(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_ingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let tier = parsed.tier()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_TS);

    let batch = mixed_validation_fixture(event_ts);
    let records_in = batch.len();

    // A fresh validator per batch (its within-batch duplicate detection starts clean) + a sink that
    // aggregates the counts and nature of quarantined records. Quarantine-and-continue: invalid records
    // are dropped (never written to primary); the valid subset is written through the tier.
    let validator = Sys77RecordValidator::new();
    let sink = QuarantineSummarySink::new();
    let outcome = DataLayer
        .ingest_market_records_quarantining(&tier, batch, &validator, &sink, observed_at())
        .map_err(|err| err.to_string())?;
    let summary = sink.summary();

    println!("event_ts:{event_ts}");
    println!("records_in:{records_in}");
    println!("valid_written:{}", outcome.written);
    println!("quarantined_total:{}", summary.quarantined_total);
    // The count AND nature of quarantined records (SyRS SYS-77 alert clause), in canonical rule order.
    for (reason, count) in summary.per_reason() {
        println!("count_{}:{count}", reason.as_str());
    }
    // Sanity: the sink total and the outcome's quarantined count must agree (one event per drop).
    println!("quarantined_records:{}", outcome.quarantined_records.len());

    println!("ssd_inserted:{}", outcome.tier.ssd_inserted);
    println!("ssd_unchanged:{}", outcome.tier.ssd_unchanged);
    match &outcome.tier.nas_sync {
        NasSyncStatus::Synced { records_added } => {
            println!("nas_sync:synced");
            println!("nas_records_added:{records_added}");
        }
        NasSyncStatus::Degraded { reason } => {
            println!("nas_sync:degraded");
            println!("nas_degraded_reason:{reason}");
        }
        NasSyncStatus::Failed { reason } => {
            println!("nas_sync:failed");
            println!("nas_failed_reason:{reason}");
        }
    }
    println!(
        "ssd_store_file:{}",
        tier.config().ssd_dir().join(STORE_FILENAME).display()
    );

    // A reachable-but-broken NAS archive is an integrity failure (the SSD write committed but NAS is
    // not a superset). Exit non-zero so operator automation gating on exit status cannot mistake it for
    // a clean ingest — same disposition as data008_tier_cli. A Degraded (unreachable) NAS stays exit 0.
    if let NasSyncStatus::Failed { reason } = &outcome.tier.nas_sync {
        return Err(format!(
            "NAS archival sync FAILED ({reason}): the valid records committed to SSD but the archive \
             is not a superset — investigate the NAS store"
        ));
    }
    Ok(())
}

fn cmd_inspect(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let ssd = resolve_dir(parsed.ssd.as_deref(), "ATP_SSD_DATA_DIR", "--ssd")?;
    let store = MarketDataStore::load_from_path(&ssd).map_err(|err| err.to_string())?;

    println!("ssd_store_file:{}", ssd.join(STORE_FILENAME).display());
    println!("store_len:{}", store.records().len());
    for kind in DatasetKind::all() {
        println!("count_{}:{}", kind.as_str(), store.count_for_kind(kind));
    }
    // One line per record present, so absence of the quarantined records is verifiable. Format:
    // `record:<kind>:<symbol>[:<option_contract>]`.
    for record in store.records() {
        let key = record.key();
        match &key.option_contract {
            Some(contract) => {
                println!("record:{}:{}:{}", key.kind.as_str(), key.symbol, contract)
            }
            None => println!("record:{}:{}", key.kind.as_str(), key.symbol),
        }
    }
    Ok(())
}

/// A fixed observation instant for the ERR-5 envelope (NOT a clock read — keeps the demo deterministic).
fn observed_at() -> u64 {
    DEFAULT_TS as u64
}

// --------------------------------------------------------------------------- //
// Argument parsing (fail-closed; mirrors data008_tier_cli)
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    ssd: Option<String>,
    nas: Option<String>,
    event_ts: Option<i64>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--ssd" => parsed.ssd = Some(take_value(&mut iter, flag)?),
                "--nas" => parsed.nas = Some(take_value(&mut iter, flag)?),
                "--event-ts" => parsed.event_ts = Some(parse_ts(&mut iter, flag)?),
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Build the validated tier from --ssd/--nas (or the env config keys) at the default hot window.
    fn tier(&self) -> Result<TieredStore, String> {
        let ssd = resolve_dir(self.ssd.as_deref(), "ATP_SSD_DATA_DIR", "--ssd")?;
        let nas = resolve_dir(self.nas.as_deref(), "ATP_NAS_DATA_DIR", "--nas")?;
        let config =
            TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).map_err(|err| err.to_string())?;
        Ok(TieredStore::new(config))
    }
}

fn parse_ts<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    let ts = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))?;
    if ts < 0 {
        return Err(format!("{flag} must be non-negative"));
    }
    Ok(ts)
}

/// Resolve a tier directory: explicit flag, else the named environment config key, else error.
fn resolve_dir(explicit: Option<&str>, env_key: &str, flag: &str) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var(env_key) {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err(format!("no directory: pass {flag} <path> or set {env_key}")),
    }
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
