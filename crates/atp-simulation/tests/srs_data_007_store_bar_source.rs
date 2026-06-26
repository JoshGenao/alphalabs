//! SRS-DATA-007 close — the backtest engine is a real consumer of the unified historical interface.
//!
//! The acceptance criterion names "strategy code, **backtests**, factor jobs, and notebooks" as the
//! consumers that "query by symbol, date range, and resolution **without specifying the original source
//! provider**." This crate-level integration test wires the real [`BacktestEngine`] to the durable
//! [`MarketDataStore`] through [`StoreBarSource`] — shipped product code, not a fixture stand-in — and
//! drives a full backtest the way a launch would: the engine reads its bars from the system catalog via
//! the source-neutral SRS-DATA-007 query path, replays them, and produces a real trade log + equity
//! curve. Both the raw and the coverage-gated split-adjusted bases are exercised, plus every fail-closed
//! boundary (uncovered split-adjusted, the bounded read, and a non-representable window bound).
//!
//! Source-neutrality is structural: [`StoreBarSource`] is constructed from only `(store, symbol via the
//! query, resolution/kind, normalization)` — there is no provider / vendor / source parameter anywhere
//! on the path, and a [`MarketDataRecord`] carries no origin field, so a backtest cannot name or branch
//! on where a bar came from (the `bar_struct.forbidden_fields` contract check pins it statically).

use atp_data::store::{
    coverage_record, DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
};
use atp_simulation::backtest::{
    BacktestBar, BacktestDataSource, BacktestEngine, BacktestError, BacktestRequest,
    BacktestStrategy, BarSource, DateRange,
};
use atp_simulation::cost::CostConfig;
use atp_simulation::store_bar_source::{Normalization, StoreBarSource};
use atp_types::StrategyId;

// --------------------------------------------------------------------------- //
// Store fixtures (built directly from the public atp-data surface)
// --------------------------------------------------------------------------- //

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn daily_bar(symbol: &str, event_ts: i64, close: i64, volume: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [field("close", close), field("volume", volume)],
    )
    .expect("well-formed daily bar")
}

fn minute_bar(symbol: &str, event_ts: i64, close: i64, volume: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::MinuteEquityBar,
            symbol: symbol.to_string(),
            resolution: "1m".to_string(),
            event_ts,
            option_contract: None,
        },
        [field("close", close), field("volume", volume)],
    )
    .expect("well-formed minute bar")
}

fn split(symbol: &str, effective_ts: i64, numerator: i64, denominator: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionSplit,
            symbol: symbol.to_string(),
            resolution: "split".to_string(),
            event_ts: effective_ts,
            option_contract: None,
        },
        [field("denominator", denominator), field("numerator", numerator)],
    )
    .expect("well-formed split record")
}

fn store_of(records: impl IntoIterator<Item = MarketDataRecord>) -> MarketDataStore {
    let mut store = MarketDataStore::new();
    for record in records {
        store.upsert(record).expect("fixture upsert");
    }
    store
}

/// Buys `lot` shares on the first bar it sees, then holds — so the fill price equals the first bar's
/// (possibly adjusted) close, which is exactly what we assert about the basis the engine read.
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

fn request(symbol: &str, range: DateRange) -> BacktestRequest {
    BacktestRequest {
        strategy_id: StrategyId::new("data007-backtest"),
        symbol: symbol.to_string(),
        data_source: BacktestDataSource::SystemData,
        range,
        starting_cash_minor: 1_000_000,
        cost_config: CostConfig::zero(),
    }
}

// --------------------------------------------------------------------------- //
// End-to-end: a backtest reads the unified store by symbol / range / resolution
// --------------------------------------------------------------------------- //

#[test]
fn backtest_runs_end_to_end_over_raw_store_bars() {
    // Five raw daily bars; the engine reads them through StoreBarSource (no provider named) and runs.
    let store = store_of([
        daily_bar("AAPL", 1, 100, 1_000),
        daily_bar("AAPL", 2, 110, 1_000),
        daily_bar("AAPL", 3, 120, 1_000),
        daily_bar("AAPL", 4, 130, 1_000),
        daily_bar("AAPL", 5, 140, 1_000),
        // A foreign symbol in the same store must never leak into AAPL's backtest.
        daily_bar("MSFT", 3, 999, 1_000),
    ]);
    let source = StoreBarSource::daily(&store, Normalization::Raw);
    assert_eq!(source.source(), BacktestDataSource::SystemData);

    let mut strategy = BuyOnceAndHold {
        lot: 10,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request("AAPL", DateRange::new(1, 5)), &mut strategy, &source)
        .expect("backtest runs over the unified store");

    assert_eq!(result.data_source, BacktestDataSource::SystemData);
    assert_eq!(result.bars_processed, 5, "only the 5 AAPL bars, never MSFT");
    assert_eq!(result.trade_log.len(), 1);
    assert_eq!(result.trade_log[0].price_minor, 100, "bought at the raw close");
    // Cash 1_000_000 - 10*100 spent = 999_000; equity at close 140 = 999_000 + 10*140.
    assert_eq!(result.final_equity_minor, 999_000 + 10 * 140);
    assert_eq!(result.equity_curve.len(), 5);
}

