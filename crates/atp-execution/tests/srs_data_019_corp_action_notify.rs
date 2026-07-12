//! SRS-DATA-019 — the "operator is notified" AC clause, proven at the execution
//! layer's emission boundary.
//!
//! The AC requires that when a resting order is CANCELLED because a corporate
//! action cannot be applied, the operator is notified through the strategy
//! callback and the notification subsystem. Following the established neutral-port
//! pattern (`ConnectivityEventSink`, `KillSwitchOperatorAlertSink`,
//! DATA-013's `QuarantineSummarySink`), `atp-execution` owns the DECISION and
//! dispatches a neutral [`RestingOrderCorpActionAlert`] through the
//! [`RestingOrderCorpActionAlertSink`] port; it never depends on `atp-notification`
//! (`tools/dependency_boundary_check.py` enforces this one-way boundary).
//!
//! This suite proves the emission half solo: every CANCELLED outcome (and ONLY a
//! cancelled outcome) is dispatched through the port carrying the symbol +
//! structured reason the notification trigger and the `OrderEvent(CANCELLED)`
//! callback need; a FAILED dispatch is surfaced (never silently dropped); and a
//! partially-filled affected order is cancelled fail-closed. The deferred
//! composition-root binding maps each alert onto
//! `NotificationTrigger::critical_failure` → `OperatorNotifier::dispatch` (whose
//! dispatch-within-SLA over email + SMS is proven by SRS-NOTIF-001's own tests)
//! and onto `deliver_order_event` (SRS-SDK-004) — which is why SRS-DATA-019 lands
//! serialized until that end-to-end delivery is proven.

use std::cell::RefCell;

use atp_execution::corporate_action_orders::{
    plan_and_emit, plan_resting_order, RestingOrderAlertError, RestingOrderCancelReason,
    RestingOrderCorpActionAlert, RestingOrderCorpActionAlertSink, RestingOrderCorporateAction,
    RestingOrderOutcome,
};
use atp_types::{
    AssetClass, ClientCorrelationId, OrderKey, OrderLedger, OrderSide, OrderState, OrderSubmission,
    OrderType, StrategyId,
};

const STRATEGY: &str = "live-1";

/// A recording sink capturing every dispatched alert (the reference aggregator a
/// real notification/callback fan-out replaces at the composition root).
#[derive(Default)]
struct RecordingSink {
    alerts: RefCell<Vec<RestingOrderCorpActionAlert>>,
}

impl RestingOrderCorpActionAlertSink for RecordingSink {
    fn dispatch(&self, alert: RestingOrderCorpActionAlert) -> Result<(), RestingOrderAlertError> {
        self.alerts.borrow_mut().push(alert);
        Ok(())
    }
}

/// A sink whose transport always fails — proves a missed operator page is surfaced.
struct FailingSink;

impl RestingOrderCorpActionAlertSink for FailingSink {
    fn dispatch(&self, _alert: RestingOrderCorpActionAlert) -> Result<(), RestingOrderAlertError> {
        Err(RestingOrderAlertError::new("email/SMS gateway unreachable"))
    }
}

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

/// Submit + drive to ACKED — a realistic resting order at the broker.
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

#[test]
fn srs_data_019_only_cancelled_outcomes_are_dispatched_through_the_alert_sink() {
    // A 1-for-10 reverse split on ZZZ: one un-adjustable order (5 shares → cancel),
    // one adjustable order (100 shares), one order on a different symbol.
    let mut ledger = OrderLedger::new();
    rest_order(&mut ledger, "cancel", "ZZZ", 5, limit(1_000));
    rest_order(&mut ledger, "adjust", "ZZZ", 100, limit(1_000));
    rest_order(&mut ledger, "other", "AAA", 100, limit(1_000));

    let sink = RecordingSink::default();
    let action = RestingOrderCorporateAction::split("ZZZ", 1, 10);
    let report = plan_and_emit(&ledger, &action, &sink);

    // Exactly ONE alert — for the cancelled order. Adjusted / Unaffected are silent
    // (the AC scopes operator notification to the cancel path).
    let alerts = sink.alerts.borrow();
    assert_eq!(alerts.len(), 1, "only the cancelled order is dispatched");
    let alert = &alerts[0];
    assert_eq!(alert.symbol, "ZZZ");
    assert_eq!(alert.order_id, key("cancel").to_string());
    assert_eq!(
        alert.reason,
        RestingOrderCancelReason::QuantityNotIntegral {
            before: 5,
            numerator: 1,
            denominator: 10,
        }
    );

    // The dispatched content is what the notification trigger summary + the callback
    // reason carry — the operator sees the order, the symbol, and why.
    let summary = alert.operator_summary();
    assert!(
        summary.contains("ZZZ") && summary.contains("fractional share"),
        "{summary}"
    );
    assert_eq!(
        alert.callback_reason(),
        summary,
        "callback reason == operator summary"
    );

    assert_eq!(report.outcomes.len(), 3);
    assert!(report.alert_failures.is_empty(), "every dispatch succeeded");
}

