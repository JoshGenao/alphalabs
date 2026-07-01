//! SRS-BT-009 operator persist/query CLI.
//!
//! Phase 2 of the SRS-BT-009 closeout (`docs/plans/bt009-closeout.md` §6): the operator-facing
//! workflow that exercises the durable [`BacktestResultStore`] file layer Phase 1 shipped. There is
//! no Python<->Rust runtime bridge, so this is a small Rust binary in the simulation crate that
//! demonstrates the SRS-BT-009 acceptance end to end over the *public* store API:
//!
//! - `persist [--dir D] [--run-id R --strategy S --completed-at T [--param k=v]...]` — run a
//!   deterministic fixture backtest through the real [`BacktestEngine`], bundle it into a
//!   [`BacktestRecord`] (the seven SRS-BT-009 artifacts), and durably persist it via
//!   [`BacktestResultStore::save_to_path`]. Existing history is loaded first and preserved
//!   (load-modify-save), so a persist *accumulates* rather than overwrites. With no provenance
//!   flags it seeds an idempotent demo pair. The blob on disk is the checksummed codec output, so
//!   an operator can inspect it directly.
//! - `query [--dir D] [--strategy S] [--from T --to T] [--completed-from T --completed-to T]
//!   [--param k=v]... [--full]` — load the persisted store with
//!   [`BacktestResultStore::load_from_path`] and print every record matching the requested axes,
//!   with all seven artifacts (`--full` renders the complete trade log and equity curve, not just
//!   their first/last summaries).
//!
//! The results directory is resolved fail-closed: an explicit `--dir` wins, else the
//! `ATP_BACKTEST_RESULTS_DIR` config key (read here as an environment variable — the configuration
//! layer that validates it lives in `python/atp_config`), else an error. A misconfigured /
//! unmounted directory surfaces as a store error rather than a silently empty history.
//!
//! Scope: the SSD/NAS tiering of this directory (SRS-DATA-008), the dashboard history view
//! (SRS-UI-004 / SRS-API-001), and a real orchestrated Python-strategy producer (SRS-BT-001-runtime)
//! are deferred. The market data and strategy here are deliberate fixtures, exactly as the
//! SRS-BT-009 verification step specifies ("fixture market data, provider mocks").
//!
//! This CLI is a **single-logical-writer** tool, matching the store's documented contract and the
//! single-user / single-orchestrator baseline (AGENTS.md): each `persist` is a load-modify-save and
//! is safe when one writer runs at a time. Coordinating genuinely *concurrent* writers so no run is
//! lost (a cross-process lock + merge) is **out of SRS-BT-009's acceptance scope** and remains the
//! deferred orchestrator single-writer path / SRS-DATA-008 owner — persisting concurrently from two
//! processes can drop a run (last-publish-wins), the same boundary documented on
//! [`BacktestResultStore::save_to_path`].

