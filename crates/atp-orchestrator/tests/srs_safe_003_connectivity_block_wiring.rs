//! SRS-SAFE-003 / ERR-2 — connectivity-block fault-injection wiring test.
//!
//! Drives [`run_connectivity_block_scenario`] directly (the scenario the
//! `safe003_connectivity_block_cli` operator surface runs) and asserts the
//! aggregate evidence for each injected connectivity state. The scenario routes
//! a DESIGNATED-LIVE order through the REAL
//! `ExecutionEngine::dispatch_order → route_order → submit_live_order` authority
//! chain over the real `InteractiveBrokersBrokerage` behind a deterministic
//! recording transport, with the connectivity state fault-injected.
//!
//! Post-conditions proven:
//!   * `Unreachable` / `ScheduledRestartWindow` — the live submission is refused
//!     with `CONNECTIVITY_BLOCKED`, ZERO IB orders are created, one reconnect is
//!     requested, one `ConnectivityEvent` is published (with the right
//!     `scheduled_restart` flag and the submitting strategy).
//!   * `Connected` — the SAME order routes through to the broker (one IB order,
//!     no reconnect, no event): the block is SELECTIVE, not a blanket disable.
//!   * a non-designated paper order routes to the simulation engine and never
//!     touches IB, in EVERY connectivity state.

use atp_orchestrator::order_routing_wiring::{
    run_connectivity_block_scenario, LiveConnectivityOutcome, SCENARIO_LIVE_STRATEGY,
};
use atp_types::ConnectivityState;

#[test]
fn unreachable_blocks_live_submission_with_zero_ib_one_reconnect_one_event() {
    let ev = run_connectivity_block_scenario(ConnectivityState::Unreachable)
        .expect("the unreachable scenario runs");

    match &ev.live_outcome {
        LiveConnectivityOutcome::Blocked {
            category,
            error_type,
            message,
        } => {
            assert_eq!(
                category, "CONNECTIVITY_BLOCKED",
                "SRS-SAFE-003: the wire string must be CONNECTIVITY_BLOCKED"
            );
            assert_eq!(error_type, "IbGatewayUnreachable");
            assert!(
                message.contains(SCENARIO_LIVE_STRATEGY),
                "the envelope message must name the submitting strategy:\n{message}"
            );
            assert!(
                message.contains("SRS-SAFE-003"),
                "the envelope message must trace SRS-SAFE-003:\n{message}"
            );
        }
        other => panic!("Unreachable must block the live submission, got {other:?}"),
    }

    assert_eq!(ev.designated, SCENARIO_LIVE_STRATEGY);
    assert_eq!(
        ev.ib_orders_created, 0,
        "no IB order side effect — the broker must never be called while unreachable"
    );
    assert_eq!(
        ev.reconnects, 1,
        "SRS-SAFE-003: the engine must attempt exactly one reconnect when blocked"
    );
    assert_eq!(
        ev.events_recorded, 1,
        "exactly one ConnectivityEvent must be published for dashboard alerting"
    );
    assert_eq!(
        ev.event_scheduled_restart,
        Some(false),
        "Unreachable is the unscheduled connectivity-loss path"
    );
    assert_eq!(
        ev.event_strategy.as_deref(),
        Some(SCENARIO_LIVE_STRATEGY),
        "the event must be attributed to the live order, not the paper contrast"
    );
    assert!(
        ev.non_designated_sim_receipt.starts_with("paper-"),
        "the non-designated paper order must simulate (receipt {:?})",
        ev.non_designated_sim_receipt
    );
}

#[test]
fn scheduled_restart_window_blocks_with_suppression_flag_set() {
    let ev = run_connectivity_block_scenario(ConnectivityState::ScheduledRestartWindow)
        .expect("the scheduled-restart scenario runs");

    match &ev.live_outcome {
        LiveConnectivityOutcome::Blocked {
            category,
            error_type,
            ..
        } => {
            assert_eq!(category, "CONNECTIVITY_BLOCKED");
            assert_eq!(error_type, "IbGatewayUnreachable");
        }
        other => panic!("ScheduledRestartWindow must block the live submission, got {other:?}"),
    }

    assert_eq!(ev.ib_orders_created, 0);
    assert_eq!(
        ev.reconnects, 1,
        "SRS-MD-005: reconnect attempts continue during the restart window"
    );
    assert_eq!(ev.events_recorded, 1);
    assert_eq!(
        ev.event_scheduled_restart,
        Some(true),
        "SRS-MD-005: the suppression flag must be set on scheduled-restart events"
    );
    assert_eq!(ev.event_strategy.as_deref(), Some(SCENARIO_LIVE_STRATEGY));
    assert!(ev.non_designated_sim_receipt.starts_with("paper-"));
}

#[test]
fn connected_routes_live_order_through_and_creates_one_ib_order() {
    // Negative control: the block must be SELECTIVE. A Connected live order still
    // reaches the broker — else the gate would disable the live path even when IB
    // is healthy.
    let ev = run_connectivity_block_scenario(ConnectivityState::Connected)
        .expect("the connected scenario runs");

    match &ev.live_outcome {
        LiveConnectivityOutcome::RoutedThrough { broker_order_id } => {
            assert!(
                broker_order_id.starts_with("IB-"),
                "the broker must mint an order id (got {broker_order_id:?})"
            );
        }
        other => panic!("Connected must route the live order through, got {other:?}"),
    }

    assert_eq!(
        ev.ib_orders_created, 1,
        "a Connected live order must create exactly one IB order"
    );
    assert_eq!(
        ev.reconnects, 0,
        "no reconnect should be requested when IB is reachable"
    );
    assert_eq!(
        ev.events_recorded, 0,
        "no connectivity event should be emitted on the happy path"
    );
    assert_eq!(ev.event_scheduled_restart, None);
    assert_eq!(ev.event_strategy, None);
    assert!(ev.non_designated_sim_receipt.starts_with("paper-"));
}

#[test]
fn non_designated_paper_never_touches_ib_in_any_state() {
    for state in [
        ConnectivityState::Unreachable,
        ConnectivityState::ScheduledRestartWindow,
        ConnectivityState::Connected,
    ] {
        let ev = run_connectivity_block_scenario(state)
            .unwrap_or_else(|err| panic!("scenario runs for {state:?}: {err}"));
        assert!(
            ev.non_designated_sim_receipt.starts_with("paper-"),
            "a non-designated paper order must route to simulation in {state:?} \
             (receipt {:?})",
            ev.non_designated_sim_receipt
        );
    }

    // Under a blocked state the WHOLE run creates zero IB orders (the live order
    // is refused before the broker and the paper order never reaches it).
    for state in [
        ConnectivityState::Unreachable,
        ConnectivityState::ScheduledRestartWindow,
    ] {
        let ev = run_connectivity_block_scenario(state).unwrap();
        assert_eq!(
            ev.ib_orders_created, 0,
            "a blocked run must create zero IB orders in {state:?}"
        );
    }
}
