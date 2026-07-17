//! ERR-8 / SRS-SAFE-002 / SyRS SYS-44b / StRS SN-1.11 — when a kill-switch
//! liquidation order stays unfilled past the configured timeout (default
//! 30 s), the execution engine's `resolve_kill_switch_timeout` gate runs the
//! SYS-44b error path: it logs the unfilled order details, notifies the
//! operator by email AND SMS, cancels the unfilled liquidation order,
//! disconnects from IB, and refuses with `KILL_SWITCH_LIQUIDATION_TIMEOUT`
//! (positions then await manual resolution). On filled-before-timeout the
//! kill switch completed in time and the error path does not engage — no
//! page, no cancel, no disconnect.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * Timeout: `Err` with category `KillSwitchLiquidationTimeout` (wire
//!     string `KILL_SWITCH_LIQUIDATION_TIMEOUT`); the operator is paged
//!     exactly once over email + SMS; the unfilled order is canceled exactly
//!     once; IB is disconnected exactly once; the audit event records
//!     `manual_resolution_required == true`; the probe is consulted once.
//!   * Filled (positive control): `Ok` with `filled_before_timeout == true`,
//!     and the forbidden alert sink + forbidden IB-cleanup are NEVER invoked
//!     (they panic if they are) — proving the gate is selective.
//!   * Failed cancel / page / disconnect are each recorded as `Failed` and
//!     still refuse (return `Err`).
//!   * Fail-closed: a probe that mislabels an over-deadline liquidation as
//!     filled is normalised to a timeout (page + cancel + disconnect fire).
//!   * Best-effort audit: a failing event sink does not abort the gate.
//!   * Pseudo-property sweep over varying `(elapsed, timeout)` keeps every
//!     timeout refusing with exactly one page + one cancel + one disconnect.

use atp_execution::{
    ExecutionEngine, IbLiquidationCleanup, KillSwitchLiquidationProbe, KillSwitchOperatorAlertSink,
    KillSwitchProbeError, KillSwitchSideEffectError, KillSwitchTimeoutEventSink,
};
use atp_types::{
    KillSwitchAlertEvent, KillSwitchLiquidationOutcome, KillSwitchTimeoutEvent,
    KillSwitchTimeoutRequest, OperatorAlertChannel, OrderErrorCategory, SideEffectOutcome,
    StrategyId, UnfilledLiquidationOrder, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
};
use std::cell::{Cell, RefCell};

struct KillSwitchLiquidationProbeSpy {
    outcome: Cell<KillSwitchLiquidationOutcome>,
    calls: Cell<u32>,
}

impl KillSwitchLiquidationProbeSpy {
    fn timed_out(elapsed_seconds: u64, timeout_seconds: u64) -> Self {
        Self {
            outcome: Cell::new(KillSwitchLiquidationOutcome::TimedOutUnfilled {
                elapsed_seconds,
                timeout_seconds,
            }),
            calls: Cell::new(0),
        }
    }

    fn filled(elapsed_seconds: u64) -> Self {
        Self {
            outcome: Cell::new(KillSwitchLiquidationOutcome::FilledBeforeTimeout {
                elapsed_seconds,
            }),
            calls: Cell::new(0),
        }
    }
}

impl KillSwitchLiquidationProbe for KillSwitchLiquidationProbeSpy {
    fn await_filled_or_timeout(
        &self,
        _request: &KillSwitchTimeoutRequest,
    ) -> Result<KillSwitchLiquidationOutcome, KillSwitchProbeError> {
        self.calls.set(self.calls.get() + 1);
        Ok(self.outcome.get())
    }
}

/// Probe that cannot confirm the fill — models connectivity loss / order-state
/// unavailability / probe timeout while awaiting fill confirmation. The gate
/// must fail closed WITHOUT any automated cancel/disconnect and refuse with the
/// distinct probe-unavailable category.
#[derive(Default)]
struct KillSwitchLiquidationProbeFailing {
    calls: Cell<u32>,
}

impl KillSwitchLiquidationProbe for KillSwitchLiquidationProbeFailing {
    fn await_filled_or_timeout(
        &self,
        _request: &KillSwitchTimeoutRequest,
    ) -> Result<KillSwitchLiquidationOutcome, KillSwitchProbeError> {
        self.calls.set(self.calls.get() + 1);
        Err(KillSwitchProbeError::connectivity_blocked(
            "IB fill-confirmation stream lost",
        ))
    }
}

