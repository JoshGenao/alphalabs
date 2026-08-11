//! # SRS-RESV-004 demotion drill composition (SyRS SYS-49b / SYS-49c)
//!
//! Binds the REAL demotion sequence, the REAL `resolve_demotion` gate, the REAL durable
//! demotion-pending lockout, and the REAL SRS-NOTIF-001 `OperatorNotifier` into one runnable
//! scenario, over **fixture transports**. The operator CLI
//! (`resv004_hot_swap_demotion_cli`) drives it; `tests/domain/` shells that CLI.
//!
//! This is the structural mirror of [`crate::kill_switch_timeout`], for the same reason: the
//! orchestrator is the only layer allowed to see both the execution seam and the notification
//! dispatcher (SRS-ARCH-002 keeps the lower crates from seeing each other), so only here can a
//! demotion be driven end to end.
//!
//! ## What is real and what is a fixture
//!
//! **Real:** [`crate::hot_swap_demotion::execute_demotion_sequence`] (the SYS-49b 1→2→3 order),
//! [`crate::hot_swap_demotion::PollingFlatProbe`] (the flat-confirmation wait loop),
//! [`crate::StrategyOrchestrator::resolve_demotion`] (the timeout decision and the promotion
//! block), [`crate::demotion_pending_store`] (the durable lockout, written to a real file with a
//! real fsync), [`atp_notification::OperatorNotifier`] (the required-channel dispatcher), and
//! [`crate::hot_swap_demotion::complete_demotion_to_paper`] (the flat-only paper transition).
//!
//! **Fixture:** the IB socket (order cancels, liquidation submissions, the position view) and the
//! SMTP/SMS transports. Those are the deferred `atp-adapters` and SRS-NOTIF-001 legs.
//!
//! The distinction is not left to a reader of this file: [`DemotionDrillOutcome::transports`]
//! self-labels the tier as `FIXTURE`, and the CLI prints it, so the tier travels into any
//! evidence record made from a run. A drill whose tier could not be established is refused
//! rather than assumed.

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;

use atp_execution::kill_switch_probe::KillSwitchProbeClock;
use atp_execution::outbox::BrokerReconcileError;
use atp_execution::LiveExecutionState;
use atp_notification::{
    NotificationEvent, NotificationTrigger, OperatorNotifier, SharedChannelClient,
};
use atp_types::{
    AssetClass, ClientCorrelationId, HotSwapDemotionEvent, HotSwapDemotionRequest, OrderKey,
    OrderLedger, OrderSide, OrderState, OrderSubmission, OrderType, RestingOrderCancel,
    SideEffectOutcome, StrategyId,
};

use crate::demotion_pending_store::FileDemotionPendingLock;
use crate::hot_swap_demotion::{
    complete_demotion_to_paper, execute_demotion_sequence, DemotionBrokerageControl,
    DemotionCompleted, DemotionSequenceReport, LivePositionSource, PaperTransition,
    PollingFlatProbe, SignalHalt,
};
use crate::kill_switch_timeout::{FixtureEmailChannel, FixtureSmsChannel};
use crate::{
    HotSwapDemotionEventSink, HotSwapSideEffectError, OperatorAlertSink, StrategyOrchestrator,
    UnfilledOrderCanceller,
};
use atp_notification::NotificationChannel;
use atp_types::OperatorAlertEvent;

/// The transport tier a drill ran on. Carried into the outcome so evidence cannot silently
/// present a fixture run as a live one.
pub const TRANSPORT_TIER_FIXTURE: &str = "FIXTURE";

/// Simulated monotonic clock: `wait_ms` advances the reading instead of sleeping, so a full 60 s
/// SYS-49b wait completes instantly and the drill never blocks an operator (or a test).
#[derive(Debug, Default)]
pub struct SimulatedDemotionClock {
    now_ms: Cell<u64>,
}

impl SimulatedDemotionClock {
    pub fn now_ms(&self) -> u64 {
        self.now_ms.get()
    }
}

impl KillSwitchProbeClock for SimulatedDemotionClock {
    fn monotonic_ms(&self) -> u64 {
        self.now_ms.get()
    }

    fn wait_ms(&self, ms: u64) {
        self.now_ms.set(self.now_ms.get().saturating_add(ms));
    }
}

/// Fixture strategy-container control for SYS-49b (1).
#[derive(Debug, Default)]
pub struct FixtureSignalHalt {
    pub fail: bool,
    calls: RefCell<Vec<String>>,
}

