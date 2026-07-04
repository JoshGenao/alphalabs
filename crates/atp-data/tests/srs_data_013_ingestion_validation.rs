//! SRS-DATA-013 / SyRS SYS-77 / ERR-5 — quarantine-and-continue end to end (L5 integration / L7
//! domain). Acceptance: "Records failing structural, range, duplicate, or required-field checks are
//! quarantined and NOT written to primary tables; ... alerts include counts and reasons."
//!
//! These tests drive the public `atp-data` quarantining ingestion path
//! ([`DataLayer::ingest_market_records_quarantining`]) over a real on-disk SSD/NAS tier with the
//! deterministic mixed fixture, and inspect the persisted primary store — exactly the "Test,
//! inspection" surface the feature step permits ("fixture market data, provider mocks, file reads, and
//! persisted output inspection").
//!
//! Invariants under test:
//!   * The valid subset is written to the primary tier; the quarantined records are ABSENT from it.
//!   * No valid record is lost (every well-formed record is persisted).
//!   * The counts-and-reasons summary is exact: one record per SYS-77 rule.
//!   * The same trust boundary as the sibling paths — corporate-action COVERAGE is refused fail-closed
//!     (nothing written).

use std::fs;
use std::path::{Path, PathBuf};

use atp_data::ingestion_validation::{mixed_validation_fixture, ALL_QUARANTINE_REASONS};
use atp_data::store::{
    fixture_batch, DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey,
};
use atp_data::tiering::{NasSyncStatus, TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, MarketIngestError, QuarantineSummarySink, Sys77RecordValidator};
use atp_types::QuarantineReason;

const TS: i64 = 1_700_000_000;
const OBSERVED_AT: u64 = 1_715_000_000;

/// A fresh, isolated `(ssd, nas)` directory pair under the system temp dir. The base carries the pid
/// so concurrent test runs in sibling worktrees (shared `/tmp`) never collide.
fn tier_dirs(label: &str) -> (PathBuf, PathBuf) {
    let base = std::env::temp_dir().join(format!("atp_data013_it_{}_{label}", std::process::id()));
    let _ = fs::remove_dir_all(&base);
    (base.join("ssd"), base.join("nas"))
}

fn tier(ssd: &Path, nas: &Path) -> TieredStore {
    fs::create_dir_all(nas).expect("provision NAS so the sync is Synced, not Degraded");
    TieredStore::new(
        TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).expect("valid tier config"),
    )
}

/// The symbols present in the persisted primary (SSD) store, as `(kind, symbol, option_contract)`.
fn primary_records(ssd: &Path) -> Vec<(DatasetKind, String, Option<String>)> {
    MarketDataStore::load_from_path(ssd)
        .expect("load the persisted primary store")
        .records()
        .iter()
        .map(|r| {
            let k = r.key();
            (k.kind, k.symbol.clone(), k.option_contract.clone())
        })
        .collect()
}

