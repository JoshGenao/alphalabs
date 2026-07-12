//! SRS-DATA-019 — adjust / cancel live resting orders on corporate actions.
//!
//! Drives the pure [`atp_execution::corporate_action_orders`] planner over fixture
//! resting orders + fixture corporate actions, and the ledger-apply path through
//! the real [`atp_types::OrderLedger::cancel_replace`]. Proves the AC:
//!
//!   * a split / reverse split adjusts resting-order QUANTITY and LIMIT / STOP
//!     prices (price factor DEN/NUM rounded half-to-even; quantity factor NUM/DEN,
//!     EXACT);
//!   * "adjustment not possible" — delisting, a fractional share count, a price
//!     that rounds to a non-positive value, a non-positive factor, or an overflow
//!     — CANCELS the order (never a silent truncate / round / wrap);
//!   * an adjustment is a cancel-then-new (`cancel_replace`), never an in-place
//!     mutation, so the pre-split and post-split order can never both be live.

use atp_execution::corporate_action_orders::{
    plan_resting_order, plan_resting_orders, PriceField, RestingOrderCancelReason,
    RestingOrderCorporateAction, RestingOrderOutcome,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderKey, OrderLedger, OrderSide, OrderState, OrderSubmission,
    OrderType, StrategyId,
};

const STRATEGY: &str = "live-1";

fn key(id: &str) -> OrderKey {
    OrderKey::new(
        StrategyId::new(STRATEGY),
        ClientCorrelationId::new(id).expect("valid correlation id"),
    )
}

fn order(symbol: &str, quantity: i64, order_type: OrderType) -> OrderSubmission {
    OrderSubmission::new(
        StrategyId::new(STRATEGY),
        symbol,
        quantity,
        AssetClass::Equity,
        OrderSide::Buy,
        order_type,
    )
}

fn limit(price_minor: i64) -> OrderType {
    OrderType::Limit {
        limit_price_minor: price_minor,
    }
}

// --------------------------------------------------------------------------- //
// Adjust (splits) — quantity ↑, prices ↓ (and inverse for reverse splits)
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_019_forward_split_scales_qty_up_and_limit_down() {
    // 4-for-1 forward split: quantity ×4, limit ÷4.
    let submission = order("AAPL", 100, limit(40_000));
    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    match plan_resting_order(&key("o1"), &submission, &action) {
        RestingOrderOutcome::Adjusted { new_submission, .. } => {
            assert_eq!(new_submission.quantity, 400, "quantity ×4");
            assert_eq!(
                new_submission.order_type.limit_price_minor(),
                Some(10_000),
                "limit ÷4"
            );
        }
        other => panic!("expected Adjusted, got {other:?}"),
    }
}

#[test]
fn srs_data_019_reverse_split_exact_qty_and_price_up() {
    // 1-for-10 reverse split on a quantity that divides evenly: quantity ÷10,
    // limit ×10.
    let submission = order("ZZZ", 100, limit(1_000));
    let action = RestingOrderCorporateAction::split("ZZZ", 1, 10);
    match plan_resting_order(&key("o1"), &submission, &action) {
        RestingOrderOutcome::Adjusted { new_submission, .. } => {
            assert_eq!(new_submission.quantity, 10, "quantity ÷10");
            assert_eq!(
                new_submission.order_type.limit_price_minor(),
                Some(10_000),
                "limit ×10"
            );
        }
        other => panic!("expected Adjusted, got {other:?}"),
    }
}

#[test]
fn srs_data_019_reverse_split_fractional_qty_is_cancelled() {
    // 1-for-10 on 5 shares: the residual is a fractional share (cash-in-lieu) —
    // the order is CANCELLED, never truncated to 0 or rounded to 1.
    let submission = order("ZZZ", 5, limit(1_000));
    let action = RestingOrderCorporateAction::split("ZZZ", 1, 10);
    match plan_resting_order(&key("frac"), &submission, &action) {
        RestingOrderOutcome::Cancelled { reason, .. } => assert_eq!(
            reason,
            RestingOrderCancelReason::QuantityNotIntegral {
                before: 5,
                numerator: 1,
                denominator: 10,
            }
        ),
        other => panic!("expected Cancelled(QuantityNotIntegral), got {other:?}"),
    }
}

