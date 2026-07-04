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
        outcome.quarantined, 6,
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
        summary.quarantined_total, outcome.quarantined as u64,
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
    assert_eq!(outcome.quarantined, 0);
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
    assert_eq!(outcome.quarantined, 1);
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
