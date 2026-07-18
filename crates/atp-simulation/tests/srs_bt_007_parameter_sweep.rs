//! SRS-BT-007 acceptance: "A parameter space definition produces ranked backtest
//! results by the selected objective function" (SyRS SYS-19; StRS SN-1.16).
//!
//! Library-level verification over the public [`sweep`] API with fixture bars, a
//! fixture benchmark source, and a genuinely parameterized fixture strategy — the
//! ranking is independently re-derived in-test (hand-run `compare` per point, hand
//! sort) so a sweep's order is proven against ground truth, not against itself. The
//! fail-closed decisions are each pinned: an undefined objective routes to the
//! unranked bucket (never fabricated, never dropped), any per-point failure aborts
//! the whole sweep naming the point, the cardinality cap fires before any backtest
//! runs, and degenerate space definitions are rejected variant by variant.

use std::cell::Cell;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestError, BacktestRequest, BacktestStrategy, BarSource,
    DateRange,
};
use atp_simulation::backtest_store::StrategyParameters;
use atp_simulation::benchmark::{
    compare, BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig};
use atp_simulation::sweep::{
    Direction, ObjectiveFunction, ObjectiveMetric, ParameterAxis, ParameterSpace, SweepError,
    SweepEvaluation, SweepRequest, SweepRunner, SweepStrategyFactory, UnrankedReason,
    MAX_SWEEP_POINTS,
};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;
const SYMBOL: &str = "AAPL";

// --------------------------------------------------------------------------- //
// Fixtures (the bt009 fixture chain, with a genuinely parameterized strategy)
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

/// The parameterized fixture strategy. `lot = 0` deliberately never trades (the
/// undefined-objective case: zero trades → `win_rate = None`); `fail_at` (when set)
/// makes `on_bar` error at that bar (the engine-failure case).
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

/// The test factory: parses `lot` / `sell_ts` (and the optional `fail_at` fault
/// injector), failing closed on anything else. Counts builds so the cap test can
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
        strategy_id: StrategyId::new("sweep-test"),
        symbol: SYMBOL.to_string(),
        data_source: BacktestDataSource::SystemData,
        range: DateRange::new(0, 100),
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

