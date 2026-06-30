//! SRS-DATA-008 (SSD-primary / NAS-archival tiered storage) — L7 domain / L5 integration tests.
//!
//! Acceptance: "All ingestion writes to SSD first; new data is synced to NAS; SSD retains at least
//! 90 days of configured hot data; NAS is used for indefinite retention; storage growth estimates
//! are documented in Section 12.1." These tests drive the public `atp-data` tier API
//! ([`TieredStore`]) over two real on-disk store directories with deterministic fixture data, and
//! inspect the persisted SSD/NAS store files — exactly the "Test, inspection" surface the feature
//! step permits ("fixture market data, provider mocks, file reads, and persisted output
//! inspection"). The §12.1 growth-estimate clause is satisfied by inspection of `docs/SRS.md` and is
//! not exercised here.
//!
//! The invariants under test:
//!   * **SSD-first + NAS superset** — an ingest commits to SSD, then NAS converges to a superset.
//!   * **Degraded mode** — an unreachable NAS never loses the SSD write; a later sync reconciles.
//!   * **≥90-day hot retention** — the window is floor-enforced; every hot datum is on SSD.
//!   * **NAS indefinite + safe archival** — cold data archived off SSD survives on NAS forever, and
//!     a record is NEVER dropped from SSD without a confirmed NAS copy.

use std::fs;
use std::path::{Path, PathBuf};

use atp_data::store::{
    fixture_batch, DatasetKind, MarketDataRecord, MarketDataStore, STORE_FILENAME,
};
use atp_data::tiering::{NasSyncStatus, RetentionVerdict, TierConfig, TierError, TieredStore};

const NOW: i64 = 1_700_000_000;
const DAY: i64 = 86_400;

/// A fresh, isolated `(ssd, nas)` directory pair under the system temp dir. The base carries the pid
/// so concurrent test runs in sibling worktrees (shared `/tmp`) never collide. Neither tier
/// directory is created — the tier provisions SSD on first write; NAS provisioning is per-test so
/// degraded mode can be exercised.
fn tier_dirs(label: &str) -> (PathBuf, PathBuf) {
    let base = std::env::temp_dir().join(format!("atp_data008_it_{}_{label}", std::process::id()));
    let _ = fs::remove_dir_all(&base);
    (base.join("ssd"), base.join("nas"))
}

/// The deterministic daily-bar fixture batch (2 records: AAPL, MSFT) dated at `event_ts`.
fn daily(event_ts: i64) -> Vec<MarketDataRecord> {
    fixture_batch(DatasetKind::DailyEquityBar, event_ts)
}

fn load(dir: &Path) -> MarketDataStore {
    MarketDataStore::load_from_path(dir).expect("load tier store")
}

fn store_file_exists(dir: &Path) -> bool {
    dir.join(STORE_FILENAME).is_file()
}

// --------------------------------------------------------------------------- //
// Invariant 0 — fail-closed configuration (the ≥90-day floor + distinct tiers).
// --------------------------------------------------------------------------- //

#[test]
fn data008_config_floor_and_distinct_dirs_fail_closed() {
    let (ssd, nas) = tier_dirs("config");

    // Below the SRS-DATA-008 floor → rejected, so SSD can never be configured to drop hot data early.
    assert_eq!(
        TierConfig::new(&ssd, &nas, 89),
        Err(TierError::HotRetentionBelowFloor {
            configured: 89,
            floor: 90,
        })
    );

    // At/above the floor → accepted; the default is the 90-day minimum.
    let cfg = TierConfig::new(&ssd, &nas, 90).expect("90 days is accepted");
    assert_eq!(cfg.hot_retention_days(), 90);
    assert!(TierConfig::new(&ssd, &nas, 365).is_ok());
    assert_eq!(
        TierConfig::with_default_retention(&ssd, &nas)
            .unwrap()
            .hot_retention_days(),
        90
    );

    // The two tiers must be distinct directories.
    assert!(matches!(
        TierConfig::with_default_retention(&ssd, &ssd),
        Err(TierError::TiersNotDistinct { .. })
    ));

    // ...and a LEXICAL alias of one directory (`ssd` vs `ssd/.`, or a trailing slash) is rejected
    // too — otherwise both tiers would share a store file and archival could delete the only copy.
    assert!(matches!(
        TierConfig::with_default_retention(&ssd, ssd.join(".")),
        Err(TierError::TiersNotDistinct { .. })
    ));
    assert!(matches!(
        TierConfig::with_default_retention(&ssd, PathBuf::from(format!("{}/", ssd.display()))),
        Err(TierError::TiersNotDistinct { .. })
    ));
}

