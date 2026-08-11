//! SRS-RESV-006 / SyRS SYS-49e — the cool-down GATE on both SRS-RESV-003 entry points.
//!
//! L7 domain (safety). The acceptance criterion has three clauses and this file covers the
//! first two; the third (the window starts at the completion timestamp) is pinned in
//! `resv_6_cooldown_store.rs`.
//!
//!   1. **Automatic triggers are IGNORED during the window.** Not "fired and discarded" —
//!      ignored. Every suppression test therefore asserts the log sink holds ZERO records:
//!      a gate that fired the triggers and dropped the proposals would satisfy a
//!      `selected.is_none()` assertion while writing a swap trigger to the audit trail that
//!      never happened.
//!   2. **A manual swap during cool-down requires a confirmation warning.** Requires — not
//!      forbids. SRS-RESV-003 guarantees manual promotion is always available, so every
//!      refusal here is paired with its acknowledged twin proving the same call fires.
//!
//! Both clauses are driven by ONE predicate (`CooldownState::proven_clear`), so the paired
//! tests below are what stop a change that suppresses automatically while silently dropping
//! the manual warning.

use atp_orchestrator::cooldown::{
    CooldownPeriodDays, CooldownState, ManualCooldownAcknowledgement, SwapCompletion,
};
use atp_orchestrator::{
    HotSwapSideEffectError, HotSwapTriggerLog, LiveStrategyProbe, ManualPromotionError,
    ReservoirRankingSource, StrategyOrchestrator,
};
use atp_types::{
    DrawdownDemotionTrigger, DrawdownThresholdBps, HotSwapTriggerConfig, HotSwapTriggerEvent,
    HotSwapTriggerKind, LiveStrategyState, RankedStrategy, RankingPromotionTrigger,
    ReservoirRankingSnapshot, StrategyId,
};
use std::cell::RefCell;

const COMPLETED_AT: u64 = 1_715_000_000;
const SEVEN_DAYS: u64 = 7 * 86_400;
/// Inside the window: one hour after the swap completed.
const DURING_COOLDOWN: u64 = COMPLETED_AT + 3_600;
/// Outside it: one second past the seven-day boundary.
const AFTER_COOLDOWN: u64 = COMPLETED_AT + SEVEN_DAYS + 1;

fn completion() -> SwapCompletion {
    SwapCompletion {
        completed_at_seconds: COMPLETED_AT,
        demoted_strategy_id: StrategyId::new("was-live"),
        promoted_strategy_id: StrategyId::new("now-live"),
    }
}

fn window_at(now: u64) -> CooldownState {
    CooldownState::classify(Some(&completion()), CooldownPeriodDays::default(), now)
}

/// Every automatic trigger armed, and every condition met — so anything that does NOT fire
/// in the tests below is the cool-down doing it, not a missing precondition.
fn all_triggers_armed() -> HotSwapTriggerConfig {
    HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: DrawdownThresholdBps::new(1_000).unwrap(),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    }
}

struct LiveProbe;

impl LiveStrategyProbe for LiveProbe {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        Ok(Some(LiveStrategyState {
            strategy_id: StrategyId::new("live-a"),
            // Well past the 1_000 bps threshold armed above.
            drawdown_bps: 9_000,
        }))
    }
}

struct Ranking;

impl ReservoirRankingSource for Ranking {
    fn snapshot(&self) -> Result<ReservoirRankingSnapshot, HotSwapSideEffectError> {
        Ok(ReservoirRankingSnapshot {
            evaluation_window_days: 30,
            ranked: vec![RankedStrategy {
                strategy_id: StrategyId::new("cand-b"),
                rank: 1,
                risk_adjusted_score: 2.5,
                momentum_score: 1.9,
            }],
        })
    }
}

/// Records every event it is handed. The COUNT is the evidence: "ignored" means nothing was
/// written, and only a spy can tell that apart from "fired, then not selected".
#[derive(Default)]
struct LogSpy {
    events: RefCell<Vec<HotSwapTriggerEvent>>,
}

