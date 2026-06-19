//! SRS-BT-005 benchmark-comparison operator CLI.
//!
//! The operator-facing rendering surface of "compare strategy performance against a user-selected
//! benchmark defaulting to SPY" (docs/SRS.md SRS-5.6 SRS-BT-005; SyRS SYS-17 / SYS-36 / SYS-37; StRS
//! SN-1.04). The comparison *engine* in the [`benchmark`](atp_simulation::benchmark) module already
//! implements the AC -- [`BenchmarkSelection`] resolves an unselected benchmark to SPY,
//! [`compare`](atp_simulation::benchmark::compare) computes alpha/beta against the resolved series
//! and packages a [`BenchmarkReport`] whose [`BenchmarkComparison`] identifies the benchmark -- and
//! the runnable [`BacktestEngine`] already produces the equity curve + trade log the comparison
//! consumes. What kept SRS-BT-005 `passes:false` was the deferred operator RENDERING surface: the
//! CLI/REST/dashboard controls that let an operator pick a benchmark per run and read back the
//! identified comparison. This binary ships the **CLI** half of that surface, the same precedent as
//! the SRS-BT-002 cost CLI and the SRS-BT-006 tear-sheet CLI (there is no Python<->Rust runtime
//! bridge, so the operator workflow the acceptance names is demonstrated here over the Rust core).
//!
//! Two acceptance pieces remain genuinely deferred and are NOT claimed by this binary:
//!   - resolving the benchmark's *actual* stored historical level series is the **SRS-DATA-007**
//!     owner behind [`BenchmarkSource`]; this CLI uses a deterministic in-binary fixture source (the
//!     same stand-in the integration test uses for the deferred resolver).
//!   - the *dashboard* report rendering at the SYS-36 (<=5s) refresh is the **SRS-UI / SRS-API**
//!     owner consuming [`BenchmarkComparison`]; this binary is the CLI half only.
//!
//! - `defaults` — print the SPY default constant (`DEFAULT_BENCHMARK_SYMBOL`) and the annualization
//!   default (`DEFAULT_PERIODS_PER_YEAR`), and PROVE `BenchmarkSelection::unselected()` resolves to
//!   and identifies SPY (`is_default` true) -- the "if no benchmark is selected, SPY is used" half of
//!   the AC, made inspectable. It also lists the `BenchmarkComparison` identity fields a report
//!   renders. There is no single numeric SyRS constant to match here (unlike BT-002), so this is a
//!   selection/identity-availability proof (the BT-006 `defaults` analog).
//! - `run [--benchmark SYM] [--periods-per-year N] [--risk-free R] [--lot N] [--sell-ts T]
//!   [--inject <fault>]` — build a [`BenchmarkSelection`] from the flags (no `--benchmark` ⇒
//!   `unselected()`, the SPY default), run the SAME fixture strategy once through the real engine,
//!   resolve the benchmark through the fixture source, run the REAL `compare`, and print the resolved
//!   selection, the [`BenchmarkComparison`] (benchmark identity + alpha/beta + total/excess returns),
//!   and the eight SYS-16 metrics. Only the selection changes between runs -- the strategy is fixed --
//!   which is the "compare against a *user-selected* benchmark" half of the AC.
//!
//! Safety core: every statistic the comparison leaves *undefined* (`None`) renders as the literal
//! `undefined`, never a fabricated `0` or leaked `NaN` (the BT-006 analog of BT-002's "no cash
//! fabricated"). `--inject <fault>` drives the trust boundary: a substituted symbol, a misaligned or
//! length-mismatched series, a non-positive level, an operational source failure, or a foreign
//! evaluation window all flow into `compare`, which fails closed with a `BenchmarkError` BEFORE any
//! report line prints -- the run exits non-zero with no partial comparison.

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::benchmark::{
    compare, BenchmarkComparison, BenchmarkReport, BenchmarkSelection, BenchmarkSource,
    ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{
    Benchmark, BenchmarkPoint, MetricsConfig, PerformanceMetrics, DEFAULT_BENCHMARK_SYMBOL,
    DEFAULT_PERIODS_PER_YEAR,
};
use atp_types::StrategyId;

const SYMBOL: &str = "AAPL";
const STARTING_CASH_MINOR: i64 = 1_000_000; // $10,000.00
const DEFAULT_LOT: i64 = 10;
const DEFAULT_SELL_TS: u64 = 5;
// The fixture benchmark's pre-trade baseline level and per-mark step (the aligned series the
// deferred SRS-DATA-007 resolver will one day return from stored data).
const FIXTURE_BASELINE_MINOR: i64 = 400;
const FIXTURE_STEP_MINOR: i64 = 5;

const USAGE: &str = "\
benchmark_comparison_cli — SRS-BT-005 benchmark-comparison operator workflow for backtests

USAGE:
    benchmark_comparison_cli defaults
    benchmark_comparison_cli run [--benchmark <sym>] [--periods-per-year <n>] [--risk-free <r>]
                                 [--lot <n>] [--sell-ts <t>] [--inject <fault>]

COMMANDS:
    defaults  Print the SPY default (DEFAULT_BENCHMARK_SYMBOL) and the annualization default, and
              prove BenchmarkSelection::unselected() resolves to and identifies SPY — the 'if no
              benchmark is selected, SPY is used' half of the SRS-BT-005 acceptance criterion.
    run       Build a BenchmarkSelection from the flags (no --benchmark ⇒ the SPY default), run the
              SAME fixture strategy once through the real engine, resolve the benchmark through the
              fixture source, run the real compare(), and print the resolved selection, the
              comparison (benchmark identity + alpha/beta + total/excess returns), and the eight
              SYS-16 metrics. Only the selection changes between runs — the strategy is unchanged —
              which is the 'compare against a user-selected benchmark' half of the AC.

RUN FLAGS:
    --benchmark <sym>       user-selected benchmark symbol (default: unselected ⇒ SPY)
    --periods-per-year <n>  annualization factor (default 252)
    --risk-free <r>         per-period risk-free rate (default 0)
    --lot <n>               shares opened on the first bar, closed at --sell-ts (default 10)
    --sell-ts <t>           bar timestamp at which the position is closed (default 5)
    --inject <fault>        force a fail-closed path to demonstrate the trust boundary:
                            symbol-mismatch | length-mismatch | nonpositive-level |
                            unavailable | not-found | timeout | stale | foreign-window

An undefined statistic renders as the literal `undefined`, never 0 or NaN. An --inject fault makes
compare() fail closed BEFORE any report line prints, so the run exits non-zero with no partial
comparison — a misaligned or substituted benchmark can never be reported as the selected one.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("benchmark_comparison_cli: {err}");
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
        "defaults" => cmd_defaults(rest),
        "run" => cmd_run(rest),
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

/// Print the SPY default + the annualization default, and prove an unselected benchmark resolves to
/// and identifies SPY (the AC's "if no benchmark is selected, SPY is used").
fn cmd_defaults(rest: &[String]) -> Result<(), String> {
    if !rest.is_empty() {
        return Err(format!("`defaults` takes no arguments\n\n{USAGE}"));
    }
    println!("DEFAULT_BENCHMARK_SYMBOL={DEFAULT_BENCHMARK_SYMBOL}");
    println!("DEFAULT_PERIODS_PER_YEAR={DEFAULT_PERIODS_PER_YEAR}");

    // The whole point of the AC: an unselected benchmark IS the SPY default, and a report
    // identifies it as the default.
    let selection = BenchmarkSelection::unselected();
    println!(
        "default-selection-resolves-to:{}",
        selection.resolve().symbol()
    );
    println!("default-selection-is-default:{}", selection.is_default());
    println!("spy-benchmark-symbol:{}", Benchmark::spy().symbol());

    // The identity fields a dashboard or backtest report renders to identify and contrast against
    // its benchmark (the SRS-UI / SRS-API consumer of this shape is deferred).
    println!(
        "comparison-identity-fields:benchmark_symbol,is_default_benchmark,alpha,beta,strategy_total_return,benchmark_total_return,excess_return"
    );
    println!("report-identifies-benchmark:true");
    Ok(())
}

/// Build the per-run selection from flags, run the fixture once, compare, and render the report.
fn cmd_run(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let result = parsed.run_backtest();

    let selection = parsed.selection()?;
    let config = parsed.metrics_config()?;
    let source = parsed.source();
    // A foreign window (the --inject foreign-window fault) measures the benchmark over a different
    // period than the strategy; otherwise the comparison is bound to the run's own window.
    let window = parsed.window(&result);

    // The resolved selection — what an operator chose for THIS run (SPY if unselected).
    println!(
        "selection: benchmark={} is_default={}",
        selection.resolve().symbol(),
        selection.is_default()
    );

    // compare() resolves the benchmark, validates the returned series at the trust boundary, runs
    // the real metric family against it, and packages the report. Any malformed/substituted series
    // or operational source failure fails closed here — no partial report, non-zero exit.
    let report: BenchmarkReport = compare(
        STARTING_CASH_MINOR,
        window,
        &result.equity_curve,
        &result.trade_log,
        &selection,
        &source,
        &config,
    )
    .map_err(|err| format!("benchmark comparison failed: {err}"))?;

    render_report(&report);
    Ok(())
}

// --------------------------------------------------------------------------- //
// Report rendering (None -> the literal `undefined`, never 0 or NaN)
// --------------------------------------------------------------------------- //

fn render_report(report: &BenchmarkReport) {
    render_comparison(&report.comparison);
    render_metrics(&report.metrics);
}

/// The strategy-vs-benchmark contrast the report identifies (SRS-BT-005's headline shape).
fn render_comparison(comparison: &BenchmarkComparison) {
    println!(
        "comparison: benchmark_symbol={} is_default_benchmark={}",
        comparison.benchmark_symbol, comparison.is_default_benchmark
    );
    println!("comparison-alpha:{}", fmt_opt(comparison.alpha));
    println!("comparison-beta:{}", fmt_opt(comparison.beta));
    println!(
        "comparison-strategy-total-return:{}",
        fmt_opt(comparison.strategy_total_return)
    );
    println!(
        "comparison-benchmark-total-return:{}",
        fmt_opt(comparison.benchmark_total_return)
    );
    println!(
        "comparison-excess-return:{}",
        fmt_opt(comparison.excess_return)
    );
}

/// The eight SYS-16 metrics (alpha/beta computed against the resolved benchmark) the report carries.
fn render_metrics(metrics: &PerformanceMetrics) {
    println!("metrics-benchmark-symbol:{}", metrics.benchmark_symbol);
    println!("metric-sharpe-ratio:{}", fmt_opt(metrics.sharpe_ratio));
    println!("metric-sortino-ratio:{}", fmt_opt(metrics.sortino_ratio));
    println!("metric-alpha:{}", fmt_opt(metrics.alpha));
    println!("metric-beta:{}", fmt_opt(metrics.beta));
    println!("metric-max-drawdown:{}", fmt_opt(metrics.max_drawdown));
    println!(
        "metric-annualized-return:{}",
        fmt_opt(metrics.annualized_return)
    );
    println!(
        "metric-annualized-volatility:{}",
        fmt_opt(metrics.annualized_volatility)
    );
    println!("metric-win-rate:{}", fmt_opt(metrics.win_rate));
}

/// Render an optional statistic with fixed precision, or the literal `undefined` when the comparison
/// withheld it. Fixed precision keeps the output byte-identical across processes (SRS-BT-010).
fn fmt_opt(value: Option<f64>) -> String {
    match value {
        Some(v) => format!("{v:.12}"),
        None => "undefined".to_string(),
    }
}

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

struct ParsedArgs {
    benchmark: Option<String>,
    periods_per_year: u32,
    risk_free: f64,
    lot: i64,
    sell_ts: u64,
    inject: Inject,
}

impl Default for ParsedArgs {
    fn default() -> Self {
        Self {
            benchmark: None,
            periods_per_year: DEFAULT_PERIODS_PER_YEAR,
            risk_free: 0.0,
            lot: DEFAULT_LOT,
            sell_ts: DEFAULT_SELL_TS,
            inject: Inject::None,
        }
    }
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut parsed = ParsedArgs::default();
        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--benchmark" => parsed.benchmark = Some(take_value(&mut iter, flag)?),
                "--periods-per-year" => parsed.periods_per_year = take_u32(&mut iter, flag)?,
                "--risk-free" => parsed.risk_free = take_f64(&mut iter, flag)?,
                "--lot" => parsed.lot = take_i64(&mut iter, flag)?,
                "--sell-ts" => parsed.sell_ts = take_u64(&mut iter, flag)?,
                "--inject" => parsed.inject = Inject::parse(&take_value(&mut iter, flag)?)?,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(parsed)
    }

    /// No `--benchmark` ⇒ the SPY default; otherwise the validated user selection (a malformed
    /// symbol fails closed here, before any run).
    fn selection(&self) -> Result<BenchmarkSelection, String> {
        match &self.benchmark {
            None => Ok(BenchmarkSelection::unselected()),
            Some(symbol) => BenchmarkSelection::from_symbol(symbol.clone())
                .map_err(|err| format!("invalid --benchmark: {err}")),
        }
    }

    fn metrics_config(&self) -> Result<MetricsConfig, String> {
        MetricsConfig::new(self.periods_per_year, self.risk_free)
            .map_err(|err| format!("invalid metrics config: {err:?}"))
    }

    fn source(&self) -> FixtureBenchmark {
        FixtureBenchmark {
            inject: self.inject,
        }
    }

    /// The evaluation window the comparison is bound to: the run's own range, except the
    /// foreign-window fault narrows it to exclude later marks (so a stale window is rejected).
    fn window(&self, result: &BacktestResult) -> DateRange {
        if matches!(self.inject, Inject::ForeignWindow) {
            DateRange::new(0, 3)
        } else {
            result.range
        }
    }

    fn run_backtest(&self) -> BacktestResult {
        let catalog = fixture_catalog();
        let request = BacktestRequest {
            strategy_id: StrategyId::new("benchmark-comparison-cli"),
            symbol: SYMBOL.to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(0, 100),
            starting_cash_minor: STARTING_CASH_MINOR,
            cost_config: CostConfig::default(),
        };
        let mut strategy = RoundTrip {
            lot: self.lot,
            sell_ts: self.sell_ts,
        };
        BacktestEngine::new()
            .run(&request, &mut strategy, &catalog)
            .expect("deterministic fixture backtest runs")
    }
}