// A symlink alias created AFTER the config is built (so it escapes the construction-time
// canonicalize check) must make EVERY NAS path fail closed — ingest reports Failed (not Synced),
// sync/report/archive all error — because NAS==SSD is not an independent archive and archival
// would otherwise delete the only copy. Unix-only (needs a symlink).
#[cfg(unix)]
#[test]
fn data008_post_config_symlink_alias_makes_every_nas_path_fail_closed() {
    use std::os::unix::fs::symlink;

    let (ssd, nas) = tier_dirs("alias_symlink");
    fs::create_dir_all(&ssd).unwrap();
    // NAS does not exist yet, so canonicalize cannot catch the alias at construction; the paths also
    // differ lexically (`ssd` vs `nas`), so the config is (deliberately) accepted here.
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    // Now alias NAS -> SSD. Ingest still commits SSD-first, but the NAS side must NOT report Synced:
    // NAS is not an independent archive, so it is surfaced as Failed (not Synced, not Degraded).
    symlink(&ssd, &nas).unwrap();
    let cold = daily(NOW - 200 * DAY);
    let outcome = tier.ingest(cold.clone()).unwrap();
    assert!(outcome.nas_sync.is_failed());
    assert!(!outcome.nas_sync.is_synced());
    assert_eq!(load(&ssd).len(), cold.len());

    // Explicit sync + report fail closed on the alias rather than claim an archived superset.
    assert!(matches!(tier.sync_ssd_to_nas(), Err(TierError::Nas(_))));
    assert!(matches!(tier.retention_report(NOW), Err(TierError::Nas(_))));

    // archive_cold MUST refuse: dropping a "confirmed on NAS" record when NAS *is* SSD would delete
    // the only copy.
    assert!(matches!(tier.archive_cold(NOW), Err(TierError::Nas(_))));

    // No data was lost — the records are still on the (shared) store.
    assert_eq!(load(&ssd).len(), cold.len());
}

// A REACHABLE NAS whose store is corrupt must NOT be reported as a recoverable outage (Degraded):
// an integrity failure of the archive is distinct (Failed) and must not be mistaken for an offline
// mount.
#[test]
fn data008_reachable_but_corrupt_nas_ingest_fails_not_degrades() {
    let (ssd, nas) = tier_dirs("corrupt_nas");
    fs::create_dir_all(&nas).unwrap();
    // Plant a corrupt NAS store file (a reachable directory with an unreadable store).
    fs::write(
        nas.join(STORE_FILENAME),
        b"not a valid market_data.store blob",
    )
    .unwrap();
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    let outcome = tier.ingest(daily(NOW)).unwrap();
    // SSD still committed; NAS surfaced as Failed (reachable but broken), NOT Degraded (offline).
    assert_eq!(outcome.ssd_inserted, 2);
    assert!(outcome.nas_sync.is_failed());
    assert!(!outcome.nas_sync.is_degraded());

    // The NAS-centric operations fail closed on the corrupt store too.
    assert!(matches!(tier.sync_ssd_to_nas(), Err(TierError::Nas(_))));
    assert!(matches!(tier.retention_report(NOW), Err(TierError::Nas(_))));
}

