//! SRS-ORCH-004 — the Strategy Orchestrator records the deployed
//! code version (source hash + deployment timestamp) for each strategy
//! at deployment time and exposes it through the
//! `DeployedVersionRegistry` port so the deferred dashboard (SyRS
//! SYS-41), REST API (IF-9), and backtest result rows (SYS-21) render
//! the same `version_identifier` across all three surfaces.
//!
//! L7 domain (safety) integration test driving spy implementations of
//! `StrategyContainerRuntime`, `HealthCheckEventSink`, and
//! `DeployedVersionRegistry` through `StrategyOrchestrator::launch`.
//!
//! Post-conditions exercised here:
//!   * `ReadyWithinDeadline` records EXACTLY one `DeployedVersion`
//!     through the registry, the outcome carries the SAME record,
//!     and a subsequent `lookup` returns it (the "queryable via REST
//!     API" half of the acceptance criterion).
//!   * Malformed source hash → `OrderErrorCategory::DeployedVersionInvalid`
//!     with NO `runtime.create`, NO sink event, NO version record.
//!   * `DeadlineExceeded` → no version record (a version that was
//!     never deployed must not appear in the active-strategy
//!     inventory).
//!   * Registry `record` failure does NOT abort the launch; the
//!     outcome still carries the version (best-effort by design;
//!     concrete callers observe failures via the typed
//!     `DeployedVersionRegistryError`).
//!   * The same `version_identifier()` string is produced by the
//!     orchestrator's outcome and by the registry lookup — the
//!     "display or return the same version identifier" guarantee.

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
/// SRS-ORCH-004 launch validation passes. All `orch_4_*` integration
/// tests use this single value (or a distinct one when they need to
/// compare records).
const TEST_SOURCE_HASH: &str =
    "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

const TEST_SOURCE_HASH_BETA: &str =
    "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

struct RuntimeSpy {
    readiness: LaunchReadiness,
    health_state: ContainerHealthState,
    create_calls: Cell<u32>,
    start_calls: Cell<u32>,
    destroy_calls: Cell<u32>,
}

