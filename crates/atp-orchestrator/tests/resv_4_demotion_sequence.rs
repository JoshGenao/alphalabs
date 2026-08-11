//! SRS-RESV-004 / SyRS SYS-49b — the Hot-Swap demotion SEQUENCE: cease new signals,
//! cancel resting IB orders, submit market liquidations, wait for flat or the
//! configured timeout, and transition to paper ONLY after the positions are flat.
//!
//! The companion to `err_7_hot_swap_demotion_timeout.rs`, which covers the timeout
//! DECISION. This suite covers what runs before and after it.
//!
//! L7 domain (safety) suite. Post-conditions:
//!   * Global phase ordering on a shared call log: cease-signals < every cancel <
//!     every liquidation. Ordering is load-bearing — a resting order that fills after
//!     the liquidation re-opens the position, and a strategy still emitting signals
//!     replaces resting orders as fast as they are cancelled.
//!   * Exactly the resting (non-terminal) orders of the DEMOTING strategy are
//!     cancelled, once each; terminal and other-strategy orders are untouched.
//!   * Every open position gets exactly one validated market liquidation: long →
//!     SELL |net|, short → BUY |net|.
//!   * Continue-to-safety: a failed phase does not suppress a later one.
//!   * A degraded sequence POISONS a flat result — `complete_demotion_to_paper`
//!     refuses, so promotion cannot follow a demotion that cannot vouch for flat.
//!   * The paper transition is unreachable without the gate's acceptance token, and
//!     refuses evidence that describes a different strategy.
//!   * The flat probe never reports flat from an unreadable position view, enforces
//!     its deadline BEFORE polling, and produces outcomes the gate's
//!     consistency check accepts.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::rc::Rc;

use atp_execution::kill_switch_probe::KillSwitchProbeClock;
use atp_execution::outbox::BrokerReconcileError;
use atp_execution::LiveExecutionState;
use atp_orchestrator::hot_swap_demotion::{
    complete_demotion_to_paper, execute_demotion_sequence, DemotionBrokerageControl,
    DemotionCompletionError, LivePositionSource, PaperTransition, PollingFlatProbe, SignalHalt,
};
use atp_orchestrator::{HotSwapDemotionResolved, HotSwapLiquidationProbe, HotSwapSideEffectError};
use atp_types::{
    AssetClass, ClientCorrelationId, HotSwapDemotionOutcome, HotSwapDemotionRequest, OrderKey,
    OrderLedger, OrderSide, OrderState, OrderSubmission, OrderType, RestingOrderCancel,
    SideEffectOutcome, StrategyId, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
};

const DEMOTING: &str = "live-momentum";
const CANDIDATE: &str = "paper-reversal";
const OTHER: &str = "paper-07";

type CallLog = Rc<RefCell<Vec<String>>>;

fn correlation(value: &str) -> ClientCorrelationId {
    ClientCorrelationId::new(value).expect("valid correlation id")
}

fn market_order(strategy: &str, symbol: &str, quantity: i64, side: OrderSide) -> OrderSubmission {
    OrderSubmission::new(
        StrategyId::new(strategy),
        symbol.to_string(),
        quantity,
        AssetClass::Equity,
        side,
        OrderType::Market,
    )
}

fn request(timeout_seconds: u64) -> HotSwapDemotionRequest {
    HotSwapDemotionRequest {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        timeout_seconds,
    }
}

