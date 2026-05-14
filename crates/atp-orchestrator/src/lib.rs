use atp_types::{
    ContainerHealthEvent, ContainerHealthState, ContainerLifecycleAction, LaunchReadiness,
    RuntimeService, StrategyId, StrategyLaunchOutcome, StrategyLaunchRequest,
    StructuredOrchestratorError, STRATEGY_STARTUP_DEADLINE_MS,
};

#[derive(Debug, Default)]
pub struct StrategyOrchestrator;

// --------------------------------------------------------------------------- //
// Strategy container lifecycle ports
// (SRS-ORCH-001, SyRS SYS-10 / SYS-13 / AC-12 / NFR-P9 / NFR-R5 / NFR-S5)
// --------------------------------------------------------------------------- //
//
// The orchestrator owns strategy container lifecycle per SRS-5.2 line
// "Strategy Orchestrator". AC-12 narrows ownership to "the Strategy
// Orchestrator shall be the sole component that manages container
// lifecycle. No strategy shall directly manage its own container or
// other containers." The orchestrator therefore mediates every call
// through two ports:
//
//   * `StrategyContainerRuntime` — the lifecycle / health-probe port.
//     Six methods: create, start, stop, restart, destroy, health. The
//     `start` call returns a `LaunchReadiness` discriminating between
//     `ReadyWithinDeadline` and `DeadlineExceeded` so the orchestrator
//     can match on NFR-P9 without re-measuring elapsed time itself
//     (the runtime is the timing source of truth).
//
//   * `HealthCheckEventSink` — the structured-event publication
//     channel. Concrete sinks (deferred to SRS-LOG-001 + SRS-NOTIF-001 +
//     SYS-13 dashboard clause) route events to the audit log, the
//     dashboard WebSocket alert pane (STRATEGY_STATE / ALERTS channel),
//     and the notification dispatcher.
//
// Both traits live in `atp-orchestrator` (not `atp-types`) because the
// consumer — `StrategyOrchestrator::launch` and
// `StrategyOrchestrator::observe_health` — lives here. Placing them in
// `atp-types` would force the type crate to know about ports, inverting
// the dependency direction (SRS-ARCH-002). Lower-layer crates (data,
// strategy-engine, execution, simulation, market-data, factor-pipeline,
// notification) MUST NOT import `atp_orchestrator` — the AC-12 "no
// strategy may manage containers" boundary is statically enforced by
// `tools/dependency_boundary_check.py` listing `atp_orchestrator` in
// the `forbidden_imports` allowlist.
pub trait StrategyContainerRuntime {
    /// Pre-stage the container image and resource profile (SRS-ORCH-002
    /// resource limits applied here on the concrete impl). Read-only
    /// with respect to the running set: `create` does not mark the
    /// container ready for traffic — that's `start`.
    fn create(&self, request: &StrategyLaunchRequest);

    /// Start the container and probe readiness. Returns
    /// `ReadyWithinDeadline { elapsed_millis }` if the strategy reaches
    /// the `ready` state within `request.deadline_millis`, or
    /// `DeadlineExceeded { elapsed_millis, deadline_millis }` if the
    /// deadline is breached. The orchestrator's `launch` gate matches
    /// on the returned `LaunchReadiness` to honour NFR-P9 without
    /// re-implementing the timing.
    fn start(&self, request: &StrategyLaunchRequest) -> LaunchReadiness;

    /// Stop the container in the canonical SYS-10 sense (graceful
    /// shutdown). No effect on persisted state.
    fn stop(&self, strategy_id: &StrategyId);

    /// Restart the container. The SYS-13 auto-restart guard calls this
    /// exclusively on the `Unresponsive` branch of `observe_health`.
    fn restart(&self, strategy_id: &StrategyId);

    /// Destroy the container, releasing its resource profile back to
    /// the host (the SRS-ORCH-002 / SyRS SYS-57 release path).
    fn destroy(&self, strategy_id: &StrategyId);

