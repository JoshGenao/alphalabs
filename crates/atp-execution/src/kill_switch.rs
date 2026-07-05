//! Kill-switch **activation gate** — the SRS-SAFE-001 QuantConnect-Liquidate
//! sequence (SyRS SYS-44a; NFR-P3; NFR-SC1; StRS SN-1.11).
//!
//! # What this module owns
//!
//! The orchestrated activation itself: given the live execution state (the
//! SRS-EXE-005 order ledger / broker-id bindings / open positions) and the
//! four ports below, [`ExecutionEngine::activate_kill_switch`] halts every
//! paper simulation engine, cancels every resting live-strategy order,
//! submits an opposite-direction market liquidation for every open
//! live-strategy position, disconnects from the brokerage, and returns a
//! [`KillSwitchActivationReport`] recording every phase outcome and the
//! monotonic timing marks the NFR-P3 (5 s) and SRS-LOG-001 (1 s HALTED
//! observability) budgets are judged against.
//!
//! # Phase ordering (deliberate, and why it deviates from the prose listing)
//!
//! SRS-SAFE-001 *lists* the sequence as cancel → liquidate → halt →
//! disconnect, but its acceptance criteria impose two measurable constraints:
//! the HALTED transition must be observable through SRS-LOG-001 within
//! **1 second** of activation, while cancel + liquidation submission may
//! lawfully take up to **5 seconds** (NFR-P3). A halt executed after the
//! brokerage phases could therefore never guarantee its own budget — a slow
//! (but compliant) IB cancel would starve it. The gate therefore runs:
//!
//! 1. **Halt paper engines** — in-process, no brokerage I/O, so the 1 s
//!    observability budget is structurally protected (and no paper fill can
//!    race the liquidation).
//! 2. **Cancel all resting live-strategy orders** (SYS-44a (a), first half).
//! 3. **Submit market liquidations** for every open live-strategy position
//!    (SYS-44a (a), second half — `liquidations_submitted_ms` is the NFR-P3
//!    measurement point).
//! 4. **Disconnect** — the one ordering the AC states explicitly ("IB Gateway
//!    is disconnected AFTER liquidation orders are submitted"), so it is
//!    always last.
//!
//! # Failure semantics: continue-to-safety, never roll back
//!
//! Mirrors `resolve_kill_switch_timeout` (SRS-SAFE-002): every phase is
//! attempted regardless of earlier failures — a failed cancel must not
//! suppress the liquidations, the halt, or the disconnect — and every outcome
//! is recorded as a [`SideEffectOutcome`] on the report, so a failure is
//! observable rather than indistinguishable from success. The gate never
//! fabricates success: a port error is preserved as `Failed { reason }`, and
//! the report is returned on every path (there is no error return — the
//! report IS the record of what was and was not achieved).
//!
//! # What is deferred (honest scope)
//!
//! This gate is a stateless single-attempt decision surface over ports, like
//! the SAFE-002 gate beside it. The concrete runtime pieces are enumerated in
//! `architecture/runtime_services.json`
//! `kill_switch_activation_contract.deferred[]`: the real IB
//! [`KillSwitchBrokerageControl`] (SRS-EXE-006), live population of
//! `LiveExecutionState` (SRS-EXE-001 / SRS-EXE-005 producers), hosting every
//! paper strategy on fleet-registered halt gates (SRS-EXE-002), operator
//! email/SMS (SRS-NOTIF-001), the rich dashboard control (UI-4), and a
//! durable cross-process activation lockout (replay protection lives at the
//! operator layer's persisted last-activation record today). SRS-SAFE-001
//! stays `passes:false` until the live path exists.

use atp_types::{
    AssetClass, KillSwitchActivationEvent, KillSwitchActivationReport,
    KillSwitchActivationRequest, KillSwitchActivationTimings, LiquidationSubmission, OrderSide,
    OrderSubmission, OrderType, PaperHaltSummary, RestingOrderCancel, RestingOrderCancelOutcome,
    SideEffectOutcome,
};

