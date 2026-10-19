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
use atp_data::store::{fixture_batch, DatasetKind, STORE_FILENAME};
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{
    DataLayer, MarketIngestError, PrimaryWrite, QuarantineRun, QuarantineStore,
    QuarantineStoreError, QuarantineSummarySink, RunRead, Sys77RecordValidator, QUARANTINE_DIRNAME,
};
use atp_types::QuarantineReason;

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
