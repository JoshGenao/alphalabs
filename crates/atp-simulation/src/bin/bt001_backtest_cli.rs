//! SRS-BT-001 operator backtest launch CLI.
//!
//! The SRS-BT-001 acceptance criterion is "A backtest can be launched with system
//! data or uploaded Parquet data; **start and end dates are selectable** through API
//! and dashboard." This binary is the operator-facing **launch surface** for the
//! `system data` + `configurable date range` halves of that criterion, driven the
//! way a real launch would over the *shipped* product path: it parses an
//! operator-supplied `YYYY-MM-DD` start/end window (via
//! [`atp_simulation::launch::parse_window`]), reads bars from the platform's stored
//! historical catalog through the real [`StoreBarSource`] (the source-neutral
//! SRS-DATA-007 unified query — not a CLI-local fixture catalog), restricts replay
//! to the selected window, runs a deterministic strategy, and prints the launched
//! window plus the produced trade log + equity curve + final equity.
//!
//! It mirrors the operator-CLI pattern every sibling backtest feature already ships
//! (`bt002_cost_cli`, `bt009_store_cli`, …); SRS-BT-001 — the actual *launch a
//! backtest* feature — previously had no launch CLI of its own.
//!
//! # Fixtures
//!
//! The catalog is a deliberate fixture (one daily bar per day across a short demo
//! window) populated into a real [`MarketDataStore`], exactly as the SRS-BT-001
//! verification step specifies ("fixture market data, provider mocks"). Because the
//! engine authoritatively restricts replay to the requested window, an operator can
//! sub-select any `[--start, --end]` inside the seeded span and watch `bars
//! processed` shrink to match.
//!
//! # Deferred (so SRS-BT-001 stays `passes:false`)
//!
//! * `--source uploaded`: the user-uploaded **Apache Parquet** reader (SyRS SYS-14 /
//!   AC-4 / IF-5) needs a new third-party Rust crate (the workspace has zero today)
//!   and SyRS scope approval, and belongs in the data layer behind the unified
//!   interface (AC-8 / SYS-27). Selecting it fails closed with a clear deferred
//!   message — the engine's `UploadedData` provenance variant exists, but no reader
//!   is wired, and this CLI never silently substitutes system data for it.
//! * the REST `POST /api/v1/backtests` handler (SRS-API-001) and the dashboard date
//!   pickers (SRS-UI): this CLI is the in-process Rust launch surface those operator
//!   interfaces wrap; the date-selection binding it uses ([`atp_simulation::launch`])
//!   is shared with them.
//! * the Rust↔Python strategy host: the strategy here is a fixture
//!   [`BacktestStrategy`], not user-authored Python.

use std::collections::BTreeMap;
use std::env;
use std::process::ExitCode;

use atp_data::store::{DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey};
use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::launch::{parse_window, LaunchWindow};
use atp_simulation::store_bar_source::{Normalization, StoreBarSource};
use atp_types::StrategyId;

/// The symbol the fixture catalog is seeded under — the only symbol present in stored
/// data. `--symbol` defaults to it; requesting any other symbol fails closed (the
/// engine returns EmptyData), so the launch's stored-data provenance is real rather
/// than fabricated per requested name.
const FIXTURE_SYMBOL: &str = "ACME";
const DEFAULT_CASH_MINOR: i64 = 1_000_000;
/// Shares bought on the first in-window bar (the fixture strategy).
const FIXTURE_LOT: i64 = 10;

/// The seeded fixture catalog: one daily close per calendar day. An operator can
/// sub-select any window inside this span.
const FIXTURE_BARS: &[(&str, i64)] = &[
    ("2024-01-02", 10_000),
    ("2024-01-03", 10_250),
    ("2024-01-04", 9_900),
    ("2024-01-05", 10_400),
    ("2024-01-08", 10_600),
    ("2024-01-09", 10_350),
];