#[test]
fn srs_data_019_failed_alert_dispatch_is_surfaced_not_swallowed() {
    // A missed operator page on a corp-action cancel is itself a safety event; the
    // failure MUST be surfaced so the composition root escalates.
    let mut ledger = OrderLedger::new();
    rest_order(&mut ledger, "cancel", "DEAD", 10, OrderType::Market);

    let report = plan_and_emit(
        &ledger,
        &RestingOrderCorporateAction::delisting("DEAD"),
        &FailingSink,
    );

    assert_eq!(
        report.alert_failures.len(),
        1,
        "the failed page is surfaced"
    );
    let failure = &report.alert_failures[0];
    assert_eq!(failure.order_id, key("cancel").to_string());
    assert_eq!(failure.symbol, "DEAD");
    assert_eq!(failure.error.reason, "email/SMS gateway unreachable");
}

#[test]
fn srs_data_019_partially_filled_affected_order_is_cancelled_fail_closed() {
    // A partially-filled order still has a live resting remainder; under a corporate
    // action it is cancelled fail-closed (never left working at the pre-action basis),
    // and the operator is notified.
    let mut ledger = OrderLedger::new();
    rest_order(&mut ledger, "part", "ZZZ", 100, limit(1_000));
    ledger
        .transition(&key("part"), OrderState::PartiallyFilled)
        .unwrap();

    let sink = RecordingSink::default();
    // Even a clean forward split (which WOULD adjust an Acked order) cancels a
    // partially-filled order, because its remaining working quantity is unknown here.
    let report = plan_and_emit(
        &ledger,
        &RestingOrderCorporateAction::split("ZZZ", 4, 1),
        &sink,
    );

    match &report.outcomes[..] {
        [RestingOrderOutcome::Cancelled { reason, .. }] => {
            assert_eq!(
                *reason,
                RestingOrderCancelReason::PartiallyFilledNotAdjustable
            );
        }
        other => panic!("expected one Cancelled outcome, got {other:?}"),
    }
    assert_eq!(sink.alerts.borrow().len(), 1, "the operator is notified");
}

#[test]
fn srs_data_019_delisting_alert_carries_the_cancel_and_reason() {
    // A delisting cancels the resting order and emits the alert + the RestingOrderCancel
    // the execution runtime routes to the broker.
    let submission = order("DEAD", 10, OrderType::Market);
    let action = RestingOrderCorporateAction::delisting("DEAD");
    let outcome = plan_resting_order(&key("d1"), &submission, &action);

    assert!(matches!(outcome, RestingOrderOutcome::Cancelled { .. }));

    let alert = outcome
        .alert()
        .expect("a cancelled outcome yields an alert");
    assert_eq!(alert.reason, RestingOrderCancelReason::Delisting);
    assert!(alert.operator_summary().contains("delisted"));

    let cancel = outcome
        .resting_order_cancel()
        .expect("a cancelled outcome yields a RestingOrderCancel");
    assert_eq!(cancel.order_id, key("d1").to_string());
    assert_eq!(cancel.symbol, "DEAD");
    // The engine plans the decision; the runtime binds the broker handle.
    assert_eq!(cancel.broker_order_id, None);
}

#[test]
fn srs_data_019_no_alerts_when_nothing_is_cancelled() {
    // A clean forward split adjusts every ACKED order — no operator notification.
    let mut ledger = OrderLedger::new();
    rest_order(&mut ledger, "a", "AAPL", 100, limit(40_000));
    rest_order(&mut ledger, "b", "AAPL", 200, limit(20_000));

    let sink = RecordingSink::default();
    let report = plan_and_emit(
        &ledger,
        &RestingOrderCorporateAction::split("AAPL", 4, 1),
        &sink,
    );

    assert!(
        sink.alerts.borrow().is_empty(),
        "an all-adjust split raises no operator alert"
    );
    assert!(report.alert_failures.is_empty());
}
