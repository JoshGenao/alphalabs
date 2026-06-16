//! SRS-BT-009 durable file-persistence integration test (Rust crate-level).
//!
//! Phase 1 of the SRS-BT-009 closeout: prove the *disk* round trip. It drives the same real
//! producer chain as `srs_bt_009_backtest_store` (run a deterministic backtest through the
//! public [`BacktestEngine`] surface, compute the SRS-BT-005 benchmark report, bundle the seven
//! SRS-BT-009 artifacts into a [`BacktestRecord`]), then exercises the new filesystem layer:
//! [`BacktestResultStore::save_to_path`] writes the checksummed blob atomically to a configured
//! directory, and [`BacktestResultStore::load_from_path`] reads it back. The loaded store must
//! equal the original and answer every query axis (by strategy, by run/completion window, by
//! parameter set, and the combined query) with all seven artifacts intact. A missing file
//! restores an empty store; a corrupt file fails closed (a persisted run is never silently lost).
//!
//! The SSD/NAS tiering of this directory is the deferred SRS-DATA-008 owner; this test covers
//! only the durable file write to a caller-supplied directory.

use std::fs;
use std::path::PathBuf;

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestResult, BacktestStrategy, BarSource, DateRange,
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

/// A well-formed aligned benchmark source (the stand-in for the deferred SRS-DATA-007
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

/// A unique scratch directory under the OS temp dir. The suffix is a fixed per-test label (not a
/// clock/RNG read), so each test owns a distinct directory and parallel runs do not collide.
fn temp_store_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_bt009_persist_query_{label}"));
    let _ = fs::remove_dir_all(&dir);
    dir
}

