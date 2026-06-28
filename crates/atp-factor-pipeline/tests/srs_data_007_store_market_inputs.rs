//! SRS-DATA-007 close — the factor job sources its market inputs from the unified historical interface.
//!
//! The acceptance criterion names "strategy code, backtests, **factor jobs**, and notebooks" as the
//! consumers that "query by symbol, date range, and resolution **without specifying the original source
//! provider**." This crate-level integration test wires the factor pipeline's market-input loader
//! ([`load_daily_market_input`]) — shipped product code — to a durable [`MarketDataStore`] and asserts
//! it reads a security's daily close series through the source-neutral SRS-DATA-007 query path, on both
//! the raw and the coverage-gated split-adjusted bases, and fails closed at every trust boundary.
//!
//! Source-neutrality is structural: the loader is called with `(store, security, window, basis)` — there
//! is no provider / vendor / source parameter, and a record carries no origin field.

use atp_data::store::{
    coverage_record, DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
};
use atp_factor_pipeline::store_inputs::{
    load_daily_market_input, FactorInputError, MarketInputBasis,
};
use atp_types::{AssetClass, SecurityKey};

fn field(name: &str, value_minor: i64) -> MarketField {
    MarketField {
        name: name.to_string(),
        value_minor,
    }
}

fn daily_bar(symbol: &str, event_ts: i64, fields: Vec<MarketField>) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        fields,
    )
    .expect("well-formed daily bar")
}