use std::env;
use std::path::PathBuf;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange, EquityPoint, Fill,
};
use atp_simulation::backtest_store::{
    BacktestRecord, BacktestResultStore, CodeVersion, RecordQuery, RunId, StrategyParameters,
    STORE_FILENAME,
};
use atp_simulation::benchmark::{
    compare, BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

const USAGE: &str = "\
bt009_store_cli — SRS-BT-009 operator persist/query workflow for completed backtest results

USAGE:
    bt009_store_cli persist [--dir <path>] [--init]
    bt009_store_cli persist [--dir <path>] [--init] --run-id <id> --strategy <id> --completed-at <ts> [--param <k=v>]...
    bt009_store_cli query   [--dir <path>] [--strategy <id>] [--from <ts> --to <ts>]
                            [--completed-from <ts> --completed-to <ts>] [--param <k=v>]... [--full]

The results directory is taken from --dir, else the ATP_BACKTEST_RESULTS_DIR environment
variable. A missing/unmounted directory fails closed (for both persist and query) rather than
masquerading as an empty history; pass --init to persist into a brand-new directory.

COMMANDS:
    persist    Run a deterministic fixture backtest and durably persist it. Existing history is
               loaded first and preserved (load-modify-save), so persists accumulate. The results
               directory must already be provisioned unless --init is given (fresh install). With no
               provenance flags an idempotent demo pair is seeded; with --run-id one labeled run is
               persisted and a duplicate run id fails closed.
    query      Load the persisted store and print records matching the requested axes.

QUERY AXES (each optional; combined axes AND together):
    --strategy <id>                 records for one strategy
    --from <ts> --to <ts>           records whose backtest run window overlaps [from, to]
    --completed-from --completed-to records completed within the inclusive window
    --param <k=v>                   records run with exactly this parameter set (repeatable)
    --full                          render the complete trade log and equity curve per record
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt009_store_cli: {err}");
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
        "persist" => cmd_persist(rest),
        "query" => cmd_query(rest),
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

/// Persist completed fixture backtests, loading any existing history first so a persist never
/// clobbers previously persisted runs.
///
/// The store is a **single-logical-writer load-modify-save**: the existing store is read, the new
/// record(s) are inserted, and the whole store is republished atomically. Coordinating genuinely
/// concurrent multi-writer merges (a lock / single-writer guard) is the deferred SRS-DATA-008 /
/// orchestrator single-writer owner — out of scope for this single-user, local workflow.
///
/// The results directory must already be provisioned: a missing directory fails closed (symmetric
/// with query's load) rather than silently creating a fresh store at an unmounted / mistyped path
/// and forking the history. `--init` is the explicit fresh-install escape hatch that allows creating
/// a brand-new directory.
///
/// With no provenance flags the command seeds the default demo pair (idempotent: a run already
/// present is left untouched). With `--run-id` it persists one operator-labeled run and fails
/// closed on a duplicate run id rather than silently replacing it.
fn cmd_persist(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;

    // Load existing history so prior runs survive. Without --init the results directory must already
    // be provisioned: a missing directory is an unmounted / deleted / mistyped path and fails closed
    // (symmetric with query's load_from_path), NEVER a silent fresh store that would fork the
    // history at the wrong location. --init is the explicit fresh-install escape hatch.
    let mut store = if parsed.init && !dir.is_dir() {
        BacktestResultStore::new()
    } else {
        BacktestResultStore::load_from_path(&dir).map_err(|err| err.to_string())?
    };
    let before = store.len();

    match parsed.run_id.clone() {
        Some(run_id) => {
            // An operator-labeled single run: its provenance is required, and a duplicate run id
            // fails closed (via insert) rather than silently replacing the existing run.
            let strategy = parsed
                .strategy
                .clone()
                .ok_or("--run-id requires --strategy")?;
            let completed_at = parsed
                .completed_at
                .ok_or("--run-id requires --completed-at")?;
            let params = StrategyParameters::from_pairs(parsed.params.iter().cloned())
                .map_err(|err| err.to_string())?;
            let record = build_record(&FixtureSpec {
                run_id,
                strategy,
                completed_at,
                params,
            })?;
            store.insert(record).map_err(|err| err.to_string())?;
        }
        None => {
            // The default demo pair: insert each only if its run id is absent, so re-running the
            // seed is idempotent and never errors or drops existing runs.
            for spec in default_fixture_specs()? {
                let record = build_record(&spec)?;
                if store.records().iter().any(|r| r.run_id == record.run_id) {
                    continue;
                }
                store.insert(record).map_err(|err| err.to_string())?;
            }
        }
    }

    store.save_to_path(&dir).map_err(|err| err.to_string())?;

    let file = dir.join(STORE_FILENAME);
    println!(
        "persisted to {}; store now holds {} result(s) ({} new this run)",
        file.display(),
        store.len(),
        store.len() - before,
    );
    for record in store.records() {
        println!(
            "  - run={} strategy={} completed_at={} params=[{}]",
            record.run_id.as_str(),
            record.request.strategy_id.as_str(),
            record.completed_at_ts,
            format_params(&record.parameters),
        );
    }
    Ok(())
}

/// Load the persisted store and print every record matching the requested query axes.
fn cmd_query(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let dir = resolve_dir(parsed.dir.as_deref())?;

    let store = BacktestResultStore::load_from_path(&dir).map_err(|err| err.to_string())?;
    let query = parsed.to_query()?;
    let matches = store.query(&query);

    println!(
        "loaded {} record(s) from {}; {} match the query",
        store.len(),
        dir.join(STORE_FILENAME).display(),
        matches.len(),
    );
    print_filters(&parsed);
    for record in matches {
        print_record(record, parsed.full);
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

/// The parsed CLI flags shared by both subcommands.
#[derive(Default)]
struct ParsedArgs {
    dir: Option<String>,
    run_id: Option<String>,
    strategy: Option<String>,
    completed_at: Option<u64>,
    from: Option<u64>,
    to: Option<u64>,
    completed_from: Option<u64>,
    completed_to: Option<u64>,
    params: Vec<(String, String)>,
    full: bool,
    init: bool,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--dir" => parsed.dir = Some(take_value(&mut iter, flag)?),
                "--run-id" => parsed.run_id = Some(take_value(&mut iter, flag)?),
                "--strategy" => parsed.strategy = Some(take_value(&mut iter, flag)?),
                "--completed-at" => parsed.completed_at = Some(take_ts(&mut iter, flag)?),
                "--from" => parsed.from = Some(take_ts(&mut iter, flag)?),
                "--to" => parsed.to = Some(take_ts(&mut iter, flag)?),
                "--completed-from" => parsed.completed_from = Some(take_ts(&mut iter, flag)?),
                "--completed-to" => parsed.completed_to = Some(take_ts(&mut iter, flag)?),
                "--full" => parsed.full = true,
                "--init" => parsed.init = true,
                "--param" => {
                    let raw = take_value(&mut iter, flag)?;
                    let (key, value) = raw
                        .split_once('=')
                        .ok_or_else(|| format!("--param expects <key=value>, got '{raw}'"))?;
                    parsed.params.push((key.to_string(), value.to_string()));
                }
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// Map the parsed flags into a [`RecordQuery`]. A `--from`/`--to` (or completion) pair must be
    /// supplied together — a half-open date axis would be ambiguous, so it fails closed.
    fn to_query(&self) -> Result<RecordQuery, String> {
        let run_window = date_window(self.from, self.to, "--from", "--to")?;
        let completed_within = date_window(
            self.completed_from,
            self.completed_to,
            "--completed-from",
            "--completed-to",
        )?;
        let parameter_set = if self.params.is_empty() {
            None
        } else {
            Some(
                StrategyParameters::from_pairs(self.params.iter().cloned())
                    .map_err(|err| err.to_string())?,
            )
        };
        Ok(RecordQuery {
            strategy_id: self.strategy.as_ref().map(StrategyId::new),
            run_window,
            completed_within,
            parameter_set,
        })
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

fn take_ts<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<u64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<u64>()
        .map_err(|_| format!("{flag} expects a non-negative integer timestamp, got '{raw}'"))
}

/// Build an optional inclusive date window, failing closed unless both bounds are present and
/// `start <= end`.
fn date_window(
    start: Option<u64>,
    end: Option<u64>,
    start_flag: &str,
    end_flag: &str,
) -> Result<Option<DateRange>, String> {
    match (start, end) {
        (None, None) => Ok(None),
        (Some(start), Some(end)) if start <= end => Ok(Some(DateRange::new(start, end))),
        (Some(start), Some(end)) => Err(format!(
            "{start_flag} ({start}) must not exceed {end_flag} ({end})"
        )),
        _ => Err(format!(
            "{start_flag} and {end_flag} must be supplied together"
        )),
    }
}

/// Resolve the results directory: explicit `--dir`, else `ATP_BACKTEST_RESULTS_DIR`, else error.
fn resolve_dir(explicit: Option<&str>) -> Result<PathBuf, String> {
    if let Some(dir) = explicit {
        return Ok(PathBuf::from(dir));
    }
    match env::var("ATP_BACKTEST_RESULTS_DIR") {
        Ok(dir) if !dir.trim().is_empty() => Ok(PathBuf::from(dir)),
        _ => Err(
            "no results directory: pass --dir <path> or set ATP_BACKTEST_RESULTS_DIR".to_string(),
        ),
    }
}

// --------------------------------------------------------------------------- //
// Output
// --------------------------------------------------------------------------- //

fn print_filters(parsed: &ParsedArgs) {
    let mut filters: Vec<String> = Vec::new();
    if let Some(strategy) = &parsed.strategy {
        filters.push(format!("strategy={strategy}"));
    }
    if let (Some(from), Some(to)) = (parsed.from, parsed.to) {
        filters.push(format!("run_window=[{from},{to}]"));
    }
    if let (Some(from), Some(to)) = (parsed.completed_from, parsed.completed_to) {
        filters.push(format!("completed_within=[{from},{to}]"));
    }
    if !parsed.params.is_empty() {
        filters.push(format!(
            "params=[{}]",
            parsed
                .params
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    if filters.is_empty() {
        println!("filters: (none — all records)");
    } else {
        println!("filters: {}", filters.join(" AND "));
    }
}

/// Print one record's seven SRS-BT-009 artifacts: parameters, metrics, trade log, equity curve,
/// benchmark comparison, strategy code version, and timestamp (plus the launch request). With
/// `full` the complete trade log and equity curve are rendered entry-by-entry, so every interior
/// fill and equity mark is recoverable from the query output (not just the first/last summary).
fn print_record(record: &BacktestRecord, full: bool) {
    println!("record {}", record.run_id.as_str());
    println!(
        "    request:      strategy={} symbol={} source={} run_window=[{},{}] starting_cash_minor={}",
        record.request.strategy_id.as_str(),
        record.request.symbol,
        record.request.data_source.as_str(),
        record.request.range.start,
        record.request.range.end,
        record.request.starting_cash_minor,
    );
    println!("    parameters:   [{}]", format_params(&record.parameters));
    let m = &record.metrics;
    println!(
        "    metrics:      sharpe={} sortino={} alpha={} beta={} max_drawdown={} ann_return={} ann_vol={} win_rate={} benchmark={}",
        fmt_opt(m.sharpe_ratio),
        fmt_opt(m.sortino_ratio),
        fmt_opt(m.alpha),
        fmt_opt(m.beta),
        fmt_opt(m.max_drawdown),
        fmt_opt(m.annualized_return),
        fmt_opt(m.annualized_volatility),
        fmt_opt(m.win_rate),
        m.benchmark_symbol,
    );
    let c = &record.comparison;
    println!(
        "    comparison:   benchmark={} default={} alpha={} beta={} strat_total={} bench_total={} excess={}",
        c.benchmark_symbol,
        c.is_default_benchmark,
        fmt_opt(c.alpha),
        fmt_opt(c.beta),
        fmt_opt(c.strategy_total_return),
        fmt_opt(c.benchmark_total_return),
        fmt_opt(c.excess_return),
    );
    println!("    trade_log:    {}", fmt_trade_log(&record.trade_log));
    println!(
        "    equity_curve: {}",
        fmt_equity_curve(&record.equity_curve)
    );
    println!("    code_version: {}", record.code_version.as_str());
    println!("    completed_at: {}", record.completed_at_ts);

    if full {
        // The complete trade log and equity curve, entry by entry, so an operator can recover every
        // interior fill and equity mark from the query output — not just the first/last summary.
        for (index, fill) in record.trade_log.iter().enumerate() {
            println!(
                "        fill[{index}] ts={} symbol={} qty={} price_minor={} commission_minor={} slippage_minor={} spread_impact_minor={}",
                fill.ts,
                fill.symbol,
                fill.quantity,
                fill.price_minor,
                fill.commission_minor,
                fill.slippage_minor,
                fill.spread_impact_minor,
            );
        }
        for (index, point) in record.equity_curve.iter().enumerate() {
            println!(
                "        equity[{index}] ts={} equity_minor={}",
                point.ts, point.equity_minor
            );
        }
    }
}

fn format_params(parameters: &StrategyParameters) -> String {
    parameters
        .entries()
        .iter()
        .map(|(k, v)| format!("{k}={v}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn fmt_opt(value: Option<f64>) -> String {
    value.map_or_else(|| "n/a".to_string(), |v| v.to_string())
}

fn fmt_trade_log(fills: &[Fill]) -> String {
    match (fills.first(), fills.last()) {
        (Some(first), Some(last)) => format!(
            "{} fill(s) (first ts={} qty={} price_minor={}; last ts={} qty={} price_minor={})",
            fills.len(),
            first.ts,
            first.quantity,
            first.price_minor,
            last.ts,
            last.quantity,
            last.price_minor,
        ),
        _ => "0 fill(s)".to_string(),
    }
}

fn fmt_equity_curve(points: &[EquityPoint]) -> String {
    match (points.first(), points.last()) {
        (Some(first), Some(last)) => format!(
            "{} point(s) (first ts={} equity_minor={}; last ts={} equity_minor={})",
            points.len(),
            first.ts,
            first.equity_minor,
            last.ts,
            last.equity_minor,
        ),
        _ => "0 point(s)".to_string(),
    }
}

// --------------------------------------------------------------------------- //
// Deterministic fixture producer (mirrors the srs_bt_009_persist_query integration test)
// --------------------------------------------------------------------------- //

/// One fixture backtest to persist: the provenance labels (run id, strategy, parameter set,
/// completion timestamp) over the shared deterministic fixture market data.
struct FixtureSpec {
    run_id: String,
    strategy: String,
    completed_at: u64,
    params: StrategyParameters,
}

/// The default demo pair: distinct strategies, parameter sets, and completion timestamps, so every
/// SRS-BT-009 query axis tells them apart.
fn default_fixture_specs() -> Result<Vec<FixtureSpec>, String> {
    Ok(vec![
        FixtureSpec {
            run_id: "run-momentum".to_string(),
            strategy: "momentum".to_string(),
            completed_at: 1_700_000_000,
            params: parameters(&[("lookback", "20"), ("threshold", "0.5")])?,
        },
        FixtureSpec {
            run_id: "run-meanrev".to_string(),
            strategy: "meanrev".to_string(),
            completed_at: 1_700_000_500,
            params: parameters(&[("window", "5")])?,
        },
    ])
}

fn parameters(entries: &[(&str, &str)]) -> Result<StrategyParameters, String> {
    StrategyParameters::from_pairs(entries.iter().map(|(k, v)| (k.to_string(), v.to_string())))
        .map_err(|err| err.to_string())
}

/// The full producer chain: run a deterministic backtest through the real engine, compare it
/// against the SPY default benchmark, and bundle the seven artifacts into a validated record.
fn build_record(spec: &FixtureSpec) -> Result<BacktestRecord, String> {
    let request = BacktestRequest {
        strategy_id: StrategyId::new(spec.strategy.clone()),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(0, 100),
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config: CostConfig::default(),
    };

    let mut strategy = RoundTrip {
        lot: 10,
        sell_ts: 5,
    };
    let result: BacktestResult = BacktestEngine::new()
        .run(&request, &mut strategy, &fixture_catalog())
        .map_err(|err| format!("backtest run failed: {err:?}"))?;

    let source = FixtureBenchmark {
        symbol: "SPY".to_string(),
        baseline: 400,
        step: 5,
    };
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .map_err(|err| format!("benchmark comparison failed: {err:?}"))?;

    BacktestRecord::from_result(
        RunId::new(spec.run_id.clone()).map_err(|err| err.to_string())?,
        request,
        spec.params.clone(),
        report.metrics,
        report.comparison,
        &result,
        CodeVersion::new("sha:deadbeef").map_err(|err| err.to_string())?,
        spec.completed_at,
    )
    .map_err(|err| err.to_string())
}

fn fixture_catalog() -> FixtureCatalog {
    FixtureCatalog {
        bars: vec![
            bar(1, 100),
            bar(2, 120),
            bar(3, 90),
            bar(4, 130),
            bar(5, 125),
        ],
    }
}

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: SYMBOL.to_string(),
        ts,
        close_minor,
        spread_minor: None,
    }
}

/// A fixture catalog of close-only bars that honors the requested window.
struct FixtureCatalog {
    bars: Vec<BacktestBar>,
}

impl BarSource for FixtureCatalog {
    fn source(&self) -> BacktestDataSource {
        BacktestDataSource::SystemData
    }

    fn bars(
        &self,
        symbol: &str,
        range: &DateRange,
        max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        let rows: Vec<BacktestBar> = self
            .bars
            .iter()
            .filter(|bar| bar.symbol == symbol && range.contains(bar.ts))
            .cloned()
            .collect();
        if rows.len() > max_bars {
            return Err(BacktestError::TooManyBars {
                count: rows.len(),
                limit: max_bars,
            });
        }
        Ok(rows)
    }
}

/// Opens `lot` shares on the first bar, then fully closes on `sell_ts` — one round trip.
struct RoundTrip {
    lot: i64,
    sell_ts: u64,
}

impl BacktestStrategy for RoundTrip {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if bar.ts == self.sell_ts {
            return Ok(-position);
        }
        if position == 0 {
            return Ok(self.lot);
        }
        Ok(0)
    }
}

/// A well-formed aligned benchmark source (the stand-in for the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001) resolver).
struct FixtureBenchmark {
    symbol: String,
    baseline: i64,
    step: i64,
}

impl BenchmarkSource for FixtureBenchmark {
    fn levels(
        &self,
        _symbol: &str,
        _window: DateRange,
        axis: &[u64],
    ) -> Result<ResolvedBenchmark, SourceFailure> {
        let baseline_ts = axis.first().map_or(0, |&first| first.saturating_sub(1));
        let mut levels = vec![BenchmarkPoint {
            ts: baseline_ts,
            level_minor: self.baseline,
        }];
        for (index, &ts) in axis.iter().enumerate() {
            levels.push(BenchmarkPoint {
                ts,
                level_minor: self.baseline + self.step * (index as i64 + 1),
            });
        }
        Ok(ResolvedBenchmark {
            symbol: self.symbol.clone(),
            levels,
        })
    }
}
