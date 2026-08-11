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

use atp_orchestrator::demotion_pending_store::{DemotionPendingRecord, DemotionPendingState};
use atp_orchestrator::{
    DemotionPendingLock, HotSwapDemotionEventSink, HotSwapLiquidationProbe, HotSwapSideEffectError,
    OperatorAlertSink, StrategyOrchestrator, UnfilledOrderCanceller,
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

/// Lockout spy over the SRS-RESV-004 durable demotion-pending store. Starts CLEAR and
/// records every engage, so a test can assert that the timeout branch persisted the
/// block rather than merely returning `Err` for one call.
#[derive(Default)]
struct DemotionPendingLockSpy {
    engaged: RefCell<Vec<DemotionPendingRecord>>,
}

impl DemotionPendingLock for DemotionPendingLockSpy {
    fn state(&self) -> DemotionPendingState {
        match self.engaged.borrow().last() {
            None => DemotionPendingState::Clear,
            Some(record) => DemotionPendingState::Pending(Box::new(record.clone())),
        }
    }

    fn engage(&self, record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        self.engaged.borrow_mut().push(record);
        Ok(())
    }

    fn amend(&self, record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        // Mirrors the store: an amend REPLACES the held record rather than adding a lockout.
        let mut engaged = self.engaged.borrow_mut();
        if engaged.is_empty() {
            return Err(HotSwapSideEffectError::new("nothing to amend"));
        }
        *engaged.last_mut().expect("held") = record;
        Ok(())
    }
}

/// Lockout that records the engage but reports failure — models an unwritable store.
/// The gate must still block promotion for THIS call, and must say the block is not
/// durable rather than implying the retry is covered.
#[derive(Default)]
struct DemotionPendingFailingLock {
    engaged: RefCell<Vec<DemotionPendingRecord>>,
}

impl DemotionPendingLock for DemotionPendingFailingLock {
    fn state(&self) -> DemotionPendingState {
        // Mirrors the real FileDemotionPendingLock: a failed engage poisons the lock, so a
        // retry cannot read Clear and promote. Before that, the store is genuinely clear.
        match self.engaged.borrow().is_empty() {
            true => DemotionPendingState::Clear,
            false => DemotionPendingState::Poisoned {
                reason: "the demotion-pending lockout could NOT be persisted".to_string(),
            },
        }
    }

    fn engage(&self, record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        self.engaged.borrow_mut().push(record);
        Err(HotSwapSideEffectError::new("lockout store unwritable"))
    }

    fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        // Phase one failed, so there is nothing to amend — and the gate must not try.
        panic!("a lockout that could not be engaged must not be amended");
    }
}

/// Lockout that panics if engaged. Used by the flat positive control to prove an
/// in-time demotion persists no demotion-pending state.
struct DemotionPendingForbiddenLock;

impl DemotionPendingLock for DemotionPendingForbiddenLock {
    fn state(&self) -> DemotionPendingState {
        DemotionPendingState::Clear
    }

    fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("ERR-7: FlatBeforeTimeout branch must not engage a demotion-pending lockout");
    }

    fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
        panic!("ERR-7: FlatBeforeTimeout branch must not touch the demotion-pending lockout");
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
    let lock = DemotionPendingLockSpy::default();
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
            &lock,
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

    // SyRS SYS-49c (c): the demotion-pending state is DURABLE, and the persisted
    // record carries the two facts an operator resolves against — whether a live
    // liquidation order may still be resting, and whether anyone was paged.
    assert_eq!(
        events_seen[0].demotion_pending,
        SideEffectOutcome::Succeeded
    );
    assert!(error.promotion_block_is_durable);
    let engaged = lock.engaged.borrow();
    assert_eq!(engaged.len(), 1);
    assert_eq!(
        engaged[0].demoting_strategy_id,
        request.demoting_strategy_id
    );
    assert_eq!(
        engaged[0].candidate_strategy_id,
        request.candidate_strategy_id
    );
    assert_eq!(engaged[0].elapsed_seconds, 72);
    assert_eq!(
        engaged[0].timeout_seconds,
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS
    );
    assert_eq!(engaged[0].observed_at_seconds, OBSERVED_AT_SECONDS);
    assert_eq!(engaged[0].liquidation_cancel, SideEffectOutcome::Succeeded);
    assert_eq!(engaged[0].operator_alert, SideEffectOutcome::Succeeded);
}