#[test]
fn mixed_batch_quarantines_invalid_and_writes_only_valid() {
    let (ssd, nas) = tier_dirs("mixed");
    let tier = tier(&ssd, &nas);
    let validator = Sys77RecordValidator::new();
    let sink = QuarantineSummarySink::new();

    let outcome = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            mixed_validation_fixture(TS),
            &validator,
            &sink,
            OBSERVED_AT,
        )
        .expect("the batch ingests (valid subset written, invalid quarantined)");

    // 4 well-formed records written; 6 malformed quarantined (one per SYS-77 rule).
    assert_eq!(
        outcome.written, 4,
        "the four well-formed fixtures are written"
    );
    assert_eq!(
        outcome.quarantined_records.len(),
        6,
        "one record quarantined per SYS-77 rule"
    );
    assert_eq!(
        outcome.tier.ssd_inserted, 4,
        "exactly the valid subset hits SSD"
    );

    // The counts-and-reasons summary (SyRS SYS-77 alert clause) is exact.
    let summary = sink.summary();
    assert_eq!(summary.quarantined_total, 6);
    assert_eq!(
        summary.quarantined_total,
        outcome.quarantined_records.len() as u64,
        "the sink total and the outcome's quarantined count agree (one event per drop)"
    );
    for reason in ALL_QUARANTINE_REASONS {
        assert_eq!(
            summary.count(reason),
            1,
            "exactly one {reason:?} quarantined in the mixed fixture"
        );
    }

    // The primary tables contain ONLY the valid records — the quarantined ones are absent.
    let present = primary_records(&ssd);
    assert_eq!(
        present.len(),
        4,
        "no quarantined record reached primary storage"
    );
    let symbols: Vec<&str> = present.iter().map(|(_, s, _)| s.as_str()).collect();
    // Valid records present:
    assert!(symbols.contains(&"AAPL"), "valid AAPL bar/option persisted");
    assert!(symbols.contains(&"MSFT"), "valid MSFT bar persisted");
    // Quarantined records absent (the malformed daily bars):
    for absent in ["TSLA", "NVDA", "AMZN", "META"] {
        assert!(
            !symbols.contains(&absent),
            "quarantined {absent} must NOT reach primary storage"
        );
    }
    // The malformed option contract (missing implied vol) is absent; the two valid contracts persist.
    let contracts: Vec<String> = present.iter().filter_map(|(_, _, c)| c.clone()).collect();
    assert!(contracts.iter().any(|c| c.contains("240119C00150000")));
    assert!(contracts.iter().any(|c| c.contains("240119P00150000")));
    assert!(
        !contracts.iter().any(|c| c.contains("240119C00160000")),
        "the quarantined option contract must NOT reach primary storage"
    );

    // The quarantined records are RETURNED (not silently dropped) so the deferred SRS-DATA-014/015
    // quarantine store can persist the rejected payloads for inspection/recovery.
    let quarantined_symbols: Vec<&str> = outcome
        .quarantined_records
        .iter()
        .map(|r| r.key().symbol.as_str())
        .collect();
    for sym in ["TSLA", "NVDA", "AMZN", "META"] {
        assert!(
            quarantined_symbols.contains(&sym),
            "quarantined {sym} must be returned, not dropped"
        );
    }

    // Valid records are archived to NAS (the tier synced), not lost.
    assert!(
        matches!(outcome.tier.nas_sync, NasSyncStatus::Synced { .. }),
        "the valid subset is NAS-synced"
    );
}

#[test]
fn all_valid_batch_writes_everything_with_zero_quarantine() {
    // No false positives: a batch of well-formed records is written in full, none quarantined.
    let (ssd, nas) = tier_dirs("all_valid");
    let tier = tier(&ssd, &nas);
    let validator = Sys77RecordValidator::new();
    let sink = QuarantineSummarySink::new();

    let mut batch = fixture_batch(DatasetKind::DailyEquityBar, TS);
    batch.extend(fixture_batch(DatasetKind::OptionChainSnapshot, TS));
    let expected = batch.len();

    let outcome = DataLayer
        .ingest_market_records_quarantining(&tier, batch, &validator, &sink, OBSERVED_AT)
        .expect("a well-formed batch ingests cleanly");

    assert_eq!(outcome.written, expected);
    assert_eq!(outcome.quarantined_records.len(), 0);
    assert_eq!(sink.summary().quarantined_total, 0);
    assert_eq!(primary_records(&ssd).len(), expected);
}

#[test]
fn duplicate_within_batch_is_quarantined_not_written() {
    // Two AAPL daily bars with the same natural key but different values: the first is written, the
    // second is quarantined as DuplicateRecord and never reaches primary storage.
    let (ssd, nas) = tier_dirs("dup");
    let tier = tier(&ssd, &nas);
    let validator = Sys77RecordValidator::new();
    let sink = QuarantineSummarySink::new();

    let first = ohlcv("AAPL", 150);
    let second = ohlcv("AAPL", 151); // same key, different close/volume
    let outcome = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![first, second],
            &validator,
            &sink,
            OBSERVED_AT,
        )
        .expect("ingests with the duplicate quarantined");

    assert_eq!(outcome.written, 1, "only the first AAPL bar is written");
    assert_eq!(outcome.quarantined_records.len(), 1);
    assert_eq!(sink.summary().count(QuarantineReason::DuplicateRecord), 1);
    assert_eq!(primary_records(&ssd).len(), 1);
}