/// Live state with two resting orders for the demoting strategy (one bound to a
/// broker id, one not), one terminal order, one order belonging to a DIFFERENT
/// strategy, and a long + a short position.
fn live_state() -> LiveExecutionState {
    let mut ledger = OrderLedger::new();
    let demoting = StrategyId::new(DEMOTING);

    ledger
        .submit(
            correlation("c-rest-new"),
            &market_order(DEMOTING, "AAPL", 10, OrderSide::Buy),
        )
        .expect("submit resting-new");

    ledger
        .submit(
            correlation("c-rest-ack"),
            &market_order(DEMOTING, "MSFT", 5, OrderSide::Sell),
        )
        .expect("submit resting-acked");
    let ack_key = OrderKey::new(demoting.clone(), correlation("c-rest-ack"));
    ledger
        .transition(&ack_key, OrderState::PendingSubmit)
        .expect("-> PENDING_SUBMIT");
    ledger
        .transition(&ack_key, OrderState::Acked)
        .expect("-> ACKED");

    ledger
        .submit(
            correlation("c-filled"),
            &market_order(DEMOTING, "NVDA", 7, OrderSide::Buy),
        )
        .expect("submit filled");
    let filled_key = OrderKey::new(demoting.clone(), correlation("c-filled"));
    ledger
        .transition(&filled_key, OrderState::PendingSubmit)
        .expect("-> PENDING_SUBMIT");
    ledger
        .transition(&filled_key, OrderState::Acked)
        .expect("-> ACKED");
    ledger
        .transition(&filled_key, OrderState::Filled)
        .expect("-> FILLED");

    // A resting order belonging to another strategy: a demotion must not touch it.
    ledger
        .submit(
            correlation("c-other"),
            &market_order(OTHER, "TSLA", 3, OrderSide::Buy),
        )
        .expect("submit other-strategy order");

    LiveExecutionState::new(ledger)
        .with_broker_id(ack_key, "B-ACK")
        .expect("bind broker id")
        .with_position("AAPL", 100)
        .expect("long position")
        .with_position("MSFT", -50)
        .expect("short position")
        .with_live_strategy(&demoting)
        .expect("live designation record")
}

// --------------------------------------------------------------------------- //
// Spies
// --------------------------------------------------------------------------- //

struct SignalHaltSpy {
    log: CallLog,
    fail: bool,
    calls: Cell<u32>,
}

impl SignalHaltSpy {
    fn clean(log: CallLog) -> Self {
        Self {
            log,
            fail: false,
            calls: Cell::new(0),
        }
    }

    fn failing(log: CallLog) -> Self {
        Self {
            log,
            fail: true,
            calls: Cell::new(0),
        }
    }
}

impl SignalHalt for SignalHaltSpy {
    fn cease_new_signals(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError> {
        self.calls.set(self.calls.get() + 1);
        self.log
            .borrow_mut()
            .push(format!("cease:{}", strategy_id.as_str()));
        if self.fail {
            return Err(HotSwapSideEffectError::new(
                "strategy container unreachable",
            ));
        }
        Ok(())
    }
}

struct BrokerageSpy {
    log: CallLog,
    fail_cancel_for: Option<String>,
    fail_liquidation_for: Option<String>,
}

impl BrokerageSpy {
    fn clean(log: CallLog) -> Self {
        Self {
            log,
            fail_cancel_for: None,
            fail_liquidation_for: None,
        }
    }

    fn failing_cancel(log: CallLog, order_id_fragment: &str) -> Self {
        Self {
            log,
            fail_cancel_for: Some(order_id_fragment.to_string()),
            fail_liquidation_for: None,
        }
    }

    fn failing_liquidation(log: CallLog, symbol: &str) -> Self {
        Self {
            log,
            fail_cancel_for: None,
            fail_liquidation_for: Some(symbol.to_string()),
        }
    }
}

impl DemotionBrokerageControl for BrokerageSpy {
    fn cancel_resting_order(
        &self,
        cancel: &RestingOrderCancel,
    ) -> Result<(), HotSwapSideEffectError> {
        self.log
            .borrow_mut()
            .push(format!("cancel:{}", cancel.symbol));
        if let Some(fragment) = &self.fail_cancel_for {
            if cancel.order_id.contains(fragment) {
                return Err(HotSwapSideEffectError::new("IB cancel_order unreachable"));
            }
        }
        Ok(())
    }

    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), HotSwapSideEffectError> {
        self.log
            .borrow_mut()
            .push(format!("liquidate:{}", submission.symbol));
        if self.fail_liquidation_for.as_deref() == Some(submission.symbol.as_str()) {
            return Err(HotSwapSideEffectError::new("IB order rejected"));
        }
        Ok(())
    }
}

#[derive(Default)]
struct PaperTransitionSpy {
    moved: RefCell<Vec<String>>,
    fail: bool,
}

impl PaperTransitionSpy {
    fn failing() -> Self {
        Self {
            moved: RefCell::new(Vec::new()),
            fail: true,
        }
    }
}

