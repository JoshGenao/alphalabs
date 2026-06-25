//! SRS-DATA-016 (make ingestion jobs idempotent) — L5 integration tests.
//!
//! Acceptance: "Re-running Databento, IB, option-chain, or Sharadar ingestion for an already
//! ingested date creates no duplicate records and does not corrupt existing data." These tests
//! drive the public `atp-data` API (`DataLayer::ingest_market_record` + `MarketDataStore`) with the
//! deterministic fixture sources that stand in for the four provider adapters, and inspect the
//! persisted file bytes — exactly the verification surface the feature step permits.

use std::fs;
use std::path::{Path, PathBuf};

use atp_data::store::{
    fixture_batch, DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
    StoreError, StoreLock, UpsertOutcome, STORE_FILENAME,
};
use atp_data::{DataLayer, IngestionValidationEventSink, MarketIngestError, RecordValidator};
use atp_types::{
    IngestionRecordSubmission, IngestionValidationEvent, QuarantineReason, RecordValidationOutcome,
};

// --------------------------------------------------------------------------- //
// Test doubles: the DATA-013 validator + event sink (deferred) stand-ins.
// --------------------------------------------------------------------------- //

struct AcceptAll;
impl RecordValidator for AcceptAll {
    fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

struct QuarantineAll;
impl RecordValidator for QuarantineAll {
    fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
        RecordValidationOutcome::Quarantined(QuarantineReason::RangeViolation)
    }
}

struct NullSink;
impl IngestionValidationEventSink for NullSink {
    fn record(&self, _event: IngestionValidationEvent) {}
}

/// Ingest a fixture batch for `kind` on `event_ts`, returning (inserted, duplicates_skipped).
fn ingest_batch(
    store: &mut MarketDataStore,
    kind: DatasetKind,
    event_ts: i64,
) -> Result<(usize, usize), MarketIngestError> {
    let layer = DataLayer;
    let (validator, sink) = (AcceptAll, NullSink);
    let mut inserted = 0;
    let mut duplicates = 0;
    for record in fixture_batch(kind, event_ts) {
        let outcome = layer.ingest_market_record(store, record, &validator, &sink, 1_700_000_000)?;
        match outcome.applied {
            UpsertOutcome::Inserted => inserted += 1,
            UpsertOutcome::UnchangedDuplicate => duplicates += 1,
        }
    }
    Ok((inserted, duplicates))
}

fn temp_dir(label: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("atp_data016_it_{label}"));
    let _ = fs::remove_dir_all(&dir);
    dir
}

fn store_bytes(dir: &Path) -> Vec<u8> {
    fs::read(dir.join(STORE_FILENAME)).expect("persisted store file exists")
}

const EVENT_TS: i64 = 1_700_000_000;

// --------------------------------------------------------------------------- //
// The acceptance: re-ingest is a no-op, no duplicate, no corruption — per kind.
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_016_reingest_each_kind_creates_no_duplicate_and_no_corruption() {
    for kind in DatasetKind::provider_ingestion_kinds() {
        let dir = temp_dir(&format!("reingest_{}", kind.as_str()));

        // First ingest: every record is new.
        let mut store = MarketDataStore::new();
        let (inserted, dups) = ingest_batch(&mut store, kind, EVENT_TS).unwrap();
        assert!(inserted > 0, "{}: first ingest inserts records", kind.as_str());
        assert_eq!(dups, 0, "{}: first ingest has no duplicates", kind.as_str());
        store.save_to_path(&dir).unwrap();
        let len_after_first = store.len();
        let bytes_after_first = store_bytes(&dir);

        // Re-ingest the SAME date from a freshly loaded store: every record is an idempotent no-op.
        let mut reloaded = MarketDataStore::load_from_path(&dir).unwrap();
        let (reinserted, redups) = ingest_batch(&mut reloaded, kind, EVENT_TS).unwrap();
        assert_eq!(reinserted, 0, "{}: re-ingest inserts nothing", kind.as_str());
        assert_eq!(redups, inserted, "{}: every re-ingested record is a no-op", kind.as_str());
        assert_eq!(reloaded.len(), len_after_first, "{}: no duplicate rows", kind.as_str());
        reloaded.save_to_path(&dir).unwrap();

        // No corruption: the persisted file is byte-for-byte identical.
        assert_eq!(
            store_bytes(&dir),
            bytes_after_first,
            "{}: re-ingest left the persisted file byte-identical",
            kind.as_str()
        );

        let _ = fs::remove_dir_all(&dir);
    }
}

