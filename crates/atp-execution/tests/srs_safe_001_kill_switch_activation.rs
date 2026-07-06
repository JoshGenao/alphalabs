//! SRS-SAFE-001 / SyRS SYS-44a / NFR-P3 / StRS SN-1.11 — the kill-switch
//! ACTIVATION gate: on activation the execution engine halts every paper
//! simulation engine (first — the 1 s SRS-LOG-001 observability budget must
//! not sit behind up to 5 s of lawful brokerage I/O), cancels every resting
//! live-strategy order, submits an opposite-direction market liquidation for
//! every open live-strategy position, and disconnects from the brokerage
//! LAST ("IB Gateway is disconnected after liquidation orders are
//! submitted"). Continue-to-safety: every phase is attempted regardless of
//! earlier failures, every outcome is recorded, and the report is returned on
//! every path.
//!
//! L7 domain (safety) suite. Post-conditions:
//!   * Global phase ordering on a shared call log: halt < every cancel <
//!     every liquidation < disconnect; disconnect strictly after the LAST
//!     liquidation.
//!   * Exactly the resting (non-terminal) live-strategy orders are cancelled,
//!     once each; terminal and non-live-strategy orders are untouched; the
//!     broker binding (or its honest absence) is carried on each cancel.
//!   * Every open position gets exactly one validated market liquidation:
//!     long → SELL |net|, short → BUY |net|.
//!   * Fault injection: a failed halt / cancel / liquidation / disconnect is
//!     recorded as `Failed { reason }` and every later phase is still
//!     attempted; `fully_clean()` is false.
//!   * Empty state: no cancels, no liquidations, halt + disconnect still run,
//!     report is fully clean.
//!   * The event sink receives the exact report (best-effort: a failing sink
//!     changes nothing).
//!   * Timings are measured on the injected monotonic clock (monotone marks;
//!     `within_nfr_p3` reflects the measured mark, not a constant).
//!   * Determinism: identical fixture + clock → identical report.

use std::cell::{Cell, RefCell};
use std::collections::BTreeSet;
use std::rc::Rc;

use atp_execution::{
    ExecutionEngine, KillSwitchActivationEventSink, KillSwitchBrokerageControl, KillSwitchClock,
    KillSwitchSideEffectError, LiveExecutionState, PaperHaltFanout,
};
use atp_types::{
    ClientCorrelationId, KillSwitchActivationEvent, KillSwitchActivationReport,
    KillSwitchActivationRequest, OrderKey, OrderLedger, OrderSide, OrderState, OrderSubmission,
    OrderType, PaperHaltSummary, SideEffectOutcome, StrategyId, KILL_SWITCH_ACTIVATION_BUDGET_MS,
};

const LIVE: &str = "alpha-live";
const PAPER: &str = "paper-07";

type CallLog = Rc<RefCell<Vec<String>>>;

/// Deterministic monotonic clock: every read advances by `step_ms`.
struct StepClock {
    now_ms: Cell<u64>,
    step_ms: u64,
}

impl StepClock {
    fn with_step(step_ms: u64) -> Self {
        Self {
            now_ms: Cell::new(0),
            step_ms,
        }
    }
}

impl KillSwitchClock for StepClock {
    fn monotonic_ms(&self) -> u64 {
        let now = self.now_ms.get();
        self.now_ms.set(now + self.step_ms);
        now
    }
}

struct BrokerageSpy {
    log: CallLog,
    fail_cancel_order_ids: BTreeSet<String>,
    fail_liquidation_symbols: BTreeSet<String>,
    fail_disconnect: bool,
    cancels: RefCell<Vec<atp_types::RestingOrderCancel>>,
    liquidations: RefCell<Vec<OrderSubmission>>,
}

impl BrokerageSpy {
    fn clean(log: CallLog) -> Self {
        Self {
            log,
            fail_cancel_order_ids: BTreeSet::new(),
            fail_liquidation_symbols: BTreeSet::new(),
            fail_disconnect: false,
            cancels: RefCell::new(Vec::new()),
            liquidations: RefCell::new(Vec::new()),
        }
    }
}

