//! # SRS-RESV-004 Hot-Swap demotion execution (SyRS SYS-49b)
//!
//! SYS-49b enumerates the demotion phase as four ordered steps: *(1) cease generating new trading
//! signals, (2) cancel all resting orders on IB, (3) submit market liquidation orders for all open
//! IB positions, (4) wait for fill confirmation or a configurable timeout (default 60 seconds).*
//! Once flat, "the strategy container transitions to paper-simulation mode … with a flat start."
//!
//! [`crate::StrategyOrchestrator::resolve_demotion`] owns step 4's *decision*. This module owns the
//! rest: steps 1–3 ([`execute_demotion_sequence`]), the concrete flat-confirmation probe that feeds
//! step 4 ([`PollingFlatProbe`]), and the paper transition that may only follow a clean, flat
//! demotion ([`complete_demotion_to_paper`]).
//!
//! ## Why the order is load-bearing, not cosmetic
//!
//! Each step exists to make the next one meaningful, so running them out of order silently defeats
//! them:
//!
//! * Cancelling resting orders **before** liquidating: a resting buy that fills while the
//!   liquidation is in flight re-opens the very position the liquidation just closed, and the probe
//!   would then never observe flat. (The kill switch documents the same race for SYS-44a.)
//! * Ceasing signals **before** cancelling: a strategy still emitting orders replaces the resting
//!   orders as fast as they are cancelled, so the cancel sweep never converges.
//!
//! The sequence therefore runs 1 → 2 → 3 unconditionally and in that order, and the report records
//! each phase's outcome. `tools/hot_swap_demotion_check.py` pins the ordering statically.
//!
//! ## Why a failed signal-halt poisons a flat result
//!
//! Continue-to-safety (the kill switch's discipline) says a failure in one phase must not suppress
//! the others — cancelling and liquidating are still the right actions. But it does **not** follow
//! that the outcome is clean. If signals were never ceased, the demoted strategy can open a new
//! position the moment after the probe observes flat, and a promotion on that observation would put
//! two strategies on the live account (SyRS SYS-2a / AC-15). So the sequence attempts everything,
//! and [`complete_demotion_to_paper`] then **refuses** to finish a demotion whose sequence was not
//! clean, even when the probe reported flat. Fail closed toward *not promoting* is always the safe
//! direction.
//!
//! ## Why the ports are narrower than the kill switch's
//!
//! [`DemotionBrokerageControl`] carries `cancel_resting_order` and `submit_market_liquidation` and
//! nothing else. The kill switch's `KillSwitchBrokerageControl` also carries `disconnect`, and
//! reusing it here would put "disconnect from IB" within reach of a routine strategy changeover.
//! SYS-49b calls for no disconnect — the account keeps trading, with a different strategy — so the
//! port the sequence is handed simply cannot express it. Only the record *shapes*
//! ([`RestingOrderCancel`], [`LiquidationSubmission`]) are shared, so a demotion's audit trail and
//! a kill switch's stay directly comparable.

use std::collections::BTreeMap;
use std::sync::Mutex;

use atp_execution::kill_switch_probe::KillSwitchProbeClock;
use atp_execution::live_state::LiveExecutionState;
use atp_execution::outbox::BrokerReconcileError;
use atp_types::{
    AssetClass, HotSwapDemotionOutcome, HotSwapDemotionRequest, LiquidationSubmission, OrderSide,
    OrderSubmission, OrderType, RestingOrderCancel, RestingOrderCancelOutcome, SideEffectOutcome,
    StrategyId,
};

use crate::{HotSwapDemotionResolved, HotSwapLiquidationProbe, HotSwapSideEffectError};

/// Milliseconds between flat-confirmation polls. 500 ms keeps the probe well inside the 60 s SYS-49b
/// budget (121 polls) without hammering the broker position source — the same cadence the SYS-44b
/// kill-switch probe uses.
pub const DEMOTION_FLAT_POLL_INTERVAL_MS: u64 = 500;

/// SYS-49b (1): cease generating new trading signals for the demoting strategy.
///
/// Read as a *mutation of the strategy runtime*, not an observation: after `Ok(())` the strategy
/// must be unable to originate a new order. The concrete implementation stops the strategy
/// container's signal loop (the deferred SRS-ORCH-* strategy runtime); the fixture implementation
/// records the call.
pub trait SignalHalt {
    fn cease_new_signals(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError>;
}

/// SYS-49b (2) and (3): the two brokerage actions a demotion performs, and no others.
///
/// Deliberately narrower than the kill switch's control port — see the module docs. The concrete
/// implementation routes to the IB adapter (the deferred `atp-adapters` leg).
pub trait DemotionBrokerageControl {
    /// Cancel one resting (non-terminal) order belonging to the demoting strategy.
    fn cancel_resting_order(
        &self,
        cancel: &RestingOrderCancel,
    ) -> Result<(), HotSwapSideEffectError>;