impl LogSpy {
    fn count(&self) -> usize {
        self.events.borrow().len()
    }
}

impl HotSwapTriggerLog for LogSpy {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

// --------------------------------------------------------------------------- //
// Clause 1 — automatic triggers are IGNORED during the window
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_an_active_cooldown_suppresses_every_automatic_trigger_and_logs_nothing() {
    let log = LogSpy::default();
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(DURING_COOLDOWN),
        &LiveProbe,
        &Ranking,
        &log,
        DURING_COOLDOWN,
    );

    assert!(
        evaluation.fired.is_empty(),
        "nothing may fire in a cool-down"
    );
    assert!(evaluation.selected.is_none());
    assert_eq!(
        log.count(),
        0,
        "SYS-49e says the triggers are IGNORED: a suppressed pass must write no audit \
         record, or the trail claims a swap trigger occurred that never did"
    );
    assert_eq!(evaluation.cooldown.as_str(), "ACTIVE");
}

#[test]
fn resv_6_an_expired_cooldown_lets_the_automatic_triggers_fire_again() {
    // The non-vacuity control for the test above. Without it, a wholly broken evaluator that
    // never fires anything would pass the suppression assertions perfectly.
    let log = LogSpy::default();
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(AFTER_COOLDOWN),
        &LiveProbe,
        &Ranking,
        &log,
        AFTER_COOLDOWN,
    );

    assert!(
        !evaluation.fired.is_empty(),
        "an expired window must not keep suppressing"
    );
    assert!(evaluation.selected.is_some());
    assert_eq!(log.count(), evaluation.fired.len());
    assert_eq!(evaluation.cooldown.as_str(), "EXPIRED");
}

#[test]
fn resv_6_no_swap_history_does_not_suppress() {
    // A fresh install has never swapped. If this suppressed, SRS-RESV-003's automatic
    // triggers would be dead from the first boot with nothing to explain why.
    let log = LogSpy::default();
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &CooldownState::NeverSwapped,
        &LiveProbe,
        &Ranking,
        &log,
        COMPLETED_AT,
    );
    assert!(!evaluation.fired.is_empty());
    assert_eq!(evaluation.cooldown.as_str(), "NEVER_SWAPPED");
}

#[test]
fn resv_6_an_unknown_cooldown_suppresses_and_surfaces_a_degraded_input() {
    // A window this build cannot read is a FAILED pass, not a clean "nothing fired". The
    // degraded_inputs entry is what makes the operator CLI exit nonzero on it.
    let log = LogSpy::default();
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &CooldownState::unknown("cool-down state file is corrupt"),
        &LiveProbe,
        &Ranking,
        &log,
        DURING_COOLDOWN,
    );

    assert!(evaluation.fired.is_empty());
    assert_eq!(log.count(), 0);
    assert_eq!(evaluation.cooldown.as_str(), "UNKNOWN");
    assert_eq!(
        evaluation.degraded_inputs.len(),
        1,
        "an unreadable window must be surfaced as a degraded input, not swallowed"
    );
    assert!(evaluation.degraded_inputs[0].contains("corrupt"));
}

#[test]
fn resv_6_an_active_cooldown_is_not_reported_as_a_degraded_input() {
    // The counterpart: a WORKING cool-down is healthy. Marking it degraded would make every
    // legitimate suppression exit the operator CLI nonzero and train operators to ignore it.
    let log = LogSpy::default();
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(DURING_COOLDOWN),
        &LiveProbe,
        &Ranking,
        &log,
        DURING_COOLDOWN,
    );
    assert!(evaluation.degraded_inputs.is_empty());
}

