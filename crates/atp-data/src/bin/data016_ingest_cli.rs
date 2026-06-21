//! SRS-DATA-016 idempotent-ingestion operator CLI.
//!
//! The operator-facing workflow that exercises the durable [`MarketDataStore`] + idempotent
//! [`DataLayer::ingest_market_record`] write path the storage substrate ships. There is no
//! Python<->Rust runtime bridge, so this is a small Rust binary in the data crate that demonstrates
//! the SRS-DATA-016 acceptance end to end over the *public* store API, driven by deterministic
//! fixture sources that stand in for the four provider adapters (Databento daily, IB minute,
//! option-chain, Sharadar fundamental) — exactly as the verification step permits ("fixture market
//! data, provider mocks, file reads, and persisted output inspection"):
//!
//! - `ingest   --dir D --kind K [--event-ts T] [--init]` — ingest a fixture batch for one kind on a
//!   date through `ingest_market_record` (ERR-5 validation gate + idempotent `upsert`) and durably
//!   persist. Loads existing history first (load-modify-save), so ingests accumulate.
//! - `reingest --dir D --kind K [--event-ts T]` — re-run the SAME ingestion for the SAME date and
//!   prove it is a no-op: no duplicate record, and the persisted file is byte-for-byte identical.
//! - `inspect  --dir D` — load the persisted store and print the total + per-kind record counts and
//!   the on-disk byte length, so an operator can confirm a re-ingest did not grow the store.
//!
//! The store directory is resolved fail-closed: an explicit `--dir` wins, else the
//! `ATP_DATA_STORE_DIR` config key (read here as an environment variable — the configuration layer
//! that validates it lives in `python/atp_config`), else an error. A misconfigured / unmounted
//! directory surfaces as a store error rather than a silently empty catalog.
//!
//! Scope (SRS-DATA-016 closes the idempotency property): the REAL Databento/IB/Sharadar/option-chain
//! network adapters are deferred (SRS-DATA-001/003/005/006); unified query consumers are
//! SRS-DATA-007; SSD/NAS tiering of this directory is SRS-DATA-008; the validator rule logic +
//! quarantine alert surface are SRS-DATA-013 / SRS-NOTIF-001. This CLI is a single-logical-writer
//! load-modify-save tool, matching the store's documented contract.

use std::env;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use atp_data::store::{fixture_batch, DatasetKind, MarketDataStore, StoreLock, UpsertOutcome};
use atp_data::{DataLayer, MarketIngestError};
use atp_types::{IngestionRecordSubmission, RecordValidationOutcome};

/// The default fixture event timestamp (a fixed epoch second — NOT a clock read, so a re-ingest is
/// deterministic). 2023-11-14T22:13:20Z.
const DEFAULT_EVENT_TS: i64 = 1_700_000_000;

const USAGE: &str = "\
data016_ingest_cli — SRS-DATA-016 idempotent-ingestion operator workflow

USAGE:
    data016_ingest_cli ingest   --dir <path> --kind <kind> [--event-ts <ts>] [--init]
    data016_ingest_cli reingest --dir <path> --kind <kind> [--event-ts <ts>]
    data016_ingest_cli inspect  --dir <path>

The store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed (for every command) rather than masquerading as an empty
catalog; pass --init to ingest into a brand-new directory.

KINDS:
    daily-equity-bar | minute-equity-bar | option-chain | fundamental

COMMANDS:
    ingest     Ingest a fixture batch for one kind on a date (load-modify-save; accumulates).
    reingest   Re-run the SAME ingestion and prove it is a no-op (no duplicate, file byte-identical).
    inspect    Print the total + per-kind record counts and the on-disk byte length.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data016_ingest_cli: {err}");
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
        "reingest" => cmd_reingest(rest),
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

/// Ingest a fixture batch for one kind, loading any existing history first so an ingest never
/// clobbers previously persisted records. Each record flows through `DataLayer::ingest_market_record`
/// — the ERR-5 validation gate followed by the idempotent `upsert` — so a record already present is
/// silently skipped (the idempotent no-op) rather than duplicated.
fn cmd_ingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let kind = parsed.require_kind()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_EVENT_TS);

    // --init provisions a fresh directory before the lock requires one; otherwise a missing
    // directory fails closed (the lock acquire rejects it), symmetric with load.
    if parsed.init {
        std::fs::create_dir_all(&dir).map_err(|err| format!("creating {}: {err}", dir.display()))?;
    }
    // Hold the single-writer lock across the WHOLE load-modify-save so a concurrent ingestion job
    // cannot load the old catalog and erase this job's records with a last-publish-wins save.
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let (inserted, duplicates) = ingest_batch(&store_layer(), &mut store, kind, event_ts)?;
    store.save_to_path(&dir).map_err(|err| err.to_string())?;

    println!("kind:{}", kind.as_str());
    println!("event_ts:{event_ts}");
    println!("inserted:{inserted}");
    println!("duplicates_skipped:{duplicates}");
    println!("store_len:{}", store.len());
    println!("store_file:{}", dir.join(store_filename()).display());
    Ok(())
}

