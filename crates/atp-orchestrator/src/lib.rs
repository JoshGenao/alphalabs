use atp_types::{
    ContainerHealthEvent, ContainerHealthState, ContainerLifecycleAction, LaunchReadiness,
    ResourceProfile, ResourceProfileError, RuntimeService, StrategyId, StrategyLaunchOutcome,
    StrategyLaunchRequest, StrategyMode, StructuredOrchestratorError,
    RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS, RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS,
    STRATEGY_STARTUP_DEADLINE_MS,
};
use std::fmt;

#[derive(Debug, Default)]
pub struct StrategyOrchestrator;

/// SRS-ORCH-002: resource-profile env-var resolution errors. Wraps
/// parse failures (an env var was set but cannot be parsed as the
/// expected numeric type) and validation failures (the parsed value
/// is outside the SRS-ARCH-005 catalogue range). The two are kept
/// distinct because parse failures are operator-typo / catalogue-bypass
/// signals while validation failures are out-of-range overrides — the
/// dashboard / readiness check should be able to render them
/// differently.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResourceProfileEnvError {
    /// `var` was set to `raw_value`, which does not parse as a u32.
    UnparseableMem { var: &'static str, raw_value: String },
    /// `var` was set to `raw_value`, which does not parse as an f64
    /// (CPU cores).
    UnparseableCpu { var: &'static str, raw_value: String },
    /// The parsed-and-converted profile failed `ResourceProfile::validate`.
    Validation(ResourceProfileError),
}

impl From<ResourceProfileError> for ResourceProfileEnvError {
    fn from(error: ResourceProfileError) -> Self {
        Self::Validation(error)
    }
}

impl fmt::Display for ResourceProfileEnvError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::UnparseableMem { var, raw_value } => write!(
                formatter,
                "SRS-ORCH-002 env-var override {var}={raw_value:?} is not a valid u32 (megabytes)"
            ),
            Self::UnparseableCpu { var, raw_value } => write!(
                formatter,
                "SRS-ORCH-002 env-var override {var}={raw_value:?} is not a valid f64 (CPU cores)"
            ),
            Self::Validation(error) => write!(formatter, "SRS-ORCH-002 validation: {error}"),
        }
    }
}