/// Run a sweep over `space` with the given objective through the public runner.
fn run_sweep(
    space: ParameterSpace,
    objective: ObjectiveFunction,
    runner: &SweepRunner,
    factory: &TestFactory,
) -> Result<atp_simulation::sweep::SweepReport, SweepError> {
    let request = SweepRequest {
        base: base_request(),
        space,
        objective,
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

/// Ground truth for one point: run the SAME engine + compare chain by hand.
fn hand_evaluate(lot: i64, sell_ts: u64, metric: ObjectiveMetric) -> Option<f64> {
    let request = base_request();
    let mut strategy = TestStrategy {
        lot,
        sell_ts,
        fail_at: None,
    };
    let result = atp_simulation::backtest::BacktestEngine::new()
        .run(&request, &mut strategy, &fixture_catalog())
        .expect("fixture backtest runs");
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &fixture_benchmark(),
        &MetricsConfig::default(),
    )
    .expect("fixture comparison computes");
    metric.value(&report.metrics)
}

// --------------------------------------------------------------------------- //
// The parameter space definition
// --------------------------------------------------------------------------- //

/// A 3×2 space enumerates exactly the six distinct combinations, deterministically:
/// axes in name order, values in declared order, last axis fastest.
#[test]
fn srs_bt_007_cartesian_product_covers_all_combinations() {
    let space = default_space();
    assert_eq!(space.point_count(), 6);

    let points = space.points(MAX_SWEEP_POINTS).expect("under the cap");
    let rendered: Vec<String> = points
        .iter()
        .map(|point| {
            point
                .entries()
                .iter()
                .map(|(key, value)| format!("{key}={value}"))
                .collect::<Vec<_>>()
                .join(",")
        })
        .collect();
    assert_eq!(
        rendered,
        vec![
            "lot=5,sell_ts=3",
            "lot=5,sell_ts=5",
            "lot=10,sell_ts=3",
            "lot=10,sell_ts=5",
            "lot=20,sell_ts=3",
            "lot=20,sell_ts=5",
        ],
        "deterministic enumeration: axes name-ordered, values declared-ordered, last axis fastest"
    );

    // All points pairwise distinct (the precondition for the strict tie-break order).
    for (index, point) in points.iter().enumerate() {
        assert!(!points[..index].contains(point));
    }

    // Declaration order does not matter: the same axes given in reverse enumerate
    // identically (the space is canonical, like StrategyParameters itself).
    let reversed = space_from(vec![
        axis("sell_ts", &["3", "5"]),
        axis("lot", &["5", "10", "20"]),
    ]);
    assert_eq!(
        reversed.points(MAX_SWEEP_POINTS).expect("under the cap"),
        points
    );
}

fn space_from(axes: Vec<ParameterAxis>) -> ParameterSpace {
    ParameterSpace::new(axes).expect("valid fixture space")
}

/// Every malformed space definition maps to its exact fail-closed error.
#[test]
fn srs_bt_007_degenerate_spaces_fail_closed() {
    assert_eq!(
        ParameterSpace::new(vec![]).unwrap_err(),
        SweepError::EmptySpace
    );
    assert_eq!(
        ParameterAxis::new("  ", vec!["1".to_string()]).unwrap_err(),
        SweepError::EmptyAxisName
    );
    assert_eq!(
        ParameterSpace::new(vec![axis("lot", &["5"]), axis("lot", &["10"])]).unwrap_err(),
        SweepError::DuplicateAxis {
            name: "lot".to_string()
        }
    );
    assert_eq!(
        ParameterAxis::new("lot", vec![]).unwrap_err(),
        SweepError::EmptyAxisValues {
            name: "lot".to_string()
        }
    );
    assert_eq!(
        ParameterAxis::new("lot", vec!["5".to_string(), " ".to_string()]).unwrap_err(),
        SweepError::EmptyAxisValue {
            name: "lot".to_string()
        }
    );
    assert_eq!(
        ParameterAxis::new("lot", vec!["5".to_string(), "5".to_string()]).unwrap_err(),
        SweepError::DuplicateAxisValue {
            name: "lot".to_string(),
            value: "5".to_string()
        }
    );
}

// --------------------------------------------------------------------------- //
// Ranking by the selected objective function
// --------------------------------------------------------------------------- //

/// The AC end to end for the first SYS-19 named objective: the sweep's ranking equals
/// an independent hand-derived ranking (hand-run compare per point, hand sort by
/// Sharpe descending), rank fields are 1-based positional, and every point is
/// accounted for.
#[test]
fn srs_bt_007_ranks_by_maximize_sharpe() {
    let factory = TestFactory::new();
    let report = run_sweep(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &factory,
    )
    .expect("sweep runs");

    assert_eq!(report.total_points, 6);
    assert_eq!(report.ranked.len() + report.unranked.len(), 6);
    assert!(report.unranked.is_empty());

    // Ground truth: evaluate each combination by hand and sort by Sharpe descending.
    let mut expected: Vec<(i64, u64, f64)> = Vec::new();
    for lot in [5i64, 10, 20] {
        for sell_ts in [3u64, 5] {
            let sharpe = hand_evaluate(lot, sell_ts, ObjectiveMetric::SharpeRatio)
                .expect("fixture Sharpe is defined");
            expected.push((lot, sell_ts, sharpe));
        }
    }
    expected.sort_by(|a, b| b.2.total_cmp(&a.2));

    for (index, (lot, sell_ts, sharpe)) in expected.iter().enumerate() {
        let point = &report.ranked[index];
        assert_eq!(point.rank, index + 1, "ranks are 1-based positional");
        assert_eq!(
            point.parameters.entries(),
            &[
                ("lot".to_string(), lot.to_string()),
                ("sell_ts".to_string(), sell_ts.to_string())
            ],
            "rank {} is the hand-derived point",
            index + 1
        );
        assert_eq!(
            point.objective_value, *sharpe,
            "the objective value IS the point's Sharpe ratio"
        );
        assert_eq!(point.metrics.sharpe_ratio, Some(*sharpe));
        assert!(point.trade_count > 0);
    }
}

/// The second SYS-19 named objective: minimizing max drawdown produces the
/// hand-derived ascending-drawdown order (a genuinely different ranking than the
/// Sharpe one — the objective SELECTION drives the result).
#[test]
fn srs_bt_007_direction_minimize_max_drawdown() {
    let factory = TestFactory::new();
    let report = run_sweep(
        default_space(),
        ObjectiveFunction::minimize_max_drawdown(),
        &SweepRunner::new(),
        &factory,
    )
    .expect("sweep runs");

    let mut expected: Vec<(i64, u64, f64)> = Vec::new();
    for lot in [5i64, 10, 20] {
        for sell_ts in [3u64, 5] {
            let drawdown = hand_evaluate(lot, sell_ts, ObjectiveMetric::MaxDrawdown)
                .expect("fixture drawdown is defined");
            expected.push((lot, sell_ts, drawdown));
        }
    }
    expected.sort_by(|a, b| a.2.total_cmp(&b.2));

    let ranked_points: Vec<(String, String)> = report
        .ranked
        .iter()
        .map(|point| {
            (
                point.parameters.entries()[0].1.clone(),
                point.parameters.entries()[1].1.clone(),
            )
        })
        .collect();
    let expected_points: Vec<(String, String)> = expected
        .iter()
        .map(|(lot, sell_ts, _)| (lot.to_string(), sell_ts.to_string()))
        .collect();
    assert_eq!(ranked_points, expected_points);
    assert_eq!(
        report.ranked[0].objective_value, expected[0].2,
        "rank 1 carries the smallest drawdown"
    );

    // The two named objectives rank differently over this fixture: the
    // smallest-drawdown point is the smallest lot, while the best-Sharpe point is not.
    let sharpe_report = run_sweep(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .expect("sweep runs");
    assert_ne!(
        sharpe_report.ranked[0].parameters, report.ranked[0].parameters,
        "the selected objective genuinely changes rank 1"
    );
}

/// A point whose objective metric is mathematically undefined is reported unranked
/// with the reason — never ranked, never a fabricated value, never dropped.
#[test]
fn srs_bt_007_undefined_objective_is_unranked_not_fabricated() {
    // lot=0 never trades: zero trades make win_rate mathematically undefined.
    let space = space(vec![axis("lot", &["0", "5"]), axis("sell_ts", &["3"])]);
    let factory = TestFactory::new();
    let report = run_sweep(
        space,
        ObjectiveFunction {
            metric: ObjectiveMetric::WinRate,
            direction: Direction::Maximize,
        },
        &SweepRunner::new(),
        &factory,
    )
    .expect("sweep runs");

    assert_eq!(report.total_points, 2);
    assert_eq!(report.ranked.len(), 1, "only the trading point is ranked");
    assert_eq!(report.unranked.len(), 1);
    assert_eq!(
        report.total_points,
        report.ranked.len() + report.unranked.len(),
        "every point is accounted for"
    );

    let unranked = &report.unranked[0];
    assert_eq!(
        unranked.parameters.entries()[0],
        ("lot".to_string(), "0".to_string())
    );
    assert_eq!(unranked.reason, UnrankedReason::ObjectiveUndefined);
    assert_eq!(unranked.reason.as_str(), "objective_undefined");
    assert_eq!(
        unranked.metrics.win_rate, None,
        "the undefined metric stays None — no fabricated stand-in"
    );
    assert_eq!(
        report.ranked[0].parameters.entries()[0],
        ("lot".to_string(), "5".to_string())
    );
}

/// Two identical runs produce an identical report (SRS-BT-010 discipline: no
/// parallelism, RNG, or clock anywhere in the sweep path).
#[test]
fn srs_bt_007_deterministic_repeat_runs_identical() {
    let first = run_sweep(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .expect("sweep runs");
    let second = run_sweep(
        default_space(),
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .expect("sweep runs");
    assert_eq!(first, second);
}

/// Equal objective values break ties by the points' canonical parameter entries, so
/// the order is deterministic (lexicographic on the sorted entries — documented as
/// determinism, not numeric intuition).
#[test]
fn srs_bt_007_ties_break_by_canonical_parameter_order() {
    // Both sell_ts values are past the last bar (5), so the two points hold to the end
    // and produce byte-identical trades, curves, and metrics — a genuine tie.
    let space = space(vec![axis("lot", &["5"]), axis("sell_ts", &["10", "20"])]);
    let report = run_sweep(
        space,
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .expect("sweep runs");

    assert_eq!(report.ranked.len(), 2);
    assert_eq!(
        report.ranked[0].objective_value, report.ranked[1].objective_value,
        "the two points genuinely tie on the objective"
    );
    assert_eq!(
        report.ranked[0].parameters.entries()[1],
        ("sell_ts".to_string(), "10".to_string()),
        "ties order by canonical entries: '10' < '20' lexicographically"
    );
    assert_eq!(report.ranked[0].rank, 1);
    assert_eq!(report.ranked[1].rank, 2);
}

// --------------------------------------------------------------------------- //
// Fail-closed evaluation
// --------------------------------------------------------------------------- //

/// The cardinality cap fires BEFORE any strategy is built or any backtest runs.
#[test]
fn srs_bt_007_point_cap_fails_before_any_backtest() {
    let factory = TestFactory::new();
    let err = run_sweep(
        default_space(), // 6 points
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::with_max_points(4),
        &factory,
    )
    .unwrap_err();
    assert_eq!(err, SweepError::TooManyPoints { count: 6, limit: 4 });
    assert_eq!(
        factory.builds.get(),
        0,
        "the cap fired before a single strategy was built"
    );
}

/// A factory rejection aborts the whole sweep naming the exact offending point.
#[test]
fn srs_bt_007_factory_rejection_names_offending_point() {
    let space = space(vec![axis("lot", &["5", "abc"]), axis("sell_ts", &["3"])]);
    let err = run_sweep(
        space,
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .unwrap_err();
    match err {
        SweepError::PointFailed { parameters, reason } => {
            assert_eq!(
                parameters.entries()[0],
                ("lot".to_string(), "abc".to_string()),
                "the offending point is named"
            );
            assert!(
                reason.contains("abc"),
                "the reason names the bad value: {reason}"
            );
        }
        other => panic!("expected PointFailed, got {other:?}"),
    }
}

/// An engine failure inside one point aborts the whole sweep (no partial ranking),
/// naming the point.
#[test]
fn srs_bt_007_backtest_failure_fails_sweep_closed() {
    let space = space(vec![
        axis("fail_at", &["3"]),
        axis("lot", &["5"]),
        axis("sell_ts", &["5"]),
    ]);
    let err = run_sweep(
        space,
        ObjectiveFunction::maximize_sharpe(),
        &SweepRunner::new(),
        &TestFactory::new(),
    )
    .unwrap_err();
    match err {
        SweepError::PointFailed { parameters, reason } => {
            assert_eq!(
                parameters.entries()[0],
                ("fail_at".to_string(), "3".to_string())
            );
            assert!(
                reason.contains("backtest failed"),
                "the reason attributes the engine failure: {reason}"
            );
        }
        other => panic!("expected PointFailed, got {other:?}"),
    }
}

// --------------------------------------------------------------------------- //
// The objective selector surface
// --------------------------------------------------------------------------- //

/// All eight SYS-16 metric tokens and both direction tokens round-trip through
/// parse/as_str; anything outside the allowlist fails closed.
#[test]
fn srs_bt_007_objective_tokens_round_trip() {
    assert_eq!(ObjectiveMetric::ALL.len(), 8);
    for metric in ObjectiveMetric::ALL {
        assert_eq!(
            ObjectiveMetric::parse(metric.as_str()).expect("canonical token parses"),
            metric
        );
    }
    assert_eq!(
        ObjectiveMetric::parse("profit").unwrap_err(),
        SweepError::UnknownMetric {
            token: "profit".to_string()
        }
    );

    for direction in [Direction::Maximize, Direction::Minimize] {
        assert_eq!(
            Direction::parse(direction.as_str()).expect("canonical token parses"),
            direction
        );
    }
    assert_eq!(
        Direction::parse("up").unwrap_err(),
        SweepError::UnknownDirection {
            token: "up".to_string()
        }
    );

    // The two SYS-19 named conveniences are exactly their metric + direction.
    assert_eq!(
        ObjectiveFunction::maximize_sharpe(),
        ObjectiveFunction {
            metric: ObjectiveMetric::SharpeRatio,
            direction: Direction::Maximize
        }
    );
    assert_eq!(
        ObjectiveFunction::minimize_max_drawdown(),
        ObjectiveFunction {
            metric: ObjectiveMetric::MaxDrawdown,
            direction: Direction::Minimize
        }
    );
}