impl FixtureSignalHalt {
    pub fn calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }
}

impl SignalHalt for FixtureSignalHalt {
    fn cease_new_signals(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError> {
        self.calls
            .borrow_mut()
            .push(strategy_id.as_str().to_string());
        if self.fail {
            return Err(HotSwapSideEffectError::new(
                "fixture: strategy container did not acknowledge the signal halt",
            ));
        }
        Ok(())
    }
}

/// Fixture IB order control for SYS-49b (2) and (3). Records every call so the drill can prove
/// the ordering, and injects failures per phase.
#[derive(Debug, Default)]
pub struct FixtureDemotionBrokerage {
    pub fail_cancels: bool,
    pub fail_liquidations: bool,
    calls: RefCell<Vec<String>>,
}

impl FixtureDemotionBrokerage {
    pub fn calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }
}

impl DemotionBrokerageControl for FixtureDemotionBrokerage {
    fn cancel_resting_order(
        &self,
        cancel: &RestingOrderCancel,
    ) -> Result<(), HotSwapSideEffectError> {
        self.calls
            .borrow_mut()
            .push(format!("cancel:{}", cancel.order_id));
        if self.fail_cancels {
            return Err(HotSwapSideEffectError::new(
                "fixture: IB cancel_order unreachable",
            ));
        }
        Ok(())
    }

    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), HotSwapSideEffectError> {
        self.calls
            .borrow_mut()
            .push(format!("liquidate:{}", submission.symbol));
        if self.fail_liquidations {
            return Err(HotSwapSideEffectError::new(
                "fixture: IB rejected the liquidation order",
            ));
        }
        Ok(())
    }
}

/// Fixture position view for SYS-49b (4): reports the open positions until `flat_after_ms`
/// elapses on the drill clock, then reports flat. `None` never goes flat, which is what produces
/// the SYS-49c timeout.
pub struct FixturePositionFeed<'a> {
    clock: &'a SimulatedDemotionClock,
    positions: BTreeMap<String, i64>,
    flat_after_ms: Option<u64>,
    fault: Option<BrokerReconcileError>,
    polls: Cell<u32>,
}

impl<'a> FixturePositionFeed<'a> {
    pub fn new(
        clock: &'a SimulatedDemotionClock,
        positions: BTreeMap<String, i64>,
        flat_after_ms: Option<u64>,
    ) -> Self {
        Self {
            clock,
            positions,
            flat_after_ms,
            fault: None,
            polls: Cell::new(0),
        }
    }

    pub fn with_fault(mut self, fault: BrokerReconcileError) -> Self {
        self.fault = Some(fault);
        self
    }

    pub fn polls(&self) -> u32 {
        self.polls.get()
    }
}

impl LivePositionSource for FixturePositionFeed<'_> {
    fn open_positions(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<BTreeMap<String, i64>, BrokerReconcileError> {
        self.polls.set(self.polls.get() + 1);
        if let Some(fault) = &self.fault {
            return Err(fault.clone());
        }
        match self.flat_after_ms {
            Some(at_ms) if self.clock.now_ms() >= at_ms => Ok(BTreeMap::new()),
            _ => Ok(self.positions.clone()),
        }
    }
}

/// Fixture unfilled-liquidation-order cancel (SYS-49c (b)). The concrete impl routes to the IB
/// adapter's `cancel_order`; this one records the attempt.
#[derive(Debug, Default)]
pub struct FixtureUnfilledOrderCanceller {
    pub fail: bool,
    calls: RefCell<Vec<String>>,
}

impl FixtureUnfilledOrderCanceller {
    pub fn calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }
}

impl UnfilledOrderCanceller for FixtureUnfilledOrderCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        self.calls
            .borrow_mut()
            .push(request.demoting_strategy_id.as_str().to_string());
        if self.fail {
            return Err(HotSwapSideEffectError::new(
                "fixture: IB cancel_order unreachable",
            ));
        }
        Ok(())
    }
}

/// Fixture container transition to paper simulation (SYS-49b closing clause).
#[derive(Debug, Default)]
pub struct FixturePaperTransition {
    pub fail: bool,
    moved: RefCell<Vec<String>>,
}

impl FixturePaperTransition {
    pub fn moved(&self) -> Vec<String> {
        self.moved.borrow().clone()
    }
}