    /// Submit one opposite-direction market liquidation for an open position.
    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), HotSwapSideEffectError>;
}

/// SYS-49b's closing clause: the demoted container transitions to paper simulation with a flat
/// start.
///
/// Reachable only through [`complete_demotion_to_paper`], which requires an acceptance token the
/// timeout branch cannot produce.
pub trait PaperTransition {
    /// Move `strategy_id` onto the internal simulation engine. The caller has already proven the
    /// strategy's live positions are flat, which is what makes the required flat start achievable —
    /// an implementation MUST NOT carry live positions across.
    fn transition_to_paper(&self, strategy_id: &StrategyId) -> Result<(), HotSwapSideEffectError>;
}

/// The demoting strategy's open IB positions.
///
/// Unlike the open-*order* seam the kill-switch probe polls, a position view needs no coverage tag:
/// a broker reports the whole position book, so an absent symbol is genuinely a flat symbol rather
/// than an ambiguous one. What it does need is fallibility — an unreadable position view must never
/// be mistaken for an empty one, which is the single way a demotion could wrongly report flat.
pub trait LivePositionSource {
    /// Signed net quantity per symbol for `strategy_id`. A symbol at zero may be present or absent;
    /// both mean flat. `Err` means the view could not be read, never that there is nothing to read.
    fn open_positions(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<BTreeMap<String, i64>, BrokerReconcileError>;
}

/// What steps 1–3 actually did, per phase.
///
/// Every phase is attempted regardless of an earlier failure (continue to safety), so this report
/// is the only place a partial demotion is visible. [`safe_to_accept_flat`](Self::safe_to_accept_flat)
/// is the predicate that decides whether a flat observation may be believed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DemotionSequenceReport {
    pub demoting_strategy_id: StrategyId,
    /// SYS-49b (1).
    pub signal_halt: SideEffectOutcome,
    /// SYS-49b (2), one entry per resting order, sorted by domain order id so an identical state
    /// produces an identical audit record.
    pub resting_order_cancels: Vec<RestingOrderCancelOutcome>,
    /// SYS-49b (3), one entry per open position.
    pub liquidations: Vec<LiquidationSubmission>,
}

impl DemotionSequenceReport {
    /// Whether a `FlatBeforeTimeout` observation may be acted on.
    ///
    /// A failed **signal halt** is disqualifying: the strategy can re-open a position immediately
    /// after the probe looks, so "flat" describes an instant rather than a state (see the module
    /// docs). A failed **cancel** is disqualifying for the same reason — a resting order that was
    /// not cancelled can still fill.
    ///
    /// A failed **liquidation submission** is deliberately NOT disqualifying on its own: it means a
    /// position was not closed, so the probe will not observe flat and the timeout branch handles
    /// it. Were it to somehow be flat anyway (the position closed by other means), the position is
    /// genuinely flat and there is nothing left to protect against.
    pub fn safe_to_accept_flat(&self) -> bool {
        !self.signal_halt.is_failed()
            && !self
                .resting_order_cancels
                .iter()
                .any(|cancel| cancel.outcome.is_failed())
    }

    /// Every phase succeeded, with nothing degraded. Reported for the operator surface; the
    /// promotion decision uses [`safe_to_accept_flat`](Self::safe_to_accept_flat).
    pub fn fully_clean(&self) -> bool {
        self.safe_to_accept_flat()
            && !self
                .liquidations
                .iter()
                .any(|liquidation| liquidation.outcome.is_failed())
    }

    /// A one-line operator-facing reason a flat result may not be accepted, or `None` when it may.
    pub fn degradation_reason(&self) -> Option<String> {
        if let SideEffectOutcome::Failed { reason } = &self.signal_halt {
            return Some(format!(
                "SYS-49b (1) cease-new-signals failed ({reason}) — the demoted strategy can open a \
                 new position after the flat observation"
            ));
        }
        let failed: Vec<&str> = self
            .resting_order_cancels
            .iter()
            .filter(|cancel| cancel.outcome.is_failed())
            .map(|cancel| cancel.order.order_id.as_str())
            .collect();
        if failed.is_empty() {
            return None;
        }
        Some(format!(
            "SYS-49b (2) cancel failed for resting order(s) {} — an uncancelled order can still fill",
            failed.join(", "),
        ))
    }
}