impl KillSwitchBrokerageControl for BrokerageSpy {
    fn cancel_resting_order(
        &self,
        cancel: &atp_types::RestingOrderCancel,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.log
            .borrow_mut()
            .push(format!("cancel:{}", cancel.order_id));
        self.cancels.borrow_mut().push(cancel.clone());
        if self.fail_cancel_order_ids.contains(&cancel.order_id) {
            return Err(KillSwitchSideEffectError::new(format!(
                "injected cancel failure for {}",
                cancel.order_id
            )));
        }
        Ok(())
    }

    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.log
            .borrow_mut()
            .push(format!("liquidate:{}", submission.symbol));
        self.liquidations.borrow_mut().push(submission.clone());
        if self.fail_liquidation_symbols.contains(&submission.symbol) {
            return Err(KillSwitchSideEffectError::new(format!(
                "injected liquidation failure for {}",
                submission.symbol
            )));
        }
        Ok(())
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        self.log.borrow_mut().push("disconnect".to_string());
        if self.fail_disconnect {
            return Err(KillSwitchSideEffectError::new(
                "injected disconnect failure",
            ));
        }
        Ok(())
    }
}

struct FanoutSpy {
    log: CallLog,
    summary: Option<PaperHaltSummary>,
}

impl FanoutSpy {
    fn halting(log: CallLog, engines: u64) -> Self {
        Self {
            log,
            summary: Some(PaperHaltSummary {
                engines_total: engines,
                transitioned: engines,
                already_halted: 0,
            }),
        }
    }

    fn failing(log: CallLog) -> Self {
        Self { log, summary: None }
    }
}

impl PaperHaltFanout for FanoutSpy {
    fn halt_all_for_kill_switch(&self) -> Result<PaperHaltSummary, KillSwitchSideEffectError> {
        self.log.borrow_mut().push("halt".to_string());
        match self.summary {
            Some(summary) => Ok(summary),
            None => Err(KillSwitchSideEffectError::new("injected fan-out failure")),
        }
    }
}

struct EventSinkSpy {
    recorded: RefCell<Vec<KillSwitchActivationEvent>>,
    fail: bool,
}

impl EventSinkSpy {
    fn recording() -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail: false,
        }
    }

    fn failing() -> Self {
        Self {
            recorded: RefCell::new(Vec::new()),
            fail: true,
        }
    }
}

impl KillSwitchActivationEventSink for EventSinkSpy {
    fn record(&self, event: KillSwitchActivationEvent) -> Result<(), KillSwitchSideEffectError> {
        self.recorded.borrow_mut().push(event);
        if self.fail {
            return Err(KillSwitchSideEffectError::new("injected sink failure"));
        }
        Ok(())
    }
}

fn correlation(value: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(value).expect("non-blank correlation id")
}

fn market_order(strategy: &str, symbol: &str, quantity: i64, side: OrderSide) -> OrderSubmission {
    OrderSubmission::new(
        StrategyId::new(strategy),
        symbol,
        quantity,
        atp_types::AssetClass::Equity,
        side,
        OrderType::Market,
    )
}

