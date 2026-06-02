//! ERR-7 / SRS-RESV-004 / SyRS SYS-49b / SYS-49c / StRS SN-1.25 — when a
//! Hot-Swap demotion's liquidation does not reach flat within the
//! configured timeout (default 60 s), the orchestrator's `resolve_demotion`
//! gate enters the demotion-pending state: it cancels the unfilled
//! liquidation order, notifies the operator over dashboard + email + SMS,
//! records the demotion transition, refuses the swap with
//! `HOT_SWAP_DEMOTION_TIMEOUT`, and blocks promotion (the caller promotes
//! only on `Ok`). On flat-before-timeout the swap proceeds with no alert
//! and no cancel.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * Timeout: `Err` with category `HotSwapDemotionTimeout` (wire string
//!     `HOT_SWAP_DEMOTION_TIMEOUT`); the canceller is called exactly once;
//!     the alert sink records exactly one event carrying all three
//!     channels; the demotion-event sink records exactly one event with
//!     `promotion_blocked == true`; the probe is consulted exactly once.
//!   * Flat (positive control): `Ok(HotSwapDemotionResolved)` with
//!     `promotion_allowed == true`, and the forbidden alert sink + forbidden
//!     canceller are NEVER invoked (they panic if they are) — proving the
//!     gate is selective.
//!   * Pseudo-property sweep over varying `(elapsed, timeout)` cases keeps
//!     every timeout blocking the swap with exactly one alert + one cancel.
//!   * Promotion-block invariant (behavioral anchor): the
//!     `HotSwapLiquidationProbe` port exposes no promotion mutator, so the
//!     gate cannot promote through it; the timeout outcome returns `Err`
//!     and constructs no `HotSwapDemotionResolved`. The primary enforcement
//!     lives in `tools/hot_swap_demotion_check.py` via the contract's
//!     `forbidden_promotions` allowlist (which rejects any `promote(`,
//!     `complete_swap(`, `go_live(`, … call in the timeout arm); this Rust
//!     test anchors the post-condition at the behavioral layer.

use atp_orchestrator::{
    HotSwapDemotionEventSink, HotSwapLiquidationProbe, HotSwapSideEffectError, OperatorAlertSink,
    StrategyOrchestrator, UnfilledOrderCanceller,
};
use atp_types::{
    HotSwapDemotionEvent, HotSwapDemotionOutcome, HotSwapDemotionRequest, OperatorAlertChannel,
    OperatorAlertEvent, OrderErrorCategory, SideEffectOutcome, StrategyId,
    HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
};
use std::cell::{Cell, RefCell};

struct HotSwapLiquidationProbeSpy {
    outcome: Cell<HotSwapDemotionOutcome>,
    calls: Cell<u32>,
}

impl HotSwapLiquidationProbeSpy {
    fn timed_out(elapsed_seconds: u64, timeout_seconds: u64) -> Self {
        Self {
            outcome: Cell::new(HotSwapDemotionOutcome::TimedOutDemotionPending {
                elapsed_seconds,
                timeout_seconds,
            }),
            calls: Cell::new(0),
        }
    }

    fn flat(elapsed_seconds: u64) -> Self {
        Self {
            outcome: Cell::new(HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds }),
            calls: Cell::new(0),
        }
    }
}

impl HotSwapLiquidationProbe for HotSwapLiquidationProbeSpy {
    fn await_flat_or_timeout(&self, _request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome {
        self.calls.set(self.calls.get() + 1);
        self.outcome.get()
    }
}

#[derive(Default)]
struct UnfilledOrderCancellerSpy {
    cancels: RefCell<Vec<HotSwapDemotionRequest>>,
}

impl UnfilledOrderCanceller for UnfilledOrderCancellerSpy {
    fn cancel_unfilled_liquidation_orders(
        &self,
        request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        self.cancels.borrow_mut().push(request.clone());
        Ok(())
    }
}

/// Canceller that records the call but reports failure — models a failed IB
/// `cancel_order` (e.g. connectivity lost). The gate must still attempt the
/// alert, record `liquidation_cancel = Failed`, and block promotion.
#[derive(Default)]
struct UnfilledOrderFailingCanceller {
    cancels: RefCell<Vec<HotSwapDemotionRequest>>,
}

impl UnfilledOrderCanceller for UnfilledOrderFailingCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        self.cancels.borrow_mut().push(request.clone());
        Err(HotSwapSideEffectError::new("IB cancel_order unreachable"))
    }
}

