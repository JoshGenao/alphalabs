//! SRS-BT-008 acceptance: "In-sample windows are optimized, out-of-sample windows are
//! evaluated, and outputs preserve the parameter set and metrics per window" (SyRS
//! SYS-20; StRS SN-1.17).
//!
//! Library-level verification over the public [`walk_forward`] API with fixture bars, a
//! fixture benchmark source, and the SAME genuinely parameterized fixture strategy BT-007
//! uses. Each fold's result is proven against TWO independent oracles:
//!
//!   1. the in-sample optimization is checked against an INDEPENDENT sweep run over the
//!      in-sample window (the rank-1 point), under both SYS-19 named objectives; and
//!   2. the out-of-sample evaluation is checked against a raw [`BacktestEngine`] + `compare`
//!      run of the winning parameters over the out-of-sample window — a path that does NOT
//!      go through the walk-forward runner's singleton sweep, so it is a genuine oracle.
//!
//! The safety core — the walk-forward NO-LOOKAHEAD invariant — is pinned directly: every
//! fold's out-of-sample window lies strictly after its in-sample window, and a lookahead
//! schedule fails closed. The remaining fail-closed decisions (an in-sample window with no
//! rankable optimum, a per-fold sweep failure naming the window, degenerate schedules) are
//! each pinned, and repeat runs are byte-identical.

// WalkForwardError wraps a SweepError plus a window, pushing it over clippy's
// `result_large_err` threshold. The error path is cold and the test helpers return it by
// value for readability, matching the crate-level convention in atp-types / atp-execution.
#![allow(clippy::result_large_err)]

use std::cell::Cell;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::backtest_store::StrategyParameters;
use atp_simulation::benchmark::{
    compare, BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig, PerformanceMetrics};
use atp_simulation::sweep::{
    Direction, ObjectiveFunction, ObjectiveMetric, ParameterAxis, ParameterSpace, SweepError,
    SweepEvaluation, SweepReport, SweepRequest, SweepRunner, SweepStrategyFactory,
};
use atp_simulation::walk_forward::{
    WalkForwardError, WalkForwardReport, WalkForwardRequest, WalkForwardRunner,
    WalkForwardSchedule, WalkForwardWindow, MAX_WALK_FORWARD_FOLDS,
};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

// --------------------------------------------------------------------------- //
// Fixtures (the BT-007 fixture chain, extended to 12 bars so folds march forward)
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

/// The parameterized fixture strategy (BT-007's). `lot = 0` never trades (the
/// undefined-objective case: zero trades → `win_rate = None`); `fail_at` (when set) makes
/// `on_bar` error at that bar (the engine-failure case).
struct TestStrategy {
    lot: i64,
    sell_ts: u64,
    fail_at: Option<u64>,
}

impl BacktestStrategy for TestStrategy {
    fn on_bar(&mut self, bar: &BacktestBar, position: i64) -> Result<i64, BacktestError> {
        if Some(bar.ts) == self.fail_at {
            return Err(BacktestError::StrategyFailed {
                ts: bar.ts,
                reason: "fixture-injected strategy failure".to_string(),
            });
        }
        if self.lot == 0 {
            return Ok(0);
        }
        if bar.ts == self.sell_ts {
            return Ok(-position);
        }
        if position == 0 {
            return Ok(self.lot);
        }
        Ok(0)
    }
}

/// The test factory (BT-007's): parses `lot` / `sell_ts` (and the optional `fail_at`
/// injector), failing closed on anything else. Counts builds so a fail-closed test can
/// prove NOTHING ran.
struct TestFactory {
    builds: Cell<usize>,
}

impl TestFactory {
    fn new() -> Self {
        Self {
            builds: Cell::new(0),
        }
    }
}

impl SweepStrategyFactory for TestFactory {
    type Strategy = TestStrategy;

