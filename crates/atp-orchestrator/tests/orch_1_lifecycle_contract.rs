//! SRS-ORCH-001 — strategy container lifecycle is orchestrator-owned;
//! launch honours NFR-P9 (≤30s); SYS-13 auto-restarts unresponsive
//! containers, logs the action, and emits a dashboard event. L7 domain
//! (safety) integration test driving spy implementations of the
//! `StrategyContainerRuntime` and `HealthCheckEventSink` ports through
//! `StrategyOrchestrator::launch` and `StrategyOrchestrator::observe_health`.
//!
//! Post-conditions exercised here:
//!   * `ReadyWithinDeadline` returns `StrategyLaunchOutcome` and the
//!     sink is NEVER touched (the ForbiddenSink panics if invoked).
//!   * `DeadlineExceeded` rejects with `STRATEGY_STARTUP_DEADLINE_EXCEEDED`,
//!     calls `runtime.destroy` exactly once (SRS-ORCH-002 / SyRS SYS-57
//!     resource release), emits exactly one `ContainerHealthEvent` whose
//!     `action_taken == Destroy` honestly reflects that call, does NOT
//!     construct `StrategyLaunchOutcome`, and surfaces the original
//!     launch request unchanged.
//!   * `observe_health(Healthy)` is read-only: no `restart`, no `stop`,
//!     no `destroy`, no sink event.
//!   * `observe_health(Unresponsive)` calls `runtime.restart` EXACTLY
//!     once and records EXACTLY one `ContainerHealthEvent` with
//!     `action_taken == Restart` (SYS-13 binding).
//!   * The launch is mode-uniform: paper and live launches flow through
//!     the same gate with the same envelope shape (AC-14 / AC-15
//!     uniformity — the gate takes no mode-branch).

use atp_orchestrator::{
    DeployedVersionRegistry, DeployedVersionRegistryError, HealthCheckEventSink,
    StrategyContainerRuntime, StrategyOrchestrator,
};
use atp_types::{
    ContainerHealthEvent, ContainerHealthState, ContainerLifecycleAction, DeployedVersion,
    LaunchReadiness, OrderErrorCategory, ResourceProfile, SourceHash, StrategyId,
    StrategyLaunchRequest, StrategyMode, STRATEGY_STARTUP_DEADLINE_MS,
};
use std::cell::{Cell, RefCell};

/// Test fixture: a valid 64-hex SHA-256 wire-form source hash so the
/// SRS-ORCH-004 launch validation passes. All `orch_1_*` integration
/// tests use this single value.
const TEST_SOURCE_HASH: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

#[derive(Default)]
struct VersionRegistrySpy {
    records: RefCell<Vec<(StrategyId, DeployedVersion)>>,
}

impl DeployedVersionRegistry for VersionRegistrySpy {
    fn record(
        &self,
        strategy_id: &StrategyId,
        version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        self.records
            .borrow_mut()
            .push((strategy_id.clone(), version));
        Ok(())
    }
    fn lookup(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        Ok(self
            .records
            .borrow()
            .iter()
            .rev()
            .find(|(id, _)| id == strategy_id)
            .map(|(_, v)| v.clone()))
    }
}

struct ForbiddenVersionRegistry;

impl DeployedVersionRegistry for ForbiddenVersionRegistry {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        panic!("DeadlineExceeded / pre-create rejection must not record a deployed version");
    }
    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        panic!("DeadlineExceeded / pre-create rejection must not query the version registry");
    }
}

struct RuntimeSpy {
    readiness: LaunchReadiness,
    health_state: ContainerHealthState,
    create_calls: Cell<u32>,
    start_calls: Cell<u32>,
    stop_calls: Cell<u32>,
    restart_calls: Cell<u32>,
    destroy_calls: Cell<u32>,
    health_calls: Cell<u32>,
}

impl RuntimeSpy {
    fn new(readiness: LaunchReadiness, health_state: ContainerHealthState) -> Self {
        Self {
            readiness,
            health_state,
            create_calls: Cell::new(0),
            start_calls: Cell::new(0),
            stop_calls: Cell::new(0),
            restart_calls: Cell::new(0),
            destroy_calls: Cell::new(0),
            health_calls: Cell::new(0),
        }
    }
}

