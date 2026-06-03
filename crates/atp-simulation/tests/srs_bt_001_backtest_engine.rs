//! SRS-BT-001 end-to-end backtest engine integration test (Rust crate-level).
//!
//! Drives the runnable [`BacktestEngine`] through the public crate surface, the
//! way a real launch would: pick a data source (system catalog **or** uploaded
//! data), pick a configurable date range, run a strategy, and inspect the
//! produced trade log + equity curve. Both data-source paths, date-range
//! sub-selection, the fail-closed branches, and deterministic replay are
//! exercised. Money assertions are exact integer minor units.

use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange,
};
use atp_types::StrategyId;

/// A fixture catalog of bars that declares which catalog it reads from and
/// honors the requested window itself (as a real reader would query by range),
/// so the test exercises both source-side and engine-side range handling.
struct FixtureCatalog {
    source: BacktestDataSource,
    bars: Vec<BacktestBar>,
}

impl BarSource for FixtureCatalog {
    fn source(&self) -> BacktestDataSource {
        self.source
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
        // Well-behaved reader: bound the read and fail closed before returning
        // an oversized response.
        if rows.len() > max_bars {
            return Err(BacktestError::TooManyBars {
                count: rows.len(),
                limit: max_bars,
            });
        }
        Ok(rows)
    }
}

/// A misbehaving source that returns its bars verbatim, ignoring the requested
/// symbol — stands in for a buggy/malicious uploaded-Parquet reader.
struct RawCatalog {
    source: BacktestDataSource,
    bars: Vec<BacktestBar>,
}

impl BarSource for RawCatalog {
    fn source(&self) -> BacktestDataSource {
        self.source
    }

    fn bars(
        &self,
        _symbol: &str,
        _range: &DateRange,
        _max_bars: usize,
    ) -> Result<Vec<BacktestBar>, BacktestError> {
        // Adversarial fixture: ignores the cap so the engine's in-window backstop
        // is exercised.
        Ok(self.bars.clone())
    }
}

/// Buys `lot` shares on the first bar it sees, then holds.
struct BuyOnceAndHold {
    lot: i64,
    bought: bool,
}

impl BacktestStrategy for BuyOnceAndHold {
    fn on_bar(&mut self, _bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
        if self.bought {
            return Ok(0);
        }
        self.bought = true;
        Ok(self.lot)
    }
}

/// Stands in for a Python strategy that raises / times out on every bar.
struct FailingStrategy;

impl BacktestStrategy for FailingStrategy {
    fn on_bar(&mut self, bar: &BacktestBar, _position: i64) -> Result<i64, BacktestError> {
        Err(BacktestError::StrategyFailed {
            ts: bar.ts,
            reason: "python strategy raised".to_string(),
        })
    }
}

fn bar(ts: u64, close_minor: i64) -> BacktestBar {
    BacktestBar {
        symbol: "AAPL".to_string(),
        ts,
        close_minor,
    }
}

fn catalog(source: BacktestDataSource) -> FixtureCatalog {
    FixtureCatalog {
        source,
        bars: vec![
            bar(1, 100),
            bar(2, 110),
            bar(3, 120),
            bar(4, 130),
            bar(5, 140),
        ],
    }
}

fn request(data_source: BacktestDataSource, range: DateRange) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("bt-strategy"),
        symbol: "AAPL".to_string(),
        data_source,
        range,
        starting_cash_minor: 10_000,
    }
}

#[test]
fn runs_end_to_end_over_system_data() {
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 10,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(
            &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
            &mut strategy,
            &source,
        )
        .expect("backtest should run");

    assert_eq!(result.data_source, BacktestDataSource::SystemData);
    assert_eq!(result.bars_processed, 5);
    // Bought 10 @ 100 on bar 1; equity at bar 5 (close 140) = 10 * 140.
    assert_eq!(result.trade_log.len(), 1);
    assert_eq!(result.trade_log[0].quantity, 10);
    assert_eq!(result.trade_log[0].price_minor, 100);
    // Cash 10_000 - 10*100 spent = 9000; equity at close 140 = 9000 + 10*140.
    assert_eq!(result.final_equity_minor, 9000 + 10 * 140);
    assert_eq!(result.equity_curve.len(), 5);
}

#[test]
fn runs_end_to_end_over_uploaded_data() {
    // Same engine, the uploaded-data launch path: proves a backtest can be
    // launched with uploaded data, not just the system catalog (SRS-BT-001 AC).
    let source = catalog(BacktestDataSource::UploadedData);
    let mut strategy = BuyOnceAndHold {
        lot: 5,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(
            &request(BacktestDataSource::UploadedData, DateRange::new(1, 5)),
            &mut strategy,
            &source,
        )
        .expect("backtest should run");

    assert_eq!(result.data_source, BacktestDataSource::UploadedData);
    assert_eq!(result.data_source.as_str(), "uploaded_data");
    // Cash 10_000 - 5*100 spent = 9500; equity at close 140 = 9500 + 5*140.
    assert_eq!(result.final_equity_minor, 9500 + 5 * 140);
}

#[test]
fn data_source_provenance_mismatch_fails_closed() {
    // The request names UploadedData but the supplied source is the system
    // catalog: the engine must reject the mismatch so a result can never
    // misreport which dataset it ran against (distinct fixtures per source).
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::UploadedData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::DataSourceMismatch {
            requested: BacktestDataSource::UploadedData,
            actual: BacktestDataSource::SystemData,
        })
    );
}

