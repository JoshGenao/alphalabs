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
//!   * Logging is best-effort: a failing log sink does not un-fire the trigger
//!     or abort the evaluation.
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
    ReservoirRankingSnapshot, StrategyId, TriggerRationale, HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
};
use std::cell::RefCell;

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
    fn current_live(&self) -> Option<LiveStrategyState> {
        self.state.clone()
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
    fn snapshot(&self) -> ReservoirRankingSnapshot {
        self.snapshot.clone()
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

/// Log sink that records the event but reports a publication failure — models an
/// unwritable system-log store. The evaluator must treat emission as best-effort:
/// it must NOT panic or un-fire the trigger.
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

    let proposal = orchestrator.request_manual_promotion(
        StrategyId::new("live-a"),
        StrategyId::new("cand-b"),
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(proposal.kind, HotSwapTriggerKind::ManualPromotion);
    assert_eq!(proposal.demoting_strategy_id.as_str(), "live-a");
    assert_eq!(proposal.candidate_strategy_id.as_str(), "cand-b");
    assert_eq!(proposal.rationale, TriggerRationale::ManualSelection);

    let events = log.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].kind, HotSwapTriggerKind::ManualPromotion);
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
    // in the same order.
    let logged_kinds: Vec<HotSwapTriggerKind> =
        log.events.borrow().iter().map(|event| event.kind).collect();
    assert_eq!(logged_kinds, kinds);
    assert_eq!(log.events.borrow().len(), evaluation.fired.len());
}

#[test]
fn resv_3_failing_log_sink_is_best_effort() {
    let orchestrator = StrategyOrchestrator;
    let config = HotSwapTriggerConfig {
        top_ranked_promotion: RankingPromotionTrigger::Enabled,
        ..HotSwapTriggerConfig::default()
    };
    let live = LiveStrategyProbeStub::live("live-a", 100);
    let ranking = ReservoirRankingSourceStub::new(snapshot(vec![ranked("cand-b", 1, 2.5, 0.4)]));
    let log = HotSwapTriggerLogFailingSink::default();

    // Must not panic despite the sink returning Err; the decision stands.
    let evaluation = orchestrator.evaluate_automatic_triggers(
        &config,
        &live,
        &ranking,
        &log,
        OBSERVED_AT_SECONDS,
    );

    assert_eq!(evaluation.fired.len(), 1);
    assert_eq!(log.events.borrow().len(), 1);
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
