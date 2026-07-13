//! SRS-DATA-020 — the "notify the operator" AC clause, proven at the execution
//! layer's emission boundary.
//!
//! The AC requires that a delisting marks the position delisted AND notifies the
//! operator. Following the established neutral-port pattern (`KillSwitchOperatorAlertSink`,
//! DATA-019's `RestingOrderCorpActionAlertSink`), `atp-execution` owns the DECISION and
//! dispatches a neutral [`PositionCorpActionAlert`] through the
//! [`PositionCorpActionAlertSink`] port; it never depends on `atp-notification`
//! (`tools/dependency_boundary_check.py` enforces this one-way boundary).
//!
//! This suite proves the emission half solo: every `Delisted` and every
//! `RequiresManualReview` outcome (and ONLY those) is dispatched through the port
//! carrying the symbol + reason the notification trigger needs; a routine adjust /
//! remap raises no operator page; and a FAILED dispatch is surfaced (never silently
//! dropped). The deferred composition-root binding maps each alert onto
//! `NotificationTrigger::critical_failure` (SRS-NOTIF-001) — which is why SRS-DATA-020
//! lands serialized until that end-to-end delivery is proven.

use std::cell::RefCell;

use atp_execution::corporate_action_positions::{
    plan_and_emit, LivePosition, PositionAlertError, PositionCorpActionAlert,
    PositionCorpActionAlertSink, PositionCorpActionOutcome, PositionCorporateAction,
};

/// A recording sink capturing every dispatched alert (the reference aggregator a real
/// notification fan-out replaces at the composition root).
#[derive(Default)]
struct RecordingSink {
    alerts: RefCell<Vec<PositionCorpActionAlert>>,
}

impl PositionCorpActionAlertSink for RecordingSink {
    fn dispatch(&self, alert: PositionCorpActionAlert) -> Result<(), PositionAlertError> {
        self.alerts.borrow_mut().push(alert);
        Ok(())
    }
}

/// A sink whose transport always fails — proves a missed operator page is surfaced.
struct FailingSink;

impl PositionCorpActionAlertSink for FailingSink {
    fn dispatch(&self, _alert: PositionCorpActionAlert) -> Result<(), PositionAlertError> {
        Err(PositionAlertError::new("email/SMS gateway unreachable"))
    }
}

fn pos(symbol: &str, quantity: i64, basis: i128) -> LivePosition {
    LivePosition::new(symbol, quantity, basis).expect("valid position")
}

#[test]
fn srs_data_020_delisting_dispatches_exactly_one_operator_alert() {
    // A delisting on ZZZ pages the operator; a position on another symbol is silent.
    let positions = vec![pos("ZZZ", 100, 500_000), pos("AAA", 100, 500_000)];
    let sink = RecordingSink::default();
    let report = plan_and_emit(
        &positions,
        &PositionCorporateAction::delisting("ZZZ"),
        &sink,
    );

    let alerts = sink.alerts.borrow();
    assert_eq!(alerts.len(), 1, "only the delisted position is dispatched");
    assert_eq!(alerts[0].symbol, "ZZZ");
    assert!(
        alerts[0].operator_summary().contains("delisted"),
        "{}",
        alerts[0].operator_summary()
    );
    assert_eq!(report.outcomes.len(), 2);
    assert!(report.alert_failures.is_empty(), "every dispatch succeeded");
}

#[test]
fn srs_data_020_manual_review_pages_the_operator() {
    // A fractional reverse split cannot be applied — the operator is paged for review.
    let positions = vec![pos("ZZZ", 105, 105_000)];
    let sink = RecordingSink::default();
    let report = plan_and_emit(
        &positions,
        &PositionCorporateAction::split("ZZZ", 1, 10),
        &sink,
    );

    assert!(matches!(
        report.outcomes[0],
        PositionCorpActionOutcome::RequiresManualReview { .. }
    ));
    let alerts = sink.alerts.borrow();
    assert_eq!(alerts.len(), 1, "the review pages the operator");
    assert!(alerts[0].operator_summary().contains("fractional share"));
}

#[test]
fn srs_data_020_routine_adjust_and_remap_raise_no_operator_alert() {
    // A clean forward split adjusts; a clean symbol change remaps — neither pages the
    // operator (the AC scopes operator notification to delisting + review).
    let sink = RecordingSink::default();
    let split = plan_and_emit(
        &[pos("AAPL", 100, 500_000)],
        &PositionCorporateAction::split("AAPL", 4, 1),
        &sink,
    );
    assert!(matches!(
        split.outcomes[0],
        PositionCorpActionOutcome::Adjusted { .. }
    ));

    let relabel = plan_and_emit(
        &[pos("OLD", 100, 500_000)],
        &PositionCorporateAction::symbol_change("OLD", "NEW"),
        &sink,
    );
    assert!(matches!(
        relabel.outcomes[0],
        PositionCorpActionOutcome::Remapped { .. }
    ));

    assert!(
        sink.alerts.borrow().is_empty(),
        "a routine adjust / remap raises no operator alert"
    );
}

#[test]
fn srs_data_020_failed_alert_dispatch_is_surfaced_not_swallowed() {
    // A missed page on a delisting is itself a safety event; the failure MUST be
    // surfaced so the composition root escalates.
    let report = plan_and_emit(
        &[pos("DEAD", 100, 500_000)],
        &PositionCorporateAction::delisting("DEAD"),
        &FailingSink,
    );

    assert_eq!(
        report.alert_failures.len(),
        1,
        "the failed page is surfaced"
    );
    let failure = &report.alert_failures[0];
    assert_eq!(failure.symbol, "DEAD");
    assert_eq!(failure.error.reason, "email/SMS gateway unreachable");
    // The delisting decision itself still stands even though the page failed.
    assert!(matches!(
        report.outcomes[0],
        PositionCorpActionOutcome::Delisted { .. }
    ));
}

#[test]
fn srs_data_020_report_carries_every_outcome_even_when_a_page_fails() {
    // A single corporate action affects at most one position (positions are unique per
    // canonical symbol), so at most one page is dispatched. When that page fails the
    // report still carries EVERY outcome — the delisted one AND the untouched others —
    // so a failed page never hides the rest of the book from the composition root.
    let positions = vec![pos("DEAD", 100, 500_000), pos("LIVE", 100, 500_000)];
    let report = plan_and_emit(
        &positions,
        &PositionCorporateAction::delisting("DEAD"),
        &FailingSink,
    );

    assert_eq!(report.outcomes.len(), 2, "every position is reported");
    assert_eq!(
        report.alert_failures.len(),
        1,
        "the single failed page is surfaced"
    );
    let live = report
        .outcomes
        .iter()
        .find(|o| o.symbol() == "LIVE")
        .expect("LIVE outcome present");
    assert!(matches!(live, PositionCorpActionOutcome::Unaffected { .. }));
}
