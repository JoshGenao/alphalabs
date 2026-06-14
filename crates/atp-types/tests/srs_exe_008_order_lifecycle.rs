//! SRS-EXE-008 integration (domain-safety) tests — the order lifecycle state
//! machine and its client-correlation-id idempotency, exercised end to end
//! through the public `atp-types` API (the same surface the live execution and
//! paper simulation submission paths consume). Traces SyRS SYS-3 / SYS-7 /
//! SYS-64 / SYS-90, NFR-R3; StRS SN-1.08 / SN-1.22.
//!
//! Each `srs_exe_008_*` function is shelled out to by the L7 domain test
//! `tests/domain/test_order_lifecycle.py`.

use atp_types::order_lifecycle::{
    ClientCorrelationId, OrderLedger, OrderLifecycleError, OrderState,
};
use atp_types::{OrderErrorCategory, OrderSubmission, StrategyId};

fn corr(value: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(value).expect("non-empty correlation id")
}

fn submission(strategy: &str, symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new(strategy),
        symbol: symbol.to_string(),
        quantity,
    }
}

/// A duplicate submission for the same correlation id is rejected idempotently
/// with the SRS-ERR-001 envelope — and the first order is never disturbed nor a
/// second order created (no double execution).
#[test]
fn srs_exe_008_duplicate_submission_is_rejected_idempotently() {
    let mut ledger = OrderLedger::new();
    let order = submission("live-strat", "AAPL", 100);

    assert_eq!(
        ledger.submit(corr("ord-1"), &order).unwrap().state(),
        OrderState::New
    );
    // advance the live order so a careless duplicate would visibly corrupt it
    ledger
        .transition(&corr("ord-1"), OrderState::PendingSubmit)
        .unwrap();
    ledger
        .transition(&corr("ord-1"), OrderState::Acked)
        .unwrap();
    ledger
        .transition(&corr("ord-1"), OrderState::PartiallyFilled)
        .unwrap();

    for _ in 0..5 {
        let err = ledger
            .submit(corr("ord-1"), &order)
            .expect_err("duplicate correlation id must be rejected");
        assert_eq!(
            err.category,
            OrderErrorCategory::DuplicateClientCorrelationId
        );
        assert_eq!(err.category.as_str(), "DUPLICATE_CLIENT_CORRELATION_ID");
        // SRS-ERR-001: the original order parameters travel with the error
        assert_eq!(err.original_order, order);
        // idempotent: existing order untouched, exactly one order tracked
        assert_eq!(
            ledger.state(&corr("ord-1")).unwrap(),
            OrderState::PartiallyFilled
        );
        assert_eq!(ledger.len(), 1);
    }
}

/// The four terminal states have no outgoing transition — a FILLED / CANCELLED
/// / REJECTED / EXPIRED order can never be resurrected.
#[test]
fn srs_exe_008_terminal_states_have_no_outgoing_transitions() {
    let terminals = [
        OrderState::Filled,
        OrderState::Cancelled,
        OrderState::Rejected,
        OrderState::Expired,
    ];
    let every = [
        OrderState::New,
        OrderState::PendingSubmit,
        OrderState::Acked,
        OrderState::PartiallyFilled,
        OrderState::Filled,
        OrderState::CancelPending,
        OrderState::Cancelled,
        OrderState::Rejected,
        OrderState::Expired,
    ];
    for terminal in terminals {
        assert!(terminal.is_terminal());
        assert!(terminal.allowed_next().is_empty());
        for target in every {
            assert!(
                !terminal.can_transition_to(target),
                "{terminal} (terminal) must not transition to {target}"
            );
        }
    }
}