#[test]
fn srs_data_019_bankers_rounding_half_tie_to_even() {
    // 2-for-1: price factor DEN/NUM = 1/2. A limit of 5 (→ 2.5) rounds to the even
    // 2; a limit of 7 (→ 3.5) rounds to the even 4 — round-half-to-even, pinned
    // identical to atp-data::normalization.
    let action = RestingOrderCorporateAction::split("AAPL", 2, 1);
    let five = plan_resting_order(&key("a"), &order("AAPL", 3, limit(5)), &action);
    let seven = plan_resting_order(&key("b"), &order("AAPL", 3, limit(7)), &action);
    assert_eq!(adjusted_limit(&five), 2, "2.5 → even 2");
    assert_eq!(adjusted_limit(&seven), 4, "3.5 → even 4");
}

#[test]
fn srs_data_019_limit_stop_and_stop_limit_all_adjusted() {
    // Every present price on the type is scaled; absent prices are untouched;
    // quantity always scales.
    let action = RestingOrderCorporateAction::split("AAPL", 2, 1);

    let stop = plan_resting_order(
        &key("stop"),
        &order(
            "AAPL",
            10,
            OrderType::Stop {
                stop_price_minor: 8_000,
            },
        ),
        &action,
    );
    match stop {
        RestingOrderOutcome::Adjusted { new_submission, .. } => {
            assert_eq!(new_submission.quantity, 20);
            assert_eq!(new_submission.order_type.stop_price_minor(), Some(4_000));
            assert_eq!(new_submission.order_type.limit_price_minor(), None);
        }
        other => panic!("expected Adjusted, got {other:?}"),
    }

    let stop_limit = plan_resting_order(
        &key("sl"),
        &order(
            "AAPL",
            10,
            OrderType::StopLimit {
                stop_price_minor: 8_000,
                limit_price_minor: 6_000,
            },
        ),
        &action,
    );
    match stop_limit {
        RestingOrderOutcome::Adjusted { new_submission, .. } => {
            assert_eq!(new_submission.order_type.stop_price_minor(), Some(4_000));
            assert_eq!(new_submission.order_type.limit_price_minor(), Some(3_000));
        }
        other => panic!("expected Adjusted, got {other:?}"),
    }

    // MARKET carries no price — only the quantity scales.
    let market = plan_resting_order(&key("mkt"), &order("AAPL", 10, OrderType::Market), &action);
    match market {
        RestingOrderOutcome::Adjusted { new_submission, .. } => {
            assert_eq!(new_submission.quantity, 20);
            assert_eq!(new_submission.order_type, OrderType::Market);
        }
        other => panic!("expected Adjusted, got {other:?}"),
    }
}

// --------------------------------------------------------------------------- //
// Cancel (adjustment not possible) — every fail mode is fail-closed
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_019_adjusted_price_rounds_to_zero_fails_closed_to_cancel() {
    // A 1-cent limit under a 4-for-1 split rounds to 0 (0.25 → 0). A resting order
    // can never carry a non-positive price, so it is CANCELLED.
    let submission = order("AAPL", 100, limit(1));
    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    match plan_resting_order(&key("z"), &submission, &action) {
        RestingOrderOutcome::Cancelled { reason, .. } => assert_eq!(
            reason,
            RestingOrderCancelReason::PriceRoundedNonPositive {
                field: PriceField::Limit,
                before_minor: 1,
            }
        ),
        other => panic!("expected Cancelled(PriceRoundedNonPositive), got {other:?}"),
    }
}