fn outcome_of(result: Result<(), HotSwapSideEffectError>) -> SideEffectOutcome {
    match result {
        Ok(()) => SideEffectOutcome::Succeeded,
        Err(error) => SideEffectOutcome::Failed {
            reason: error.reason,
        },
    }
}

/// Run SYS-49b steps 1–3 for `request.demoting_strategy_id`, in that order.
///
/// Mirrors the SRS-SAFE-001 activation sequence's structure — attempt every phase, record every
/// outcome, never abort early — over the narrower demotion port set. Step 4 (the wait) is
/// [`PollingFlatProbe`], and the decision it feeds is
/// [`crate::StrategyOrchestrator::resolve_demotion`].
pub fn execute_demotion_sequence<H, B>(
    request: &HotSwapDemotionRequest,
    state: &LiveExecutionState,
    signals: &H,
    brokerage: &B,
) -> DemotionSequenceReport
where
    H: SignalHalt,
    B: DemotionBrokerageControl,
{
    let demoting = &request.demoting_strategy_id;

    // Phase 1 — SYS-49b (1). FIRST, so the cancel sweep below converges: a strategy still emitting
    // orders replaces resting orders as fast as they are cancelled.
    let signal_halt = outcome_of(signals.cease_new_signals(demoting));

    // Phase 2 — SYS-49b (2). Every resting (non-terminal) order of the demoting strategy, attempted
    // regardless of earlier failures. An order with no broker binding is still handed to the port
    // (it may be in flight), never silently skipped. The ledger iterates in hash order, so the set
    // is SORTED by domain order id — this report is audit evidence and must be deterministic for an
    // identical state.
    let mut resting: Vec<RestingOrderCancel> = state
        .orders()
        .orders_iter()
        .filter(|order| !order.state().is_terminal() && order.strategy_id() == demoting)
        .map(|order| RestingOrderCancel {
            order_id: order.key().to_string(),
            symbol: order.submission().symbol.clone(),
            broker_order_id: state.broker_id(order.key()).map(String::from),
        })
        .collect();
    resting.sort_by(|a, b| a.order_id.cmp(&b.order_id));
    let resting_order_cancels: Vec<RestingOrderCancelOutcome> = resting
        .into_iter()
        .map(|cancel| {
            let outcome = outcome_of(brokerage.cancel_resting_order(&cancel));
            RestingOrderCancelOutcome {
                order: cancel,
                outcome,
            }
        })
        .collect();

    // Phase 3 — SYS-49b (3). One opposite-direction market liquidation per open position: long
    // `net` → SELL |net|, short `net` → BUY |net|. Each submission is validated before routing, and
    // a failure (validation or port) is recorded without stopping the remaining liquidations.
    let liquidations: Vec<LiquidationSubmission> = state
        .open_positions()
        .iter()
        .filter(|(_, net_quantity)| **net_quantity != 0)
        .map(|(symbol, &net_quantity)| {
            let side = if net_quantity > 0 {
                OrderSide::Sell
            } else {
                OrderSide::Buy
            };
            let outcome = match i64::try_from(net_quantity.unsigned_abs()) {
                Err(_) => SideEffectOutcome::Failed {
                    reason: format!(
                        "liquidation quantity for {symbol} overflows the order envelope \
                         (net position {net_quantity})",
                    ),
                },
                Ok(quantity) => {
                    let submission = OrderSubmission::new(
                        demoting.clone(),
                        symbol.clone(),
                        quantity,
                        AssetClass::Equity,
                        side,
                        OrderType::Market,
                    );
                    match submission.validate() {
                        Err(error) => SideEffectOutcome::Failed {
                            reason: format!(
                                "liquidation submission rejected before routing: {error}",
                            ),
                        },
                        Ok(()) => outcome_of(brokerage.submit_market_liquidation(&submission)),
                    }
                }
            };
            LiquidationSubmission {
                symbol: symbol.clone(),
                side,
                quantity: net_quantity.unsigned_abs().min(i64::MAX as u64) as i64,
                outcome,
            }
        })
        .collect();

    DemotionSequenceReport {
        demoting_strategy_id: demoting.clone(),
        signal_halt,
        resting_order_cancels,
        liquidations,
    }
}

