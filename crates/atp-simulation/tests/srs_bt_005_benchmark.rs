//! SRS-BT-005 end-to-end benchmark-comparison integration test (Rust crate-level).
//!
//! Drives [`atp_simulation::benchmark::compare`] the way a real report would: run a
//! deterministic backtest through the public [`BacktestEngine`] surface to produce a real
//! equity curve + trade log, then compare it against a benchmark resolved through a
//! fixture [`BenchmarkSource`] (the stand-in for the deferred SRS-DATA-007 stored-data
//! resolver). The SPY-default selection, the user-selected benchmark, alpha/beta against
//! the resolved series, report identification, determinism, and the fail-closed trust
//! boundary (wrong symbol / misaligned series) are all exercised from real engine output.
//! Money/level inputs are exact integer minor units; the comparison ratios are
//! dimensionless f64.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::benchmark::{
    compare, BenchmarkError, BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig};
use atp_types::StrategyId;

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

/// Opens `lot` shares on the first bar, then fully closes on `sell_ts` -- one round trip.
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

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: "AAPL".to_string(),
        ts,
        close_minor,
        spread_minor: None,
    }
}

/// The pre-trade starting equity the backtest is launched with; passed to `compare` as
/// the baseline value.
const STARTING_CASH_MINOR: i64 = 1_000_000;

fn run_backtest(
    bars: Vec<BacktestBar>,
    lot: i64,
    sell_ts: u64,
    range: DateRange,
) -> BacktestResult {
    let catalog = FixtureCatalog { bars };
    let request = BacktestRequest {
        strategy_id: StrategyId::new("bt-005"),
        symbol: "AAPL".to_string(),
        data_source: BacktestDataSource::SystemData,
        range,
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config: CostConfig::default(),
    };
    let mut strategy = RoundTrip { lot, sell_ts };
    BacktestEngine::new()
        .run(&request, &mut strategy, &catalog)
        .expect("backtest runs")
}

fn sample_bars() -> Vec<BacktestBar> {
    vec![
        bar(1, 100),
        bar(2, 120),
        bar(3, 90),
        bar(4, 130),
        bar(5, 125),
    ]
}

/// A backtest over the inclusive window [0, 100] whose first bar (ts 1) is strictly after
/// the window open.
fn sample_curve() -> BacktestResult {
    run_backtest(sample_bars(), 10, 5, DateRange::new(0, 100))
}

/// How the fixture benchmark source behaves -- the stand-in for the deferred
/// SRS-DATA-007 stored-data resolver.
enum SourceMode {
    /// A well-formed, aligned series: `baseline` then `baseline + step*(i+1)` per mark.
    Aligned { baseline: i64, step: i64 },
    /// Drops the final point, so the resolved series is one short (misaligned length).
    DropLast { baseline: i64, step: i64 },
    /// The (deferred) data layer read fails operationally.
    Failing(SourceFailure),
}

struct FixtureBenchmark {
    /// The symbol the returned payload claims (a substitution test sets it to differ from
    /// the requested benchmark).
    resolved_symbol: String,
    mode: SourceMode,
}

impl FixtureBenchmark {
    fn aligned(symbol: &str, baseline: i64, step: i64) -> Self {
        Self {
            resolved_symbol: symbol.to_string(),
            mode: SourceMode::Aligned { baseline, step },
        }
    }

    fn drop_last(symbol: &str, baseline: i64, step: i64) -> Self {
        Self {
            resolved_symbol: symbol.to_string(),
            mode: SourceMode::DropLast { baseline, step },
        }
    }

    fn failing(symbol: &str, failure: SourceFailure) -> Self {
        Self {
            resolved_symbol: symbol.to_string(),
            mode: SourceMode::Failing(failure),
        }
    }
}

impl BenchmarkSource for FixtureBenchmark {
    fn levels(
        &self,
        _symbol: &str,
        _window: DateRange,
        axis: &[u64],
    ) -> Result<ResolvedBenchmark, SourceFailure> {
        let (baseline, step) = match self.mode {
            SourceMode::Aligned { baseline, step } | SourceMode::DropLast { baseline, step } => {
                (baseline, step)
            }
            SourceMode::Failing(failure) => return Err(failure),
        };
        // The baseline is the pre-trade prior close: the instant just before the first
        // mark, strictly earlier than axis[0]. It is independent of the inclusive run
        // window, which may open exactly on the first mark.
        let baseline_ts = axis.first().map_or(0, |&first| first.saturating_sub(1));
        let mut levels = vec![BenchmarkPoint {
            ts: baseline_ts,
            level_minor: baseline,
        }];
        for (index, &ts) in axis.iter().enumerate() {
            levels.push(BenchmarkPoint {
                ts,
                level_minor: baseline + step * (index as i64 + 1),
            });
        }
        if matches!(self.mode, SourceMode::DropLast { .. }) {
            levels.pop();
        }
        Ok(ResolvedBenchmark {
            symbol: self.resolved_symbol.clone(),
            levels,
        })
    }
}

#[test]
fn srs_bt_005_compare_defaults_to_spy() {
    // No benchmark selected: the comparison resolves to and identifies SPY (SYS-17).
    let result = sample_curve();
    let source = FixtureBenchmark::aligned("SPY", 400, 5);
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .expect("comparison runs");

    assert_eq!(report.comparison.benchmark_symbol, "SPY");
    assert!(report.comparison.is_default_benchmark);
    assert_eq!(report.metrics.benchmark_symbol, "SPY");
}