fn close_bar(symbol: &str, event_ts: i64, close: i64, volume: i64) -> MarketDataRecord {
    daily_bar(
        symbol,
        event_ts,
        vec![field("close", close), field("volume", volume)],
    )
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
        [
            field("denominator", denominator),
            field("numerator", numerator),
        ],
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

fn equity(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("equity key")
}

#[test]
fn factor_sources_market_input_from_the_unified_store() {
    // Closes 100, 120, 120 ($ in minor units): trailing (12000-10000)/10000 = 0.2; per-bar returns
    // [0.2, 0.0], population std = 0.1. Queried by symbol / range / resolution, no provider named.
    let store = store_of([
        close_bar("AAPL", 1, 10_000, 1_000),
        close_bar("AAPL", 2, 12_000, 1_000),
        close_bar("AAPL", 3, 12_000, 1_000),
        // A foreign symbol must never leak into AAPL's factor input.
        close_bar("MSFT", 2, 99_999, 1_000),
    ]);
    let input = load_daily_market_input(&store, &equity("AAPL"), 0, 10, MarketInputBasis::Raw)
        .expect("loader reads the store")
        .expect("two+ closes yield a market input");
    assert!((input.trailing_return - 0.2).abs() < 1e-12);
    assert!((input.realized_volatility - 0.1).abs() < 1e-12);
}

#[test]
fn insufficient_history_is_an_auditable_absence() {
    let store = store_of([close_bar("AAPL", 1, 10_000, 1_000)]);
    let input = load_daily_market_input(&store, &equity("AAPL"), 0, 10, MarketInputBasis::Raw)
        .expect("a single-bar window is a value, not an error");
    assert_eq!(
        input, None,
        "fewer than two closes -> skip, never a fabricated factor"
    );
}

#[test]
fn split_adjusted_basis_differs_from_raw_across_a_split() {
    // AAPL @100 close 10000, @300 close 3000, a 4-for-1 split @200, coverage through 300.
    //   raw closes [10000, 3000] -> trailing (3000-10000)/10000 = -0.7 (the artificial split jump).
    //   split-adjusted: @100 re-quoted to 10000/4 = 2500, @300 unchanged 3000 -> trailing 0.2.
    // Proves the loader genuinely reads the ADJUSTED basis through the gated path, not raw.
    let store = store_of([
        close_bar("AAPL", 100, 10_000, 100_000),
        close_bar("AAPL", 300, 3_000, 100_000),
        split("AAPL", 200, 4, 1),
        coverage_record(300, "AAPL"),
    ]);
    let security = equity("AAPL");

    let raw = load_daily_market_input(&store, &security, 0, 300, MarketInputBasis::Raw)
        .expect("raw read")
        .expect("two closes");
    assert!(
        (raw.trailing_return - (-0.7)).abs() < 1e-12,
        "raw spans the split jump"
    );

    let adjusted =
        load_daily_market_input(&store, &security, 0, 300, MarketInputBasis::SplitAdjusted)
            .expect("covered split-adjusted read")
            .expect("two closes");
    assert!(
        (adjusted.trailing_return - 0.2).abs() < 1e-12,
        "split-adjusted re-quotes the pre-split bar onto a comparable basis"
    );
}

#[test]
fn split_effective_after_the_as_of_date_is_not_applied_no_lookahead() {
    // Point-in-time correctness: a split effective AFTER the query window end (the as-of date) -- even
    // one within proven coverage -- must NOT be applied, or a future corporate action would re-base the
    // historical window (lookahead bias). Bars @100 close 10000, @300 close 3000; a 4-for-1 split
    // effective @400 (AFTER the as-of date 300); coverage through 500. As of 300 the split is in the
    // future, so the split-adjusted factor input must equal the raw one.
    let store = store_of([
        close_bar("AAPL", 100, 10_000, 100_000),
        close_bar("AAPL", 300, 3_000, 100_000),
        split("AAPL", 400, 4, 1),
        coverage_record(500, "AAPL"),
    ]);
    let security = equity("AAPL");

    let raw = load_daily_market_input(&store, &security, 0, 300, MarketInputBasis::Raw)
        .expect("raw read")
        .expect("two closes");
    let adjusted =
        load_daily_market_input(&store, &security, 0, 300, MarketInputBasis::SplitAdjusted)
            .expect("covered split-adjusted read")
            .expect("two closes");
    assert!(
        (adjusted.trailing_return - raw.trailing_return).abs() < 1e-12,
        "a split effective after the as-of date must not change the factor input (no lookahead)"
    );
}

#[test]
fn split_adjusted_over_uncovered_store_fails_closed_naming_011() {
    // Bars but NO coverage record: the gated read refuses rather than deriving a factor from a raw
    // series mislabeled as adjusted.
    let store = store_of([
        close_bar("AAPL", 100, 10_000, 100_000),
        close_bar("AAPL", 300, 3_000, 100_000),
    ]);
    let err = load_daily_market_input(
        &store,
        &equity("AAPL"),
        0,
        300,
        MarketInputBasis::SplitAdjusted,
    )
    .expect_err("uncovered split-adjusted must fail closed");
    match err {
        FactorInputError::CoverageNotProven { reason, .. } => {
            assert!(
                reason.contains("SRS-DATA-011"),
                "reason must name coverage: {reason}"
            );
        }
        other => panic!("expected CoverageNotProven, got {other:?}"),
    }
}

#[test]
fn inverted_window_fails_closed() {
    // A bad lookback / range (start_ts > end_ts) must fail closed BEFORE querying, NOT fall through to
    // Ok(None) that a caller would misread as "no market data" and silently drop the security from a run.
    let store = store_of([
        close_bar("AAPL", 100, 10_000, 1_000),
        close_bar("AAPL", 200, 12_000, 1_000),
    ]);
    let err = load_daily_market_input(&store, &equity("AAPL"), 300, 100, MarketInputBasis::Raw)
        .expect_err("inverted window must fail closed");
    assert_eq!(
        err,
        FactorInputError::InvalidWindow {
            start_ts: 300,
            end_ts: 100,
        }
    );
}

#[test]
fn bar_missing_its_close_field_fails_closed() {
    // A daily bar carrying only volume (no close): the loader fails closed rather than fabricate a price.
    let store = store_of([daily_bar("AAPL", 1, vec![field("volume", 1_000)])]);
    let err = load_daily_market_input(&store, &equity("AAPL"), 0, 10, MarketInputBasis::Raw)
        .expect_err("a bar with no close must fail closed");
    assert_eq!(
        err,
        FactorInputError::MissingClose {
            symbol: "AAPL".to_string(),
            event_ts: 1,
        }
    );
}
