//! SRS-BT-008 operator walk-forward-analysis CLI.
//!
//! The operator-facing workflow over the public [`walk_forward`] API: define a fold
//! schedule (either explicit `--fold IS_START:IS_END:OOS_START:OOS_END` flags or a rolling
//! `--rolling START:IS_LEN:OOS_LEN:STEP:COUNT` generator), a parameter space (repeatable
//! `--axis name=v1,v2,...`), and an objective (`--objective <metric> --direction
//! <max|min>`), and get, per fold, the in-sample-optimized parameter set and both windows'
//! metrics — the SRS-BT-008 acceptance criterion demonstrated end to end. There is no
//! Python<->Rust runtime bridge, so this is a small Rust binary in the simulation crate
//! (the same pattern as `bt007_sweep_cli`), and the market data, benchmark source, and
//! strategy are deliberate fixtures, exactly as the SRS-BT-008 verification step specifies
//! ("fixture market data, provider mocks").
//!
//! - The fixture strategy is the SAME genuinely parameterized round trip BT-007 uses
//!   ([`ParamRoundTrip`]: open `lot` shares at the window's first bar, close fully at
//!   `sell_ts` if that timestamp falls in the window, else hold to the window's end), and
//!   the SAME fail-closed [`SweepStrategyFactory`] — so walk-forward reuses BT-007's whole
//!   optimizer boundary, not a parallel one.
//! - The **no-lookahead invariant** is enforced by the schedule: an explicit `--fold`
//!   whose out-of-sample range is not strictly after its in-sample range, or a `--rolling`
//!   config that would produce overlapping out-of-sample windows, fails closed. With no
//!   schedule flags a demo rolling schedule is used; with no `--objective` the analysis
//!   optimizes/evaluates by maximize-Sharpe (stated in the output). An EXPLICIT
//!   `--objective` requires an explicit `--direction`.
//! - `--format kv` emits flat, indexed proof lines (the same single-grammar discipline as
//!   `bt007_sweep_cli`): counts first, then contiguous `fold.<i>.*` blocks, so a machine
//!   consumer fails closed on any drift. An undefined out-of-sample objective renders as
//!   `n/a` (mathematically undefined — never a fabricated 0), and any string field carrying
//!   a control character fails closed before a forgeable line is emitted.
//!
//! Scope: the real Python-strategy factory (the deferred strategy host), the REST/dashboard
//! surface (SRS-API-001 / SRS-UI), the real stored-data benchmark resolver (SRS-BT-005
//! owner), and fold persistence (SRS-BT-009) are deferred, so SRS-BT-008 stays
//! `passes:false`.

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
    Direction, ObjectiveFunction, ObjectiveMetric, ParameterAxis, ParameterSpace, SweepError,
    SweepEvaluation, SweepStrategyFactory,
};
use atp_simulation::walk_forward::{
    WalkForwardFold, WalkForwardReport, WalkForwardRequest, WalkForwardRunner, WalkForwardSchedule,
    WalkForwardWindow,
};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

const USAGE: &str = "\
bt008_walk_forward_cli — SRS-BT-008 operator walk-forward-analysis workflow

USAGE:
    bt008_walk_forward_cli run [--fold <is_start:is_end:oos_start:oos_end>]...
                              [--rolling <start:is_len:oos_len:step:count>]
                              [--axis <name=v1,v2,...>]...
                              [--objective <metric> --direction <max|min>]
                              [--format human|kv]
    bt008_walk_forward_cli help

For each fold, the in-sample window is optimized (a full grid search over the parameter
space) and the winning parameter set is evaluated on the immediately-following out-of-sample
window; the output preserves the parameter set and both windows' metrics per fold. Every
window is evaluated through the real backtest engine + benchmark comparison over
deterministic fixture market data.

