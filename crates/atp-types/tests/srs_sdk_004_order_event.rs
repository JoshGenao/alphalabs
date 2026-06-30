//! SRS-SDK-004 integration (domain-safety) tests — the source-neutral
//! order-event callback authority, exercised through the **order-bound public
//! API** `OrderLedger::transition_with_event` (the same surface the live
//! execution and paper simulation dispatchers consume). Traces SyRS SYS-7 /
//! SYS-85 / NFR-P4; StRS SN-1.22 / SN-1.29.
//!
//! A dispatcher can only obtain a callback by *successfully* transitioning a
//! *tracked* order: `OrderEventCategory` is opaque (private construction) and the
//! `from` state is the ledger's own record, never a caller argument. A single
//! transition returns *every* event it produces, including a cascaded
//! auto-rejection of a held cancel-replace replacement — so no callback is
//! silently lost. These tests drive a real `OrderLedger`.
//!
//! Each `srs_sdk_004_*` function is shelled out to by the L7 domain test
//! `tests/domain/test_order_event_category.py`.

use atp_types::order_event::{
    OrderEvent, OrderEventCategory, LIVE_CALLBACK_LATENCY_P95_MS, PAPER_CALLBACK_LATENCY_P95_MS,
};
use atp_types::order_lifecycle::{
    ClientCorrelationId, OrderKey, OrderLedger, OrderLifecycleError, OrderState,
};
use atp_types::{OrderSubmission, StrategyId};

const STRAT: &str = "sdk004-strat";

fn corr(value: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(value).expect("non-empty correlation id")
}

fn key(value: &str) -> OrderKey {
    OrderKey::new(StrategyId::new(STRAT), corr(value))
}

fn submission() -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(STRAT),
        symbol: "AAPL".to_string(),
        quantity: 100,
        asset_class: atp_types::AssetClass::Equity,
        side: atp_types::OrderSide::Buy,
        order_type: atp_types::OrderType::Market,
    }
}

/// The primary (transitioned-order) event's callback wire string.
fn primary(events: &[OrderEvent]) -> Option<&'static str> {
    events[0].category().map(OrderEventCategory::as_str)
}

/// Submit a fresh order `id` and apply `path` through `transition_with_event`,
/// returning the primary callback wire string of the final step.
fn drive(ledger: &mut OrderLedger, id: &str, path: &[OrderState]) -> Option<&'static str> {
    ledger
        .submit(corr(id), &submission())
        .expect("fresh submit");
    let mut last = Vec::new();
    for &state in path {
        last = ledger
            .transition_with_event(&key(id), state)
            .expect("legal modeled transition");
    }
    primary(&last)
}

/// Drive an order to a callback-bearing final state and return its category.
fn ledger_category(ledger: &mut OrderLedger, id: &str, path: &[OrderState]) -> OrderEventCategory {
    ledger
        .submit(corr(id), &submission())
        .expect("fresh submit");
    let mut last = Vec::new();
    for &state in path {
        last = ledger
            .transition_with_event(&key(id), state)
            .expect("legal modeled transition");
    }
    last[0].category().expect("final state surfaces a callback")
}

/// The live and paper dispatchers share one ledger-bound authority, so the same
/// transition sequence yields the same callback categories — the SRS-SDK-001 /
/// AC-14 parity guarantee, by construction.
#[test]
fn srs_sdk_004_live_and_paper_derive_identical_category() {
    let path = [
        OrderState::PendingSubmit,
        OrderState::Acked,
        OrderState::PartiallyFilled,
        OrderState::Filled,
    ];
    let mut live = OrderLedger::new();
    let mut paper = OrderLedger::new();
    live.submit(corr("o"), &submission()).unwrap();
    paper.submit(corr("o"), &submission()).unwrap();

    let mut live_seq = Vec::new();
    let mut paper_seq = Vec::new();
    for &state in &path {
        live_seq.push(primary(
            &live.transition_with_event(&key("o"), state).unwrap(),
        ));
        paper_seq.push(primary(
            &paper.transition_with_event(&key("o"), state).unwrap(),
        ));
    }
    assert_eq!(live_seq, paper_seq);
    assert_eq!(
        live_seq,
        vec![None, Some("ACK"), Some("PARTIAL_FILL"), Some("FILL")]
    );
}