#[test]
fn coverage_kind_is_refused_fail_closed() {
    // Same trust boundary as ingest_market_record / ingest_market_records_tiered: corporate-action
    // COVERAGE is an operator trust assertion, not provider market data — refused, nothing written.
    let (ssd, nas) = tier_dirs("coverage");
    let tier = tier(&ssd, &nas);
    let validator = Sys77RecordValidator::new();
    let sink = QuarantineSummarySink::new();

    let coverage = MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::CorporateActionCoverage,
            symbol: "AAPL".to_string(),
            resolution: "coverage".to_string(),
            event_ts: TS,
            option_contract: None,
        },
        [MarketField {
            name: "complete_through".to_string(),
            value_minor: TS,
        }],
    )
    .expect("a well-formed coverage record");

    let err = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 150), coverage],
            &validator,
            &sink,
            OBSERVED_AT,
        )
        .expect_err("COVERAGE must be refused");
    assert!(matches!(err, MarketIngestError::UnsupportedKind { .. }));
    // Fail-closed before the tier write: nothing was persisted.
    assert!(
        MarketDataStore::load_from_path(&ssd)
            .map(|s| s.records().is_empty())
            .unwrap_or(true),
        "a refused batch writes nothing to primary storage"
    );
}

#[test]
fn conflicting_cross_store_duplicate_is_quarantined_not_batch_abort() {
    // SYS-77 rule (e) beyond the within-batch check: a record whose natural key already exists in the
    // primary store with DIFFERING content is a cross-store DuplicateRecord. It must be quarantined
    // (event + returned) while OTHER valid records in the same batch are still written — NOT abort the
    // whole batch as a tier ConflictingContent (the pre-fix behavior).
    let (ssd, nas) = tier_dirs("cross_store_dup");
    let tier = tier(&ssd, &nas);

    // Seed the primary store with AAPL@150.
    DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 150)],
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect("seed ingest");
    assert_eq!(primary_records(&ssd).len(), 1);

    // Second batch: a CONFLICTING AAPL (same key, different value) + a fresh MSFT.
    let sink = QuarantineSummarySink::new();
    let outcome = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 151), ohlcv("MSFT", 320)],
            &Sys77RecordValidator::new(),
            &sink,
            OBSERVED_AT,
        )
        .expect("a conflicting cross-store duplicate must NOT abort the batch");

    assert_eq!(
        outcome.written, 1,
        "the fresh MSFT is written (quarantine-and-continue)"
    );
    assert_eq!(outcome.quarantined_records.len(), 1);
    assert_eq!(outcome.quarantined_records[0].key().symbol, "AAPL");
    assert_eq!(
        sink.summary().count(QuarantineReason::DuplicateRecord),
        1,
        "the cross-store conflict is reported as DuplicateRecord, not an uncounted tier error"
    );
    // The primary store keeps the ORIGINAL AAPL@150 (the conflict never overwrote it) plus MSFT.
    let present = primary_records(&ssd);
    assert_eq!(present.len(), 2);
    assert!(present.iter().any(|(_, s, _)| s == "MSFT"));
}

#[test]
fn identical_cross_store_reingest_is_idempotent_not_quarantined() {
    // Re-ingesting an IDENTICAL record (same key, same value) across batches is idempotent
    // (SRS-DATA-016), NOT a DuplicateRecord quarantine.
    let (ssd, nas) = tier_dirs("idempotent_reingest");
    let tier = tier(&ssd, &nas);

    for _ in 0..2 {
        let outcome = DataLayer
            .ingest_market_records_quarantining(
                &tier,
                vec![ohlcv("AAPL", 150)],
                &Sys77RecordValidator::new(),
                &QuarantineSummarySink::new(),
                OBSERVED_AT,
            )
            .expect("idempotent re-ingest");
        assert_eq!(
            outcome.quarantined_records.len(),
            0,
            "an identical re-ingest is idempotent, not a duplicate violation"
        );
    }
    assert_eq!(
        primary_records(&ssd).len(),
        1,
        "no duplicate row created across runs"
    );
}