impl StrategyContainerRuntime for RuntimeSpy {
    fn create(&self, _request: &StrategyLaunchRequest) {
        self.create_calls.set(self.create_calls.get() + 1);
    }
    fn start(&self, _request: &StrategyLaunchRequest) -> LaunchReadiness {
        self.start_calls.set(self.start_calls.get() + 1);
        self.readiness
    }
    fn stop(&self, _strategy_id: &StrategyId) {
        self.stop_calls.set(self.stop_calls.get() + 1);
    }
    fn restart(&self, _strategy_id: &StrategyId) {
        self.restart_calls.set(self.restart_calls.get() + 1);
    }
    fn destroy(&self, _strategy_id: &StrategyId) {
        self.destroy_calls.set(self.destroy_calls.get() + 1);
    }
    fn health(&self, _strategy_id: &StrategyId) -> ContainerHealthState {
        self.health_calls.set(self.health_calls.get() + 1);
        self.health_state
    }
}

#[derive(Default)]
struct EventSinkSpy {
    events: RefCell<Vec<ContainerHealthEvent>>,
}

impl HealthCheckEventSink for EventSinkSpy {
    fn record(&self, event: ContainerHealthEvent) {
        self.events.borrow_mut().push(event);
    }
}

struct ForbiddenSink;

impl HealthCheckEventSink for ForbiddenSink {
    fn record(&self, _event: ContainerHealthEvent) {
        panic!("ReadyWithinDeadline / Healthy must not record a ContainerHealthEvent");
    }
}

fn request(id: &str, mode: StrategyMode) -> StrategyLaunchRequest {
    StrategyLaunchRequest {
        strategy_id: StrategyId::new(id),
        mode,
        deployment_hash: SourceHash::new(TEST_SOURCE_HASH),
        deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        profile: ResourceProfile::for_mode(mode),
    }
}

#[test]
fn orch_1_ready_within_deadline_state_returns_outcome_and_emits_no_event() {
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 4_200,
        },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = VersionRegistrySpy::default();
    let outcome = orchestrator
        .launch(
            request("alpha-1", StrategyMode::Live),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect("ReadyWithinDeadline must accept the launch");
    assert_eq!(outcome.strategy_id.as_str(), "alpha-1");
    assert!(outcome.ready_within_deadline);
    assert_eq!(outcome.elapsed_millis, 4_200);
    assert_eq!(outcome.deadline_millis, STRATEGY_STARTUP_DEADLINE_MS);
    assert_eq!(
        runtime.create_calls.get(),
        1,
        "create must be called exactly once"
    );
    assert_eq!(
        runtime.start_calls.get(),
        1,
        "start must be called exactly once"
    );
}

#[test]
fn orch_1_deadline_exceeded_state_blocks_launch_with_structured_error() {
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::DeadlineExceeded {
            elapsed_millis: 32_500,
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        },
        ContainerHealthState::Healthy,
    );
    let sink = EventSinkSpy::default();
    let version_registry = ForbiddenVersionRegistry;
    let error = orchestrator
        .launch(
            request("alpha-1", StrategyMode::Live),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect_err("DeadlineExceeded must refuse the launch");
    assert_eq!(
        error.category,
        OrderErrorCategory::StrategyStartupDeadlineExceeded
    );
    assert_eq!(
        error.category.as_str(),
        "STRATEGY_STARTUP_DEADLINE_EXCEEDED"
    );
    assert_eq!(error.error_type, "StrategyStartupDeadlineExceeded");
    assert_eq!(error.original_request.strategy_id.as_str(), "alpha-1");
    assert_eq!(error.original_request.mode, StrategyMode::Live);
    assert!(error.message.contains("SRS-ORCH-001"));
    assert!(error.message.contains("NFR-P9"));
    assert!(error.message.contains("alpha-1"));
    assert!(error.message.contains("32500"));
    assert!(error.message.contains("30000"));

    // SRS-ORCH-002 + SyRS SYS-57 + NFR-R5: the destroy is the audited
    // resource release; the event payload's action_taken=Destroy below
    // is the public claim that this destroy happened.
    assert_eq!(
        runtime.destroy_calls.get(),
        1,
        "DeadlineExceeded must invoke runtime.destroy exactly once"
    );
    assert_eq!(
        runtime.stop_calls.get(),
        0,
        "over-deadline cleanup uses destroy, not stop"
    );
    assert_eq!(
        runtime.restart_calls.get(),
        0,
        "over-deadline launches must not silently retry via restart"
    );

    let events = sink.events.borrow();
    assert_eq!(events.len(), 1, "exactly one event per refused launch");
    assert_eq!(events[0].state, ContainerHealthState::Unresponsive);
    assert_eq!(events[0].action_taken, ContainerLifecycleAction::Destroy);
    assert_eq!(events[0].strategy_id.as_str(), "alpha-1");
    assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
}