/// Canceller that panics if consulted. Used by the flat positive control to
/// prove the unfilled-order cancel path is never invoked on an in-time
/// demotion.
struct UnfilledOrderForbiddenCanceller;

impl UnfilledOrderCanceller for UnfilledOrderForbiddenCanceller {
    fn cancel_unfilled_liquidation_orders(
        &self,
        _request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError> {
        panic!("ERR-7: FlatBeforeTimeout branch must not cancel any liquidation order");
    }
}

#[derive(Default)]
struct OperatorAlertSinkSpy {
    alerts: RefCell<Vec<OperatorAlertEvent>>,
}

impl OperatorAlertSink for OperatorAlertSinkSpy {
    fn dispatch(&self, event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        self.alerts.borrow_mut().push(event);
        Ok(())
    }
}

/// Alert sink that records the call but reports failure — models an
/// unreachable email/SMS transport. The gate must still record
/// `operator_alert = Failed` and block promotion.
#[derive(Default)]
struct OperatorAlertFailingSink {
    alerts: RefCell<Vec<OperatorAlertEvent>>,
}

impl OperatorAlertSink for OperatorAlertFailingSink {
    fn dispatch(&self, event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        self.alerts.borrow_mut().push(event);
        Err(HotSwapSideEffectError::new("SMS gateway timed out"))
    }
}

/// Alert sink that panics if consulted. Used by the flat positive control
/// to prove no operator alert is dispatched on an in-time demotion.
struct OperatorAlertForbiddenSink;

impl OperatorAlertSink for OperatorAlertForbiddenSink {
    fn dispatch(&self, _event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
        panic!("ERR-7: FlatBeforeTimeout branch must not dispatch an operator alert");
    }
}

#[derive(Default)]
struct HotSwapDemotionEventSinkSpy {
    events: RefCell<Vec<HotSwapDemotionEvent>>,
}

impl HotSwapDemotionEventSink for HotSwapDemotionEventSinkSpy {
    fn record(&self, event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// Event sink that records the event but reports a publication failure —
/// models an unwritable audit log / disconnected dashboard channel. The gate
/// must treat emission as best-effort: it must NOT panic or abort, and the
/// safety side effects (cancel + alert) and the promotion block must stand.
#[derive(Default)]
struct HotSwapDemotionEventFailingSink {
    events: RefCell<Vec<HotSwapDemotionEvent>>,
}

impl HotSwapDemotionEventSink for HotSwapDemotionEventFailingSink {
    fn record(&self, event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Err(HotSwapSideEffectError::new("audit log unwritable"))
    }
}

fn demotion(demoting: &str, candidate: &str, timeout_seconds: u64) -> HotSwapDemotionRequest {
    HotSwapDemotionRequest {
        demoting_strategy_id: StrategyId::new(demoting),
        candidate_strategy_id: StrategyId::new(candidate),
        timeout_seconds,
    }
}

const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

#[test]
fn err_7_timeout_enters_demotion_pending_blocks_promotion_and_alerts_all_channels() {
    // SRS-RESV-004: the liquidation timed out — enter demotion-pending,
    // notify the operator, cancel the unfilled order, and block promotion.
    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
    let canceller = UnfilledOrderCancellerSpy::default();
    let alerts = OperatorAlertSinkSpy::default();
    let events = HotSwapDemotionEventSinkSpy::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    let error = orchestrator
        .resolve_demotion(
            request.clone(),
            &probe,
            &canceller,
            &alerts,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-7: a liquidation timeout must refuse the swap");

    // Structured error: category + SyRS SYS-64 wire string + SRS trace.
    assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
    assert_eq!(error.category.as_str(), "HOT_SWAP_DEMOTION_TIMEOUT");
    assert_eq!(error.original_request, request);
    assert!(error.message.contains("SRS-RESV-004"));
    assert!(error.message.contains("SYS-49b"));
    assert!(error.message.contains("SYS-49c"));
    assert!(error.message.contains("live-momentum"));
    assert!(error.message.contains("paper-reversal"));

    // The probe is the timing authority, consulted exactly once.
    assert_eq!(probe.calls.get(), 1);

    // The unfilled liquidation order is canceled exactly once, for this request.
    let cancels = canceller.cancels.borrow();
    assert_eq!(cancels.len(), 1);
    assert_eq!(cancels[0], request);

    // The operator is alerted over all three channels, exactly once.
    let alerts_seen = alerts.alerts.borrow();
    assert_eq!(alerts_seen.len(), 1);
    let alert = &alerts_seen[0];
    assert!(alert.channels.contains(&OperatorAlertChannel::Dashboard));
    assert!(alert.channels.contains(&OperatorAlertChannel::Email));
    assert!(alert.channels.contains(&OperatorAlertChannel::Sms));
    assert_eq!(alert.elapsed_seconds, 72);
    assert_eq!(alert.timeout_seconds, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);

    // The demotion-pending transition is recorded with promotion blocked
    // and both side effects recorded as Succeeded (the spies returned Ok).
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].promotion_blocked);
    assert!(events_seen[0].outcome.is_demotion_pending());
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::Succeeded
    );
    assert_eq!(events_seen[0].operator_alert, SideEffectOutcome::Succeeded);
}

#[test]
fn err_7_flat_before_timeout_promotes_with_no_alert_or_cancel() {
    // SRS-RESV-004: positions reached flat in time — the swap proceeds, no
    // alert and no cancel. The forbidden stubs panic if either is touched.
    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::flat(11);
    let canceller = UnfilledOrderForbiddenCanceller;
    let alerts = OperatorAlertForbiddenSink;
    let events = HotSwapDemotionEventSinkSpy::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    let resolved = orchestrator
        .resolve_demotion(
            request.clone(),
            &probe,
            &canceller,
            &alerts,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect("ERR-7: a flat-before-timeout demotion must proceed");

    assert!(resolved.promotion_allowed);
    assert_eq!(resolved.demoting_strategy_id, request.demoting_strategy_id);
    assert_eq!(
        resolved.candidate_strategy_id,
        request.candidate_strategy_id
    );
    assert_eq!(resolved.elapsed_seconds, 11);
    assert_eq!(probe.calls.get(), 1);

    // The audit transition is still recorded, with promotion NOT blocked and
    // both side effects NotAttempted (no cancel / no alert on the flat path).
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(!events_seen[0].promotion_blocked);
    assert!(!events_seen[0].outcome.is_demotion_pending());
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
    assert_eq!(
        events_seen[0].operator_alert,
        SideEffectOutcome::NotAttempted
    );
}

#[test]
fn err_7_failed_cancel_and_alert_are_observable_and_still_block_promotion() {
    // SRS-RESV-004 observability: when the IB cancel AND the operator-alert
    // transport both fail, the gate must still attempt BOTH (a failed cancel
    // must not suppress the page), record each as Failed on the demotion
    // event so the failure is not indistinguishable from success, and still
    // block promotion (return Err).
    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::timed_out(80, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
    let canceller = UnfilledOrderFailingCanceller::default();
    let alerts = OperatorAlertFailingSink::default();
    let events = HotSwapDemotionEventSinkSpy::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    let error = orchestrator
        .resolve_demotion(
            request,
            &probe,
            &canceller,
            &alerts,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-7: a timeout must block promotion even when side effects fail");

    // Promotion still blocked.
    assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);

    // BOTH side effects were attempted despite each failing.
    assert_eq!(canceller.cancels.borrow().len(), 1);
    assert_eq!(alerts.alerts.borrow().len(), 1);

    // The event records each failure (observable, not silent).
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].promotion_blocked);
    assert!(events_seen[0].liquidation_cancel.is_failed());
    assert!(events_seen[0].operator_alert.is_failed());
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::Failed {
            reason: "IB cancel_order unreachable".to_string(),
        }
    );
}