#[test]
fn srs_data_016_reingest_all_kinds_together_is_stable() {
    // Ingest every provider kind, then re-ingest them: the combined catalog is unchanged.
    let dir = temp_dir("all_kinds");
    let mut store = MarketDataStore::new();
    for kind in DatasetKind::provider_ingestion_kinds() {
        ingest_batch(&mut store, kind, EVENT_TS).unwrap();
    }
    store.save_to_path(&dir).unwrap();
    let len = store.len();
    let bytes = store_bytes(&dir);

    let mut reloaded = MarketDataStore::load_from_path(&dir).unwrap();
    let mut total_reinserted = 0;
    for kind in DatasetKind::provider_ingestion_kinds() {
        let (reinserted, _) = ingest_batch(&mut reloaded, kind, EVENT_TS).unwrap();
        total_reinserted += reinserted;
    }
    reloaded.save_to_path(&dir).unwrap();

    assert_eq!(total_reinserted, 0, "no kind re-inserts on the second pass");
    assert_eq!(reloaded.len(), len, "combined catalog has no duplicates");
    assert_eq!(store_bytes(&dir), bytes, "combined persisted file is byte-identical");
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_data_016_generic_ingestion_refuses_corporate_action_coverage() {
    // The generic market-data ingestion API does NOT mint corporate-action COVERAGE: a coverage
    // frontier is an operator trust assertion (data011_coverage_cli), not provider market data. So a
    // generic ingest of a coverage record fails closed (UnsupportedKind) and the store stays empty —
    // no generic flow can grant the split-adjusted gate a trusted frontier. (coverage_record is built
    // directly here to prove the data-layer boundary, not the CLI parser.)
    let layer = DataLayer;
    let mut store = MarketDataStore::new();
    let coverage = atp_data::store::coverage_record(200, "AAPL");
    let result = layer.ingest_market_record(&mut store, coverage, &AcceptAll, &NullSink, EVENT_TS as u64);
    assert!(
        matches!(result, Err(MarketIngestError::UnsupportedKind { .. })),
        "generic ingestion must refuse a coverage record, got {result:?}"
    );
    assert!(store.is_empty(), "the refused coverage record must not enter the store");
    // The provider fixture generator also emits none for coverage (defence in depth).
    assert!(fixture_batch(DatasetKind::CorporateActionCoverage, EVENT_TS).is_empty());
}

// --------------------------------------------------------------------------- //
// No corruption: a conflicting re-ingest fails closed, leaving existing data intact.
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_016_conflicting_reingest_fails_closed_without_corrupting() {
    let dir = temp_dir("conflict");
    let mut store = MarketDataStore::new();
    ingest_batch(&mut store, DatasetKind::DailyEquityBar, EVENT_TS).unwrap();
    store.save_to_path(&dir).unwrap();
    let bytes_before = store_bytes(&dir);
    let len_before = store.len();

    // Build a record with the SAME natural key as a fixture record but DIFFERENT content.
    let key = NaturalKey {
        kind: DatasetKind::DailyEquityBar,
        symbol: "AAPL".to_string(),
        resolution: "1d".to_string(),
        event_ts: EVENT_TS,
        option_contract: None,
    };
    let conflicting = MarketDataRecord::new(
        key,
        [MarketField { name: "close".to_string(), value_minor: 1 }],
    )
    .unwrap();

    let layer = DataLayer;
    let err = layer
        .ingest_market_record(&mut store, conflicting, &AcceptAll, &NullSink, EVENT_TS as u64)
        .expect_err("a conflicting re-ingest must fail closed");
    assert!(matches!(err, MarketIngestError::Store(StoreError::ConflictingContent { .. })));

    // The in-memory store is untouched; persisting it again leaves the file byte-identical.
    assert_eq!(store.len(), len_before, "no record added on conflict");
    store.save_to_path(&dir).unwrap();
    assert_eq!(store_bytes(&dir), bytes_before, "existing data left intact");
    let _ = fs::remove_dir_all(&dir);
}

// --------------------------------------------------------------------------- //
// The ERR-5 validation gate is composed: a quarantined record never reaches the store.
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_016_quarantined_record_is_not_written_to_the_store() {
    let mut store = MarketDataStore::new();
    let layer = DataLayer;
    let record = fixture_batch(DatasetKind::DailyEquityBar, EVENT_TS)
        .into_iter()
        .next()
        .unwrap();
    let err = layer
        .ingest_market_record(&mut store, record, &QuarantineAll, &NullSink, EVENT_TS as u64)
        .expect_err("a quarantined record must be rejected");
    assert!(matches!(err, MarketIngestError::Rejected(_)));
    assert!(store.is_empty(), "a quarantined record never reaches the store");
}

// --------------------------------------------------------------------------- //
// Validation is bound to the persisted record: the ERR-5 gate validates exactly the record
// that will be written (the envelope is derived from it, not supplied independently).
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_016_validation_is_bound_to_the_persisted_record() {
    use std::cell::RefCell;

    // A validator that captures every envelope it is asked to validate.
    struct Capturing {
        seen: RefCell<Vec<IngestionRecordSubmission>>,
    }
    impl RecordValidator for Capturing {
        fn validate(&self, record: &IngestionRecordSubmission) -> RecordValidationOutcome {
            self.seen.borrow_mut().push(record.clone());
            RecordValidationOutcome::Valid
        }
    }

    let mut store = MarketDataStore::new();
    let layer = DataLayer;
    let validator = Capturing {
        seen: RefCell::new(Vec::new()),
    };
    let record = fixture_batch(DatasetKind::DailyEquityBar, EVENT_TS)
        .into_iter()
        .next()
        .unwrap();
    // The envelope the gate validates must be the one DERIVED from this exact record, so a caller
    // cannot validate one payload and persist another.
    let expected = record.ingestion_submission();
    layer
        .ingest_market_record(&mut store, record, &validator, &NullSink, EVENT_TS as u64)
        .expect("a valid record is ingested");

    let seen = validator.seen.borrow();
    assert_eq!(seen.len(), 1, "the gate validates exactly once");
    assert_eq!(seen[0], expected, "validation is bound to the persisted record's derived envelope");
    assert_eq!(store.len(), 1);
}

// --------------------------------------------------------------------------- //
// Durable load fails closed on a missing/unmounted directory (no silent empty catalog).
// --------------------------------------------------------------------------- //

// --------------------------------------------------------------------------- //
// No corruption under concurrent ingestion jobs: the single-writer lock serializes
// load-modify-save so a later job's save can never erase an earlier job's records.
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_016_store_lock_serializes_writers_without_loss() {
    let dir = temp_dir("lock_serialize");
    fs::create_dir_all(&dir).unwrap();

    // Job A holds the lock across its load-modify-save; a concurrent acquire is refused (so a
    // second job cannot load the old catalog and race A's save).
    {
        let _held = StoreLock::acquire(&dir).unwrap();
        assert!(
            matches!(StoreLock::acquire(&dir), Err(StoreError::Locked)),
            "a concurrent writer is refused while the lock is held"
        );
        let mut a = MarketDataStore::load_from_path(&dir).unwrap();
        ingest_batch(&mut a, DatasetKind::DailyEquityBar, EVENT_TS).unwrap();
        a.save_to_path(&dir).unwrap();
    }

    // Job B runs only after A releases; it loads A's persisted catalog and adds its own records.
    {
        let _held = StoreLock::acquire(&dir).unwrap();
        let mut b = MarketDataStore::load_from_path(&dir).unwrap();
        assert!(
            b.count_for_kind(DatasetKind::DailyEquityBar) > 0,
            "job B loads job A's persisted records (not an empty base)"
        );
        ingest_batch(&mut b, DatasetKind::Fundamental, EVENT_TS).unwrap();
        b.save_to_path(&dir).unwrap();
    }

    // Neither job's records were lost: the final catalog holds BOTH.
    let final_store = MarketDataStore::load_from_path(&dir).unwrap();
    assert!(
        final_store.count_for_kind(DatasetKind::DailyEquityBar) > 0,
        "job A's records survive the second job's publish"
    );
    assert!(
        final_store.count_for_kind(DatasetKind::Fundamental) > 0,
        "job B's records are present"
    );
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn srs_data_016_missing_store_directory_fails_closed() {
    let dir = temp_dir("missing").join("never-provisioned");
    assert!(matches!(
        MarketDataStore::load_from_path(&dir),
        Err(StoreError::Io { .. })
    ));
}