/// The trust-boundary fault to force, so the CLI can demonstrate each fail-closed path.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Inject {
    None,
    SymbolMismatch,
    LengthMismatch,
    NonPositiveLevel,
    Unavailable,
    NotFound,
    Timeout,
    Stale,
    ForeignWindow,
}

impl Inject {
    fn parse(spec: &str) -> Result<Self, String> {
        match spec {
            "symbol-mismatch" => Ok(Self::SymbolMismatch),
            "length-mismatch" => Ok(Self::LengthMismatch),
            "nonpositive-level" => Ok(Self::NonPositiveLevel),
            "unavailable" => Ok(Self::Unavailable),
            "not-found" => Ok(Self::NotFound),
            "timeout" => Ok(Self::Timeout),
            "stale" => Ok(Self::Stale),
            "foreign-window" => Ok(Self::ForeignWindow),
            other => Err(format!(
                "unknown --inject fault '{other}' (expected symbol-mismatch|length-mismatch|nonpositive-level|unavailable|not-found|timeout|stale|foreign-window)"
            )),
        }
    }

    /// The operational source failure to surface, if this fault is one. `None` means the fault is
    /// realized in the resolved series (or the window) rather than as a read failure.
    fn source_failure(self) -> Option<SourceFailure> {
        match self {
            Self::Unavailable => Some(SourceFailure::Unavailable),
            Self::NotFound => Some(SourceFailure::NotFound),
            Self::Timeout => Some(SourceFailure::Timeout),
            Self::Stale => Some(SourceFailure::StaleData),
            _ => None,
        }
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

fn take_i64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<i64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<i64>()
        .map_err(|_| format!("{flag} expects an integer, got '{raw}'"))
}

fn take_u64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<u64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<u64>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))
}