#[test]
fn srs_data_019_non_positive_factor_fails_closed_to_cancel() {
    let submission = order("AAPL", 100, limit(40_000));
    for (numerator, denominator) in [(0, 1), (4, 0), (-2, 1)] {
        let action = RestingOrderCorporateAction::split("AAPL", numerator, denominator);
        match plan_resting_order(&key("nf"), &submission, &action) {
            RestingOrderOutcome::Cancelled { reason, .. } => assert_eq!(
                reason,
                RestingOrderCancelReason::NonPositiveFactor {
                    numerator,
                    denominator,
                }
            ),
            other => panic!("expected Cancelled(NonPositiveFactor) for {numerator}/{denominator}, got {other:?}"),
        }
    }
}

#[test]
fn srs_data_019_overflow_fails_closed_to_cancel() {
    // A quantity near i64::MAX under a 2-for-1 split overflows i64 — the order is
    // CANCELLED, the arithmetic never wraps.
    let submission = order("AAPL", i64::MAX, limit(40_000));
    let action = RestingOrderCorporateAction::split("AAPL", 2, 1);
    match plan_resting_order(&key("ov"), &submission, &action) {
        RestingOrderOutcome::Cancelled { reason, .. } => assert_eq!(
            reason,
            RestingOrderCancelReason::Overflow {
                context: "quantity"
            }
        ),
        other => panic!("expected Cancelled(Overflow), got {other:?}"),
    }
}

#[test]
fn srs_data_019_delisting_cancels() {
    let submission = order("DEAD", 10, OrderType::Market);
    let action = RestingOrderCorporateAction::delisting("DEAD");
    match plan_resting_order(&key("d1"), &submission, &action) {
        RestingOrderOutcome::Cancelled { reason, symbol, .. } => {
            assert_eq!(reason, RestingOrderCancelReason::Delisting);
            assert_eq!(symbol, "DEAD");
        }
        other => panic!("expected Cancelled(Delisting), got {other:?}"),
    }
}

// --------------------------------------------------------------------------- //
// Selection: only RESTING (non-terminal), matching-symbol orders are affected
// --------------------------------------------------------------------------- //

#[test]
fn srs_data_019_other_symbol_order_is_unaffected() {
    // A split on AAPL never touches an order on another symbol.
    let submission = order("MSFT", 50, limit(30_000));
    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    assert!(matches!(
        plan_resting_order(&key("x"), &submission, &action),
        RestingOrderOutcome::Unaffected { .. }
    ));
}

#[test]
fn srs_data_019_symbol_match_is_case_and_whitespace_insensitive() {
    // The order and the action may name the same security in different case /
    // whitespace (OrderSubmission is not canonicalized); a raw compare would leave
    // an affected order un-cancelled. Match on the canonical (trim + upper) form.
    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    for sym in ["aapl", " AAPL ", "Aapl"] {
        match plan_resting_order(&key("o"), &order(sym, 100, limit(40_000)), &action) {
            RestingOrderOutcome::Adjusted { new_submission, .. } => {
                assert_eq!(
                    new_submission.quantity, 400,
                    "sym {sym:?} matched + adjusted"
                );
            }
            other => panic!("sym {sym:?} should be Adjusted, got {other:?}"),
        }
    }
    // Symmetric: a case-variant action symbol matches an upper-case order symbol.
    assert!(matches!(
        plan_resting_order(
            &key("o"),
            &order("AAPL", 100, limit(40_000)),
            &RestingOrderCorporateAction::delisting("  aapl  "),
        ),
        RestingOrderOutcome::Cancelled { .. }
    ));
    // A genuinely different security is still Unaffected.
    assert!(matches!(
        plan_resting_order(&key("o"), &order("MSFT", 100, limit(40_000)), &action),
        RestingOrderOutcome::Unaffected { .. }
    ));
}