impl PaperTransition for PaperTransitionSpy {
    fn transition_to_paper(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError> {
        self.moved
            .borrow_mut()
            .push(strategy_id.as_str().to_string());
        if self.fail {
            return Err(HotSwapSideEffectError::new("simulation engine unavailable"));
        }
        Ok(())
    }
}

/// Panics if consulted — proves a refused completion never touches the runtime.
struct ForbiddenPaperTransition;

impl PaperTransition for ForbiddenPaperTransition {
    fn transition_to_paper(&self, _strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError> {
        panic!("SRS-RESV-004: a demotion that cannot vouch for flat must not reach the runtime");
    }
}

/// Simulated clock: `wait_ms` advances the reading instead of sleeping, so a full 60 s
/// SYS-49b drill runs instantly and no test blocks.
struct SimulatedClock {
    now_ms: Cell<u64>,
}

impl SimulatedClock {
    fn start() -> Self {
        Self {
            now_ms: Cell::new(0),
        }
    }
}

impl KillSwitchProbeClock for SimulatedClock {
    fn monotonic_ms(&self) -> u64 {
        self.now_ms.get()
    }

    fn wait_ms(&self, ms: u64) {
        self.now_ms.set(self.now_ms.get() + ms);
    }
}

/// Position source that reports the given sequence of answers, one per poll, repeating
/// the last one forever.
struct ScriptedPositions {
    answers: RefCell<Vec<Result<BTreeMap<String, i64>, BrokerReconcileError>>>,
    polls: Cell<u32>,
}

impl ScriptedPositions {
    fn new(answers: Vec<Result<BTreeMap<String, i64>, BrokerReconcileError>>) -> Self {
        Self {
            answers: RefCell::new(answers),
            polls: Cell::new(0),
        }
    }

    fn never_flat() -> Self {
        Self::new(vec![Ok(BTreeMap::from([("AAPL".to_string(), 100_i64)]))])
    }

    fn always_unreadable() -> Self {
        Self::new(vec![Err(BrokerReconcileError::connectivity_blocked(
            "IB gateway in a scheduled restart window",
        ))])
    }
}

impl LivePositionSource for ScriptedPositions {
    fn open_positions(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<BTreeMap<String, i64>, BrokerReconcileError> {
        self.polls.set(self.polls.get() + 1);
        let mut answers = self.answers.borrow_mut();
        if answers.len() > 1 {
            answers.remove(0)
        } else {
            answers[0].clone()
        }
    }
}

fn flat() -> Result<BTreeMap<String, i64>, BrokerReconcileError> {
    Ok(BTreeMap::new())
}

// --------------------------------------------------------------------------- //
// SYS-49b steps 1-3
// --------------------------------------------------------------------------- //

#[test]
fn resv_4_sequence_ceases_signals_then_cancels_then_liquidates() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let signals = SignalHaltSpy::clean(Rc::clone(&log));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));

    let report = execute_demotion_sequence(&request(60), &live_state(), &signals, &brokerage);

    let calls = log.borrow().clone();
    let cease_at = calls.iter().position(|c| c.starts_with("cease:")).unwrap();
    let first_cancel = calls.iter().position(|c| c.starts_with("cancel:")).unwrap();
    let last_cancel = calls
        .iter()
        .rposition(|c| c.starts_with("cancel:"))
        .unwrap();
    let first_liquidation = calls
        .iter()
        .position(|c| c.starts_with("liquidate:"))
        .unwrap();

    // SYS-49b (1) < (2) < (3). Each boundary is a real safety property, not style:
    // signals must stop before the cancel sweep can converge, and every resting order
    // must be cancelled before a liquidation, or a late fill re-opens the position.
    assert!(cease_at < first_cancel, "signals must cease before cancels");
    assert!(
        last_cancel < first_liquidation,
        "every resting order must be cancelled before the first liquidation"
    );
    assert_eq!(signals.calls.get(), 1);

    // Exactly the DEMOTING strategy's resting orders, once each. The terminal NVDA
    // order and the other strategy's TSLA order are untouched.
    let cancelled: Vec<&str> = report
        .resting_order_cancels
        .iter()
        .map(|cancel| cancel.order.symbol.as_str())
        .collect();
    assert_eq!(cancelled.len(), 2);
    assert!(cancelled.contains(&"AAPL"));
    assert!(cancelled.contains(&"MSFT"));
    assert!(!calls.iter().any(|c| c == "cancel:TSLA"));
    assert!(!calls.iter().any(|c| c == "cancel:NVDA"));