use crate::live_state::LiveExecutionState;
use crate::{ExecutionEngine, KillSwitchSideEffectError};

/// Injected monotonic clock for the activation timing marks. No hidden
/// wall-clock: the gate reads time ONLY through this port, so the NFR-P3 /
/// 1 s-observability evidence is measurable deterministically in tests and
/// against a real monotonic clock in the runtime.
pub trait KillSwitchClock {
    /// Milliseconds on an arbitrary-epoch monotonic clock.
    fn monotonic_ms(&self) -> u64;
}

/// The single brokerage-control port the activation sequence needs: cancel a
/// resting order, submit a market liquidation, disconnect the session. The
/// concrete impl routes to the SRS-EXE-006 IB adapter (deferred — contract
/// `deferred[]`); defining the port here keeps `atp-execution` independent of
/// `atp-adapters` (SRS-ARCH-002 dependency direction), exactly like
/// [`crate::LiveBrokerageSubmit`] and [`crate::IbLiquidationCleanup`].
pub trait KillSwitchBrokerageControl {
    /// SYS-44a (a): cancel one resting live-strategy order. Called once per
    /// resting order; a failure is recorded on that order's
    /// [`RestingOrderCancelOutcome`] and the remaining cancels are still
    /// attempted. The port receives the full [`RestingOrderCancel`] (domain
    /// order id + optional broker binding) so it can also handle an order
    /// that never reached the broker (`broker_order_id: None`) without the
    /// gate guessing.
    fn cancel_resting_order(
        &self,
        cancel: &RestingOrderCancel,
    ) -> Result<(), KillSwitchSideEffectError>;

    /// SYS-44a (a): submit one opposite-direction market liquidation order.
    /// The submission is `validate()`d by the gate before this is called; a
    /// port failure is recorded on that position's [`LiquidationSubmission`]
    /// and the remaining liquidations are still attempted.
    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), KillSwitchSideEffectError>;

    /// SYS-44a (c): disconnect from the brokerage gateway. Called LAST,
    /// unconditionally (even after failures — a wedged cancel is more reason
    /// to sever the session, not less).
    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError>;
}

/// Fan-out port halting EVERY paper simulation engine (SYS-44a (b)). The
/// concrete impl composes `atp-simulation`'s `PaperEngineFleet` over the
/// sealed per-engine `HaltablePaperEngine` gates; the port keeps
/// `atp-execution` independent of `atp-simulation` (sibling layers — the
/// orchestrator wires them together).
pub trait PaperHaltFanout {
    /// Halt every registered engine for the kill switch. Idempotent at the
    /// fleet level (an already-halted engine is counted, not an error).
    /// Returns the fan-out totals; `Err` means the fan-out itself could not
    /// run — recorded as a `Failed` paper-halt outcome, never masked.
    fn halt_all_for_kill_switch(&self) -> Result<PaperHaltSummary, KillSwitchSideEffectError>;
}

/// Best-effort audit sink for the activation record (the SRS-LOG-001
/// "kill-switch activations" system event's producer seam). Mirrors
/// [`crate::KillSwitchTimeoutEventSink`]: the activation decisions and side
/// effects are already made when this is called, so a sink failure never
/// rolls them back — durable delivery is the SRS-LOG-001 sink's
/// responsibility at the operator layer.
pub trait KillSwitchActivationEventSink {
    fn record(&self, event: KillSwitchActivationEvent) -> Result<(), KillSwitchSideEffectError>;
}

fn outcome_of(result: Result<(), KillSwitchSideEffectError>) -> SideEffectOutcome {
    match result {
        Ok(()) => SideEffectOutcome::Succeeded,
        Err(error) => SideEffectOutcome::Failed {
            reason: error.reason,
        },
    }
}