#[test]
fn err_7_flat_outcome_over_deadline_is_failed_closed_and_blocks_promotion() {
    // Defense-in-depth: a probe that mislabels an over-deadline demotion as
    // FlatBeforeTimeout (elapsed 80 > 60 s timeout) must NOT bypass the
    // promotion block. The gate normalises it to a timeout: the cancel + the
    // operator alert fire, the event is demotion-pending, and promotion is
    // blocked (Err).
    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::flat(80);
    let canceller = UnfilledOrderCancellerSpy::default();
    let alerts = OperatorAlertSinkSpy::default();
    let events = HotSwapDemotionEventSinkSpy::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    let error = orchestrator
        .resolve_demotion(
            request,
            &probe,
            &canceller,
            &alerts,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-7: a flat outcome past the timeout must fail closed");

    assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
    assert_eq!(canceller.cancels.borrow().len(), 1);
    assert_eq!(alerts.alerts.borrow().len(), 1);
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].promotion_blocked);
    assert!(events_seen[0].outcome.is_demotion_pending());
}

#[test]
fn err_7_audit_sink_failure_is_best_effort_and_safety_posture_holds() {
    // SRS-RESV-004: if the demotion event sink fails (audit log unwritable),
    // the gate must NOT panic or abort — the cancel + the operator alert
    // still fire and promotion stays blocked. Event emission is best-effort;
    // durable delivery is the deferred SRS-LOG-001 sink's concern.
    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::timed_out(70, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
    let canceller = UnfilledOrderCancellerSpy::default();
    let alerts = OperatorAlertSinkSpy::default();
    let events = HotSwapDemotionEventFailingSink::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    let error = orchestrator
        .resolve_demotion(
            request,
            &probe,
            &canceller,
            &alerts,
            &events,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("ERR-7: a timeout blocks promotion even when the audit sink fails");

    assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
    // The safety side effects still fired despite the audit-sink failure.
    assert_eq!(canceller.cancels.borrow().len(), 1);
    assert_eq!(alerts.alerts.borrow().len(), 1);
    // The sink was invoked (it recorded, then reported the publication failure).
    assert_eq!(events.events.borrow().len(), 1);
}

#[test]
fn err_7_timeout_blocks_promotion_across_many_demotions() {
    // Pseudo-property sweep: every timeout outcome blocks the swap and emits
    // exactly one alert + one cancel, regardless of the (elapsed, timeout)
    // numerics.
    let orchestrator = StrategyOrchestrator;
    let cases = [(61_u64, 60_u64), (90, 60), (120, 60), (75, 45)];
    for (elapsed, timeout) in cases {
        let probe = HotSwapLiquidationProbeSpy::timed_out(elapsed, timeout);
        let canceller = UnfilledOrderCancellerSpy::default();
        let alerts = OperatorAlertSinkSpy::default();
        let events = HotSwapDemotionEventSinkSpy::default();
        let request = demotion("live-x", "paper-y", timeout);

        let error = orchestrator
            .resolve_demotion(
                request,
                &probe,
                &canceller,
                &alerts,
                &events,
                OBSERVED_AT_SECONDS,
            )
            .expect_err("ERR-7: every liquidation timeout must block the swap");

        assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
        assert_eq!(canceller.cancels.borrow().len(), 1);
        let alerts_seen = alerts.alerts.borrow();
        assert_eq!(alerts_seen.len(), 1);
        assert_eq!(alerts_seen[0].channels.len(), 3);
        let events_seen = events.events.borrow();
        assert_eq!(events_seen.len(), 1);
        assert!(events_seen[0].promotion_blocked);
    }
}
