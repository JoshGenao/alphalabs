//! SRS-DATA-005 — Sharadar fundamental ingestion is *available to the factor pipeline*.
//!
//! This is the end-to-end proof of the SRS-DATA-005 ingestion path across the two dependency-legal
//! halves that meet at the vendor-neutral [`atp_types::FundamentalStatements`] boundary DTO:
//!
//!   1. **build + ingest (atp-data):** a `FundamentalStatements` bundle →
//!      [`atp_data::fundamentals::build_fundamental_records`] (the four canonical statement records:
//!      income / balance / cashflow / ratios) → `DataLayer::ingest_market_record` (the ERR-5
//!      validation gate + the idempotent `upsert`) → durable persist.
//!   2. **read (atp-factor-pipeline):** the persisted `fundamental:ratios` record is read back through
//!      the REAL factor loader [`load_fundamental_input`] — by symbol / resolution, NO provider named
//!      — yielding the dimensionless `FundamentalFactorInput` (earnings_yield, book_to_price) the
//!      factor job scores. This is the AC's load-bearing clause: the ingested fundamentals are
//!      genuinely consumable by the factor pipeline.
//!
//! It lives in `atp-factor-pipeline` because that is the one crate allowed to depend on BOTH `atp-data`
//! (to ingest) and the loader (to read) — `atp-data` may not depend on the factor pipeline
//! (SRS-ARCH-002 one-way direction). The test also re-reads through the loader's `fundamental:ratios`
//! resolution, so any drift between the data-layer builder's resolution literal and the loader's const
//! fails this test.
//!
//! Stays passes:false: the REAL Sharadar network adapter and the NFR-P8d overnight-window wall-clock
//! completion proof over the full US-equity universe are deferred; fixture sources + a deterministic
//! window stand in, exactly as the verification step permits.

use std::fs;
use std::path::PathBuf;

use atp_data::fundamentals::build_fundamental_records;
use atp_data::store::{DatasetKind, MarketDataStore, StoreError, UpsertOutcome};
use atp_data::{DataLayer, IngestionValidationEventSink, MarketIngestError, RecordValidator};
use atp_factor_pipeline::store_inputs::load_fundamental_input;
use atp_types::{
    AssetClass, FundamentalStatements, IngestionRecordSubmission, IngestionValidationEvent,
    RecordValidationOutcome, SecurityKey,
};

// ---- ingestion stubs (the concrete SYS-77 validator + alert sink are deferred SRS-DATA-013) -------

struct AcceptAll;
impl RecordValidator for AcceptAll {
    fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

struct NullSink;
impl IngestionValidationEventSink for NullSink {
    fn record(&self, _event: IngestionValidationEvent) {}
}

const PERIOD_END_TS: i64 = 1_700_000_000;
const AVAILABLE_TS: i64 = 1_700_000_000 + 45 * 86_400; // filed 45 days after the period closed
const AS_OF_AFTER_FILING: i64 = 1_704_000_000;

fn temp_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_data005_it_{label}"));
    let _ = fs::remove_dir_all(&dir);
    dir
}

fn equity(symbol: &str) -> SecurityKey {
    SecurityKey::new(symbol, AssetClass::Equity).expect("equity security key")
}

/// A bundle whose derived key ratios are exact: earnings_yield = net_income / market_value,
/// book_to_price = book_equity / market_value.
#[allow(clippy::too_many_arguments)]
fn statements(
    symbol: &str,
    net_income_minor: i64,
    book_equity_minor: i64,
    market_value_minor: i64,
) -> FundamentalStatements {
    FundamentalStatements::new(
        symbol,
        PERIOD_END_TS,
        AVAILABLE_TS,
        100_000_000, // revenue (not read by the ratios loader)
        net_income_minor,
        80_000_000, // total assets
        30_000_000, // total liabilities
        book_equity_minor,
        30_000_000,  // operating cash flow
        -10_000_000, // investing cash flow
        -5_000_000,  // financing cash flow
        market_value_minor,
    )
    .expect("well-formed statements")
}

