//! SRS-SAFE-002 / SyRS SYS-44b — the concrete `PollingLiquidationProbe` wait
//! loop, driven end-to-end on a simulated clock (no test sleeps):
//!
//!   * a liquidation that fills mid-window returns `FilledBeforeTimeout` with
//!     a truncated in-window `elapsed_seconds`;
//!   * a liquidation that never fills polls at the 500 ms cadence to the
//!     EXACT 30 s deadline (61 polls) and only then reports
//!     `TimedOutUnfilled { elapsed >= timeout, timeout = request's }` — the
//!     probe never reports a timeout early (the gate's outcome-consistency
//!     hardening would reject that as an inconsistency);
//!   * every `BrokerReconcileError` kind maps to its typed
//!     `KillSwitchProbeError` variant and fails the probe immediately (fail
//!     closed on an unconfirmable order state — no retry, no guess);
//!   * an order ABSENT from an `OpenOnly` snapshot is ambiguous →
//!     `OrderStateUnavailable`; absent under `OpenAndRecentlyCompleted` keeps
//!     polling to the deadline;
//!   * a terminal-but-not-filled order (broker-side `Cancelled`) still waits
//!     to the deadline — only a broker-confirmed `Filled` is a fill.

use std::cell::Cell;

use atp_execution::{
    BrokerOpenOrder, BrokerOpenOrderSnapshot, BrokerOpenOrderSource, BrokerReconcileError,
    KillSwitchLiquidationProbe, KillSwitchProbeClock, KillSwitchProbeError,
    PollingLiquidationProbe, SnapshotCoverage, KILL_SWITCH_FILL_POLL_INTERVAL_MS,
};
use atp_types::{
    ClientCorrelationId, KillSwitchLiquidationOutcome, KillSwitchTimeoutRequest, OrderKey,
    OrderState, StrategyId, UnfilledLiquidationOrder, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
};

/// Simulated monotonic clock: `wait_ms` advances the reading instead of
/// sleeping, so the full 30 s wait loop runs instantly.
#[derive(Default)]
struct SimulatedClock {
    now_ms: Cell<u64>,
}

impl KillSwitchProbeClock for SimulatedClock {
    fn monotonic_ms(&self) -> u64 {
        self.now_ms.get()
    }

    fn wait_ms(&self, ms: u64) {
        self.now_ms.set(self.now_ms.get() + ms);
    }
}

fn liquidation_key() -> OrderKey {
    OrderKey::new(
        StrategyId::new("live-momentum"),
        ClientCorrelationId::new("ks-liq-0001").expect("valid correlation id"),
    )
}

fn timeout_request() -> KillSwitchTimeoutRequest {
    KillSwitchTimeoutRequest {
        live_strategy_id: StrategyId::new("live-momentum"),
        unfilled_order: UnfilledLiquidationOrder {
            // The SAFE-001 binding convention: the domain order_id is the
            // OrderKey's Display form ("strategy/correlation").
            order_id: liquidation_key().to_string(),
            symbol: "AAPL".to_string(),
            side: "SELL".to_string(),
            quantity: 250,
        },
        timeout_seconds: KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
    }
}

/// Scripted broker order-state feed, keyed to the shared simulated clock: the
/// liquidation order presents `Acked` until `fill_at_ms` (if any), then
/// `Filled`. Records the poll count so the cadence is testable.
struct ScriptedFillFeed<'a> {
    clock: &'a SimulatedClock,
    fill_at_ms: Option<u64>,
    coverage: SnapshotCoverage,
    include_order: bool,
    terminal_state: Option<OrderState>,
    error: Option<BrokerReconcileError>,
    polls: Cell<u32>,
}

impl<'a> ScriptedFillFeed<'a> {
    fn filling_at(clock: &'a SimulatedClock, fill_at_ms: u64) -> Self {
        Self {
            clock,
            fill_at_ms: Some(fill_at_ms),
            coverage: SnapshotCoverage::OpenAndRecentlyCompleted,
            include_order: true,
            terminal_state: None,
            error: None,
            polls: Cell::new(0),
        }
    }

    fn never_filling(clock: &'a SimulatedClock) -> Self {
        Self {
            fill_at_ms: None,
            ..Self::filling_at(clock, 0)
        }
    }

    fn failing(clock: &'a SimulatedClock, error: BrokerReconcileError) -> Self {
        Self {
            error: Some(error),
            ..Self::never_filling(clock)
        }
    }

