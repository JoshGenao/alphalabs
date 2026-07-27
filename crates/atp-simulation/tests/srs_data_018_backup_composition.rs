//! SRS-DATA-018 composition test: prove the backup engine, wired with the **real** backtest codec,
//! verifies a `backtest_results.store` to record-level depth.
//!
//! SYS-59 lists backtest results as one of the NAS families that must be backed up. Proving such a
//! blob is *restorable* needs `atp_simulation::backtest_store::BacktestResultStore::restore`, which
//! `atp-data` must not depend on (it is the lower layer). `atp_data::backup` therefore fails closed
//! — a backtest unit with no injected validator is `Unverified`, never `Verified`, because envelope
//! integrity alone cannot prove the simulation layer could load it.
//!
//! `data018_backup_cli` lives in THIS crate for exactly that reason: it is the composition root
//! that injects the real decoder. This test pins that composition, so a future refactor that drops
//! the validator (or moves the CLI back down a layer) fails here rather than silently reverting the
//! operator surface to envelope-only evidence.

use std::fs;
use std::path::PathBuf;
use std::sync::Arc;

use atp_data::backup::{
    run_backup, BackupConfig, BackupVerdict, ForeignCodecValidator, VerificationDepth,
    BACKTEST_STORE_FILENAME,
};
use atp_simulation::backtest_store::BacktestResultStore;

const NOW: i64 = 1_700_000_000;

/// The same validator `data018_backup_cli` installs: the real owning codec.
fn real_backtest_validator() -> ForeignCodecValidator {
    Arc::new(|text: &str| {
        BacktestResultStore::restore(text)
            .map(|_| ())
            .map_err(|err| err.to_string())
    })
}

fn scratch(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!(
        "atp-data018-composition-{}-{}",
        tag,
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    base
}

#[test]
fn a_real_backtest_store_verifies_at_record_level_through_the_injected_codec() {
    let root = scratch("real-codec");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();

    // A REAL backtest store written by its owning writer — not a hand-rolled envelope.
    let store = BacktestResultStore::new();
    store.save_to_path(&nas.join("backtests")).unwrap();
    assert!(nas
        .join("backtests")
        .join(BACKTEST_STORE_FILENAME)
        .is_file());

    // Without the validator: honestly Unverified — this is the fail-closed default.
    let bare = BackupConfig::with_default_cadence(&nas, &target).unwrap();
    let bare_report = run_backup(&bare, NOW).unwrap();
    assert_eq!(bare_report.verdict(), BackupVerdict::Unverified);
    assert_eq!(
        bare_report.units[0].verification,
        VerificationDepth::EnvelopeOnly
    );

    // With the real codec injected — exactly what data018_backup_cli does — it verifies for real.
    let wired = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(real_backtest_validator());
    let report = run_backup(&wired, NOW).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Verified,
        "{:?}",
        report.units
    );
    assert_eq!(
        report.units[0].verification,
        VerificationDepth::RecordLevel,
        "the owning codec was consulted, so the depth is a real decode"
    );
    assert!(target
        .join("backtests")
        .join(BACKTEST_STORE_FILENAME)
        .is_file());

    let _ = fs::remove_dir_all(&root);
}

#[test]
fn an_envelope_valid_blob_the_real_codec_rejects_is_corrupt_not_verified() {
    // The whole point of injecting the codec: a blob whose checksum is perfectly consistent but
    // whose body the simulation layer cannot load must not be certified as a good backup.
    let root = scratch("codec-rejects");
    let nas = root.join("nas");
    let target = root.join("usb");
    fs::create_dir_all(&target).unwrap();
    let unit = nas.join("backtests");
    fs::create_dir_all(&unit).unwrap();

    // Take a real blob and replace its BODY with garbage, recomputing the checksum so the envelope
    // stays valid — only the owning codec can tell this is unusable.
    let real = BacktestResultStore::new().serialize();
    let (magic, rest) = real.split_once('\n').unwrap();
    let (_checksum, _body) = rest.split_once('\n').unwrap();
    let garbage = "not-a-backtest-body\n";
    let checksum = {
        const OFFSET_BASIS: u64 = 0xcbf2_9ce4_8422_2325;
        const PRIME: u64 = 0x0000_0100_0000_01b3;
        let mut hash = OFFSET_BASIS;
        for &byte in garbage.as_bytes() {
            hash ^= u64::from(byte);
            hash = hash.wrapping_mul(PRIME);
        }
        hash
    };
    fs::write(
        unit.join(BACKTEST_STORE_FILENAME),
        format!("{magic}\n{}\n{garbage}", i128::from(checksum)),
    )
    .unwrap();

    let config = BackupConfig::with_default_cadence(&nas, &target)
        .unwrap()
        .with_backtest_validator(real_backtest_validator());
    let report = run_backup(&config, NOW).unwrap();
    assert_eq!(
        report.verdict(),
        BackupVerdict::Corrupt,
        "the real codec must reject it: {:?}",
        report.units
    );
    assert!(
        report.units[0].detail.contains("backtest codec rejected"),
        "detail should name the owning codec: {}",
        report.units[0].detail
    );
    assert!(
        !target
            .join("backtests")
            .join(BACKTEST_STORE_FILENAME)
            .exists(),
        "a source the owning codec rejects must never reach the archive"
    );

    let _ = fs::remove_dir_all(&root);
}