    fn build(&self, params: &StrategyParameters) -> Result<TestStrategy, SweepError> {
        self.builds.set(self.builds.get() + 1);
        let mut lot: Option<i64> = None;
        let mut sell_ts: Option<u64> = None;
        let mut fail_at: Option<u64> = None;
        for (key, value) in params.entries() {
            match key.as_str() {
                "lot" => {
                    lot = Some(value.parse::<i64>().map_err(|_| {
                        SweepError::InvalidParameterValue {
                            name: key.clone(),
                            value: value.clone(),
                            reason: "expected an integer share count".to_string(),
                        }
                    })?)
                }
                "sell_ts" => {
                    sell_ts = Some(value.parse::<u64>().map_err(|_| {
                        SweepError::InvalidParameterValue {
                            name: key.clone(),
                            value: value.clone(),
                            reason: "expected an integer bar timestamp".to_string(),
                        }
                    })?)
                }
                "fail_at" => {
                    fail_at = Some(value.parse::<u64>().map_err(|_| {
                        SweepError::InvalidParameterValue {
                            name: key.clone(),
                            value: value.clone(),
                            reason: "expected an integer bar timestamp".to_string(),
                        }
                    })?)
                }
                other => {
                    return Err(SweepError::UnknownParameter {
                        name: other.to_string(),
                    })
                }
            }
        }
        Ok(TestStrategy {
            lot: lot.ok_or(SweepError::MissingParameter {
                name: "lot".to_string(),
            })?,
            sell_ts: sell_ts.ok_or(SweepError::MissingParameter {
                name: "sell_ts".to_string(),
            })?,
            fail_at,
        })
    }
}

fn base_request() -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("walk-forward-test"),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        // Overridden per fold by the runner; a wide base keeps it inert.
        range: DateRange::new(0, u64::MAX),
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config: CostConfig::default(),
    }
}

fn fixture_benchmark() -> FixtureBenchmark {
    FixtureBenchmark {
        symbol: "SPY".to_string(),
        baseline: 400,
        step: 5,
    }
}

fn axis(name: &str, values: &[&str]) -> ParameterAxis {
    ParameterAxis::new(name, values.iter().map(|value| value.to_string()).collect())
        .expect("valid fixture axis")
}

fn space(axes: Vec<ParameterAxis>) -> ParameterSpace {
    ParameterSpace::new(axes).expect("valid fixture space")
}

fn default_space() -> ParameterSpace {
    space(vec![
        axis("lot", &["5", "10", "20"]),
        axis("sell_ts", &["3", "5"]),
    ])
}

/// The default demo schedule: 3 rolling folds tiling the fixture catalog forward —
/// in-sample [1,4] oos [5,6]; in-sample [3,6] oos [7,8]; in-sample [5,8] oos [9,10].
fn default_schedule() -> WalkForwardSchedule {
    WalkForwardSchedule::rolling(1, 4, 2, 2, 3).expect("valid rolling schedule")
}

/// Run the full walk-forward through the public runner.
fn run_walk_forward(
    space: ParameterSpace,
    objective: ObjectiveFunction,
    schedule: WalkForwardSchedule,
    factory: &TestFactory,
    runner: &WalkForwardRunner,
) -> Result<WalkForwardReport, WalkForwardError> {
    let request = WalkForwardRequest {
        base: base_request(),
        space,
        objective,
        schedule,
    };
    let source = fixture_benchmark();
    let selection = BenchmarkSelection::unselected();
    let metrics_config = MetricsConfig::default();
    runner.run(
        &request,
        factory,
        &fixture_catalog(),
        &SweepEvaluation {
            selection: &selection,
            source: &source,
            metrics_config: &metrics_config,
        },
    )
}

