//! SRS-RESV-003 / SyRS SYS-49a / StRS SN-1.25 / SN-1.30 — the Hot-Swap trigger
//! DECISION + CONFIGURATION + LOGGING layer. A Hot-Swap may be triggered by
//! manual operator selection (always available) or by three AUTOMATIC triggers
//! — drawdown-demotion, top-ranked promotion, highest-momentum promotion — each
//! enable/disable-able per type and DEFAULTING TO DISABLED. Every fired trigger
//! is logged. The trigger layer proposes + logs; it does NOT execute the swap
//! (that is the SRS-RESV-004 `resolve_demotion` gate, which consumes the
//! `HotSwapDemotionRequest` this layer produces).
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * Default posture: a default `HotSwapTriggerConfig` fires NOTHING and logs
//!     NOTHING even when every input condition is met (the "automatic triggers
//!     default to disabled" safety invariant). The forbidden log sink panics if
//!     touched, proving no trigger fired.
//!   * Each automatic trigger, when enabled and its condition met, fires exactly
//!     once with the correct candidate/rationale and is logged exactly once;
//!     when its condition is not met (or the candidate would be the live
//!     strategy, or there is no live strategy, or the ranking is empty /
//!     non-finite) it fails closed and does not fire.
//!   * Manual promotion is always available: it fires + logs regardless of the
//!     automatic-trigger config.
//!   * "All swap triggers are logged": with every trigger enabled and met, all
//!     fire in a fixed priority order (drawdown-demotion first) and the log
//!     record count equals the fired count.
//!   * Logging is LOAD-BEARING on the actionable path: a fired trigger whose
//!     required audit-log record is rejected is surfaced in `unlogged` and is
//!     never `selected`; a rejected manual record returns `Err`; but the
//!     evaluation never panics or aborts.
//!   * A degraded input port (`LiveStrategyProbe` / `ReservoirRankingSource`
//!     `Err`) fails closed (no swap) while surfacing the reason in
//!     `degraded_inputs` — distinct from a healthy empty state.
//!   * The selected proposal maps cleanly to a `HotSwapDemotionRequest` for the
//!     SRS-RESV-004 gate, carrying the shared default timeout.
//!
//! Primary structural enforcement lives in `tools/hot_swap_trigger_check.py`
//! (the contract's default-disabled + log-on-every-fire guard); this Rust test
//! anchors the post-conditions at the behavioral layer.

use atp_orchestrator::{
    HotSwapSideEffectError, HotSwapTriggerLog, LiveStrategyProbe, ReservoirRankingSource,
    StrategyOrchestrator,
};
use atp_types::{
    DrawdownDemotionTrigger, DrawdownThresholdBps, HotSwapTriggerConfig, HotSwapTriggerEvent,
    HotSwapTriggerKind, LiveStrategyState, RankedStrategy, RankingPromotionTrigger,
    ReservoirRankingSnapshot, StrategyId, TriggerRationale, UnloggedHotSwapTrigger,
    HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
};
use std::cell::{Cell, RefCell};

const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

// --- injected input stubs (mirror the RESV-004 spy/forbidden fake-port style) ---

struct LiveStrategyProbeStub {
    state: Option<LiveStrategyState>,
}

impl LiveStrategyProbeStub {
    fn live(strategy_id: &str, drawdown_bps: u32) -> Self {
        Self {
            state: Some(LiveStrategyState {
                strategy_id: StrategyId::new(strategy_id),
                drawdown_bps,
            }),
        }
    }

    fn none() -> Self {
        Self { state: None }
    }
}

impl LiveStrategyProbe for LiveStrategyProbeStub {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        Ok(self.state.clone())
    }
}

/// Probe that reports it could not read live state — the degraded case.
struct DegradedLiveProbe;

impl LiveStrategyProbe for DegradedLiveProbe {
    fn current_live(&self) -> Result<Option<LiveStrategyState>, HotSwapSideEffectError> {
        Err(HotSwapSideEffectError::new(
            "live-state registry unavailable",
        ))
    }
}

struct ReservoirRankingSourceStub {
    snapshot: ReservoirRankingSnapshot,
}

