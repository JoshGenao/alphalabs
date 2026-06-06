//! Integration coverage for **SRS-SIM-003** — "maintain an independent virtual
//! position ledger for each paper strategy" (SyRS SYS-84; StRS SN-1.29 / SN-1.07).
//!
//! These tests drive the ledger end-to-end from a *real* priced fill: each fill
//! is produced by [`PaperSimulationEngine::simulate_fill`] (the SRS-BT-003 shared
//! cost path) and then applied to the [`VirtualLedgerBook`], so the ledger is
//! shown consuming the same `PaperFill` the cost family emits. They assert the
//! SYS-84 acceptance criterion: quantity, average cost, unrealized P&L, realized
//! P&L, and commission paid are isolated per paper strategy and per symbol, and
//! the ledger fails closed on corrupt input.

use atp_simulation::fill_model::MarketSnapshot;
use atp_simulation::sim::PaperSimulationEngine;
use atp_simulation::virtual_ledger::{LedgerError, VirtualLedgerBook};
use atp_types::StrategyId;

fn engine() -> PaperSimulationEngine {
    PaperSimulationEngine::new()
}

fn snapshot(last_minor: i64) -> MarketSnapshot {
    MarketSnapshot {
        bid_minor: last_minor - 1,
        ask_minor: last_minor + 1,
        last_minor,
        bar_volume: 10_000,
    }
}

#[test]
fn srs_sim_003_long_round_trip_realizes_pnl_and_accumulates_commission() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-1");

    let buy = engine
        .simulate_fill(1, "AAPL", 100, 10_000, None)
        .expect("buy");
    book.apply_fill(&strat, &buy).expect("apply buy");
    let sell = engine
        .simulate_fill(2, "AAPL", -100, 11_000, None)
        .expect("sell");
    book.apply_fill(&strat, &sell).expect("apply sell");

    let pos = book.position(&strat, "AAPL").expect("position");
    assert_eq!(pos.quantity(), 0);
    assert_eq!(pos.cost_basis_minor(), 0);
    // Realized P&L comes from the price move (gross of cost): (11_000 - 10_000) * 100.
    assert_eq!(pos.realized_pnl_minor(), 100_000);
    // Commission paid is the SUM of the two fills' real commissions (SYS-15a
    // default: $0.35 floor per 100-share fill), tracked separately from P&L.
    assert_eq!(
        pos.commission_paid_minor(),
        i128::from(buy.commission_minor + sell.commission_minor)
    );
    assert!(pos.commission_paid_minor() > 0);
}

#[test]
fn srs_sim_003_blends_average_cost_and_marks_to_market() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-2");

    book.apply_fill(
        &strat,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("buy 1");
    book.apply_fill(
        &strat,
        &engine.simulate_fill(2, "AAPL", 100, 12_000, None).unwrap(),
    )
    .expect("buy 2");

    let pos = book.position(&strat, "AAPL").expect("position");
    assert_eq!(pos.quantity(), 200);
    assert_eq!(pos.average_cost_minor(), Some(11_000));
    // Mark at 11_500: (11_500 - 11_000) * 200 = 100_000.
    assert_eq!(pos.unrealized_pnl_minor(&snapshot(11_500)), Ok(100_000));
}

#[test]
fn srs_sim_003_short_cover_and_flip_through_zero() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-3");

    // Open a long, then sell more than held: closes the long and opens a short.
    book.apply_fill(
        &strat,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("buy");
    book.apply_fill(
        &strat,
        &engine.simulate_fill(2, "AAPL", -150, 11_000, None).unwrap(),
    )
    .expect("flip");

    let pos = book.position(&strat, "AAPL").expect("position");
    assert_eq!(pos.quantity(), -50);
    // Only the closed 100-share long realizes: (11_000 - 10_000) * 100.
    assert_eq!(pos.realized_pnl_minor(), 100_000);
    assert_eq!(pos.average_cost_minor(), Some(11_000));
    // The short gains as the mark falls: (11_000 - 9_000) * 50.
    assert_eq!(pos.unrealized_pnl_minor(&snapshot(9_000)), Ok(100_000));
}

#[test]
fn srs_sim_003_isolates_strategies_holding_the_same_symbol() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let alpha = StrategyId::new("alpha");
    let beta = StrategyId::new("beta");

    book.apply_fill(
        &alpha,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("alpha buy");
    book.apply_fill(
        &beta,
        &engine.simulate_fill(1, "AAPL", -40, 9_500, None).unwrap(),
    )
    .expect("beta short");

    // Same symbol, independent quantities and average cost.
    let alpha_pos = book.position(&alpha, "AAPL").expect("alpha position");
    let beta_pos = book.position(&beta, "AAPL").expect("beta position");
    assert_eq!(alpha_pos.quantity(), 100);
    assert_eq!(beta_pos.quantity(), -40);
    assert_eq!(alpha_pos.average_cost_minor(), Some(10_000));
    assert_eq!(beta_pos.average_cost_minor(), Some(9_500));
    assert_eq!(book.strategy_count(), 2);

    // Mutating beta does not touch alpha.
    let alpha_before = book.position(&alpha, "AAPL").cloned();
    book.apply_fill(
        &beta,
        &engine.simulate_fill(2, "AAPL", -10, 9_400, None).unwrap(),
    )
    .expect("beta short 2");
    assert_eq!(book.position(&alpha, "AAPL").cloned(), alpha_before);
}