/// The canonical fixture: live strategy `alpha-live` with
///   * `c-rest-new`  — resting (NEW), never reached the broker (no binding);
///   * `c-rest-ack`  — resting (ACKED), broker id `B-ACK`;
///   * `c-filled`    — terminal (FILLED) — must NOT be cancelled;
///   * one PAPER-strategy resting order — must NOT be cancelled;
///
/// and open positions AAPL +100 (long) / MSFT -50 (short).
fn fixture_state() -> LiveExecutionState {
    let mut ledger = OrderLedger::new();
    let live = StrategyId::new(LIVE);

    ledger
        .submit(
            correlation("c-rest-new"),
            &market_order(LIVE, "AAPL", 10, OrderSide::Buy),
        )
        .expect("submit resting-new");

    ledger
        .submit(
            correlation("c-rest-ack"),
            &market_order(LIVE, "MSFT", 5, OrderSide::Sell),
        )
        .expect("submit resting-acked");
    let ack_key = OrderKey::new(live.clone(), correlation("c-rest-ack"));
    ledger
        .transition(&ack_key, OrderState::PendingSubmit)
        .expect("resting-acked -> PENDING_SUBMIT");
    ledger
        .transition(&ack_key, OrderState::Acked)
        .expect("resting-acked -> ACKED");

    ledger
        .submit(
            correlation("c-filled"),
            &market_order(LIVE, "NVDA", 7, OrderSide::Buy),
        )
        .expect("submit filled");
    let filled_key = OrderKey::new(live.clone(), correlation("c-filled"));
    ledger
        .transition(&filled_key, OrderState::PendingSubmit)
        .expect("filled -> PENDING_SUBMIT");
    ledger
        .transition(&filled_key, OrderState::Acked)
        .expect("filled -> ACKED");
    ledger
        .transition(&filled_key, OrderState::Filled)
        .expect("filled -> FILLED");

    ledger
        .submit(
            correlation("c-paper"),
            &market_order(PAPER, "TSLA", 3, OrderSide::Buy),
        )
        .expect("submit paper-strategy order");

    LiveExecutionState::new(ledger)
        .with_broker_id(ack_key, "B-ACK")
        .expect("bind broker id")
        .with_position("AAPL", 100)
        .expect("long position")
        .with_position("MSFT", -50)
        .expect("short position")
        .with_live_strategy(&StrategyId::new(LIVE))
        .expect("live designation record")
}

fn request() -> KillSwitchActivationRequest {
    KillSwitchActivationRequest {
        activation_id: "act-0001".to_string(),
        live_strategy_id: StrategyId::new(LIVE),
        activated_at_epoch_ms: 1_750_000_000_000,
    }
}

fn activate_with(
    brokerage: &BrokerageSpy,
    fanout: &FanoutSpy,
    events: &EventSinkSpy,
    clock: &StepClock,
) -> KillSwitchActivationReport {
    ExecutionEngine::default().activate_kill_switch(
        request(),
        &fixture_state(),
        clock,
        brokerage,
        fanout,
        events,
    )
}

#[test]
fn srs_safe_001_phase_ordering_halt_cancels_liquidations_disconnect() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    activate_with(&brokerage, &fanout, &events, &clock);

    let log = log.borrow();
    let position = |prefix: &str, last: bool| -> usize {
        let mut found = None;
        for (index, entry) in log.iter().enumerate() {
            if entry.starts_with(prefix) {
                found = Some(index);
                if !last {
                    break;
                }
            }
        }
        found.unwrap_or_else(|| panic!("no {prefix:?} call in {log:?}"))
    };

    let halt = position("halt", false);
    let first_cancel = position("cancel:", false);
    let last_cancel = position("cancel:", true);
    let first_liquidation = position("liquidate:", false);
    let last_liquidation = position("liquidate:", true);
    let disconnect = position("disconnect", false);

    assert!(
        halt < first_cancel,
        "halt must run before any cancel: {log:?}"
    );
    assert!(
        last_cancel < first_liquidation,
        "every cancel must precede the first liquidation: {log:?}"
    );
    assert!(
        last_liquidation < disconnect,
        "disconnect must come strictly after the LAST liquidation: {log:?}"
    );
    assert_eq!(
        log.iter().filter(|entry| *entry == "disconnect").count(),
        1,
        "exactly one disconnect"
    );
}

#[test]
fn srs_safe_001_cancels_exactly_the_resting_live_strategy_orders() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = activate_with(&brokerage, &fanout, &events, &clock);

    let cancels = brokerage.cancels.borrow();
    assert_eq!(
        cancels.len(),
        2,
        "exactly the two resting live orders: {cancels:?}"
    );
    assert_eq!(report.resting_order_cancels.len(), 2);

    let new_order = cancels
        .iter()
        .find(|cancel| cancel.order_id.contains("c-rest-new"))
        .expect("the NEW resting order is cancelled");
    assert_eq!(
        new_order.broker_order_id, None,
        "an order that never reached the broker carries an honest None binding"
    );
    let acked_order = cancels
        .iter()
        .find(|cancel| cancel.order_id.contains("c-rest-ack"))
        .expect("the ACKED resting order is cancelled");
    assert_eq!(acked_order.broker_order_id.as_deref(), Some("B-ACK"));

    assert!(
        !cancels
            .iter()
            .any(|cancel| cancel.order_id.contains("c-filled")),
        "a terminal (FILLED) order must not be cancelled"
    );
    assert!(
        !cancels.iter().any(|cancel| cancel.order_id.contains(PAPER)),
        "another strategy's resting order must not be cancelled"
    );
    assert!(report
        .resting_order_cancels
        .iter()
        .all(|cancel| cancel.outcome == SideEffectOutcome::Succeeded));
}

