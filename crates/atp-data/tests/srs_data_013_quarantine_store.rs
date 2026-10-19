//! SRS-DATA-013 / SyRS SYS-77 — the durable quarantine store, through both primary-storage write
//! paths (L5 integration / L7 domain).
//!
//! SYS-77: records failing validation "shall be quarantined in a separate store". These tests drive
//! the two public paths that write primary storage — `ingest_market_records_quarantining` and
//! `ingest_market_records_tiered` — over a real on-disk tier and read the store back through its
//! public reader, so they cover what the dashboard and the notifier will see.
//!
//! Invariants under test:
//!   * Every batch on either path is recorded as one run, clean or not.
//!   * Every quarantined record is persisted with its reason, source, and hash.
//!   * A batch whose run cannot be recorded writes NOTHING to primary storage.
//!   * The run records what happened to the primary write (WRITTEN / REFUSED / FAILED).

use std::fs;
use std::path::{Path, PathBuf};

use atp_data::ingestion_validation::mixed_validation_fixture;
use atp_data::store::{
    coverage_record, fixture_batch, DatasetKind, MarketDataRecord, MarketDataStore, MarketField,
    STORE_FILENAME,
};
use atp_data::tiering::TierError;
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{
    DataLayer, MarketIngestError, PrimaryWrite, QuarantineRun, QuarantineStore,
    QuarantineStoreError, QuarantineSummarySink, RunRead, Sys77RecordValidator, QUARANTINE_DIRNAME,
};
use atp_data::{IngestionValidationEventSink, RecordValidator};
use atp_types::QuarantineReason;
use atp_types::{IngestionValidationEvent, RecordValidationOutcome};

const TS: i64 = 1_700_000_000;
const OBSERVED_AT: u64 = 1_715_000_000;

fn tier_dirs(label: &str) -> (PathBuf, PathBuf) {
    let base = std::env::temp_dir().join(format!("atp_data013_qs_{}_{label}", std::process::id()));
    let _ = fs::remove_dir_all(&base);
    (base.join("ssd"), base.join("nas"))
}

fn tier(ssd: &Path, nas: &Path) -> TieredStore {
    fs::create_dir_all(nas).expect("provision NAS");
    TieredStore::new(TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).expect("tier config"))
}

/// The single run in the store beside `ssd`, which must be readable.
fn only_run(ssd: &Path) -> QuarantineRun {
    let latest = QuarantineStore::beside(ssd)
        .read_latest(10)
        .expect("read store");
    assert_eq!(latest.total_runs, 1, "exactly one run recorded");
    match latest.runs.into_iter().next() {
        Some(RunRead::Readable(run)) => run,
        other => panic!("expected one readable run, got {other:?}"),
    }
}

/// Make the quarantine store unwritable: a plain file where its directory belongs.
fn block_quarantine_dir(ssd: &Path) {
    fs::create_dir_all(ssd).unwrap();
    fs::write(ssd.join(QUARANTINE_DIRNAME), b"not a directory").unwrap();
}

#[test]
fn quarantining_path_persists_every_reject_with_its_reason() {
    let (ssd, nas) = tier_dirs("q_persist");
    let tier = tier(&ssd, &nas);
    let sink = QuarantineSummarySink::new();
    let outcome = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            mixed_validation_fixture(TS),
            &Sys77RecordValidator::new(),
            &sink,
            OBSERVED_AT,
        )
        .expect("ingest");

    let run = only_run(&ssd);
    assert_eq!(run.run_id, outcome.quarantine.run_id);
    assert!(outcome.quarantine.finalized);
    assert_eq!(run.records_in, 10);
    assert_eq!(run.observed_at_seconds, OBSERVED_AT);
    assert_eq!(run.primary, PrimaryWrite::Written { records: 4 });
    // The store and the in-memory summary agree reason by reason.
    let summary = sink.summary();
    assert_eq!(run.quarantined_total(), summary.quarantined_total);
    assert_eq!(run.per_reason(), summary.per_reason());
    // The rejected payloads themselves are persisted, each with the ERR-5 envelope it was judged by.
    let persisted: Vec<_> = run.entries.iter().map(|e| e.record.clone()).collect();
    assert_eq!(persisted, outcome.quarantined_records);
    for entry in &run.entries {
        let submission = entry.record.ingestion_submission();
        assert_eq!(entry.source, submission.source);
        assert_eq!(entry.record_hash, submission.record_hash);
    }
}