impl ReservoirRankingSourceStub {
    fn new(snapshot: ReservoirRankingSnapshot) -> Self {
        Self { snapshot }
    }
}

impl ReservoirRankingSource for ReservoirRankingSourceStub {
    fn snapshot(&self) -> Result<ReservoirRankingSnapshot, HotSwapSideEffectError> {
        Ok(self.snapshot.clone())
    }
}

/// Ranking source that reports it could not read the ranking — the degraded case.
struct DegradedRankingSource;

impl ReservoirRankingSource for DegradedRankingSource {
    fn snapshot(&self) -> Result<ReservoirRankingSnapshot, HotSwapSideEffectError> {
        Err(HotSwapSideEffectError::new("ranking source unavailable"))
    }
}

/// Log sink that records every trigger event and reports success.
#[derive(Default)]
struct HotSwapTriggerLogSpy {
    events: RefCell<Vec<HotSwapTriggerEvent>>,
}

impl HotSwapTriggerLog for HotSwapTriggerLogSpy {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// Log sink that records the event but reports a REJECTION — models a sink that
/// cannot accept the write. The evaluator must not panic, must keep the fired
/// trigger out of `selected` (fail closed), and must surface it in `unlogged`.
#[derive(Default)]
struct HotSwapTriggerLogFailingSink {
    events: RefCell<Vec<HotSwapTriggerEvent>>,
}

impl HotSwapTriggerLog for HotSwapTriggerLogFailingSink {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        Err(HotSwapSideEffectError::new("system log store unwritable"))
    }
}

/// Log sink that ACCEPTS the first record and REJECTS every later one — models a
/// sink that degrades mid-pass (e.g. the log store fills after the first write).
/// Exercises the atomic "all swap triggers are logged" rule: a later rejection
/// must block the whole pass even though the first (highest-priority) trigger
/// logged cleanly.
#[derive(Default)]
struct HotSwapTriggerLogRejectAfterFirst {
    seen: Cell<u32>,
    events: RefCell<Vec<HotSwapTriggerEvent>>,
}

impl HotSwapTriggerLog for HotSwapTriggerLogRejectAfterFirst {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        self.events.borrow_mut().push(event);
        let seen = self.seen.get();
        self.seen.set(seen + 1);
        if seen == 0 {
            Ok(())
        } else {
            Err(HotSwapSideEffectError::new(
                "log store degraded after first write",
            ))
        }
    }
}

/// Log sink that panics if consulted. Used by the default-posture / no-fire
/// tests to prove no trigger was logged (and therefore none fired).
struct HotSwapTriggerLogForbiddenSink;

impl HotSwapTriggerLog for HotSwapTriggerLogForbiddenSink {
    fn record(&self, _event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError> {
        panic!("SRS-RESV-003: a disabled / no-fire evaluation must not log any trigger");
    }
}

// --- helpers ---

fn ranked(strategy_id: &str, rank: u32, score: f64, momentum: f64) -> RankedStrategy {
    RankedStrategy {
        strategy_id: StrategyId::new(strategy_id),
        rank,
        risk_adjusted_score: score,
        momentum_score: momentum,
    }
}

fn snapshot(ranked: Vec<RankedStrategy>) -> ReservoirRankingSnapshot {
    ReservoirRankingSnapshot {
        evaluation_window_days: 30,
        ranked,
    }
}

fn threshold(bps: u32) -> DrawdownThresholdBps {
    DrawdownThresholdBps::new(bps).expect("valid threshold")
}

// --- tests ---

#[test]
fn resv_3_default_config_fires_nothing_even_when_conditions_met() {
    // The core safety invariant: automatic triggers default to disabled, so a
    // default config produces an empty evaluation even with a deep drawdown and
    // an excellent candidate present. The forbidden sink proves nothing logged.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig::default();
    let live = LiveStrategyProbeStub::live("live-a", 9_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-top", 1, 2.5, 1.9)]));
    let log = HotSwapTriggerLogForbiddenSink;

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert!(
        evaluation.fired.is_empty(),
        "default config must fire no automatic trigger"
    );
    assert!(evaluation.selected.is_none());
}

#[test]
fn resv_3_drawdown_demotion_fires_and_logs_when_breached() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_500),
        },
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 2_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-b", 1, 2.5, 1.9),
        ranked("cand-c", 2, 1.0, 0.5),
    ]));
    let log = HotSwapTriggerLogSpy::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 1);
    let proposal = &evaluation.fired[0];
    assert_eq!(proposal.kind, HotSwapTriggerKind::DrawdownDemotion);
    assert_eq!(proposal.demoting_strategy_id.as_str(), "live-a");
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-b");
    assert_eq!(
        proposal.rationale,
        TriggerRationale::DrawdownBreached {
            observed_bps: 2_000,
            threshold_bps: 1_500,
        }
    );
    assert_eq!(evaluation.selected.as_ref(), Some(proposal));

    let events = log.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind, HotSwapTriggerKind::DrawdownDemotion);
    assert_eq!(events[0].candidate_strategy_id.as_str(), "cand-b");
}

