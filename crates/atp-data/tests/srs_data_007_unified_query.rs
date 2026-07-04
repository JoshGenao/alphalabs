//! SRS-DATA-007 (unified historical data access interface) — L5 integration tests.
//!
//! Acceptance: "Strategy code, backtests, factor jobs, and notebooks query by symbol, date range, and
//! resolution **without specifying the original source provider**." These tests drive the public
//! `atp-data` query path (`DataLayer::ingest_market_record` to build a real catalog from the
//! deterministic fixture sources that stand in for the four provider adapters, then
//! `MarketDataStore::query_unified` to read it back), and prove the query over a store persisted to and
//! reloaded from disk — exactly the runnable end-to-end surface the feature step permits ("CLI/API
//! workflows with fixture market data ... and persisted output inspection").

use std::fs;
use std::path::PathBuf;

use atp_data::store::{
    fixture_batch, DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
    UpsertOutcome,
};
use atp_data::{
    DataLayer, IngestionValidationEventSink, MarketIngestError, RecordValidator,
    UnifiedHistoricalQuery,
};
use atp_types::{IngestionValidationEvent, RecordValidationOutcome};

// --------------------------------------------------------------------------- //
// Test doubles + helpers (mirror the SRS-DATA-016 integration harness).
// --------------------------------------------------------------------------- //

struct AcceptAll;
impl RecordValidator for AcceptAll {
    fn validate(&self, _record: &MarketDataRecord) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

struct NullSink;
impl IngestionValidationEventSink for NullSink {
    fn record(&self, _event: IngestionValidationEvent) {}
}

/// Ingest a fixture batch for `kind` on `event_ts` through the real validating write path.
fn ingest_batch(
    store: &mut MarketDataStore,
    kind: DatasetKind,
    event_ts: i64,
) -> Result<(), MarketIngestError> {
    let layer = DataLayer;
    let (validator, sink) = (AcceptAll, NullSink);
    for record in fixture_batch(kind, event_ts) {
        let outcome =
            layer.ingest_market_record(store, record, &validator, &sink, 1_700_000_000)?;
        // A fresh fixture batch inserts; a repeated one is the idempotent no-op (SRS-DATA-016).
        let _ = matches!(
            outcome.applied,
            UpsertOutcome::Inserted | UpsertOutcome::UnchangedDuplicate
        );
    }
    Ok(())
}

fn daily(symbol: &str, event_ts: i64, close: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [
            MarketField {
                name: "close".to_string(),
                value_minor: close,
            },
            MarketField {
                name: "open".to_string(),
                value_minor: close - 10,
            },
        ],
    )
    .expect("well-formed daily record")
}

fn temp_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_data007_it_{label}"));
    let _ = fs::remove_dir_all(&dir);
    dir
}

fn event_ts_of(result: &atp_data::UnifiedHistoricalResult<'_>) -> Vec<i64> {
    result.records().iter().map(|r| r.key().event_ts).collect()
}

const T1: i64 = 1_700_000_000;
const T2: i64 = 1_700_086_400; // T1 + 1 day
const T3: i64 = 1_700_172_800; // T1 + 2 days

// --------------------------------------------------------------------------- //
// The acceptance: query by symbol + date range + resolution, source-neutrally.
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_007_filters_by_symbol_resolution_and_inclusive_range() {
    let mut store = MarketDataStore::new();
    for ts in [T1, T2, T3] {
        store.upsert(daily("AAPL", ts, 100)).unwrap();
    }
    // Inclusive [T1, T2] excludes T3; ascending order.
    let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", T1, T2));
    assert_eq!(event_ts_of(&result), vec![T1, T2]);

    // A window below / above every record yields an empty result (not an error).
    assert!(store
        .query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, T1 - 1))
        .is_empty());
    assert!(store
        .query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", T3 + 1, i64::MAX))
        .is_empty());
}

#[test]
fn srs_data_007_symbol_and_resolution_are_exact() {
    let mut store = MarketDataStore::new();
    // Two providers' worth of records under one symbol: daily (≤ Databento) and minute (≤ IB).
    ingest_batch(&mut store, DatasetKind::DailyEquityBar, T1).unwrap();
    ingest_batch(&mut store, DatasetKind::MinuteEquityBar, T1).unwrap();

    let daily_aapl = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, i64::MAX));
    assert_eq!(
        daily_aapl.len(),
        1,
        "only the AAPL daily bar — not MSFT, not the AAPL minute bar"
    );
    assert_eq!(daily_aapl.records()[0].key().symbol, "AAPL");
    assert_eq!(daily_aapl.records()[0].key().resolution, "1d");

    // The SAME query path, a different resolution → the IB-sourced minute bar, no provider named.
    let minute_aapl = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1m", 0, i64::MAX));
    assert_eq!(minute_aapl.len(), 1);
    assert_eq!(minute_aapl.records()[0].key().resolution, "1m");

    // A non-existent symbol is an empty value, never an error.
    let none = store.query_unified(&UnifiedHistoricalQuery::new("NOSUCH", "1d", 0, i64::MAX));
    assert!(none.is_empty());
}