#[derive(Default)]
struct KillSwitchOperatorAlertSinkSpy {
    alerts: RefCell<Vec<KillSwitchAlertEvent>>,
}

impl KillSwitchOperatorAlertSink for KillSwitchOperatorAlertSinkSpy {
    fn dispatch(&self, event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError> {
        self.alerts.borrow_mut().push(event);
        Ok(())
    }
}

/// Alert sink that records the call but reports failure — models an
/// unreachable email/SMS transport. The gate must still record
/// `operator_alert = Failed` and still refuse.
#[derive(Default)]
struct OperatorAlertFailingSink {
    alerts: RefCell<Vec<KillSwitchAlertEvent>>,
}

impl KillSwitchOperatorAlertSink for OperatorAlertFailingSink {
    fn dispatch(&self, event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError> {
        self.alerts.borrow_mut().push(event);
        Err(KillSwitchSideEffectError::new("SMS gateway timed out"))
    }
}

/// Alert sink that panics if consulted. Used by the filled positive control to
/// prove no operator page is dispatched when liquidation fills in time.
struct OperatorAlertForbiddenSink;

impl KillSwitchOperatorAlertSink for OperatorAlertForbiddenSink {
    fn dispatch(&self, _event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError> {
        panic!("ERR-8: FilledBeforeTimeout branch must not page the operator");
    }
}

#[derive(Default)]
struct IbLiquidationCleanupSpy {
    cancels: RefCell<Vec<KillSwitchTimeoutRequest>>,
    disconnects: Cell<u32>,
}

impl IbLiquidationCleanup for IbLiquidationCleanupSpy {
    fn cancel_unfilled_liquidation_order(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.cancels.borrow_mut().push(request.clone());
        Ok(())
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        self.disconnects.set(self.disconnects.get() + 1);
        Ok(())
    }
}

/// IB cleanup that records each call but reports failure — models a failed IB
/// `cancel_order` and a wedged disconnect. The gate must still attempt both,
/// record each as `Failed`, and still refuse.
#[derive(Default)]
struct IbLiquidationFailingCleanup {
    cancels: RefCell<Vec<KillSwitchTimeoutRequest>>,
    disconnects: Cell<u32>,
}

impl IbLiquidationCleanup for IbLiquidationFailingCleanup {
    fn cancel_unfilled_liquidation_order(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.cancels.borrow_mut().push(request.clone());
        Err(KillSwitchSideEffectError::new(
            "IB cancel_order unreachable",
        ))
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        self.disconnects.set(self.disconnects.get() + 1);
        Err(KillSwitchSideEffectError::new("IB disconnect wedged"))
    }
}

/// IB cleanup that panics if consulted. Used by the filled positive control to
/// prove the cancel + disconnect paths are never invoked on an in-time fill.
struct IbLiquidationForbiddenCleanup;

impl IbLiquidationCleanup for IbLiquidationForbiddenCleanup {
    fn cancel_unfilled_liquidation_order(
        &self,
        _request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError> {
        panic!("ERR-8: FilledBeforeTimeout branch must not cancel any liquidation order");
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        panic!("ERR-8: FilledBeforeTimeout branch must not disconnect from IB");
    }
}

#[derive(Default)]
struct KillSwitchTimeoutEventSinkSpy {
    events: RefCell<Vec<KillSwitchTimeoutEvent>>,
}

impl KillSwitchTimeoutEventSink for KillSwitchTimeoutEventSinkSpy {
    fn record(&self, event: KillSwitchTimeoutEvent) -> Result<(), KillSwitchSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// Event sink that records the event but reports a publication failure — models
/// an unwritable audit log. The gate must treat emission as best-effort: it
/// must NOT panic or abort, and the safety side effects + the refusal stand.
#[derive(Default)]
struct KillSwitchTimeoutEventFailingSink {
    events: RefCell<Vec<KillSwitchTimeoutEvent>>,
}

impl KillSwitchTimeoutEventSink for KillSwitchTimeoutEventFailingSink {
    fn record(&self, event: KillSwitchTimeoutEvent) -> Result<(), KillSwitchSideEffectError> {
        self.events.borrow_mut().push(event);
        Err(KillSwitchSideEffectError::new("audit log unwritable"))
    }
}

fn timeout_request(strategy: &str, order: &str, timeout_seconds: u64) -> KillSwitchTimeoutRequest {
    KillSwitchTimeoutRequest {
        live_strategy_id: StrategyId::new(strategy),
        unfilled_order: UnfilledLiquidationOrder {
            order_id: order.to_string(),
            symbol: "AAPL".to_string(),
            side: "SELL".to_string(),
            quantity: 250,
        },
        timeout_seconds,
    }
}

const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

#[test]
fn err_8_timeout_pages_email_sms_cancels_disconnects_and_refuses() {
    // SRS-SAFE-002 / SYS-44b: the liquidation stayed unfilled — page the
    // operator (email + SMS), cancel the unfilled order, disconnect from IB,
    // and refuse so positions await manual resolution.
    let engine = ExecutionEngine::default();
    let probe =
        KillSwitchLiquidationProbeSpy::timed_out(41, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
    let alerts = KillSwitchOperatorAlertSinkSpy::default();
    let cleanup = IbLiquidationCleanupSpy::default();
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request.clone(),
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: an unfilled liquidation timeout must refuse");

    // Structured error: category + SyRS SYS-64 wire string + SRS trace.
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    assert_eq!(error.category.as_str(), "KILL_SWITCH_LIQUIDATION_TIMEOUT");
    assert_eq!(error.original_request, request);
    assert!(error.message.contains("SRS-SAFE-002"));
    assert!(error.message.contains("SYS-44b"));
    assert!(error.message.contains("live-momentum"));
    assert!(error.message.contains("ord-7791"));

    // The probe is the timing authority, consulted exactly once.
    assert_eq!(probe.calls.get(), 1);

    // The operator is paged over email + SMS, exactly once.
    let alerts_seen = alerts.alerts.borrow();
    assert_eq!(alerts_seen.len(), 1);
    let alert = &alerts_seen[0];
    assert!(alert.channels.contains(&OperatorAlertChannel::Email));
    assert!(alert.channels.contains(&OperatorAlertChannel::Sms));
    assert!(!alert.channels.contains(&OperatorAlertChannel::Dashboard));
    assert_eq!(alert.elapsed_seconds, 41);
    assert_eq!(alert.unfilled_order, request.unfilled_order);

    // The unfilled liquidation order is canceled once and IB is disconnected once.
    let cancels = cleanup.cancels.borrow();
    assert_eq!(cancels.len(), 1);
    assert_eq!(cancels[0], request);
    assert_eq!(cleanup.disconnects.get(), 1);

    // The audit event records manual resolution required and each side effect
    // Succeeded (the spies returned Ok).
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].manual_resolution_required);
    assert!(events_seen[0].outcome.is_timed_out());
    assert_eq!(events_seen[0].operator_alert, SideEffectOutcome::Succeeded);
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::Succeeded
    );
    assert_eq!(events_seen[0].ib_disconnect, SideEffectOutcome::Succeeded);
}

#[test]
fn err_8_filled_before_timeout_completes_with_no_page_cancel_or_disconnect() {
    // SRS-SAFE-002: liquidation filled in time — the SYS-44b error path does
    // not engage. The forbidden stubs panic if any side effect is touched.
    let engine = ExecutionEngine::default();
    let probe = KillSwitchLiquidationProbeSpy::filled(7);
    let alerts = OperatorAlertForbiddenSink;
    let cleanup = IbLiquidationForbiddenCleanup;
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let resolved = engine
        .resolve_kill_switch_timeout(
            request.clone(),
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect("ERR-8: a filled-before-timeout liquidation must complete");

    assert!(resolved.filled_before_timeout);
    assert_eq!(resolved.live_strategy_id, request.live_strategy_id);
    assert_eq!(resolved.elapsed_seconds, 7);
    assert_eq!(probe.calls.get(), 1);

    // The audit transition is still recorded, with no manual resolution and
    // all side effects NotAttempted.
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(!events_seen[0].manual_resolution_required);
    assert!(!events_seen[0].outcome.is_timed_out());
    assert_eq!(
        events_seen[0].operator_alert,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(
        events_seen[0].ib_disconnect,
        SideEffectOutcome::NotAttempted
    );
}

#[test]
fn err_8_failed_page_cancel_and_disconnect_are_observable_and_still_refuse() {
    // SRS-SAFE-002 observability: when the page, the IB cancel, AND the
    // disconnect all fail, the gate must still attempt ALL THREE (a failed
    // cancel must not suppress the page or the disconnect), record each as
    // Failed so the failure is not indistinguishable from success, and still
    // refuse (return Err).
    let engine = ExecutionEngine::default();
    let probe =
        KillSwitchLiquidationProbeSpy::timed_out(50, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
    let alerts = OperatorAlertFailingSink::default();
    let cleanup = IbLiquidationFailingCleanup::default();
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request,
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: a timeout must refuse even when side effects fail");

    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );

    // The error message must NOT claim the side effects succeeded — it says the
    // cleanup was *attempted* and points to the event for each outcome (the
    // audit event below carries the Failed truth).
    assert!(error.message.contains("attempted"));
    assert!(!error.message.contains("order canceled"));
    assert!(!error.message.contains("IB disconnected;"));

    // ALL THREE side effects were attempted despite each failing.
    assert_eq!(alerts.alerts.borrow().len(), 1);
    assert_eq!(cleanup.cancels.borrow().len(), 1);
    assert_eq!(cleanup.disconnects.get(), 1);

    // The event records each failure (observable, not silent).
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].manual_resolution_required);
    assert!(events_seen[0].operator_alert.is_failed());
    assert!(events_seen[0].liquidation_cancel.is_failed());
    assert!(events_seen[0].ib_disconnect.is_failed());
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::Failed {
            reason: "IB cancel_order unreachable".to_string(),
        }
    );
    // The same failures are carried ON the error (self-contained recovery
    // facts); here the audit sink succeeded so audit_recorded is true.
    assert!(error.cleanup.audit_recorded);
    assert!(error.cleanup.operator_alert.is_failed());
    assert!(error.cleanup.liquidation_cancel.is_failed());
    assert!(error.cleanup.ib_disconnect.is_failed());
}

#[test]
fn err_8_probe_unavailable_refuses_without_any_automated_action() {
    // SRS-SAFE-002 / finding-2: when the fill-confirmation probe fails (cannot
    // confirm whether the liquidation filled), the gate must fail closed — it
    // must NOT pretend the order filled and must NOT fire the destructive
    // cleanup (cancel / disconnect) on an unconfirmable order state. It refuses
    // with the DISTINCT probe-unavailable category and takes no automated
    // order/session action. The forbidden cleanup + alert sinks panic if
    // touched, proving no side effect runs.
    let engine = ExecutionEngine::default();
    let probe = KillSwitchLiquidationProbeFailing::default();
    let alerts = OperatorAlertForbiddenSink;
    let cleanup = IbLiquidationForbiddenCleanup;
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request.clone(),
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: an unconfirmable probe must refuse");

    // Distinct category — NOT misclassified as a confirmed timeout.
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
    );
    assert_eq!(
        error.category.as_str(),
        "KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE"
    );
    assert_eq!(error.original_request, request);
    // The typed probe-error kind travels into the structured message so the
    // operator can tell WHICH degraded path blocked confirmation.
    assert!(error
        .message
        .contains("probe error: CONNECTIVITY_BLOCKED: IB fill-confirmation stream lost"));
    assert_eq!(probe.calls.get(), 1);