/// Build the four records for a bundle and ingest each through the idempotent market-record path,
/// returning (inserted, duplicates).
fn ingest(store: &mut MarketDataStore, bundle: &FundamentalStatements) -> (usize, usize) {
    let layer = DataLayer;
    let records = build_fundamental_records(bundle).expect("records build");
    let mut inserted = 0;
    let mut duplicates = 0;
    for record in records {
        let outcome = layer
            .ingest_market_record(store, record, &AcceptAll, &NullSink, AVAILABLE_TS as u64)
            .expect("ingest succeeds");
        match outcome.applied {
            UpsertOutcome::Inserted => inserted += 1,
            UpsertOutcome::UnchangedDuplicate => duplicates += 1,
        }
    }
    (inserted, duplicates)
}

fn approx(value: f64, expected: f64) {
    assert!(
        (value - expected).abs() < 1e-12,
        "expected ~{expected}, got {value}"
    );
}

/// The full path: build → ingest → persist → reload → re-ingest (idempotent) → read via the loader.
#[test]
fn srs_data_005_fundamentals_ingested_and_read_by_factor_loader() {
    let dir = temp_dir("available_to_factor");
    let bundle = statements("AAPL", 25_000_000, 50_000_000, 250_000_000);

    // First ingest: four records (income / balance / cashflow / ratios), all new.
    let mut store = MarketDataStore::new();
    let (inserted, duplicates) = ingest(&mut store, &bundle);
    assert_eq!(inserted, 4, "four statement records ingested");
    assert_eq!(duplicates, 0);
    assert_eq!(store.count_for_kind(DatasetKind::Fundamental), 4);
    store.save_to_path(&dir).expect("persist");
    let bytes_after_first = fs::read(dir.join(atp_data::store::STORE_FILENAME)).unwrap();

    // Re-ingest the SAME bundle from a freshly loaded store: an idempotent no-op (SRS-DATA-016
    // substrate), no duplicate rows, byte-identical file.
    let mut reloaded = MarketDataStore::load_from_path(&dir).expect("reload");
    let (reinserted, redups) = ingest(&mut reloaded, &bundle);
    assert_eq!(reinserted, 0, "re-ingest inserts nothing");
    assert_eq!(redups, 4, "every re-ingested record is a no-op");
    assert_eq!(reloaded.count_for_kind(DatasetKind::Fundamental), 4);
    reloaded.save_to_path(&dir).expect("persist again");
    assert_eq!(
        fs::read(dir.join(atp_data::store::STORE_FILENAME)).unwrap(),
        bytes_after_first,
        "re-ingest left the persisted file byte-identical"
    );

    // The AC clause: the ratios record is AVAILABLE TO THE FACTOR PIPELINE — read it back through the
    // real loader and derive the dimensionless factor inputs.
    let input = load_fundamental_input(&reloaded, &equity("AAPL"), AS_OF_AFTER_FILING)
        .expect("loader read succeeds")
        .expect("a statement is available as of the run date");
    approx(input.earnings_yield, 0.1); // 25M / 250M
    approx(input.book_to_price, 0.2); // 50M / 250M

    let _ = fs::remove_dir_all(&dir);
}

/// Point-in-time correctness: a statement filed AFTER the run date is not knowable then, so the loader
/// returns an auditable absence (the factor job records a MissingFundamentalData skip) — no lookahead.
#[test]
fn srs_data_005_point_in_time_skip_before_filing() {
    let dir = temp_dir("point_in_time");
    let bundle = statements("AAPL", 25_000_000, 50_000_000, 250_000_000);
    let mut store = MarketDataStore::new();
    ingest(&mut store, &bundle);
    store.save_to_path(&dir).expect("persist");
    let store = MarketDataStore::load_from_path(&dir).expect("reload");

    // As-of the period end (BEFORE the 45-day-later filing): the statement is not yet available.
    let before = load_fundamental_input(&store, &equity("AAPL"), PERIOD_END_TS).expect("read ok");
    assert!(before.is_none(), "a not-yet-filed statement is skipped");

    // As-of after the filing: now available.
    let after =
        load_fundamental_input(&store, &equity("AAPL"), AS_OF_AFTER_FILING).expect("read ok");
    assert!(after.is_some(), "an available statement is read");

    let _ = fs::remove_dir_all(&dir);
}