// CLI exit semantics: an archival integrity failure (reachable but broken NAS) must exit NON-ZERO
// so operator automation gating on exit status cannot mistake it for a clean ingest; a healthy
// ingest exits zero.
#[test]
fn data008_cli_ingest_exits_nonzero_on_failed_nas_but_zero_on_success() {
    use std::process::Command;
    let bin = env!("CARGO_BIN_EXE_data008_tier_cli");

    // Reachable-but-corrupt NAS -> the CLI must exit non-zero, but the SSD write still committed.
    let (ssd_bad, nas_bad) = tier_dirs("cli_failed");
    fs::create_dir_all(&nas_bad).unwrap();
    fs::write(nas_bad.join(STORE_FILENAME), b"corrupt nas store").unwrap();
    let status = Command::new(bin)
        .args([
            "ingest",
            "--ssd",
            ssd_bad.to_str().unwrap(),
            "--nas",
            nas_bad.to_str().unwrap(),
            "--kind",
            "daily-equity-bar",
        ])
        .status()
        .unwrap();
    assert!(
        !status.success(),
        "ingest must exit non-zero when the NAS archive write fails"
    );
    assert_eq!(load(&ssd_bad).len(), 2, "SSD write still committed");

    // Healthy NAS -> exit zero.
    let (ssd_ok, nas_ok) = tier_dirs("cli_ok");
    fs::create_dir_all(&nas_ok).unwrap();
    let status = Command::new(bin)
        .args([
            "ingest",
            "--ssd",
            ssd_ok.to_str().unwrap(),
            "--nas",
            nas_ok.to_str().unwrap(),
            "--kind",
            "daily-equity-bar",
        ])
        .status()
        .unwrap();
    assert!(status.success(), "a healthy ingest must exit zero");
}

// --------------------------------------------------------------------------- //
// Invariant 1 — ingest writes SSD-first, then NAS converges to a superset.
// --------------------------------------------------------------------------- //

#[test]
fn data008_ingest_writes_ssd_first_then_syncs_nas_superset() {
    let (ssd, nas) = tier_dirs("ssd_first");
    fs::create_dir_all(&nas).unwrap(); // NAS provisioned (reachable).
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    let batch = daily(NOW - 5 * DAY);
    let n = batch.len();
    let outcome = tier.ingest(batch.clone()).expect("ingest");

    // SSD is the primary: every record inserted and the SSD store file exists with all records.
    assert_eq!(outcome.ssd_inserted, n);
    assert_eq!(outcome.ssd_unchanged, 0);
    assert!(store_file_exists(&ssd));
    assert_eq!(load(&ssd).len(), n);

    // NAS synced to a superset of SSD.
    assert_eq!(outcome.nas_sync, NasSyncStatus::Synced { records_added: n });
    assert!(store_file_exists(&nas));
    assert_eq!(load(&nas).len(), n);
    for record in &batch {
        assert_eq!(load(&nas).get(record.key()), Some(record));
    }

    // The report independently confirms both retention invariants.
    let report = tier.retention_report(NOW).expect("report");
    assert!(report.nas_reachable);
    assert!(report.ssd_hot_retention_verdict().is_satisfied());
    assert!(report.nas_superset_verdict().is_satisfied());
    assert_eq!(report.ssd_missing_from_nas, 0);
}

// --------------------------------------------------------------------------- //
// Invariant 2 — NAS unreachable degrades but never loses the SSD write; sync recovers.
// --------------------------------------------------------------------------- //