#[test]
fn resv_6_suppression_is_distinguishable_from_having_no_candidates() {
    // Both produce `selected: None` and an empty `fired`. Without the cooldown field on the
    // evaluation they would be byte-identical, and an operator would watch automatic
    // triggers do nothing for a week with no surface saying which of the two it was
    // (CLAUDE.md rule 3).
    let log = LogSpy::default();
    let suppressed = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(DURING_COOLDOWN),
        &LiveProbe,
        &Ranking,
        &log,
        DURING_COOLDOWN,
    );

    struct EmptyRanking;
    impl ReservoirRankingSource for EmptyRanking {
        fn snapshot(&self) -> Result<ReservoirRankingSnapshot, HotSwapSideEffectError> {
            Ok(ReservoirRankingSnapshot {
                evaluation_window_days: 30,
                ranked: Vec::new(),
            })
        }
    }
    let no_candidates = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(AFTER_COOLDOWN),
        &LiveProbe,
        &EmptyRanking,
        &log,
        AFTER_COOLDOWN,
    );

    assert!(suppressed.selected.is_none() && no_candidates.selected.is_none());
    assert_ne!(
        suppressed.cooldown.as_str(),
        no_candidates.cooldown.as_str(),
        "the two no-swap outcomes must be tellable apart"
    );
}

#[test]
fn resv_6_a_degraded_probe_does_not_claim_the_window_was_clear() {
    // The early exits carry the window they were given rather than defaulting to
    // NeverSwapped. A pass that never read the ports must not assert "no cool-down is in
    // effect" — that is a fact about a window it did not establish.
    let log = LogSpy::default();
    struct DegradedProbe;
    impl LiveStrategyProbe for DegradedProbe {
        fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
            Err(HotSwapSideEffectError::new("registry unavailable"))
        }
    }
    let evaluation = StrategyOrchestrator.evaluate_automatic_triggers(
        &all_triggers_armed(),
        &window_at(AFTER_COOLDOWN),
        &DegradedProbe,
        &Ranking,
        &log,
        AFTER_COOLDOWN,
    );
    assert_eq!(evaluation.cooldown.as_str(), "EXPIRED");
    assert_eq!(evaluation.degraded_inputs.len(), 1);
}

// --------------------------------------------------------------------------- //
// Clause 2 — a manual swap during cool-down requires a confirmation warning
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_manual_during_cooldown_without_acknowledgement_is_refused_and_logs_nothing() {
    let log = LogSpy::default();
    let outcome = StrategyOrchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("cand-b"),
        &window_at(DURING_COOLDOWN),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &log,
        DURING_COOLDOWN,
    );

    match outcome {
        Err(ManualPromotionError::CooldownConfirmationRequired { state, warning }) => {
            assert_eq!(state.as_str(), "ACTIVE");
            assert!(warning.contains("SYS-49e"));
        }
        other => panic!("expected a cool-down confirmation refusal, got {other:?}"),
    }
    assert_eq!(
        log.count(),
        0,
        "a refused manual swap proposed nothing, so it must log nothing"
    );
}

#[test]
fn resv_6_manual_during_cooldown_with_acknowledgement_fires_and_logs() {
    // The paired twin, and the SRS-RESV-003 invariant: manual promotion is ALWAYS
    // available. The cool-down adds a confirmation, never a block — so the identical call
    // one argument different must succeed.
    let log = LogSpy::default();
    let proposal = StrategyOrchestrator
        .request_manual_promotion(
            StrategyId::new("live-a"),
            StrategyId::new("cand-b"),
            &window_at(DURING_COOLDOWN),
            ManualCooldownAcknowledgement::Acknowledged,
            &log,
            DURING_COOLDOWN,
        )
        .expect("an acknowledged manual swap must fire even inside a cool-down");

    assert_eq!(proposal.kind, HotSwapTriggerKind::ManualPromotion);
    assert_eq!(log.count(), 1, "an acknowledged override is still audited");
}

#[test]
fn resv_6_manual_outside_a_cooldown_needs_no_acknowledgement() {
    // SRS-RESV-003's behaviour, unchanged: no window, no confirmation.
    for window in [CooldownState::NeverSwapped, window_at(AFTER_COOLDOWN)] {
        let log = LogSpy::default();
        let proposal = StrategyOrchestrator
            .request_manual_promotion(
                StrategyId::new("live-a"),
                StrategyId::new("cand-b"),
                &window,
                ManualCooldownAcknowledgement::NotAcknowledged,
                &log,
                AFTER_COOLDOWN,
            )
            .expect("a clear window must not demand a confirmation");
        assert_eq!(proposal.kind, HotSwapTriggerKind::ManualPromotion);
        assert_eq!(log.count(), 1);
    }
}

