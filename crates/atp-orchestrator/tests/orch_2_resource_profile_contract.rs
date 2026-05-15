//! SRS-ORCH-002 — strategy container resource profile is enforced at the
//! orchestrator boundary; defaults match the SyRS SYS-11 spec literals
//! (live: 512 MB / 0.25 CPU; paper: 300 MB / 0.10 CPU); configuration
//! overrides are validated against the SRS-ARCH-005 catalogue bounds
//! and a misconfigured launch never reaches the runtime port.
//!
//! L7 domain (safety) integration test driving spy implementations of
//! `StrategyContainerRuntime` and `HealthCheckEventSink` through
//! `StrategyOrchestrator::launch`.
//!
//! Post-conditions exercised here:
//!   * Live launch with `ResourceProfile::live_default()` → outcome's
//!     `profile == live_default()`, runtime received `create + start`
//!     exactly once, and the sink is NEVER touched (the ForbiddenSink
//!     panics if invoked).
//!   * Paper launch with `paper_default()` → symmetric.
//!   * Below-floor mem rejected → `runtime.create_calls == 0`,
//!     `runtime.start_calls == 0`, sink is NEVER touched, error category
//!     is `ResourceProfileInvalid`, error_type is
//!     `ResourceProfileInvalid::MemBelowFloor`.
//!   * Above-ceiling cpu rejected → symmetric, error_type is
//!     `ResourceProfileInvalid::CpuAboveCeiling`.
//!   * Custom in-range override propagated unchanged through
//!     `runtime.create` (the profile recorded by the spy equals the one
//!     the request supplied) and `outcome.profile == request.profile`.
//!   * Mode-uniformity: live and paper launches share envelope shape;
//!     only the default profile differs.

use atp_orchestrator::{
    DeployedVersionRegistry, DeployedVersionRegistryError, HealthCheckEventSink,
    StrategyContainerRuntime, StrategyOrchestrator,
};
use atp_types::{
    ContainerHealthEvent, ContainerHealthState, DeployedVersion, LaunchReadiness,
    OrderErrorCategory, ResourceProfile, SourceHash, StrategyId, StrategyLaunchRequest,
    StrategyMode, STRATEGY_STARTUP_DEADLINE_MS,
};
use std::cell::{Cell, RefCell};

/// Test fixture: a valid 64-hex SHA-256 wire-form source hash so the
/// SRS-ORCH-004 launch validation passes.
const TEST_SOURCE_HASH: &str =
    "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

#[derive(Default)]
struct VersionRegistryNoop;

impl DeployedVersionRegistry for VersionRegistryNoop {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        Ok(())
    }
    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        Ok(None)
    }
}

struct ForbiddenVersionRegistry;

impl DeployedVersionRegistry for ForbiddenVersionRegistry {
    fn record(
        &self,
        _strategy_id: &StrategyId,
        _version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError> {
        panic!("ResourceProfileInvalid rejection must not record a deployed version");
    }
    fn lookup(
        &self,
        _strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
        panic!("ResourceProfileInvalid rejection must not query the version registry");
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
    create_profiles: RefCell<Vec<ResourceProfile>>,
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
            create_profiles: RefCell::new(Vec::new()),
        }
    }
}