impl RuntimeSpy {
    fn new(readiness: LaunchReadiness, health_state: ContainerHealthState) -> Self {
        Self {
            readiness,
            health_state,
            create_calls: Cell::new(0),
            start_calls: Cell::new(0),
            destroy_calls: Cell::new(0),
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
    fn stop(&self, _strategy_id: &StrategyId) {}
    fn restart(&self, _strategy_id: &StrategyId) {}
    fn destroy(&self, _strategy_id: &StrategyId) {
        self.destroy_calls.set(self.destroy_calls.get() + 1);
    }
    fn health(&self, _strategy_id: &StrategyId) -> ContainerHealthState {
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
        panic!("ReadyWithinDeadline / pre-create rejection must not record a ContainerHealthEvent");
    }
}

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

fn request_with_hash(id: &str, mode: StrategyMode, hash: &str) -> StrategyLaunchRequest {
    StrategyLaunchRequest {
        strategy_id: StrategyId::new(id),
        mode,
        deployment_hash: SourceHash::new(hash),
        deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        profile: ResourceProfile::for_mode(mode),
    }
}

#[test]
fn orch_4_ready_within_deadline_records_deployed_version_exactly_once() {
    // SRS-ORCH-004 happy path: the orchestrator records the deployed
    // version (hash + observed_at_seconds) through the registry port
    // exactly once per successful launch, and the same record appears
    // on the launch outcome (the "stores a source hash and timestamp"
    // half of the acceptance criterion).
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
            request_with_hash("alpha-1", StrategyMode::Live, TEST_SOURCE_HASH),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect("ReadyWithinDeadline must accept the launch");

    assert_eq!(
        outcome.deployed_version.source_hash.as_str(),
        TEST_SOURCE_HASH
    );
    assert_eq!(outcome.deployed_version.deployed_at_seconds, 1_715_700_000);

    let records = version_registry.records.borrow();
    assert_eq!(
        records.len(),
        1,
        "exactly one version record per successful launch"
    );
    assert_eq!(records[0].0.as_str(), "alpha-1");
    assert_eq!(records[0].1, outcome.deployed_version);
}

#[test]
fn orch_4_version_identifier_is_queryable_via_registry_lookup() {
    // SRS-ORCH-004 acceptance: dashboard, REST API, and backtest
    // results "display or return the same version identifier".
    // The registry is the read path that future surfaces consume;
    // looking up the strategy id must return the same record the
    // orchestrator wrote and the same `version_identifier()` string
    // appears on both the outcome and the lookup.
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
            request_with_hash("alpha-1", StrategyMode::Live, TEST_SOURCE_HASH),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect("ReadyWithinDeadline must accept the launch");

    let looked_up = version_registry
        .lookup(&StrategyId::new("alpha-1"))
        .expect("registry lookup must not fail")
        .expect("registry must have a record for the deployed strategy");
    assert_eq!(looked_up, outcome.deployed_version);
    assert_eq!(
        looked_up.version_identifier(),
        outcome.deployed_version.version_identifier(),
    );
    // Pin the canonical form so a future surface drift requires
    // touching THIS string.
    assert_eq!(
        looked_up.version_identifier(),
        format!("{TEST_SOURCE_HASH}@1715700000")
    );
}

#[test]
fn orch_4_malformed_source_hash_is_refused_without_invoking_runtime() {
    // SRS-ORCH-004 validate-before-create: a misformed override
    // (wrong digest length here) must never reach `runtime.create`.
    // The gate short-circuits with DeployedVersionInvalid, emits no
    // sink event, and never records a version.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = ForbiddenVersionRegistry;
    let error = orchestrator
        .launch(
            request_with_hash("alpha-1", StrategyMode::Live, "sha256:short"),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect_err("malformed hash must be refused");
    assert_eq!(error.category, OrderErrorCategory::DeployedVersionInvalid);
    assert_eq!(error.category.as_str(), "DEPLOYED_VERSION_INVALID");
    assert!(error.error_type.starts_with("DeployedVersionInvalid::"));
    assert!(error.message.contains("SRS-ORCH-004"));
    assert!(error.message.contains("SYS-79"));
    assert!(error.message.contains("alpha-1"));
    assert_eq!(
        runtime.create_calls.get(),
        0,
        "validation gate must short-circuit before runtime.create"
    );
    assert_eq!(runtime.start_calls.get(), 0);
    assert_eq!(runtime.destroy_calls.get(), 0);
}

#[test]
fn orch_4_unknown_algorithm_prefix_is_rejected() {
    // Algorithm-prefix drift (md5: instead of sha256:) must surface
    // as a distinct discriminator so the dashboard can render the
    // cause precisely.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = ForbiddenVersionRegistry;
    let bad_hash = "md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    let error = orchestrator
        .launch(
            request_with_hash("alpha-1", StrategyMode::Live, bad_hash),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect_err("unknown algorithm prefix must be refused");
    assert_eq!(error.error_type, "DeployedVersionInvalid::UnknownAlgorithm");
}

#[test]
fn orch_4_deadline_exceeded_records_no_version() {
    // A version that was never deployed must not appear in the
    // active-strategy inventory (SYS-41) or REST API listing (IF-9).
    // The DeadlineExceeded path destroys the container and skips the
    // version record (the ForbiddenVersionRegistry would panic if
    // record / lookup were invoked).
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
    let _error = orchestrator
        .launch(
            request_with_hash("alpha-1", StrategyMode::Live, TEST_SOURCE_HASH),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect_err("DeadlineExceeded must refuse the launch");
    // The container WAS destroyed; the destroy event is the audit
    // record. The version record was correctly skipped.
    assert_eq!(runtime.destroy_calls.get(), 1);
    let events = sink.events.borrow();
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].action_taken, ContainerLifecycleAction::Destroy);
}

#[test]
fn orch_4_record_failure_does_not_abort_the_launch() {
    // SRS-ORCH-004: the version record is best-effort. Once the
    // container is running, a registry-record failure must NOT
    // retroactively abort the launch — that would lie to operators
    // and force a destroy. The typed DeployedVersionRegistryError
    // surface lets concrete registries / wrapping callers observe
    // the failure separately from the launch outcome.
    struct FlakyRegistry;
    impl DeployedVersionRegistry for FlakyRegistry {
        fn record(
            &self,
            _strategy_id: &StrategyId,
            _version: DeployedVersion,
        ) -> Result<(), DeployedVersionRegistryError> {
            Err(DeployedVersionRegistryError::new(
                "simulated durable-store outage",
            ))
        }
        fn lookup(
            &self,
            _strategy_id: &StrategyId,
        ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError> {
            Ok(None)
        }
    }
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 4_200,
        },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = FlakyRegistry;
    let outcome = orchestrator
        .launch(
            request_with_hash("alpha-1", StrategyMode::Live, TEST_SOURCE_HASH),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect("record failure must not abort the launch");
    assert_eq!(
        outcome.deployed_version.source_hash.as_str(),
        TEST_SOURCE_HASH
    );
    assert_eq!(runtime.create_calls.get(), 1);
    assert_eq!(runtime.start_calls.get(), 1);
}

#[test]
fn orch_4_distinct_strategies_carry_distinct_version_records() {
    // The registry's read path must discriminate by strategy_id —
    // two distinct strategies deployed in the same session must
    // surface distinct version_identifier strings to the dashboard
    // / REST API. The lookup-by-strategy_id semantics are pinned
    // here.
    let orchestrator = StrategyOrchestrator;
    let runtime = RuntimeSpy::new(
        LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 4_200,
        },
        ContainerHealthState::Healthy,
    );
    let sink = ForbiddenSink;
    let version_registry = VersionRegistrySpy::default();
    orchestrator
        .launch(
            request_with_hash("alpha-1", StrategyMode::Live, TEST_SOURCE_HASH),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_000,
        )
        .expect("first launch accepts");
    orchestrator
        .launch(
            request_with_hash("beta-1", StrategyMode::Paper, TEST_SOURCE_HASH_BETA),
            &runtime,
            &sink,
            &version_registry,
            1_715_700_100,
        )
        .expect("second launch accepts");
    let alpha = version_registry
        .lookup(&StrategyId::new("alpha-1"))
        .expect("alpha lookup")
        .expect("alpha record present");
    let beta = version_registry
        .lookup(&StrategyId::new("beta-1"))
        .expect("beta lookup")
        .expect("beta record present");
    assert_eq!(alpha.source_hash.as_str(), TEST_SOURCE_HASH);
    assert_eq!(beta.source_hash.as_str(), TEST_SOURCE_HASH_BETA);
    assert_ne!(alpha.version_identifier(), beta.version_identifier());
}