/// A callback cannot be produced without a *successful* mutation of a *tracked*
/// order. This is the core SRS-SDK-004 safety property: a dispatcher cannot
/// fabricate a fill (or any callback) for an order not actually in the expected
/// state, and the opaque category cannot be constructed directly.
#[test]
fn srs_sdk_004_callback_is_bound_to_a_real_transition() {
    let mut ledger = OrderLedger::new();

    // (a) unknown order — no event, fails closed.
    assert_eq!(
        ledger.transition_with_event(&key("ghost"), OrderState::Acked),
        Err(OrderLifecycleError::UnknownOrder(key("ghost")))
    );

    // (b) a tracked order in NEW: a dispatcher cannot fabricate ACKED -> FILLED.
    // The ledger feeds the order's REAL state (NEW), and NEW -> FILLED is illegal,
    // so no FILL callback is minted.
    ledger.submit(corr("o"), &submission()).unwrap();
    assert_eq!(
        ledger.transition_with_event(&key("o"), OrderState::Filled),
        Err(OrderLifecycleError::IllegalTransition {
            from: OrderState::New,
            to: OrderState::Filled,
        })
    );

    // (c) a legal mutation does return the bound callback.
    let events = ledger
        .transition_with_event(&key("o"), OrderState::Rejected)
        .unwrap();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].state(), OrderState::Rejected);
    assert_eq!(primary(&events), Some("REJECTED"));
}

/// A duplicate / stale / out-of-order broker event that maps to no legal
/// transition of the tracked order is fail-closed (`Err`) — the authority never
/// fabricates a callback. Dedup / reconciliation of such events is owned by the
/// IB adapter (SRS-EXE-006) / sim fill model (SRS-SIM-002), not this slice.
#[test]
fn srs_sdk_004_illegal_or_stale_event_is_fail_closed() {
    let mut ledger = OrderLedger::new();
    ledger.submit(corr("o"), &submission()).unwrap();
    for state in [OrderState::PendingSubmit, OrderState::Filled] {
        ledger.transition_with_event(&key("o"), state).unwrap();
    }
    // The order is now terminal (FILLED). A duplicate FILL has no legal edge.
    assert_eq!(
        ledger.transition_with_event(&key("o"), OrderState::Filled),
        Err(OrderLifecycleError::IllegalTransition {
            from: OrderState::Filled,
            to: OrderState::Filled,
        })
    );
    // A stale ACK after the terminal state is likewise refused.
    assert_eq!(
        ledger.transition_with_event(&key("o"), OrderState::Acked),
        Err(OrderLifecycleError::IllegalTransition {
            from: OrderState::Filled,
            to: OrderState::Acked,
        })
    );
}

/// A cancel-replace whose original then *fills* (a fill races the cancel) drives
/// the held replacement to `REJECTED` — and that rejection callback is returned
/// **in the same result**, not silently lost. The replacement is terminal after
/// this, so the callback could never be re-derived through a later transition.
#[test]
fn srs_sdk_004_cascaded_replacement_rejection_is_returned() {
    let mut ledger = OrderLedger::new();
    ledger.submit(corr("orig"), &submission()).unwrap();
    ledger
        .transition_with_event(&key("orig"), OrderState::PendingSubmit)
        .unwrap();
    ledger
        .transition_with_event(&key("orig"), OrderState::Acked)
        .unwrap();
    // Request cancel-replace: orig -> CANCEL_PENDING, a held replacement "repl"
    // is registered (NEW, replaces orig).
    ledger
        .cancel_replace(&key("orig"), &submission(), corr("repl"))
        .unwrap();
    // A fill races the cancel: orig CANCEL_PENDING -> FILLED (non-cancelled
    // terminal) auto-suppresses the held replacement to REJECTED.
    let events = ledger
        .transition_with_event(&key("orig"), OrderState::Filled)
        .unwrap();

    assert_eq!(
        events.len(),
        2,
        "primary FILL + cascaded replacement REJECTED"
    );
    assert_eq!(events[0].key(), &key("orig"));
    assert_eq!(events[0].state(), OrderState::Filled);
    assert_eq!(
        events[0].category().map(OrderEventCategory::as_str),
        Some("FILL")
    );
    assert_eq!(events[1].key(), &key("repl"));
    assert_eq!(events[1].state(), OrderState::Rejected);
    assert_eq!(
        events[1].category().map(OrderEventCategory::as_str),
        Some("REJECTED")
    );
}