#[test]
fn srs_sim_003_fails_closed_on_corrupt_input() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-4");

    // A non-positive mark is rejected before any valuation.
    book.apply_fill(
        &strat,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("buy");
    let pos = book.position(&strat, "AAPL").expect("position");
    assert_eq!(
        pos.unrealized_pnl_minor(&snapshot(0)),
        Err(LedgerError::NonPositiveMark { mark_minor: 0 })
    );

    // An empty-symbol fill never creates a position.
    let mut bad = engine.simulate_fill(2, "AAPL", 100, 10_000, None).unwrap();
    bad.symbol = "   ".to_string();
    assert_eq!(book.apply_fill(&strat, &bad), Err(LedgerError::EmptySymbol));
}

#[test]
fn srs_sim_003_ledger_reconciles_with_simulated_cash() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-recon");

    // Drive a round trip from REAL fills (real commission + slippage + spread).
    let buy = engine
        .simulate_fill(1, "AAPL", 100, 10_000, None)
        .expect("buy");
    let sell = engine
        .simulate_fill(2, "AAPL", -100, 11_000, None)
        .expect("sell");
    book.apply_fill(&strat, &buy).expect("apply buy");
    book.apply_fill(&strat, &sell).expect("apply sell");

    let pos = book.position(&strat, "AAPL").expect("position");
    // Net result = gross realized P&L - the FULL transaction cost; it must equal
    // the simulator's cash impact exactly, so no charged cost (slippage, spread,
    // commission) silently disappears from the ledger.
    let net = pos.realized_pnl_minor() - pos.transaction_cost_paid_minor().expect("cost");
    let simulated_cash = i128::from(buy.cash_delta_minor) + i128::from(sell.cash_delta_minor);
    assert_eq!(net, simulated_cash);
    assert!(pos.transaction_cost_paid_minor().unwrap() > 0);
}

#[test]
fn srs_sim_003_aliased_symbols_share_one_position() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-5");

    // The same security arriving under different casing/whitespace must land in
    // ONE position, not split per alias (the symbol is canonicalized).
    book.apply_fill(
        &strat,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("AAPL");
    book.apply_fill(
        &strat,
        &engine.simulate_fill(2, "aapl", 100, 12_000, None).unwrap(),
    )
    .expect("aapl");
    book.apply_fill(
        &strat,
        &engine
            .simulate_fill(3, " AAPL ", -50, 11_000, None)
            .unwrap(),
    )
    .expect("padded");

    assert_eq!(book.ledger(&strat).unwrap().symbol_count(), 1);
    let pos = book.position(&strat, "aapl").expect("one shared position");
    assert_eq!(pos.quantity(), 150);
    assert_eq!(pos.average_cost_minor(), Some(11_000));
}

#[test]
fn srs_sim_003_rejects_inconsistent_cash_delta() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-cd");

    // A real fill whose public cash_delta_minor has been tampered with must be
    // rejected before any mutation, so a malformed fill cannot break the ledger's
    // reconciliation guarantee.
    let mut tampered = engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap();
    tampered.cash_delta_minor += 1;
    assert!(matches!(
        book.apply_fill(&strat, &tampered),
        Err(LedgerError::InconsistentCashDelta { .. })
    ));
    assert_eq!(book.strategy_count(), 0);
}

#[test]
fn srs_sim_003_symbol_keyed_marking_selects_the_named_position() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let strat = StrategyId::new("reservoir-mark");

    book.apply_fill(
        &strat,
        &engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap(),
    )
    .expect("aapl");
    book.apply_fill(
        &strat,
        &engine.simulate_fill(2, "MSFT", 100, 20_000, None).unwrap(),
    )
    .expect("msft");

    // The keyed surface marks the position named by the symbol, never another.
    assert_eq!(
        book.unrealized_pnl_minor(&strat, "AAPL", &snapshot(10_500)),
        Some(Ok(50_000))
    );
    assert_eq!(
        book.unrealized_pnl_minor(&strat, "MSFT", &snapshot(20_500)),
        Some(Ok(50_000))
    );
    assert_eq!(
        book.unrealized_pnl_minor(&strat, "NONE", &snapshot(1_000)),
        None
    );
}

#[test]
fn srs_sim_003_rejected_first_fill_leaves_no_phantom_strategy() {
    let engine = engine();
    let mut book = VirtualLedgerBook::new();
    let fresh = StrategyId::new("never-traded");

    // A rejected first fill for a brand-new strategy must not register the
    // strategy at all -- otherwise it would pollute later metrics, persistence,
    // and orchestrator accounting with a strategy that never had a valid fill.
    let mut bad = engine.simulate_fill(1, "AAPL", 100, 10_000, None).unwrap();
    bad.symbol = "   ".to_string();
    assert_eq!(book.apply_fill(&fresh, &bad), Err(LedgerError::EmptySymbol));
    assert_eq!(book.strategy_count(), 0);
    assert!(book.ledger(&fresh).is_none());
}
