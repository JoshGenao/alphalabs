//! SRS-EXE-005 — the ledger persistence surface the durable state-recovery codec
//! (in `atp-execution`) builds on: iterate every tracked order, and rebuild a
//! ledger from previously-persisted lifecycles while re-checking the cross-order
//! invariants fail-closed.

use atp_types::{
    AssetClass, ClientCorrelationId, OrderKey, OrderLedger, OrderLifecycle, OrderLifecycleError,
    OrderSide, OrderState, OrderSubmission, OrderType, StrategyId,
};

fn corr(id: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(id).expect("non-empty id")
}

fn key(strat: &str, id: &str) -> OrderKey {
    OrderKey::new(StrategyId::new(strat), corr(id))
}

fn submission(strat: &str) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strat),
        symbol: "AAPL".to_string(),
        quantity: 10,
        asset_class: AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

#[test]
fn orders_iter_yields_every_tracked_order() {
    let mut ledger = OrderLedger::new();
    ledger.submit(corr("a"), &submission("s1")).unwrap();
    ledger.submit(corr("b"), &submission("s1")).unwrap();
    let mut ids: Vec<&str> = ledger
        .orders_iter()
        .map(|order| order.correlation_id().as_str())
        .collect();
    ids.sort();
    assert_eq!(ids, vec!["a", "b"]);
}

#[test]
fn restore_from_rebuilds_and_preserves_idempotency() {
    // Capture a ledger's lifecycles, then rebuild — the rebuilt ledger still
    // rejects a duplicate submission (the SRS-EXE-005 "no duplicate after restart").
    let mut original = OrderLedger::new();
    original.submit(corr("a"), &submission("s1")).unwrap();
    original
        .transition(&key("s1", "a"), OrderState::PendingSubmit)
        .unwrap();
    let captured: Vec<OrderLifecycle> = original.orders_iter().cloned().collect();

    let mut rebuilt = OrderLedger::restore_from(captured).unwrap();
    assert_eq!(
        rebuilt.state(&key("s1", "a")).unwrap(),
        OrderState::PendingSubmit
    );
    let err = rebuilt.submit(corr("a"), &submission("s1")).unwrap_err();
    assert_eq!(
        err.category,
        atp_types::OrderErrorCategory::DuplicateClientCorrelationId
    );
    assert_eq!(rebuilt.len(), 1);
}

#[test]
fn restore_from_rejects_a_duplicate_key() {
    let a = OrderLifecycle::restore(key("s1", "a"), submission("s1"), OrderState::New, None);
    let a_again =
        OrderLifecycle::restore(key("s1", "a"), submission("s1"), OrderState::Acked, None);
    assert_eq!(
        OrderLedger::restore_from(vec![a, a_again]).unwrap_err(),
        OrderLifecycleError::RestoredDuplicateKey(key("s1", "a"))
    );
}

#[test]
fn restore_from_rejects_a_key_strategy_mismatch() {
    // The lifecycle's key says strategy s1 but its submission is for s2.
    let bad = OrderLifecycle::restore(key("s1", "a"), submission("s2"), OrderState::New, None);
    assert_eq!(
        OrderLedger::restore_from(vec![bad]).unwrap_err(),
        OrderLifecycleError::RestoredKeyStrategyMismatch(key("s1", "a"))
    );
}

#[test]
fn restore_from_rejects_a_dangling_replaces_link() {
    // A replacement whose original is not in the restored set loses its lineage.
    let replacement = OrderLifecycle::restore(
        key("s1", "repl"),
        submission("s1"),
        OrderState::New,
        Some(key("s1", "missing-original")),
    );
    assert_eq!(
        OrderLedger::restore_from(vec![replacement]).unwrap_err(),
        OrderLifecycleError::UnknownOrder(key("s1", "missing-original"))
    );
}

#[test]
fn restore_from_rejects_a_live_replacement_over_a_non_cancelled_original() {
    // A replacement that has gone live (PENDING_SUBMIT) while its original is still
    // ACKED would be doubled exposure — the live machine forbids it at transition
    // time and restore must too.
    let original =
        OrderLifecycle::restore(key("s1", "orig"), submission("s1"), OrderState::Acked, None);
    let replacement = OrderLifecycle::restore(
        key("s1", "repl"),
        submission("s1"),
        OrderState::PendingSubmit,
        Some(key("s1", "orig")),
    );
    assert_eq!(
        OrderLedger::restore_from(vec![original, replacement]).unwrap_err(),
        OrderLifecycleError::ReplacementBlockedUntilOriginalCancelled {
            replacement: key("s1", "repl"),
            original: key("s1", "orig"),
            original_state: OrderState::Acked,
        }
    );
}

#[test]
fn restore_from_accepts_a_held_replacement_and_a_live_one_over_a_cancelled_original() {
    // A held (NEW) replacement over a working original is fine...
    let held_original = OrderLifecycle::restore(
        key("s1", "o1"),
        submission("s1"),
        OrderState::CancelPending,
        None,
    );
    let held_repl = OrderLifecycle::restore(
        key("s1", "r1"),
        submission("s1"),
        OrderState::New,
        Some(key("s1", "o1")),
    );
    // ...and a live replacement over a CANCELLED original is fine.
    let cancelled_original = OrderLifecycle::restore(
        key("s1", "o2"),
        submission("s1"),
        OrderState::Cancelled,
        None,
    );
    let live_repl = OrderLifecycle::restore(
        key("s1", "r2"),
        submission("s1"),
        OrderState::Acked,
        Some(key("s1", "o2")),
    );
    let ledger = OrderLedger::restore_from(vec![
        held_original,
        held_repl,
        cancelled_original,
        live_repl,
    ])
    .unwrap();
    assert_eq!(ledger.len(), 4);
}