#[test]
fn resv_3_drawdown_demotion_does_not_fire_below_threshold() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_500),
        },
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 1_499);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 1.9)]));
    let log = HotSwapTriggerLogForbiddenSink;

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert!(evaluation.fired.is_empty());
    assert!(evaluation.selected.is_none());
}

#[test]
fn resv_3_top_ranked_promotion_fires_and_logs() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-b", 1, 2.5, 0.4),
        ranked("cand-c", 2, 1.0, 1.9),
    ]));
    let log = HotSwapTriggerLogSpy::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 1);
    let proposal = &evaluation.fired[0];
    assert_eq!(proposal.kind, HotSwapTriggerKind::TopRankedPromotion);
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-b");
    assert_eq!(
        proposal.rationale,
        TriggerRationale::TopRanked {
            rank: 1,
            score: 2.5,
        }
    );
    assert_eq!(log.events.borrow().len(), 1);
}

#[test]
fn resv_3_highest_momentum_promotion_fires_and_logs() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-b", 1, 2.5, 0.4),
        ranked("cand-c", 2, 1.0, 1.9),
    ]));
    let log = HotSwapTriggerLogSpy::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 1);
    let proposal = &evaluation.fired[0];
    assert_eq!(proposal.kind, HotSwapTriggerKind::HighestMomentumPromotion);
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-c");
    assert_eq!(
        proposal.rationale,
        TriggerRationale::HighestMomentum {
            momentum_score: 1.9
        }
    );
    assert_eq!(log.events.borrow().len(), 1);
}

#[test]
fn resv_3_manual_promotion_always_fires_and_logs_even_when_all_disabled() {
    let orchestrator = StrategyOrchestrator;
    let log = HotSwapTriggerLogSpy::default();

    let proposal = orchestrator
        .request_manual_promotion(
            StrategyId::new("live-a"),
            StrategyId::new("cand-b"),
            &log,
            OBSERVED_AT_SECONDS,
        )
        .expect("manual promotion is logged with a healthy sink");

    assert_eq!(proposal.kind, HotSwapTriggerKind::ManualPromotion);
    assert_eq!(proposal.demoting_strategy_id.as_str(), "live-a");
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-b");
    assert_eq!(proposal.rationale, TriggerRationale::ManualSelection);

    let events = log.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind, HotSwapTriggerKind::ManualPromotion);
}

#[test]
fn resv_3_manual_promotion_fails_closed_when_log_rejected() {
    // A manual trigger fired but its required audit-log record was rejected — it
    // must come back as `Err(UnloggedHotSwapTrigger)`, carrying the proposal, so
    // the operator/caller never acts on an unlogged manual swap.
    let orchestrator = StrategyOrchestrator;
    let log = HotSwapTriggerLogFailingSink::default();

    let outcome = orchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("cand-b"),
        &log,
        OBSERVED_AT_SECONDS,
    );

    let Err(UnloggedHotSwapTrigger {
        proposal,
        rejection_reason,
    }) = outcome
    else {
        panic!("a rejected log must fail closed to Err(UnloggedHotSwapTrigger)");
    };
    assert_eq!(proposal.kind, HotSwapTriggerKind::ManualPromotion);
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-b");
    // The sink's rejection reason is carried so a caller can surface WHY.
    assert_eq!(rejection_reason, "system log store unwritable");
    // The record was still attempted (observable), it was just rejected.
    assert_eq!(log.events.borrow().len(), 1);
}