#[test]
fn resv_4_a_held_lockout_blocks_a_later_flat_demotion_until_it_is_resolved() {
    // THE regression this feature exists for. SyRS SYS-49c (d): "block the promotion
    // phase until the operator manually resolves the unfilled positions."
    //
    // Before the durable lockout, `resolve_demotion` was a stateless single-attempt
    // decision: attempt 1 timed out and returned Err, and attempt 2 — a retry, a
    // restart, another operator surface — was judged purely on its own probe. A probe
    // reporting flat (the demoted strategy's positions having been closed by anything
    // OTHER than the timed-out liquidation, or simply a different sampling moment)
    // promoted the candidate over positions nobody had resolved.
    //
    // The lockout persists across the two calls here, so attempt 2 must be refused
    // even though its probe says flat — and refused BEFORE any destructive side
    // effect fires, since nothing new has gone wrong.
    let orchestrator = StrategyOrchestrator;
    let lock = DemotionPendingLockSpy::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    // Attempt 1 — times out and engages the lockout.
    let first = orchestrator
        .resolve_demotion(
            request.clone(),
            &HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            &UnfilledOrderCancellerSpy::default(),
            &OperatorAlertSinkSpy::default(),
            &HotSwapDemotionEventSinkSpy::default(),
            &lock,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("attempt 1: a liquidation timeout must refuse the swap");
    assert_eq!(first.error_type, "HotSwapDemotionTimeout");
    assert!(first.promotion_block_is_durable);
    assert_eq!(lock.engaged.borrow().len(), 1);

    // Attempt 2 — the probe now reports FLAT, well inside the deadline. The forbidden
    // stubs prove the refusal happens before the swap is even attempted: a request
    // that was never permitted to start must not cancel orders or page anyone.
    let probe = HotSwapLiquidationProbeSpy::flat(3);
    let events = HotSwapDemotionEventSinkSpy::default();
    let second = orchestrator
        .resolve_demotion(
            request.clone(),
            &probe,
            &UnfilledOrderForbiddenCanceller,
            &OperatorAlertForbiddenSink,
            &events,
            &lock,
            OBSERVED_AT_SECONDS + 600,
        )
        .expect_err("attempt 2: a held demotion-pending lockout must block a flat retry");

    assert_eq!(second.error_type, "HotSwapDemotionPending");
    assert_eq!(second.category, OrderErrorCategory::HotSwapDemotionTimeout);
    assert!(second.promotion_block_is_durable);
    assert!(second.message.contains("SYS-49c"));
    assert!(second.message.contains("live-momentum"));
    // The probe was never even consulted — the block precedes the demotion attempt.
    assert_eq!(probe.calls.get(), 0);
    // And no second lockout was engaged over the one the operator still must resolve.
    assert_eq!(lock.engaged.borrow().len(), 1);
    assert!(events.events.borrow().is_empty());
}

#[test]
fn resv_4_an_unreadable_lockout_blocks_promotion_exactly_like_a_held_one() {
    // "Unreadable, absent, or unknown is NEVER empty." A corrupt lockout file yields
    // no record, which is the same shape as no lockout at all — and reading it as
    // "clear" would be a false all-clear on the single question the file answers.
    struct UnreadableLock;
    impl DemotionPendingLock for UnreadableLock {
        fn state(&self) -> DemotionPendingState {
            DemotionPendingState::Unreadable {
                reason: "payload declares unknown field 'demotng_strategy_id'".to_string(),
            }
        }
        fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
            panic!("RESV-004: a blocked-before-start swap must not engage a second lockout");
        }
        fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
            panic!("RESV-004: a blocked-before-start swap must not touch the lockout");
        }
    }

    let orchestrator = StrategyOrchestrator;
    let probe = HotSwapLiquidationProbeSpy::flat(3);
    let error = StrategyOrchestrator::resolve_demotion(
        &orchestrator,
        demotion(
            "live-momentum",
            "paper-reversal",
            HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
        ),
        &probe,
        &UnfilledOrderForbiddenCanceller,
        &OperatorAlertForbiddenSink,
        &HotSwapDemotionEventSinkSpy::default(),
        &UnreadableLock,
        OBSERVED_AT_SECONDS,
    )
    .expect_err("an unreadable lockout must block promotion");

    assert_eq!(error.error_type, "HotSwapDemotionPending");
    assert!(error.message.contains("cannot be read"));
    assert_eq!(probe.calls.get(), 0);
}