#[test]
fn backtest_runs_over_minute_store_bars_and_excludes_daily() {
    // StoreBarSource::minute queries the 1m / MinuteEquityBar dataset. A daily bar sharing the symbol must
    // be EXCLUDED (different kind + resolution), so a copy/paste drift in the minute resolution or kind is
    // caught here rather than leaving SRS-DATA-007 minute backtests silently unverified. (If the daily bar
    // leaked in, the engine would either over-count or trip DuplicateBar at the shared ts.)
    let store = store_of([
        minute_bar("AAPL", 1, 100, 1_000),
        minute_bar("AAPL", 2, 110, 1_000),
        minute_bar("AAPL", 3, 120, 1_000),
        daily_bar("AAPL", 2, 999, 1_000),
    ]);
    let source = StoreBarSource::minute(&store, Normalization::Raw);
    assert_eq!(source.source(), BacktestDataSource::SystemData);

    let mut strategy = BuyOnceAndHold {
        lot: 5,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request("AAPL", DateRange::new(1, 3)), &mut strategy, &source)
        .expect("minute backtest runs over the unified store");

    assert_eq!(result.bars_processed, 3, "only the 3 minute bars, never the daily bar");
    assert_eq!(result.trade_log.len(), 1);
    assert_eq!(
        result.trade_log[0].price_minor, 100,
        "bought at the first MINUTE close (not the daily 999)"
    );
}

#[test]
fn backtest_runs_end_to_end_over_covered_split_adjusted_bars() {
    // AAPL bar @100 (close 10000), a 4-for-1 split @200, coverage through 200. A query of [0,100] is
    // covered (frontier 200 >= 100) and the pre-split bar is re-quoted: 10000 / 4 = 2500. The engine
    // trades on the ADJUSTED series read through the gated unified path — the SRS-DATA-011 "correct P&L
    // for backtests spanning corporate-action dates" property, end to end.
    let store = store_of([
        daily_bar("AAPL", 100, 10_000, 100_000),
        split("AAPL", 200, 4, 1),
        coverage_record(200, "AAPL"),
    ]);
    let source = StoreBarSource::daily(&store, Normalization::SplitAdjusted);

    let mut strategy = BuyOnceAndHold {
        lot: 4,
        bought: false,
    };
    let result = BacktestEngine::new()
        .run(&request("AAPL", DateRange::new(0, 100)), &mut strategy, &source)
        .expect("covered split-adjusted backtest runs");

    assert_eq!(result.bars_processed, 1);
    assert_eq!(result.trade_log.len(), 1);
    assert_eq!(
        result.trade_log[0].price_minor, 2_500,
        "bought at the SPLIT-ADJUSTED close (10000 / 4), not the raw 10000"
    );
}

// --------------------------------------------------------------------------- //
// Fail-closed boundaries
// --------------------------------------------------------------------------- //

#[test]
fn split_adjusted_over_uncovered_store_fails_closed_naming_011() {
    // A bar but NO coverage record: the gated read refuses rather than returning raw bars dressed up as
    // adjusted. The engine surfaces it as SourceUnavailable, whose reason names SRS-DATA-011.
    let store = store_of([daily_bar("AAPL", 100, 10_000, 100_000)]);
    let source = StoreBarSource::daily(&store, Normalization::SplitAdjusted);

    let err = source
        .bars("AAPL", &DateRange::new(0, 100), 100)
        .expect_err("uncovered split-adjusted must fail closed");
    match err {
        BacktestError::SourceUnavailable { reason } => {
            assert!(
                reason.contains("SRS-DATA-011"),
                "the refusal must name the coverage requirement: {reason}"
            );
        }
        other => panic!("expected SourceUnavailable, got {other:?}"),
    }

    // And it must fail closed through the engine too (never a silent raw fallback).
    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome =
        BacktestEngine::new().run(&request("AAPL", DateRange::new(0, 100)), &mut strategy, &source);
    assert!(matches!(outcome, Err(BacktestError::SourceUnavailable { .. })));
}

#[test]
fn source_bounds_its_own_read_before_allocating() {
    // The BarSource contract: a response exceeding max_bars fails closed inside bars(), not by
    // materializing an unbounded Vec.
    let store = store_of([
        daily_bar("AAPL", 1, 100, 1_000),
        daily_bar("AAPL", 2, 110, 1_000),
        daily_bar("AAPL", 3, 120, 1_000),
    ]);
    let source = StoreBarSource::daily(&store, Normalization::Raw);
    let err = source
        .bars("AAPL", &DateRange::new(0, 1_000), 1)
        .expect_err("3 bars over a 1-bar cap must fail closed");
    assert_eq!(err, BacktestError::TooManyBars { count: 3, limit: 1 });
}

#[test]
fn window_bound_above_i64_max_fails_closed() {
    // The backtest window is u64; the query is i64. A bound above i64::MAX is unrepresentable and fails
    // closed rather than wrapping to a negative timestamp that would silently empty the query.
    let store = store_of([daily_bar("AAPL", 1, 100, 1_000)]);
    let source = StoreBarSource::daily(&store, Normalization::Raw);
    let err = source
        .bars("AAPL", &DateRange::new(0, u64::MAX), 100)
        .expect_err("an unrepresentable window bound must fail closed");
    match err {
        BacktestError::SourceUnavailable { reason } => {
            assert!(reason.contains("queryable timestamp range"), "reason: {reason}");
        }
        other => panic!("expected SourceUnavailable, got {other:?}"),
    }
}

#[test]
fn empty_window_yields_no_bars_and_the_engine_reports_empty_data() {
    // A valid-but-empty result is a normal value from the source (no records in range); the engine
    // itself reports EmptyData. This distinguishes "no data" from the SourceUnavailable refusals above.
    let store = store_of([daily_bar("AAPL", 500, 100, 1_000)]);
    let source = StoreBarSource::daily(&store, Normalization::Raw);
    assert!(source
        .bars("AAPL", &DateRange::new(0, 100), 100)
        .expect("empty range is a value, not an error")
        .is_empty());

    let mut strategy = BuyOnceAndHold {
        lot: 1,
        bought: false,
    };
    let outcome =
        BacktestEngine::new().run(&request("AAPL", DateRange::new(0, 100)), &mut strategy, &source);
    assert_eq!(outcome, Err(BacktestError::EmptyData));
}