/// Every edge not in the documented graph is refused, and the order's state is
/// left unchanged on a refused transition.
#[test]
fn srs_exe_008_illegal_transitions_are_refused() {
    // NEW cannot leap to ACKED / FILLED / PARTIALLY_FILLED / CANCEL_PENDING.
    let mut order = atp_types::OrderLifecycle::new(corr("c"));
    for illegal in [
        OrderState::Acked,
        OrderState::Filled,
        OrderState::PartiallyFilled,
        OrderState::CancelPending,
        OrderState::Cancelled,
        OrderState::Expired,
    ] {
        assert_eq!(
            order.transition_to(illegal).unwrap_err(),
            OrderLifecycleError::IllegalTransition {
                from: OrderState::New,
                to: illegal
            }
        );
        assert_eq!(
            order.state(),
            OrderState::New,
            "refused transition must not mutate state"
        );
    }
    // The full legal happy path is accepted.
    order.transition_to(OrderState::PendingSubmit).unwrap();
    order.transition_to(OrderState::Acked).unwrap();
    order.transition_to(OrderState::Filled).unwrap();
    assert!(order.state().is_terminal());
}

/// Cancel-replace is cancel-then-new: the original moves to CANCEL_PENDING and
/// is retained, and the replacement is a fresh NEW order whose `replaces` keeps
/// the original correlation id for audit.
#[test]
fn srs_exe_008_cancel_replace_is_cancel_then_new_retaining_original_id() {
    let mut ledger = OrderLedger::new();
    ledger
        .submit(corr("orig"), &submission("live-strat", "MSFT", 50))
        .unwrap();
    ledger
        .transition(&corr("orig"), OrderState::PendingSubmit)
        .unwrap();
    ledger.transition(&corr("orig"), OrderState::Acked).unwrap();
    ledger
        .transition(&corr("orig"), OrderState::PartiallyFilled)
        .unwrap();

    {
        let replacement = ledger.cancel_replace(&corr("orig"), corr("repl")).unwrap();
        assert_eq!(replacement.state(), OrderState::New);
        assert_eq!(replacement.correlation_id().as_str(), "repl");
        assert_eq!(
            replacement.replaces().map(ClientCorrelationId::as_str),
            Some("orig"),
            "the replacement must retain the original correlation id for audit"
        );
    }

    // cancel: the original is retained in CANCEL_PENDING (cancel requested).
    assert_eq!(
        ledger.state(&corr("orig")).unwrap(),
        OrderState::CancelPending
    );
    assert_eq!(ledger.len(), 2);

    // The already-cancel-replaced original is no longer cancellable.
    assert_eq!(
        ledger
            .cancel_replace(&corr("orig"), corr("again"))
            .unwrap_err(),
        OrderLifecycleError::IllegalTransition {
            from: OrderState::CancelPending,
            to: OrderState::CancelPending
        }
    );

    // On a fresh, working order: a replacement that reuses the original id, or
    // collides with an existing id, is refused without mutating the original.
    ledger
        .submit(corr("alpha"), &submission("live-strat", "NVDA", 5))
        .unwrap();
    ledger
        .transition(&corr("alpha"), OrderState::PendingSubmit)
        .unwrap();
    ledger
        .transition(&corr("alpha"), OrderState::Acked)
        .unwrap();
    assert_eq!(
        ledger
            .cancel_replace(&corr("alpha"), corr("alpha"))
            .unwrap_err(),
        OrderLifecycleError::ReplacementReusesOriginalId(corr("alpha"))
    );
    assert_eq!(
        ledger
            .cancel_replace(&corr("alpha"), corr("repl"))
            .unwrap_err(),
        OrderLifecycleError::DuplicateReplacementId(corr("repl"))
    );
    assert_eq!(ledger.state(&corr("alpha")).unwrap(), OrderState::Acked);
}

/// The client-assigned correlation id is the stable idempotency key: the same
/// id always maps to the same single order, regardless of how many times it is
/// re-submitted, and an unknown id is never silently created by a transition.
#[test]
fn srs_exe_008_correlation_id_is_the_stable_idempotency_key() {
    let mut ledger = OrderLedger::new();
    let order = submission("paper-strat", "TSLA", 10);

    // 1 live + 30 paper style fan-in: 31 distinct ids -> 31 distinct orders.
    ledger.submit(corr("live"), &order).unwrap();
    for i in 0..30 {
        ledger.submit(corr(&format!("paper-{i}")), &order).unwrap();
    }
    assert_eq!(ledger.len(), 31);

    // Re-submitting any existing id never creates a second order.
    for i in 0..30 {
        assert!(ledger.submit(corr(&format!("paper-{i}")), &order).is_err());
    }
    assert_eq!(ledger.len(), 31);

    // A transition on an untracked id is refused, not auto-created.
    assert_eq!(
        ledger
            .transition(&corr("ghost"), OrderState::Acked)
            .unwrap_err(),
        OrderLifecycleError::UnknownOrder(corr("ghost"))
    );
    assert_eq!(ledger.len(), 31);

    // An empty correlation id is rejected at construction (fail closed).
    assert!(ClientCorrelationId::new("").is_err());
}