#[test]
fn resv_4_a_lockout_that_could_not_be_persisted_still_refuses_but_says_it_is_not_durable() {
    // A failed engage is a real safety degradation: promotion is blocked for THIS
    // call and for no other. The gate must neither swallow it (reporting a durable
    // block it did not achieve) nor let it turn the refusal into an acceptance.
    let orchestrator = StrategyOrchestrator;
    let lock = DemotionPendingFailingLock::default();
    let canceller = UnfilledOrderCancellerSpy::default();
    let alerts = OperatorAlertSinkSpy::default();
    let events = HotSwapDemotionEventSinkSpy::default();

    let error = orchestrator
        .resolve_demotion(
            demotion(
                "live-momentum",
                "paper-reversal",
                HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
            ),
            &HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            &canceller,
            &alerts,
            &events,
            &lock,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("a timeout refuses the swap whether or not the lockout persisted");

    // Still a refusal, and the safety side effects still ran.
    assert_eq!(error.error_type, "HotSwapDemotionTimeout");
    assert_eq!(canceller.cancels.borrow().len(), 1);
    assert_eq!(alerts.alerts.borrow().len(), 1);
    // But the block is NOT durable, and both the error and the event say so.
    assert!(!error.promotion_block_is_durable);
    assert!(error.message.contains("WARNING"));
    assert_eq!(lock.engaged.borrow().len(), 1);
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].demotion_pending.is_failed());
    assert!(events_seen[0].promotion_blocked);
}