    fn absent(clock: &'a SimulatedClock, coverage: SnapshotCoverage) -> Self {
        Self {
            include_order: false,
            coverage,
            ..Self::never_filling(clock)
        }
    }

    fn terminal(clock: &'a SimulatedClock, state: OrderState) -> Self {
        Self {
            terminal_state: Some(state),
            ..Self::never_filling(clock)
        }
    }
}

impl BrokerOpenOrderSource for ScriptedFillFeed<'_> {
    fn open_orders(&self) -> Result<BrokerOpenOrderSnapshot, BrokerReconcileError> {
        self.polls.set(self.polls.get() + 1);
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        let mut orders = Vec::new();
        if self.include_order {
            let filled = self
                .fill_at_ms
                .is_some_and(|at| self.clock.monotonic_ms() >= at);
            let state = if filled {
                OrderState::Filled
            } else {
                self.terminal_state.unwrap_or(OrderState::Acked)
            };
            orders.push(BrokerOpenOrder {
                key: liquidation_key(),
                broker_order_id: "B-0001".to_string(),
                state,
            });
        }
        Ok(BrokerOpenOrderSnapshot::new(orders, self.coverage))
    }
}

#[test]
fn probe_reports_filled_before_timeout_with_in_window_elapsed() {
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::filling_at(&clock, 1_200); // fills on the 3rd poll (1 000 ms → no, 1 200 ms → poll at 1 500)
    let probe = PollingLiquidationProbe::new(&clock, &feed);

    let outcome = probe
        .await_filled_or_timeout(&timeout_request())
        .expect("a scripted feed must not error");

    match outcome {
        KillSwitchLiquidationOutcome::FilledBeforeTimeout { elapsed_seconds } => {
            // Filled at the 1 500 ms poll → truncated to 1 s, well in-window.
            assert_eq!(elapsed_seconds, 1);
        }
        other => panic!("expected FilledBeforeTimeout, got {other:?}"),
    }
    // Polls at 0, 500, 1000, 1500 ms — the 1 500 ms poll observes the fill.
    assert_eq!(feed.polls.get(), 4);
}

#[test]
fn probe_polls_to_the_exact_deadline_then_reports_a_consistent_timeout() {
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::never_filling(&clock);
    let probe = PollingLiquidationProbe::new(&clock, &feed);
    let request = timeout_request();

    let outcome = probe
        .await_filled_or_timeout(&request)
        .expect("a scripted feed must not error");

    match outcome {
        KillSwitchLiquidationOutcome::TimedOutUnfilled {
            elapsed_seconds,
            timeout_seconds,
        } => {
            // The loop's final poll lands exactly at the 30 000 ms deadline:
            // the outcome is consistent (elapsed >= timeout, timeout echoes
            // the request) so the gate's hardening accepts it.
            assert_eq!(elapsed_seconds, KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS);
            assert_eq!(timeout_seconds, request.timeout_seconds);
            assert!(elapsed_seconds >= timeout_seconds);
        }
        other => panic!("expected TimedOutUnfilled, got {other:?}"),
    }
    // 30 000 ms / 500 ms cadence → polls at 0, 500, …, 30 000 = 61 polls.
    assert_eq!(feed.polls.get(), 61);
    assert_eq!(clock.monotonic_ms(), 30_000);
}

type ExpectedProbeError = fn(&KillSwitchProbeError) -> bool;

#[test]
fn probe_maps_each_reconcile_error_kind_to_its_typed_variant_and_fails_fast() {
    let cases: Vec<(BrokerReconcileError, ExpectedProbeError)> = vec![
        (
            BrokerReconcileError::connectivity_blocked("gateway unreachable"),
            |e| matches!(e, KillSwitchProbeError::ConnectivityBlocked { .. }),
        ),
        (
            BrokerReconcileError::timeout("order-state query deadline"),
            |e| matches!(e, KillSwitchProbeError::ProbeTimeout { .. }),
        ),
        (BrokerReconcileError::stale_data("snapshot too old"), |e| {
            matches!(e, KillSwitchProbeError::OrderStateUnavailable { .. })
        }),
        (
            BrokerReconcileError::malformed_snapshot("duplicate keys"),
            |e| matches!(e, KillSwitchProbeError::OrderStateUnavailable { .. }),
        ),
        (
            BrokerReconcileError::unavailable("order-state service down"),
            |e| matches!(e, KillSwitchProbeError::OrderStateUnavailable { .. }),
        ),
    ];
    for (error, is_expected) in cases {
        let clock = SimulatedClock::default();
        let feed = ScriptedFillFeed::failing(&clock, error.clone());
        let probe = PollingLiquidationProbe::new(&clock, &feed);

        let probe_error = probe
            .await_filled_or_timeout(&timeout_request())
            .expect_err("a failing source must fail the probe closed");

        assert!(
            is_expected(&probe_error),
            "{error:?} mapped to unexpected {probe_error:?}"
        );
        // Fail-fast: one poll, no retry loop on an unconfirmable state.
        assert_eq!(feed.polls.get(), 1);
        // No simulated time was consumed — the gate refuses immediately.
        assert_eq!(clock.monotonic_ms(), 0);
    }
}