#[test]
fn configurable_date_range_sub_selects_bars() {
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 0,
        bought: false,
    };
    // Narrower window [2, 4]: only bars 2, 3, 4 are replayed.
    let result = BacktestEngine::new()
        .run(
            &request(BacktestDataSource::SystemData, DateRange::new(2, 4)),
            &mut strategy,
            &source,
        )
        .expect("backtest should run");

    assert_eq!(result.bars_processed, 3);
    assert_eq!(result.range, DateRange::new(2, 4));
    assert_eq!(result.equity_curve[0].ts, 2);
    assert_eq!(result.equity_curve[2].ts, 4);
}

#[test]
fn empty_window_fails_closed() {
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::SystemData, DateRange::new(40, 50)),
        &mut strategy,
        &source,
    );
    assert_eq!(outcome, Err(BacktestError::EmptyData));
}

#[test]
fn inverted_window_fails_closed() {
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::SystemData, DateRange::new(5, 1)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::InvalidDateRange { start: 5, end: 1 })
    );
}

#[test]
fn foreign_symbol_from_source_fails_closed() {
    // A source (e.g. a buggy uploaded-Parquet reader) that returns a bar for a
    // different symbol must be rejected, not silently traded under AAPL.
    let source = RawCatalog {
        source: BacktestDataSource::UploadedData,
        bars: vec![
            bar(1, 100),
            BacktestBar {
                symbol: "MSFT".to_string(),
                ts: 2,
                close_minor: 110,
            },
        ],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::UploadedData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::UnexpectedSymbol {
            expected: "AAPL".to_string(),
            found: "MSFT".to_string(),
        })
    );
}

#[test]
fn strategy_failure_aborts_the_run() {
    // The deferred Python host can raise/time out; the engine must surface that
    // as a typed error and apply no fills, not silently complete.
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = FailingStrategy;
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::StrategyFailed {
            ts: 1,
            reason: "python strategy raised".to_string(),
        })
    );
}

#[test]
fn duplicate_timestamp_bars_fail_closed() {
    // A source returning two bars for the same instant must fail closed (double
    // fill + order-dependent replay otherwise).
    let source = RawCatalog {
        source: BacktestDataSource::UploadedData,
        bars: vec![bar(2, 100), bar(2, 110)],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::UploadedData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(outcome, Err(BacktestError::DuplicateBar { ts: 2 }));
}

#[test]
fn non_positive_price_fails_closed() {
    // A corrupt non-positive close must fail closed before any fill, so a buy
    // cannot fabricate cash via a negative notional.
    let source = RawCatalog {
        source: BacktestDataSource::SystemData,
        bars: vec![bar(1, 100), bar(2, -50)],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 10,
        bought: false,
    };
    let outcome = BacktestEngine::new().run(
        &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::NonPositivePrice {
            ts: 2,
            close_minor: -50,
        })
    );
}

#[test]
fn large_out_of_window_superset_does_not_trip_the_replay_cap() {
    // The cap counts IN-WINDOW replay bars, not the raw source response: a source
    // returning a big out-of-window superset must not fail a valid narrow backtest.
    let source = RawCatalog {
        source: BacktestDataSource::SystemData,
        bars: vec![
            bar(1, 100),
            bar(2, 110),
            bar(3, 120),
            bar(4, 130),
            bar(5, 140),
        ],
    };
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    // Cap of 2: the 5-bar response exceeds it, but the [4, 5] window is 2 bars.
    let result = BacktestEngine::with_max_bars(2)
        .run(
            &request(BacktestDataSource::SystemData, DateRange::new(4, 5)),
            &mut strategy,
            &source,
        )
        .expect("a narrow in-window slice within the cap should run");
    assert_eq!(result.bars_processed, 2);

    // But an in-window set above the cap still fails closed.
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::with_max_bars(2).run(
        &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::TooManyBars { count: 5, limit: 2 })
    );
}

#[test]
fn well_behaved_source_bounds_its_own_read() {
    // A well-behaved reader fails closed INSIDE bars() (before returning an
    // oversized response), so a large upload cannot materialize unbounded — the
    // cap is passed into the source, not only checked after the fact.
    let source = catalog(BacktestDataSource::SystemData);
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome = BacktestEngine::with_max_bars(2).run(
        &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
        &mut strategy,
        &source,
    );
    assert_eq!(
        outcome,
        Err(BacktestError::TooManyBars { count: 5, limit: 2 })
    );
}

#[test]
fn identical_inputs_produce_identical_results() {
    // The SRS-BT-010 determinism seam: two independent runs with identical
    // inputs return byte-for-byte identical results.
    let run = || {
        let source = catalog(BacktestDataSource::SystemData);
        let mut strategy = BuyOnceAndHold {
            lot: 7,
            bought: false,
        };
        BacktestEngine::new()
            .run(
                &request(BacktestDataSource::SystemData, DateRange::new(1, 5)),
                &mut strategy,
                &source,
            )
            .expect("backtest should run")
    };
    assert_eq!(run(), run());
}
