//! SRS-BT-007 operator grid-search / parameter-sweep CLI.
//!
//! The operator-facing workflow over the public [`sweep`] API: define a parameter space
//! with repeatable `--axis name=v1,v2,...` flags, select an objective function
//! (`--objective <metric> --direction <max|min>`), and get ranked backtest results —
//! the SRS-BT-007 acceptance criterion demonstrated end to end. There is no
//! Python<->Rust runtime bridge, so this is a small Rust binary in the simulation crate
//! (the same pattern as `bt009_store_cli`), and the market data, benchmark source, and
//! strategy are deliberate fixtures, exactly as the SRS-BT-007 verification step
//! specifies ("fixture market data, provider mocks").
//!
//! - The fixture strategy is a genuinely parameterized round trip
//!   ([`ParamRoundTrip`]: open `lot` shares on the first bar, close fully at
//!   `sell_ts`), so different points produce genuinely different Sharpe / drawdown /
//!   return values over the shared 5-bar fixture catalog — the ranking is real, not
//!   cosmetic. Its [`SweepStrategyFactory`] parses each point fail-closed: a missing
//!   parameter, an axis the strategy does not declare, or an unparseable / non-positive
//!   value aborts the sweep naming the point, never silently runs a default.
//! - With no `--axis` flags a demo space is used (`lot=5,10,20 × sell_ts=3,5`, 6
//!   points); with no `--objective` the sweep ranks by maximize-Sharpe (stated in the
//!   output). An EXPLICIT `--objective` requires an explicit `--direction`: whether a
//!   metric should be maximized or minimized is the operator's selection, and guessing
//!   it (e.g. assuming drawdown is minimized) could silently invert a ranking.
//! - `--format kv` emits flat, indexed proof lines (the same single-grammar discipline
//!   as `bt009_store_cli`): counts first, then contiguous `ranked.<i>.*` /
//!   `unranked.<i>.*` blocks, so a machine consumer fails closed on any drift.
//!   Undefined metrics render as `n/a` (mathematically undefined — never a fabricated
//!   0), and any string field carrying a control character fails closed before a
//!   forgeable line is emitted.
//!
//! Scope: the real Python-strategy factory (the deferred strategy host), the
//! REST/dashboard sweep surface (SRS-API-001 / SRS-UI), the real stored-data benchmark
//! resolver (SRS-BT-005 owner), and the SRS-BT-008 walk-forward consumer are deferred.

use std::env;
use std::process::ExitCode;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestError, BacktestRequest, BacktestStrategy, BarSource,
    DateRange,
};
use atp_simulation::backtest_store::StrategyParameters;
use atp_simulation::benchmark::{
    BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig, PerformanceMetrics};
use atp_simulation::sweep::{
    Direction, ObjectiveFunction, ObjectiveMetric, ParameterAxis, ParameterSpace, RankedPoint,
    SweepError, SweepEvaluation, SweepReport, SweepRequest, SweepRunner, SweepStrategyFactory,
    UnrankedPoint,
};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

const USAGE: &str = "\
bt007_sweep_cli — SRS-BT-007 operator grid-search / parameter-sweep workflow

USAGE:
    bt007_sweep_cli run [--axis <name=v1,v2,...>]...
                        [--objective <metric> --direction <max|min>]
                        [--format human|kv]
    bt007_sweep_cli help

A parameter space definition (repeatable --axis flags) produces ranked backtest results
by the selected objective function. Every axis combination is evaluated through the real
backtest engine + benchmark comparison over deterministic fixture market data.