    // No automated action: no event recorded (the forbidden alert/cleanup would
    // have panicked if dispatched), and the error's cleanup record shows every
    // side effect NotAttempted.
    assert!(events.events.borrow().is_empty());
    assert_eq!(
        error.cleanup.operator_alert,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(
        error.cleanup.liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(error.cleanup.ib_disconnect, SideEffectOutcome::NotAttempted);
}

#[test]
fn err_8_filled_over_deadline_is_failed_closed_and_refuses() {
    // Defense-in-depth: a probe that mislabels an over-deadline liquidation as
    // FilledBeforeTimeout (elapsed 45 > 30 s timeout) must NOT skip the SYS-44b
    // cleanup. The gate normalises it to a timeout: the page + cancel +
    // disconnect fire and the gate refuses.
    let engine = ExecutionEngine::default();
    let probe = KillSwitchLiquidationProbeSpy::filled(45);
    let alerts = KillSwitchOperatorAlertSinkSpy::default();
    let cleanup = IbLiquidationCleanupSpy::default();
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request,
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: a filled outcome past the timeout must fail closed");

    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    assert_eq!(alerts.alerts.borrow().len(), 1);
    assert_eq!(cleanup.cancels.borrow().len(), 1);
    assert_eq!(cleanup.disconnects.get(), 1);
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].manual_resolution_required);
    assert!(events_seen[0].outcome.is_timed_out());
}

#[test]
fn err_8_audit_sink_failure_is_best_effort_and_safety_posture_holds() {
    // SRS-SAFE-002: if the timeout event sink fails (audit log unwritable),
    // the gate must NOT panic or abort — the page + cancel + disconnect still
    // fire and the gate still refuses. Event emission is best-effort.
    let engine = ExecutionEngine::default();
    let probe =
        KillSwitchLiquidationProbeSpy::timed_out(40, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
    let alerts = KillSwitchOperatorAlertSinkSpy::default();
    let cleanup = IbLiquidationCleanupSpy::default();
    let events = KillSwitchTimeoutEventFailingSink::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request,
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: a timeout refuses even when the audit sink fails");

    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    // The safety side effects still fired despite the audit-sink failure.
    assert_eq!(alerts.alerts.borrow().len(), 1);
    assert_eq!(cleanup.cancels.borrow().len(), 1);
    assert_eq!(cleanup.disconnects.get(), 1);
    // The sink was invoked (it recorded, then reported the publication failure).
    assert_eq!(events.events.borrow().len(), 1);
    // Recovery-critical: because the durable audit emission FAILED, the
    // per-side-effect outcomes must still be observable ON the returned error
    // (they would otherwise be lost). audit_recorded flags the lost durable
    // record.
    assert!(!error.cleanup.audit_recorded);
    assert_eq!(error.cleanup.operator_alert, SideEffectOutcome::Succeeded);
    assert_eq!(
        error.cleanup.liquidation_cancel,
        SideEffectOutcome::Succeeded
    );
    assert_eq!(error.cleanup.ib_disconnect, SideEffectOutcome::Succeeded);
}

#[test]
fn err_8_premature_timeout_report_is_rejected_without_any_automated_action() {
    // Outcome-consistency hardening: a probe reporting TimedOutUnfilled at
    // 12 s against a 30 s deadline is INCONSISTENT — trusting it would cancel
    // + disconnect EARLY on an order that may still lawfully fill. The gate
    // must reject with the distinct probe-inconsistent discriminator and take
    // NO automated action (forbidden stubs panic if touched).
    let engine = ExecutionEngine::default();
    let probe =
        KillSwitchLiquidationProbeSpy::timed_out(12, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
    let alerts = OperatorAlertForbiddenSink;
    let cleanup = IbLiquidationForbiddenCleanup;
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request.clone(),
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: a premature timeout report must be rejected");

    // Fail-closed family (untrustworthy fill confirmation), distinct
    // discriminator.
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
    );
    assert_eq!(error.error_type, "KillSwitchLiquidationProbeInconsistent");
    assert_eq!(error.original_request, request);
    assert!(error.message.contains("INCONSISTENT"));
    assert_eq!(probe.calls.get(), 1);

    // Nothing destructive ran: no event, every side effect NotAttempted.
    assert!(events.events.borrow().is_empty());
    assert_eq!(
        error.cleanup.operator_alert,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(
        error.cleanup.liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(error.cleanup.ib_disconnect, SideEffectOutcome::NotAttempted);
}

#[test]
fn err_8_mismatched_timeout_report_is_rejected_without_any_automated_action() {
    // A TimedOutUnfilled carrying a DIFFERENT timeout_seconds than the request
    // (60 vs 30) is version-skewed / misconfigured — same non-destructive
    // rejection as the premature report, even though elapsed exceeds the
    // request's deadline.
    let engine = ExecutionEngine::default();
    let probe = KillSwitchLiquidationProbeSpy::timed_out(65, 60);
    let alerts = OperatorAlertForbiddenSink;
    let cleanup = IbLiquidationForbiddenCleanup;
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request.clone(),
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: a mismatched-timeout report must be rejected");

    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
    );
    assert_eq!(error.error_type, "KillSwitchLiquidationProbeInconsistent");
    assert!(events.events.borrow().is_empty());
    assert_eq!(
        error.cleanup,
        atp_types::KillSwitchCleanupOutcome::not_attempted()
    );
}

#[test]
fn err_8_boundary_timeout_at_exact_deadline_runs_the_cleanup() {
    // Boundary control: elapsed == timeout == the request's 30 s deadline is a
    // CONSISTENT timeout — the SYS-44b cleanup must fire normally (this pins
    // the hardening to strictly-premature reports only).
    let engine = ExecutionEngine::default();
    let probe = KillSwitchLiquidationProbeSpy::timed_out(
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );
    let alerts = KillSwitchOperatorAlertSinkSpy::default();
    let cleanup = IbLiquidationCleanupSpy::default();
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    let error = engine
        .resolve_kill_switch_timeout(
            request,
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: an exact-deadline timeout must refuse");

    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    assert_eq!(alerts.alerts.borrow().len(), 1);
    assert_eq!(cleanup.cancels.borrow().len(), 1);
    assert_eq!(cleanup.disconnects.get(), 1);
}

/// Alert sink + cleanup sharing one call log, so the CROSS-PORT ordering of
/// the timeout branch is observable: the destructive broker-side safety
/// actions (cancel, then disconnect) must run BEFORE the operator page — the
/// concrete SRS-NOTIF-001 dispatcher sends email/SMS synchronously with
/// per-channel deadlines, and a slow notification transport must never delay
/// killing the live order or severing the session.
struct OrderedAlertSink<'a> {
    log: &'a RefCell<Vec<&'static str>>,
}

impl KillSwitchOperatorAlertSink for OrderedAlertSink<'_> {
    fn dispatch(&self, _event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError> {
        self.log.borrow_mut().push("alert");
        Ok(())
    }
}

struct OrderedCleanup<'a> {
    log: &'a RefCell<Vec<&'static str>>,
}

impl IbLiquidationCleanup for OrderedCleanup<'_> {
    fn cancel_unfilled_liquidation_order(
        &self,
        _request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.log.borrow_mut().push("cancel");
        Ok(())
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        self.log.borrow_mut().push("disconnect");
        Ok(())
    }
}

#[test]
fn err_8_broker_safety_actions_run_before_the_operator_page() {
    // Ordering is safety-load-bearing: cancel → disconnect → alert. A page
    // dispatched first could sit behind tens of seconds of synchronous
    // email/SMS transport latency while the unfilled liquidation order stays
    // live on a connected session.
    let engine = ExecutionEngine::default();
    let probe =
        KillSwitchLiquidationProbeSpy::timed_out(41, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
    let log = RefCell::new(Vec::new());
    let alerts = OrderedAlertSink { log: &log };
    let cleanup = OrderedCleanup { log: &log };
    let events = KillSwitchTimeoutEventSinkSpy::default();
    let request = timeout_request(
        "live-momentum",
        "ord-7791",
        KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    );

    engine
        .resolve_kill_switch_timeout(
            request,
            &probe,
            &alerts,
            &cleanup,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-8: the timeout refuses");

    assert_eq!(*log.borrow(), vec!["cancel", "disconnect", "alert"]);
}

#[test]
fn err_8_timeout_refuses_across_many_liquidations() {
    // Pseudo-property sweep: every timeout outcome refuses and emits exactly
    // one page (email + SMS) + one cancel + one disconnect, regardless of the
    // (elapsed, timeout) numerics.
    let engine = ExecutionEngine::default();
    let cases = [(31_u64, 30_u64), (45, 30), (90, 30), (60, 20)];
    for (elapsed, timeout) in cases {
        let probe = KillSwitchLiquidationProbeSpy::timed_out(elapsed, timeout);
        let alerts = KillSwitchOperatorAlertSinkSpy::default();
        let cleanup = IbLiquidationCleanupSpy::default();
        let events = KillSwitchTimeoutEventSinkSpy::default();
        let request = timeout_request("live-x", "ord-y", timeout);

        let error = engine
            .resolve_kill_switch_timeout(
                request,
                &probe,
                &alerts,
                &cleanup,
                &events,
                OBSERVED_AT_SECONDS,
            )
            .expect_err("ERR-8: every liquidation timeout must refuse");

        assert_eq!(
            error.category,
            OrderErrorCategory::KillSwitchLiquidationTimeout
        );
        let alerts_seen = alerts.alerts.borrow();
        assert_eq!(alerts_seen.len(), 1);
        assert_eq!(alerts_seen[0].channels.len(), 2);
        assert_eq!(cleanup.cancels.borrow().len(), 1);
        assert_eq!(cleanup.disconnects.get(), 1);
        let events_seen = events.events.borrow();
        assert_eq!(events_seen.len(), 1);
        assert!(events_seen[0].manual_resolution_required);
    }
}