#[test]
fn data008_nas_unreachable_degrades_but_preserves_ssd_write_then_recovers() {
    let (ssd, nas) = tier_dirs("degraded");
    // NAS deliberately NOT provisioned — models an unmounted/unreachable archival tier.
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    let batch = daily(NOW - 3 * DAY);
    let n = batch.len();
    let outcome = tier
        .ingest(batch.clone())
        .expect("ingest must succeed even with NAS down");

    // The SSD primary write STILL committed despite NAS being down (no data loss).
    assert_eq!(outcome.ssd_inserted, n);
    assert!(store_file_exists(&ssd));
    assert_eq!(load(&ssd).len(), n);

    // NAS reported degraded, not failed; the ingest did not error.
    assert!(outcome.nas_sync.is_degraded());
    assert!(!store_file_exists(&nas));

    let report = tier.retention_report(NOW).expect("report");
    assert!(!report.nas_reachable);
    assert_eq!(report.ssd_total, n); // SSD intact
                                     // With NAS unreachable, NEITHER invariant can be cross-checked — the verdict is Unverified, NOT
                                     // a false-positive Satisfied (the report never loaded NAS to look for hot records SSD is missing).
    assert_eq!(report.nas_superset_verdict(), RetentionVerdict::Unverified);
    assert_eq!(
        report.ssd_hot_retention_verdict(),
        RetentionVerdict::Unverified
    );

    // NAS returns: an explicit reconcile catches up the backlog.
    fs::create_dir_all(&nas).unwrap();
    let added = tier.sync_ssd_to_nas().expect("reconcile");
    assert_eq!(added, n);
    let report = tier.retention_report(NOW).expect("report after recovery");
    assert!(report.nas_superset_verdict().is_satisfied());
    assert!(report.ssd_hot_retention_verdict().is_satisfied());

    // An explicit sync to an unreachable NAS is fatal (the operator asked for confirmation).
    let (ssd2, nas2) = tier_dirs("degraded_fatal");
    let tier2 = TieredStore::new(TierConfig::with_default_retention(&ssd2, &nas2).unwrap());
    tier2.ingest(daily(NOW)).unwrap();
    assert!(matches!(tier2.sync_ssd_to_nas(), Err(TierError::Nas(_))));
}

// --------------------------------------------------------------------------- //
// Invariant 3 — hot/cold classification; SSD retains the full ≥90-day hot window.
// --------------------------------------------------------------------------- //

#[test]
fn data008_hot_cold_classification_ssd_retains_full_hot_window() {
    let (ssd, nas) = tier_dirs("hotcold");
    fs::create_dir_all(&nas).unwrap();
    let cfg = TierConfig::with_default_retention(&ssd, &nas).unwrap();

    // Classification is a pure function of (event_ts, now, window).
    assert!(cfg.is_hot(NOW - 10 * DAY, NOW)); // inside 90d → hot
    assert!(!cfg.is_hot(NOW - 200 * DAY, NOW)); // outside 90d → cold
    assert!(cfg.is_hot(NOW - 90 * DAY, NOW)); // exactly at the boundary → still hot (inclusive)

    let tier = TieredStore::new(cfg);
    let hot = daily(NOW - 10 * DAY);
    let cold = daily(NOW - 200 * DAY);
    tier.ingest(hot.clone()).unwrap();
    tier.ingest(cold.clone()).unwrap();

    let report = tier.retention_report(NOW).expect("report");
    assert_eq!(report.ssd_hot, hot.len());
    assert_eq!(report.ssd_cold, cold.len());
    assert_eq!(report.hot_missing_from_ssd, 0); // every hot datum is on SSD
    assert!(report.ssd_hot_retention_verdict().is_satisfied());
    assert!(report.nas_superset_verdict().is_satisfied()); // both hot and cold archived to NAS
    assert_eq!(report.nas_total, hot.len() + cold.len());
}

// --------------------------------------------------------------------------- //
// Invariant 4 — NAS indefinite retention + data-loss-safe cold archival.
// --------------------------------------------------------------------------- //