impl PaperTransition for FixturePaperTransition {
    fn transition_to_paper(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError> {
        self.moved
            .borrow_mut()
            .push(strategy_id.as_str().to_string());
        if self.fail {
            return Err(HotSwapSideEffectError::new(
                "fixture: internal simulation engine unavailable",
            ));
        }
        Ok(())
    }
}

/// The concrete SRS-RESV-004 [`OperatorAlertSink`]: builds a `CriticalFailure` trigger — never
/// suppressed, which is right for a liquidation timeout that blocks a live changeover — and fans
/// it out through the REAL [`OperatorNotifier`] over exactly the required email + SMS pair.
///
/// SYS-49c (a) names three destinations: dashboard, email, SMS. The dashboard leg is not a
/// transport — it is the demotion-pending lockout this same branch persists, which the UI-5 pane
/// reads — so this sink owns the two that *are* transports and succeeds only when BOTH delivered.
/// Any other outcome surfaces as a `Failed` side effect on the demotion event rather than a
/// silent miss: a missed page on a blocked swap is itself a safety event.
pub struct DemotionNotifierAlertSink {
    notifier: OperatorNotifier,
    channels: Vec<SharedChannelClient>,
    events: RefCell<Vec<NotificationEvent>>,
}

impl DemotionNotifierAlertSink {
    pub fn new(notifier: OperatorNotifier, channels: Vec<SharedChannelClient>) -> Self {
        Self {
            notifier,
            channels,
            events: RefCell::new(Vec::new()),
        }
    }

    pub fn events(&self) -> Vec<NotificationEvent> {
        self.events.borrow().clone()
    }