/// Independent oracle for one window's in-sample optimization: run a standalone sweep over
/// exactly that window and return its report (its rank-1 point is the expected optimum).
fn independent_sweep(
    space: ParameterSpace,
    objective: ObjectiveFunction,
    window: DateRange,
) -> SweepReport {
    let mut base = base_request();
    base.range = window;
    let request = SweepRequest {
        base,
        space,
        objective,
    };
    let source = fixture_benchmark();
    let selection = BenchmarkSelection::unselected();
    let metrics_config = MetricsConfig::default();
    SweepRunner::new()
        .run(
            &request,
            &TestFactory::new(),
            &fixture_catalog(),
            &SweepEvaluation {
                selection: &selection,
                source: &source,
                metrics_config: &metrics_config,
            },
        )
        .expect("independent sweep runs")
}

/// Independent oracle for the out-of-sample evaluation: a RAW BacktestEngine + compare run
/// of `params` over `window` — NOT through the walk-forward runner's singleton sweep, so it
/// is a genuine cross-check of the out-of-sample number.
fn raw_evaluate(params: &StrategyParameters, window: DateRange) -> PerformanceMetrics {
    let mut base = base_request();
    base.range = window;
    let mut strategy = TestFactory::new()
        .build(params)
        .expect("winner rebuilds from its own parameters");
    let result = BacktestEngine::new()
        .run(&base, &mut strategy, &fixture_catalog())
        .expect("out-of-sample backtest runs");
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &fixture_benchmark(),
        &MetricsConfig::default(),
    )
    .expect("out-of-sample comparison computes");
    report.metrics
}

// --------------------------------------------------------------------------- //
// In-sample windows are OPTIMIZED
// --------------------------------------------------------------------------- //

/// The AC's first obligation under the first SYS-19 objective: each fold's selected
/// parameter set and in-sample metrics equal the rank-1 point of an INDEPENDENT sweep run
/// over that fold's in-sample window (maximize Sharpe).
#[test]
fn srs_bt_008_in_sample_optimized_matches_independent_sweep_maximize_sharpe() {
    let report = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");

    assert_eq!(report.folds.len(), 3);
    for fold in &report.folds {
        let expected = independent_sweep(
            default_space(),
            ObjectiveFunction::maximize_sharpe(),
            fold.window.in_sample,
        );
        let best = &expected.ranked[0];
        assert_eq!(
            fold.selected_parameters, best.parameters,
            "the fold selects the rank-1 in-sample point for window {:?}",
            fold.window
        );
        assert_eq!(
            fold.in_sample_objective, best.objective_value,
            "the in-sample objective is the winner's objective value"
        );
        assert_eq!(
            fold.in_sample_metrics, best.metrics,
            "the in-sample metrics are preserved verbatim from the optimization"
        );
    }
}