impl std::error::Error for ResourceProfileEnvError {}

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
        // SRS-ORCH-002 + SyRS SYS-11: validate the resource profile at
        // the orchestrator boundary BEFORE invoking the runtime port.
        // A misconfigured override (out-of-range mem / CPU) must never
        // reach `runtime.create` — there is no container to destroy
        // because none was ever created, so this rejection is a pure
        // structured error with NO sink event (an event would lie about
        // a destroy that never happened). The contract check
        // (`tools/orchestrator_resource_profile_check.py`) statically
        // enforces the validate-before-create ordering.
        if let Err(violation) = request.profile.validate() {
            return Err(StructuredOrchestratorError::resource_profile_invalid(
                request, violation,
            ));
        }
        runtime.create(&request);
        let readiness = runtime.start(&request);
        match readiness {
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis } => {
                Ok(StrategyLaunchOutcome {
                    strategy_id: request.strategy_id,
                    ready_within_deadline: true,
                    elapsed_millis,
                    deadline_millis: request.deadline_millis,
                    profile: request.profile,
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

    /// SRS-ORCH-002 / SyRS SYS-11 default live-container resource profile
    /// (512 MB / 0.25 CPU). Re-exported here so callers populating a
    /// `StrategyLaunchRequest.profile` field do not have to reach into
    /// `atp_types` — the orchestrator owns the lifecycle boundary.
    pub const fn live_profile_default(&self) -> ResourceProfile {
        ResourceProfile::live_default()
    }

    /// SRS-ORCH-002 / SyRS SYS-11 default paper-container resource profile
    /// (300 MB / 0.10 CPU). See `live_profile_default` for rationale.
    pub const fn paper_profile_default(&self) -> ResourceProfile {
        ResourceProfile::paper_default()
    }

    /// Mode-keyed dispatch: live → live default; paper → paper default.
    /// Forwarded so the orchestrator boundary owns the selection rule.
    pub const fn profile_for_mode(&self, mode: StrategyMode) -> ResourceProfile {
        ResourceProfile::for_mode(mode)
    }

    /// SRS-ORCH-002 / SyRS SYS-11: build a `ResourceProfile` for `mode`
    /// from the ATP_*_STRATEGY_* configuration values supplied by
    /// `lookup`. Falls back to the SyRS spec-literal default ONLY when
    /// the lookup returns `None`. A set-but-unparseable value is
    /// returned as a structured `ResourceProfileEnvError` — silently
    /// substituting a default for an invalid override would be the
    /// opposite of "configuration overrides are validated." The
    /// resulting profile is then validated against the SRS-ARCH-005
    /// catalogue bounds (defence in depth so a programmatically-
    /// constructed override that bypassed the catalogue is still
    /// rejected before it reaches the runtime port).
    ///
    /// Splitting the lookup out as a generic argument keeps this
    /// function pure and testable — callers can drive it with a stub
    /// map; the production wrapper `profile_for_mode_from_env` plugs in
    /// `std::env::var` for the same shape.
    pub fn profile_for_mode_from_lookup<F>(
        &self,
        mode: StrategyMode,
        lookup: F,
    ) -> Result<ResourceProfile, ResourceProfileEnvError>
    where
        F: Fn(&str) -> Option<String>,
    {
        let default = ResourceProfile::for_mode(mode);
        let (mem_var, cpu_var) = match mode {
            StrategyMode::Live => ("ATP_LIVE_STRATEGY_MEM_MB", "ATP_LIVE_STRATEGY_CPU"),
            StrategyMode::Paper => ("ATP_PAPER_STRATEGY_MEM_MB", "ATP_PAPER_STRATEGY_CPU"),
        };
        let mem_mb = match lookup(mem_var) {
            Some(raw) => raw.parse::<u32>().map_err(|_| {
                ResourceProfileEnvError::UnparseableMem {
                    var: mem_var,
                    raw_value: raw,
                }
            })?,
            None => default.mem_mb,
        };
        // CPU is stored in the catalogue as a float (cores) and on the
        // orchestrator as integer hundredths; convert at this boundary.
        // The contract check (config_catalogue_binding) statically
        // verifies the catalogue defaults convert to the constants so a
        // future tuning change touches both sides consistently.
        let cpu_hundredths = match lookup(cpu_var) {
            Some(raw) => {
                let cores = raw.parse::<f64>().map_err(|_| {
                    ResourceProfileEnvError::UnparseableCpu {
                        var: cpu_var,
                        raw_value: raw.clone(),
                    }
                })?;
                // Compare cores against the f64 catalogue bounds
                // BEFORE rounding to integer hundredths. A naive
                // `(cores * 100.0).round() as u32` would let 0.046
                // round up to 5 (silently passing the floor) and
                // 16.004 round down to 1600 (silently passing the
                // ceiling). The f64-side check rejects both before
                // the conversion. The tolerance below absorbs binary-
                // float representation noise (e.g. 0.05 ≠ 0.05 exactly
                // in IEEE-754); 1e-9 is well below the catalogue's
                // intended granularity of 0.01 cores.
                let cpu_floor_cores = RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS as f64 / 100.0;
                let cpu_ceiling_cores = RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS as f64 / 100.0;
                if cores < cpu_floor_cores - 1e-9 {
                    return Err(ResourceProfileEnvError::Validation(
                        ResourceProfileError::CpuBelowFloor {
                            cpu_hundredths: (cores * 100.0).max(0.0).floor() as u32,
                            floor_hundredths: RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS,
                        },
                    ));
                }
                if cores > cpu_ceiling_cores + 1e-9 {
                    return Err(ResourceProfileEnvError::Validation(
                        ResourceProfileError::CpuAboveCeiling {
                            cpu_hundredths: (cores * 100.0).ceil() as u32,
                            ceiling_hundredths: RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS,
                        },
                    ));
                }
                (cores * 100.0).round() as u32
            }
            None => default.cpu_hundredths,
        };
        let profile = ResourceProfile { mem_mb, cpu_hundredths };
        profile.validate()?;
        Ok(profile)
    }

    /// SRS-ORCH-002 testable env-var bridge. Resolves the
    /// ATP_*_STRATEGY_* values via `env_lookup` and feeds them into
    /// `profile_for_mode_from_lookup`, distinguishing
    /// `VarError::NotPresent` (silently falls back to the SyRS default)
    /// from `VarError::NotUnicode` (returned as a structured
    /// `Unparseable*` error — a set-but-invalid override must NEVER
    /// silently collapse to the default; that is the opposite of
    /// "configuration overrides are validated"). Callers that already
    /// have parsed `Option<String>` values should use
    /// `profile_for_mode_from_lookup` directly.
    pub fn profile_for_mode_via_env_lookup<F>(
        &self,
        mode: StrategyMode,
        env_lookup: F,
    ) -> Result<ResourceProfile, ResourceProfileEnvError>
    where
        F: Fn(&str) -> Result<String, std::env::VarError>,
    {
        let (mem_var, cpu_var) = match mode {
            StrategyMode::Live => ("ATP_LIVE_STRATEGY_MEM_MB", "ATP_LIVE_STRATEGY_CPU"),
            StrategyMode::Paper => ("ATP_PAPER_STRATEGY_MEM_MB", "ATP_PAPER_STRATEGY_CPU"),
        };
        let mem_value = match env_lookup(mem_var) {
            Ok(value) => Some(value),
            Err(std::env::VarError::NotPresent) => None,
            Err(std::env::VarError::NotUnicode(raw)) => {
                return Err(ResourceProfileEnvError::UnparseableMem {
                    var: mem_var,
                    raw_value: raw.to_string_lossy().into_owned(),
                });
            }
        };
        let cpu_value = match env_lookup(cpu_var) {
            Ok(value) => Some(value),
            Err(std::env::VarError::NotPresent) => None,
            Err(std::env::VarError::NotUnicode(raw)) => {
                return Err(ResourceProfileEnvError::UnparseableCpu {
                    var: cpu_var,
                    raw_value: raw.to_string_lossy().into_owned(),
                });
            }
        };
        self.profile_for_mode_from_lookup(mode, |name| {
            if name == mem_var {
                mem_value.clone()
            } else if name == cpu_var {
                cpu_value.clone()
            } else {
                None
            }
        })
    }

    /// SRS-ORCH-002 production env-var wrapper. Plugs `std::env::var`
    /// into `profile_for_mode_via_env_lookup` (the closure wrapper
    /// gives the type-checker an explicit `for<'a> Fn(&'a str)`
    /// higher-ranked lifetime — passing the bare `std::env::var`
    /// trips lifetime inference on stable Rust).
    pub fn profile_for_mode_from_env(
        &self,
        mode: StrategyMode,
    ) -> Result<ResourceProfile, ResourceProfileEnvError> {
        self.profile_for_mode_via_env_lookup(mode, |name: &str| std::env::var(name))
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
            profile: ResourceProfile::for_mode(mode),
        }
    }

    fn request_with_profile(
        id: &str,
        mode: StrategyMode,
        profile: ResourceProfile,
    ) -> StrategyLaunchRequest {
        StrategyLaunchRequest {
            strategy_id: StrategyId::new(id),
            mode,
            deployment_hash: "sha256:abc".to_string(),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile,
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

    // ---------------------------------------------------------------- //
    // SRS-ORCH-002 resource profile launch validation
    // ---------------------------------------------------------------- //

    #[test]
    fn launch_threads_request_profile_through_to_outcome() {
        // SRS-ORCH-002 evidence: the outcome must carry the profile the
        // request supplied — not a re-defaulted value at the gate. The
        // contract check enforces `outcome.profile == request.profile`
        // statically; this test anchors it behaviourally.
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 4_200 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let custom = ResourceProfile {
            mem_mb: 384,
            cpu_hundredths: 20,
        };
        let outcome = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, custom),
                &runtime,
                &sink,
                1_715_000_000,
            )
            .expect("in-range custom profile must be accepted");
        assert_eq!(outcome.profile, custom);
    }

    #[test]
    fn launch_rejects_below_floor_memory_without_invoking_runtime() {
        // A misconfigured override must never reach `runtime.create`
        // (let alone `runtime.start`). The spy counters prove the
        // rejection short-circuited at the validation gate.
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let bad = ResourceProfile {
            mem_mb: 32,
            cpu_hundredths: 25,
        };
        let error = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, bad),
                &runtime,
                &sink,
                1_715_000_000,
            )
            .expect_err("below-floor mem must be refused");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::ResourceProfileInvalid
        );
        assert_eq!(error.error_type, "ResourceProfileInvalid::MemBelowFloor");
        assert_eq!(error.original_request.profile, bad);
        assert_eq!(runtime.create_calls.get(), 0, "validation gate must short-circuit");
        assert_eq!(runtime.start_calls.get(), 0);
        assert_eq!(runtime.destroy_calls.get(), 0);
    }

    #[test]
    fn launch_rejects_above_ceiling_cpu_without_invoking_runtime() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let bad = ResourceProfile {
            mem_mb: 512,
            cpu_hundredths: 9_999,
        };
        let error = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, bad),
                &runtime,
                &sink,
                1_715_000_000,
            )
            .expect_err("above-ceiling cpu must be refused");
        assert_eq!(error.error_type, "ResourceProfileInvalid::CpuAboveCeiling");
        assert_eq!(runtime.create_calls.get(), 0);
        assert_eq!(runtime.start_calls.get(), 0);
    }

    #[test]
    fn live_and_paper_default_profiles_are_distinct_per_sys_11() {
        let orchestrator = StrategyOrchestrator;
        let live = orchestrator.live_profile_default();
        let paper = orchestrator.paper_profile_default();
        assert_eq!(live.mem_mb, 512);
        assert_eq!(live.cpu_hundredths, 25);
        assert_eq!(paper.mem_mb, 300);
        assert_eq!(paper.cpu_hundredths, 10);
        assert_ne!(live, paper);
        assert_eq!(orchestrator.profile_for_mode(StrategyMode::Live), live);
        assert_eq!(orchestrator.profile_for_mode(StrategyMode::Paper), paper);
    }

    fn empty_lookup(_name: &str) -> Option<String> {
        None
    }

    #[test]
    fn profile_for_mode_from_lookup_returns_default_when_lookup_is_empty() {
        let orchestrator = StrategyOrchestrator;
        let live = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, empty_lookup)
            .expect("default must validate");
        assert_eq!(live, ResourceProfile::live_default());
        let paper = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Paper, empty_lookup)
            .expect("default must validate");
        assert_eq!(paper, ResourceProfile::paper_default());
    }

    #[test]
    fn profile_for_mode_from_lookup_applies_in_range_overrides() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_MEM_MB" => Some("768".to_string()),
                "ATP_LIVE_STRATEGY_CPU" => Some("0.5".to_string()),
                _ => None,
            }
        };
        let profile = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect("in-range override must validate");
        assert_eq!(profile.mem_mb, 768);
        // 0.5 cores → 50 hundredths after the cores → hundredths conversion.
        assert_eq!(profile.cpu_hundredths, 50);
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_unparseable_mem_value() {
        // SRS-ORCH-002 "configuration overrides are validated" — a
        // set-but-unparseable override is an operator error, not a
        // signal to silently apply the default. The orchestrator
        // returns a structured ResourceProfileEnvError so a future
        // dashboard / readiness check can surface the bad value.
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_MEM_MB" => Some("not-a-number".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect_err("unparseable mem must be rejected");
        match err {
            ResourceProfileEnvError::UnparseableMem { var, raw_value } => {
                assert_eq!(var, "ATP_LIVE_STRATEGY_MEM_MB");
                assert_eq!(raw_value, "not-a-number");
            }
            other => panic!("expected UnparseableMem, got {other:?}"),
        }
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_unparseable_cpu_value() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_PAPER_STRATEGY_CPU" => Some("not-a-float".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Paper, lookup)
            .expect_err("unparseable cpu must be rejected");
        match err {
            ResourceProfileEnvError::UnparseableCpu { var, raw_value } => {
                assert_eq!(var, "ATP_PAPER_STRATEGY_CPU");
                assert_eq!(raw_value, "not-a-float");
            }
            other => panic!("expected UnparseableCpu, got {other:?}"),
        }
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_below_floor_override() {
        // SRS-ORCH-002 defence-in-depth: even if the catalogue accepted
        // a value (it would not), the orchestrator's validate() catches
        // out-of-range overrides before they reach the launch envelope.
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_PAPER_STRATEGY_MEM_MB" => Some("16".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Paper, lookup)
            .expect_err("below-floor mem must be rejected");
        assert!(matches!(
            err,
            ResourceProfileEnvError::Validation(ResourceProfileError::MemBelowFloor { .. })
        ));
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_above_ceiling_cpu() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_CPU" => Some("32.0".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect_err("above-ceiling cpu must be rejected");
        assert!(matches!(
            err,
            ResourceProfileEnvError::Validation(ResourceProfileError::CpuAboveCeiling { .. })
        ));
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_just_below_cpu_floor() {
        // Boundary case: 0.046 cores → rounded to 5 hundredths would
        // silently pass the integer-side validation, but 0.046 < 0.05
        // (the catalogue's f64 floor) — so the cores-side check must
        // catch this BEFORE the round-to-hundredths conversion.
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_CPU" => Some("0.046".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect_err("0.046 cores is below the catalogue floor of 0.05");
        assert!(matches!(
            err,
            ResourceProfileEnvError::Validation(ResourceProfileError::CpuBelowFloor { .. })
        ));
    }

    #[test]
    fn profile_for_mode_from_lookup_rejects_just_above_cpu_ceiling() {
        // Boundary case: 16.004 cores → rounded to 1600 hundredths
        // would silently pass the integer-side validation, but
        // 16.004 > 16.0 (the catalogue's f64 ceiling).
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_CPU" => Some("16.004".to_string()),
                _ => None,
            }
        };
        let err = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect_err("16.004 cores is above the catalogue ceiling of 16.0");
        assert!(matches!(
            err,
            ResourceProfileEnvError::Validation(ResourceProfileError::CpuAboveCeiling { .. })
        ));
    }

    #[test]
    fn profile_for_mode_from_lookup_accepts_exact_floor_cpu() {
        // The exact catalogue floor (0.05 cores) must be accepted; the
        // 1e-9 tolerance in the bounds-check absorbs binary-float
        // representation noise without rejecting the floor itself.
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_CPU" => Some("0.05".to_string()),
                _ => None,
            }
        };
        let profile = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect("exact floor must be accepted");
        assert_eq!(profile.cpu_hundredths, 5);
    }

    #[test]
    fn profile_for_mode_from_lookup_accepts_exact_ceiling_cpu() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| -> Option<String> {
            match name {
                "ATP_LIVE_STRATEGY_CPU" => Some("16.0".to_string()),
                _ => None,
            }
        };
        let profile = orchestrator
            .profile_for_mode_from_lookup(StrategyMode::Live, lookup)
            .expect("exact ceiling must be accepted");
        assert_eq!(profile.cpu_hundredths, 1600);
    }

    #[test]
    fn profile_for_mode_via_env_lookup_treats_not_present_as_default() {
        // VarError::NotPresent means the operator did not set the
        // override; the env wrapper falls back to the SyRS default.
        let orchestrator = StrategyOrchestrator;
        let env_lookup =
            |_name: &str| -> Result<String, std::env::VarError> {
                Err(std::env::VarError::NotPresent)
            };
        let profile = orchestrator
            .profile_for_mode_via_env_lookup(StrategyMode::Live, env_lookup)
            .expect("NotPresent for both vars must default-to-live");
        assert_eq!(profile, ResourceProfile::live_default());
    }

    #[test]
    fn profile_for_mode_via_env_lookup_rejects_not_unicode_mem() {
        // VarError::NotUnicode means the operator SET the override but
        // it cannot be decoded as UTF-8. This is a "set-but-invalid"
        // case that must surface as a structured Unparseable* error
        // rather than silently collapsing to the default.
        use std::ffi::OsString;
        #[cfg(unix)]
        fn invalid_unicode_osstring() -> OsString {
            use std::os::unix::ffi::OsStringExt;
            OsString::from_vec(vec![0xFF, 0xFE, 0xFD])
        }
        #[cfg(not(unix))]
        fn invalid_unicode_osstring() -> OsString {
            // Windows OsStrings are WTF-16; constructing an unpaired
            // surrogate is the equivalent. The test still exercises
            // the NotUnicode path because the helper accepts any
            // OsString.
            OsString::from("placeholder-non-unicode")
        }
        let orchestrator = StrategyOrchestrator;
        let bad = invalid_unicode_osstring();
        let env_lookup =
            |name: &str| -> Result<String, std::env::VarError> {
                if name == "ATP_LIVE_STRATEGY_MEM_MB" {
                    Err(std::env::VarError::NotUnicode(bad.clone()))
                } else {
                    Err(std::env::VarError::NotPresent)
                }
            };
        let err = orchestrator
            .profile_for_mode_via_env_lookup(StrategyMode::Live, env_lookup)
            .expect_err("non-Unicode mem override must be rejected");
        match err {
            ResourceProfileEnvError::UnparseableMem { var, .. } => {
                assert_eq!(var, "ATP_LIVE_STRATEGY_MEM_MB");
            }
            other => panic!("expected UnparseableMem, got {other:?}"),
        }
    }

    #[test]
    fn profile_for_mode_via_env_lookup_rejects_not_unicode_cpu() {
        use std::ffi::OsString;
        #[cfg(unix)]
        fn invalid_unicode_osstring() -> OsString {
            use std::os::unix::ffi::OsStringExt;
            OsString::from_vec(vec![0xFF])
        }
        #[cfg(not(unix))]
        fn invalid_unicode_osstring() -> OsString {
            OsString::from("placeholder-non-unicode")
        }
        let orchestrator = StrategyOrchestrator;
        let bad = invalid_unicode_osstring();
        let env_lookup =
            |name: &str| -> Result<String, std::env::VarError> {
                if name == "ATP_PAPER_STRATEGY_CPU" {
                    Err(std::env::VarError::NotUnicode(bad.clone()))
                } else {
                    Err(std::env::VarError::NotPresent)
                }
            };
        let err = orchestrator
            .profile_for_mode_via_env_lookup(StrategyMode::Paper, env_lookup)
            .expect_err("non-Unicode cpu override must be rejected");
        match err {
            ResourceProfileEnvError::UnparseableCpu { var, .. } => {
                assert_eq!(var, "ATP_PAPER_STRATEGY_CPU");
            }
            other => panic!("expected UnparseableCpu, got {other:?}"),
        }
    }

    #[test]
    fn profile_for_mode_via_env_lookup_threads_present_values_to_lookup() {
        // Sanity check: an Ok env value flows through to the lookup
        // path the same as a manually-set Some(...) would.
        let orchestrator = StrategyOrchestrator;
        let env_lookup = |name: &str| -> Result<String, std::env::VarError> {
            match name {
                "ATP_LIVE_STRATEGY_MEM_MB" => Ok("768".to_string()),
                _ => Err(std::env::VarError::NotPresent),
            }
        };
        let profile = orchestrator
            .profile_for_mode_via_env_lookup(StrategyMode::Live, env_lookup)
            .expect("present mem override + default cpu must validate");
        assert_eq!(profile.mem_mb, 768);
        assert_eq!(profile.cpu_hundredths, 25);
    }
}