impl StrategyContainerRuntime for RuntimeSpy {
    fn create(&self, request: &StrategyLaunchRequest) {
        self.create_calls.set(self.create_calls.get() + 1);
        // SRS-ORCH-002 evidence: the runtime port receives the profile
        // verbatim from the launch request; recording it here lets the
        // test assert no silent re-defaulting at the gate.
        self.create_profiles.borrow_mut().push(request.profile);
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

struct ForbiddenSink;

impl HealthCheckEventSink for ForbiddenSink {
    fn record(&self, _event: ContainerHealthEvent) {
        panic!(
            "ReadyWithinDeadline / ResourceProfileInvalid must not record a ContainerHealthEvent"
        );
    }
}

fn request(id: &str, mode: StrategyMode, profile: ResourceProfile) -> StrategyLaunchRequest {
    StrategyLaunchRequest {
        strategy_id: StrategyId::new(id),
        mode,
        deployment_hash: SourceHash::new(TEST_SOURCE_HASH),
        deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        profile,
    }
}

#[test]
fn orch_2_live_default_profile_is_propagated_through_create_to_outcome() {
    // SyRS SYS-11 default: live containers get 512 MB / 0.25 CPU. The
    // outcome must carry the SAME profile the request supplied — not a
    // re-default at the gate. The runtime spy records what was passed
    // to `create`; the assertion below proves the gate did not strip,
    // re-default, or rewrite the profile.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 4_200 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = VersionRegistryNoop;
    let outcome = orchestrator
        .launch(
            request("alpha-1", StrategyMode::Live, ResourceProfile::live_default()),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect("Live launch with default profile must accept");
    assert_eq!(outcome.profile, ResourceProfile::live_default());
    assert_eq!(outcome.profile.mem_mb, 512);
    assert_eq!(outcome.profile.cpu_hundredths, 25);
    assert_eq!(runtime.create_calls.get(), 1);
    assert_eq!(runtime.start_calls.get(), 1);
    let recorded = runtime.create_profiles.borrow();
    assert_eq!(recorded.len(), 1);
    assert_eq!(recorded[0], ResourceProfile::live_default());
}

#[test]
fn orch_2_paper_default_profile_is_propagated_through_create_to_outcome() {
    // SyRS SYS-11 default: paper containers get 300 MB / 0.10 CPU.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 3_100 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = VersionRegistryNoop;
    let outcome = orchestrator
        .launch(
            request("paper-1", StrategyMode::Paper, ResourceProfile::paper_default()),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect("Paper launch with default profile must accept");
    assert_eq!(outcome.profile, ResourceProfile::paper_default());
    assert_eq!(outcome.profile.mem_mb, 300);
    assert_eq!(outcome.profile.cpu_hundredths, 10);
    let recorded = runtime.create_profiles.borrow();
    assert_eq!(recorded[0], ResourceProfile::paper_default());
}

#[test]
fn orch_2_in_range_custom_override_is_propagated_unchanged() {
    // "Configuration overrides are validated" — an override within the
    // catalogue bounds (≥ 64 MB, ≤ 65,536 MB; ≥ 0.05 CPU, ≤ 16.0 CPU)
    // must be accepted and threaded byte-equal through `create` and
    // into the outcome. No re-defaulting; no clamping; no rounding.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1_000 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = VersionRegistryNoop;
    let custom = ResourceProfile {
        mem_mb: 1_024,
        cpu_hundredths: 80,
    };
    let outcome = orchestrator
        .launch(
            request("alpha-2", StrategyMode::Live, custom),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect("in-range custom profile must accept");
    assert_eq!(outcome.profile, custom);
    let recorded = runtime.create_profiles.borrow();
    assert_eq!(recorded[0], custom);
}

#[test]
fn orch_2_below_floor_memory_is_refused_without_invoking_runtime() {
    // SRS-ORCH-002: a misconfigured override (mem below the 64 MB
    // catalogue floor) must never reach `runtime.create` — there is
    // no container to destroy, no event to emit, no resources to
    // release. The spy counters prove the gate short-circuited at
    // validation. The error envelope carries the original request and
    // the specific violation discriminator.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = ForbiddenVersionRegistry;
    let bad = ResourceProfile {
        mem_mb: 16,
        cpu_hundredths: 25,
    };
    let error = orchestrator
        .launch(
            request("alpha-3", StrategyMode::Live, bad),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect_err("below-floor mem must be refused");
    assert_eq!(error.category, OrderErrorCategory::ResourceProfileInvalid);
    assert_eq!(error.category.as_str(), "RESOURCE_PROFILE_INVALID");
    assert_eq!(error.error_type, "ResourceProfileInvalid::MemBelowFloor");
    assert_eq!(error.original_request.profile, bad);
    assert!(error.message.contains("SRS-ORCH-002"));
    assert!(error.message.contains("SYS-11"));
    assert!(error.message.contains("alpha-3"));
    assert_eq!(
        runtime.create_calls.get(),
        0,
        "validation gate must short-circuit before runtime.create"
    );
    assert_eq!(runtime.start_calls.get(), 0);
    assert_eq!(runtime.destroy_calls.get(), 0);
    assert_eq!(runtime.stop_calls.get(), 0);
    assert!(runtime.create_profiles.borrow().is_empty());
}

#[test]
fn orch_2_above_ceiling_cpu_is_refused_without_invoking_runtime() {
    // Symmetric to the floor case but on the CPU upper bound.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = ForbiddenVersionRegistry;
    let bad = ResourceProfile {
        mem_mb: 512,
        cpu_hundredths: 9_999,
    };
    let error = orchestrator
        .launch(
            request("alpha-4", StrategyMode::Live, bad),
            &runtime,
            &sink,
            &version_registry,
            1_715_000_000,
        )
        .expect_err("above-ceiling cpu must be refused");
    assert_eq!(error.error_type, "ResourceProfileInvalid::CpuAboveCeiling");
    assert_eq!(runtime.create_calls.get(), 0);
    assert_eq!(runtime.start_calls.get(), 0);
}

#[test]
fn orch_2_launch_envelope_is_mode_uniform_with_distinct_default_profiles() {
    // AC-14 / AC-15 uniformity: the same gate, same envelope shape,
    // accepts a Live launch and a Paper launch — only the default
    // profile differs. No mode-branch in the gate logic.
    let orchestrator = StrategyOrchestrator;
    let runtime_live = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 4_200 },
        ContainerHealthState::Healthy,
    );
    let runtime_paper = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 3_100 },
        ContainerHealthState::Healthy,
    );
    let version_registry = VersionRegistryNoop;
    let live_outcome = orchestrator
        .launch(
            request("live-1", StrategyMode::Live, ResourceProfile::live_default()),
            &runtime_live,
            &ForbiddenSink,
            &version_registry,
            1,
        )
        .expect("Live launch must accept");
    let paper_outcome = orchestrator
        .launch(
            request("paper-1", StrategyMode::Paper, ResourceProfile::paper_default()),
            &runtime_paper,
            &ForbiddenSink,
            &version_registry,
            1,
        )
        .expect("Paper launch must accept");
    assert_eq!(live_outcome.deadline_millis, paper_outcome.deadline_millis);
    assert_ne!(live_outcome.profile, paper_outcome.profile);
    assert_eq!(live_outcome.profile, ResourceProfile::live_default());
    assert_eq!(paper_outcome.profile, ResourceProfile::paper_default());
    assert_eq!(
        runtime_live.create_calls.get(),
        runtime_paper.create_calls.get(),
        "gate must call create the same number of times regardless of mode"
    );
}