#[test]
fn resv_3_all_enabled_all_conditions_met_fire_in_priority_order_and_each_logged() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    };
    let live = LiveStrategyProbeStub::live("live-a", 5_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-top", 1, 2.5, 0.4),
        ranked("cand-mom", 2, 1.0, 1.9),
    ]));
    let log = HotSwapTriggerLogSpy::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 3);
    let kinds: Vec<HotSwapTriggerKind> = evaluation.fired.iter().map(|p| p.kind).collect();
    assert_eq!(
        kinds,
        vec![
            HotSwapTriggerKind::DrawdownDemotion,
            HotSwapTriggerKind::TopRankedPromotion,
            HotSwapTriggerKind::HighestMomentumPromotion,
        ]
    );
    // selected = the highest-priority trigger (drawdown-demotion, the risk control)
    assert_eq!(
        evaluation.selected.as_ref().map(|proposal| proposal.kind),
        Some(HotSwapTriggerKind::DrawdownDemotion)
    );
    // "all swap triggers are logged": exactly one log record per fired trigger,
    // in the same order, and none rejected (so nothing is `unlogged`).
    let logged_kinds: Vec<HotSwapTriggerKind> =
        log.events.borrow().iter().map(|event| event.kind).collect();
    assert_eq!(logged_kinds, kinds);
    assert_eq!(log.events.borrow().len(), evaluation.fired.len());
    assert!(evaluation.unlogged.is_empty());
}

#[test]
fn resv_3_failing_log_sink_fails_closed_not_selected() {
    // A fired trigger whose required audit-log record is REJECTED must not become
    // actionable: it fires (recorded in `fired`), is surfaced in `unlogged`, and
    // `selected` is None — SRS-RESV-004 is never handed an unlogged swap trigger.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 0.4)]));
    let log = HotSwapTriggerLogFailingSink::default();

    // Must not panic despite the sink returning Err.
    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 1);
    assert_eq!(evaluation.unlogged.len(), 1);
    assert_eq!(
        evaluation.unlogged[0].proposal.kind,
        HotSwapTriggerKind::TopRankedPromotion
    );
    // The sink's rejection reason travels through the automatic path too.
    assert_eq!(
        evaluation.unlogged[0].rejection_reason,
        "system log store unwritable"
    );
    assert!(
        evaluation.selected.is_none(),
        "an unlogged trigger must never be selected (fail closed)"
    );
    // The record was still attempted (observable), just rejected.
    assert_eq!(log.events.borrow().len(), 1);
}

#[test]
fn resv_3_all_fired_but_top_priority_unlogged_selects_nothing() {
    // Every automatic trigger fires and every record is rejected: all three land
    // in `unlogged`, and because the highest-priority (drawdown) trigger could not
    // be logged, `selected` is None — never silently substitute a lower-priority
    // logged trigger for an unlogged higher-priority one.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    };
    let live = LiveStrategyProbeStub::live("live-a", 5_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-top", 1, 2.5, 0.4),
        ranked("cand-mom", 2, 1.0, 1.9),
    ]));
    let log = HotSwapTriggerLogFailingSink::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 3);
    assert_eq!(evaluation.unlogged.len(), 3);
    assert!(evaluation.selected.is_none());
}

#[test]
fn resv_3_partial_log_rejection_fails_whole_pass_closed() {
    // The highest-priority trigger (drawdown) logs OK, but a LATER fired trigger's
    // record is rejected. "All swap triggers are logged" is atomic for the pass:
    // the known rejected record blocks EVERY swap from this pass, so `selected` is
    // None even though the top-priority trigger itself logged cleanly.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    };
    let live = LiveStrategyProbeStub::live("live-a", 5_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("cand-top", 1, 2.5, 0.4),
        ranked("cand-mom", 2, 1.0, 1.9),
    ]));
    let log = HotSwapTriggerLogRejectAfterFirst::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 3);
    // The first record (drawdown) was accepted; the two later ones were rejected.
    assert_eq!(evaluation.unlogged.len(), 2);
    assert!(
        evaluation.selected.is_none(),
        "a pass with ANY rejected trigger log must select nothing (atomic all-logged)"
    );
}