    // The report is audit evidence, so an identical state must produce an identical
    // record. The ledger iterates in HASH order, so the cancels are sorted by domain
    // order id (`c-rest-ack` before `c-rest-new`, i.e. MSFT before AAPL) — sorting by
    // the id rather than the symbol is what makes two orders on one symbol stable too.
    let ids: Vec<&str> = report
        .resting_order_cancels
        .iter()
        .map(|cancel| cancel.order.order_id.as_str())
        .collect();
    let mut sorted = ids.clone();
    sorted.sort_unstable();
    assert_eq!(ids, sorted, "cancels must be emitted in order-id order");
    let repeat = execute_demotion_sequence(
        &request(60),
        &live_state(),
        &SignalHaltSpy::clean(Rc::new(RefCell::new(Vec::new()))),
        &BrokerageSpy::clean(Rc::new(RefCell::new(Vec::new()))),
    );
    assert_eq!(
        repeat.resting_order_cancels, report.resting_order_cancels,
        "an identical state must produce an identical audit record"
    );

    // The broker binding, or its honest absence, is carried on each cancel.
    let bindings: Vec<Option<&str>> = report
        .resting_order_cancels
        .iter()
        .map(|cancel| cancel.order.broker_order_id.as_deref())
        .collect();
    assert!(bindings.contains(&Some("B-ACK")));
    assert!(bindings.contains(&None));

    // One validated liquidation per open position: long AAPL 100 → SELL 100,
    // short MSFT -50 → BUY 50.
    assert_eq!(report.liquidations.len(), 2);
    let aapl = report
        .liquidations
        .iter()
        .find(|l| l.symbol == "AAPL")
        .unwrap();
    assert_eq!(aapl.side, OrderSide::Sell);
    assert_eq!(aapl.quantity, 100);
    let msft = report
        .liquidations
        .iter()
        .find(|l| l.symbol == "MSFT")
        .unwrap();
    assert_eq!(msft.side, OrderSide::Buy);
    assert_eq!(msft.quantity, 50);

    assert!(report.fully_clean());
    assert!(report.safe_to_accept_flat());
    assert_eq!(report.degradation_reason(), None);
}

#[test]
fn resv_4_a_failed_signal_halt_still_runs_the_rest_but_poisons_a_flat_result() {
    // Continue-to-safety: cancelling and liquidating are still the right actions.
    // But a strategy that was never silenced can open a new position the instant
    // after the probe observes flat, so "flat" describes a moment rather than a
    // state — and the demotion must not be completed on it.
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let signals = SignalHaltSpy::failing(Rc::clone(&log));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));

    let report = execute_demotion_sequence(&request(60), &live_state(), &signals, &brokerage);

    // Everything downstream still ran.
    assert!(report.signal_halt.is_failed());
    assert_eq!(report.resting_order_cancels.len(), 2);
    assert_eq!(report.liquidations.len(), 2);
    assert!(log.borrow().iter().any(|c| c.starts_with("cancel:")));
    assert!(log.borrow().iter().any(|c| c.starts_with("liquidate:")));

    // But the flat result is not believable.
    assert!(!report.safe_to_accept_flat());
    assert!(!report.fully_clean());
    let reason = report.degradation_reason().expect("degradation reason");
    assert!(reason.contains("cease-new-signals"));

    let resolved = HotSwapDemotionResolved {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        promotion_allowed: true,
        elapsed_seconds: 12,
    };
    let error = complete_demotion_to_paper(&resolved, &report, &ForbiddenPaperTransition)
        .expect_err("a degraded sequence must not complete the demotion");
    assert!(matches!(
        error,
        DemotionCompletionError::SequenceDegraded { .. }
    ));
}