#[test]
fn srs_data_019_ledger_routes_each_order_by_state_fail_closed() {
    // `Acked` is adjusted; every OTHER affected order that could still rest or fill
    // at the stale basis (`PartiallyFilled`, and the unacknowledged in-flight `New` /
    // `PendingSubmit`) is CANCELLED fail-closed; `CancelPending` (already terminating)
    // and an unaffected symbol are never touched, regardless of state.
    let mut ledger = OrderLedger::new();

    // New (just submitted; not sent to the broker) — pre-ack race, cancel fail-closed.
    ledger
        .submit(
            ClientCorrelationId::new("new").unwrap(),
            &order("AAPL", 100, limit(40_000)),
        )
        .unwrap();
    // PendingSubmit (submitting; not acknowledged) — can still ack OR fill, cancel fail-closed.
    ledger
        .submit(
            ClientCorrelationId::new("pend").unwrap(),
            &order("AAPL", 100, limit(40_000)),
        )
        .unwrap();
    ledger
        .transition(&key("pend"), OrderState::PendingSubmit)
        .unwrap();
    // Acked (adjusted).
    rest_order(&mut ledger, "ack", "AAPL", 100, limit(40_000));
    // PartiallyFilled on the affected symbol (cancelled fail-closed).
    rest_order(&mut ledger, "part", "AAPL", 100, limit(40_000));
    ledger
        .transition(&key("part"), OrderState::PartiallyFilled)
        .unwrap();
    // PartiallyFilled on ANOTHER symbol (untouched).
    rest_order(&mut ledger, "part_other", "MSFT", 100, limit(40_000));
    ledger
        .transition(&key("part_other"), OrderState::PartiallyFilled)
        .unwrap();
    // CancelPending (already being cancelled).
    rest_order(&mut ledger, "cancelp", "AAPL", 100, limit(40_000));
    ledger
        .transition(&key("cancelp"), OrderState::CancelPending)
        .unwrap();

    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    let outcomes = plan_resting_orders(&ledger, &action);

    assert!(
        matches!(find(&outcomes, "ack"), RestingOrderOutcome::Adjusted { .. }),
        "the Acked order is adjusted"
    );
    assert_eq!(
        cancel_reason(find(&outcomes, "part")),
        RestingOrderCancelReason::PartiallyFilledNotAdjustable,
        "the partially-filled affected order is cancelled fail-closed"
    );
    for id in ["new", "pend"] {
        assert_eq!(
            cancel_reason(find(&outcomes, id)),
            RestingOrderCancelReason::UnacknowledgedNotAdjustable,
            "{id} (pre-ack, affected) is cancelled fail-closed",
        );
    }
    for id in ["cancelp", "part_other"] {
        assert!(
            matches!(find(&outcomes, id), RestingOrderOutcome::Unaffected { .. }),
            "{id} must be Unaffected"
        );
    }
}

#[test]
fn srs_data_019_plan_over_ledger_selects_exactly_the_resting_matching_orders() {
    // Two resting (ACKED) AAPL orders, one resting MSFT order, one terminal
    // (REJECTED) AAPL order. A 4-for-1 AAPL split adjusts exactly the two resting
    // AAPL orders; the other symbol and the terminal order are Unaffected.
    let mut ledger = OrderLedger::new();
    for id in ["a1", "a2"] {
        rest_order(&mut ledger, id, "AAPL", 100, limit(40_000));
    }
    rest_order(&mut ledger, "m1", "MSFT", 100, limit(30_000));
    // A terminal AAPL order (REJECTED) must be Unaffected even though its symbol matches.
    ledger
        .submit(
            ClientCorrelationId::new("term").unwrap(),
            &order("AAPL", 100, limit(40_000)),
        )
        .unwrap();
    ledger
        .transition(&key("term"), OrderState::Rejected)
        .unwrap();

    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    let outcomes = plan_resting_orders(&ledger, &action);
    assert_eq!(outcomes.len(), 4, "one outcome per tracked order");

    assert!(matches!(
        find(&outcomes, "a1"),
        RestingOrderOutcome::Adjusted { .. }
    ));
    assert!(matches!(
        find(&outcomes, "a2"),
        RestingOrderOutcome::Adjusted { .. }
    ));
    assert!(
        matches!(
            find(&outcomes, "m1"),
            RestingOrderOutcome::Unaffected { .. }
        ),
        "the other-symbol order is Unaffected"
    );
    assert!(
        matches!(
            find(&outcomes, "term"),
            RestingOrderOutcome::Unaffected { .. }
        ),
        "the terminal (REJECTED) order is Unaffected"
    );
}