impl ExecutionEngine {
    /// Run the SRS-SAFE-001 activation sequence (module doc: phase ordering
    /// and failure semantics). Always returns the full
    /// [`KillSwitchActivationReport`] — the report is the record of what was
    /// and was not achieved; callers judge safety posture from its per-phase
    /// [`SideEffectOutcome`]s (`fully_clean`) and its timings
    /// (`within_nfr_p3`), never from reaching the return alone.
    pub fn activate_kill_switch<K, B, F, E>(
        &self,
        request: KillSwitchActivationRequest,
        state: &LiveExecutionState,
        clock: &K,
        brokerage: &B,
        paper_engines: &F,
        events: &E,
    ) -> KillSwitchActivationReport
    where
        K: KillSwitchClock,
        B: KillSwitchBrokerageControl,
        F: PaperHaltFanout,
        E: KillSwitchActivationEventSink,
    {
        let started_ms = clock.monotonic_ms();
        let elapsed = |clock: &K| clock.monotonic_ms().saturating_sub(started_ms);

        // Phase 1 — halt every paper engine (SYS-44a (b); module doc explains
        // why this runs FIRST: the 1 s SRS-LOG-001 observability budget must
        // not sit behind up to 5 s of lawful brokerage I/O).
        let (paper_halt, paper_halt_summary) = match paper_engines.halt_all_for_kill_switch() {
            Ok(summary) => (SideEffectOutcome::Succeeded, Some(summary)),
            Err(error) => (
                SideEffectOutcome::Failed {
                    reason: error.reason,
                },
                None,
            ),
        };
        let halt_completed_ms = elapsed(clock);

        // Phase 2 — cancel every resting (non-terminal) live-strategy order
        // (SYS-44a (a), first half). Every cancel is attempted regardless of
        // earlier failures; an order with no broker binding is still handed to
        // the port (it may be in flight), never silently skipped. The ledger
        // iterates in hash order, so the resting set is SORTED by domain order
        // id first — the activation report is audit evidence and must be
        // deterministic for an identical state (positions already are: the
        // position map is ordered).
        let mut resting: Vec<RestingOrderCancel> = state
            .orders()
            .orders_iter()
            .filter(|order| {
                !order.state().is_terminal() && order.strategy_id() == &request.live_strategy_id
            })
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
        let cancels_completed_ms = elapsed(clock);

        // Phase 3 — submit an opposite-direction market liquidation for every
        // open live-strategy position (SYS-44a (a), second half; the single-
        // live-strategy invariant, SyRS SYS-2a, is what makes every open
        // position a live-strategy position). Long `net` → SELL |net|, short
        // `net` → BUY |net|; each submission is validated before routing and
        // a failure (validation or port) is recorded without stopping the
        // remaining liquidations.
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
                            request.live_strategy_id.clone(),
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
        // The NFR-P3 measurement point: all cancels confirmed AND all
        // liquidation orders submitted.
        let liquidations_submitted_ms = elapsed(clock);

        // Phase 4 — disconnect, always last (explicit AC ordering) and always
        // attempted (continue-to-safety).
        let ib_disconnect = outcome_of(brokerage.disconnect());
        let disconnect_completed_ms = elapsed(clock);

        let report = KillSwitchActivationReport {
            activation_id: request.activation_id,
            live_strategy_id: request.live_strategy_id,
            paper_halt,
            paper_halt_summary,
            resting_order_cancels,
            liquidations,
            ib_disconnect,
            timings: KillSwitchActivationTimings {
                halt_completed_ms,
                cancels_completed_ms,
                liquidations_submitted_ms,
                disconnect_completed_ms,
            },
            activated_at_epoch_ms: request.activated_at_epoch_ms,
        };

        // Best-effort audit emission (mirrors the SAFE-002 sink semantics):
        // the safety actions above are already attempted; a sink failure
        // loses nothing the returned report does not still carry.
        let _ = events.record(KillSwitchActivationEvent {
            report: report.clone(),
        });

        report
    }
}