#[test]
fn srs_bt_005_alpha_beta_against_selected_benchmark() {
    // A user-selected benchmark (QQQ): alpha and beta are computed against it and the
    // report identifies QQQ, not the default.
    let result = sample_curve();
    let source = FixtureBenchmark::aligned("QQQ", 400, 5);
    let selection = BenchmarkSelection::from_symbol("QQQ").unwrap();
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &selection,
        &source,
        &MetricsConfig::default(),
    )
    .expect("comparison runs");

    assert_eq!(report.comparison.benchmark_symbol, "QQQ");
    assert!(!report.comparison.is_default_benchmark);
    // A five-mark curve with an aligned benchmark series yields defined alpha/beta.
    assert!(report.comparison.alpha.is_some());
    assert!(report.comparison.beta.is_some());
    // The comparison's alpha/beta equal the metric family's.
    assert_eq!(report.comparison.alpha, report.metrics.alpha);
    assert_eq!(report.comparison.beta, report.metrics.beta);
    // No comparison ratio leaked a NaN/inf.
    for value in [
        report.comparison.alpha,
        report.comparison.beta,
        report.comparison.strategy_total_return,
        report.comparison.benchmark_total_return,
        report.comparison.excess_return,
    ]
    .into_iter()
    .flatten()
    {
        assert!(value.is_finite());
    }
}

#[test]
fn srs_bt_005_report_identifies_benchmark_and_excess_return() {
    // The report carries the benchmark identity plus a strategy-vs-benchmark contrast.
    let result = sample_curve();
    let source = FixtureBenchmark::aligned("SPY", 400, 5);
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .expect("comparison runs");

    let strategy = report
        .comparison
        .strategy_total_return
        .expect("strategy return");
    let bench = report
        .comparison
        .benchmark_total_return
        .expect("benchmark return");
    let excess = report.comparison.excess_return.expect("excess return");
    // excess == strategy - benchmark by construction.
    assert!((excess - (strategy - bench)).abs() < 1e-12);
}

#[test]
fn srs_bt_005_compare_is_deterministic() {
    // Identical inputs must produce identical comparisons (SRS-BT-010).
    let result = sample_curve();
    let config = MetricsConfig::default();
    let first = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &FixtureBenchmark::aligned("SPY", 400, 5),
        &config,
    )
    .unwrap();
    let second = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &FixtureBenchmark::aligned("SPY", 400, 5),
        &config,
    )
    .unwrap();
    assert_eq!(first, second);
}

#[test]
fn srs_bt_005_fails_closed_on_substituted_benchmark_series() {
    // Negative control (Codex R3): the selection resolves to SPY but the source returns a
    // well-formed series LABELED QQQ. Identity is bound to the returned payload and
    // validated after the fetch, so the comparison fails closed rather than computing
    // against QQQ's levels while the report identifies SPY.
    let result = sample_curve();
    let source = FixtureBenchmark::aligned("QQQ", 400, 5);
    let err = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(err, BenchmarkError::SourceSymbolMismatch { .. }));
}

#[test]
fn srs_bt_005_fails_closed_on_misaligned_source() {
    // Negative control: a source that drops the final level returns a series that cannot
    // align period-for-period, so the comparison fails closed before any metric.
    let result = sample_curve();
    let source = FixtureBenchmark::drop_last("SPY", 400, 5);
    let err = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(err, BenchmarkError::SourceLengthMismatch { .. }));
}

#[test]
fn srs_bt_005_fails_closed_on_foreign_window() {
    // Negative control (Codex R1): the comparison is bound to the run window. A window
    // that does not contain the equity curve (a stale/foreign window) is rejected, so the
    // benchmark cannot be measured over a different period than the strategy.
    let result = sample_curve(); // marks at ts 1..5
    let source = FixtureBenchmark::aligned("SPY", 400, 5);
    let err = compare(
        STARTING_CASH_MINOR,
        DateRange::new(0, 3), // excludes the ts-4 and ts-5 marks
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert!(matches!(
        err,
        BenchmarkError::EquityMarkOutsideWindow { .. }
    ));
}

#[test]
fn srs_bt_005_accepts_inclusive_window_first_mark_at_range_start() {
    // Codex R6 (locked): DateRange is inclusive, so a real backtest can emit its first
    // equity mark exactly at range.start. compare() accepts such a BacktestResult -- the
    // benchmark baseline is the prior close (before range.start), not pinned to range.start.
    // Window [1, 100] with the first bar at ts 1 => equity_curve[0].ts == 1 == range.start.
    let result = run_backtest(sample_bars(), 10, 5, DateRange::new(1, 100));
    assert_eq!(
        result.range.start, result.equity_curve[0].ts,
        "first mark is at range.start"
    );
    let source = FixtureBenchmark::aligned("SPY", 400, 5);
    let report = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .expect("an inclusive window opening on the first mark is valid");
    assert_eq!(report.comparison.benchmark_symbol, "SPY");
}

#[test]
fn srs_bt_005_propagates_source_unavailable() {
    // Negative control (Codex R2): a (deferred) resolver whose read fails operationally
    // surfaces a typed SourceFailure, not a malformed series, so a caller can retry/alert.
    let result = sample_curve();
    let source = FixtureBenchmark::failing("SPY", SourceFailure::StaleData);
    let err = compare(
        STARTING_CASH_MINOR,
        result.range,
        &result.equity_curve,
        &result.trade_log,
        &BenchmarkSelection::unselected(),
        &source,
        &MetricsConfig::default(),
    )
    .unwrap_err();
    assert_eq!(
        err,
        BenchmarkError::SourceUnavailable {
            failure: SourceFailure::StaleData
        }
    );
}
