//! SRS-ORCH-003 — workload-priority admission gate enforces the SyRS
//! SYS-57 hierarchy against the host memory safety margin. New
//! lower-priority workloads are refused when admitting them would
//! breach the safety margin; higher-priority workloads may evict the
//! lowest-priority active *batch* workload (SYS-58 (b)); the live
//! strategy is never selected for eviction (SYS-58 last clause).
//!
//! L7 domain (safety) integration test driving spy implementations of
//! `HostMemoryProbe`, `WorkloadRegistry`, and `WorkloadEventSink`
//! through `StrategyOrchestrator::admit_workload`.
//!
//! Post-conditions exercised here:
//!   * Ample headroom → admit silently. No events, no registry
//!     mutation (the `ForbiddenWorkloadEventSink` /
//!     `ForbiddenWorkloadRegistry` panic if invoked).
//!   * Available below margin AND no batch evictable → refusal with
//!     `OrderErrorCategory::HostMemorySafetyMarginBreach` AND a
//!     `WorkloadAdmissionEvent::Refused` event. No `runtime.create`
//!     is ever called (the gate sits in front of the runtime port —
//!     this test does not even thread a runtime through).
//!   * Lowest-priority batch (Research, rank 7) is evicted before any
//!     higher-priority batch (FactorPipeline, rank 5).
//!   * Continuous workloads (PaperStrategy) are never selected for
//!     eviction even when they are the lowest-priority active workload.
//!   * Live strategy is never selected for eviction even if a registry
//!     implementation drifts and lists it (debug_assert pins this).
//!   * Custom safety-margin override is honoured.

use atp_orchestrator::{
    HostMemoryProbe, HostMemoryProbeError, StrategyOrchestrator, WorkloadEventSink,
    WorkloadEventSinkError, WorkloadRegistry, WorkloadRegistryError, WorkloadTerminationError,
};
use atp_types::{
    HostMemorySafetyMargin, OrderErrorCategory, RegisteredWorkload, ResourceProfile, StrategyId,
    StrategyLaunchRequest, StrategyMode, WorkloadAdmissionEvent, WorkloadAdmissionReason,
    WorkloadId, WorkloadKind, WorkloadPriority, STRATEGY_STARTUP_DEADLINE_MS,
};
use std::cell::{Cell, RefCell};

struct HostMemorySpy {
    available_mb: Cell<u64>,
    probe_calls: Cell<u32>,
}

impl HostMemorySpy {
    fn new(available_mb: u64) -> Self {
        Self {
            available_mb: Cell::new(available_mb),
            probe_calls: Cell::new(0),
        }
    }
}

impl HostMemoryProbe for HostMemorySpy {
    fn available_mb(&self) -> Result<u64, HostMemoryProbeError> {
        self.probe_calls.set(self.probe_calls.get() + 1);
        Ok(self.available_mb.get())
    }
}

#[derive(Default)]
struct RegistrySpy {
    workloads: RefCell<Vec<RegisteredWorkload>>,
    terminate_calls: RefCell<Vec<WorkloadId>>,
}

impl RegistrySpy {
    fn with(workloads: Vec<RegisteredWorkload>) -> Self {
        Self {
            workloads: RefCell::new(workloads),
            terminate_calls: RefCell::new(Vec::new()),
        }
    }
}

impl WorkloadRegistry for RegistrySpy {
    fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError> {
        Ok(self.workloads.borrow().clone())
    }
    fn terminate(&self, id: &WorkloadId) -> Result<(), WorkloadTerminationError> {
        self.terminate_calls.borrow_mut().push(id.clone());
        self.workloads
            .borrow_mut()
            .retain(|workload| workload.id != *id);
        Ok(())
    }
}

#[derive(Default)]
struct EventSpy {
    events: RefCell<Vec<WorkloadAdmissionEvent>>,
}