FLAGS:
    --fold <is:...:oos>       one explicit fold: in-sample [is_start,is_end] then
                              out-of-sample [oos_start,oos_end] (four colon-separated
                              timestamps; repeatable). The out-of-sample range must be
                              strictly after the in-sample range (no lookahead).
    --rolling <s:i:o:t:c>     a rolling schedule: start, in-sample length, out-of-sample
                              length, step, fold count (colon-separated). Mutually exclusive
                              with --fold. Default (no schedule flag): 1:4:2:2:3.
    --axis <name=v1,v2,...>   one parameter-space dimension (repeatable; each axis name
                              once). Default space: lot=5,10,20 and sell_ts=3,5 (6 points).
                              The fixture strategy declares exactly: lot (positive integer
                              shares), sell_ts (positive integer bar timestamp to close at).
    --objective <metric>      objective metric: sharpe_ratio, sortino_ratio, alpha, beta,
                              max_drawdown, annualized_return, annualized_volatility,
                              win_rate. Default: sharpe_ratio maximized. An explicit
                              --objective REQUIRES an explicit --direction.
    --direction <max|min>     whether the objective is maximized or minimized.
    --format <human|kv>       human (default) or flat indexed machine proof lines
";

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("bt008_walk_forward_cli: {err}");
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
    folds: Vec<WalkForwardWindow>,
    rolling: Option<String>,
    axes: Vec<ParameterAxis>,
    objective: Option<String>,
    direction: Option<String>,
    format: OutputFormat,
}

impl ParsedArgs {
    fn parse(rest: &[String]) -> Result<Self, String> {
        let mut folds: Vec<WalkForwardWindow> = Vec::new();
        let mut rolling: Option<String> = None;
        let mut axes: Vec<ParameterAxis> = Vec::new();
        let mut objective: Option<String> = None;
        let mut direction: Option<String> = None;
        let mut format = OutputFormat::default();

        let mut iter = rest.iter();
        while let Some(flag) = iter.next() {
            match flag.as_str() {
                "--fold" => {
                    let raw = take_value(&mut iter, flag)?;
                    folds.push(parse_fold(&raw)?);
                }
                "--rolling" => {
                    if rolling.is_some() {
                        return Err("--rolling may be given at most once".to_string());
                    }
                    rolling = Some(take_value(&mut iter, flag)?);
                }
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
            folds,
            rolling,
            axes,
            objective,
            direction,
            format,
        })
    }

    /// Resolve the selected objective. No `--objective` selects the stated default
    /// (maximize Sharpe); an explicit `--objective` REQUIRES an explicit `--direction`, and
    /// a `--direction` without an `--objective` is meaningless — both half-selections fail
    /// closed rather than let the CLI guess.
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
    /// validation is the sweep core's, so the CLI and library enforce one rule set.
    fn to_space(&self) -> Result<ParameterSpace, String> {
        let axes = if self.axes.is_empty() {
            default_axes()?
        } else {
            self.axes.clone()
        };
        ParameterSpace::new(axes).map_err(|err| err.to_string())
    }