/// Internal lifecycle states (PENDING_SUBMIT / CANCEL_PENDING) surface no
/// strategy-facing callback even on a legal transition.
#[test]
fn srs_sdk_004_internal_states_surface_no_callback() {
    let mut ledger = OrderLedger::new();
    ledger.submit(corr("o"), &submission()).unwrap();

    let pending = ledger
        .transition_with_event(&key("o"), OrderState::PendingSubmit)
        .unwrap();
    assert_eq!(pending.len(), 1);
    assert_eq!(pending[0].state(), OrderState::PendingSubmit);
    assert_eq!(pending[0].category(), None);

    assert_eq!(
        primary(
            &ledger
                .transition_with_event(&key("o"), OrderState::Acked)
                .unwrap()
        ),
        Some("ACK")
    );

    let cancel_pending = ledger
        .transition_with_event(&key("o"), OrderState::CancelPending)
        .unwrap();
    assert_eq!(cancel_pending[0].category(), None);
}

/// Each callback-bearing destination state classifies correctly when reached by
/// a real transition; the internal states classify to `None`.
#[test]
fn srs_sdk_004_destination_states_classify() {
    let mut ledger = OrderLedger::new();
    // One fresh order per path so we never reuse a terminal order.
    assert_eq!(
        drive(
            &mut ledger,
            "ack",
            &[OrderState::PendingSubmit, OrderState::Acked]
        ),
        Some("ACK")
    );
    assert_eq!(
        drive(
            &mut ledger,
            "pf",
            &[
                OrderState::PendingSubmit,
                OrderState::Acked,
                OrderState::PartiallyFilled
            ]
        ),
        Some("PARTIAL_FILL")
    );
    assert_eq!(
        drive(
            &mut ledger,
            "fill",
            &[
                OrderState::PendingSubmit,
                OrderState::Acked,
                OrderState::Filled
            ]
        ),
        Some("FILL")
    );
    assert_eq!(
        drive(
            &mut ledger,
            "cancel",
            &[
                OrderState::PendingSubmit,
                OrderState::Acked,
                OrderState::CancelPending,
                OrderState::Cancelled,
            ]
        ),
        Some("CANCELLED")
    );
    assert_eq!(
        drive(&mut ledger, "rej", &[OrderState::Rejected]),
        Some("REJECTED")
    );
    assert_eq!(
        drive(
            &mut ledger,
            "exp",
            &[
                OrderState::PendingSubmit,
                OrderState::Acked,
                OrderState::Expired
            ]
        ),
        Some("EXPIRED")
    );
    // Internal state -> no callback.
    assert_eq!(
        drive(&mut ledger, "pend", &[OrderState::PendingSubmit]),
        None
    );
}

/// The four AC-named categories require fill economics; CANCELLED / REJECTED /
/// EXPIRED require a reason — the Rust analog of the Python SDK's
/// `assert_order_event_payload` field-presence rules. The categories are
/// obtained from real transitions (a foreign crate cannot construct them).
#[test]
fn srs_sdk_004_ac_named_categories_require_fill_economics() {
    let mut ledger = OrderLedger::new();

    let fill = ledger_category(
        &mut ledger,
        "fill",
        &[
            OrderState::PendingSubmit,
            OrderState::Acked,
            OrderState::Filled,
        ],
    );
    assert!(fill.is_ac_named());
    assert!(fill.requires_fill_economics());
    assert!(!fill.requires_reason());

    let rejected = ledger_category(&mut ledger, "rej", &[OrderState::Rejected]);
    assert!(rejected.is_ac_named());
    assert!(rejected.requires_fill_economics());
    assert!(rejected.requires_reason());

    let ack = ledger_category(
        &mut ledger,
        "ack",
        &[OrderState::PendingSubmit, OrderState::Acked],
    );
    assert!(!ack.is_ac_named());
    assert!(!ack.requires_fill_economics());
    assert!(!ack.requires_reason());

    let expired = ledger_category(
        &mut ledger,
        "exp",
        &[
            OrderState::PendingSubmit,
            OrderState::Acked,
            OrderState::Expired,
        ],
    );
    assert!(!expired.is_ac_named());
    assert!(expired.requires_reason());
}

/// The NFR-P4 latency budgets are the documented numbers (single source of
/// truth shared with the architecture metadata and the Python SDK constants).
#[test]
fn srs_sdk_004_latency_budgets_are_the_nfr_p4_numbers() {
    assert_eq!(LIVE_CALLBACK_LATENCY_P95_MS, 1000);
    assert_eq!(PAPER_CALLBACK_LATENCY_P95_MS, 100);
}