/// The file names directly under `dir`, sorted -- used to assert a save leaves exactly the final
/// store file and no leftover scratch file.
fn dir_file_names(dir: &PathBuf) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn srs_bt_009_persist_to_disk_and_query_round_trip() {
    let dir = temp_store_dir("round_trip");

    // Two completed fixture backtests with distinct strategies, parameter sets, and completion
    // timestamps -- the producer chain stamps real metrics/trade-log/equity over the engine.
    let momentum_params = parameters(&[("lookback", "20"), ("threshold", "0.5")]);
    let meanrev_params = parameters(&[("window", "5")]);
    let momentum = build_record(
        "run-momentum",
        "momentum",
        1_700_000_000,
        momentum_params.clone(),
    );
    let meanrev = build_record(
        "run-meanrev",
        "meanrev",
        1_700_000_500,
        meanrev_params.clone(),
    );

    let mut store = BacktestResultStore::new();
    store.insert(momentum).unwrap();
    store.insert(meanrev).unwrap();

    // Persist to the configured directory, then confirm the durable file landed on disk and the
    // atomic publish left no scratch file behind (the directory holds exactly the final store).
    store.save_to_path(&dir).unwrap();
    assert!(
        dir.join(STORE_FILENAME).exists(),
        "the persisted store file must exist on disk"
    );
    assert_eq!(
        dir_file_names(&dir),
        vec![STORE_FILENAME.to_string()],
        "the atomic publish must not leave a scratch file behind"
    );
    // The on-disk blob is the checksummed codec output (operator-inspectable).
    let on_disk = fs::read_to_string(dir.join(STORE_FILENAME)).unwrap();
    assert!(on_disk.starts_with("ATP-BACKTEST-RECORD"));

    // Load it back: the reconstructed store equals the original byte-for-byte (same records).
    let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
    assert_eq!(loaded, store);
    assert_eq!(loaded.len(), 2);
    assert_eq!(loaded.serialize(), store.serialize());

    // Every SRS-BT-009 query axis answers off the LOADED store, with all seven artifacts intact.

    // (1) By strategy.
    let by_strategy = loaded.query_by_strategy(&StrategyId::new("momentum"));
    assert_eq!(by_strategy.len(), 1);
    let m = by_strategy[0];
    assert_eq!(m.run_id.as_str(), "run-momentum"); // identity
    assert_eq!(m.parameters, momentum_params); // parameter set
    assert_eq!(m.metrics.benchmark_symbol, "SPY"); // metrics
    assert_eq!(m.comparison.benchmark_symbol, "SPY"); // benchmark comparison
    assert!(!m.trade_log.is_empty()); // trade log
    assert!(!m.equity_curve.is_empty()); // equity curve
    assert_eq!(m.code_version.as_str(), "sha:deadbeef"); // strategy code version
    assert_eq!(m.completed_at_ts, 1_700_000_000); // timestamp

    // (2) By date range -- the SYS-21 run-window axis (the tested period [0, 100]).
    assert_eq!(loaded.query_by_run_window(DateRange::new(50, 200)).len(), 2);
    assert!(loaded
        .query_by_run_window(DateRange::new(200, 300))
        .is_empty());

    // (3) By completion window -- the distinct "when was it run" axis.
    let by_completion =
        loaded.query_by_completion_window(DateRange::new(1_700_000_400, 1_700_000_600));
    assert_eq!(by_completion.len(), 1);
    assert_eq!(by_completion[0].run_id.as_str(), "run-meanrev");

    // (4) By parameter set -- the tuned strategy parameters tell two runs apart.
    assert_eq!(loaded.query_by_parameter_set(&meanrev_params).len(), 1);
    assert!(loaded
        .query_by_parameter_set(&parameters(&[("window", "9")]))
        .is_empty());

    // (5) Combined query ANDs every axis.
    let combined = loaded.query(&RecordQuery {
        strategy_id: Some(StrategyId::new("momentum")),
        run_window: Some(DateRange::new(0, 100)),
        completed_within: Some(DateRange::new(1_699_000_000, 1_700_000_100)),
        parameter_set: Some(momentum_params),
    });
    assert_eq!(combined.len(), 1);
    assert_eq!(combined[0].run_id.as_str(), "run-momentum");

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_load_missing_file_in_present_dir_is_empty() {
    let dir = temp_store_dir("missing_file");
    // The directory is provisioned but nothing has been persisted yet: a fresh install loads
    // empty, not an error.
    fs::create_dir_all(&dir).unwrap();
    let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
    assert!(loaded.is_empty());
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_load_missing_directory_fails_closed() {
    let dir = temp_store_dir("missing_dir");
    // The configured directory is absent (unmounted / deleted / misconfigured). Loading must
    // fail closed rather than present an empty history that silently drops persisted runs.
    assert!(
        BacktestResultStore::load_from_path(&dir).is_err(),
        "a missing store directory must fail closed, not load empty"
    );
}

#[test]
fn srs_bt_009_load_corrupt_file_fails_closed() {
    let dir = temp_store_dir("corrupt");
    let mut store = BacktestResultStore::new();
    store
        .insert(build_record(
            "run-1",
            "momentum",
            1_700_000_000,
            StrategyParameters::new(),
        ))
        .unwrap();
    store.save_to_path(&dir).unwrap();

    // Flip the last byte of the persisted blob so the checksum no longer matches. Loading must
    // fail closed rather than silently drop the run or hand back a partially restored store.
    let path = dir.join(STORE_FILENAME);
    let mut bytes = fs::read(&path).unwrap();
    *bytes.last_mut().unwrap() ^= 0xFF;
    fs::write(&path, &bytes).unwrap();

    assert!(
        BacktestResultStore::load_from_path(&dir).is_err(),
        "a corrupt store file must fail closed"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_resave_atomically_replaces_prior_store() {
    let dir = temp_store_dir("resave");

    let mut first = BacktestResultStore::new();
    first
        .insert(build_record(
            "run-1",
            "momentum",
            1,
            StrategyParameters::new(),
        ))
        .unwrap();
    first.save_to_path(&dir).unwrap();

    // A second, larger store published to the same directory atomically REPLACES the first --
    // the durable file always reflects exactly one fully-written store, never a merge or a
    // partial overwrite, and no scratch file is left behind.
    let mut second = BacktestResultStore::new();
    second
        .insert(build_record(
            "run-1",
            "momentum",
            1,
            StrategyParameters::new(),
        ))
        .unwrap();
    second
        .insert(build_record(
            "run-2",
            "meanrev",
            2,
            StrategyParameters::new(),
        ))
        .unwrap();
    second.save_to_path(&dir).unwrap();

    assert_eq!(dir_file_names(&dir), vec![STORE_FILENAME.to_string()]);
    let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
    assert_eq!(loaded, second);
    assert_eq!(loaded.len(), 2);

    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_bt_009_concurrent_saves_never_corrupt() {
    use std::sync::Arc;
    use std::thread;

    let dir = temp_store_dir("concurrent");

    // One logical store, persisted concurrently from many threads to the same directory. Each
    // save writes a per-call-unique scratch file and atomically renames it, so a reader can never
    // observe a half-written blob: every concurrent publish lands a complete, restorable store.
    let mut store = BacktestResultStore::new();
    store
        .insert(build_record(
            "run-1",
            "momentum",
            1,
            StrategyParameters::new(),
        ))
        .unwrap();
    store
        .insert(build_record(
            "run-2",
            "meanrev",
            2,
            StrategyParameters::new(),
        ))
        .unwrap();
    let store = Arc::new(store);

    let mut handles = Vec::new();
    for _ in 0..8 {
        let store = Arc::clone(&store);
        let dir = dir.clone();
        handles.push(thread::spawn(move || store.save_to_path(&dir).unwrap()));
    }
    for handle in handles {
        handle.join().unwrap();
    }

    // The published file is always a complete, valid store equal to the one every writer wrote,
    // and no scratch file leaks.
    assert_eq!(dir_file_names(&dir), vec![STORE_FILENAME.to_string()]);
    let loaded = BacktestResultStore::load_from_path(&dir).unwrap();
    assert_eq!(loaded, *store);

    let _ = fs::remove_dir_all(&dir);
}