FLAGS:
    --axis <name=v1,v2,...>   one space dimension: an axis name and its comma-separated
                              values (repeatable; each axis name once). Default space:
                              lot=5,10,20 and sell_ts=3,5 (6 points). The fixture
                              strategy declares exactly: lot (positive integer shares),
                              sell_ts (positive integer bar timestamp to close at).
    --objective <metric>      objective metric: sharpe_ratio, sortino_ratio, alpha,
                              beta, max_drawdown, annualized_return,
                              annualized_volatility, win_rate. Default: sharpe_ratio
                              maximized. An explicit --objective REQUIRES an explicit
                              --direction (the direction is a selection, not a guess).
    --direction <max|min>     whether the objective is maximized or minimized.
    --format <human|kv>       human (default) or flat indexed machine proof lines
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt007_sweep_cli: {err}");
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

// --------------------------------------------------------------------------- //
// Argument parsing
// --------------------------------------------------------------------------- //

#[derive(Default, Clone, Copy, PartialEq, Eq)]
enum OutputFormat {
    #[default]
    Human,
    Kv,
}

fn parse_format(raw: &str) -> Result<OutputFormat, String> {
    match raw {
        "human" => Ok(OutputFormat::Human),
        "kv" => Ok(OutputFormat::Kv),
        other => Err(format!("--format expects 'human' or 'kv', got '{other}'")),
    }
}

struct ParsedArgs {
    axes: Vec<ParameterAxis>,
    objective: Option<String>,
    direction: Option<String>,
    format: OutputFormat,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut axes: Vec<ParameterAxis> = Vec::new();
        let mut objective: Option<String> = None;
        let mut direction: Option<String> = None;
        let mut format = OutputFormat::default();

        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--axis" => {
                    let raw = take_value(&mut iter, flag)?;
                    axes.push(parse_axis(&raw)?);
                }
                "--objective" => objective = Some(take_value(&mut iter, flag)?),
                "--direction" => direction = Some(take_value(&mut iter, flag)?),
                "--format" => format = parse_format(&take_value(&mut iter, flag)?)?,
                other => return Err(format!("unknown flag '{other}'\n\n{USAGE}")),
            }
        }
        Ok(Self {
            axes,
            objective,
            direction,
            format,
        })
    }

    /// Resolve the selected objective. No `--objective` selects the stated default
    /// (maximize Sharpe); an explicit `--objective` REQUIRES an explicit
    /// `--direction`, and a `--direction` without an `--objective` is meaningless —
    /// both half-selections fail closed rather than let the CLI guess.
    fn to_objective(&self) -> Result<ObjectiveFunction, String> {
        match (&self.objective, &self.direction) {
            (None, None) => Ok(ObjectiveFunction::maximize_sharpe()),
            (Some(metric), Some(direction)) => Ok(ObjectiveFunction {
                metric: ObjectiveMetric::parse(metric).map_err(|err| err.to_string())?,
                direction: Direction::parse(direction).map_err(|err| err.to_string())?,
            }),
            (Some(_), None) => Err(
                "--objective requires --direction <max|min> (the direction is the \
                     operator's selection; the CLI never guesses it)"
                    .to_string(),
            ),
            (None, Some(_)) => Err("--direction requires --objective <metric>".to_string()),
        }
    }

    /// Resolve the parameter space: the operator's axes, else the demo space. All
    /// validation (empty/duplicate names, empty/duplicate values, cardinality cap)
    /// is the sweep core's, so the CLI and library enforce one rule set.
    fn to_space(&self) -> Result<ParameterSpace, String> {
        let axes = if self.axes.is_empty() {
            default_axes()?
        } else {
            self.axes.clone()
        };
        ParameterSpace::new(axes).map_err(|err| err.to_string())
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

/// Parse one `--axis name=v1,v2,...` occurrence. Control characters are rejected here
/// (a name/value with a newline could forge a kv proof line downstream); the axis's
/// own structural validation (empty name/values, duplicates) is [`ParameterAxis::new`].
fn parse_axis(raw: &str) -> Result<ParameterAxis, String> {
    let (name, values_raw) = raw
        .split_once('=')
        .ok_or_else(|| format!("--axis expects <name=v1,v2,...>, got '{raw}'"))?;
    if raw.chars().any(char::is_control) {
        return Err(format!("--axis '{name}' contains a control character"));
    }
    let values: Vec<String> = values_raw.split(',').map(str::to_string).collect();
    ParameterAxis::new(name, values).map_err(|err| err.to_string())
}

fn default_axes() -> Result<Vec<ParameterAxis>, String> {
    Ok(vec![
        ParameterAxis::new("lot", to_values(&["5", "10", "20"])).map_err(|err| err.to_string())?,
        ParameterAxis::new("sell_ts", to_values(&["3", "5"])).map_err(|err| err.to_string())?,
    ])
}

fn to_values(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| value.to_string()).collect()
}