    /// The page an operator acts on, describing what the gate ACTUALLY did.
    ///
    /// The cancel clause is derived from `event.liquidation_cancel`, never assumed: the
    /// probe-inconsistency branch blocks without cancelling, and a shared body claiming "the
    /// unfilled liquidation order is being canceled" would send the operator into recovery
    /// over an order that is still live and unmentioned. A FAILED cancel is likewise stated
    /// outright — that is the case where a live order most likely remains.
    fn page_summary(event: &OperatorAlertEvent) -> String {
        let cancel_clause = match &event.liquidation_cancel {
            SideEffectOutcome::Succeeded => {
                "The unfilled liquidation order has been canceled".to_string()
            }
            SideEffectOutcome::Failed { reason } => format!(
                "The unfilled liquidation order could NOT be canceled ({reason}) — a live IB \
                 order may still be resting and needs manual cancellation"
            ),
            SideEffectOutcome::NotAttempted => {
                "NO liquidation cancel was issued: the liquidation probe contradicted itself, \
                 so the gate refused to act destructively on a report it cannot trust. Any \
                 unfilled liquidation order is still live and needs manual inspection"
                    .to_string()
            }
        };
        format!(
            "SRS-RESV-004 + SyRS SYS-49c: the Hot-Swap demotion of live strategy {demoting} \
             (candidate {candidate}) did NOT reach flat within the {timeout} s timeout \
             ({elapsed} s elapsed). {cancel_clause}; the swap is held in demotion-pending, and \
             PROMOTION IS BLOCKED until the open positions are resolved manually",
            demoting = event.demoting_strategy_id.as_str(),
            candidate = event.candidate_strategy_id.as_str(),
            timeout = event.timeout_seconds,
            elapsed = event.elapsed_seconds,
        )
    }
}

impl OperatorAlertSink for DemotionNotifierAlertSink {
    fn dispatch(&self, event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        let detected_at_millis = event.observed_at_seconds.saturating_mul(1_000);
        let trigger =
            NotificationTrigger::critical_failure(Self::page_summary(&event), detected_at_millis);
        let notification = self
            .notifier
            .dispatch(&trigger, detected_at_millis, &self.channels)
            .map_err(|error| {
                HotSwapSideEffectError::new(format!("SRS-NOTIF-001 dispatch refused: {error}"))
            })?;
        let mut undelivered = Vec::new();
        for channel in [NotificationChannel::Email, NotificationChannel::Sms] {
            let delivered = notification
                .delivery_for(channel)
                .is_some_and(|delivery| delivery.outcome().is_delivered());
            if !delivered {
                undelivered.push(channel.as_str());
            }
        }
        self.events.borrow_mut().push(notification);
        if undelivered.is_empty() {
            Ok(())
        } else {
            Err(HotSwapSideEffectError::new(format!(
                "operator page not delivered on required channel(s): {}",
                undelivered.join(", ")
            )))
        }
    }
}

/// In-memory demotion-event sink. The durable SRS-LOG-001 write is the deferred consumer; the
/// drill retains the events so the CLI can print them as evidence.
#[derive(Debug, Default)]
pub struct CollectingDemotionEventSink {
    events: RefCell<Vec<HotSwapDemotionEvent>>,
}

impl CollectingDemotionEventSink {
    pub fn recorded(&self) -> Vec<HotSwapDemotionEvent> {
        self.events.borrow().clone()
    }
}

impl HotSwapDemotionEventSink for CollectingDemotionEventSink {
    fn record(&self, event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// The knobs a drill run exposes.
#[derive(Debug, Clone)]
pub struct DemotionScenario {
    pub demoting_strategy_id: String,
    pub candidate_strategy_id: String,
    pub timeout_seconds: u64,
    /// Open positions the demotion must liquidate, symbol → signed net quantity.
    pub positions: BTreeMap<String, i64>,
    /// Resting orders of the demoting strategy that must be cancelled, by symbol.
    pub resting_orders: Vec<String>,
    /// Seconds after which the fixture position view reports flat. `None` never goes flat and
    /// produces the SYS-49c timeout.
    pub flat_after_seconds: Option<u64>,
    /// Where the durable lockout lives. A real file, really fsynced.
    pub state_path: PathBuf,
    pub observed_at_seconds: u64,
    pub fail_signal_halt: bool,
    pub fail_cancels: bool,
    pub fail_liquidations: bool,
    pub fail_unfilled_cancel: bool,
    pub fail_email: bool,
    pub fail_sms: bool,
    pub fail_paper_transition: bool,
    pub position_fault: Option<BrokerReconcileError>,
    /// Who the live registry says is live. `None` designates the demoting strategy (the normal
    /// case); `Some(ids)` designates exactly those, so a drill can exercise the SyRS SYS-2a
    /// refusal — no live strategy, the wrong one, or more than one.
    pub designated_live: Option<Vec<String>>,
}

impl DemotionScenario {
    /// The SYS-49b reference case: one long and one short position, two resting orders, flat
    /// reached comfortably inside the default timeout.
    pub fn reference_flat(state_path: PathBuf) -> Self {
        Self {
            demoting_strategy_id: "live-momentum".to_string(),
            candidate_strategy_id: "paper-reversal".to_string(),
            timeout_seconds: atp_types::HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
            positions: BTreeMap::from([("AAPL".to_string(), 100), ("MSFT".to_string(), -50)]),
            resting_orders: vec!["AAPL".to_string(), "MSFT".to_string()],
            flat_after_seconds: Some(12),
            state_path,
            observed_at_seconds: 1_715_000_000,
            fail_signal_halt: false,
            fail_cancels: false,
            fail_liquidations: false,
            fail_unfilled_cancel: false,
            fail_email: false,
            fail_sms: false,
            fail_paper_transition: false,
            position_fault: None,
            designated_live: None,
        }
    }

    fn request(&self) -> Result<HotSwapDemotionRequest, String> {
        if self.demoting_strategy_id.trim().is_empty() {
            return Err("--demoting must name a strategy".to_string());
        }
        if self.candidate_strategy_id.trim().is_empty() {
            return Err("--candidate must name a strategy".to_string());
        }
        if self.demoting_strategy_id == self.candidate_strategy_id {
            return Err(
                "--demoting and --candidate name the same strategy; a swap must name two"
                    .to_string(),
            );
        }
        if self.timeout_seconds == 0 {
            return Err("--timeout-seconds must be positive".to_string());
        }
        Ok(HotSwapDemotionRequest {
            demoting_strategy_id: StrategyId::new(self.demoting_strategy_id.clone()),
            candidate_strategy_id: StrategyId::new(self.candidate_strategy_id.clone()),
            timeout_seconds: self.timeout_seconds,
        })
    }

    fn live_state(&self) -> Result<LiveExecutionState, String> {
        let mut ledger = OrderLedger::new();
        let demoting = StrategyId::new(self.demoting_strategy_id.clone());
        for (index, symbol) in self.resting_orders.iter().enumerate() {
            let correlation = ClientCorrelationId::new(format!("resv004-rest-{index:03}"))
                .map_err(|error| format!("building the fixture resting order: {error}"))?;
            let submission = OrderSubmission::new(
                demoting.clone(),
                symbol.clone(),
                1,
                AssetClass::Equity,
                OrderSide::Buy,
                OrderType::Market,
            );
            ledger
                .submit(correlation.clone(), &submission)
                .map_err(|error| format!("building the fixture resting order: {error}"))?;
            // Acked, so the order is genuinely resting at the broker rather than pre-submit.
            let key = OrderKey::new(demoting.clone(), correlation);
            ledger
                .transition(&key, OrderState::PendingSubmit)
                .map_err(|error| format!("building the fixture resting order: {error}"))?;
            ledger
                .transition(&key, OrderState::Acked)
                .map_err(|error| format!("building the fixture resting order: {error}"))?;
        }
        let mut state = LiveExecutionState::new(ledger);
        for (symbol, quantity) in &self.positions {
            state = state
                .with_position(symbol, *quantity)
                .map_err(|error| format!("building the fixture position: {error}"))?;
        }
        // Whom the live registry names. Defaults to the demoting strategy — the case a real
        // swap is built on — but a drill can designate someone else, or nobody, to exercise the
        // SyRS SYS-2a refusal.
        let designated: Vec<StrategyId> = match &self.designated_live {
            None => vec![demoting.clone()],
            Some(ids) => ids.iter().map(|id| StrategyId::new(id.clone())).collect(),
        };
        for id in &designated {
            state = state
                .with_live_strategy(id)
                .map_err(|error| format!("building the fixture live designation: {error}"))?;
        }
        Ok(state)
    }
}

/// Everything one drill run produced, in the shape the CLI prints.
#[derive(Debug, Clone)]
pub struct DemotionDrillOutcome {
    /// `flat` | `demotion-pending` | `blocked-pending` | `probe-inconsistent` | `refused`.
    pub disposition: String,
    /// Always [`TRANSPORT_TIER_FIXTURE`] here. Printed so a record made from this run cannot
    /// present itself as a live one.
    pub transports: &'static str,
    pub sequence: DemotionSequenceReport,
    pub demotion_events: Vec<HotSwapDemotionEvent>,
    pub notifications: Vec<NotificationEvent>,
    pub unfilled_cancels: Vec<String>,
    pub paper_moved: Vec<String>,
    pub completed: Option<DemotionCompleted>,
    pub error_type: Option<String>,
    pub error_message: Option<String>,
    pub promotion_blocked: bool,
    pub promotion_block_is_durable: bool,
    pub probe_degradation: Option<String>,
    pub brokerage_calls: Vec<String>,
}

/// Drive one SYS-49b/49c demotion end to end over fixture transports.
///
/// Runs the REAL sequence, the REAL probe, the REAL gate (against the REAL durable lockout at
/// `scenario.state_path`), the REAL notifier, and — on a clean flat demotion — the REAL paper
/// transition.
pub fn run_fixture_demotion(scenario: &DemotionScenario) -> Result<DemotionDrillOutcome, String> {
    let request = scenario.request()?;
    let state = scenario.live_state()?;

    let signals = FixtureSignalHalt {
        fail: scenario.fail_signal_halt,
        ..FixtureSignalHalt::default()
    };
    let brokerage = FixtureDemotionBrokerage {
        fail_cancels: scenario.fail_cancels,
        fail_liquidations: scenario.fail_liquidations,
        ..FixtureDemotionBrokerage::default()
    };
    let lock = FileDemotionPendingLock::new(scenario.state_path.clone());

    // SyRS SYS-49c (d) is consulted BEFORE the sequence, not merely before the gate's probe.
    // The gate refuses a blocked swap on its own, but by then the sequence would already have
    // cancelled this strategy's resting orders and submitted market liquidations — destructive
    // broker actions taken for a swap that was never permitted to start. A held lockout means
    // an operator still has unresolved positions; touching the account again before they do is
    // precisely what "block the promotion phase" is protecting.
    let blocked_before_start = crate::DemotionPendingLock::state(&lock).blocks_promotion();

    // SYS-49b (1)-(3), skipped entirely when the swap is already blocked.
    //
    // A `DemotionRefusal` aborts the WHOLE run, before the gate exists — which matters because
    // the gate's timeout branch fires the IB unfilled-order cancel on `request` alone. Letting
    // an unauthorised request reach it would put a destructive broker action behind an identity
    // nothing proved (SyRS SYS-2a). The refusal is the composition's single authorisation point,
    // and it sits ahead of every port in the run.
    let sequence = if blocked_before_start {
        DemotionSequenceReport {
            demoting_strategy_id: request.demoting_strategy_id.clone(),
            signal_halt: SideEffectOutcome::NotAttempted,
            resting_order_cancels: Vec::new(),
            liquidations: Vec::new(),
        }
    } else {
        execute_demotion_sequence(&request, &state, &signals, &brokerage)
            .map_err(|refusal| refusal.to_string())?
    };

    // SYS-49b (4): the wait, on the REAL polling probe over a simulated clock.
    let clock = SimulatedDemotionClock::default();
    let mut feed = FixturePositionFeed::new(
        &clock,
        scenario.positions.clone(),
        scenario
            .flat_after_seconds
            .map(|seconds| seconds.saturating_mul(1_000)),
    );
    if let Some(fault) = &scenario.position_fault {
        feed = feed.with_fault(fault.clone());
    }
    let probe = PollingFlatProbe::new(&clock, &feed);

    let canceller = FixtureUnfilledOrderCanceller {
        fail: scenario.fail_unfilled_cancel,
        ..FixtureUnfilledOrderCanceller::default()
    };
    let email = Arc::new(FixtureEmailChannel::with_failure(scenario.fail_email));
    let sms = Arc::new(FixtureSmsChannel::with_failure(scenario.fail_sms));
    let alerts = DemotionNotifierAlertSink::new(
        OperatorNotifier::new(),
        vec![
            Arc::clone(&email) as SharedChannelClient,
            Arc::clone(&sms) as SharedChannelClient,
        ],
    );
    let events = CollectingDemotionEventSink::default();
    let paper = FixturePaperTransition {
        fail: scenario.fail_paper_transition,
        ..FixturePaperTransition::default()
    };

    let orchestrator = StrategyOrchestrator;
    let decision = orchestrator.resolve_demotion(
        request,
        &probe,
        &canceller,
        &alerts,
        &events,
        &lock,
        scenario.observed_at_seconds,
    );

    let mut outcome = DemotionDrillOutcome {
        disposition: String::new(),
        transports: TRANSPORT_TIER_FIXTURE,
        sequence: sequence.clone(),
        demotion_events: events.recorded(),
        notifications: alerts.events(),
        unfilled_cancels: canceller.calls(),
        paper_moved: Vec::new(),
        completed: None,
        error_type: None,
        error_message: None,
        promotion_blocked: true,
        promotion_block_is_durable: false,
        probe_degradation: probe.degradation(),
        brokerage_calls: brokerage.calls(),
    };

    match decision {
        Ok(resolved) => match complete_demotion_to_paper(&resolved, &sequence, &paper) {
            Ok(completed) => {
                outcome.disposition = "flat".to_string();
                outcome.promotion_blocked = false;
                outcome.completed = Some(completed);
                outcome.paper_moved = paper.moved();
            }
            Err(error) => {
                // Flat, but the demotion could not be completed — promotion must not follow.
                outcome.disposition = "refused".to_string();
                outcome.error_type = Some("DemotionNotCompleted".to_string());
                outcome.error_message = Some(error.to_string());
                outcome.paper_moved = paper.moved();
            }
        },
        Err(error) => {
            outcome.disposition = match error.error_type.as_str() {
                "HotSwapDemotionPending" => "blocked-pending".to_string(),
                "HotSwapDemotionProbeInconsistent" => "probe-inconsistent".to_string(),
                _ => "demotion-pending".to_string(),
            };
            outcome.promotion_block_is_durable = error.promotion_block_is_durable;
            outcome.error_type = Some(error.error_type.clone());
            outcome.error_message = Some(error.message.clone());
        }
    }

    // Cross-validate the disposition against the evidence, in BOTH directions, before the
    // outcome is allowed to stand as a record. A drill that says "flat" while the lockout it
    // consulted is engaged, or one that says "demotion-pending" having cancelled nothing, is
    // describing a run that did not happen — refuse rather than publish it.
    validate_disposition(&outcome, scenario)?;
    Ok(outcome)
}

/// Refuse an outcome whose disposition and evidence disagree.
fn validate_disposition(
    outcome: &DemotionDrillOutcome,
    scenario: &DemotionScenario,
) -> Result<(), String> {
    match outcome.disposition.as_str() {
        "flat" => {
            if outcome.promotion_blocked {
                return Err("drill reported 'flat' with promotion still blocked".to_string());
            }
            if outcome.completed.is_none() {
                return Err("drill reported 'flat' with no completed demotion".to_string());
            }
            if outcome.paper_moved.is_empty() {
                return Err(
                    "drill reported 'flat' but the container never moved to paper".to_string(),
                );
            }
            if !outcome.unfilled_cancels.is_empty() {
                return Err(
                    "drill reported 'flat' but an unfilled-order cancel was issued".to_string(),
                );
            }
            // The other direction: a completed demotion must have DONE the work the scenario
            // set up. A "flat" that liquidated none of the declared positions, or cancelled
            // none of the declared resting orders, describes a demotion that had nothing to
            // demote — the shape a silently no-op sequence would produce.
            let liquidatable = scenario
                .positions
                .values()
                .filter(|quantity| **quantity != 0)
                .count();
            if outcome.sequence.liquidations.len() != liquidatable {
                return Err(format!(
                    "drill reported 'flat' having submitted {} liquidation(s) for {} open \
                     position(s)",
                    outcome.sequence.liquidations.len(),
                    liquidatable
                ));
            }
            if outcome.sequence.resting_order_cancels.len() != scenario.resting_orders.len() {
                return Err(format!(
                    "drill reported 'flat' having cancelled {} of {} resting order(s)",
                    outcome.sequence.resting_order_cancels.len(),
                    scenario.resting_orders.len()
                ));
            }
        }
        "demotion-pending" => {
            if !outcome.promotion_blocked {
                return Err("drill reported a timeout with promotion allowed".to_string());
            }
            if outcome.unfilled_cancels.is_empty() {
                return Err(
                    "drill reported a liquidation timeout but cancelled nothing (SYS-49c (b))"
                        .to_string(),
                );
            }
            if outcome.notifications.is_empty() {
                return Err(
                    "drill reported a liquidation timeout but paged nobody (SYS-49c (a))"
                        .to_string(),
                );
            }
            if !outcome.paper_moved.is_empty() {
                return Err(
                    "drill reported a timeout but still moved the container to paper".to_string(),
                );
            }
        }
        "probe-inconsistent" => {
            if !outcome.unfilled_cancels.is_empty() {
                return Err(
                    "a probe-inconsistency must block WITHOUT the premature cancel".to_string(),
                );
            }
        }
        "blocked-pending" => {
            // A swap refused before it started must have left the account alone: no cancel, no
            // liquidation, no unfilled-order cancel, and no signal halt. This is the check that
            // caught the composition running the destructive sequence ahead of the lockout.
            if !outcome.brokerage_calls.is_empty() {
                return Err(format!(
                    "a swap blocked before it started touched the broker: {:?}",
                    outcome.brokerage_calls
                ));
            }
            if !outcome.unfilled_cancels.is_empty() {
                return Err("a swap blocked before it started must not cancel anything".to_string());
            }
            if outcome.sequence.signal_halt != SideEffectOutcome::NotAttempted {
                return Err(
                    "a swap blocked before it started must not have run the demotion sequence"
                        .to_string(),
                );
            }
            if !outcome.paper_moved.is_empty() {
                return Err(
                    "a swap blocked before it started must not move a container to paper"
                        .to_string(),
                );
            }
        }
        "refused" => {}
        other => return Err(format!("drill produced an unknown disposition {other:?}")),
    }
    // Every non-flat disposition must leave a durable block, or say plainly that it did not.
    if outcome.disposition != "flat"
        && outcome.disposition != "refused"
        && !outcome.promotion_block_is_durable
        && outcome
            .demotion_events
            .iter()
            .all(|event| !event.demotion_pending.is_failed())
    {
        return Err(
            "drill reports a non-durable promotion block that no event records as failed"
                .to_string(),
        );
    }
    Ok(())
}

/// The `SideEffectOutcome` wire spelling the CLI prints.
pub fn outcome_label(outcome: &SideEffectOutcome) -> &'static str {
    match outcome {
        SideEffectOutcome::NotAttempted => "NOT_ATTEMPTED",
        SideEffectOutcome::Succeeded => "SUCCEEDED",
        SideEffectOutcome::Failed { .. } => "FAILED",
    }
}