/// The concrete SYS-49b step-4 wait loop: poll the demoting strategy's positions until they are all
/// flat, or the configured timeout elapses.
///
/// Timing is injected through [`KillSwitchProbeClock`] (reused rather than re-declared — it is
/// already the repo's probe-clock seam), so a full 60 s drill completes instantly under a simulated
/// clock and no test ever sleeps.
///
/// ## Fail-closed rules
///
/// * The deadline is enforced **before** each poll, so a flat state first observed at or after the
///   deadline is not accepted. This also guarantees `elapsed_seconds >= timeout_seconds` on the
///   timeout outcome and `elapsed_seconds <= timeout_seconds` on the flat outcome — exactly the
///   outcome-consistency the gate checks, so this probe can never trip it.
/// * A position-source error is **never** flat. [`HotSwapLiquidationProbe`] returns a bare outcome
///   with no error arm, so a degraded source cannot be surfaced as a third answer; the probe
///   instead keeps polling and lets the deadline decide, which is the fail-closed direction. The
///   reason is not lost: it is recorded and readable through [`degradation`](Self::degradation) so
///   the composition can put "the position source was down" on the operator record instead of a
///   bare "liquidation timed out".
/// * A position at a non-zero quantity is not flat. A symbol present at zero is flat, and so is an
///   absent one (see [`LivePositionSource`]).
pub struct PollingFlatProbe<'a, C: KillSwitchProbeClock, S: LivePositionSource> {
    clock: &'a C,
    positions: &'a S,
    poll_interval_ms: u64,
    degradation: Mutex<Option<String>>,
    degraded_polls: Mutex<u64>,
}

impl<'a, C: KillSwitchProbeClock, S: LivePositionSource> PollingFlatProbe<'a, C, S> {
    pub fn new(clock: &'a C, positions: &'a S) -> Self {
        Self::with_poll_interval(clock, positions, DEMOTION_FLAT_POLL_INTERVAL_MS)
    }

    /// Custom poll cadence. Exposed for tests and the operator drill; production uses
    /// [`DEMOTION_FLAT_POLL_INTERVAL_MS`]. A zero interval is clamped to 1 ms so the loop always
    /// makes progress.
    pub fn with_poll_interval(clock: &'a C, positions: &'a S, poll_interval_ms: u64) -> Self {
        Self {
            clock,
            positions,
            poll_interval_ms: poll_interval_ms.max(1),
            degradation: Mutex::new(None),
            degraded_polls: Mutex::new(0),
        }
    }

    /// The most recent position-source failure observed while waiting, if any.
    ///
    /// A timeout with a populated degradation reason means "we could not see the positions", which
    /// an operator must not confuse with "the liquidation did not fill".
    pub fn degradation(&self) -> Option<String> {
        self.degradation
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    /// How many polls could not read the position view.
    pub fn degraded_polls(&self) -> u64 {
        *self
            .degraded_polls
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn record_degradation(&self, error: &BrokerReconcileError) {
        let mut reason = self
            .degradation
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *reason = Some(format!(
            "position view unreadable ({}): {}",
            error.category(),
            error.reason()
        ));
        let mut count = self
            .degraded_polls
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        *count = count.saturating_add(1);
    }

    /// One poll: `true` when every position is flat, `false` when a position remains OR the view
    /// could not be read. Never an error — see the fail-closed rules.
    fn poll_flat(&self, strategy_id: &StrategyId) -> bool {
        match self.positions.open_positions(strategy_id) {
            Ok(positions) => positions.values().all(|quantity| *quantity == 0),
            Err(error) => {
                self.record_degradation(&error);
                false
            }
        }
    }
}

impl<C: KillSwitchProbeClock, S: LivePositionSource> HotSwapLiquidationProbe
    for PollingFlatProbe<'_, C, S>
{
    fn await_flat_or_timeout(&self, request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        let started_ms = self.clock.monotonic_ms();
        let deadline_ms = request.timeout_seconds.saturating_mul(1_000);
        loop {
            let elapsed_ms = self.clock.monotonic_ms().saturating_sub(started_ms);
            // The deadline is enforced BEFORE polling: SYS-49b asks whether the positions reached
            // flat WITHIN the window, so a flat state first observed now — including via a real
            // clock's final sleep overshooting under scheduler jitter — must not be accepted.
            if elapsed_ms >= deadline_ms {
                return HotSwapDemotionOutcome::TimedOutDemotionPending {
                    // elapsed_ms >= timeout_seconds * 1000, so the truncated division reports
                    // elapsed_seconds >= timeout_seconds and the gate's consistency check passes.
                    elapsed_seconds: elapsed_ms / 1_000,
                    timeout_seconds: request.timeout_seconds,
                };
            }
            if self.poll_flat(&request.demoting_strategy_id) {
                return HotSwapDemotionOutcome::FlatBeforeTimeout {
                    // elapsed_ms < deadline_ms here, so the truncated division always reports an
                    // in-window elapsed_seconds <= timeout_seconds.
                    elapsed_seconds: elapsed_ms / 1_000,
                };
            }
            let remaining_ms = deadline_ms - elapsed_ms;
            self.clock.wait_ms(self.poll_interval_ms.min(remaining_ms));
        }
    }
}

/// A demotion that finished: positions were flat inside the timeout, the sequence was clean, and the
/// container is on the internal simulation engine with a flat start.
///
/// This is SYS-49d's precondition ("Once the demotion phase completes successfully (all IB positions
/// flat), the system shall execute the promotion phase"). The promotion phase itself is SRS-RESV-005.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DemotionCompleted {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    /// What the probe reported, carried so the operator surface renders the gate's own number.
    pub elapsed_seconds: u64,
}

/// Why a demotion that reached flat still did not complete.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DemotionCompletionError {
    /// Steps 1–3 left the demotion unable to vouch for the flat observation. Promotion must not
    /// follow; see [`DemotionSequenceReport::safe_to_accept_flat`].
    SequenceDegraded { reason: String },
    /// The acceptance token and the sequence report describe different strategies. Refused rather
    /// than resolved: pairing one strategy's clean sequence with another's acceptance is exactly
    /// how a demotion would be vouched for by evidence that is not about it.
    IdentityMismatch { resolved: String, sequence: String },
    /// The container could not be moved onto the simulation engine.
    Transition { reason: String },
}