#[test]
fn resv_4_a_premature_timeout_report_blocks_promotion_without_cancelling() {
    // The inverse outcome-consistency case. A probe reporting TimedOutDemotionPending
    // at 12 s against a 60 s budget contradicts itself, and the old gate would have
    // fired the destructive unfilled-order cancel on that report.
    //
    // Block-WITHOUT-cancel: promotion is refused (an unbelievable probe cannot vouch
    // for flat positions) and the lockout is engaged, but the cancel is not issued —
    // a destructive broker action must not be taken on evidence the gate is
    // simultaneously declaring untrustworthy. The operator is still paged.
    let orchestrator = StrategyOrchestrator;
    let lock = DemotionPendingLockSpy::default();
    let alerts = OperatorAlertSinkSpy::default();
    let events = HotSwapDemotionEventSinkSpy::default();

    let error = orchestrator
        .resolve_demotion(
            demotion(
                "live-momentum",
                "paper-reversal",
                HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
            ),
            &HotSwapLiquidationProbeSpy::timed_out(12, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            // Panics if the cancel path is reached.
            &UnfilledOrderForbiddenCanceller,
            &alerts,
            &events,
            &lock,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("a self-contradicting timeout report must block promotion");

    assert_eq!(error.error_type, "HotSwapDemotionProbeInconsistent");
    assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
    assert!(error.promotion_block_is_durable);
    // Paged, locked out, recorded — but nothing cancelled.
    assert_eq!(alerts.alerts.borrow().len(), 1);
    assert_eq!(lock.engaged.borrow().len(), 1);
    assert_eq!(
        lock.engaged.borrow()[0].liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
    let events_seen = events.events.borrow();
    assert_eq!(events_seen.len(), 1);
    assert!(events_seen[0].promotion_blocked);
    assert_eq!(
        events_seen[0].liquidation_cancel,
        SideEffectOutcome::NotAttempted
    );
}

#[test]
fn resv_4_a_timeout_reported_against_the_wrong_budget_is_also_inconsistent() {
    // The other half of the same class: elapsed is past the REPORTED budget, so a
    // check that only compared `elapsed < timeout_seconds` would wave it through —
    // but the reported budget is not the configured one, so the probe is still
    // describing a demotion that was not the one requested.
    let orchestrator = StrategyOrchestrator;
    let lock = DemotionPendingLockSpy::default();

    let error = orchestrator
        .resolve_demotion(
            demotion(
                "live-momentum",
                "paper-reversal",
                HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
            ),
            // 31 s elapsed against a 30 s budget is internally consistent, but the
            // request configured 60 s.
            &HotSwapLiquidationProbeSpy::timed_out(31, 30),
            &UnfilledOrderForbiddenCanceller,
            &OperatorAlertSinkSpy::default(),
            &HotSwapDemotionEventSinkSpy::default(),
            &lock,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("a timeout against a foreign budget must block promotion");

    assert_eq!(error.error_type, "HotSwapDemotionProbeInconsistent");
    assert!(error.message.contains("30"));
    assert!(error.message.contains("60"));
    assert_eq!(lock.engaged.borrow().len(), 1);
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
    let lock = DemotionPendingForbiddenLock;
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
            &lock,
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
    let lock = DemotionPendingLockSpy::default();
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
            &lock,
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
    let lock = DemotionPendingLockSpy::default();
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
            &lock,
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
    let lock = DemotionPendingLockSpy::default();
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
            &lock,
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
        let lock = DemotionPendingLockSpy::default();
        let request = demotion("live-x", "paper-y", timeout);

        let error = orchestrator
            .resolve_demotion(
                request,
                &probe,
                &canceller,
                &alerts,
                &events,
                &lock,
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

#[test]
fn resv_4_a_retry_after_a_failed_engage_is_still_blocked() {
    // The deepest form of the lockout defect. On timeout the gate engages a lockout; if that
    // write FAILS it reports `promotion_block_is_durable = false` and returns Err — which
    // blocks this call. But if nothing else changes, the store is still empty, the next
    // attempt reads Clear, and a probe reporting flat is accepted: promotion proceeds over
    // positions no operator resolved. Saying "not durable" describes the hole; the lock has
    // to close it. `(found by /codex:adversarial-review, SRS-RESV-004 r3 [high])`
    let orchestrator = StrategyOrchestrator;
    let lock = DemotionPendingFailingLock::default();
    let request = demotion(
        "live-momentum",
        "paper-reversal",
        HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
    );

    // Attempt 1 — times out, and the lockout write fails.
    let first = orchestrator
        .resolve_demotion(
            request.clone(),
            &HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            &UnfilledOrderCancellerSpy::default(),
            &OperatorAlertSinkSpy::default(),
            &HotSwapDemotionEventSinkSpy::default(),
            &lock,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("a timeout refuses the swap");
    assert_eq!(first.error_type, "HotSwapDemotionTimeout");
    assert!(
        !first.promotion_block_is_durable,
        "the engage failed, so the block did not reach disk"
    );

    // Attempt 2 — the probe now reports FLAT, well inside the deadline. Nothing may promote.
    // The forbidden stubs prove the refusal precedes any side effect.
    let probe = HotSwapLiquidationProbeSpy::flat(3);
    let second = orchestrator
        .resolve_demotion(
            request,
            &probe,
            &UnfilledOrderForbiddenCanceller,
            &OperatorAlertForbiddenSink,
            &HotSwapDemotionEventSinkSpy::default(),
            &lock,
            OBSERVED_AT_SECONDS + 600,
        )
        .expect_err("a failed engage must not leave the retry free to promote");

    assert_eq!(second.error_type, "HotSwapDemotionPending");
    assert_eq!(
        probe.calls.get(),
        0,
        "the block precedes the demotion attempt"
    );
}

#[test]
fn resv_4_the_operator_page_states_what_was_actually_done_about_the_cancel() {
    // The page is a RECOVERY instruction. The timeout branch cancels the unfilled order and
    // the probe-inconsistency branch deliberately does not, so a body that describes a cancel
    // in both cases sends the operator after an order that is still live and unmentioned.
    // `(found by /codex:adversarial-review, SRS-RESV-004 r3 [medium])`
    let orchestrator = StrategyOrchestrator;

    // Timeout branch: the cancel ran, and its REAL outcome rides on the alert.
    let alerts = OperatorAlertSinkSpy::default();
    let lock = DemotionPendingLockSpy::default();
    let _ = orchestrator.resolve_demotion(
        demotion("live-a", "paper-b", HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &UnfilledOrderCancellerSpy::default(),
        &alerts,
        &HotSwapDemotionEventSinkSpy::default(),
        &lock,
        OBSERVED_AT_SECONDS,
    );
    assert_eq!(
        alerts.alerts.borrow()[0].liquidation_cancel,
        SideEffectOutcome::Succeeded
    );

    // A FAILED cancel is carried as failed — the case where a live order most likely remains.
    let failing_alerts = OperatorAlertSinkSpy::default();
    let _ = orchestrator.resolve_demotion(
        demotion("live-a", "paper-b", HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &UnfilledOrderFailingCanceller::default(),
        &failing_alerts,
        &HotSwapDemotionEventSinkSpy::default(),
        &DemotionPendingLockSpy::default(),
        OBSERVED_AT_SECONDS,
    );
    assert!(failing_alerts.alerts.borrow()[0]
        .liquidation_cancel
        .is_failed());

    // Probe-inconsistency branch: nothing was cancelled, and the page must say NOT_ATTEMPTED.
    let inconsistent_alerts = OperatorAlertSinkSpy::default();
    let _ = orchestrator.resolve_demotion(
        demotion("live-a", "paper-b", HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &HotSwapLiquidationProbeSpy::timed_out(12, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
        &UnfilledOrderForbiddenCanceller,
        &inconsistent_alerts,
        &HotSwapDemotionEventSinkSpy::default(),
        &DemotionPendingLockSpy::default(),
        OBSERVED_AT_SECONDS,
    );
    assert_eq!(
        inconsistent_alerts.alerts.borrow()[0].liquidation_cancel,
        SideEffectOutcome::NotAttempted,
        "a branch that cancels nothing must not page as though it did"
    );
}

#[test]
fn resv_4_every_blocked_branch_engages_the_lockout_before_any_fallible_side_effect() {
    // The class, checked behaviourally on BOTH blocked branches. A branch that pages (or
    // cancels) before engaging leaves a window in which a crash loses the block entirely, and
    // the next attempt reads an empty store as "nothing is pending". The timeout arm was fixed
    // for this first and the probe-inconsistency branch was not — so this test enumerates the
    // branches rather than naming one. `(found by /codex:adversarial-review, SRS-RESV-004 r5)`
    use std::rc::Rc;

    type CallLog = Rc<RefCell<Vec<&'static str>>>;

    struct OrderedLock(CallLog);
    impl DemotionPendingLock for OrderedLock {
        fn state(&self) -> DemotionPendingState {
            DemotionPendingState::Clear
        }
        fn engage(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
            self.0.borrow_mut().push("engage");
            Ok(())
        }
        fn amend(&self, _record: DemotionPendingRecord) -> Result<(), HotSwapSideEffectError> {
            self.0.borrow_mut().push("amend");
            Ok(())
        }
    }

    struct OrderedAlerts(CallLog);
    impl OperatorAlertSink for OrderedAlerts {
        fn dispatch(&self, _event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError> {
            self.0.borrow_mut().push("alert");
            Ok(())
        }
    }

    struct OrderedCanceller(CallLog);
    impl UnfilledOrderCanceller for OrderedCanceller {
        fn cancel_unfilled_liquidation_orders(
            &self,
            _request: &HotSwapDemotionRequest,
        ) -> Result<(), HotSwapSideEffectError> {
            self.0.borrow_mut().push("cancel");
            Ok(())
        }
    }

    let orchestrator = StrategyOrchestrator;
    // (probe outcome, branch name) — both blocked branches of the gate.
    for (probe, branch) in [
        (
            HotSwapLiquidationProbeSpy::timed_out(72, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            "liquidation timeout",
        ),
        (
            HotSwapLiquidationProbeSpy::timed_out(12, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            "probe inconsistency",
        ),
    ] {
        let log: CallLog = Rc::new(RefCell::new(Vec::new()));
        let _ = orchestrator.resolve_demotion(
            demotion("live-a", "paper-b", HOT_SWAP_DEMOTION_TIMEOUT_SECONDS),
            &probe,
            &OrderedCanceller(Rc::clone(&log)),
            &OrderedAlerts(Rc::clone(&log)),
            &HotSwapDemotionEventSinkSpy::default(),
            &OrderedLock(Rc::clone(&log)),
            OBSERVED_AT_SECONDS,
        );

        let calls = log.borrow().clone();
        let engage_at = calls
            .iter()
            .position(|c| *c == "engage")
            .unwrap_or_else(|| panic!("{branch}: the block was never made durable ({calls:?})"));
        for fallible in ["cancel", "alert"] {
            if let Some(at) = calls.iter().position(|c| *c == fallible) {
                assert!(
                    engage_at < at,
                    "{branch}: `{fallible}` ran before the lockout was engaged ({calls:?})"
                );
            }
        }
        // ...and the record is completed afterwards, so it describes what actually happened.
        assert!(
            calls.iter().any(|c| *c == "amend"),
            "{branch}: the provisional record was never amended ({calls:?})"
        );
    }
}
