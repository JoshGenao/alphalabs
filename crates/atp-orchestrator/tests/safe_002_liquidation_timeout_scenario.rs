//! SRS-SAFE-002 / SyRS SYS-44b / StRS SN-1.11 — the fault-injection SCENARIO
//! test: the REAL `resolve_kill_switch_timeout` gate driven end-to-end through
//! the REAL `PollingLiquidationProbe` (full 30 s window on a simulated clock),
//! the REAL SRS-NOTIF-001 `OperatorNotifier` (over fixture email/SMS
//! transports), and the REAL `IbGatewayLiquidationCleanup` (over the fixture
//! IB gateway) — the mocked-IB workflow the feature's own verification Step 2
//! prescribes.
//!
//! Acceptance shape (SYS-44b): "If a liquidation order remains unfilled after
//! 30 seconds, details are logged, email and SMS are sent, the unfilled
//! liquidation order is canceled, and IB is disconnected."
//!
//! L7 domain (safety) scenarios:
//!   (a) never-fills → refusal at exactly 30 simulated seconds; ONE page
//!       delivered on EACH of email + SMS carrying the order details; the
//!       gateway saw exactly ["cancel:B-0001", "disconnect"]; the audit event
//!       records every side effect Succeeded + manual_resolution_required.
//!   (b) fills at 10 s → acceptance; ZERO pages, ZERO gateway calls.
//!   (c) probe fault → fail closed: probe-unavailable refusal, ZERO pages,
//!       ZERO gateway calls, no timeout event.
//!   (d) premature lying probe → probe-inconsistent refusal, ZERO destructive
//!       calls.
//!   (e) both channels failing → operator_alert Failed but cancel + disconnect
//!       STILL run (continue-to-safety).
//!   (f) cancel failing → liquidation_cancel Failed but disconnect STILL runs.
//!   (g) disconnect failing → ib_disconnect Failed, recorded observably.
//!   (h) missing broker-order-id binding → cancel Failed (observable), the
//!       disconnect still runs.

use atp_orchestrator::kill_switch_timeout::{run_fixture_timeout, ProbeFault, TimeoutScenario};
use atp_types::{OrderErrorCategory, SideEffectOutcome};

#[test]
fn unfilled_liquidation_runs_the_full_sys_44b_sequence_at_thirty_seconds() {
    let scenario = TimeoutScenario::reference_unfilled();
    let run = run_fixture_timeout(&scenario).expect("reference scenario runs");

    // The gate refused with the confirmed-timeout category.
    let error = run.result.expect_err("an unfilled liquidation must refuse");
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    assert_eq!(error.category.as_str(), "KILL_SWITCH_LIQUIDATION_TIMEOUT");

    // The REAL probe polled the mocked IB for the FULL 30 s window on the
    // simulated clock (61 polls at the 500 ms cadence) — the timeout fired at
    // the deadline, not before.
    assert_eq!(run.simulated_elapsed_ms, 30_000);
    assert_eq!(run.probe_polls, 61);

    // "email and SMS are sent": the REAL dispatcher produced ONE notification
    // event whose required channels BOTH delivered, and each fixture transport
    // accepted exactly one page carrying the unfilled-order details.
    assert_eq!(run.notifications.len(), 1);
    assert_eq!(run.email_pages.len(), 1);
    assert_eq!(run.sms_pages.len(), 1);
    for page in run.email_pages.iter().chain(run.sms_pages.iter()) {
        for needle in [
            "live-momentum/ks-liq-0001",
            "SELL",
            "250",
            "AAPL",
            "MANUAL resolution",
        ] {
            assert!(
                page.body().contains(needle),
                "operator page must carry the order details; missing {needle:?} in {:?}",
                page.body()
            );
        }
    }

    // "the unfilled liquidation order is canceled, and IB is disconnected":
    // exactly one cancel (by the bound broker order id), then the disconnect —
    // in that order, nothing else.
    assert_eq!(run.gateway_calls, vec!["cancel:B-0001", "disconnect"]);

    // "details are logged": the audit event carries the unfilled order and
    // every side effect Succeeded; positions await manual resolution.
    assert_eq!(run.timeout_events.len(), 1);
    let event = &run.timeout_events[0];
    assert!(event.manual_resolution_required);
    assert!(event.outcome.is_timed_out());
    assert_eq!(event.unfilled_order.order_id, "live-momentum/ks-liq-0001");
    assert_eq!(event.operator_alert, SideEffectOutcome::Succeeded);
    assert_eq!(event.liquidation_cancel, SideEffectOutcome::Succeeded);
    assert_eq!(event.ib_disconnect, SideEffectOutcome::Succeeded);
    assert!(error.cleanup.audit_recorded);
}

