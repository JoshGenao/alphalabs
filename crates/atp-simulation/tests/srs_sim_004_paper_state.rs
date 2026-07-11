//! Integration coverage for **SRS-SIM-004** — "persist paper strategy simulation
//! state" (SyRS SYS-89; StRS SN-1.29 / SN-2.05).
//!
//! These tests drive the persistence path end-to-end from *real* priced fills:
//! each fill is produced by [`PaperSimulationEngine::simulate_fill`] (the
//! SRS-BT-003 shared cost path) and applied to a [`VirtualLedgerBook`]
//! (SRS-SIM-003), then the book is captured, serialized, and restored. They assert
//! the SYS-89 safety core: a captured snapshot restores the ledger EXACTLY,
//! serialization is DETERMINISTIC (so a 60s checkpoint of unchanged state never
//! churns), per-strategy isolation and the cash-reconciliation invariant survive
//! the round trip, and a corrupt or tampered snapshot fails closed with no
//! partially-restored state.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use atp_simulation::paper_metrics::PaperMetricsAccumulator;
use atp_simulation::paper_state::{
    recover_from_path, restore, PaperStateSnapshot, PersistenceConfig, PersistenceError, MAGIC,
};
use atp_simulation::sim::{PaperFill, PaperSimulationEngine};
use atp_simulation::virtual_ledger::VirtualLedgerBook;
use atp_types::StrategyId;

/// A unique temp directory for a disk round-trip test (no `tempfile` dep; mirrors the
/// SRS-EXE-005 / SRS-EXE-009 tests' `<pid>-<seq>` scheme).
fn temp_dir(label: &str) -> std::path::PathBuf {
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("atp-sim004-{label}-{}-{seq}", std::process::id()))
}

fn sim(engine: &PaperSimulationEngine, ts: u64, symbol: &str, qty: i64, price: i64) -> PaperFill {
    engine.simulate_fill(ts, symbol, qty, price, None).unwrap()
}

/// A coherent accumulator: a fill-then-mark sequence per SYS-85.
fn sample_accumulator(engine: &PaperSimulationEngine) -> PaperMetricsAccumulator {
    let mut acc = PaperMetricsAccumulator::new(1_000_000).unwrap();
    acc.apply_fill(&sim(engine, 1, "AAPL", 100, 10_000))
        .unwrap();
    acc.mark(1, &[("AAPL".to_string(), 10_100)]).unwrap();
    acc.apply_fill(&sim(engine, 2, "AAPL", -50, 10_200))
        .unwrap();
    acc.mark(2, &[("AAPL".to_string(), 10_300)]).unwrap();
    acc
}

