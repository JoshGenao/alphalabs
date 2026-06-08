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

use atp_simulation::paper_state::{
    restore, PaperStateSnapshot, PersistenceConfig, PersistenceError, MAGIC,
};
use atp_simulation::sim::PaperSimulationEngine;
use atp_simulation::virtual_ledger::VirtualLedgerBook;
use atp_types::StrategyId;

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