#[test]
fn resv_4_a_failed_cancel_also_poisons_a_flat_result_but_a_failed_liquidation_does_not() {
    // An uncancelled resting order can still fill, so it carries the same hazard as an
    // unsilenced strategy. A failed liquidation SUBMISSION does not: it means a
    // position was not closed, so the probe will not observe flat in the first place —
    // and if the position is flat anyway, it is genuinely flat.
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let signals = SignalHaltSpy::clean(Rc::clone(&log));
    let failing_cancel = BrokerageSpy::failing_cancel(Rc::clone(&log), "c-rest-ack");
    let with_bad_cancel =
        execute_demotion_sequence(&request(60), &live_state(), &signals, &failing_cancel);
    assert!(!with_bad_cancel.safe_to_accept_flat());
    assert!(with_bad_cancel
        .degradation_reason()
        .expect("reason")
        .contains("cancel failed"));

    let log2: CallLog = Rc::new(RefCell::new(Vec::new()));
    let signals2 = SignalHaltSpy::clean(Rc::clone(&log2));
    let failing_liquidation = BrokerageSpy::failing_liquidation(Rc::clone(&log2), "AAPL");
    let with_bad_liquidation =
        execute_demotion_sequence(&request(60), &live_state(), &signals2, &failing_liquidation);
    assert!(with_bad_liquidation
        .liquidations
        .iter()
        .any(|l| l.outcome.is_failed()));
    assert!(with_bad_liquidation.safe_to_accept_flat());
    // ...but it is still not a clean demotion, and the operator surface says so.
    assert!(!with_bad_liquidation.fully_clean());
    // Continue-to-safety: the OTHER position was still liquidated.
    assert!(log2.borrow().iter().any(|c| c == "liquidate:MSFT"));
}

#[test]
fn resv_4_an_empty_live_state_still_ceases_signals_and_is_clean() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let signals = SignalHaltSpy::clean(Rc::clone(&log));
    let brokerage = BrokerageSpy::clean(Rc::clone(&log));

    let report = execute_demotion_sequence(
        &request(60),
        &LiveExecutionState::new(OrderLedger::new()),
        &signals,
        &brokerage,
    );

    assert!(report.resting_order_cancels.is_empty());
    assert!(report.liquidations.is_empty());
    assert_eq!(report.signal_halt, SideEffectOutcome::Succeeded);
    assert_eq!(log.borrow().as_slice(), [format!("cease:{DEMOTING}")]);
    assert!(report.fully_clean());
}

// --------------------------------------------------------------------------- //
// SYS-49b closing clause: the paper transition
// --------------------------------------------------------------------------- //

#[test]
fn resv_4_a_clean_flat_demotion_transitions_the_container_to_paper() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let report = execute_demotion_sequence(
        &request(60),
        &live_state(),
        &SignalHaltSpy::clean(Rc::clone(&log)),
        &BrokerageSpy::clean(Rc::clone(&log)),
    );
    let resolved = HotSwapDemotionResolved {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        promotion_allowed: true,
        elapsed_seconds: 12,
    };
    let paper = PaperTransitionSpy::default();

    let completed =
        complete_demotion_to_paper(&resolved, &report, &paper).expect("a clean flat demotion");

    assert_eq!(completed.demoting_strategy_id.as_str(), DEMOTING);
    assert_eq!(completed.candidate_strategy_id.as_str(), CANDIDATE);
    assert_eq!(completed.elapsed_seconds, 12);
    assert_eq!(paper.moved.borrow().as_slice(), [DEMOTING.to_string()]);
}

#[test]
fn resv_4_completion_refuses_evidence_that_describes_a_different_strategy() {
    // Time-ordering is not correlation: a clean sequence for strategy A paired with an
    // acceptance for strategy B would vouch for a demotion using evidence that is not
    // about it. Both directions of the identity must agree before the runtime is
    // touched.
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let report = execute_demotion_sequence(
        &request(60),
        &live_state(),
        &SignalHaltSpy::clean(Rc::clone(&log)),
        &BrokerageSpy::clean(Rc::clone(&log)),
    );
    let foreign = HotSwapDemotionResolved {
        demoting_strategy_id: StrategyId::new("live-someone-else"),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        promotion_allowed: true,
        elapsed_seconds: 12,
    };

    let error = complete_demotion_to_paper(&foreign, &report, &ForbiddenPaperTransition)
        .expect_err("mismatched identity must refuse");
    assert!(matches!(
        error,
        DemotionCompletionError::IdentityMismatch { .. }
    ));
}

#[test]
fn resv_4_a_failed_paper_transition_is_surfaced_not_swallowed() {
    let log: CallLog = Rc::new(RefCell::new(Vec::new()));
    let report = execute_demotion_sequence(
        &request(60),
        &live_state(),
        &SignalHaltSpy::clean(Rc::clone(&log)),
        &BrokerageSpy::clean(Rc::clone(&log)),
    );
    let resolved = HotSwapDemotionResolved {
        demoting_strategy_id: StrategyId::new(DEMOTING),
        candidate_strategy_id: StrategyId::new(CANDIDATE),
        promotion_allowed: true,
        elapsed_seconds: 12,
    };

    let error = complete_demotion_to_paper(&resolved, &report, &PaperTransitionSpy::failing())
        .expect_err("a failed transition must not read as a completed demotion");
    assert!(matches!(error, DemotionCompletionError::Transition { .. }));
    assert!(error.to_string().contains("SYS-49b"));
}