/// A partial fill that races an in-flight cancel must not lose the
/// cancellation: CANCEL_PENDING -> PARTIALLY_FILLED -> CANCELLED is legal, so a
/// cancel-acknowledgement that lands after the partial fill is honoured.
#[test]
fn srs_exe_008_partial_fill_racing_cancel_can_still_be_cancelled() {
    let mut ledger = OrderLedger::new();
    ledger
        .submit(corr("o"), &submission("live-strat", "AMD", 40))
        .unwrap();
    ledger
        .transition(&corr("o"), OrderState::PendingSubmit)
        .unwrap();
    ledger.transition(&corr("o"), OrderState::Acked).unwrap();
    ledger
        .transition(&corr("o"), OrderState::CancelPending)
        .unwrap();
    // a partial fill races the in-flight cancel
    ledger
        .transition(&corr("o"), OrderState::PartiallyFilled)
        .unwrap();
    // the cancel of the remainder is acknowledged
    assert_eq!(
        ledger
            .transition(&corr("o"), OrderState::Cancelled)
            .unwrap(),
        OrderState::Cancelled
    );
}

/// Cancel-replace must not create doubled exposure: the replacement is held out
/// of the live path until the original is confirmed CANCELLED, and if the
/// original instead fills (cancel too late) the held replacement is
/// auto-suppressed to REJECTED.
#[test]
fn srs_exe_008_cancel_replace_blocks_doubled_exposure() {
    // Case A: the replacement is blocked until the original is CANCELLED.
    let mut ledger = OrderLedger::new();
    ledger
        .submit(corr("orig"), &submission("live-strat", "MSFT", 50))
        .unwrap();
    ledger
        .transition(&corr("orig"), OrderState::PendingSubmit)
        .unwrap();
    ledger.transition(&corr("orig"), OrderState::Acked).unwrap();
    ledger.cancel_replace(&corr("orig"), corr("repl")).unwrap();

    // while the original rests in CANCEL_PENDING (and could still fill) the
    // replacement cannot go live
    assert_eq!(
        ledger
            .transition(&corr("repl"), OrderState::PendingSubmit)
            .unwrap_err(),
        OrderLifecycleError::ReplacementBlockedUntilOriginalCancelled {
            replacement: corr("repl"),
            original: corr("orig"),
            original_state: OrderState::CancelPending,
        }
    );
    assert_eq!(ledger.state(&corr("repl")).unwrap(), OrderState::New);

    // once the original is CANCELLED, the replacement is free to go live
    ledger
        .transition(&corr("orig"), OrderState::Cancelled)
        .unwrap();
    assert_eq!(
        ledger
            .transition(&corr("repl"), OrderState::PendingSubmit)
            .unwrap(),
        OrderState::PendingSubmit
    );

    // Case B: a filled original auto-suppresses its held replacement.
    let mut ledger = OrderLedger::new();
    ledger
        .submit(corr("o2"), &submission("live-strat", "MSFT", 50))
        .unwrap();
    ledger
        .transition(&corr("o2"), OrderState::PendingSubmit)
        .unwrap();
    ledger.transition(&corr("o2"), OrderState::Acked).unwrap();
    ledger.cancel_replace(&corr("o2"), corr("r2")).unwrap();
    // the cancel loses the race: the original fully fills
    ledger.transition(&corr("o2"), OrderState::Filled).unwrap();
    // the held replacement is auto-suppressed so it can never doubled-expose
    assert_eq!(ledger.state(&corr("r2")).unwrap(), OrderState::Rejected);
}