#[test]
fn filled_liquidation_completes_with_zero_pages_and_zero_gateway_calls() {
    let scenario = TimeoutScenario {
        fill_after_seconds: Some(10),
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("filled scenario runs");

    let resolved = run.result.expect("a filled liquidation must complete");
    assert!(resolved.filled_before_timeout);
    assert_eq!(resolved.elapsed_seconds, 10);

    // The SYS-44b error path did not engage anywhere in the composition.
    assert!(run.notifications.is_empty());
    assert!(run.email_pages.is_empty());
    assert!(run.sms_pages.is_empty());
    assert!(run.gateway_calls.is_empty());
    // The audit transition is still recorded (filled outcome, no side effects).
    assert_eq!(run.timeout_events.len(), 1);
    assert!(!run.timeout_events[0].manual_resolution_required);
    // The probe stopped polling at the fill, well short of the window.
    assert_eq!(run.simulated_elapsed_ms, 10_000);
}

#[test]
fn probe_fault_fails_closed_with_no_destructive_action() {
    for fault in [
        ProbeFault::Connectivity,
        ProbeFault::OrderState,
        ProbeFault::ProbeTimeout,
    ] {
        let scenario = TimeoutScenario {
            probe_fault: Some(fault),
            ..TimeoutScenario::reference_unfilled()
        };
        let run = run_fixture_timeout(&scenario).expect("faulted scenario runs");

        let error = run.result.expect_err("an unconfirmable probe must refuse");
        assert_eq!(
            error.category,
            OrderErrorCategory::KillSwitchLiquidationProbeUnavailable,
            "{fault:?}"
        );
        // Fail closed on the unconfirmable order state: nothing destructive,
        // no page, no audit event, every side effect NotAttempted.
        assert!(run.gateway_calls.is_empty(), "{fault:?}");
        assert!(run.email_pages.is_empty(), "{fault:?}");
        assert!(run.sms_pages.is_empty(), "{fault:?}");
        assert!(run.timeout_events.is_empty(), "{fault:?}");
        assert_eq!(
            error.cleanup.liquidation_cancel,
            SideEffectOutcome::NotAttempted
        );
        // One poll — the probe failed fast, no retry loop.
        assert_eq!(run.probe_polls, 1, "{fault:?}");
    }
}

#[test]
fn premature_lying_probe_is_rejected_with_no_destructive_action() {
    let scenario = TimeoutScenario {
        premature_timeout_at: Some(12),
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("lying-probe scenario runs");

    let error = run
        .result
        .expect_err("a premature timeout report must be rejected");
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
    );
    assert_eq!(error.error_type, "KillSwitchLiquidationProbeInconsistent");
    // Nothing destructive fired early on an order that may still fill.
    assert!(run.gateway_calls.is_empty());
    assert!(run.email_pages.is_empty());
    assert!(run.sms_pages.is_empty());
    assert!(run.timeout_events.is_empty());
}

#[test]
fn failed_channels_still_cancel_and_disconnect() {
    let scenario = TimeoutScenario {
        fail_email: true,
        fail_sms: true,
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("failed-channel scenario runs");

    let error = run.result.expect_err("the timeout still refuses");
    assert_eq!(
        error.category,
        OrderErrorCategory::KillSwitchLiquidationTimeout
    );
    // The page attempt is observable as Failed (both required channels down)…
    assert!(error.cleanup.operator_alert.is_failed());
    assert_eq!(run.timeout_events.len(), 1);
    assert!(run.timeout_events[0].operator_alert.is_failed());
    // …and the dispatcher still produced the notification event evidence
    // (both deliveries recorded as failed, not silently dropped).
    assert_eq!(run.notifications.len(), 1);
    // Continue-to-safety: the cancel + disconnect STILL ran, in order.
    assert_eq!(run.gateway_calls, vec!["cancel:B-0001", "disconnect"]);
    assert_eq!(
        run.timeout_events[0].liquidation_cancel,
        SideEffectOutcome::Succeeded
    );
    assert_eq!(
        run.timeout_events[0].ib_disconnect,
        SideEffectOutcome::Succeeded
    );
}

#[test]
fn one_failed_channel_is_a_failed_page_but_the_other_still_receives_it() {
    let scenario = TimeoutScenario {
        fail_sms: true,
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("one-channel scenario runs");

    let error = run.result.expect_err("the timeout still refuses");
    // SYS-44b requires email AND SMS — one failed required channel is a
    // Failed page even though the other delivered.
    assert!(error.cleanup.operator_alert.is_failed());
    assert_eq!(run.email_pages.len(), 1);
    assert!(run.sms_pages.is_empty());
    assert_eq!(run.gateway_calls, vec!["cancel:B-0001", "disconnect"]);
}

#[test]
fn failed_cancel_still_disconnects_and_refuses() {
    let scenario = TimeoutScenario {
        fail_cancel: true,
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("failed-cancel scenario runs");

    let error = run.result.expect_err("the timeout still refuses");
    assert!(error.cleanup.liquidation_cancel.is_failed());
    // The failed cancel did NOT suppress the disconnect (the final safety
    // action) — both calls were attempted, in order.
    assert_eq!(run.gateway_calls, vec!["cancel:B-0001", "disconnect"]);
    assert_eq!(
        run.timeout_events[0].ib_disconnect,
        SideEffectOutcome::Succeeded
    );
    // The page still went out on both channels.
    assert_eq!(run.email_pages.len(), 1);
    assert_eq!(run.sms_pages.len(), 1);
}

#[test]
fn failed_disconnect_is_observable_and_still_refuses() {
    let scenario = TimeoutScenario {
        fail_disconnect: true,
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("failed-disconnect scenario runs");

    let error = run.result.expect_err("the timeout still refuses");
    assert!(error.cleanup.ib_disconnect.is_failed());
    assert_eq!(run.timeout_events.len(), 1);
    assert!(run.timeout_events[0].ib_disconnect.is_failed());
    assert_eq!(
        run.timeout_events[0].liquidation_cancel,
        SideEffectOutcome::Succeeded
    );
}

#[test]
fn missing_broker_binding_is_an_observable_cancel_failure_and_still_disconnects() {
    let scenario = TimeoutScenario {
        bind_broker_order_id: false,
        ..TimeoutScenario::reference_unfilled()
    };
    let run = run_fixture_timeout(&scenario).expect("missing-binding scenario runs");

    let error = run.result.expect_err("the timeout still refuses");
    // The cancel failed OBSERVABLY (no binding → no silent skip)…
    assert!(error.cleanup.liquidation_cancel.is_failed());
    match &run.timeout_events[0].liquidation_cancel {
        SideEffectOutcome::Failed { reason } => {
            assert!(reason.contains("no broker order id bound"), "{reason}");
        }
        other => panic!("expected Failed cancel, got {other:?}"),
    }
    // …and the gateway saw ONLY the disconnect (nothing to cancel with).
    assert_eq!(run.gateway_calls, vec!["disconnect"]);
}