// --------------------------------------------------------------------------- //
// The sweep run
// --------------------------------------------------------------------------- //

fn cmd_run(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let objective = parsed.to_objective()?;
    let space = parsed.to_space()?;

    let request = SweepRequest {
        base: BacktestRequest {
            strategy_id: StrategyId::new("sweep-fixture"),
            symbol: SYMBOL.to_string(),
            data_source: BacktestDataSource::SystemData,
            range: DateRange::new(0, 100),
            starting_cash_minor: STARTING_CASH_MINOR,
            cost_config: CostConfig::default(),
        },
        space,
        objective,
    };
    let source = FixtureBenchmark {
        symbol: "SPY".to_string(),
        baseline: 400,
        step: 5,
    };
    let selection = BenchmarkSelection::unselected();
    let metrics_config = MetricsConfig::default();

    let report = SweepRunner::new()
        .run(
            &request,
            &ParamRoundTripFactory,
            &fixture_catalog(),
            &SweepEvaluation {
                selection: &selection,
                source: &source,
                metrics_config: &metrics_config,
            },
        )
        .map_err(|err| err.to_string())?;

    match parsed.format {
        OutputFormat::Kv => print_report_kv(&report),
        OutputFormat::Human => {
            print_report_human(&request, &report);
            Ok(())
        }
    }
}

// --------------------------------------------------------------------------- //
// Output
// --------------------------------------------------------------------------- //

fn format_params(parameters: &StrategyParameters) -> String {
    parameters
        .entries()
        .iter()
        .map(|(key, value)| format!("{key}={value}"))
        .collect::<Vec<_>>()
        .join(", ")
}

fn fmt_opt(value: Option<f64>) -> String {
    value.map_or_else(|| "n/a".to_string(), |v| v.to_string())
}

fn print_report_human(request: &SweepRequest, report: &SweepReport) {
    println!("parameter space:");
    for axis in request.space.axes() {
        println!("    axis {} = [{}]", axis.name(), axis.values().join(", "));
    }
    println!("points: {}", report.total_points);
    println!(
        "objective: {} {}",
        match report.objective.direction {
            Direction::Maximize => "maximize",
            Direction::Minimize => "minimize",
        },
        report.objective.metric.as_str(),
    );
    println!(
        "benchmark: {} (default={})",
        report
            .ranked
            .first()
            .map(|point| point.comparison.benchmark_symbol.as_str())
            .or_else(|| {
                report
                    .unranked
                    .first()
                    .map(|point| point.comparison.benchmark_symbol.as_str())
            })
            .unwrap_or("n/a"),
        report
            .ranked
            .first()
            .map(|point| point.comparison.is_default_benchmark)
            .or_else(|| {
                report
                    .unranked
                    .first()
                    .map(|point| point.comparison.is_default_benchmark)
            })
            .map_or_else(|| "n/a".to_string(), |default| default.to_string()),
    );
    println!("ranked ({} point(s), best first):", report.ranked.len());
    for point in &report.ranked {
        println!(
            "    rank={} params=[{}] objective={} sharpe={} sortino={} max_drawdown={} \
             ann_return={} win_rate={} final_equity_minor={} trades={}",
            point.rank,
            format_params(&point.parameters),
            point.objective_value,
            fmt_opt(point.metrics.sharpe_ratio),
            fmt_opt(point.metrics.sortino_ratio),
            fmt_opt(point.metrics.max_drawdown),
            fmt_opt(point.metrics.annualized_return),
            fmt_opt(point.metrics.win_rate),
            point.final_equity_minor,
            point.trade_count,
        );
    }
    println!("unranked ({} point(s)):", report.unranked.len());
    for point in &report.unranked {
        println!(
            "    params=[{}] reason={} sharpe={} win_rate={}",
            format_params(&point.parameters),
            point.reason.as_str(),
            fmt_opt(point.metrics.sharpe_ratio),
            fmt_opt(point.metrics.win_rate),
        );
    }
}

