//! `sim_paper_metrics_cli` — operator surface for the SRS-BT-004 / SYS-86 paper-strategy
//! performance-metric accumulator.
//!
//! SYS-86 requires the internal simulation engine to compute the SAME eight performance
//! metrics for a PAPER strategy that the backtest engine computes, so paper and backtest
//! performance are directly comparable. This CLI drives the
//! [`PaperMetricsAccumulator`](atp_simulation::paper_metrics::PaperMetricsAccumulator) over a
//! deterministic fixture paper run (a sequence of simulated fills + per-bar mark-to-market
//! instants) and renders the eight metrics, then proves the SYS-86 comparison by running the
//! IDENTICAL activity through the real [`BacktestEngine`] and asserting the two metric
//! families are equal.
//!
//! Subcommands:
//!   * `paper [--benchmark SYM]` — accumulate the fixture paper run and render its eight
//!     metrics (an undefined metric renders as the literal `undefined`, never 0 or NaN);
//!   * `parity [--benchmark SYM]` — run the same activity as a backtest and a paper
//!     accumulation and print `paper-backtest-parity:true` only if every metric matches;
//!   * `help`.
//!
//! Money is exact integer minor units throughout; the metrics are dimensionless f64 ratios.
//! Deferred (so SRS-BT-004 stays `passes:false`): the runtime that supplies the marks from
//! the live/paper feed (SYS-70 subscription manager) and the live dashboard reporting path
//! (SRS-UI / SRS-API, SYS-36 <= 5s). This CLI is the solo-demonstrable accumulator surface.

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{Benchmark, BenchmarkPoint, MetricsConfig, PerformanceMetrics};
use atp_simulation::paper_metrics::PaperMetricsAccumulator;
use atp_simulation::sim::PaperFill;
use atp_types::StrategyId;

/// The pre-trade starting cash the fixture run is launched with (minor units).
const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

const USAGE: &str = "\
sim_paper_metrics_cli — SRS-BT-004 / SYS-86 paper-strategy performance metrics

USAGE:
    sim_paper_metrics_cli <SUBCOMMAND> [--benchmark SYMBOL]

SUBCOMMANDS:
    paper      Accumulate the fixture paper run and render its eight metrics.
    parity     Prove the paper metric family equals the backtest family for the
               same activity (SYS-86 comparability).
    help       Show this message.

OPTIONS:
    --benchmark SYMBOL   Benchmark identity to report (default SPY). The fixture's
                         benchmark level series is fixed; only the identity changes.

A paper strategy reports the SAME eight metrics (Sharpe, Sortino, alpha, beta, max
drawdown, annualized return, annualized volatility, win rate) a backtest of the same
activity reports. An undefined metric renders as `undefined`, never 0 or NaN.
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("sim_paper_metrics_cli: {err}");
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
        "paper" => cmd_paper(rest),
        "parity" => cmd_parity(rest),
        "help" | "--help" | "-h" => {
            print!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown subcommand '{other}'\n\n{USAGE}")),
    }
}

/// Parse the only supported option, `--benchmark SYMBOL`, rejecting any unknown flag so a
/// typo fails loudly rather than being silently ignored. Returns the selected benchmark
/// identity (default SPY).
fn parse_benchmark(rest: &[String]) -> Result<Benchmark, String> {
    let mut benchmark = Benchmark::spy();
    let mut iter = rest.iter();
    while let Some(arg) = iter.next() {
        match arg.as_str() {
            "--benchmark" => {
                let symbol = iter
                    .next()
                    .ok_or_else(|| "--benchmark requires a SYMBOL argument".to_string())?;
                benchmark = Benchmark::new(symbol.clone())
                    .map_err(|err| format!("invalid benchmark symbol: {err:?}"))?;
            }
            other => return Err(format!("unknown argument '{other}'\n\n{USAGE}")),
        }
    }
    Ok(benchmark)
}

// --------------------------------------------------------------------------- //
// Subcommands
// --------------------------------------------------------------------------- //

fn cmd_paper(rest: &[String]) -> Result<(), String> {
    let benchmark = parse_benchmark(rest)?;
    let accumulator = accumulate_fixture_paper_run()?;
    let levels = fixture_benchmark_levels();
    let metrics = accumulator
        .compute_metrics(&benchmark, Some(&levels), &MetricsConfig::default())
        .map_err(|err| format!("paper metric computation failed: {err}"))?;

    println!("paper-run-symbol:{SYMBOL}");
    println!("paper-run-starting-cash-minor:{STARTING_CASH_MINOR}");
    println!("paper-run-final-cash-minor:{}", accumulator.cash_minor());
    println!(
        "paper-run-equity-points:{}",
        accumulator.equity_curve().len()
    );
    println!(
        "paper-run-trade-log-fills:{}",
        accumulator.trade_log().len()
    );
    render_metrics(&metrics);
    Ok(())
}