#[test]
fn data008_archive_cold_keeps_hot_on_ssd_and_nas_retains_indefinitely() {
    let (ssd, nas) = tier_dirs("archive");
    fs::create_dir_all(&nas).unwrap();
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    let hot = daily(NOW - 10 * DAY);
    let cold = daily(NOW - 200 * DAY);
    tier.ingest(hot.clone()).unwrap(); // both ingests sync to NAS
    tier.ingest(cold.clone()).unwrap();

    // Archive cold data off SSD — only what is confirmed on NAS is dropped.
    let archived = tier.archive_cold(NOW).expect("archive");
    assert!(archived.nas_reachable);
    assert_eq!(archived.archived, cold.len());
    assert_eq!(archived.retained_unconfirmed, 0);

    // SSD now holds ONLY the hot records; the cold ones are gone from SSD.
    let ssd_store = load(&ssd);
    assert_eq!(ssd_store.len(), hot.len());
    for record in &hot {
        assert_eq!(ssd_store.get(record.key()), Some(record));
    }
    for record in &cold {
        assert_eq!(ssd_store.get(record.key()), None);
    }

    // NAS retains EVERYTHING indefinitely — both hot and the archived cold.
    let nas_store = load(&nas);
    assert_eq!(nas_store.len(), hot.len() + cold.len());
    for record in hot.iter().chain(cold.iter()) {
        assert_eq!(nas_store.get(record.key()), Some(record));
    }

    // Retention invariants still hold after archival: hot on SSD, all on NAS.
    let report = tier.retention_report(NOW).expect("report");
    assert_eq!(report.ssd_cold, 0);
    assert_eq!(report.ssd_hot, hot.len());
    assert!(report.ssd_hot_retention_verdict().is_satisfied());
    assert!(report.nas_superset_verdict().is_satisfied());
}

#[test]
fn data008_archive_never_drops_a_record_absent_from_nas() {
    let (ssd, nas) = tier_dirs("archive_safe");
    // Ingest cold data with NAS DOWN: SSD has it, NAS does not.
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());
    let cold = daily(NOW - 200 * DAY);
    let outcome = tier.ingest(cold.clone()).unwrap();
    assert!(outcome.nas_sync.is_degraded());

    // Now NAS is reachable but EMPTY (the cold record was never synced).
    fs::create_dir_all(&nas).unwrap();
    let archived = tier.archive_cold(NOW).expect("archive");
    assert!(archived.nas_reachable);
    assert_eq!(archived.archived, 0); // nothing dropped...
    assert_eq!(archived.retained_unconfirmed, cold.len()); // ...because nothing is confirmed on NAS

    // The cold record is STILL on SSD — never lost without a confirmed archival copy.
    let ssd_store = load(&ssd);
    assert_eq!(ssd_store.len(), cold.len());
    for record in &cold {
        assert_eq!(ssd_store.get(record.key()), Some(record));
    }
}

#[test]
fn data008_archive_with_nas_unreachable_archives_nothing() {
    let (ssd, nas) = tier_dirs("archive_nas_down");
    // NAS never provisioned.
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());
    let hot = daily(NOW - 10 * DAY);
    let cold = daily(NOW - 200 * DAY);
    tier.ingest(hot.clone()).unwrap();
    tier.ingest(cold.clone()).unwrap();

    let archived = tier
        .archive_cold(NOW)
        .expect("archive must not error when NAS is down");
    assert!(!archived.nas_reachable);
    assert_eq!(archived.archived, 0);
    assert_eq!(archived.retained_unconfirmed, cold.len());

    // SSD is untouched — fail-safe: with no archival tier to confirm, nothing is dropped.
    assert_eq!(load(&ssd).len(), hot.len() + cold.len());
}

// --------------------------------------------------------------------------- //
// Idempotency carries through the tier (the SRS-DATA-016 property the tier inherits).
// --------------------------------------------------------------------------- //

#[test]
fn data008_reingest_is_idempotent_across_both_tiers() {
    let (ssd, nas) = tier_dirs("idem");
    fs::create_dir_all(&nas).unwrap();
    let tier = TieredStore::new(TierConfig::with_default_retention(&ssd, &nas).unwrap());

    let batch = daily(NOW - 5 * DAY);
    let n = batch.len();
    tier.ingest(batch.clone()).unwrap();

    // Re-ingest the SAME batch: no new SSD rows, nothing new to add to NAS.
    let outcome = tier.ingest(batch.clone()).unwrap();
    assert_eq!(outcome.ssd_inserted, 0);
    assert_eq!(outcome.ssd_unchanged, n);
    assert_eq!(outcome.nas_sync, NasSyncStatus::Synced { records_added: 0 });

    assert_eq!(load(&ssd).len(), n);
    assert_eq!(load(&nas).len(), n);
}
