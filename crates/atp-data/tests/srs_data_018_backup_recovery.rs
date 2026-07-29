//! SRS-DATA-018 boundary (L4) test: exercise the backup + validated-recovery surface end-to-end over
//! real on-disk fixture stores, proving each acceptance clause of SyRS SYS-59 / SYS-60.
//!
//! The acceptance criterion has three parts and this file proves each against real files, not mocks:
//!
//!   1. *"Weekly default backups can export NAS data to an external target"* — a default-cadence
//!      config exports every discovered unit under the NAS root to a separate target root.
//!   2. *"backup completion validates integrity"* — the verification re-reads the EXPORTED bytes;
//!      a single flipped byte in the target is caught, and a corrupt SOURCE is refused rather than
//!      faithfully replicated and then declared verified.
//!   3. *"documented RPO is no more than 7 days"* — the ledger drives an RPO assessment that fails
//!      closed with no verified backup and past the 7-day boundary.
//!
//! Plus the recovery half: `restore` reproduces the archived bytes into a fresh destination and
//! proves the restored record set matches.
//!
//! What is NOT proven here, and why the feature stays `passes:false` (serialized): the external
//! target in these tests is a sibling temp directory standing in for real external media. The AC's
//! "export to an external storage target (USB drive, secondary NAS, or cloud archival bucket)" on a
//! real weekly schedule is a Demonstration/inspection step that needs the operator's hardware.

use std::fs;
use std::path::{Path, PathBuf};

use atp_data::backup::{
    discover_unit_names, due, restore, rpo_report, run_backup, run_backup_locked, verify_archive,
    BackupConfig, BackupError, BackupLedger, BackupVerdict, ForeignCodecValidator, TargetStatus,
    UnitKind, VerificationDepth, BACKTEST_STORE_FILENAME, BACKTEST_STORE_MAGIC,
    BACKUP_LEDGER_FILENAME, DEFAULT_CADENCE_DAYS, RPO_MAX_DAYS,
};
use atp_data::store::{
    DatasetKind, MarketDataRecord, MarketDataStore, MarketField, NaturalKey, STORE_FILENAME,
};

const NOW: i64 = 1_700_000_000;
const SECONDS_PER_DAY: i64 = 86_400;

fn daily(symbol: &str, event_ts: i64) -> MarketDataRecord {
    MarketDataRecord::new(
        NaturalKey {
            kind: DatasetKind::DailyEquityBar,
            symbol: symbol.to_string(),
            resolution: "1d".to_string(),
            event_ts,
            option_contract: None,
        },
        [MarketField {
            name: "close".to_string(),
            value_minor: 10_000,
        }],
    )
    .unwrap()
}

/// A unique scratch root for a test (the crate has no `tempfile` dependency).
fn scratch(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!(
        "atp-data018-{}-{}-{}",
        tag,
        std::process::id(),
        line!()
    ));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    base
}

/// Seed a market-data store unit at `dir` with `symbols`.
fn seed_market_data(dir: &Path, symbols: &[&str]) {
    let mut store = MarketDataStore::new();
    for symbol in symbols {
        store.upsert(daily(symbol, NOW)).unwrap();
    }
    store.save_to_path(dir).unwrap();
}

/// Seed a *backtest-results*-shaped unit: the same magic+checksum envelope written by
/// `atp-simulation`, which this crate must back up without being able to import its codec.
fn seed_backtest_results(dir: &Path, body: &str) {
    fs::create_dir_all(dir).unwrap();
    // FNV-1a, matching `store::checksum` — this test stands in for the atp-simulation writer.
    let checksum = {
        const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut hash = OFFSET_BASIS;
        for &byte in body.as_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
        hash
    };
    let blob = format!("{BACKTEST_STORE_MAGIC}\n{}\n{body}", i128::from(checksum));
    fs::write(dir.join(BACKTEST_STORE_FILENAME), blob).unwrap();
}

/// A stand-in for `atp-simulation`'s backtest decoder. Without one, a backtest unit is reported
/// `Unverified` by design — this crate cannot prove such a blob is restorable.
fn backtest_validator() -> ForeignCodecValidator {
    std::sync::Arc::new(|text: &str| {
        if text.contains("run-") {
            Ok(())
        } else {
            Err("no run id in backtest body".to_string())
        }
    })
}

// ------------------------------------------------------------------------- //
// AC 1 — weekly default backup exports NAS data to an external target
// ------------------------------------------------------------------------- //