/// REGRESSION PIN (known limitation): the fundamental natural key is
/// `(Fundamental, symbol, resolution, period_end_ts)` with `available_ts` as a value FIELD, so it
/// cannot yet represent two filings for the SAME fiscal period (an original + a later restatement /
/// amended 10-K with a different filing date and revised numbers). Re-ingesting a restatement for an
/// already-ingested period is a same-key / different-content write, which the SRS-DATA-016 idempotency
/// core fails CLOSED on (`StoreError::ConflictingContent`) — so a restatement is REJECTED, not silently
/// corrupted and not a lookahead leak, but also not yet cataloged as point-in-time history.
///
/// This pins the conservative current behavior. Representing multiple filings per period (a
/// filing-version dimension in the fundamental identity + a loader that picks the latest filing
/// available as-of the run date) is a STORAGE-SCHEMA change deferred with the real Sharadar
/// restatement feed (see the `fundamental_ingestion_contract` deferred owners). When that lands, this
/// test changes from "rejects" to "both filings coexist point-in-time".
#[test]
fn srs_data_005_restatement_currently_fails_closed_pending_filing_version_keying() {
    let layer = DataLayer;
    let mut store = MarketDataStore::new();

    // Original filing for the period.
    let original = statements("AAPL", 25_000_000, 50_000_000, 250_000_000);
    let (inserted, _) = ingest(&mut store, &original);
    assert_eq!(inserted, 4);

    // A restatement: SAME symbol + period end, but filed later with revised net income. Same natural
    // key, different content -> the first conflicting record fails closed (no corruption).
    let restatement = FundamentalStatements::new(
        "AAPL",
        PERIOD_END_TS,
        AVAILABLE_TS + 30 * 86_400, // amended filing, 30 days after the original
        100_000_000,
        30_000_000, // revised net income (was 25M)
        80_000_000,
        30_000_000,
        50_000_000,
        30_000_000,
        -10_000_000,
        -5_000_000,
        250_000_000,
    )
    .expect("the restatement bundle is itself well-formed");
    let records = build_fundamental_records(&restatement).expect("records build");

    let mut conflicted = false;
    for record in records {
        match layer.ingest_market_record(&mut store, record, &AcceptAll, &NullSink, 0) {
            Ok(_) => {}
            Err(MarketIngestError::Store(StoreError::ConflictingContent { .. })) => {
                conflicted = true;
                break;
            }
            Err(other) => panic!("unexpected ingest error: {other:?}"),
        }
    }
    assert!(
        conflicted,
        "a same-period restatement currently fails closed (ConflictingContent) -- the known \
         limitation pinned by this test until filing-version keying lands"
    );
    // No corruption: the store still holds exactly the original four records.
    assert_eq!(store.count_for_kind(DatasetKind::Fundamental), 4);
}

/// Negatives are legitimate (a loss-making, negative-book-value security): the loader does not fail
/// closed on a negative numerator — only a non-positive market value (the denominator) is rejected.
#[test]
fn srs_data_005_negative_fundamentals_are_not_fabricated() {
    let dir = temp_dir("negatives");
    let bundle = statements("LOSS", -5_000_000, -1_000_000, 100_000_000);
    let mut store = MarketDataStore::new();
    ingest(&mut store, &bundle);
    store.save_to_path(&dir).expect("persist");
    let store = MarketDataStore::load_from_path(&dir).expect("reload");

    let input = load_fundamental_input(&store, &equity("LOSS"), AS_OF_AFTER_FILING)
        .expect("read ok")
        .expect("available");
    approx(input.earnings_yield, -0.05); // -5M / 100M
    approx(input.book_to_price, -0.01); // -1M / 100M

    let _ = fs::remove_dir_all(&dir);
}
