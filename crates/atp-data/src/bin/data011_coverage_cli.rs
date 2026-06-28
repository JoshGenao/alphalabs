//! SRS-DATA-011 corporate-action **coverage** operator CLI.
//!
//! The operator-facing workflow that records and inspects the per-symbol corporate-action **coverage
//! frontier** — the completeness-through-date `D` that makes a split-adjusted label honest. A
//! split-adjusted read (`data007_query_cli --normalization split-adjusted`) is served ONLY when a
//! symbol's coverage frontier reaches the query end (`MarketDataStore::query_split_adjusted`); this CLI
//! is how that frontier is asserted. Real provider corporate-action ingestion (Databento/IB) is
//! deferred (SRS-DATA-001/003/006); the operator-set frontier here stands in, exactly as the
//! SRS-DATA-011 verification step permits ("fixture market data, provider mocks, operator controls").
//!
//! - `assert-coverage --dir D --symbol S --through T [--init]` — record "symbol S complete through T"
//!   as a [`DatasetKind::CorporateActionCoverage`] record (keyed by `event_ts = T`), under the
//!   single-writer [`StoreLock`] (load-modify-save). Monotonic + idempotent: a higher `T` advances the
//!   frontier (a new record), re-asserting the same `T` is a no-op, and a `T` below the current
//!   frontier never regresses it (the effective frontier is the maximum). Prints the `outcome`
//!   (inserted | unchanged) and the resulting `frontier`.
//! - `show-coverage --dir D [--symbol S]` — print the coverage frontier for `S` (or every covered
//!   symbol), a pure READ that takes no lock.
//!
//! The store directory is resolved fail-closed: an explicit `--dir` wins, else the
//! `ATP_DATA_STORE_DIR` config key (read here as an environment variable — the configuration layer that
//! validates it lives in `python/atp_config`), else an error. A misconfigured / unmounted directory
//! surfaces as a store error rather than a silently empty catalog.

use std::collections::BTreeSet;
use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_data::store::{
    coverage_record, DatasetKind, MarketDataStore, StoreLock, UpsertOutcome, STORE_FILENAME,
};

const USAGE: &str = "\
data011_coverage_cli — SRS-DATA-011 corporate-action coverage operator workflow

USAGE:
    data011_coverage_cli assert-coverage --dir <path> --symbol <sym> --through <ts> [--init]
    data011_coverage_cli show-coverage   --dir <path> [--symbol <sym>]

The store directory is taken from --dir, else the ATP_DATA_STORE_DIR environment variable. A
missing/unmounted directory fails closed rather than masquerading as an empty catalog; pass --init to
assert into a brand-new directory.

`assert-coverage` records 'symbol complete through <ts>' (all corporate actions effective on or before
<ts> are known). It is the frontier the split-adjusted read (data007_query_cli --normalization
split-adjusted) checks against the query end before serving. Coverage is monotonic: the effective
frontier is the MAXIMUM asserted --through, so a lower --through never regresses it.

COMMANDS:
    assert-coverage   Record a per-symbol completeness-through-date (load-modify-save under the lock).
    show-coverage     Print the coverage frontier for a symbol (or every covered symbol); read-only.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("data011_coverage_cli: {err}");
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
        "assert-coverage" => cmd_assert_coverage(rest),
        "show-coverage" => cmd_show_coverage(rest),
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

/// Record "symbol complete through `--through`" under the single-writer lock (load-modify-save), so a
/// concurrent writer cannot erase the assertion with a last-publish-wins save. The coverage record is
/// keyed by `event_ts = through`, so advancing the frontier is an `Inserted` and re-asserting the same
/// through is an `UnchangedDuplicate` (never a `ConflictingContent` — a different through is a new key).
fn cmd_assert_coverage(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let symbol = parsed.require_symbol()?;
    let through = parsed.require_through()?;

    if parsed.init {
        std::fs::create_dir_all(&dir)
            .map_err(|err| format!("creating {}: {err}", dir.display()))?;
    }
    // Hold the single-writer lock across the WHOLE load-modify-save (symmetric with data016_ingest_cli).
    let _lock = StoreLock::acquire(&dir).map_err(|err| err.to_string())?;
    let mut store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let outcome = store
        .upsert(coverage_record(through, &symbol))
        .map_err(|err| err.to_string())?;
    store.save_to_path(&dir).map_err(|err| err.to_string())?;

    let frontier = store
        .coverage_frontier(&symbol)
        .expect("a coverage record was just upserted for this symbol");
    println!("symbol:{symbol}");
    println!("through:{through}");
    println!("frontier:{frontier}");
    println!(
        "outcome:{}",
        match outcome {
            UpsertOutcome::Inserted => "inserted",
            UpsertOutcome::UnchangedDuplicate => "unchanged",
        }
    );
    println!("store_len:{}", store.len());
    println!("store_file:{}", dir.join(STORE_FILENAME).display());
    Ok(())
}

/// Print the coverage frontier for `--symbol` (or every covered symbol). A pure READ over the
/// atomically-published snapshot — it takes NO single-writer lock (concurrent-read property).
fn cmd_show_coverage(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;
    let store = MarketDataStore::load_from_path(&dir).map_err(|err| err.to_string())?;

    match parsed.symbol {
        Some(symbol) => {
            println!("symbol:{symbol}");
            match store.coverage_frontier(&symbol) {
                Some(frontier) => println!("frontier:{frontier}"),
                None => println!("frontier:none"),
            }
        }
        None => {
            // Every symbol with a coverage record, in deterministic sorted order.
            let symbols: BTreeSet<&str> = store
                .records()
                .iter()
                .filter(|record| record.key().kind == DatasetKind::CorporateActionCoverage)
                .map(|record| record.key().symbol.as_str())
                .collect();
            println!("coverage_symbols:{}", symbols.len());
            for symbol in symbols {
                let frontier = store
                    .coverage_frontier(symbol)
                    .expect("symbol came from a coverage record");
                println!("coverage.{symbol}:{frontier}");
            }
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
    through: Option<i64>,
    init: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--symbol" => parsed.symbol = Some(take_value(&mut iter, flag)?),
                "--through" => {
                    let raw = take_value(&mut iter, flag)?;
                    let ts = raw.parse::<i64>().map_err(|_| {
                        format!("--through expects a non-negative integer, got '{raw}'")
                    })?;
                    // A coverage frontier is an event timestamp; a negative value is rejected at parse
                    // (the store would also reject it, but failing here gives a clear message).
                    if ts < 0 {
                        return Err("--through must be non-negative".to_string());
                    }
                    parsed.through = Some(ts);
                }
                "--init" => parsed.init = true,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    fn require_symbol(&self) -> Result<String, String> {
        self.symbol
            .clone()
            .ok_or_else(|| "missing required --symbol".to_string())
    }

    fn require_through(&self) -> Result<i64, String> {
        self.through
            .ok_or_else(|| "missing required --through".to_string())
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