#[test]
fn quarantining_path_writes_nothing_when_the_run_cannot_be_recorded() {
    let (ssd, nas) = tier_dirs("q_blocked");
    let tier = tier(&ssd, &nas);
    block_quarantine_dir(&ssd);
    let err = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            mixed_validation_fixture(TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("an unrecordable run must refuse the batch");
    assert!(
        matches!(
            err,
            MarketIngestError::Quarantine(QuarantineStoreError::Io { .. })
        ),
        "got {err:?}"
    );
    assert!(
        !ssd.join(STORE_FILENAME).exists(),
        "no primary write when the quarantine cannot be recorded"
    );
}

#[test]
fn tiered_path_records_every_reject_and_refuses_the_batch() {
    let (ssd, nas) = tier_dirs("t_refused");
    let tier = tier(&ssd, &nas);
    let err = DataLayer
        .ingest_market_records_tiered(
            &tier,
            mixed_validation_fixture(TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("the tiered path refuses a batch with any reject");
    assert!(matches!(err, MarketIngestError::Rejected(_)), "got {err:?}");

    let run = only_run(&ssd);
    assert_eq!(run.primary, PrimaryWrite::Refused);
    assert_eq!(run.records_in, 10);
    // Every reject is recorded, not only the first one that stopped the batch.
    assert_eq!(run.quarantined_total(), 6);
    for (reason, count) in run.per_reason() {
        assert_eq!(count, 1, "{} recorded once", reason.as_str());
    }
    assert!(
        !ssd.join(STORE_FILENAME).exists(),
        "nothing reached primary"
    );
}

#[test]
fn tiered_path_records_a_clean_batch_as_written() {
    let (ssd, nas) = tier_dirs("t_clean");
    let tier = tier(&ssd, &nas);
    let outcome = DataLayer
        .ingest_market_records_tiered(
            &tier,
            fixture_batch(DatasetKind::DailyEquityBar, TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect("clean ingest");
    let run = only_run(&ssd);
    assert_eq!(run.run_id, outcome.quarantine.run_id);
    assert!(outcome.quarantine.finalized);
    assert_eq!(run.primary, PrimaryWrite::Written { records: 2 });
    assert_eq!(run.quarantined_total(), 0);
    assert_eq!(run.count(QuarantineReason::RangeViolation), 0);
}

#[test]
fn tiered_path_writes_nothing_when_the_run_cannot_be_recorded() {
    let (ssd, nas) = tier_dirs("t_blocked");
    let tier = tier(&ssd, &nas);
    block_quarantine_dir(&ssd);
    let err = DataLayer
        .ingest_market_records_tiered(
            &tier,
            fixture_batch(DatasetKind::DailyEquityBar, TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("an unrecordable run must refuse the batch");
    assert!(
        matches!(err, MarketIngestError::Quarantine(_)),
        "got {err:?}"
    );
    assert!(!ssd.join(STORE_FILENAME).exists());
}

#[test]
fn tiered_path_records_a_failed_primary_write_as_failed() {
    let (ssd, nas) = tier_dirs("t_failed");
    let tier = tier(&ssd, &nas);
    // A present-but-corrupt SSD store: the tier's primary write fails closed.
    fs::create_dir_all(&ssd).unwrap();
    fs::write(ssd.join(STORE_FILENAME), b"corrupt primary").unwrap();
    let err = DataLayer
        .ingest_market_records_tiered(
            &tier,
            fixture_batch(DatasetKind::DailyEquityBar, TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("a corrupt primary fails the write");
    assert!(matches!(err, MarketIngestError::Tier(_)), "got {err:?}");
    assert_eq!(only_run(&ssd).primary, PrimaryWrite::Failed);
}

#[test]
fn quarantining_path_records_its_rejects_when_a_catalog_cannot_be_read() {
    let (ssd, nas) = tier_dirs("q_corrupt_catalog");
    let tier = tier(&ssd, &nas);
    // A present-but-corrupt SSD catalog: the cross-store duplicate pass fails closed AFTER the
    // batch's rejects were classified. They must not be dropped with the error.
    fs::create_dir_all(&ssd).unwrap();
    fs::write(ssd.join(STORE_FILENAME), b"corrupt primary").unwrap();
    let err = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            mixed_validation_fixture(TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("a corrupt catalog fails the batch closed");
    assert!(matches!(err, MarketIngestError::Store(_)), "got {err:?}");
    let run = only_run(&ssd);
    assert_eq!(run.primary, PrimaryWrite::Refused);
    assert_eq!(run.records_in, 10);
    assert_eq!(
        run.quarantined_total(),
        6,
        "every classified reject is recorded"
    );
}

#[test]
fn a_coverage_record_refuses_and_records_the_whole_batch_on_both_paths() {
    for path in ["quarantining", "tiered"] {
        let (ssd, nas) = tier_dirs(&format!("coverage_{path}"));
        let tier = tier(&ssd, &nas);
        let mut batch = mixed_validation_fixture(TS);
        batch.push(coverage_record(TS, "AAPL"));
        let validator = Sys77RecordValidator::new();
        let sink = QuarantineSummarySink::new();
        let err = if path == "quarantining" {
            DataLayer
                .ingest_market_records_quarantining(&tier, batch, &validator, &sink, OBSERVED_AT)
                .map(|_| ())
                .expect_err("coverage is refused")
        } else {
            DataLayer
                .ingest_market_records_tiered(&tier, batch, &validator, &sink, OBSERVED_AT)
                .map(|_| ())
                .expect_err("coverage is refused")
        };
        assert!(
            matches!(err, MarketIngestError::UnsupportedKind { .. }),
            "{path}: got {err:?}"
        );
        let run = only_run(&ssd);
        assert_eq!(run.primary, PrimaryWrite::Refused, "{path}");
        assert_eq!(
            run.records_in, 11,
            "{path}: the whole batch is accounted for"
        );
        assert!(
            !ssd.join(STORE_FILENAME).exists(),
            "{path}: nothing reached primary"
        );
    }
}

/// Admits every record: lets a same-key, different-content pair reach the tier, where only the
/// writer-locked conflict check can catch it.
struct AcceptAll;
impl RecordValidator for AcceptAll {
    fn validate(&self, _record: &MarketDataRecord) -> RecordValidationOutcome {
        RecordValidationOutcome::Valid
    }
}

#[derive(Default)]
struct CountingSink(std::cell::Cell<usize>);
impl IngestionValidationEventSink for CountingSink {
    fn record(&self, _event: IngestionValidationEvent) {
        self.0.set(self.0.get() + 1);
    }
}

/// `record` with every value field moved by one minor unit: same natural key, different content.
fn restated(record: &MarketDataRecord) -> MarketDataRecord {
    let fields = record.fields().iter().map(|f| MarketField {
        name: f.name.clone(),
        value_minor: f.value_minor + 1,
    });
    MarketDataRecord::new(record.key().clone(), fields).unwrap()
}

#[test]
fn tiered_path_records_a_conflicting_reingest_with_its_payload() {
    let (ssd, nas) = tier_dirs("t_conflict");
    let tier = tier(&ssd, &nas);
    let original = fixture_batch(DatasetKind::DailyEquityBar, TS);
    DataLayer
        .ingest_market_records_tiered(
            &tier,
            original.clone(),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect("seed the primary");
    let conflicting = restated(&original[0]);
    let sink = QuarantineSummarySink::new();
    let err = DataLayer
        .ingest_market_records_tiered(
            &tier,
            vec![conflicting.clone()],
            &Sys77RecordValidator::new(),
            &sink,
            OBSERVED_AT,
        )
        .expect_err("a conflicting re-ingest is refused");
    assert!(
        matches!(err, MarketIngestError::Tier(TierError::Ssd(_))),
        "got {err:?}"
    );
    let latest = QuarantineStore::beside(&ssd).read_latest(1).unwrap();
    let RunRead::Readable(run) = &latest.runs[0] else {
        panic!("expected a readable run")
    };
    assert_eq!(run.primary, PrimaryWrite::Refused);
    assert_eq!(run.count(QuarantineReason::DuplicateRecord), 1);
    assert_eq!(
        run.entries[0].record, conflicting,
        "the rejected payload itself is kept"
    );
    assert_eq!(
        sink.summary().quarantined_total,
        1,
        "the counts sink agrees with the run"
    );
    // The primary still holds the original values.
    let primary = MarketDataStore::load_from_path(&ssd).unwrap();
    assert_eq!(primary.get(original[0].key()), Some(&original[0]));
}

#[test]
fn tiered_path_catches_a_conflict_within_the_batch_under_the_lock() {
    let (ssd, nas) = tier_dirs("t_conflict_in_batch");
    let tier = tier(&ssd, &nas);
    let original = fixture_batch(DatasetKind::DailyEquityBar, TS);
    let batch = vec![original[0].clone(), restated(&original[0])];
    let err = DataLayer
        .ingest_market_records_tiered(
            &tier,
            batch,
            &AcceptAll,
            &CountingSink::default(),
            OBSERVED_AT,
        )
        .expect_err("the batch is refused");
    assert!(matches!(err, MarketIngestError::Tier(_)), "got {err:?}");
    assert_eq!(only_run(&ssd).primary, PrimaryWrite::Refused);
    assert_eq!(only_run(&ssd).quarantined_total(), 1);
    assert!(
        !ssd.join(STORE_FILENAME).exists(),
        "nothing reached primary"
    );
}

#[test]
fn quarantining_path_sets_aside_a_conflict_the_tier_finds_and_writes_the_rest() {
    let (ssd, nas) = tier_dirs("q_conflict_in_batch");
    let tier = tier(&ssd, &nas);
    let original = fixture_batch(DatasetKind::DailyEquityBar, TS);
    let conflicting = restated(&original[0]);
    let mut batch = original.clone();
    batch.push(conflicting.clone());
    let sink = CountingSink::default();
    let outcome = DataLayer
        .ingest_market_records_quarantining(&tier, batch, &AcceptAll, &sink, OBSERVED_AT)
        .expect("quarantine-and-continue");
    assert_eq!(
        outcome.written,
        original.len(),
        "the non-conflicting records are written"
    );
    assert_eq!(outcome.quarantined_records, vec![conflicting.clone()]);
    assert_eq!(sink.0.get(), 1, "one event for the set-aside conflict");
    let run = only_run(&ssd);
    assert_eq!(
        run.primary,
        PrimaryWrite::Written {
            records: original.len() as u64
        }
    );
    assert_eq!(run.count(QuarantineReason::DuplicateRecord), 1);
    assert_eq!(run.entries[0].record, conflicting);
}