/// A string field is safe to emit only if it is exactly one line: in the flat
/// `key:value` machine format a control character (a newline above all) would forge or
/// corrupt a proof line, so the emitter fails CLOSED rather than emit a forgeable line
/// (the same discipline as `bt009_store_cli`).
fn kv_field<'a>(label: &str, value: &'a str) -> Result<&'a str, String> {
    if value.chars().any(char::is_control) {
        return Err(format!(
            "cannot emit kv machine format: {label} contains a control character (forgeable output)"
        ));
    }
    Ok(value)
}

fn print_metrics_kv(prefix: &str, metrics: &PerformanceMetrics) -> Result<(), String> {
    println!(
        "{prefix}.benchmark_symbol:{}",
        kv_field("benchmark_symbol", &metrics.benchmark_symbol)?
    );
    println!("{prefix}.metric.sharpe:{}", fmt_opt(metrics.sharpe_ratio));
    println!("{prefix}.metric.sortino:{}", fmt_opt(metrics.sortino_ratio));
    println!("{prefix}.metric.alpha:{}", fmt_opt(metrics.alpha));
    println!("{prefix}.metric.beta:{}", fmt_opt(metrics.beta));
    println!(
        "{prefix}.metric.max_drawdown:{}",
        fmt_opt(metrics.max_drawdown)
    );
    println!(
        "{prefix}.metric.annualized_return:{}",
        fmt_opt(metrics.annualized_return)
    );
    println!(
        "{prefix}.metric.annualized_volatility:{}",
        fmt_opt(metrics.annualized_volatility)
    );
    println!("{prefix}.metric.win_rate:{}", fmt_opt(metrics.win_rate));
    Ok(())
}

fn print_params_kv(prefix: &str, parameters: &StrategyParameters) -> Result<(), String> {
    let entries = parameters.entries();
    println!("{prefix}.param_count:{}", entries.len());
    for (index, (key, value)) in entries.iter().enumerate() {
        println!("{prefix}.param.{index}.key:{}", kv_field("param key", key)?);
        println!(
            "{prefix}.param.{index}.value:{}",
            kv_field("param value", value)?
        );
    }
    Ok(())
}

