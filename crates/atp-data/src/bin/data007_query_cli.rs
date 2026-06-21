//! SRS-DATA-007 unified historical data access operator CLI.
//!
//! The operator-facing workflow that exercises the [`MarketDataStore::query_unified`] read path the
//! SRS-DATA-007 unified historical interface ships. It loads the durable store the SRS-DATA-016
//! ingestion CLI (`data016_ingest_cli`) persists, then answers a query by **symbol, date range, and
//! resolution — without ever naming the original source provider**, exactly as the acceptance
//! criterion requires. There is no Python<->Rust runtime bridge, so this is a small Rust binary in the
//! data crate that demonstrates the SRS-DATA-007 acceptance end to end over the *public* store API:
//!
//! - `query --dir D --symbol S --resolution R --start T0 --end T1 [--kind K]` — load the persisted
//!   store and print every record matching `(symbol, resolution, [start, end])` (optionally narrowed to
//!   one vendor-neutral [`DatasetKind`]), in deterministic `event_ts`-ascending order. The output is
//!   **source-neutral**: it echoes the queried symbol/resolution/range, a `match_count`, and each
//!   record's `event_ts` + integer-minor value fields — there is no provider/source/vendor line, and no
//!   `--provider`/`--source` flag to give one.
//!
//! The store directory is resolved fail-closed: an explicit `--dir` wins, else the `ATP_DATA_STORE_DIR`
//! config key (read here as an environment variable — the configuration layer that validates it lives
//! in `python/atp_config`), else an error. A misconfigured / unmounted directory surfaces as a store
//! error rather than a silently empty catalog.
//!
//! Read-path scope: a query is a pure READ over the atomically-published on-disk snapshot
//! ([`MarketDataStore::load_from_path`]). It does NOT acquire the single-writer `StoreLock` — a read
//! does not need it, and `save_to_path` publishes via fsync + atomic rename so a reader never observes a
//! half-written store. Coordinating concurrent reads *during* an active ingestion write is the deferred
//! owner SRS-DATA-017; the SSD/NAS tiering of this directory is SRS-DATA-008/009/010; the real provider
//! network adapters are SRS-DATA-001/003/005/006 (fixture sources stand in); the in-process Python /
//! backtest / factor bindings over this engine are downstream consumers.

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::store::{DatasetKind, MarketDataStore};
use atp_data::UnifiedHistoricalQuery;

const USAGE: &str = "\
data007_query_cli — SRS-DATA-007 unified historical data access operator workflow

USAGE:
    data007_query_cli query --dir <path> --symbol <sym> --resolution <res> --start <ts> --end <ts> [--kind <kind>]

The store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed rather than masquerading as an empty catalog. The query is a
read-only snapshot load (no single-writer lock); ingest first with data016_ingest_cli.

The query names NO source provider — it matches purely on symbol, resolution, and the inclusive
[start, end] event-timestamp range. --kind narrows to one vendor-neutral dataset kind (not a provider).

KINDS (optional --kind disambiguator):
    daily-equity-bar | minute-equity-bar | option-chain | fundamental

COMMANDS:
    query    Print every record matching symbol + resolution + [start, end] (event_ts-ascending).
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data007_query_cli: {err}");
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
        "query" => cmd_query(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

// --------------------------------------------------------------------------- //
// Subcommand
// --------------------------------------------------------------------------- //

/// Load the persisted store (read-only snapshot) and print every record matching the unified query.
/// The output is source-neutral: symbol/resolution/range header, a match count, then each record's
/// event_ts + integer-minor value fields — never the provider a record came from.
fn cmd_query(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let symbol = parsed.require_symbol()?;
    let resolution = parsed.require_resolution()?;
    let start = parsed.require_start()?;
    let end = parsed.require_end()?;

    // A READ does not acquire the single-writer StoreLock. load_from_path reads the atomically-published
    // whole-file snapshot, so a reader never observes a half-written store; concurrent-read-DURING-write
    // coordination is the separate deferred SRS-DATA-017.
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;

    let mut query = UnifiedHistoricalQuery::new(symbol.clone(), resolution.clone(), start, end);
    if let Some(kind) = parsed.kind {
        query = query.with_kind(kind);
    }
    let result = store.query_unified(&query);

    println!("symbol:{symbol}");
    println!("resolution:{resolution}");
    println!("start:{start}");
    println!("end:{end}");
    println!("kind:{}", parsed.kind.map_or("any", |kind| kind.as_str()));
    println!("match_count:{}", result.len());
    for (index, record) in result.records().iter().enumerate() {
        let key = record.key();
        println!("record.{index}.event_ts:{}", key.event_ts);
        println!(
            "record.{index}.option_contract:{}",
            key.option_contract.as_deref().unwrap_or("-")
        );
        for value in record.fields() {
            println!("record.{index}.field.{}:{}", value.name, value.value_minor);
        }
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Store directory helper
// --------------------------------------------------------------------------- //

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
    symbol: Option<String>,
    resolution: Option<String>,
    start: Option<i64>,
    end: Option<i64>,
    kind: Option<DatasetKind>,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--symbol" => parsed.symbol = Some(take_value(&mut iter, flag)?),
                "--resolution" => parsed.resolution = Some(take_value(&mut iter, flag)?),
                "--start" => parsed.start = Some(parse_ts(&take_value(&mut iter, flag)?, flag)?),
                "--end" => parsed.end = Some(parse_ts(&take_value(&mut iter, flag)?, flag)?),
                "--kind" => {
                    let raw = take_value(&mut iter, flag)?;
                    let kind = DatasetKind::from_label(&raw).ok_or_else(|| {
                        format!(
                            "unknown --kind '{raw}' (expected daily-equity-bar | minute-equity-bar | option-chain | fundamental)"
                        )
                    })?;
                    parsed.kind = Some(kind);
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn require_symbol(&self) -> Result<String, String> {
        self.symbol.clone().ok_or_else(|| "missing required --symbol".to_string())
    }

    fn require_resolution(&self) -> Result<String, String> {
        self.resolution
            .clone()
            .ok_or_else(|| "missing required --resolution".to_string())
    }

    fn require_start(&self) -> Result<i64, String> {
        self.start.ok_or_else(|| "missing required --start".to_string())
    }

    fn require_end(&self) -> Result<i64, String> {
        self.end.ok_or_else(|| "missing required --end".to_string())
    }
}

fn parse_ts(raw: &str, flag: &str) -> Result<i64, String> {
    let ts = raw
        .parse::<i64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))?;
    if ts < 0 {
        return Err(format!("{flag} must be non-negative"));
    }
    Ok(ts)
}

fn take_value<'a>(
    iter: &mut impl Iterator<Item = &'a String>,
    flag: &str,
) -> Result<String, String> {
    iter.next()
        .map(|value| value.to_string())
        .ok_or_else(|| format!("{flag} expects a value"))
}