#[test]
fn srs_data_007_one_path_serves_every_provider_kind() {
    // A multi-provider catalog (all four kinds the acceptance names) is served by exactly ONE query
    // method — there is no provider-specific branch, and the caller names no provider.
    let mut store = MarketDataStore::new();
    for kind in DatasetKind::all() {
        ingest_batch(&mut store, kind, T1).unwrap();
    }
    // DailyEquityBar ⇐ Databento.
    assert_eq!(
        store
            .query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, i64::MAX))
            .len(),
        1
    );
    // MinuteEquityBar ⇐ IB.
    assert_eq!(
        store
            .query_unified(&UnifiedHistoricalQuery::new("AAPL", "1m", 0, i64::MAX))
            .len(),
        1
    );
    // OptionChainSnapshot ⇐ IB option-chain (two contracts at the same event_ts under symbol AAPL).
    assert_eq!(
        store
            .query_unified(&UnifiedHistoricalQuery::new("AAPL", "chain", 0, i64::MAX))
            .len(),
        2
    );
    // Fundamental ⇐ Sharadar.
    assert_eq!(
        store
            .query_unified(&UnifiedHistoricalQuery::new(
                "AAPL",
                "fundamental:income",
                0,
                i64::MAX
            ))
            .len(),
        1
    );
}

#[test]
fn srs_data_007_kind_disambiguator_narrows() {
    let mut store = MarketDataStore::new();
    for kind in DatasetKind::all() {
        ingest_batch(&mut store, kind, T1).unwrap();
    }
    // Without a kind, an AAPL "chain" query returns the two option-chain records...
    let any = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "chain", 0, i64::MAX));
    assert_eq!(any.len(), 2);
    // ...and narrowing to OptionChainSnapshot returns the same set (the resolution already implies it),
    // while narrowing to a different kind returns nothing — the optional disambiguator is honored.
    let narrowed = store.query_unified(
        &UnifiedHistoricalQuery::new("AAPL", "chain", 0, i64::MAX)
            .with_kind(DatasetKind::OptionChainSnapshot),
    );
    assert_eq!(narrowed.len(), 2);
    assert!(store
        .query_unified(
            &UnifiedHistoricalQuery::new("AAPL", "chain", 0, i64::MAX)
                .with_kind(DatasetKind::DailyEquityBar)
        )
        .is_empty());
}

#[test]
fn srs_data_007_query_is_deterministic_across_persisted_reload() {
    // Build a catalog (insert order shuffled), persist it, reload it, and assert the query result is
    // byte-for-byte identical to the in-memory query — the runnable persisted-output proof.
    let dir = temp_dir("reload");
    let mut store = MarketDataStore::new();
    for ts in [T3, T1, T2] {
        store.upsert(daily("AAPL", ts, 100)).unwrap();
    }
    let q = UnifiedHistoricalQuery::new("AAPL", "1d", 0, i64::MAX);
    let in_memory = event_ts_of(&store.query_unified(&q));
    assert_eq!(
        in_memory,
        vec![T1, T2, T3],
        "ascending regardless of insert order"
    );

    store.save_to_path(&dir).unwrap();
    let reloaded = MarketDataStore::load_from_path(&dir).unwrap();
    let after_reload = event_ts_of(&reloaded.query_unified(&q));
    assert_eq!(
        after_reload, in_memory,
        "the persisted-then-reloaded query is identical"
    );

    // Two successive in-process queries are identical (no clock/RNG ordering drift).
    assert_eq!(event_ts_of(&reloaded.query_unified(&q)), after_reload);
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_data_007_cross_kind_results_are_event_ts_ascending() {
    // Regression: the store's canonical order sorts by kind BEFORE event_ts, so a kind-agnostic
    // match spanning two kinds that share symbol+resolution must still be returned event_ts-ascending
    // (the date-range query contract), not in store (kind-first) order.
    let shared = |kind: DatasetKind, event_ts: i64| {
        MarketDataRecord::new(
            NaturalKey {
                kind,
                symbol: "AAPL".to_string(),
                resolution: "blend".to_string(),
                event_ts,
                option_contract: None,
            },
            [MarketField {
                name: "close".to_string(),
                value_minor: 1,
            }],
        )
        .expect("well-formed shared record")
    };
    let mut store = MarketDataStore::new();
    store
        .upsert(shared(DatasetKind::DailyEquityBar, T3))
        .unwrap(); // kind tag 0, latest ts
    store
        .upsert(shared(DatasetKind::MinuteEquityBar, T1))
        .unwrap(); // kind tag 1, earliest ts
    let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "blend", 0, i64::MAX));
    assert_eq!(
        event_ts_of(&result),
        vec![T1, T3],
        "event_ts-ascending across kinds, not store (kind-first) order"
    );
}

#[test]
fn srs_data_007_result_carries_no_origin_metadata() {
    // Source-neutrality, structurally: the result exposes only the queried symbol/resolution and the
    // matched records; a matched record's key exposes only the vendor-NEUTRAL DatasetKind, never a
    // provider string. (The static check enforces the absence of a provider field; here we assert the
    // result round-trips a record whose only "origin" surface is the neutral kind taxonomy.)
    let mut store = MarketDataStore::new();
    ingest_batch(&mut store, DatasetKind::DailyEquityBar, T1).unwrap();
    let result = store.query_unified(&UnifiedHistoricalQuery::new("AAPL", "1d", 0, i64::MAX));
    assert_eq!(result.symbol, "AAPL");
    assert_eq!(result.resolution, "1d");
    assert_eq!(result.records()[0].key().kind, DatasetKind::DailyEquityBar);
}