#[test]
fn srs_safe_001_liquidations_close_every_position_opposite_side_abs_quantity() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = activate_with(&brokerage, &fanout, &events, &clock);

    let submissions = brokerage.liquidations.borrow();
    assert_eq!(submissions.len(), 2, "one liquidation per open position");
    for submission in submissions.iter() {
        assert_eq!(submission.order_type, OrderType::Market);
        assert_eq!(submission.strategy_id, StrategyId::new(LIVE));
        assert!(
            submission.validate().is_ok(),
            "gate submits only validated orders"
        );
    }
    let aapl = submissions
        .iter()
        .find(|submission| submission.symbol == "AAPL")
        .expect("AAPL liquidation");
    assert_eq!(
        (aapl.side, aapl.quantity),
        (OrderSide::Sell, 100),
        "long 100 → SELL 100"
    );
    let msft = submissions
        .iter()
        .find(|submission| submission.symbol == "MSFT")
        .expect("MSFT liquidation");
    assert_eq!(
        (msft.side, msft.quantity),
        (OrderSide::Buy, 50),
        "short 50 → BUY 50"
    );

    assert_eq!(report.liquidations.len(), 2);
    assert!(report
        .liquidations
        .iter()
        .all(|liquidation| liquidation.outcome == SideEffectOutcome::Succeeded));
    assert!(report.fully_clean());
}

#[test]
fn srs_safe_001_failures_are_recorded_and_never_stop_later_phases() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let mut brokerage = BrokerageSpy::clean(Rc::clone(&log));
    // Fail the NEW resting order's cancel, the AAPL liquidation, and the
    // disconnect — every later phase must still be attempted in full.
    brokerage
        .fail_cancel_order_ids
        .insert(format!("{LIVE}/c-rest-new"));
    brokerage
        .fail_liquidation_symbols
        .insert("AAPL".to_string());
    brokerage.fail_disconnect = true;
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = activate_with(&brokerage, &fanout, &events, &clock);

    assert_eq!(
        brokerage.cancels.borrow().len(),
        2,
        "both cancels still attempted"
    );
    assert_eq!(
        brokerage.liquidations.borrow().len(),
        2,
        "both liquidations still attempted"
    );
    assert_eq!(
        log.borrow()
            .iter()
            .filter(|entry| *entry == "disconnect")
            .count(),
        1,
        "disconnect still attempted after failures"
    );

    let failed_cancel = report
        .resting_order_cancels
        .iter()
        .find(|cancel| cancel.order.order_id.contains("c-rest-new"))
        .expect("failed cancel present in report");
    assert!(failed_cancel.outcome.is_failed(), "cancel failure recorded");
    let ok_cancel = report
        .resting_order_cancels
        .iter()
        .find(|cancel| cancel.order.order_id.contains("c-rest-ack"))
        .expect("other cancel present");
    assert_eq!(ok_cancel.outcome, SideEffectOutcome::Succeeded);

    let failed_liquidation = report
        .liquidations
        .iter()
        .find(|liquidation| liquidation.symbol == "AAPL")
        .expect("AAPL liquidation present");
    assert!(failed_liquidation.outcome.is_failed());
    assert!(report.ib_disconnect.is_failed());
    assert!(
        !report.fully_clean(),
        "any failure anywhere means NOT fully clean"
    );
}

#[test]
fn srs_safe_001_failed_paper_halt_is_recorded_and_brokerage_phases_still_run() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::failing(Rc::clone(&log));
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = activate_with(&brokerage, &fanout, &events, &clock);

    assert!(report.paper_halt.is_failed(), "fan-out failure recorded");
    assert_eq!(report.paper_halt_summary, None, "no summary fabricated");
    assert_eq!(brokerage.cancels.borrow().len(), 2);
    assert_eq!(brokerage.liquidations.borrow().len(), 2);
    assert!(!report.fully_clean());
}