impl WorkloadEventSink for EventSpy {
    fn record(
        &self,
        event: WorkloadAdmissionEvent,
    ) -> Result<(), WorkloadEventSinkError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

struct ForbiddenEventSink;

impl WorkloadEventSink for ForbiddenEventSink {
    fn record(
        &self,
        _event: WorkloadAdmissionEvent,
    ) -> Result<(), WorkloadEventSinkError> {
        panic!(
            "happy-path admit_workload must not emit a WorkloadAdmissionEvent"
        );
    }
}

struct ForbiddenRegistry;

impl WorkloadRegistry for ForbiddenRegistry {
    fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError> {
        Ok(Vec::new())
    }
    fn terminate(&self, _id: &WorkloadId) -> Result<(), WorkloadTerminationError> {
        panic!("happy-path admit_workload must not call registry.terminate");
    }
}

fn request(id: &str, mode: StrategyMode) -> StrategyLaunchRequest {
    StrategyLaunchRequest {
        strategy_id: StrategyId::new(id),
        mode,
        deployment_hash: atp_types::SourceHash::new(
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        profile: ResourceProfile::for_mode(mode),
    }
}

fn workload(id: &str, priority: WorkloadPriority, mem_mb: u32) -> RegisteredWorkload {
    RegisteredWorkload {
        id: WorkloadId::new(id),
        priority,
        kind: priority.default_kind(),
        profile: ResourceProfile {
            mem_mb,
            cpu_hundredths: 10,
        },
    }
}

#[test]
fn ample_headroom_admits_silently() {
    let orchestrator = StrategyOrchestrator;
    let host = HostMemorySpy::new(8_192);
    let registry = ForbiddenRegistry;
    let sink = ForbiddenEventSink;
    orchestrator
        .admit_workload(
            &request("paper-1", StrategyMode::Paper),
            WorkloadId::new("paper-1"),
            WorkloadPriority::PaperStrategy,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect("ample headroom must admit");
}

#[test]
fn refusal_emits_event_and_returns_breach_category() {
    let orchestrator = StrategyOrchestrator;
    // 2200 - 300 = 1900 < margin 2048 → must refuse with no batch
    // available.
    let host = HostMemorySpy::new(2_200);
    let registry = RegistrySpy::with(vec![]);
    let sink = EventSpy::default();
    let error = orchestrator
        .admit_workload(
            &request("paper-2", StrategyMode::Paper),
            WorkloadId::new("paper-2"),
            WorkloadPriority::PaperStrategy,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect_err("safety margin breach must refuse");
    assert_eq!(
        error.category,
        OrderErrorCategory::HostMemorySafetyMarginBreach
    );
    assert_eq!(
        error.category.as_str(),
        "HOST_MEMORY_SAFETY_MARGIN_BREACH"
    );
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    match &events[0] {
        WorkloadAdmissionEvent::Refused {
            workload_id,
            priority,
            reason,
            ..
        } => {
            assert_eq!(workload_id.as_str(), "paper-2");
            assert_eq!(*priority, WorkloadPriority::PaperStrategy);
            assert!(matches!(
                reason,
                WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                    available_mb: 2_200,
                    safety_margin_mb: 2_048,
                }
            ));
        }
        other => panic!("expected Refused, got {other:?}"),
    }
    assert!(
        registry.terminate_calls.borrow().is_empty(),
        "no batch in registry must mean no terminate call"
    );
}

#[test]
fn lowest_priority_batch_is_evicted_first() {
    let orchestrator = StrategyOrchestrator;
    let host = HostMemorySpy::new(2_300);
    let registry = RegistrySpy::with(vec![
        workload("factor-nightly", WorkloadPriority::FactorPipeline, 512),
        workload("research-jupyter-01", WorkloadPriority::Research, 512),
        workload("backtest-2026-05-14", WorkloadPriority::Backtest, 512),
    ]);
    let sink = EventSpy::default();
    orchestrator
        .admit_workload(
            &request("md-subscriber", StrategyMode::Paper),
            WorkloadId::new("md-subscriber"),
            WorkloadPriority::MarketDataSubscriptionManager,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect("post-eviction headroom must admit");
    let terminate_calls = registry.terminate_calls.borrow();
    assert_eq!(terminate_calls.len(), 1);
    assert_eq!(
        terminate_calls[0].as_str(),
        "research-jupyter-01",
        "Research (rank 7) must be evicted before FactorPipeline (rank 5) or Backtest (rank 6)"
    );
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert!(matches!(
        events[0],
        WorkloadAdmissionEvent::Terminated { .. }
    ));
}

#[test]
fn continuous_workloads_are_never_evicted() {
    let orchestrator = StrategyOrchestrator;
    // Registry has only Continuous workloads (paper strategies); none
    // are eligible for eviction even though they are lower priority
    // than the incoming MarketData workload (rank 2).
    let host = HostMemorySpy::new(2_200);
    let registry = RegistrySpy::with(vec![
        workload("paper-strat-1", WorkloadPriority::PaperStrategy, 300),
        workload("paper-strat-2", WorkloadPriority::PaperStrategy, 300),
    ]);
    let sink = EventSpy::default();
    let error = orchestrator
        .admit_workload(
            &request("md-subscriber", StrategyMode::Paper),
            WorkloadId::new("md-subscriber"),
            WorkloadPriority::MarketDataSubscriptionManager,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect_err("only-continuous registry must refuse without eviction");
    assert_eq!(
        error.category,
        OrderErrorCategory::HostMemorySafetyMarginBreach
    );
    assert!(
        registry.terminate_calls.borrow().is_empty(),
        "Continuous workloads must never be selected for eviction"
    );
}

#[test]
fn live_strategy_is_never_terminated_even_if_registry_lists_it() {
    // Defensive test for SyRS SYS-58 last clause. Live strategy is
    // Continuous so the kind-filter should already exclude it; if a
    // registry implementation drifts and incorrectly marks it as Batch,
    // the gate's debug_assert would catch it. Here we list it as
    // Continuous (the correct kind) and confirm the gate ignores it.
    let orchestrator = StrategyOrchestrator;
    let host = HostMemorySpy::new(2_200);
    let registry = RegistrySpy::with(vec![
        RegisteredWorkload {
            id: WorkloadId::new("live-strategy-flagship"),
            priority: WorkloadPriority::LiveStrategy,
            kind: WorkloadKind::Continuous,
            profile: ResourceProfile::live_default(),
        },
    ]);
    let sink = EventSpy::default();
    let error = orchestrator
        .admit_workload(
            &request("research-1", StrategyMode::Paper),
            WorkloadId::new("research-1"),
            WorkloadPriority::Research,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect_err("live strategy must never be evicted; refusal expected");
    assert_eq!(
        error.category,
        OrderErrorCategory::HostMemorySafetyMarginBreach
    );
    assert!(
        registry.terminate_calls.borrow().is_empty(),
        "live strategy must never appear in terminate calls"
    );
}

#[test]
fn lower_priority_incoming_does_not_evict_higher_priority_batch() {
    // SyRS SYS-58 (b): batch workloads are evicted ONLY if a
    // higher-priority workload requires resources. A Research (rank 7)
    // arriving when a Backtest (rank 6) batch is active must NOT
    // evict the Backtest.
    let orchestrator = StrategyOrchestrator;
    let host = HostMemorySpy::new(2_200);
    let registry = RegistrySpy::with(vec![workload(
        "backtest-2026-05-14",
        WorkloadPriority::Backtest,
        512,
    )]);
    let sink = EventSpy::default();
    let error = orchestrator
        .admit_workload(
            &request("research-1", StrategyMode::Paper),
            WorkloadId::new("research-1"),
            WorkloadPriority::Research,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect_err("equal-or-lower-priority incoming must not evict batch");
    assert_eq!(
        error.category,
        OrderErrorCategory::HostMemorySafetyMarginBreach
    );
    assert!(
        registry.terminate_calls.borrow().is_empty(),
        "lower-priority incoming must not trigger any eviction"
    );
}

#[test]
fn custom_safety_margin_override_is_honoured() {
    let orchestrator = StrategyOrchestrator;
    let host = HostMemorySpy::new(1_000);
    let registry = ForbiddenRegistry;
    let sink = ForbiddenEventSink;
    orchestrator
        .admit_workload(
            &request("paper-1", StrategyMode::Paper),
            WorkloadId::new("paper-1"),
            WorkloadPriority::PaperStrategy,
            HostMemorySafetyMargin { mb: 512 },
            &host,
            &registry,
            &sink,
            1_715_700_000,
        )
        .expect("post-admit headroom above custom margin must admit");
}