#[test]
fn srs_data_019_adjust_applies_as_cancel_replace_without_doubled_exposure() {
    // Applying an Adjusted outcome against the live ledger is a cancel-then-new:
    // the original moves to CANCEL_PENDING and the held replacement carries the
    // SCALED submission (never an in-place mutation of the original).
    let mut ledger = OrderLedger::new();
    rest_order(&mut ledger, "o1", "AAPL", 100, limit(40_000));

    let action = RestingOrderCorporateAction::split("AAPL", 4, 1);
    let outcome = plan_resting_order(
        &key("o1"),
        ledger.get(&key("o1")).unwrap().submission(),
        &action,
    );
    let new_submission = match outcome {
        RestingOrderOutcome::Adjusted { new_submission, .. } => new_submission,
        other => panic!("expected Adjusted, got {other:?}"),
    };

    let replacement_corr = ClientCorrelationId::new("o1-adj").unwrap();
    ledger
        .cancel_replace(&key("o1"), &new_submission, replacement_corr)
        .expect("cancel-replace of a resting (ACKED) order succeeds");

    assert_eq!(
        ledger.state(&key("o1")),
        Some(OrderState::CancelPending),
        "the original order is being cancelled, not mutated"
    );
    let replacement = ledger.get(&key("o1-adj")).expect("held replacement exists");
    assert_eq!(replacement.submission().quantity, 400, "scaled quantity");
    assert_eq!(
        replacement.submission().order_type.limit_price_minor(),
        Some(10_000),
        "scaled limit"
    );
    assert_eq!(
        replacement.replaces(),
        Some(&key("o1")),
        "the replacement audit-links to the original"
    );
}

// --------------------------------------------------------------------------- //
// Helpers
// --------------------------------------------------------------------------- //

/// Submit an order and transition it to ACKED — a realistic resting order at the
/// broker (non-terminal, and a state that cancel-replace can act on).
fn rest_order(
    ledger: &mut OrderLedger,
    id: &str,
    symbol: &str,
    quantity: i64,
    order_type: OrderType,
) {
    ledger
        .submit(
            ClientCorrelationId::new(id).unwrap(),
            &order(symbol, quantity, order_type),
        )
        .unwrap();
    ledger
        .transition(&key(id), OrderState::PendingSubmit)
        .unwrap();
    ledger.transition(&key(id), OrderState::Acked).unwrap();
}

fn find<'a>(outcomes: &'a [RestingOrderOutcome], id: &str) -> &'a RestingOrderOutcome {
    let wanted = key(id).to_string();
    outcomes
        .iter()
        .find(|outcome| outcome.key().to_string() == wanted)
        .unwrap_or_else(|| panic!("no outcome for {id}"))
}

fn adjusted_limit(outcome: &RestingOrderOutcome) -> i64 {
    match outcome {
        RestingOrderOutcome::Adjusted { new_submission, .. } => new_submission
            .order_type
            .limit_price_minor()
            .expect("limit order"),
        other => panic!("expected Adjusted, got {other:?}"),
    }
}

fn cancel_reason(outcome: &RestingOrderOutcome) -> RestingOrderCancelReason {
    match outcome {
        RestingOrderOutcome::Cancelled { reason, .. } => *reason,
        other => panic!("expected Cancelled, got {other:?}"),
    }
}