/// The second SYS-19 objective (minimize max drawdown) genuinely changes which point is
/// selected in at least one fold — the objective SELECTION drives the optimization.
#[test]
fn srs_bt_008_in_sample_optimized_matches_independent_sweep_minimize_drawdown() {
    let drawdown = run_walk_forward(
        default_space(),
        ObjectiveFunction::minimize_max_drawdown(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");
    let sharpe = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");

    for fold in &drawdown.folds {
        let expected = independent_sweep(
            default_space(),
            ObjectiveFunction::minimize_max_drawdown(),
            fold.window.in_sample,
        );
        assert_eq!(
            fold.selected_parameters, expected.ranked[0].parameters,
            "each fold selects the hand-derived minimize-drawdown rank-1 point"
        );
    }

    let differs = drawdown
        .folds
        .iter()
        .zip(&sharpe.folds)
        .any(|(d, s)| d.selected_parameters != s.selected_parameters);
    assert!(
        differs,
        "the selected objective genuinely changes the optimized parameter set for some fold"
    );
}

// --------------------------------------------------------------------------- //
// Out-of-sample windows are EVALUATED
// --------------------------------------------------------------------------- //

/// The AC's second obligation: each fold's out-of-sample metrics equal a raw backtest of
/// the SELECTED parameters over the out-of-sample window (an oracle independent of the
/// runner's singleton-sweep path), and the out-of-sample objective is the metric read off
/// those metrics.
#[test]
fn srs_bt_008_out_of_sample_evaluates_winner_on_the_unseen_window() {
    let report = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");

    for fold in &report.folds {
        let expected = raw_evaluate(&fold.selected_parameters, fold.window.out_of_sample);
        assert_eq!(
            fold.out_of_sample_metrics, expected,
            "the out-of-sample metrics are a real backtest of the winner on the unseen window"
        );
        assert_eq!(
            fold.out_of_sample_objective,
            ObjectiveMetric::SharpeRatio.value(&expected),
            "the out-of-sample objective is read off the out-of-sample metrics (honestly \
             Option — never fabricated)"
        );
        // On this fixture the selected round-trip always trades, so Sharpe is defined.
        assert!(fold.out_of_sample_objective.is_some());
    }
}

// --------------------------------------------------------------------------- //
// The NO-LOOKAHEAD safety invariant
// --------------------------------------------------------------------------- //

/// Every fold's out-of-sample window lies strictly after its in-sample window — the
/// walk-forward no-lookahead invariant, checked on the produced report.
#[test]
fn srs_bt_008_no_lookahead_holds_across_folds() {
    let report = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");

    for fold in &report.folds {
        assert!(
            fold.window.in_sample.end < fold.window.out_of_sample.start,
            "out-of-sample must be strictly after in-sample: {:?}",
            fold.window
        );
    }
}

/// A lookahead window (out-of-sample overlapping or preceding in-sample) fails closed, both
/// at the window constructor and at the schedule constructor.
#[test]
fn srs_bt_008_lookahead_schedule_fails_closed() {
    // Overlap: out-of-sample starts inside the in-sample window.
    assert_eq!(
        WalkForwardWindow::new(DateRange::new(1, 5), DateRange::new(5, 6)),
        Err(WalkForwardError::LookaheadWindow {
            in_sample_end: 5,
            out_of_sample_start: 5,
        })
    );
    // A schedule carrying a lookahead window is rejected as a whole.
    let ok = WalkForwardWindow::new(DateRange::new(1, 4), DateRange::new(5, 6)).expect("valid");
    let lookahead = WalkForwardWindow {
        in_sample: DateRange::new(3, 8),
        out_of_sample: DateRange::new(6, 9),
    };
    assert_eq!(
        WalkForwardSchedule::new(vec![ok, lookahead]),
        Err(WalkForwardError::LookaheadWindow {
            in_sample_end: 8,
            out_of_sample_start: 6,
        })
    );
}

// --------------------------------------------------------------------------- //
// Outputs preserve every window; determinism
// --------------------------------------------------------------------------- //

/// `total_folds` equals the number of scheduled windows and `folds.len()`, and each fold's
/// window matches the schedule in forward order — every window is accounted for.
#[test]
fn srs_bt_008_total_folds_accounts_for_every_window() {
    let schedule = default_schedule();
    let expected_windows: Vec<WalkForwardWindow> = schedule.windows().to_vec();
    let report = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        schedule,
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");

    assert_eq!(report.total_folds, expected_windows.len());
    assert_eq!(report.folds.len(), report.total_folds);
    let got_windows: Vec<WalkForwardWindow> = report.folds.iter().map(|f| f.window).collect();
    assert_eq!(
        got_windows, expected_windows,
        "folds are in forward schedule order"
    );
}

/// Two identical runs produce an identical report (SRS-BT-010 discipline: the walk-forward
/// path reuses the sweep's no-parallelism / no-RNG / no-clock evaluation).
#[test]
fn srs_bt_008_deterministic_repeat_runs_identical() {
    let first = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");
    let second = run_walk_forward(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .expect("walk-forward runs");
    assert_eq!(first, second);
}

// --------------------------------------------------------------------------- //
// Fail-closed evaluation
// --------------------------------------------------------------------------- //

/// An in-sample window whose every point has an undefined objective yields no rankable
/// optimum: the analysis fails closed naming the window, never selecting an unranked point.
#[test]
fn srs_bt_008_no_optimum_fails_closed() {
    // lot=0 never trades → win_rate undefined for the only point → ranked empty.
    let single_fold = WalkForwardSchedule::rolling(1, 4, 2, 2, 1).expect("one-fold schedule");
    let err = run_walk_forward(
        space(vec![axis("lot", &["0"]), axis("sell_ts", &["3"])]),
        ObjectiveFunction {
            metric: ObjectiveMetric::WinRate,
            direction: Direction::Maximize,
        },
        single_fold,
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .unwrap_err();
    match err {
        WalkForwardError::NoOptimum { window } => {
            assert_eq!(window.in_sample, DateRange::new(1, 4));
            assert_eq!(window.out_of_sample, DateRange::new(5, 6));
        }
        other => panic!("expected NoOptimum, got {other:?}"),
    }
}

/// A per-fold in-sample sweep failure (a factory-rejected point) aborts the WHOLE analysis,
/// naming the offending window — a partial walk-forward could mis-rank a configuration.
#[test]
fn srs_bt_008_in_sample_failure_names_the_window() {
    let err = run_walk_forward(
        space(vec![axis("lot", &["abc"]), axis("sell_ts", &["3"])]),
        ObjectiveFunction::maximize_sharpe(),
        default_schedule(),
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .unwrap_err();
    match err {
        WalkForwardError::InSampleSweepFailed { window, source } => {
            assert_eq!(
                window.in_sample,
                DateRange::new(1, 4),
                "the first fold is named"
            );
            match source {
                SweepError::PointFailed { parameters, .. } => {
                    assert_eq!(
                        parameters.entries()[0],
                        ("lot".to_string(), "abc".to_string())
                    );
                }
                other => panic!("expected an inner PointFailed, got {other:?}"),
            }
        }
        other => panic!("expected InSampleSweepFailed, got {other:?}"),
    }
}

/// A winner that fails only on its out-of-sample window (a fault injected at a timestamp
/// outside the in-sample window) aborts the analysis with the OUT-OF-SAMPLE failure naming
/// the window — the in-sample optimization succeeded, but the evaluation is fail-closed.
#[test]
fn srs_bt_008_out_of_sample_failure_names_the_window() {
    // fail_at=5 is outside in-sample [1,4] (so optimization succeeds) but inside
    // out-of-sample [5,6] (so the evaluation of the winner fails at ts 5).
    let single_fold = WalkForwardSchedule::rolling(1, 4, 2, 2, 1).expect("one-fold schedule");
    let err = run_walk_forward(
        space(vec![
            axis("fail_at", &["5"]),
            axis("lot", &["5"]),
            axis("sell_ts", &["3"]),
        ]),
        ObjectiveFunction::maximize_sharpe(),
        single_fold,
        &TestFactory::new(),
        &WalkForwardRunner::new(),
    )
    .unwrap_err();
    match err {
        WalkForwardError::OutOfSampleEvalFailed { window, source } => {
            assert_eq!(window.out_of_sample, DateRange::new(5, 6));
            match source {
                SweepError::PointFailed { reason, .. } => {
                    assert!(reason.contains("backtest failed"), "reason: {reason}");
                }
                other => panic!("expected an inner PointFailed, got {other:?}"),
            }
        }
        other => panic!("expected OutOfSampleEvalFailed, got {other:?}"),
    }
}

/// The rolling generator produces exactly the forward-tiling windows (each out-of-sample
/// window immediately following its in-sample window), an abutting window is valid, and an
/// inverted sub-range / boundary overflow fails closed.
#[test]
fn srs_bt_008_schedule_construction_is_exact_and_fail_closed() {
    // rolling(start=1, is_len=4, oos_len=2, step=2, 3 folds):
    //   fold 0: is=[1,4]  oos=[5,6]
    //   fold 1: is=[3,6]  oos=[7,8]
    //   fold 2: is=[5,8]  oos=[9,10]
    let schedule = WalkForwardSchedule::rolling(1, 4, 2, 2, 3).expect("rolling");
    let windows = schedule.windows();
    assert_eq!(windows.len(), 3);
    assert_eq!(windows[0].in_sample, DateRange::new(1, 4));
    assert_eq!(windows[0].out_of_sample, DateRange::new(5, 6));
    assert_eq!(windows[1].in_sample, DateRange::new(3, 6));
    assert_eq!(windows[1].out_of_sample, DateRange::new(7, 8));
    assert_eq!(windows[2].in_sample, DateRange::new(5, 8));
    assert_eq!(windows[2].out_of_sample, DateRange::new(9, 10));

    // An abutting window (out-of-sample immediately after in-sample) is valid — only overlap
    // or precedence is a lookahead.
    assert!(WalkForwardWindow::new(DateRange::new(1, 4), DateRange::new(5, 6)).is_ok());

    // An inverted in-sample sub-range fails closed.
    assert_eq!(
        WalkForwardWindow::new(DateRange::new(6, 3), DateRange::new(7, 8)),
        Err(WalkForwardError::InvalidWindow { start: 6, end: 3 })
    );

    // Boundary arithmetic that would overflow u64 fails closed before any window is built.
    assert_eq!(
        WalkForwardSchedule::rolling(u64::MAX - 1, 4, 2, 4, 3),
        Err(WalkForwardError::ScheduleOverflow)
    );
}

/// Every degenerate schedule maps to its exact fail-closed error — a malformed walk-forward
/// never silently produces a partial or misleading report.
#[test]
fn srs_bt_008_degenerate_schedules_fail_closed() {
    assert_eq!(
        WalkForwardSchedule::new(vec![]),
        Err(WalkForwardError::EmptySchedule)
    );
    // Non-advancing folds.
    let w = WalkForwardWindow::new(DateRange::new(1, 4), DateRange::new(5, 6)).expect("valid");
    assert_eq!(
        WalkForwardSchedule::new(vec![w, w]),
        Err(WalkForwardError::NonMonotonicFolds {
            prev_in_sample_start: 1,
            next_in_sample_start: 1,
        })
    );
    // Rolling zero arguments and overflow.
    assert_eq!(
        WalkForwardSchedule::rolling(1, 0, 2, 2, 3),
        Err(WalkForwardError::ZeroLength)
    );
    assert_eq!(
        WalkForwardSchedule::rolling(1, 4, 2, 0, 3),
        Err(WalkForwardError::ZeroStep)
    );
    assert_eq!(
        WalkForwardSchedule::rolling(1, 4, 2, 2, 0),
        Err(WalkForwardError::ZeroFolds)
    );
    // step (1) < out_of_sample_len (2): overlapping out-of-sample windows are rejected.
    assert_eq!(
        WalkForwardSchedule::rolling(1, 4, 2, 1, 2),
        Err(WalkForwardError::OverlappingFolds {
            prev_out_of_sample_end: 6,
            next_out_of_sample_start: 6,
        })
    );
    // An unbounded operator-supplied fold count fails closed with a typed error BEFORE any
    // allocation — never a Vec::with_capacity capacity-overflow panic or a huge allocation.
    assert_eq!(
        WalkForwardSchedule::rolling(1, 4, 2, 2, usize::MAX),
        Err(WalkForwardError::TooManyFolds {
            count: usize::MAX,
            limit: MAX_WALK_FORWARD_FOLDS,
        })
    );
    assert_eq!(
        WalkForwardSchedule::rolling(1, 4, 2, 2, MAX_WALK_FORWARD_FOLDS + 1),
        Err(WalkForwardError::TooManyFolds {
            count: MAX_WALK_FORWARD_FOLDS + 1,
            limit: MAX_WALK_FORWARD_FOLDS,
        })
    );
}