    /// Resolve the fold schedule: explicit `--fold`s, a `--rolling` generator, or the demo
    /// schedule. `--fold` and `--rolling` are mutually exclusive (one schedule per run). All
    /// window validation (no-lookahead, forward-tiling) is the walk_forward core's.
    fn to_schedule(&self) -> Result<WalkForwardSchedule, String> {
        match (self.folds.is_empty(), &self.rolling) {
            (false, Some(_)) => {
                Err("--fold and --rolling are mutually exclusive (choose one schedule)".to_string())
            }
            (false, None) => {
                WalkForwardSchedule::new(self.folds.clone()).map_err(|err| err.to_string())
            }
            (true, Some(raw)) => parse_rolling(raw),
            // Default demo schedule: 3 rolling folds over the fixture catalog.
            (true, None) => {
                WalkForwardSchedule::rolling(1, 4, 2, 2, 3).map_err(|err| err.to_string())
            }
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

/// Parse one `--fold is_start:is_end:oos_start:oos_end` occurrence into a validated window.
/// The four fields are timestamps (numeric), so there is no control-character forging
/// surface here; the window's own no-lookahead validation is [`WalkForwardWindow::new`].
fn parse_fold(raw: &str) -> Result<WalkForwardWindow, String> {
    let parts: Vec<&str> = raw.split(':').collect();
    if parts.len() != 4 {
        return Err(format!(
            "--fold expects <is_start:is_end:oos_start:oos_end> (four fields), got '{raw}'"
        ));
    }
    let value = |index: usize, label: &str| -> Result<u64, String> {
        parts[index]
            .parse::<u64>()
            .map_err(|_| format!("--fold {label} '{}' is not a timestamp", parts[index]))
    };
    let in_sample = DateRange::new(value(0, "is_start")?, value(1, "is_end")?);
    let out_of_sample = DateRange::new(value(2, "oos_start")?, value(3, "oos_end")?);
    WalkForwardWindow::new(in_sample, out_of_sample).map_err(|err| err.to_string())
}

/// Parse a `--rolling start:is_len:oos_len:step:count` occurrence into a schedule. All
/// zero-length / zero-step / zero-fold and forward-tiling validation is the walk_forward
/// core's [`WalkForwardSchedule::rolling`].
fn parse_rolling(raw: &str) -> Result<WalkForwardSchedule, String> {
    let parts: Vec<&str> = raw.split(':').collect();
    if parts.len() != 5 {
        return Err(format!(
            "--rolling expects <start:is_len:oos_len:step:count> (five fields), got '{raw}'"
        ));
    }
    let u64_field = |index: usize, label: &str| -> Result<u64, String> {
        parts[index].parse::<u64>().map_err(|_| {
            format!(
                "--rolling {label} '{}' is not a non-negative integer",
                parts[index]
            )
        })
    };
    let count = parts[4].parse::<usize>().map_err(|_| {
        format!(
            "--rolling count '{}' is not a non-negative integer",
            parts[4]
        )
    })?;
    WalkForwardSchedule::rolling(
        u64_field(0, "start")?,
        u64_field(1, "is_len")?,
        u64_field(2, "oos_len")?,
        u64_field(3, "step")?,
        count,
    )
    .map_err(|err| err.to_string())
}

/// Parse one `--axis name=v1,v2,...` occurrence. Control characters are rejected here (a
/// name/value with a newline could forge a kv proof line downstream); the axis's own
/// structural validation (empty name/values, duplicates) is [`ParameterAxis::new`].
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
// The walk-forward run
// --------------------------------------------------------------------------- //

fn cmd_run(rest: &[String]) -> Result<(), String> {
    let parsed = ParsedArgs::parse(rest)?;
    let objective = parsed.to_objective()?;
    let space = parsed.to_space()?;
    let schedule = parsed.to_schedule()?;

    let request = WalkForwardRequest {
        base: BacktestRequest {
            strategy_id: StrategyId::new("walk-forward-fixture"),
            symbol: SYMBOL.to_string(),
            data_source: BacktestDataSource::SystemData,
            // The per-fold windows override this range; a wide base keeps it inert.
            range: DateRange::new(0, u64::MAX),
            starting_cash_minor: STARTING_CASH_MINOR,
            cost_config: CostConfig::default(),
        },
        space,
        objective,
        schedule,
    };
    let source = FixtureBenchmark {
        symbol: "SPY".to_string(),
        baseline: 400,
        step: 5,
    };
    let selection = BenchmarkSelection::unselected();
    let metrics_config = MetricsConfig::default();

    let report = WalkForwardRunner::new()
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

/// Render the FULL SYS-16 metric family (all eight metrics) plus the benchmark identity for
/// one window — so the default human report preserves every metric per window (SRS-BT-008
/// "outputs preserve the parameter set and metrics per window"), not a subset. An undefined
/// metric renders as `n/a`, never a fabricated value.
fn format_metrics_human(metrics: &PerformanceMetrics) -> String {
    format!(
        "benchmark={} sharpe={} sortino={} alpha={} beta={} max_drawdown={} ann_return={} \
         ann_volatility={} win_rate={}",
        metrics.benchmark_symbol,
        fmt_opt(metrics.sharpe_ratio),
        fmt_opt(metrics.sortino_ratio),
        fmt_opt(metrics.alpha),
        fmt_opt(metrics.beta),
        fmt_opt(metrics.max_drawdown),
        fmt_opt(metrics.annualized_return),
        fmt_opt(metrics.annualized_volatility),
        fmt_opt(metrics.win_rate),
    )
}

fn print_report_human(request: &WalkForwardRequest, report: &WalkForwardReport) {
    println!("parameter space:");
    for axis in request.space.axes() {
        println!("    axis {} = [{}]", axis.name(), axis.values().join(", "));
    }
    println!(
        "objective: {} {}",
        match report.objective.direction {
            Direction::Maximize => "maximize",
            Direction::Minimize => "minimize",
        },
        report.objective.metric.as_str(),
    );
    println!("folds: {}", report.total_folds);
    for (index, fold) in report.folds.iter().enumerate() {
        println!(
            "fold {index}: in_sample=[{},{}] out_of_sample=[{},{}]",
            fold.window.in_sample.start,
            fold.window.in_sample.end,
            fold.window.out_of_sample.start,
            fold.window.out_of_sample.end,
        );
        println!(
            "    selected params=[{}]",
            format_params(&fold.selected_parameters)
        );
        println!(
            "    in_sample: objective={} {}",
            fold.in_sample_objective,
            format_metrics_human(&fold.in_sample_metrics),
        );
        println!(
            "    out_of_sample: objective={} {}",
            fmt_opt(fold.out_of_sample_objective),
            format_metrics_human(&fold.out_of_sample_metrics),
        );
    }
}

/// A string field is safe to emit only if it is exactly one line: in the flat `key:value`
/// machine format a control character (a newline above all) would forge or corrupt a proof
/// line, so the emitter fails CLOSED rather than emit a forgeable line (the same discipline
/// as `bt007_sweep_cli`).
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

/// Emit the report as flat, indexed proof lines: counts first, then contiguous `fold.<i>.*`
/// blocks, so a machine consumer can fail closed on any count/index drift or forged line.
/// An undefined out-of-sample objective renders as `n/a`.
fn print_report_kv(report: &WalkForwardReport) -> Result<(), String> {
    println!("objective.metric:{}", report.objective.metric.as_str());
    println!(
        "objective.direction:{}",
        report.objective.direction.as_str()
    );
    println!("fold_count:{}", report.total_folds);
    for (index, fold) in report.folds.iter().enumerate() {
        let WalkForwardFold {
            window,
            selected_parameters,
            in_sample_objective,
            in_sample_metrics,
            in_sample_comparison,
            out_of_sample_objective,
            out_of_sample_metrics,
            out_of_sample_comparison,
        } = fold;
        let p = format!("fold.{index}");
        println!("{p}.in_sample.start:{}", window.in_sample.start);
        println!("{p}.in_sample.end:{}", window.in_sample.end);
        println!("{p}.out_of_sample.start:{}", window.out_of_sample.start);
        println!("{p}.out_of_sample.end:{}", window.out_of_sample.end);
        print_params_kv(&p, selected_parameters)?;
        println!("{p}.in_sample.objective:{in_sample_objective}");
        print_metrics_kv(&format!("{p}.in_sample"), in_sample_metrics)?;
        println!(
            "{p}.in_sample.comparison.excess_return:{}",
            fmt_opt(in_sample_comparison.excess_return)
        );
        println!(
            "{p}.out_of_sample.objective:{}",
            fmt_opt(*out_of_sample_objective)
        );
        print_metrics_kv(&format!("{p}.out_of_sample"), out_of_sample_metrics)?;
        println!(
            "{p}.out_of_sample.comparison.excess_return:{}",
            fmt_opt(out_of_sample_comparison.excess_return)
        );
    }
    Ok(())
}

// --------------------------------------------------------------------------- //
// Deterministic fixture producer (mirrors bt007_sweep_cli's fixture chain)
// --------------------------------------------------------------------------- //

fn fixture_catalog() -> FixtureCatalog {
    FixtureCatalog {
        bars: vec![
            bar(1, 100),
            bar(2, 120),
            bar(3, 90),
            bar(4, 130),
            bar(5, 125),
            bar(6, 140),
            bar(7, 110),
            bar(8, 150),
            bar(9, 135),
            bar(10, 160),
            bar(11, 145),
            bar(12, 170),
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

/// The genuinely parameterized fixture strategy (identical to BT-007): opens `lot` shares
/// at the window's first bar, then fully closes at `sell_ts` if that timestamp falls within
/// the window (else holds to the window's end) — so `lot` scales exposure and `sell_ts`
/// selects the exit, and different points produce genuinely different metrics per window.
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

/// The fixture realization of the [`SweepStrategyFactory`] seam (identical to BT-007):
/// parses exactly the two parameters [`ParamRoundTrip`] declares, fail-closed — a missing
/// parameter, an axis the strategy does not declare, or an unparseable / non-positive value
/// aborts the sweep naming the point (never a silent default run misattributed to the point).
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