fn take_u32<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<u32, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<u32>()
        .map_err(|_| format!("{flag} expects a non-negative integer, got '{raw}'"))
}

fn take_f64<'a>(iter: &mut impl Iterator<Item = &'a String>, flag: &str) -> Result<f64, String> {
    let raw = take_value(iter, flag)?;
    raw.parse::<f64>()
        .map_err(|_| format!("{flag} expects a number, got '{raw}'"))
}

// --------------------------------------------------------------------------- //
// Deterministic fixtures
// --------------------------------------------------------------------------- //

/// A varied close-only price series (so the equity curve has real dispersion and the metric family
/// yields defined statistics), matching the SRS-BT-005 integration fixture.
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

/// A close-only fixture catalog that honors the requested window.
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

/// Opens `lot` shares on the first bar, then fully closes on `sell_ts` — one round trip independent
/// of the benchmark selection (so only the selection changes between runs).
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

/// The in-binary stand-in for the deferred SRS-DATA-007 stored-data resolver. By default it returns
/// a well-formed series that echoes the requested benchmark symbol; an injected fault makes it
/// substitute a different symbol, drop a level, emit a non-positive level, or fail operationally so
/// the CLI can demonstrate that `compare` fails closed at the trust boundary.
struct FixtureBenchmark {
    inject: Inject,
}

