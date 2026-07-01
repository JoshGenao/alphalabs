//! SRS-BT-009 end-to-end backtest-record persistence integration test (Rust crate-level).
//!
//! Drives the persistence path the way a real backtest-history feature would: run a
//! deterministic backtest through the public [`BacktestEngine`] surface to produce a real
//! equity curve + trade log, compute the SRS-BT-005 [`BenchmarkReport`] (metric family +
//! benchmark comparison) over it, bundle the seven SRS-BT-009 artifacts into a
//! [`BacktestRecord`], insert it into a [`BacktestResultStore`], serialize the store to a
//! checksummed blob, restore it fail-closed, and query the restored records by strategy,
//! date range, and parameter set. The corrupt-blob fail-closed and duplicate-run-id
//! degraded paths are exercised from real engine output. Trade-log/equity money is exact
//! integer minor units; the metric/comparison ratios are dimensionless f64 round-tripped
//! exactly.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::backtest_store::{
    BacktestRecord, BacktestResultStore, CodeVersion, RecordQuery, RunId, StoreError,
    StrategyParameters,
};
use atp_simulation::benchmark::{
    compare, BenchmarkSelection, BenchmarkSource, ResolvedBenchmark, SourceFailure,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::metrics::{BenchmarkPoint, MetricsConfig};
use atp_types::StrategyId;

const STARTING_CASH_MINOR: i64 = 1_000_000;

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

/// A well-formed aligned benchmark source (the stand-in for the deferred (SRS-DATA-007 interface complete; real data = SRS-DATA-005 / SRS-FAC-001)
/// resolver): a pre-trade baseline then `baseline + step*(i+1)` per equity mark.
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

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: "AAPL".to_string(),
        ts,
        close_minor,
        spread_minor: None,
    }
}

fn request(strategy: &str, range: DateRange) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new(strategy),
        symbol: "AAPL".to_string(),
        data_source: BacktestDataSource::SystemData,
        range,
        starting_cash_minor: STARTING_CASH_MINOR,
        cost_config: CostConfig::default(),
    }
}

fn run_backtest(req: &BacktestRequest) -> BacktestResult {
    let catalog = FixtureCatalog {
        bars: vec![
            bar(1, 100),
            bar(2, 120),
            bar(3, 90),
            bar(4, 130),
            bar(5, 125),
        ],
    };
    let mut strategy = RoundTrip {
        lot: 10,
        sell_ts: 5,
    };
    BacktestEngine::new()
        .run(req, &mut strategy, &catalog)
        .expect("backtest runs")
}

/// The full producer chain: run a backtest, compare it against an SPY-default benchmark, and
/// bundle the SRS-BT-009 artifacts (including the strategy parameter set) into a validated
/// record.
fn build_record(
    run_id: &str,
    strategy: &str,
    completed_at: u64,
    parameters: StrategyParameters,
) -> BacktestRecord {
    let req = request(strategy, DateRange::new(0, 100));
    let result = run_backtest(&req);
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
    .expect("comparison runs");

    // The SAFE producer path: bind the persisted artifacts to the BacktestResult that produced
    // them and verify the request's provenance (data source + window) matches.
    BacktestRecord::from_result(
        RunId::new(run_id).unwrap(),
        req,
        parameters,
        report.metrics,
        report.comparison,
        &result,
        CodeVersion::new("sha:deadbeef").unwrap(),
        completed_at,
    )
    .expect("record is coherent")
}

/// A strategy parameter set from `(name, value)` pairs.
fn parameters(pairs: &[(&str, &str)]) -> StrategyParameters {
    StrategyParameters::from_pairs(pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())))
        .expect("valid parameter set")
}

#[test]
fn srs_bt_009_persists_and_queries_completed_backtest() {
    // The headline: a completed backtest's artifacts are persisted into one record and are
    // queryable by strategy, date range, and parameter set.
    let sweep_point = parameters(&[("lookback", "20"), ("threshold", "0.5")]);
    let record = build_record("run-1", "momentum", 1_700_000_000, sweep_point.clone());
    // The benchmark comparison and metrics survived into the record (the SRS-BT-005 fields).
    assert_eq!(record.comparison.benchmark_symbol, "SPY");
    assert!(record.comparison.is_default_benchmark);
    assert_eq!(record.metrics.benchmark_symbol, "SPY");
    assert!(!record.trade_log.is_empty());
    assert!(!record.equity_curve.is_empty());
    assert_eq!(record.parameters, sweep_point);

    let mut store = BacktestResultStore::new();
    store.insert(record).unwrap();

    // By strategy.
    let by_strategy = store.query_by_strategy(&StrategyId::new("momentum"));
    assert_eq!(by_strategy.len(), 1);
    assert_eq!(by_strategy[0].run_id.as_str(), "run-1");
    assert_eq!(by_strategy[0].code_version.as_str(), "sha:deadbeef");

    // By date range = the SYS-21 run-window axis (the tested period [0, 100]).
    let by_run_window = store.query_by_run_window(DateRange::new(50, 200));
    assert_eq!(by_run_window.len(), 1);
    assert!(store
        .query_by_run_window(DateRange::new(200, 300))
        .is_empty());

    // By completion window (the distinct "when was it run" axis).
    let by_completion =
        store.query_by_completion_window(DateRange::new(1_699_000_000, 1_701_000_000));
    assert_eq!(by_completion.len(), 1);
    assert!(store
        .query_by_completion_window(DateRange::new(0, 1_000_000))
        .is_empty());

    // By parameter set (the tuned strategy parameters).
    let by_params = store.query_by_parameter_set(&sweep_point);
    assert_eq!(by_params.len(), 1);
    // A different sweep point matches nothing.
    let other = parameters(&[("lookback", "30"), ("threshold", "0.5")]);
    assert!(store.query_by_parameter_set(&other).is_empty());

    // Combined query ANDs all axes (strategy + run window + completion window + parameter set).
    let combined = store.query(&RecordQuery {
        strategy_id: Some(StrategyId::new("momentum")),
        run_window: Some(DateRange::new(0, 100)),
        completed_within: Some(DateRange::new(1_699_000_000, 1_701_000_000)),
        parameter_set: Some(sweep_point),
    });
    assert_eq!(combined.len(), 1);
}