#[test]
fn resv_3_ranking_non_finite_and_empty_fail_closed_no_fire() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    };
    let live = LiveStrategyProbeStub::live("live-a", 5_000);

    // Empty ranking → no candidate → no automatic trigger fires.
    let empty_ranking = ReservoirRankingSourceStub::new(snapshot(vec![]));
    let empty_eval = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &empty_ranking,
        &HotSwapTriggerLogForbiddenSink,
        OBSERVED_AT_SECONDS,
    );
    assert!(empty_eval.fired.is_empty());

    // Non-finite scores → fail-closed accessors → no fire, no fabricated pick.
    let nan_ranking =
        ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, f64::NAN, f64::NAN)]));
    let nan_eval = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &nan_ranking,
        &HotSwapTriggerLogForbiddenSink,
        OBSERVED_AT_SECONDS,
    );
    assert!(nan_eval.fired.is_empty());
}

#[test]
fn resv_3_promotion_skips_when_top_candidate_is_already_live() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);
    // The top-ranked strategy IS the currently-live one — swapping a strategy
    // with itself is nonsensical, so no trigger fires.
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![
        ranked("live-a", 1, 2.5, 0.4),
        ranked("cand-b", 2, 1.0, 0.3),
    ]));
    let log = HotSwapTriggerLogForbiddenSink;

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert!(
        evaluation.fired.is_empty(),
        "must not propose swapping a strategy with itself"
    );
}

#[test]
fn resv_3_no_live_strategy_fires_nothing() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::none();
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 0.4)]));
    let log = HotSwapTriggerLogForbiddenSink;

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert!(evaluation.fired.is_empty());
    assert!(evaluation.selected.is_none());
    // A healthy Ok(None) is NOT a degradation.
    assert!(evaluation.degraded_inputs.is_empty());
}

#[test]
fn resv_3_degraded_live_probe_fails_closed_and_surfaces_reason() {
    // A live-strategy probe that cannot read state (Err) must fail closed (no
    // swap) AND surface the reason in `degraded_inputs` — distinguishable from a
    // healthy "no live strategy", not silently collapsed.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        highest_momentum_promotion: RankingPromotionTrigger::Enabled,
    };
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 1.9)]));

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &DegradedLiveProbe,
        &ranking,
        &HotSwapTriggerLogForbiddenSink,
        OBSERVED_AT_SECONDS,
    );

    assert!(evaluation.fired.is_empty());
    assert!(evaluation.selected.is_none());
    assert_eq!(
        evaluation.degraded_inputs,
        vec!["live-state registry unavailable"]
    );
}

#[test]
fn resv_3_degraded_ranking_source_fails_closed_and_surfaces_reason() {
    // A ranking source that cannot be read (Err) fails closed with the reason
    // surfaced, distinct from a healthy empty ranking.
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &DegradedRankingSource,
        &HotSwapTriggerLogForbiddenSink,
        OBSERVED_AT_SECONDS,
    );

    assert!(evaluation.fired.is_empty());
    assert!(evaluation.selected.is_none());
    assert_eq!(
        evaluation.degraded_inputs,
        vec!["ranking source unavailable"]
    );
}

#[test]
fn resv_3_selected_proposal_maps_to_demotion_request() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        drawdown_demotion: DrawdownDemotionTrigger::Enabled {
            threshold: threshold(1_000),
        },
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 2_000);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 0.4)]));
    let log = HotSwapTriggerLogSpy::default();

    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    let selected = evaluation.selected.expect("a trigger fired");
    let request = selected.to_demotion_request();
    assert_eq!(request.demoting_strategy_id.as_str(), "live-a");
    assert_eq!(request.candidate_strategy_id.as_str(), "cand-b");
    assert_eq!(request.timeout_seconds, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS);
}