#[test]
fn default_cadence_export_covers_every_discovered_unit_and_verifies_each() {
    let root = scratch("export");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL", "MSFT"]);
    seed_market_data(&nas.join("options"), &["SPY"]);
    seed_backtest_results(&nas.join("backtests"), "run-1\n");

    // The backtest unit needs the owning codec wired in to reach `Verified`; without it the run is
    // deliberately `Unverified` (see backtest_units_without_a_validator_are_unverified_not_verified).
    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(backtest_validator());
    assert_eq!(config.cadence_days(), DEFAULT_CADENCE_DAYS);

    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Verified);
    assert_eq!(report.units.len(), 3, "{:?}", report.units);
    assert_eq!(report.verified_units().len(), 3);

    // Every unit landed at the mirrored relative path under the target.
    assert!(target.join("equities").join(STORE_FILENAME).is_file());
    assert!(target.join("options").join(STORE_FILENAME).is_file());
    assert!(target
        .join("backtests")
        .join(BACKTEST_STORE_FILENAME)
        .is_file());

    // The market-data units report their record counts; the foreign codec honestly reports none.
    let equities = report
        .units
        .iter()
        .find(|u| u.unit == "equities/market_data.store")
        .unwrap();
    assert_eq!(equities.kind, UnitKind::MarketData);
    assert_eq!(equities.records, Some(2));
    assert_eq!(equities.status, TargetStatus::Written);
    let backtests = report
        .units
        .iter()
        .find(|u| u.unit == "backtests/backtest_results.store")
        .unwrap();
    assert_eq!(backtests.kind, UnitKind::BacktestResults);
    assert_eq!(backtests.records, None);
    assert_eq!(backtests.verdict, BackupVerdict::Verified);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn an_absent_target_mount_is_degraded_and_is_never_created_on_local_disk() {
    // If the USB / secondary-NAS mount is not present, creating the path on demand would write the
    // "backup" onto the very machine whose loss it exists to survive — and it would verify, because
    // it would be verifying against itself. Refuse, report Degraded (recoverable: plug the drive
    // in), and leave nothing behind.
    let root = scratch("absent-mount");
    let nas = root.join("nas");
    let target = root.join("not-mounted");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    assert!(!target.exists());

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();

    assert_eq!(
        report.verdict(),
        BackupVerdict::Unverified,
        "an absent target must never yield a verified backup"
    );
    assert_eq!(report.units.len(), 1);
    assert_eq!(report.units[0].status, TargetStatus::Degraded);
    assert!(report.verified_units().is_empty());
    assert!(
        !target.exists(),
        "the backup must NOT create the external target root on local disk"
    );

    // ...and nothing is certified: the ledger stays empty and the RPO stays breached.
    let mut ledger = BackupLedger::new();
    ledger.record(&report, &target).unwrap();
    assert!(
        !target.exists(),
        "recording a report with nothing verified must not create the target either"
    );
    let current = discover_unit_names(&nas).unwrap();
    assert!(!rpo_report(&ledger, &current, NOW).within_rpo);
    assert!(due(&ledger, &config, &current, NOW));

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_mount_that_vanishes_mid_run_is_never_recreated_on_local_disk() {
    // The reachability classifier must CLASSIFY, not create. If it called create_dir_all, a mount
    // that disappeared after the first unit would be materialised locally, and the remaining units
    // would export and verify inside the source's failure domain — and be recorded as real backups.
    let root = scratch("vanishing-mount");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    for unit in ["a", "b", "c"] {
        seed_market_data(&nas.join(unit), &["AAPL"]);
    }
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    // Simulate the mount vanishing between the run-start guard and the per-unit exports by making
    // the target root unwritable-and-gone right after the guard would have passed.
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Verified);
    fs::remove_dir_all(&target).unwrap();

    // A subsequent run sees no mount at all: every unit degrades and nothing is created.
    let second = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(second.verdict(), BackupVerdict::Unverified);
    assert!(second
        .units
        .iter()
        .all(|u| u.status == TargetStatus::Degraded));
    assert!(second.verified_units().is_empty());
    assert!(
        !target.exists(),
        "no code path may recreate the external mount locally"
    );

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_backtest_export_must_equal_the_source_bytes_not_merely_be_well_formed() {
    // Envelope validity is not identity: a DIFFERENT but perfectly well-formed backtest blob would
    // pass the envelope check. Since the export writes the source bytes verbatim, byte equality is
    // the universal proof of a faithful copy — and it is the only one available for a codec that
    // lives in a higher layer.
    let root = scratch("bt-identity");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_backtest_results(&nas.join("backtests"), "run-1\n");

    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(backtest_validator());
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );

    // Replace the ARCHIVE with a different, still perfectly well-formed backtest blob.
    seed_backtest_results(&target.join("backtests"), "run-2-totally-different\n");
    let archived = fs::read(target.join("backtests").join(BACKTEST_STORE_FILENAME)).unwrap();

    // Verifying the archive in isolation passes — it IS well formed. That is precisely why the
    // export path additionally requires equality with the source.
    let names = discover_unit_names(&nas).unwrap();
    let validator = backtest_validator();
    assert!(verify_archive(&target, &names, Some(&validator))
        .unwrap()
        .verdict()
        .is_verified());

    // Re-running the export overwrites it with the real source bytes and verifies.
    let second = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(second.verdict(), BackupVerdict::Verified);
    let now_archived = fs::read(target.join("backtests").join(BACKTEST_STORE_FILENAME)).unwrap();
    assert_ne!(now_archived, archived);
    assert_eq!(
        now_archived,
        fs::read(nas.join("backtests").join(BACKTEST_STORE_FILENAME)).unwrap(),
        "the archived backtest blob must be byte-identical to the NAS source"
    );
    assert_eq!(
        second.units[0].verification,
        VerificationDepth::RecordLevel,
        "with the owning codec wired in this is a real decode"
    );

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn an_injected_backtest_validator_closes_the_foreign_codec_gap() {
    // Without a validator this crate can only prove a backtest blob's envelope + byte-identity with
    // the source, because the owning codec lives in atp-simulation (a higher layer). The seam lets
    // a caller that CAN reach that codec supply it, so the gap is wireable rather than permanent.
    let root = scratch("bt-validator");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_backtest_results(&nas.join("backtests"), "run-1\n");

    // A validator standing in for atp-simulation's decoder: it rejects bodies without a run id.
    let strict: ForeignCodecValidator = std::sync::Arc::new(|text: &str| {
        if text.contains("run-") {
            Ok(())
        } else {
            Err("no run id in backtest body".to_string())
        }
    });

    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(strict.clone());
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Verified);
    assert_eq!(
        report.units[0].verification,
        VerificationDepth::RecordLevel,
        "with the owning codec wired in, the depth is a real decode"
    );

    // A blob the injected codec rejects is Corrupt, not silently blessed by the envelope alone.
    seed_backtest_results(&nas.join("backtests"), "no-identifier-here\n");
    let second = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(second.verdict(), BackupVerdict::Corrupt);
    assert!(
        second.units[0].detail.contains("backtest codec rejected"),
        "detail should name the owning codec: {}",
        second.units[0].detail
    );

    // Without the validator this layer cannot prove restorability, so it fails CLOSED: Unverified
    // (not Corrupt — the blob may well be fine; we simply cannot say).
    seed_backtest_results(&nas.join("backtests"), "run-3\n");
    let lenient = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let third = run_backup(&lenient, NOW + 2 * SECONDS_PER_DAY).unwrap();
    assert_eq!(third.verdict(), BackupVerdict::Unverified);
    assert_eq!(third.units[0].verdict, BackupVerdict::Unverified);
    assert_eq!(third.units[0].verification, VerificationDepth::EnvelopeOnly);
    assert!(third.units[0].detail.contains("UNPROVEN"));

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_target_root_removed_between_the_guard_and_the_write_is_not_recreated() {
    // The run-start guard alone is not enough: `create_dir_all` on a unit's parent path creates
    // INTERMEDIATE directories, so a mount that vanishes after the guard would be recreated on
    // local disk and the export would verify against a copy inside the source's failure domain.
    // Simulate the vanish precisely: seed a unit whose export is attempted with the root gone.
    let root = scratch("root-vanishes");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    // Prove the happy path first, then remove the mount and re-run.
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );
    fs::remove_dir_all(&target).unwrap();

    let report = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Unverified);
    assert!(report.verified_units().is_empty());
    assert!(
        !target.exists(),
        "no write path may recreate the external target root: {:?}",
        report.units
    );

    // The ledger write is also root-guarded, so it cannot resurrect the mount either.
    let mut ledger = BackupLedger::new();
    ledger.record(&report, &target).unwrap();
    assert!(!target.exists());

    let _ = fs::remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn a_target_unit_subdirectory_symlinked_into_the_nas_is_refused() {
    // The root-level guard passes here — `usb` really is outside `nas`. The hole is one level
    // down: `usb/equities` is a symlink INTO the NAS, so writing "the backup" would publish into
    // the source tree and, under concurrent ingestion, could overwrite newer NAS data while
    // reporting a verified backup.
    use std::os::unix::fs::symlink;

    let root = scratch("unit-symlink");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    let unit = nas.join("equities");
    seed_market_data(&unit, &["AAPL"]);
    let before = fs::read(unit.join(STORE_FILENAME)).unwrap();

    symlink(&unit, target.join("equities")).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();

    assert_eq!(
        report.verdict(),
        BackupVerdict::Unverified,
        "a unit path resolving into the source must not verify: {:?}",
        report.units
    );
    assert!(
        report.units[0].detail.contains("inside the NAS source")
            || report.units[0]
                .detail
                .contains("outside the backup target root"),
        "detail should name the resolved-path violation: {}",
        report.units[0].detail
    );
    assert_eq!(
        fs::read(unit.join(STORE_FILENAME)).unwrap(),
        before,
        "the NAS source must be byte-identical — nothing may be written through the symlink"
    );

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_corrupt_archived_unit_no_longer_on_the_nas_still_fails_verification() {
    // `restore` discovers and restores EVERY archived unit, so a stale blob left in the target —
    // one that is no longer on the NAS, and therefore not in the expected list — must not be
    // skipped by the status check. Otherwise status blesses an archive whose recovery then fails.
    let root = scratch("stale-extra");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_market_data(&nas.join("a"), &["AAPL"]);
    seed_market_data(&nas.join("b"), &["MSFT"]);

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );

    // `b` is removed from the NAS and its ARCHIVED copy rots.
    fs::remove_dir_all(nas.join("b")).unwrap();
    let stale = target.join("b").join(STORE_FILENAME);
    let text = fs::read_to_string(&stale).unwrap();
    fs::write(&stale, text.replace("MSFT", "MSFX")).unwrap();

    let current = discover_unit_names(&nas).unwrap();
    assert_eq!(current, vec!["a/market_data.store".to_string()]);

    let archive = verify_archive(&target, &current, None).unwrap();
    assert_eq!(
        archive.verdict(),
        BackupVerdict::Corrupt,
        "a corrupt extra unit must not be hidden by a green status: {:?}",
        archive.units
    );
    assert!(
        archive
            .units
            .iter()
            .any(|u| u.unit == "b/market_data.store"),
        "the extra archived unit must appear in the report: {:?}",
        archive.units
    );

    // And restore — which would hit it for real — agrees.
    assert_eq!(
        restore(&target, &root.join("dest"), None)
            .unwrap()
            .verdict(),
        BackupVerdict::Corrupt
    );

    let _ = fs::remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn an_archived_store_file_that_is_a_symlink_never_verifies() {
    // `is_file()` and `read_to_string` both follow symlinks, so an archive entry that is really a
    // link back into the NAS reads and checksums perfectly while holding no bytes of its own — a
    // "backup" that dies with the thing it was supposed to survive.
    use std::os::unix::fs::symlink;

    let root = scratch("archive-symlink");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    let unit = nas.join("equities");
    seed_market_data(&unit, &["AAPL"]);

    // Hand-build an archive whose store "file" is a link into the NAS.
    let archived_dir = target.join("equities");
    fs::create_dir_all(&archived_dir).unwrap();
    symlink(unit.join(STORE_FILENAME), archived_dir.join(STORE_FILENAME)).unwrap();

    let current = discover_unit_names(&nas).unwrap();
    let archive = verify_archive(&target, &current, None).unwrap();
    assert_eq!(
        archive.verdict(),
        BackupVerdict::Unverified,
        "a symlinked archive entry must not verify: {:?}",
        archive.units
    );
    assert!(
        archive.units[0].detail.contains("symlink"),
        "detail should name the symlink: {}",
        archive.units[0].detail
    );

    // Recovery agrees — it will not treat the link as a recoverable copy.
    let restored = restore(&target, &root.join("dest"), None).unwrap();
    assert_ne!(restored.verdict(), BackupVerdict::Verified);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_locked_run_does_export_verify_and_ledger_advance_as_one_unit() {
    let root = scratch("locked-run");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    let (report, ledger) = run_backup_locked(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Verified);
    assert_eq!(
        ledger.newest_per_unit().get("equities/market_data.store"),
        Some(&NOW),
        "the returned ledger already reflects this run"
    );
    // ...and it is durable, not just in memory.
    let reloaded = BackupLedger::load(&target).unwrap();
    let current = discover_unit_names(&nas).unwrap();
    assert!(rpo_report(&reloaded, &current, NOW).within_rpo);
    // The lock is released on drop, so a second run works.
    assert_eq!(
        run_backup_locked(&config, NOW + SECONDS_PER_DAY)
            .unwrap()
            .0
            .verdict(),
        BackupVerdict::Verified
    );

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_concurrent_run_is_refused_rather_than_interleaved() {
    // Two runs must not interleave: an older one could publish staler bytes over a newer archive
    // while the newer ledger timestamp survives, giving a confident within-RPO status for data that
    // is no longer there. The second holder fails closed.
    let root = scratch("locked-conflict");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    // Hold the target lock the way a concurrent run would.
    let held = atp_data::store::StoreLock::acquire(&target).unwrap();
    let blocked = run_backup_locked(&config, NOW);
    assert!(
        matches!(blocked, Err(BackupError::Store(_))),
        "a concurrent run must be refused, got {blocked:?}"
    );
    assert!(
        !target.join("equities").exists(),
        "the refused run must not have published anything"
    );

    drop(held);
    assert_eq!(
        run_backup_locked(&config, NOW).unwrap().0.verdict(),
        BackupVerdict::Verified,
        "once the holder releases, the run proceeds"
    );

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn the_source_is_never_mutated_by_a_backup() {
    let root = scratch("readonly");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let before = fs::read(nas.join("equities").join(STORE_FILENAME)).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    run_backup(&config, NOW).unwrap();

    let after = fs::read(nas.join("equities").join(STORE_FILENAME)).unwrap();
    assert_eq!(before, after, "a backup must never rewrite its source");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn an_empty_nas_root_reports_unverified_not_a_vacuous_success() {
    let root = scratch("empty");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    fs::create_dir_all(&nas).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();
    assert!(report.units.is_empty());
    assert_eq!(
        report.verdict(),
        BackupVerdict::Unverified,
        "a misconfigured source path must not report a green backup forever"
    );
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_missing_nas_root_fails_closed() {
    let root = scratch("missing");
    let config = BackupConfig::with_default_cadence(root.join("absent"), root.join("usb")).unwrap();
    assert!(matches!(
        run_backup(&config, NOW),
        Err(BackupError::Io { .. })
    ));
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_target_sharing_the_sources_failure_domain_is_refused_at_run_time_too() {
    let root = scratch("nested");
    let nas = root.join("nas");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    // Construction already refuses this; assert the guard exists rather than relying on config.
    assert!(matches!(
        BackupConfig::new(&nas, nas.join("inner-backup"), 7),
        Err(BackupError::TargetSharesFailureDomain { .. })
    ));
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// AC 2 — backup completion validates integrity
// ------------------------------------------------------------------------- //

#[test]
fn a_single_flipped_byte_in_the_target_is_caught_by_verification() {
    let root = scratch("corrupt-target");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );

    // Corrupt the ARCHIVE after the fact, then re-verify through the recovery path.
    let archived = target.join("equities").join(STORE_FILENAME);
    let text = fs::read_to_string(&archived).unwrap();
    fs::write(&archived, text.replace("AAPL", "AAPX")).unwrap();

    let dest = root.join("restore");
    let report = restore(&target, &dest, None).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Corrupt,
        "a checksum-breaking edit must be caught, not restored: {:?}",
        report.units
    );
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_corrupt_source_is_refused_rather_than_replicated_and_declared_verified() {
    let root = scratch("corrupt-source");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let unit_dir = nas.join("equities");
    seed_market_data(&unit_dir, &["AAPL"]);
    // Break the source blob's checksum.
    let path = unit_dir.join(STORE_FILENAME);
    let text = fs::read_to_string(&path).unwrap();
    fs::write(&path, text.replace("AAPL", "AAPX")).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Corrupt);
    let unit = &report.units[0];
    assert_eq!(unit.verdict, BackupVerdict::Corrupt);
    assert!(
        unit.detail.contains("source unit is corrupt"),
        "detail should name the source as the fault: {}",
        unit.detail
    );
    // ...and nothing was written to the target, so a corrupt blob cannot displace a good archive.
    assert!(!target.join("equities").join(STORE_FILENAME).exists());
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_truncated_archive_unit_fails_verification() {
    let root = scratch("truncated");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    run_backup(&config, NOW).unwrap();

    let archived = target.join("equities").join(STORE_FILENAME);
    let text = fs::read_to_string(&archived).unwrap();
    fs::write(&archived, &text[..text.len() / 2]).unwrap();

    let report = restore(&target, &root.join("restore"), None).unwrap();
    assert_ne!(report.verdict(), BackupVerdict::Verified);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn only_verified_units_advance_the_ledger() {
    let root = scratch("ledger-gate");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("good"), &["AAPL"]);
    let bad_dir = nas.join("bad");
    seed_market_data(&bad_dir, &["MSFT"]);
    let bad_path = bad_dir.join(STORE_FILENAME);
    let bad_text = fs::read_to_string(&bad_path).unwrap();
    fs::write(&bad_path, bad_text.replace("MSFT", "MSFX")).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Corrupt);

    let mut ledger = BackupLedger::new();
    ledger.record(&report, &target).unwrap();
    let reloaded = BackupLedger::load(&target).unwrap();
    let units: Vec<&str> = reloaded.entries().iter().map(|e| e.unit.as_str()).collect();
    assert_eq!(
        units,
        vec!["good/market_data.store"],
        "a corrupt unit must never be recorded as backed up"
    );
    assert!(target.join(BACKUP_LEDGER_FILENAME).is_file());
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// AC 3 — documented RPO is no more than 7 days
// ------------------------------------------------------------------------- //

#[test]
fn rpo_is_proven_only_by_a_verified_backup_and_expires_after_seven_days() {
    let root = scratch("rpo");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    // The units currently on the NAS — what the RPO must be judged against.
    let current = discover_unit_names(&nas).unwrap();
    assert_eq!(current, vec!["equities/market_data.store".to_string()]);

    // Before any run: not within RPO, and a backup is due.
    let empty = BackupLedger::load(&target).unwrap();
    assert!(!rpo_report(&empty, &current, NOW).within_rpo);
    assert!(due(&empty, &config, &current, NOW));

    let report = run_backup(&config, NOW).unwrap();
    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger.record(&report, &target).unwrap();

    let fresh = BackupLedger::load(&target).unwrap();
    let now_rpo = rpo_report(&fresh, &current, NOW);
    assert!(now_rpo.within_rpo);
    assert_eq!(now_rpo.age_days, Some(0));
    assert_eq!(now_rpo.ceiling_days, RPO_MAX_DAYS);
    assert!(now_rpo.unbacked_units.is_empty());
    assert!(!due(&fresh, &config, &current, NOW));

    // Exactly at the ceiling it still holds; one day later it does not.
    let at_ceiling = NOW + i64::from(RPO_MAX_DAYS) * SECONDS_PER_DAY;
    assert!(rpo_report(&fresh, &current, at_ceiling).within_rpo);
    assert!(due(&fresh, &config, &current, at_ceiling));
    assert!(!rpo_report(&fresh, &current, at_ceiling + SECONDS_PER_DAY).within_rpo);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn the_stalest_unit_determines_the_rpo() {
    let root = scratch("stalest");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    // An old verified run for one unit, a fresh one for another.
    let mut ledger = BackupLedger::new();
    let old = NOW - 30 * SECONDS_PER_DAY;
    ledger
        .record(&run_backup(&config, old).unwrap(), &target)
        .unwrap();
    seed_market_data(&nas.join("options"), &["SPY"]);
    ledger
        .record(&run_backup(&config, NOW).unwrap(), &target)
        .unwrap();

    let reloaded = BackupLedger::load(&target).unwrap();
    // "equities" was re-verified at NOW too, so the archive is fresh; the point is that the
    // assessment reads the OLDEST per-unit high-water mark, not the newest entry overall.
    let per_unit = reloaded.newest_per_unit();
    assert_eq!(per_unit.get("equities/market_data.store"), Some(&NOW));
    assert_eq!(per_unit.get("options/market_data.store"), Some(&NOW));
    let current = discover_unit_names(&nas).unwrap();
    assert!(rpo_report(&reloaded, &current, NOW).within_rpo);

    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regression (r4): a partially-failed run must not read green
// ------------------------------------------------------------------------- //

#[test]
fn a_mixed_good_and_corrupt_run_leaves_the_archive_out_of_rpo_and_still_due() {
    // The false green: one unit verifies, another is corrupt. The good unit lands in the ledger,
    // and a ledger-only RPO check would then report "backed up 0 days ago" and no backup due —
    // while the corrupt unit has never been exported at all.
    let root = scratch("mixed-run");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("good"), &["AAPL"]);
    let bad_dir = nas.join("bad");
    seed_market_data(&bad_dir, &["MSFT"]);
    let bad_path = bad_dir.join(STORE_FILENAME);
    let bad_text = fs::read_to_string(&bad_path).unwrap();
    fs::write(&bad_path, bad_text.replace("MSFT", "MSFX")).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Corrupt);

    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger.record(&report, &target).unwrap();
    let reloaded = BackupLedger::load(&target).unwrap();

    // The ledger legitimately holds the good unit...
    assert_eq!(
        reloaded.newest_per_unit().get("good/market_data.store"),
        Some(&NOW)
    );
    // ...but the assessment is over the units actually on the NAS, so `bad` is a visible breach.
    let current = discover_unit_names(&nas).unwrap();
    let rpo = rpo_report(&reloaded, &current, NOW);
    assert!(
        !rpo.within_rpo,
        "a run that failed on one unit must not certify the archive: {rpo:?}"
    );
    assert_eq!(
        rpo.unbacked_units,
        vec!["bad/market_data.store".to_string()]
    );
    assert_eq!(rpo.verified_at, None);
    assert!(
        due(&reloaded, &config, &current, NOW),
        "a backup must stay due while a unit is unprotected"
    );

    // ...and it is STILL out of RPO a day later, rather than ageing into compliance.
    assert!(!rpo_report(&reloaded, &current, NOW + SECONDS_PER_DAY).within_rpo);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn fixing_the_corrupt_unit_and_re_running_restores_the_rpo() {
    // The other half of the invariant: once every unit is genuinely backed up, the archive is
    // green. A guard that could never be satisfied would be useless.
    let root = scratch("mixed-recover");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("good"), &["AAPL"]);
    let bad_dir = nas.join("bad");
    seed_market_data(&bad_dir, &["MSFT"]);
    let bad_path = bad_dir.join(STORE_FILENAME);
    let good_bytes = fs::read(&bad_path).unwrap();
    let bad_text = fs::read_to_string(&bad_path).unwrap();
    fs::write(&bad_path, bad_text.replace("MSFT", "MSFX")).unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger
        .record(&run_backup(&config, NOW).unwrap(), &target)
        .unwrap();

    // Repair the source and re-run.
    fs::write(&bad_path, good_bytes).unwrap();
    let second = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(second.verdict(), BackupVerdict::Verified);
    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger.record(&second, &target).unwrap();

    let reloaded = BackupLedger::load(&target).unwrap();
    let current = discover_unit_names(&nas).unwrap();
    let rpo = rpo_report(&reloaded, &current, NOW + SECONDS_PER_DAY);
    assert!(rpo.within_rpo, "{rpo:?}");
    assert!(rpo.unbacked_units.is_empty());
    assert!(!due(&reloaded, &config, &current, NOW + SECONDS_PER_DAY));

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_unit_that_appears_after_a_green_run_drops_the_archive_out_of_rpo() {
    // A complete run over {equities}, then a new unit shows up on the NAS. Gating the ledger on
    // whole-run success would NOT catch this; judging against the current unit set does.
    let root = scratch("new-unit");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger
        .record(&run_backup(&config, NOW).unwrap(), &target)
        .unwrap();
    let reloaded = BackupLedger::load(&target).unwrap();
    let before = discover_unit_names(&nas).unwrap();
    assert!(rpo_report(&reloaded, &before, NOW).within_rpo);

    // A new data family lands on the NAS and has never been backed up.
    seed_market_data(&nas.join("options"), &["SPY"]);
    let after = discover_unit_names(&nas).unwrap();
    let rpo = rpo_report(&reloaded, &after, NOW + SECONDS_PER_DAY);
    assert!(
        !rpo.within_rpo,
        "a never-backed-up unit is a breach: {rpo:?}"
    );
    assert_eq!(
        rpo.unbacked_units,
        vec!["options/market_data.store".to_string()]
    );
    assert!(due(&reloaded, &config, &after, NOW + SECONDS_PER_DAY));

    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regressions (r6): the archive must be re-read, not assumed
// ------------------------------------------------------------------------- //

#[test]
fn a_wiped_archive_stops_verifying_even_though_the_ledger_still_says_it_was_backed_up() {
    // The ledger records that a unit verified ONCE. It is not evidence about the media now: an
    // external drive can be wiped or unmounted between runs. Skipping a not-yet-due backup on the
    // strength of a stale ledger would give an infinite RPO behind a green status.
    let root = scratch("wiped");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();

    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger
        .record(&run_backup(&config, NOW).unwrap(), &target)
        .unwrap();
    let current = discover_unit_names(&nas).unwrap();

    // The ledger-based view still looks fresh one day later...
    let reloaded = BackupLedger::load(&target).unwrap();
    assert!(rpo_report(&reloaded, &current, NOW + SECONDS_PER_DAY).within_rpo);
    assert!(!due(&reloaded, &config, &current, NOW + SECONDS_PER_DAY));

    // ...but the archive itself is gone, and re-reading the media says so.
    fs::remove_dir_all(target.join("equities")).unwrap();
    let archive = verify_archive(&target, &current, None).unwrap();
    assert_eq!(
        archive.verdict(),
        BackupVerdict::Unverified,
        "a missing archived unit must not verify: {:?}",
        archive.units
    );
    assert!(archive.units[0].detail.contains("absent"));

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn verify_archive_writes_nothing() {
    let root = scratch("verify-readonly");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    run_backup(&config, NOW).unwrap();

    let before: Vec<_> = {
        let mut v: Vec<_> = fs::read_dir(&target)
            .unwrap()
            .flatten()
            .map(|e| e.file_name())
            .collect();
        v.sort();
        v
    };
    let current = discover_unit_names(&nas).unwrap();
    assert!(verify_archive(&target, &current, None)
        .unwrap()
        .verdict()
        .is_verified());
    let after: Vec<_> = {
        let mut v: Vec<_> = fs::read_dir(&target)
            .unwrap()
            .flatten()
            .map(|e| e.file_name())
            .collect();
        v.sort();
        v
    };
    assert_eq!(before, after, "verification must not touch the archive");
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_corrupted_archive_fails_verify_archive_at_record_level() {
    let root = scratch("verify-corrupt");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    run_backup(&config, NOW).unwrap();

    let archived = target.join("equities").join(STORE_FILENAME);
    let text = fs::read_to_string(&archived).unwrap();
    fs::write(&archived, text.replace("AAPL", "AAPX")).unwrap();

    let current = discover_unit_names(&nas).unwrap();
    let archive = verify_archive(&target, &current, None).unwrap();
    assert_eq!(archive.verdict(), BackupVerdict::Corrupt);
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regression (r7): evidence strength is reported honestly
// ------------------------------------------------------------------------- //

#[test]
fn backtest_units_without_a_validator_are_unverified_not_verified() {
    // The fail-closed rule. This crate can prove a backtest blob's envelope and its byte-identity
    // with the NAS source, but NOT that `atp-simulation` could restore it — that codec lives in a
    // higher layer. Reporting `Verified` on the weaker evidence would let an archive the simulation
    // layer cannot load advance the RPO ledger and exit a `restore` successfully. So without an
    // injected validator the verdict is `Unverified` ("could not be checked"), never `Corrupt`
    // (which would wrongly accuse an intact blob), and the depth label says which evidence was got.
    let root = scratch("depth");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    seed_backtest_results(&nas.join("backtests"), "run-1\n");

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Unverified,
        "an unvalidatable unit must hold the whole run back: {:?}",
        report.units
    );

    let equities = report
        .units
        .iter()
        .find(|u| u.unit == "equities/market_data.store")
        .unwrap();
    let backtests = report
        .units
        .iter()
        .find(|u| u.unit == "backtests/backtest_results.store")
        .unwrap();
    // The market-data unit still verifies on its own evidence...
    assert_eq!(equities.verdict, BackupVerdict::Verified);
    assert_eq!(equities.verification, VerificationDepth::RecordLevel);
    // ...while the backtest unit is honestly unproven, with the reason spelled out.
    assert_eq!(backtests.verdict, BackupVerdict::Unverified);
    assert_eq!(backtests.verification, VerificationDepth::EnvelopeOnly);
    assert!(backtests.detail.contains("UNPROVEN"));
    assert_eq!(equities.verification.label(), "record-level");
    assert_eq!(backtests.verification.label(), "envelope-only");

    // A unit that did not verify carries no verification depth at all.
    let bad_dir = nas.join("broken");
    seed_market_data(&bad_dir, &["MSFT"]);
    let bad_path = bad_dir.join(STORE_FILENAME);
    let bad_text = fs::read_to_string(&bad_path).unwrap();
    fs::write(&bad_path, bad_text.replace("MSFT", "MSFX")).unwrap();
    let second = run_backup(&config, NOW).unwrap();
    let broken = second
        .units
        .iter()
        .find(|u| u.unit == "broken/market_data.store")
        .unwrap();
    assert_eq!(broken.verification, VerificationDepth::NotVerified);

    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regressions (r8): a failed export must not destroy the
// last good archive
// ------------------------------------------------------------------------- //

#[test]
fn a_codec_invalid_source_never_replaces_the_previous_verified_archive() {
    // The critical ordering bug: publish-then-check. A blob can pass the cheap envelope check and
    // still be rejected by the record codec; if it were written before that check, a corrupt source
    // would overwrite a perfectly good previous backup and merely *report* the failure afterwards.
    let root = scratch("no-clobber");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let unit_dir = nas.join("equities");
    seed_market_data(&unit_dir, &["AAPL", "MSFT"]);

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );
    let good_archive = fs::read(target.join("equities").join(STORE_FILENAME)).unwrap();

    // Replace the source with a blob that has a VALID envelope but a body the codec rejects.
    let body = "wholly-invalid-store-body\n";
    let checksum = {
        const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut hash = OFFSET_BASIS;
        for &byte in body.as_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
        hash
    };
    fs::write(
        unit_dir.join(STORE_FILENAME),
        format!("ATP-MARKET-DATA-STORE\n{}\n{body}", i128::from(checksum)),
    )
    .unwrap();

    let report = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(report.verdict(), BackupVerdict::Corrupt);

    assert_eq!(
        fs::read(target.join("equities").join(STORE_FILENAME)).unwrap(),
        good_archive,
        "a failed export must leave the previous verified archive byte-identical"
    );
    // ...and the previous archive still verifies on its own terms.
    let archive =
        verify_archive(&target, &["equities/market_data.store".to_string()], None).unwrap();
    assert!(archive.verdict().is_verified(), "{:?}", archive.units);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_failed_export_leaves_no_scratch_files_behind_in_the_archive() {
    let root = scratch("no-scratch");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let unit_dir = nas.join("equities");
    seed_market_data(&unit_dir, &["AAPL"]);
    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    run_backup(&config, NOW).unwrap();

    let path = unit_dir.join(STORE_FILENAME);
    let text = fs::read_to_string(&path).unwrap();
    fs::write(&path, text.replace("AAPL", "AAPX")).unwrap();
    run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();

    let leftovers: Vec<_> = fs::read_dir(target.join("equities"))
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".tmp"))
        .collect();
    assert!(
        leftovers.is_empty(),
        "scratch files left behind: {leftovers:?}"
    );
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regression (r9): two store kinds in ONE directory
// ------------------------------------------------------------------------- //

#[test]
fn two_store_kinds_in_one_directory_are_tracked_as_distinct_units() {
    // Keying a unit by its directory alone would collapse these two blobs into one ledger entry,
    // so a verified market-data export would silently vouch for a backtest blob that failed.
    let root = scratch("same-dir");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let shared = nas.join("shared");
    seed_market_data(&shared, &["AAPL"]);
    seed_backtest_results(&shared, "run-1\n");

    let names = discover_unit_names(&nas).unwrap();
    assert_eq!(
        names,
        vec![
            "shared/backtest_results.store".to_string(),
            "shared/market_data.store".to_string(),
        ],
        "both blobs in one directory must have distinct identities"
    );

    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(backtest_validator());
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(report.units.len(), 2);
    assert_eq!(report.verdict(), BackupVerdict::Verified);
    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger.record(&report, &target).unwrap();

    // Corrupt ONLY the backtest blob; the market-data unit must not cover for it.
    let bt = shared.join(BACKTEST_STORE_FILENAME);
    // Break it in a way the injected codec rejects (no run id), not merely a different valid body.
    seed_backtest_results(&shared, "no-identifier\n");
    let _ = &bt;

    let second = run_backup(&config, NOW + SECONDS_PER_DAY).unwrap();
    assert_eq!(second.verdict(), BackupVerdict::Corrupt);

    let mut ledger = BackupLedger::load(&target).unwrap();
    ledger.record(&second, &target).unwrap();
    let reloaded = BackupLedger::load(&target).unwrap();
    let current = discover_unit_names(&nas).unwrap();
    let rpo = rpo_report(&reloaded, &current, NOW + SECONDS_PER_DAY);
    assert_eq!(
        rpo.unbacked_units,
        Vec::<String>::new(),
        "the backtest unit WAS backed up at NOW, so it is not unbacked — just stale"
    );
    assert_eq!(
        reloaded
            .newest_per_unit()
            .get("shared/backtest_results.store"),
        Some(&NOW),
        "the failed re-export must not advance the backtest unit past its last good backup"
    );
    assert_eq!(
        reloaded.newest_per_unit().get("shared/market_data.store"),
        Some(&(NOW + SECONDS_PER_DAY)),
        "the market-data unit re-verified and advances independently"
    );

    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regression (r10): restore must not write into the archive
// ------------------------------------------------------------------------- //

#[test]
fn restoring_into_a_directory_inside_the_archive_is_refused() {
    let root = scratch("restore-nested");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&target.join("equities"), &["AAPL"]);
    let dest = target.join("recovered");

    let err = restore(&target, &dest, None).unwrap_err();
    assert!(
        matches!(err, BackupError::TargetSharesFailureDomain { .. }),
        "recovery must not write into the archive it is reading, got {err:?}"
    );
    assert!(
        !dest.exists(),
        "nothing may be written before the guard fires"
    );
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Validated recovery
// ------------------------------------------------------------------------- //

#[test]
fn restore_reproduces_every_archived_unit_and_proves_the_record_set() {
    let root = scratch("restore");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let dest = root.join("recovered");
    seed_market_data(&nas.join("equities"), &["AAPL", "MSFT"]);
    seed_backtest_results(&nas.join("backtests"), "run-1\n");

    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(backtest_validator());
    run_backup(&config, NOW).unwrap();

    let validator = backtest_validator();
    let report = restore(&target, &dest, Some(&validator)).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Verified,
        "{:?}",
        report.units
    );
    assert_eq!(report.units.len(), 2);

    // The recovered market-data store holds the same records the source did.
    let recovered = MarketDataStore::load_from_path(&dest.join("equities")).unwrap();
    let original = MarketDataStore::load_from_path(&nas.join("equities")).unwrap();
    assert_eq!(recovered.len(), 2);
    assert_eq!(recovered.serialize(), original.serialize());

    // The foreign-codec unit is byte-identical.
    assert_eq!(
        fs::read(dest.join("backtests").join(BACKTEST_STORE_FILENAME)).unwrap(),
        fs::read(nas.join("backtests").join(BACKTEST_STORE_FILENAME)).unwrap()
    );
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn restoring_onto_the_archive_itself_is_refused() {
    let root = scratch("restore-alias");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&target.join("equities"), &["AAPL"]);
    assert!(matches!(
        restore(&target, &target, None),
        Err(BackupError::TargetNotDistinct { .. })
    ));
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regressions (r1): envelope-valid but codec-invalid
// ------------------------------------------------------------------------- //

#[test]
fn an_envelope_valid_but_codec_invalid_archive_is_corrupt_not_verified() {
    // A blob can carry an intact magic + checksum and still be rejected by the record codec. The
    // envelope check alone would bless it; `verify`/`restore` must not.
    let root = scratch("codec-invalid");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let unit = target.join("equities");
    fs::create_dir_all(&unit).unwrap();

    // A structurally-valid envelope whose body the market-data codec cannot decode.
    let body = "not-a-valid-store-body\n";
    let checksum = {
        const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut hash = OFFSET_BASIS;
        for &byte in body.as_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
        hash
    };
    let magic = "ATP-MARKET-DATA-STORE";
    fs::write(
        unit.join(STORE_FILENAME),
        format!("{magic}\n{}\n{body}", i128::from(checksum)),
    )
    .unwrap();

    let report = restore(&target, &root.join("dest"), None).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Corrupt,
        "an archive the real store codec rejects must never verify: {:?}",
        report.units
    );
    assert!(
        report.units[0].detail.contains("record codec"),
        "detail should name the codec as the fault: {}",
        report.units[0].detail
    );
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regressions (r2): an unreadable NAS subtree
// ------------------------------------------------------------------------- //

#[cfg(unix)]
#[test]
fn an_unreadable_nas_subtree_fails_the_run_rather_than_backing_up_only_what_is_readable() {
    use std::os::unix::fs::PermissionsExt;

    let root = scratch("unreadable");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let locked = nas.join("locked");
    seed_market_data(&locked, &["MSFT"]);

    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read_dir(&locked).is_ok() {
        // Running as root (or on a filesystem ignoring the mode) — the precondition cannot be set
        // up, so this case is untestable here rather than silently passing.
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();
        let _ = fs::remove_dir_all(&root);
        return;
    }

    let config = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let outcome = run_backup(&config, NOW);
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o755)).unwrap();

    assert!(
        matches!(outcome, Err(BackupError::Io { .. })),
        "an unreadable NAS subtree must fail the run: backing up only the readable part and \
         reporting `verified` would advance the RPO ledger over data that was never exported — \
         got {outcome:?}"
    );
    let _ = fs::remove_dir_all(&root);
}

// ------------------------------------------------------------------------- //
// Adversarial-review regressions (r3): a symlinked target parent
// ------------------------------------------------------------------------- //

#[cfg(unix)]
#[test]
fn a_target_whose_parent_symlinks_into_the_nas_is_refused_before_anything_is_written() {
    use std::os::unix::fs::symlink;

    let root = scratch("symlink-parent");
    let nas = root.join("nas");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let inside = nas.join("inside");
    fs::create_dir_all(&inside).unwrap();

    // `/link` points INTO the NAS tree; `/link/backup` does not exist yet, which is exactly the
    // state a first-run target is in — so canonicalizing only fully-existing paths would miss it.
    let link = root.join("link");
    symlink(&inside, &link).unwrap();
    let target = link.join("backup");
    assert!(!target.exists());

    let err = BackupConfig::new(&nas, &target, 7).unwrap_err();
    assert!(
        matches!(err, BackupError::TargetSharesFailureDomain { .. }),
        "a target reached through a symlinked parent still lives in the source's failure domain, \
         got {err:?}"
    );
    assert!(!target.exists(), "validation must not create the target");
    let _ = fs::remove_dir_all(&root);
}

#[cfg(unix)]
#[test]
fn a_target_whose_parent_symlinks_outside_the_nas_is_still_accepted() {
    // The guard must reject the failure-domain overlap, not symlinks in general.
    use std::os::unix::fs::symlink;

    let root = scratch("symlink-ok");
    let nas = root.join("nas");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let elsewhere = root.join("elsewhere");
    fs::create_dir_all(&elsewhere).unwrap();
    let link = root.join("usb-link");
    symlink(&elsewhere, &link).unwrap();

    let mounted = link.join("backup");
    fs::create_dir_all(&mounted).unwrap(); // the external target is "mounted"
    let config = BackupConfig::new(&nas, &mounted, 7).unwrap();
    assert_eq!(
        run_backup(&config, NOW).unwrap().verdict(),
        BackupVerdict::Verified
    );
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_codec_invalid_archive_never_overwrites_a_good_restored_file() {
    // Publish-ordering, recovery side: an envelope-valid but codec-invalid archive must be rejected
    // BEFORE it is written, or a failed restore would destroy a good previously-recovered copy.
    let root = scratch("restore-no-clobber");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    let dest = root.join("recovered");

    // A good recovered copy already exists at the destination.
    seed_market_data(&dest.join("equities"), &["AAPL", "MSFT"]);
    let good_dest = fs::read(dest.join("equities").join(STORE_FILENAME)).unwrap();

    // The archive has a valid envelope but a body the record codec rejects.
    let unit = target.join("equities");
    fs::create_dir_all(&unit).unwrap();
    let body = "wholly-invalid-store-body\n";
    let checksum = {
        const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut hash = OFFSET_BASIS;
        for &byte in body.as_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
        hash
    };
    fs::write(
        unit.join(STORE_FILENAME),
        format!("ATP-MARKET-DATA-STORE\n{}\n{body}", i128::from(checksum)),
    )
    .unwrap();

    let report = restore(&target, &dest, None).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Corrupt,
        "{:?}",
        report.units
    );
    assert_eq!(
        fs::read(dest.join("equities").join(STORE_FILENAME)).unwrap(),
        good_dest,
        "a failed restore must leave the existing recovered file byte-identical"
    );
    let leftovers: Vec<_> = fs::read_dir(dest.join("equities"))
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".tmp"))
        .collect();
    assert!(leftovers.is_empty(), "scratch left behind: {leftovers:?}");

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn restoring_an_empty_archive_is_unverified_not_a_silent_success() {
    let root = scratch("restore-empty");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap(); // the external target is "mounted"
    fs::create_dir_all(&target).unwrap();
    let report = restore(&target, &root.join("dest"), None).unwrap();
    assert!(report.units.is_empty());
    assert_eq!(report.verdict(), BackupVerdict::Unverified);
    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_unit_missing_from_the_archive_is_reported_under_its_own_codec() {
    // A unit that is expected but absent is known only by its id — there is no discovered file to
    // read a kind from. Defaulting those to market-data told the operator a missing
    // `backtest_results.store` was a market-data unit, i.e. named the wrong codec for the very
    // thing that is not there, and rendered the envelope-only unit exactly like a record-level one.
    let root = scratch("absent-unit-kind");
    let nas = root.join("nas");
    let target = root.join("usb");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    seed_backtest_results(&nas.join("backtests"), "run-momentum\n");
    fs::create_dir_all(&target).unwrap(); // mounted, but nothing has ever been exported to it

    let expected = discover_unit_names(&nas).unwrap();
    let report = verify_archive(&target, &expected, Some(&backtest_validator())).unwrap();

    let backtest = report
        .units
        .iter()
        .find(|u| u.unit.ends_with(BACKTEST_STORE_FILENAME))
        .expect("the absent backtest unit is reported");
    assert_eq!(backtest.kind, UnitKind::BacktestResults);
    let market = report
        .units
        .iter()
        .find(|u| u.unit.ends_with(STORE_FILENAME))
        .expect("the absent market-data unit is reported");
    assert_eq!(market.kind, UnitKind::MarketData);

    // The safety verdict is unchanged by the fix: absent is still Unverified, never a pass.
    assert!(report
        .units
        .iter()
        .all(|u| u.verdict == BackupVerdict::Unverified));
    assert_eq!(report.verdict(), BackupVerdict::Unverified);

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_successful_export_to_ordinary_local_media_reports_a_full_sync_barrier() {
    // The durability axis is reported SEPARATELY from the verdict, and on a normal filesystem it
    // must read `full-sync` — otherwise the barrier-less path below would not be distinguishable
    // from the ordinary one, and the warning would be noise an operator learns to ignore.
    let root = scratch("durability-fullsync");
    let nas = root.join("nas");
    let target = root.join("usb");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    fs::create_dir_all(&target).unwrap();

    let config = BackupConfig::new(&nas, &target, DEFAULT_CADENCE_DAYS).unwrap();
    let report = run_backup(&config, NOW).unwrap();

    assert_eq!(report.verdict(), BackupVerdict::Verified);
    assert_eq!(report.durability, Some(atp_data::SyncDurability::FullSync));

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn a_run_that_writes_nothing_reports_no_durability_at_all() {
    // `None` is a third state on purpose: an absent mount attempted no barrier, and rendering that
    // as either `full-sync` or `unsupported-by-target` would invent an observation never made.
    let root = scratch("durability-none");
    let nas = root.join("nas");
    seed_market_data(&nas.join("equities"), &["AAPL"]);
    let target = root.join("usb"); // never created: the external mount is absent

    let config = BackupConfig::new(&nas, &target, DEFAULT_CADENCE_DAYS).unwrap();
    let report = run_backup(&config, NOW).unwrap();

    assert_eq!(report.verdict(), BackupVerdict::Unverified);
    assert_eq!(report.durability, None);

    let _ = fs::remove_dir_all(&root);
}