#[test]
fn conflict_with_nas_archived_cold_record_is_quarantined_no_ssd_mutation() {
    // The cross-TIER case: a record archived off SSD (present only on the NAS archival tier) must still
    // be seen by the conflict snapshot, so a re-ingest with the same natural key + DIFFERING content is
    // quarantined as DuplicateRecord BEFORE the SSD write — not committed to SSD and only surfaced as a
    // failed NAS sync afterwards (which would leave the SSD primary holding the divergent value).
    const DAY: i64 = 86_400;
    let (ssd, nas) = tier_dirs("nas_archived_conflict");
    let tier = tier(&ssd, &nas);
    let now = TS + 500 * DAY; // well past any hot-retention window → the TS-dated record is cold

    // Ingest AAPL@150 (dated at TS, cold relative to `now`) and archive it off SSD onto NAS.
    DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 150)],
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect("seed ingest");
    let archived = tier.archive_cold(now).expect("archive cold data off SSD");
    assert!(
        archived.archived >= 1,
        "the cold AAPL bar is archived off SSD"
    );
    assert!(
        primary_records(&ssd).iter().all(|(_, s, _)| s != "AAPL"),
        "AAPL is no longer on the SSD primary tier (archived to NAS)"
    );

    // Re-ingest a CONFLICTING AAPL (same key, different value).
    let sink = QuarantineSummarySink::new();
    let outcome = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 151)],
            &Sys77RecordValidator::new(),
            &sink,
            OBSERVED_AT,
        )
        .expect("conflicting re-ingest of an archived key must not abort or error");

    assert_eq!(outcome.written, 0, "the conflicting record is not written");
    assert_eq!(outcome.quarantined_records.len(), 1);
    assert_eq!(
        sink.summary().count(QuarantineReason::DuplicateRecord),
        1,
        "the archived-record conflict is reported as DuplicateRecord"
    );
    // Crucially: the divergent value never reached the SSD primary tier.
    assert!(
        primary_records(&ssd).iter().all(|(_, s, _)| s != "AAPL"),
        "the conflicting AAPL must NOT be committed to the SSD primary tier"
    );
}

#[test]
fn corrupt_tier_store_fails_closed_before_ssd_write() {
    // FAIL CLOSED, not open: if a tier needed for the cross-tier duplicate snapshot is present but
    // UNREADABLE (corrupt), the ingest must abort BEFORE any SSD write rather than treating it as "no
    // keys" and committing a possibly-conflicting record.
    use atp_data::store::STORE_FILENAME;
    let (ssd, nas) = tier_dirs("corrupt_nas");
    let tier = tier(&ssd, &nas);

    // Seed a real NAS store (so the store file exists), then corrupt it.
    DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("AAPL", 150)],
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect("seed ingest provisions the NAS store");
    fs::write(nas.join(STORE_FILENAME), b"not a valid store file").expect("corrupt the NAS store");

    // A subsequent ingest must FAIL CLOSED — the corrupt NAS cannot be read for the dup snapshot, and a
    // conflicting archived record could hide there.
    let err = DataLayer
        .ingest_market_records_quarantining(
            &tier,
            vec![ohlcv("MSFT", 320)],
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .expect_err("a corrupt tier store must fail closed, not silently proceed");
    assert!(matches!(err, MarketIngestError::Store(_)));
    // MSFT was never written — the method aborted before the SSD write.
    assert!(
        primary_records(&ssd).iter().all(|(_, s, _)| s != "MSFT"),
        "the ingest failed closed — nothing new committed to the SSD primary tier"
    );
}

/// A well-formed daily OHLCV bar (low < open,close < high; positive volume).
fn ohlcv(symbol: &str, base: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts: TS,
            option_contract: None,
        },
        [
            MarketField {
                name: "open".to_string(),
                value_minor: base,
            },
            MarketField {
                name: "high".to_string(),
                value_minor: base + 10,
            },
            MarketField {
                name: "low".to_string(),
                value_minor: base - 10,
            },
            MarketField {
                name: "close".to_string(),
                value_minor: base + 2,
            },
            MarketField {
                name: "volume".to_string(),
                value_minor: base * 100,
            },
        ],
    )
    .expect("well-formed OHLCV bar")
}