#[test]
fn probe_treats_absence_from_an_open_only_snapshot_as_unconfirmable() {
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::absent(&clock, SnapshotCoverage::OpenOnly);
    let probe = PollingLiquidationProbe::new(&clock, &feed);

    let probe_error = probe
        .await_filled_or_timeout(&timeout_request())
        .expect_err("an ambiguous absent order must fail the probe closed");

    assert!(matches!(
        probe_error,
        KillSwitchProbeError::OrderStateUnavailable { .. }
    ));
    assert!(probe_error.reason().contains("OpenOnly"));
    assert_eq!(feed.polls.get(), 1);
}

#[test]
fn probe_keeps_polling_when_absent_from_a_complete_snapshot_then_times_out() {
    // Absent under OpenAndRecentlyCompleted proves "not filled recently" —
    // the probe keeps waiting (never guesses a fill) and reports the
    // consistent timeout at the deadline.
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::absent(&clock, SnapshotCoverage::OpenAndRecentlyCompleted);
    let probe = PollingLiquidationProbe::new(&clock, &feed);
    let request = timeout_request();

    let outcome = probe
        .await_filled_or_timeout(&request)
        .expect("a complete-coverage absence is not an error");

    assert!(matches!(
        outcome,
        KillSwitchLiquidationOutcome::TimedOutUnfilled { .. }
    ));
    assert_eq!(feed.polls.get(), 61);
}

#[test]
fn probe_never_reports_a_timeout_early_for_a_broker_cancelled_order() {
    // A broker-side Cancelled liquidation is NOT a fill — and the probe must
    // not short-circuit into an early TimedOutUnfilled either (the gate would
    // reject that as a probe inconsistency). It waits out the full window.
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::terminal(&clock, OrderState::Cancelled);
    let probe = PollingLiquidationProbe::new(&clock, &feed);
    let request = timeout_request();

    let outcome = probe
        .await_filled_or_timeout(&request)
        .expect("a cancelled order is observable, not an error");

    match outcome {
        KillSwitchLiquidationOutcome::TimedOutUnfilled {
            elapsed_seconds,
            timeout_seconds,
        } => {
            assert!(elapsed_seconds >= timeout_seconds);
            assert_eq!(timeout_seconds, request.timeout_seconds);
        }
        other => panic!("expected TimedOutUnfilled, got {other:?}"),
    }
    assert_eq!(clock.monotonic_ms(), 30_000);
}

#[test]
fn probe_final_wait_is_clamped_to_the_remaining_window() {
    // A custom cadence that does not divide the window evenly: the last wait
    // is min(interval, remaining) so the deadline poll lands exactly at
    // 30 000 ms — never past it by a full interval.
    let clock = SimulatedClock::default();
    let feed = ScriptedFillFeed::never_filling(&clock);
    let probe = PollingLiquidationProbe::with_poll_interval(&clock, &feed, 7_000);
    let request = timeout_request();

    let outcome = probe
        .await_filled_or_timeout(&request)
        .expect("a scripted feed must not error");

    assert!(matches!(
        outcome,
        KillSwitchLiquidationOutcome::TimedOutUnfilled {
            elapsed_seconds: 30,
            timeout_seconds: 30,
        }
    ));
    // Polls at 0, 7 000, 14 000, 21 000, 28 000, then a clamped 2 000 ms wait
    // → the final poll at exactly 30 000 ms.
    assert_eq!(clock.monotonic_ms(), 30_000);
    assert_eq!(feed.polls.get(), 6);
}

#[test]
fn probe_poll_cadence_matches_the_declared_constant() {
    assert_eq!(KILL_SWITCH_FILL_POLL_INTERVAL_MS, 500);
}