/// A full snapshot carrying all three persisted sub-states (ledger + metrics + user-state).
fn full_snapshot() -> PaperStateSnapshot {
    let engine = engine();
    let book = sample_book();
    let a = StrategyId::new("reservoir-a");
    let mut metrics: HashMap<StrategyId, PaperMetricsAccumulator> = HashMap::new();
    metrics.insert(a.clone(), sample_accumulator(&engine));
    let mut user_state: HashMap<StrategyId, String> = HashMap::new();
    user_state.insert(a, r#"{"regime":"trend","lookback":20}"#.to_string());
    PaperStateSnapshot::capture_full(&book, &metrics, &user_state, &PersistenceConfig::default())
}

fn engine() -> PaperSimulationEngine {
    PaperSimulationEngine::new()
}

/// A book with two independent strategies: A holds an open AAPL long and a
/// fully-closed MSFT round trip; B holds an open AAPL short.
fn sample_book() -> VirtualLedgerBook {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let a = StrategyId::new("reservoir-a");
    let b = StrategyId::new("reservoir-b");
    book.apply_fill(
        &a,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .unwrap();
    book.apply_fill(
        &a,
        &engine.simulate_fill(2, "MSFT", 50, 20_000, None).unwrap(),
    )
    .unwrap();
    book.apply_fill(
        &a,
        &engine.simulate_fill(3, "MSFT", -50, 21_000, None).unwrap(),
    )
    .unwrap();
    book.apply_fill(
        &b,
        &engine.simulate_fill(1, "AAPL", -30, 10_500, None).unwrap(),
    )
    .unwrap();
    book
}

#[test]
fn srs_sim_004_round_trip_reproduces_the_book_exactly() {
    let book = sample_book();
    let config = PersistenceConfig::default();
    let snapshot = PaperStateSnapshot::capture(&book, &config);
    let restored = restore(&snapshot.serialize()).expect("restore");
    // The full ledger round-trips: every strategy, symbol, quantity, signed cost
    // basis, realized P&L, and cost component survives.
    assert_eq!(restored, book);
    // ...and the convenience deserialize reproduces the whole envelope too.
    assert_eq!(
        PaperStateSnapshot::deserialize(&snapshot.serialize()).unwrap(),
        snapshot
    );
}

#[test]
fn srs_sim_004_serialization_is_deterministic_across_insertion_order() {
    // Two books with the same positions inserted in different orders must serialize
    // to byte-identical output -- determinism comes from sorting keys, not from
    // HashMap iteration order, so an unchanged 60s checkpoint never churns.
    let engine = engine();
    let s = StrategyId::new("s");
    let mut first = VirtualLedgerBook::new();
    first
        .apply_fill(&s, &engine.simulate_fill(1, "AAA", 1, 100, None).unwrap())
        .unwrap();
    first
        .apply_fill(&s, &engine.simulate_fill(2, "MMM", 1, 100, None).unwrap())
        .unwrap();
    first
        .apply_fill(&s, &engine.simulate_fill(3, "ZZZ", 1, 100, None).unwrap())
        .unwrap();

    let mut second = VirtualLedgerBook::new();
    second
        .apply_fill(&s, &engine.simulate_fill(3, "ZZZ", 1, 100, None).unwrap())
        .unwrap();
    second
        .apply_fill(&s, &engine.simulate_fill(1, "AAA", 1, 100, None).unwrap())
        .unwrap();
    second
        .apply_fill(&s, &engine.simulate_fill(2, "MMM", 1, 100, None).unwrap())
        .unwrap();

    let config = PersistenceConfig::default();
    assert_eq!(
        PaperStateSnapshot::capture(&first, &config).serialize(),
        PaperStateSnapshot::capture(&second, &config).serialize()
    );
    // Re-serializing the same captured state is also byte-stable.
    let once = PaperStateSnapshot::capture(&first, &config).serialize();
    let twice = PaperStateSnapshot::capture(&first, &config).serialize();
    assert_eq!(once, twice);
}

#[test]
fn srs_sim_004_isolated_strategies_survive_persistence() {
    // The SYS-84 isolation property must hold through persistence: each strategy's
    // independent position is restored intact, and one strategy's restore never
    // bleeds into another's.
    let book = sample_book();
    let restored =
        restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
            .expect("restore");
    let a = StrategyId::new("reservoir-a");
    let b = StrategyId::new("reservoir-b");
    assert_eq!(restored.strategy_count(), 2);
    assert_eq!(restored.position(&a, "AAPL").unwrap().quantity(), 100);
    assert_eq!(restored.position(&b, "AAPL").unwrap().quantity(), -30);
    // A's long and B's short of the SAME symbol stayed independent.
    assert_ne!(
        restored.position(&a, "AAPL").unwrap(),
        restored.position(&b, "AAPL").unwrap()
    );
}

#[test]
fn srs_sim_004_flat_closed_position_keeps_realized_pnl() {
    // A fully-closed position is flat (quantity 0, basis 0) but still carries the
    // realized P&L and commission from the round trip; persistence must not drop it.
    let book = sample_book();
    let restored =
        restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
            .expect("restore");
    let a = StrategyId::new("reservoir-a");
    let msft = restored
        .position(&a, "MSFT")
        .expect("flat MSFT position survives");
    assert_eq!(msft.quantity(), 0);
    assert_eq!(msft.cost_basis_minor(), 0);
    assert_eq!(msft.realized_pnl_minor(), 50_000); // (21_000 - 20_000) * 50
    assert!(msft.commission_paid_minor() > 0);
}

#[test]
fn srs_sim_004_reconciliation_survives_persistence() {
    // Money-correctness invariant through persistence: a closed round trip's
    // realized P&L minus the FULL transaction cost still reconciles exactly with
    // the simulator's cash (sum of the fills' cash_delta) after restore -- no
    // charged cost disappears across serialize/restore.
    let engine = engine();
    let s = StrategyId::new("recon");
    let buy = engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap();
    let sell = engine.simulate_fill(2, "AAPL", -100, 11_000, None).unwrap();
    let mut book = VirtualLedgerBook::new();
    book.apply_fill(&s, &buy).unwrap();
    book.apply_fill(&s, &sell).unwrap();

    let restored =
        restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
            .expect("restore");
    let pos = restored.position(&s, "AAPL").unwrap();
    let net = pos.realized_pnl_minor() - pos.transaction_cost_paid_minor().unwrap();
    assert_eq!(
        net,
        i128::from(buy.cash_delta_minor) + i128::from(sell.cash_delta_minor)
    );
}

#[test]
fn srs_sim_004_occ_option_symbol_survives() {
    // A canonical OCC option contract string contains spaces; length-prefixing must
    // keep it intact through serialize/restore.
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let s = StrategyId::new("opt");
    book.apply_fill(
        &s,
        &engine
            .simulate_fill(1, "AAPL  240119C00190000", 1, 250, None)
            .unwrap(),
    )
    .unwrap();
    let restored =
        restore(&PaperStateSnapshot::capture(&book, &PersistenceConfig::default()).serialize())
            .expect("restore");
    assert_eq!(restored, book);
    assert!(restored.position(&s, "AAPL  240119C00190000").is_some());
}

#[test]
fn srs_sim_004_custom_cadence_config_round_trips() {
    let book = sample_book();
    let config = PersistenceConfig::new(15, 10).unwrap();
    let restored =
        PaperStateSnapshot::deserialize(&PaperStateSnapshot::capture(&book, &config).serialize())
            .expect("deserialize");
    assert_eq!(restored.config().interval_secs(), 15);
    assert_eq!(restored.config().restore_deadline_secs(), 10);
    // Shutdown persistence is mandatory (SYS-89), so it is always on.
    assert!(restored.config().persist_on_shutdown());
}

#[test]
fn srs_sim_004_corrupt_snapshot_fails_closed_with_no_partial_state() {
    let serialized =
        PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default()).serialize();

    // A tampered magic header is rejected (before the checksum), nothing partial.
    let bad_magic = serialized.replacen(MAGIC, "NOT-ATP", 1);
    assert_eq!(
        restore(&bad_magic),
        Err(PersistenceError::CorruptSnapshot {
            context: "magic header"
        })
    );

    // A truncated blob is rejected (checksum mismatch or a parse error); either way
    // no partial book is returned.
    let truncated = &serialized[..serialized.len() / 2];
    assert!(matches!(
        restore(truncated),
        Err(PersistenceError::ChecksumMismatch) | Err(PersistenceError::CorruptSnapshot { .. })
    ));

    // Trailing data changes the checksummed body, so it is caught.
    let trailing = serialized.clone() + "junk\n";
    assert_eq!(restore(&trailing), Err(PersistenceError::ChecksumMismatch));
}

#[test]
fn srs_sim_004_tampered_value_fails_closed() {
    // The Codex finding: a value changed to ANOTHER structurally-valid value (AAPL's
    // cost basis 1_000_000 -> 1_000_001, still positive and sign-consistent with the
    // long quantity) passes every field invariant. The integrity checksum is what
    // makes restore fail closed under fault injection instead of resuming with
    // fabricated paper positions/P&L.
    let serialized =
        PaperStateSnapshot::capture(&sample_book(), &PersistenceConfig::default()).serialize();
    let tampered = serialized.replacen("\n1000000\n", "\n1000001\n", 1);
    assert_ne!(tampered, serialized);
    assert_eq!(restore(&tampered), Err(PersistenceError::ChecksumMismatch));
}

// --------------------------------------------------------------------------- //
// SRS-SIM-004 disk persistence, restore-deadline, and metrics/user-state capture
// --------------------------------------------------------------------------- //

#[test]
fn srs_sim_004_disk_round_trip_reproduces_full_state() {
    // The on-disk store must round-trip ALL THREE captured sub-states (ledger,
    // metrics, user-state) exactly: persist to disk, then load it back from a fresh
    // path handle and get a byte-identical snapshot.
    let snapshot = full_snapshot();
    let dir = temp_dir("disk-round-trip");
    snapshot.save_to_path(&dir).expect("save");
    let loaded = PaperStateSnapshot::load_from_path(&dir).expect("load");
    assert_eq!(loaded, snapshot);
    // Every sub-state survived, not just the ledger.
    assert_eq!(loaded.book(), snapshot.book());
    assert_eq!(loaded.metrics(), snapshot.metrics());
    assert_eq!(loaded.user_state(), snapshot.user_state());
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_sim_004_metrics_and_user_state_round_trip_exactly() {
    // In-memory codec round-trip of the extended (v2) snapshot: the metrics
    // accumulator (cash, ledger, trade log, equity curve, cursors) and the
    // user-state dictionary survive serialize/deserialize exactly.
    let snapshot = full_snapshot();
    let restored = PaperStateSnapshot::deserialize(&snapshot.serialize()).expect("deserialize");
    assert_eq!(restored, snapshot);
    let a = StrategyId::new("reservoir-a");
    let acc = restored.metrics().get(&a).expect("metrics survive");
    assert_eq!(acc.trade_log().len(), 2);
    assert_eq!(acc.equity_curve().len(), 2);
    assert_eq!(acc.starting_cash_minor(), 1_000_000);
    assert_eq!(
        restored.user_state().get(&a).map(String::as_str),
        Some(r#"{"regime":"trend","lookback":20}"#)
    );
}

#[test]
fn srs_sim_004_recover_from_path_measures_and_meets_the_deadline() {
    // recover_from_path loads, times the restore phase, enforces the SYS-89 30s
    // deadline, and returns the restored state. A file load is far under 30s.
    let snapshot = full_snapshot();
    let dir = temp_dir("recover");
    snapshot.save_to_path(&dir).expect("save");
    let outcome = recover_from_path(&dir).expect("recover");
    assert!(
        outcome.restore_elapsed() < Duration::from_secs(DEFAULT_DEADLINE_SECS),
        "restore took {:?}, must be well under the 30s deadline",
        outcome.restore_elapsed()
    );
    assert_eq!(outcome.strategy_count(), snapshot.book().strategy_count());
    assert_eq!(outcome.snapshot(), &snapshot);
    let _ = std::fs::remove_dir_all(&dir);
}

const DEFAULT_DEADLINE_SECS: u64 = 30;

#[test]
fn srs_sim_004_restore_deadline_fails_closed_when_overrun() {
    // The deadline guard fails closed on a state-restore phase that overran (the
    // strategy must not silently resume from a too-slow restore).
    let config = PersistenceConfig::default();
    assert_eq!(
        config.restore_within_deadline(Duration::from_secs(30)),
        Ok(())
    );
    assert_eq!(
        config.restore_within_deadline(Duration::from_secs(31)),
        Err(PersistenceError::RestoreDeadlineExceeded {
            elapsed_secs: 31,
            deadline_secs: 30,
        })
    );
}

#[test]
fn srs_sim_004_load_from_missing_dir_and_file_fail_closed() {
    // Recovery assumes durable state should be present; a missing store directory
    // OR a missing snapshot file inside a present directory both fail closed with
    // Io rather than silently substituting an empty state.
    let absent = temp_dir("absent");
    assert!(matches!(
        PaperStateSnapshot::load_from_path(&absent),
        Err(PersistenceError::Io { .. })
    ));

    let present_but_empty = temp_dir("empty");
    std::fs::create_dir_all(&present_but_empty).unwrap();
    assert!(matches!(
        PaperStateSnapshot::load_from_path(&present_but_empty),
        Err(PersistenceError::Io { .. })
    ));
    let _ = std::fs::remove_dir_all(&present_but_empty);
}

#[test]
fn srs_sim_004_corrupt_file_on_disk_fails_closed() {
    // A garbage store file is rejected whole on load, never restored partially.
    let dir = temp_dir("corrupt");
    full_snapshot().save_to_path(&dir).expect("save");
    std::fs::write(PaperStateSnapshot::store_path(&dir), b"not-a-snapshot\n").unwrap();
    assert!(matches!(
        PaperStateSnapshot::load_from_path(&dir),
        Err(PersistenceError::CorruptSnapshot { .. }) | Err(PersistenceError::ChecksumMismatch)
    ));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_sim_004_non_object_user_state_fails_closed() {
    // SYS-89 names a user-state DICTIONARY: a persisted user-state value that is not
    // a JSON object is rejected on restore (the serializer carries the bytes; the
    // deserializer validates the shape). capture_full does not validate, so a foreign
    // writer's non-object value reaches deserialize and must fail closed.
    let a = StrategyId::new("reservoir-a");
    let mut user_state: HashMap<StrategyId, String> = HashMap::new();
    user_state.insert(a, r#"["not","a","dict"]"#.to_string());
    let snapshot = PaperStateSnapshot::capture_full(
        &sample_book(),
        &HashMap::new(),
        &user_state,
        &PersistenceConfig::default(),
    );
    assert_eq!(
        PaperStateSnapshot::deserialize(&snapshot.serialize()),
        Err(PersistenceError::InconsistentField {
            context: "user-state value is not a JSON object"
        })
    );
}

#[test]
fn srs_sim_004_full_snapshot_is_deterministic_across_insertion_order() {
    // The extended snapshot (metrics + user-state maps) must serialize identically
    // regardless of HashMap insertion order -- an unchanged 60s checkpoint of the
    // full state never churns.
    let engine = engine();
    let a = StrategyId::new("a");
    let b = StrategyId::new("b");

    let mut metrics_first: HashMap<StrategyId, PaperMetricsAccumulator> = HashMap::new();
    metrics_first.insert(a.clone(), sample_accumulator(&engine));
    metrics_first.insert(b.clone(), sample_accumulator(&engine));
    let mut user_first: HashMap<StrategyId, String> = HashMap::new();
    user_first.insert(a.clone(), r#"{"k":1}"#.to_string());
    user_first.insert(b.clone(), r#"{"k":2}"#.to_string());

    // Same contents, inserted in the opposite order.
    let mut metrics_second: HashMap<StrategyId, PaperMetricsAccumulator> = HashMap::new();
    metrics_second.insert(b.clone(), sample_accumulator(&engine));
    metrics_second.insert(a.clone(), sample_accumulator(&engine));
    let mut user_second: HashMap<StrategyId, String> = HashMap::new();
    user_second.insert(b, r#"{"k":2}"#.to_string());
    user_second.insert(a, r#"{"k":1}"#.to_string());

    let config = PersistenceConfig::default();
    let book = VirtualLedgerBook::new();
    assert_eq!(
        PaperStateSnapshot::capture_full(&book, &metrics_first, &user_first, &config).serialize(),
        PaperStateSnapshot::capture_full(&book, &metrics_second, &user_second, &config).serialize()
    );
}

#[test]
fn srs_sim_004_save_rejects_non_object_user_state_and_preserves_last_good_store() {
    // The write-boundary poison-pill guard: save_to_path rejects a snapshot whose
    // user-state is not a JSON object BEFORE touching the store, so a bad caller can
    // never atomically overwrite the last valid checkpoint with a file the fail-closed
    // recovery path would refuse (SYS-89 recovery must never fail on self-written data).
    let dir = temp_dir("poison-pill");
    // Persist a GOOD snapshot first, so there is a last-good store to protect.
    let good = full_snapshot();
    good.save_to_path(&dir).expect("good save");

    // Now attempt to persist a snapshot with a non-object user-state; it must be
    // rejected at the write boundary.
    let mut bad_user_state: HashMap<StrategyId, String> = HashMap::new();
    bad_user_state.insert(
        StrategyId::new("reservoir-a"),
        "not-a-json-object".to_string(),
    );
    let bad = PaperStateSnapshot::capture_full(
        &sample_book(),
        &HashMap::new(),
        &bad_user_state,
        &PersistenceConfig::default(),
    );
    assert_eq!(
        bad.save_to_path(&dir),
        Err(PersistenceError::InconsistentField {
            context: "user-state value is not a JSON object"
        })
    );

    // The prior good store survived the rejected write and still recovers exactly.
    let recovered = recover_from_path(&dir).expect("last-good store still recovers");
    assert_eq!(recovered.into_snapshot(), good);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn srs_sim_004_atomic_save_leaves_no_scratch_file() {
    // The atomic save publishes exactly one store file and leaves no scratch behind.
    let dir = temp_dir("atomic");
    full_snapshot().save_to_path(&dir).expect("save");
    let entries: Vec<String> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    assert_eq!(entries, vec!["paper_sim_state.snapshot".to_string()]);
    let _ = std::fs::remove_dir_all(&dir);
}