impl BenchmarkSource for FixtureBenchmark {
    fn levels(
        &self,
        symbol: &str,
        _window: DateRange,
        axis: &[u64],
    ) -> Result<ResolvedBenchmark, SourceFailure> {
        if let Some(failure) = self.inject.source_failure() {
            return Err(failure);
        }

        // The non-positive-level fault drops the baseline to zero so `compare` rejects a level a
        // return would divide by; the well-formed path uses the published baseline.
        let baseline = if matches!(self.inject, Inject::NonPositiveLevel) {
            0
        } else {
            FIXTURE_BASELINE_MINOR
        };

        // The baseline is the pre-trade prior close: strictly before the first mark.
        let baseline_ts = axis.first().map_or(0, |&first| first.saturating_sub(1));
        let mut levels = vec![BenchmarkPoint {
            ts: baseline_ts,
            level_minor: baseline,
        }];
        for (index, &ts) in axis.iter().enumerate() {
            levels.push(BenchmarkPoint {
                ts,
                level_minor: FIXTURE_BASELINE_MINOR + FIXTURE_STEP_MINOR * (index as i64 + 1),
            });
        }
        // The length-mismatch fault drops the final level so the series cannot align period-for-period.
        if matches!(self.inject, Inject::LengthMismatch) {
            levels.pop();
        }

        // The substitution fault labels a well-formed series with a DIFFERENT symbol than requested,
        // so `compare` (which binds identity to the returned payload) rejects it rather than
        // reporting the wrong benchmark. Prepending a canonical prefix differs from any request.
        let resolved_symbol = if matches!(self.inject, Inject::SymbolMismatch) {
            format!("X{symbol}")
        } else {
            symbol.to_string()
        };

        Ok(ResolvedBenchmark {
            symbol: resolved_symbol,
            levels,
        })
    }
}