#[test]
fn srs_safe_001_empty_state_still_halts_and_disconnects() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 0);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = ExecutionEngine::default().activate_kill_switch(
        request(),
        &LiveExecutionState::new(OrderLedger::new()),
        &clock,
        &brokerage,
        &fanout,
        &events,
    );

    assert!(report.resting_order_cancels.is_empty());
    assert!(report.liquidations.is_empty());
    assert_eq!(report.paper_halt, SideEffectOutcome::Succeeded);
    assert_eq!(
        log.borrow().as_slice(),
        ["halt".to_string(), "disconnect".to_string()],
        "halt + disconnect run even with nothing to cancel or liquidate"
    );
    assert!(report.fully_clean());
}

#[test]
fn srs_safe_001_event_sink_receives_the_exact_report_and_is_best_effort() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();
    let clock = StepClock::with_step(1);

    let report = activate_with(&brokerage, &fanout, &events, &clock);
    let recorded = events.recorded.borrow();
    assert_eq!(recorded.len(), 1, "activation recorded exactly once");
    assert_eq!(
        recorded[0].report, report,
        "the sink sees the exact returned report"
    );

    // Best-effort: a failing sink changes nothing about the report.
    let log2: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage2 = BrokerageSpy::clean(Rc::clone(&log2));
    let fanout2 = FanoutSpy::halting(Rc::clone(&log2), 30);
    let failing_events = EventSinkSpy::failing();
    let clock2 = StepClock::with_step(1);
    let report2 = activate_with(&brokerage2, &fanout2, &failing_events, &clock2);
    assert_eq!(
        report2, report,
        "a sink failure never alters the activation outcome"
    );
    assert_eq!(failing_events.recorded.borrow().len(), 1);
}

#[test]
fn srs_safe_001_timings_are_measured_on_the_injected_clock() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));
    let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
    let events = EventSinkSpy::recording();

    // Fast clock: every phase mark lands inside the budget.
    let fast = StepClock::with_step(1);
    let fast_report = activate_with(&brokerage, &fanout, &events, &fast);
    let timings = fast_report.timings;
    assert!(timings.halt_completed_ms <= timings.cancels_completed_ms);
    assert!(timings.cancels_completed_ms <= timings.liquidations_submitted_ms);
    assert!(timings.liquidations_submitted_ms <= timings.disconnect_completed_ms);
    assert!(fast_report.within_nfr_p3());
    assert!(
        timings.halt_completed_ms <= atp_types::KILL_SWITCH_HALT_OBSERVABILITY_BUDGET_MS,
        "halt-first keeps the HALTED mark inside the 1 s budget on a sane clock"
    );

    // Slow clock: 2 s per phase read pushes the liquidation mark past 5 s —
    // the verdict must reflect the MEASURED mark, not a constant.
    let log_slow: CallLog = Rc::new(RefCell::new(Vec::new()));
    let brokerage_slow = BrokerageSpy::clean(Rc::clone(&log_slow));
    let fanout_slow = FanoutSpy::halting(Rc::clone(&log_slow), 30);
    let events_slow = EventSinkSpy::recording();
    let slow = StepClock::with_step(2_000);
    let slow_report = activate_with(&brokerage_slow, &fanout_slow, &events_slow, &slow);
    assert!(
        slow_report.timings.liquidations_submitted_ms > KILL_SWITCH_ACTIVATION_BUDGET_MS,
        "slow-clock mark exceeds the budget: {:?}",
        slow_report.timings
    );
    assert!(!slow_report.within_nfr_p3());
}

#[test]
fn srs_safe_001_identical_fixture_and_clock_produce_identical_reports() {
    let run = || {
        let log: CallLog = Rc::new(RefCell::new(Vec::new()));
        let brokerage = BrokerageSpy::clean(Rc::clone(&log));
        let fanout = FanoutSpy::halting(Rc::clone(&log), 30);
        let events = EventSinkSpy::recording();
        let clock = StepClock::with_step(3);
        activate_with(&brokerage, &fanout, &events, &clock)
    };
    assert_eq!(run(), run(), "the gate is deterministic over its ports");
}