/// Emit the report as flat, indexed proof lines: counts first, then contiguous
/// `ranked.<i>.*` and `unranked.<i>.*` blocks, so a machine consumer can fail closed
/// on any count/index drift or forged line. Undefined metrics render as `n/a`.
fn print_report_kv(report: &SweepReport) -> Result<(), String> {
    println!("objective.metric:{}", report.objective.metric.as_str());
    println!(
        "objective.direction:{}",
        report.objective.direction.as_str()
    );
    println!("point_count:{}", report.total_points);
    println!("ranked_count:{}", report.ranked.len());
    println!("unranked_count:{}", report.unranked.len());
    for (index, point) in report.ranked.iter().enumerate() {
        let RankedPoint {
            rank,
            parameters,
            objective_value,
            metrics,
            comparison,
            final_equity_minor,
            trade_count,
        } = point;
        let p = format!("ranked.{index}");
        println!("{p}.rank:{rank}");
        println!("{p}.objective_value:{objective_value}");
        print_params_kv(&p, parameters)?;
        print_metrics_kv(&p, metrics)?;
        println!(
            "{p}.comparison.strategy_total_return:{}",
            fmt_opt(comparison.strategy_total_return)
        );
        println!(
            "{p}.comparison.benchmark_total_return:{}",
            fmt_opt(comparison.benchmark_total_return)
        );
        println!(
            "{p}.comparison.excess_return:{}",
            fmt_opt(comparison.excess_return)
        );
        println!("{p}.final_equity_minor:{final_equity_minor}");
        println!("{p}.trade_count:{trade_count}");
    }
    for (index, point) in report.unranked.iter().enumerate() {
        let UnrankedPoint {
            parameters,
            metrics,
            comparison: _,
            reason,
        } = point;
        let p = format!("unranked.{index}");
        println!("{p}.reason:{}", reason.as_str());
        print_params_kv(&p, parameters)?;
        print_metrics_kv(&p, metrics)?;
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Deterministic fixture producer (mirrors bt009_store_cli's fixture chain)
// --------------------------------------------------------------------------- //

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

/// The genuinely parameterized fixture strategy: opens `lot` shares on the first bar,
/// then fully closes at `sell_ts` — so `lot` scales exposure and `sell_ts` selects the
/// exit price, and different sweep points produce genuinely different metrics.
struct ParamRoundTrip {
    lot: i64,
    sell_ts: u64,
}

impl BacktestStrategy for ParamRoundTrip {
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

/// The fixture realization of the [`SweepStrategyFactory`] seam: parses exactly the two
/// parameters [`ParamRoundTrip`] declares, fail-closed — a missing parameter, an axis
/// the strategy does not declare, or an unparseable / non-positive value aborts the
/// sweep naming the point (never a silent default run misattributed to the point).
struct ParamRoundTripFactory;

impl SweepStrategyFactory for ParamRoundTripFactory {
    type Strategy = ParamRoundTrip;

    fn build(&self, params: &StrategyParameters) -> Result<ParamRoundTrip, SweepError> {
        let mut lot: Option<i64> = None;
        let mut sell_ts: Option<u64> = None;
        for (key, value) in params.entries() {
            match key.as_str() {
                "lot" => {
                    let parsed =
                        value
                            .parse::<i64>()
                            .map_err(|_| SweepError::InvalidParameterValue {
                                name: key.clone(),
                                value: value.clone(),
                                reason: "expected an integer share count".to_string(),
                            })?;
                    if parsed <= 0 {
                        return Err(SweepError::InvalidParameterValue {
                            name: key.clone(),
                            value: value.clone(),
                            reason: "lot must be positive".to_string(),
                        });
                    }
                    lot = Some(parsed);
                }
                "sell_ts" => {
                    let parsed =
                        value
                            .parse::<u64>()
                            .map_err(|_| SweepError::InvalidParameterValue {
                                name: key.clone(),
                                value: value.clone(),
                                reason: "expected a non-negative integer bar timestamp".to_string(),
                            })?;
                    if parsed == 0 {
                        return Err(SweepError::InvalidParameterValue {
                            name: key.clone(),
                            value: value.clone(),
                            reason: "sell_ts must be positive".to_string(),
                        });
                    }
                    sell_ts = Some(parsed);
                }
                other => {
                    return Err(SweepError::UnknownParameter {
                        name: other.to_string(),
                    })
                }
            }
        }
        Ok(ParamRoundTrip {
            lot: lot.ok_or(SweepError::MissingParameter {
                name: "lot".to_string(),
            })?,
            sell_ts: sell_ts.ok_or(SweepError::MissingParameter {
                name: "sell_ts".to_string(),
            })?,
        })
    }
}

/// A well-formed aligned benchmark source (the stand-in for the deferred stored-data
/// resolver — the SRS-BT-005 owner).
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