const USAGE: &str = "\
bt001_backtest_cli — SRS-BT-001 operator backtest launch surface

USAGE:
    bt001_backtest_cli run --start <YYYY-MM-DD> --end <YYYY-MM-DD>
                           [--symbol <S>] [--source system] [--cash <minor>]
    bt001_backtest_cli help

COMMANDS:
    run     Launch a deterministic backtest over the configurable inclusive
            [start, end] calendar window, reading bars from the platform's stored
            historical catalog (system data) via the unified SRS-DATA-007 query
            path (StoreBarSource). Prints the launched window and the produced
            trade log summary, equity curve summary, and final equity.

FLAGS:
    --start <YYYY-MM-DD>   inclusive window start date (required)
    --end <YYYY-MM-DD>     inclusive window end date (required)
    --symbol <S>           equity symbol to backtest (default: ACME)
    --source <name>        data source: `system` (default). `uploaded` (Apache
                           Parquet) is DEFERRED and fails closed (see below).
    --cash <minor>         starting cash, integer minor units (default 1000000)

The market data and strategy are deliberate fixtures (SRS-BT-001 verification step:
\"fixture market data, provider mocks\"). The seeded span is 2024-01-02..2024-01-09;
sub-select any window inside it to watch replay restrict to the selected dates.

DEFERRED (SRS-BT-001 keeps passes:false):
    --source uploaded   the user-uploaded Apache Parquet reader (SyRS SYS-14 / AC-4
                        / IF-5) needs a new third-party crate + SyRS scope approval
                        and belongs in the data layer (AC-8 / SYS-27).
    REST + dashboard    POST /api/v1/backtests (SRS-API-001) and the dashboard date
                        pickers (SRS-UI) wrap this in-process launch surface.
    Python strategy     the strategy here is a fixture, not user-authored Python.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt001_backtest_cli: {err}");
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
        "run" => cmd_run(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

/// Launch a backtest over the operator-selected window via the real system-catalog
/// reader.
fn cmd_run(args: &[String]) -> Result<(), String> {
    // Parse the whole argument list up front against the known-flag allowlist, so an
    // UNKNOWN or misspelled flag (e.g. `--sorce uploaded`) is rejected rather than
    // silently ignored — otherwise the CLI would fall back to a default (system data)
    // the operator never intended and report success, defeating fail-closed source
    // selection.
    let flags = parse_flags(
        args,
        &["--symbol", "--start", "--end", "--source", "--cash"],
    )?;
    let symbol = flags
        .get("--symbol")
        .cloned()
        .unwrap_or_else(|| FIXTURE_SYMBOL.to_string());
    let start = flags
        .get("--start")
        .cloned()
        .ok_or_else(|| format!("--start is required\n\n{USAGE}"))?;
    let end = flags
        .get("--end")
        .cloned()
        .ok_or_else(|| format!("--end is required\n\n{USAGE}"))?;
    let source = flags
        .get("--source")
        .cloned()
        .unwrap_or_else(|| "system".to_string());
    let starting_cash_minor = match flags.get("--cash") {
        Some(raw) => raw
            .parse::<i64>()
            .map_err(|_| format!("--cash must be an integer minor-unit amount, got {raw:?}"))?,
        None => DEFAULT_CASH_MINOR,
    };

    // Data-source selection (the AC's "system data or uploaded Parquet data"). Only
    // `system` is wired; `uploaded` fails closed rather than silently running system
    // data under an UploadedData label.
    match source.as_str() {
        "system" => {}
        "uploaded" => {
            return Err(
                "data source 'uploaded' (Apache Parquet) is DEFERRED: the user-uploaded \
                 Parquet reader (SyRS SYS-14 / AC-4 / IF-5) needs a new third-party Rust crate \
                 (arrow/parquet) + SyRS scope approval and belongs in the data layer behind the \
                 unified interface (AC-8 / SYS-27). Use --source system. SRS-BT-001 stays \
                 passes:false until the Parquet reader, the Python strategy host, and the REST/\
                 dashboard launch surface (SRS-API-001 / SRS-UI) land."
                    .to_string(),
            );
        }
        other => {
            return Err(format!(
                "unknown --source {other:?}: expected 'system' (default) or 'uploaded' (deferred)"
            ));
        }
    }

    // Reject a non-positive starting balance at the operator input boundary: a zero
    // or negative starting cash is a nonsensical launch (and the persistence layer
    // rejects a negative starting cash on the resulting record), so fail closed
    // before constructing the request rather than reporting a successful run over a
    // fabricated balance.
    if starting_cash_minor <= 0 {
        return Err(format!(
            "--cash must be a positive minor-unit amount, got {starting_cash_minor}"
        ));
    }

    // Parse the configurable [start, end] calendar window into the engine's
    // epoch-second range — fail-closed on malformed / impossible / inverted dates.
    let window: LaunchWindow = parse_window(&start, &end).map_err(|err| err.to_string())?;

    // Read bars from the REAL stored catalog via StoreBarSource (the source-neutral
    // SRS-DATA-007 unified query) — shipped product code. The fixture catalog is seeded
    // under a FIXED symbol independent of `--symbol`, so requesting a symbol that is not
    // in stored data fails closed (the engine returns EmptyData) rather than fabricating
    // a catalog under the requested name — the launch's stored-data provenance is real.
    let store = fixture_store()?;
    let bar_source = StoreBarSource::daily(&store, Normalization::Raw);
    let request = BacktestRequest {
        strategy_id: StrategyId::new("bt001-launch-demo"),
        symbol: symbol.clone(),
        data_source: BacktestDataSource::SystemData,
        range: window.range,
        starting_cash_minor,
        cost_config: CostConfig::default(),
    };
    let mut strategy = BuyOnceAndHold { lot: FIXTURE_LOT };
    let result: BacktestResult = BacktestEngine::new()
        .run(&request, &mut strategy, &bar_source)
        .map_err(|err| match err {
            BacktestError::EmptyData => format!(
                "no stored bars for symbol {symbol:?} in {} .. {} — the requested symbol/window \
                 is not in the system catalog (the fixture catalog holds symbol {FIXTURE_SYMBOL:?} \
                 over 2024-01-02..2024-01-09)",
                window.start_date, window.end_date
            ),
            other => format!("backtest run failed: {other:?}"),
        })?;

    print_result(&symbol, starting_cash_minor, &window, &result);
    Ok(())
}

/// Render the launched window and the produced backtest artifacts.
fn print_result(
    symbol: &str,
    starting_cash_minor: i64,
    window: &LaunchWindow,
    result: &BacktestResult,
) {
    println!("SRS-BT-001 LAUNCH OK");
    println!("  source:          {}", result.data_source.as_str());
    println!("  symbol:          {symbol}");
    println!(
        "  window (dates):  {} .. {} (inclusive)",
        window.start_date, window.end_date
    );
    println!(
        "  window (epoch):  {} .. {}",
        result.range.start, result.range.end
    );
    println!("  starting cash:   {starting_cash_minor} minor");
    println!("  bars processed:  {}", result.bars_processed);
    println!("  trades:          {}", result.trade_log.len());
    for fill in &result.trade_log {
        println!(
            "    fill ts={} {} qty={} @ {} minor (commission={} slippage={} spread={})",
            fill.ts,
            fill.symbol,
            fill.quantity,
            fill.price_minor,
            fill.commission_minor,
            fill.slippage_minor,
            fill.spread_impact_minor,
        );
    }
    if let (Some(first), Some(last)) = (result.equity_curve.first(), result.equity_curve.last()) {
        println!(
            "  equity curve:    {} point(s), first ts={} {} minor -> last ts={} {} minor",
            result.equity_curve.len(),
            first.ts,
            first.equity_minor,
            last.ts,
            last.equity_minor,
        );
    } else {
        println!("  equity curve:    0 point(s)");
    }
    println!("  final equity:    {} minor", result.final_equity_minor);
}

/// Build a real [`MarketDataStore`] seeded with the fixture daily bars under the
/// fixed [`FIXTURE_SYMBOL`] (independent of the operator's `--symbol`, so an unseeded
/// symbol fails closed). Bar timestamps are the calendar date's epoch-second
/// midnight, so the launch window's date selection lines up with the stored bars.
fn fixture_store() -> Result<MarketDataStore, String> {
    let mut store = MarketDataStore::new();
    for (date, close_minor) in FIXTURE_BARS {
        let event_ts = epoch_seconds(date)?;
        let record = daily_bar(FIXTURE_SYMBOL, event_ts, *close_minor)?;
        store
            .upsert(record)
            .map_err(|err| format!("seeding fixture bar for {date}: {err:?}"))?;
    }
    Ok(store)
}

/// Epoch-second midnight (UTC) for a `YYYY-MM-DD` date, via the shared launch
/// binding so the fixture and the operator's window agree on the time axis.
fn epoch_seconds(date: &str) -> Result<i64, String> {
    let day = atp_simulation::launch::parse_epoch_day(date).map_err(|err| err.to_string())?;
    Ok(day * atp_simulation::launch::SECONDS_PER_DAY as i64)
}

/// A well-formed daily equity bar record (kind/resolution match StoreBarSource::daily).
fn daily_bar(symbol: &str, event_ts: i64, close_minor: i64) -> Result<MarketDataRecord, String> {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [
            MarketField {
                name: "close".to_string(),
                value_minor: close_minor,
            },
            MarketField {
                name: "volume".to_string(),
                value_minor: 1_000,
            },
        ],
    )
    .map_err(|err| format!("building fixture bar: {err:?}"))
}

/// Buys `lot` shares on the first bar it sees, then holds — a deterministic fixture
/// strategy. The fill price equals the first in-window bar's close, so the launched
/// date window is directly observable in the trade log.
struct BuyOnceAndHold {
    lot: i64,
}

impl BacktestStrategy for BuyOnceAndHold {
    fn on_bar(&mut self, _bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if position == 0 {
            Ok(self.lot)
        } else {
            Ok(0)
        }
    }
}

// --------------------------------------------------------------------------- //
// Fail-closed flag parsing (allowlist; no external arg crate)
// --------------------------------------------------------------------------- //

/// Parse `args` into a `flag -> value` map against the `known` allowlist, every
/// known flag taking a value. **Fail-closed:** an unknown / misspelled flag, a bare
/// positional argument, a duplicate flag, or a flag whose value is missing (or is
/// itself another flag) is rejected — never silently dropped. Silently ignoring an
/// unrecognized flag would let the CLI fall back to a default the operator did not
/// intend (e.g. a `--sorce uploaded` typo launching system data) and report success.
fn parse_flags(args: &[String], known: &[&str]) -> Result<BTreeMap<String, String>, String> {
    let mut map = BTreeMap::new();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        if !known.contains(&arg.as_str()) {
            return Err(format!(
                "unknown or unexpected argument {arg:?} (expected one of {})\n\n{USAGE}",
                known.join(", ")
            ));
        }
        if map.contains_key(arg) {
            return Err(format!("duplicate flag {arg:?}"));
        }
        let value = iter
            .next()
            .ok_or_else(|| format!("{arg} requires a value"))?;
        // A value that is itself a known flag means the operator omitted this flag's
        // value (`--start --end ...`); reject rather than swallow the next flag.
        if known.contains(&value.as_str()) {
            return Err(format!("{arg} requires a value (got the flag {value:?})"));
        }
        map.insert(arg.clone(), value.clone());
    }
    Ok(map)
}