// --------------------------------------------------------------------------- //
// SYS-49b step 4: the flat-confirmation probe
// --------------------------------------------------------------------------- //

#[test]
fn resv_4_probe_reports_flat_once_the_positions_close() {
    let clock = SimulatedClock::start();
    // Two polls hold a position, the third is flat.
    let positions = ScriptedPositions::new(vec![
        Ok(BTreeMap::from([("AAPL".to_string(), 100)])),
        Ok(BTreeMap::from([("AAPL".to_string(), 40)])),
        flat(),
    ]);
    let probe = PollingFlatProbe::with_poll_interval(&clock, &positions, 500);

    let outcome = probe.await_flat_or_timeout(&request(HOT_SWAP_DEMOTION_TIMEOUT_SECONDS));

    match outcome {
        HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds } => {
            // Inside the window, which is what the gate's consistency check requires.
            assert!(elapsed_seconds <= HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
        }
        other => panic!("expected FlatBeforeTimeout, got {other:?}"),
    }
    assert_eq!(positions.polls.get(), 3);
    assert_eq!(probe.degradation(), None);
    // A symbol carried at zero is flat, not a position.
    assert_eq!(probe.degraded_polls(), 0);
}

#[test]
fn resv_4_probe_times_out_when_the_positions_never_close() {
    let clock = SimulatedClock::start();
    let positions = ScriptedPositions::never_flat();
    let probe = PollingFlatProbe::with_poll_interval(&clock, &positions, 500);

    let outcome = probe.await_flat_or_timeout(&request(HOT_SWAP_DEMOTION_TIMEOUT_SECONDS));

    match outcome {
        HotSwapDemotionOutcome::TimedOutDemotionPending {
            elapsed_seconds,
            timeout_seconds,
        } => {
            // The gate rejects a timeout reported before its own deadline, so the
            // probe must never produce one. This is the invariant that keeps the
            // concrete probe out of the probe-inconsistency branch.
            assert!(elapsed_seconds >= HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
            assert_eq!(timeout_seconds, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
        }
        other => panic!("expected TimedOutDemotionPending, got {other:?}"),
    }
}

#[test]
fn resv_4_an_unreadable_position_view_is_never_flat_and_is_reported_as_degraded() {
    // The single way a demotion could wrongly report flat is to read an unreadable
    // position view as an empty one. The probe's outcome type has no error arm, so
    // the fail-closed answer is "not flat" — and the reason must survive, or the
    // operator record would say "the liquidation did not fill" when the truth is
    // "we could not see the positions".
    let clock = SimulatedClock::start();
    let positions = ScriptedPositions::always_unreadable();
    let probe = PollingFlatProbe::with_poll_interval(&clock, &positions, 1_000);

    let outcome = probe.await_flat_or_timeout(&request(10));

    assert!(matches!(
        outcome,
        HotSwapDemotionOutcome::TimedOutDemotionPending { .. }
    ));
    let degradation = probe.degradation().expect("the reason must survive");
    assert!(degradation.contains("CONNECTIVITY_BLOCKED"));
    assert!(degradation.contains("scheduled restart"));
    assert!(probe.degraded_polls() >= 1);
}

#[test]
fn resv_4_probe_enforces_the_deadline_before_polling() {
    // A flat state first observed AT or after the deadline must not be accepted: the
    // question SYS-49b asks is whether the positions reached flat WITHIN the window.
    // With a zero-second budget the probe must not poll at all.
    let clock = SimulatedClock::start();
    let positions = ScriptedPositions::new(vec![flat()]);
    let probe = PollingFlatProbe::with_poll_interval(&clock, &positions, 500);

    let outcome = probe.await_flat_or_timeout(&request(0));

    assert!(matches!(
        outcome,
        HotSwapDemotionOutcome::TimedOutDemotionPending {
            elapsed_seconds: 0,
            timeout_seconds: 0,
        }
    ));
    assert_eq!(
        positions.polls.get(),
        0,
        "an elapsed deadline must be decided before any poll"
    );
}