fn cmd_parity(rest: &[String]) -> Result<(), String> {
    let benchmark = parse_benchmark(rest)?;
    let config = MetricsConfig::default();
    let levels = fixture_benchmark_levels();

    // The backtest path: run the fixture activity through the real engine.
    let bars = fixture_bars();
    let result = run_fixture_backtest(&bars)?;
    let backtest_metrics = atp_simulation::metrics::compute(
        STARTING_CASH_MINOR,
        &result.equity_curve,
        &result.trade_log,
        &benchmark,
        Some(&levels),
        &config,
    )
    .map_err(|err| format!("backtest metric computation failed: {err:?}"))?;

    // The paper path: accumulate the same activity.
    let accumulator = accumulate_fixture_paper_run()?;
    let paper_metrics = accumulator
        .compute_metrics(&benchmark, Some(&levels), &config)
        .map_err(|err| format!("paper metric computation failed: {err}"))?;

    let curves_match = accumulator.equity_curve() == result.equity_curve.as_slice();
    let logs_match = accumulator.trade_log() == result.trade_log.as_slice();
    let metrics_match = paper_metrics == backtest_metrics;

    println!("paper-backtest-equity-curve-match:{curves_match}");
    println!("paper-backtest-trade-log-match:{logs_match}");
    println!("paper-backtest-parity:{metrics_match}");
    println!("-- backtest metrics --");
    render_metrics(&backtest_metrics);
    println!("-- paper metrics --");
    render_metrics(&paper_metrics);

    if curves_match && logs_match && metrics_match {
        Ok(())
    } else {
        Err("SYS-86 parity FAILED: paper and backtest metric families diverged".to_string())
    }
}

// --------------------------------------------------------------------------- //
// Fixture activity (shared by both subcommands)
// --------------------------------------------------------------------------- //

/// The fixture's close prices: a winning round trip (buy 10 @ 100, sell 10 @ 125) with a
/// real intra-hold drawdown (120 -> 90), so every metric is defined and non-trivial.
fn fixture_bars() -> Vec<BacktestBar> {
    [(1, 100), (2, 120), (3, 90), (4, 130), (5, 125)]
        .into_iter()
        .map(|(ts, close_minor)| BacktestBar {
            symbol: SYMBOL.to_string(),
            ts,
            close_minor,
            spread_minor: None,
        })
        .collect()
}

/// A fixed benchmark level series aligned with the fixture curve: the baseline at ts 0,
/// then one level per bar (so alpha/beta are defined). Identity only is operator-selectable.
fn fixture_benchmark_levels() -> Vec<BenchmarkPoint> {
    [(0, 400), (1, 400), (2, 420), (3, 380), (4, 430), (5, 425)]
        .into_iter()
        .map(|(ts, level_minor)| BenchmarkPoint { ts, level_minor })
        .collect()
}

/// Drive the paper accumulator over the fixture: buy 10 on bar 1, hold (marking each bar),
/// sell 10 on bar 5 — the fill-then-mark order a paper runtime uses over a live feed.
fn accumulate_fixture_paper_run() -> Result<PaperMetricsAccumulator, String> {
    let mut accumulator = PaperMetricsAccumulator::new(STARTING_CASH_MINOR)
        .map_err(|err| format!("accumulator init failed: {err}"))?;
    let bars = fixture_bars();
    let lot = 10;
    for (index, bar) in bars.iter().enumerate() {
        if index == 0 {
            apply_fixture_fill(&mut accumulator, bar.ts, lot, bar.close_minor)?;
        } else if index == bars.len() - 1 {
            apply_fixture_fill(&mut accumulator, bar.ts, -lot, bar.close_minor)?;
        }
        accumulator
            .mark(bar.ts, &[(SYMBOL.to_string(), bar.close_minor)])
            .map_err(|err| format!("mark failed: {err}"))?;
    }
    Ok(accumulator)
}

/// Apply one zero-cost fixture fill, computing the simulator's `cash_delta_minor`
/// (`-(notional) - cost`) so it passes the ledger's consistency guard.
fn apply_fixture_fill(
    accumulator: &mut PaperMetricsAccumulator,
    ts: u64,
    quantity: i64,
    price_minor: i64,
) -> Result<(), String> {
    let cash_delta = -(i128::from(quantity) * i128::from(price_minor));
    let fill = PaperFill {
        ts,
        symbol: SYMBOL.to_string(),
        quantity,
        price_minor,
        commission_minor: 0,
        slippage_minor: 0,
        spread_impact_minor: 0,
        cash_delta_minor: i64::try_from(cash_delta)
            .map_err(|_| "cash delta overflow".to_string())?,
    };
    accumulator
        .apply_fill(&fill)
        .map_err(|err| format!("fill failed: {err}"))
}

/// A fixture catalog serving the fixture bars within the requested window.
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

/// The same buy-and-hold-one-round-trip strategy the paper run uses: buy `lot` on the first
/// bar, sell to flat on the last.
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

fn run_fixture_backtest(bars: &[BacktestBar]) -> Result<BacktestResult, String> {
    let catalog = FixtureCatalog {
        bars: bars.to_vec(),
    };
    let request = BacktestRequest {
        strategy_id: StrategyId::new("bt-004-paper-cli"),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(0, 100),
        starting_cash_minor: STARTING_CASH_MINOR,
        // The frictionless cost config so the engine's fills mirror the zero-cost paper
        // fixture exactly; the cost-INCLUSIVE parity is proven in the srs_bt_004_metrics
        // integration test (`..._with_costs`).
        cost_config: CostConfig::zero(),
    };
    let mut strategy = RoundTrip {
        lot: 10,
        sell_ts: bars.last().map(|bar| bar.ts).unwrap_or(0),
    };
    BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .map_err(|err| format!("backtest failed: {err:?}"))
}

/// Render the eight SYS-16 metrics + the benchmark identity, an undefined metric as the
/// literal `undefined`.
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

fn fmt_opt(value: Option<f64>) -> String {
    match value {
        Some(v) => format!("{v:.12}"),
        None => "undefined".to_string(),
    }
}