#[test]
fn srs_bt_009_serialize_restore_round_trips() {
    let mut store = BacktestResultStore::new();
    store
        .insert(build_record(
            "run-a",
            "momentum",
            1_700_000_100,
            parameters(&[("lookback", "10")]),
        ))
        .unwrap();
    store
        .insert(build_record(
            "run-b",
            "meanrev",
            1_700_000_050,
            StrategyParameters::new(),
        ))
        .unwrap();

    let blob = store.serialize();
    let restored = BacktestResultStore::restore(&blob).unwrap();
    assert_eq!(restored, store);
    // Deterministic: re-serializing the restored store is byte-identical.
    assert_eq!(restored.serialize(), blob);
    // The metrics + benchmark comparison survived the round trip exactly.
    let restored_b = restored.query_by_strategy(&StrategyId::new("meanrev"));
    assert_eq!(restored_b.len(), 1);
    assert_eq!(restored_b[0].comparison.benchmark_symbol, "SPY");
}

#[test]
fn srs_bt_009_restore_fails_closed_on_corruption() {
    let mut store = BacktestResultStore::new();
    store
        .insert(build_record(
            "run-1",
            "momentum",
            1_700_000_000,
            StrategyParameters::new(),
        ))
        .unwrap();
    let blob = store.serialize();

    // A tampered body byte (without fixing the checksum) is rejected before any state is built.
    let tampered = blob.replacen("momentum", "momentun", 1);
    assert!(matches!(
        BacktestResultStore::restore(&tampered),
        Err(StoreError::ChecksumMismatch)
    ));

    // A foreign magic header is rejected.
    let foreign = blob.replacen("ATP-BACKTEST-RECORD", "ATP-FOREIGN", 1);
    assert!(matches!(
        BacktestResultStore::restore(&foreign),
        Err(StoreError::CorruptRecord { .. })
    ));
}

#[test]
fn srs_bt_009_from_result_rejects_mismatched_provenance() {
    // A request whose data source disagrees with the producing BacktestResult is rejected, so a
    // record can never be persisted under false provenance.
    let req = request("momentum", DateRange::new(0, 100));
    let result = run_backtest(&req);
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
    .expect("comparison runs");

    // Same run window, but the request claims UploadedData while the result is SystemData.
    let mut mismatched = req.clone();
    mismatched.data_source = BacktestDataSource::UploadedData;
    let err = BacktestRecord::from_result(
        RunId::new("bad").unwrap(),
        mismatched,
        StrategyParameters::new(),
        report.metrics.clone(),
        report.comparison.clone(),
        &result,
        CodeVersion::new("v").unwrap(),
        1,
    )
    .unwrap_err();
    assert!(matches!(err, StoreError::InconsistentField { .. }));

    // A request whose window disagrees with the result is also rejected.
    let mut wrong_window = request("momentum", DateRange::new(0, 999));
    wrong_window.data_source = result.data_source;
    let err = BacktestRecord::from_result(
        RunId::new("bad2").unwrap(),
        wrong_window,
        StrategyParameters::new(),
        report.metrics,
        report.comparison,
        &result,
        CodeVersion::new("v").unwrap(),
        1,
    )
    .unwrap_err();
    assert!(matches!(err, StoreError::InconsistentField { .. }));
}

#[test]
fn srs_bt_009_rejects_duplicate_run_id() {
    let mut store = BacktestResultStore::new();
    store
        .insert(build_record(
            "dup",
            "momentum",
            1_700_000_000,
            StrategyParameters::new(),
        ))
        .unwrap();
    let err = store
        .insert(build_record(
            "dup",
            "meanrev",
            1_700_000_001,
            StrategyParameters::new(),
        ))
        .unwrap_err();
    assert!(matches!(err, StoreError::DuplicateRunId { .. }));
    assert_eq!(store.len(), 1);
}