impl std::fmt::Display for DemotionCompletionError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::SequenceDegraded { reason } => write!(
                formatter,
                "SRS-RESV-004: demotion reached flat but its sequence was degraded — {reason}; \
                 refusing to complete the demotion, promotion stays blocked"
            ),
            Self::IdentityMismatch { resolved, sequence } => write!(
                formatter,
                "SRS-RESV-004: demotion acceptance names strategy {resolved} but the sequence \
                 report describes {sequence} — the evidence is not about this demotion"
            ),
            Self::Transition { reason } => write!(
                formatter,
                "SRS-RESV-004 + SyRS SYS-49b: live positions are flat but the demoted container \
                 could not transition to paper simulation — {reason}"
            ),
        }
    }
}

impl std::error::Error for DemotionCompletionError {}

/// SYS-49b's closing clause: move the demoted strategy to paper simulation with a flat start.
///
/// Takes [`HotSwapDemotionResolved`] — the acceptance token the gate constructs **only** on the
/// `FlatBeforeTimeout` arm — so "transition to paper only after live positions are flat" is
/// enforced by the type, not by a caller remembering to check an outcome. There is no way to obtain
/// this token from a timed-out demotion.
///
/// It additionally re-validates the demotion against its own sequence, in both directions
/// (identity agreement, then sequence cleanliness), before touching the runtime. A flat observation
/// alone is not sufficient evidence — see the module docs.
pub fn complete_demotion_to_paper<T>(
    resolved: &HotSwapDemotionResolved,
    sequence: &DemotionSequenceReport,
    paper: &T,
) -> Result<DemotionCompleted, DemotionCompletionError>
where
    T: PaperTransition,
{
    if resolved.demoting_strategy_id != sequence.demoting_strategy_id {
        return Err(DemotionCompletionError::IdentityMismatch {
            resolved: resolved.demoting_strategy_id.as_str().to_string(),
            sequence: sequence.demoting_strategy_id.as_str().to_string(),
        });
    }
    if let Some(reason) = sequence.degradation_reason() {
        return Err(DemotionCompletionError::SequenceDegraded { reason });
    }
    paper
        .transition_to_paper(&resolved.demoting_strategy_id)
        .map_err(|error| DemotionCompletionError::Transition {
            reason: error.reason,
        })?;
    Ok(DemotionCompleted {
        demoting_strategy_id: resolved.demoting_strategy_id.clone(),
        candidate_strategy_id: resolved.candidate_strategy_id.clone(),
        elapsed_seconds: resolved.elapsed_seconds,
    })
}