#[test]
fn resv_6_manual_under_an_unknown_window_also_requires_acknowledgement() {
    // The same fail-closed direction as the automatic arm: a build that cannot prove the
    // window is clear must not let a manual swap through as though it had.
    let log = LogSpy::default();
    let outcome = StrategyOrchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("cand-b"),
        &CooldownState::unknown("window unreadable"),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &log,
        DURING_COOLDOWN,
    );
    assert!(matches!(
        outcome,
        Err(ManualPromotionError::CooldownConfirmationRequired { .. })
    ));
    assert_eq!(log.count(), 0);
}

#[test]
fn resv_6_a_self_swap_is_refused_before_the_cooldown_warning() {
    // Ordering: asking an operator to acknowledge a safety window for a request that is
    // malformed anyway teaches them to confirm past the warning — and confirming would
    // produce the same refusal one step later regardless.
    let log = LogSpy::default();
    let outcome = StrategyOrchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("live-a"),
        &window_at(DURING_COOLDOWN),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &log,
        DURING_COOLDOWN,
    );
    assert!(
        matches!(outcome, Err(ManualPromotionError::SameStrategy { .. })),
        "the malformed request must be refused first, got {outcome:?}"
    );
    assert_eq!(log.count(), 0);
}

#[test]
fn resv_6_the_warning_names_the_expiry_the_operator_is_overriding() {
    // The AC asks for a confirmation WARNING — content, not a boolean. An operator
    // overriding a safety window has to be told which window, and until when.
    let log = LogSpy::default();
    let outcome = StrategyOrchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("cand-b"),
        &window_at(DURING_COOLDOWN),
        ManualCooldownAcknowledgement::NotAcknowledged,
        &log,
        DURING_COOLDOWN,
    );
    let Err(ManualPromotionError::CooldownConfirmationRequired { warning, .. }) = outcome else {
        panic!("expected a confirmation refusal");
    };
    assert!(
        warning.contains(&(COMPLETED_AT + SEVEN_DAYS).to_string()),
        "the warning must name the expiry: {warning}"
    );
}

// --------------------------------------------------------------------------- //
// The two arms share ONE predicate
// --------------------------------------------------------------------------- //

#[test]
fn resv_6_suppression_and_confirmation_agree_on_every_window() {
    // The property that keeps the two consequences from drifting: automatic suppresses
    // exactly when manual demands a confirmation. A change that fixed one arm and forgot
    // the other fails here rather than shipping half a cool-down.
    for window in [
        CooldownState::NeverSwapped,
        window_at(DURING_COOLDOWN),
        window_at(AFTER_COOLDOWN),
        CooldownState::unknown("unreadable"),
    ] {
        let auto_log = LogSpy::default();
        let suppressed = StrategyOrchestrator
            .evaluate_automatic_triggers(
                &all_triggers_armed(),
                &window,
                &LiveProbe,
                &Ranking,
                &auto_log,
                DURING_COOLDOWN,
            )
            .fired
            .is_empty();

        let manual_log = LogSpy::default();
        let confirmation_required = matches!(
            StrategyOrchestrator.request_manual_promotion(
                StrategyId::new("live-a"),
                StrategyId::new("cand-b"),
                &window,
                ManualCooldownAcknowledgement::NotAcknowledged,
                &manual_log,
                DURING_COOLDOWN,
            ),
            Err(ManualPromotionError::CooldownConfirmationRequired { .. })
        );

        assert_eq!(
            suppressed, confirmation_required,
            "the arms disagreed for {window:?}: suppressed={suppressed}, \
             confirmation_required={confirmation_required}"
        );
    }
}