#[test]
fn orch_1_healthy_observation_is_read_only() {
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let state =
        orchestrator.observe_health(StrategyId::new("alpha-1"), &runtime, &sink, 1_715_000_000);
    assert_eq!(state, ContainerHealthState::Healthy);
    assert_eq!(runtime.health_calls.get(), 1);
    assert_eq!(
        runtime.restart_calls.get(),
        0,
        "Healthy observation MUST NOT invoke restart (SYS-13 selectivity)"
    );
    assert_eq!(runtime.destroy_calls.get(), 0);
    assert_eq!(runtime.stop_calls.get(), 0);
}

#[test]
fn orch_1_unresponsive_observation_restarts_and_records_event_exactly_once() {
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Unresponsive,
    );
    let sink = EventSinkSpy::default();
    let state =
        orchestrator.observe_health(StrategyId::new("alpha-1"), &runtime, &sink, 1_715_000_000);
    assert_eq!(state, ContainerHealthState::Unresponsive);
    assert_eq!(runtime.health_calls.get(), 1);
    assert_eq!(
        runtime.restart_calls.get(),
        1,
        "Unresponsive observation must trigger exactly one restart"
    );
    assert_eq!(
        runtime.destroy_calls.get(),
        0,
        "auto-restart must NOT destroy"
    );
    assert_eq!(runtime.stop_calls.get(), 0, "auto-restart must NOT stop");
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1, "exactly one dashboard event per restart");
    assert_eq!(events[0].state, ContainerHealthState::Unresponsive);
    assert_eq!(events[0].action_taken, ContainerLifecycleAction::Restart);
    assert_eq!(events[0].strategy_id.as_str(), "alpha-1");
    assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
}

#[test]
fn orch_1_launch_is_mode_uniform_across_live_and_paper() {
    // AC-14 / AC-15: the orchestrator's lifecycle gate must take no
    // mode-branch. The same envelope shape and the same gate must
    // accept a Live launch and a Paper launch with byte-equal
    // structural shape (only the mode field differs).
    let orchestrator = StrategyOrchestrator;
    let runtime_live = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 4_200,
        },
        ContainerHealthState::Healthy,
    );
    let runtime_paper = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 3_100,
        },
        ContainerHealthState::Healthy,
    );
    let version_registry = VersionRegistrySpy::default();
    let live_outcome = orchestrator
        .launch(
            request("live-1", StrategyMode::Live),
            &runtime_live,
            &ForbiddenSink,
            &version_registry,
            1,
        )
        .expect("Live launch must accept");
    let paper_outcome = orchestrator
        .launch(
            request("paper-1", StrategyMode::Paper),
            &runtime_paper,
            &ForbiddenSink,
            &version_registry,
            1,
        )
        .expect("Paper launch must accept");
    assert!(live_outcome.ready_within_deadline);
    assert!(paper_outcome.ready_within_deadline);
    assert_eq!(live_outcome.deadline_millis, paper_outcome.deadline_millis);
    assert_eq!(
        runtime_live.create_calls.get(),
        runtime_paper.create_calls.get(),
        "the gate must call create the same number of times regardless of mode"
    );
}

#[test]
fn orch_1_deadline_exceeded_anchors_zero_outcome_on_refusal() {
    // Zero-acceptance invariant: a DeadlineExceeded launch must NOT
    // surface a StrategyLaunchOutcome under any circumstance. The
    // PRIMARY enforcement is the static check
    // (tools/orchestrator_lifecycle_check.py) via the contract's
    // forbidden_mutations + accepted_struct allowlist; this test
    // anchors the behavioural post-condition at the integration layer.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::DeadlineExceeded {
            elapsed_millis: 45_000,
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        },
        ContainerHealthState::Healthy,
    );
    let sink = EventSinkSpy::default();
    let version_registry = ForbiddenVersionRegistry;
    let result = orchestrator.launch(
        request("alpha-1", StrategyMode::Live),
        &runtime,
        &sink,
        &version_registry,
        0,
    );
    assert!(
        result.is_err(),
        "DeadlineExceeded must never return a StrategyLaunchOutcome"
    );
}