/// Re-run the SAME ingestion for the SAME date and prove it is a genuine no-op: no duplicate record
/// is created, and the persisted file is byte-for-byte identical.
///
/// This is a **pure, non-mutating proof**: it NEVER writes to disk. It re-ingests into an in-memory
/// copy of the loaded store and verifies every outcome was an idempotent no-op (`inserted == 0`, the
/// length unchanged) and that the post-reingest store re-serializes to exactly the on-disk bytes. So
/// running `reingest` on the wrong kind/date or a fresh directory fails closed *without* persisting
/// the newly-inserted records — a failed proof can never become a state-changing ingest.
fn cmd_reingest(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let kind = parsed.require_kind()?;
    let event_ts = parsed.event_ts.unwrap_or(DEFAULT_EVENT_TS);

    // Hold the lock for a consistent snapshot (and fail closed on a missing directory). The proof
    // never saves, so it cannot corrupt the store even though it does not.
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let store_len_before = store.len();
    let bytes_before = read_store_bytes(&dir)?;

    // Re-ingest into the IN-MEMORY store only — the proof never persists. A genuine no-op leaves the
    // store unchanged, so its serialized form still equals the on-disk bytes; a non-no-op (wrong
    // kind/date, fresh directory) inserts records and is rejected below WITHOUT ever touching disk.
    let (inserted, duplicates) = ingest_batch(&store_layer(), &mut store, kind, event_ts)?;
    let store_len_after = store.len();
    let bytes_identical = store.serialize().into_bytes() == bytes_before;

    println!("kind:{}", kind.as_str());
    println!("event_ts:{event_ts}");
    println!("inserted:{inserted}");
    println!("duplicates_skipped:{duplicates}");
    println!("store_len_before:{store_len_before}");
    println!("store_len_after:{store_len_after}");
    println!("bytes_identical:{bytes_identical}");

    if inserted != 0 || store_len_before != store_len_after || !bytes_identical {
        return Err(format!(
            "re-ingest was NOT idempotent (inserted={inserted}, len {store_len_before}->{store_len_after}, bytes_identical={bytes_identical}); the store was NOT modified"
        ));
    }
    Ok(())
}

/// Load the persisted store and print the total + per-kind record counts and the on-disk byte
/// length, so an operator can confirm a re-ingest did not grow the store.
fn cmd_inspect(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let bytes = read_store_bytes(&dir)?;

    println!("store_len:{}", store.len());
    for kind in DatasetKind::all() {
        println!("count_{}:{}", kind.as_str(), store.count_for_kind(kind));
    }
    println!("store_bytes:{}", bytes.len());
    println!("store_file:{}", dir.join(store_filename()).display());
    Ok(())
}

// --------------------------------------------------------------------------- //
// Ingestion helpers
// --------------------------------------------------------------------------- //

/// Ingest the deterministic fixture batch for `kind` on `event_ts`, returning (inserted,
/// duplicates_skipped). A conflicting re-ingest fails closed (propagated as an error).
fn ingest_batch(
    layer: &DataLayer,
    store: &mut MarketDataStore,
    kind: DatasetKind,
    event_ts: i64,
) -> Result<(usize, usize), String> {
    let validator = AcceptAllValidator;
    let events = NullSink;
    let mut inserted = 0;
    let mut duplicates = 0;
    for record in fixture_batch(kind, event_ts) {
        // The ERR-5 envelope is derived from the record inside ingest_market_record, so validation
        // binds to exactly the record being persisted.
        let outcome = layer
            .ingest_market_record(store, record, &validator, &events, observed_at())
            .map_err(format_ingest_error)?;
        match outcome.applied {
            UpsertOutcome::Inserted => inserted += 1,
            UpsertOutcome::UnchangedDuplicate => duplicates += 1,
        }
    }
    Ok((inserted, duplicates))
}

/// The DATA-013 validator (deferred) stand-in: accepts every fixture record so the demonstration
/// focuses on the idempotency property. The real SYS-77 rule logic is SRS-DATA-013's owner.
struct AcceptAllValidator;

impl atp_data::RecordValidator for AcceptAllValidator {
    fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

/// A no-op validation event sink (the dashboard/notification fan-out is SRS-DATA-014 / SRS-NOTIF-001).
struct NullSink;

impl atp_data::IngestionValidationEventSink for NullSink {
    fn record(&self, _event: atp_types::IngestionValidationEvent) {}
}

fn store_layer() -> DataLayer {
    DataLayer
}

/// A fixed observation instant for the ERR-5 envelope (NOT a clock read — keeps the demo
/// deterministic).
fn observed_at() -> u64 {
    DEFAULT_EVENT_TS as u64
}

fn format_ingest_error(err: MarketIngestError) -> String {
    err.to_string()
}

// --------------------------------------------------------------------------- //
// Store directory + file helpers
// --------------------------------------------------------------------------- //

/// The persisted store bytes, or an empty vec if no file exists yet (a fresh, provisioned
/// directory). A real I/O failure propagates.
fn read_store_bytes(dir: &Path) -> Result<Vec<u8>, String> {
    let path = dir.join(store_filename());
    match std::fs::read(&path) {
        Ok(bytes) => Ok(bytes),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(Vec::new()),
        Err(err) => Err(format!("reading {}: {err}", path.display())),
    }
}

fn store_filename() -> &'static str {
    atp_data::store::STORE_FILENAME
}

/// Resolve the store directory: explicit `--dir`, else `ATP_DATA_STORE_DIR`, else error.
fn resolve_dir(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var("ATP_DATA_STORE_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err("no store directory: pass --dir <path> or set ATP_DATA_STORE_DIR".to_string()),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Default)]
struct ParsedArgs {
    dir: Option<String>,
    kind: Option<DatasetKind>,
    event_ts: Option<i64>,
    init: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental)"
                        )
                    })?;
                    parsed.kind = Some(kind);
                }
                "--event-ts" => {
                    let raw = take_value(&mut iter, flag)?;
                    let ts = raw.parse::<i64>().map_err(|_| {
                        format!("--event-ts expects a non-negative integer, got '{raw}'")
                    })?;
                    if ts < 0 {
                        return Err("--event-ts must be non-negative".to_string());
                    }
                    parsed.event_ts = Some(ts);
                }
                "--init" => parsed.init = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn require_kind(&self) -> Result<DatasetKind, String> {
        self.kind.ok_or_else(|| "missing required --kind".to_string())
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