    /// Read-only health probe. Returns `Healthy` if the container
    /// responds to the orchestrator's liveness ping within the
    /// configured probe window, or `Unresponsive` otherwise. SYS-13's
    /// auto-restart guarantee depends on this probe distinguishing
    /// the two states cleanly.
    fn health(&self, strategy_id: &StrategyId) -> ContainerHealthState;
}

pub trait HealthCheckEventSink {
    fn record(&self, event: ContainerHealthEvent);
}

impl StrategyOrchestrator {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::StrategyOrchestrator
    }

    pub fn owns_strategy_container_lifecycle(&self) -> bool {
        true
    }

    /// SRS-ORCH-001 / NFR-P9 launch gate. Matches on the runtime port's
    /// `LaunchReadiness` classification of the launch; `ReadyWithinDeadline`
    /// returns `StrategyLaunchOutcome`; `DeadlineExceeded` emits a structured
    /// `ContainerHealthEvent` through the sink AND returns a
    /// `StructuredOrchestratorError` whose category is
    /// `OrderErrorCategory::StrategyStartupDeadlineExceeded` (wire string
    /// `STRATEGY_STARTUP_DEADLINE_EXCEEDED`).
    ///
    /// **Invariants** (statically checked by
    /// `tools/orchestrator_lifecycle_check.py`):
    ///
    /// * The `DeadlineExceeded` arm MUST call `runtime.destroy(` —
    ///   the over-deadline container must release its resource profile
    ///   so the SRS-ORCH-002 / SyRS SYS-57 host memory budget is not
    ///   consumed by an orphaned half-launched container.
    /// * The `DeadlineExceeded` arm MUST call `sink.record(` — the
    ///   audit log + dashboard fan-out is the public record of the
    ///   destroy action.
    /// * The `DeadlineExceeded` arm MUST produce
    ///   `OrderErrorCategory::StrategyStartupDeadlineExceeded` (directly
    ///   or via the `StructuredOrchestratorError::startup_deadline_exceeded(`
    ///   factory).
    /// * The `DeadlineExceeded` arm MUST NOT construct
    ///   `StrategyLaunchOutcome` — the over-deadline launch is not an
    ///   acceptance.
    /// * The `DeadlineExceeded` arm MUST NOT mutate the orchestrator's
    ///   own container registry behind the runtime port (no
    ///   `containers.insert(`, `registry.add(`, `self.spawn_container(`,
    ///   etc.) — every mutation must go through the runtime port so the
    ///   AC-12 boundary stays auditable.
    /// * `ReadyWithinDeadline` is the only call site of
    ///   `StrategyLaunchOutcome {`.
    ///
    /// The gate takes no `StrategyMode` parameter at the match — SYS-10
    /// applies the same five-action lifecycle to live and paper
    /// containers (resource profiles differ per SRS-ORCH-002 but the
    /// lifecycle vocabulary is identical), so the launch envelope is
    /// uniform regardless of mode.
    pub fn launch<R, S>(
        &self,
        request: StrategyLaunchRequest,
        runtime: &R,
        sink: &S,
        observed_at_seconds: u64,
    ) -> Result<StrategyLaunchOutcome, StructuredOrchestratorError>
    where
        R: StrategyContainerRuntime,
        S: HealthCheckEventSink,
    {
        runtime.create(&request);
        let readiness = runtime.start(&request);
        match readiness {
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis } => {
                Ok(StrategyLaunchOutcome {
                    strategy_id: request.strategy_id,
                    ready_within_deadline: true,
                    elapsed_millis,
                    deadline_millis: request.deadline_millis,
                })
            }
            LaunchReadiness::DeadlineExceeded {
                elapsed_millis,
                deadline_millis,
            } => {
                // SRS-ORCH-002 / SyRS SYS-57 + NFR-R5: an over-deadline
                // launch must release its resource profile so the host
                // memory safety margin is not consumed by an orphaned
                // half-started container. The `action_taken = Destroy`
                // event payload below is the audit-log record of
                // exactly THIS call. Skipping the destroy would lie to
                // the dashboard about what the orchestrator did.
                runtime.destroy(&request.strategy_id);
                sink.record(ContainerHealthEvent {
                    state: ContainerHealthState::Unresponsive,
                    strategy_id: request.strategy_id.clone(),
                    action_taken: ContainerLifecycleAction::Destroy,
                    observed_at_seconds,
                });
                Err(StructuredOrchestratorError::startup_deadline_exceeded(
                    request,
                    elapsed_millis,
                    deadline_millis,
                ))
            }
        }
    }

    /// SyRS SYS-13 auto-restart gate. Matches on the runtime port's
    /// `health(strategy_id)` classification:
    ///   * `Healthy` returns the observed state without side effects —
    ///     no restart, no event (preserves dashboard accuracy by
    ///     never injecting an action for a healthy probe).
    ///   * `Unresponsive` calls `runtime.restart(strategy_id)` AND
    ///     publishes a `ContainerHealthEvent` carrying
    ///     `action_taken = Restart` through the sink.
    ///
    /// **Invariants** (statically checked by
    /// `tools/orchestrator_lifecycle_check.py`):
    ///
    /// * The `Unresponsive` arm MUST call BOTH
    ///   `runtime.restart(` AND `sink.record(`. SYS-13 binds the
    ///   restart action AND the dashboard fan-out into one transaction.
    /// * The `Healthy` arm MUST NOT call `runtime.restart(`,
    ///   `runtime.destroy(`, `runtime.stop(`, or `sink.record(` — a
    ///   healthy probe is read-only.
    pub fn observe_health<R, S>(
        &self,
        strategy_id: StrategyId,
        runtime: &R,
        sink: &S,
        observed_at_seconds: u64,
    ) -> ContainerHealthState
    where
        R: StrategyContainerRuntime,
        S: HealthCheckEventSink,
    {
        let state = runtime.health(&strategy_id);
        match state {
            ContainerHealthState::Healthy => state,
            ContainerHealthState::Unresponsive => {
                runtime.restart(&strategy_id);
                sink.record(ContainerHealthEvent {
                    state,
                    strategy_id,
                    action_taken: ContainerLifecycleAction::Restart,
                    observed_at_seconds,
                });
                state
            }
        }
    }

    /// Read-only constant accessor for NFR-P9's startup-time ceiling.
    /// Exposed so callers can populate `StrategyLaunchRequest.deadline_millis`
    /// without reaching into `atp_types` directly.
    pub const fn startup_deadline_millis(&self) -> u64 {
        STRATEGY_STARTUP_DEADLINE_MS
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::StrategyMode;
    use std::cell::{Cell, RefCell};

    struct RuntimeStub {
        readiness: LaunchReadiness,
        health_state: ContainerHealthState,
        create_calls: Cell<u32>,
        start_calls: Cell<u32>,
        stop_calls: Cell<u32>,
        restart_calls: Cell<u32>,
        destroy_calls: Cell<u32>,
        health_calls: Cell<u32>,
    }

    impl RuntimeStub {
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

    impl StrategyContainerRuntime for RuntimeStub {
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
    struct SinkSpy {
        events: RefCell<Vec<ContainerHealthEvent>>,
    }

    impl HealthCheckEventSink for SinkSpy {
        fn record(&self, event: ContainerHealthEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    struct ForbiddenSink;

    impl HealthCheckEventSink for ForbiddenSink {
        fn record(&self, _event: ContainerHealthEvent) {
            panic!("Healthy / ReadyWithinDeadline must not record a ContainerHealthEvent");
        }
    }

    fn request(id: &str, mode: StrategyMode) -> StrategyLaunchRequest {
        StrategyLaunchRequest {
            strategy_id: StrategyId::new(id),
            mode,
            deployment_hash: "sha256:abc".to_string(),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        }
    }

    #[test]
    fn identifies_strategy_orchestrator() {
        let orchestrator = StrategyOrchestrator;
        assert_eq!(orchestrator.service(), RuntimeService::StrategyOrchestrator);
        assert!(orchestrator.owns_strategy_container_lifecycle());
        assert_eq!(orchestrator.startup_deadline_millis(), 30_000);
    }

    #[test]
    fn ready_within_deadline_returns_outcome_and_emits_no_event() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 4_200 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let outcome = orchestrator
            .launch(request("alpha-1", StrategyMode::Live), &runtime, &sink, 1_715_000_000)
            .expect("ReadyWithinDeadline must accept the launch");
        assert_eq!(outcome.strategy_id.as_str(), "alpha-1");
        assert!(outcome.ready_within_deadline);
        assert_eq!(outcome.elapsed_millis, 4_200);
        assert_eq!(outcome.deadline_millis, 30_000);
        assert_eq!(runtime.create_calls.get(), 1);
        assert_eq!(runtime.start_calls.get(), 1);
    }

    #[test]
    fn deadline_exceeded_rejects_with_startup_deadline_exceeded() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::DeadlineExceeded {
                elapsed_millis: 32_500,
                deadline_millis: 30_000,
            },
            ContainerHealthState::Healthy,
        );
        let sink = SinkSpy::default();
        let error = orchestrator
            .launch(request("alpha-1", StrategyMode::Live), &runtime, &sink, 1_715_000_000)
            .expect_err("DeadlineExceeded must refuse the launch");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::StrategyStartupDeadlineExceeded
        );
        assert_eq!(error.category.as_str(), "STRATEGY_STARTUP_DEADLINE_EXCEEDED");
        assert_eq!(error.original_request.strategy_id.as_str(), "alpha-1");
        // SRS-ORCH-002 / SyRS SYS-57: the over-deadline container must
        // be destroyed so the host memory budget is not consumed by an
        // orphan. The event below claims `action_taken = Destroy`; this
        // assertion proves the claim is honest.
        assert_eq!(
            runtime.destroy_calls.get(),
            1,
            "DeadlineExceeded must invoke runtime.destroy exactly once"
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one event per refused launch");
        assert_eq!(events[0].state, ContainerHealthState::Unresponsive);
        assert_eq!(events[0].action_taken, ContainerLifecycleAction::Destroy);
        assert_eq!(events[0].strategy_id.as_str(), "alpha-1");
        assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn healthy_observation_emits_no_event_and_no_restart() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let state = orchestrator.observe_health(
            StrategyId::new("alpha-1"),
            &runtime,
            &sink,
            1_715_000_000,
        );
        assert_eq!(state, ContainerHealthState::Healthy);
        assert_eq!(runtime.health_calls.get(), 1);
        assert_eq!(
            runtime.restart_calls.get(),
            0,
            "Healthy probe must not invoke restart"
        );
        assert_eq!(runtime.destroy_calls.get(), 0);
        assert_eq!(runtime.stop_calls.get(), 0);
    }

    #[test]
    fn unresponsive_observation_restarts_and_records_event() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Unresponsive,
        );
        let sink = SinkSpy::default();
        let state = orchestrator.observe_health(
            StrategyId::new("alpha-1"),
            &runtime,
            &sink,
            1_715_000_000,
        );
        assert_eq!(state, ContainerHealthState::Unresponsive);
        assert_eq!(runtime.health_calls.get(), 1);
        assert_eq!(
            runtime.restart_calls.get(),
            1,
            "Unresponsive observation must trigger exactly one restart"
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one dashboard event per restart");
        assert_eq!(events[0].state, ContainerHealthState::Unresponsive);
        assert_eq!(events[0].action_taken, ContainerLifecycleAction::Restart);
        assert_eq!(events[0].strategy_id.as_str(), "alpha-1");
        assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn observe_health_consults_runtime_exactly_once_per_probe() {
        // Sanity check: the gate must consult `health` exactly once per
        // call. A future refactor that double-probes would double-emit
        // restart events and distort dashboard counts.
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Unresponsive,
        );
        let sink = SinkSpy::default();
        let _ = orchestrator.observe_health(
            StrategyId::new("alpha-1"),
            &runtime,
            &sink,
            1_715_000_000,
        );
        assert_eq!(runtime.health_calls.get(), 1);
        assert_eq!(runtime.restart_calls.get(), 1);
    }
}
