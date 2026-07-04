use atp_types::{
    ContainerHealthEvent, ContainerHealthState, ContainerLifecycleAction, DeployedVersion,
    DrawdownDemotionTrigger, HostMemorySafetyMargin, HostMemorySafetyMarginError,
    HotSwapDemotionEvent, HotSwapDemotionOutcome, HotSwapDemotionRequest, HotSwapTriggerConfig,
    HotSwapTriggerEvent, HotSwapTriggerKind, HotSwapTriggerProposal, LaunchReadiness,
    LiveStrategyState, OperatorAlertChannel, OperatorAlertEvent, RegisteredWorkload,
    ReservoirRankingSnapshot, ResourceProfile, ResourceProfileError, RuntimeService,
    SideEffectOutcome, StrategyId, StrategyLaunchOutcome, StrategyLaunchRequest, StrategyMode,
    StructuredHotSwapDemotionError, StructuredOrchestratorError, TriggerRationale,
    WorkloadAdmissionEvent, WorkloadAdmissionReason, WorkloadId, WorkloadKind, WorkloadPriority,
    HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT, RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS,
    RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS, STRATEGY_STARTUP_DEADLINE_MS,
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
    UnparseableMem {
        var: &'static str,
        raw_value: String,
    },
    /// `var` was set to `raw_value`, which does not parse as an f64
    /// (CPU cores).
    UnparseableCpu {
        var: &'static str,
        raw_value: String,
    },
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

/// SRS-ORCH-003: host-memory safety margin env-var resolution errors.
/// Mirrors the SRS-ORCH-002 `ResourceProfileEnvError` shape so the
/// dashboard / readiness check can render parse failures (operator
/// typo / catalogue bypass) separately from validation failures
/// (out-of-range overrides).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HostMemorySafetyMarginEnvError {
    /// `var` was set to `raw_value`, which does not parse as a u32.
    Unparseable {
        var: &'static str,
        raw_value: String,
    },
    /// The parsed value failed `HostMemorySafetyMargin::validate`.
    Validation(HostMemorySafetyMarginError),
}

impl From<HostMemorySafetyMarginError> for HostMemorySafetyMarginEnvError {
    fn from(error: HostMemorySafetyMarginError) -> Self {
        Self::Validation(error)
    }
}

impl fmt::Display for HostMemorySafetyMarginEnvError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Unparseable { var, raw_value } => write!(
                formatter,
                "SRS-ORCH-003 env-var override {var}={raw_value:?} is not a valid u32 (megabytes)"
            ),
            Self::Validation(error) => write!(formatter, "SRS-ORCH-003 validation: {error}"),
        }
    }
}

impl std::error::Error for HostMemorySafetyMarginEnvError {}

// --------------------------------------------------------------------------- //
// Workload-priority admission ports
// (SRS-ORCH-003, SyRS SYS-57 / SYS-58)
// --------------------------------------------------------------------------- //
//
// SRS-ORCH-003 requires the orchestrator to admit new workloads against
// the configured host memory safety margin per the SYS-57 hierarchy:
// refuse lower-priority workloads when available memory would dip below
// the margin, and evict the lowest-priority *batch* workload (SYS-58 (b))
// to make room for a higher-priority arriving workload — while NEVER
// terminating the live-trading strategy (SYS-58 last clause).
//
// The admission gate is a separate orchestrator method from `launch`
// (not folded into it) because callers may need to re-check admission
// periodically: SYS-58 (a) "refuse to deploy new strategy containers"
// is decided at deployment time, but SYS-58 (b) eviction can also be
// triggered out-of-band when a higher-priority job arrives at an
// already-busy host. Keeping admission separate also preserves the
// existing `launch` signature so SRS-ORCH-001's lifecycle contract is
// unaffected by this gate. The production flow becomes
// `orchestrator.admit_workload(&request, …)?; orchestrator.launch(request, …)?`.
//
// The gate consumes three NEW ports — `HostMemoryProbe`,
// `WorkloadRegistry`, and `WorkloadEventSink` — all of which live in
// `atp-orchestrator` (not `atp-types`) for the same reason as
// `StrategyContainerRuntime` and `HealthCheckEventSink`: their
// consumer (the gate) lives here, and pushing them into `atp-types`
// would invert the dependency direction (SRS-ARCH-002). Concrete
// implementations of all three remain deferred: the sysinfo-backed
// `HostMemoryProbe`, the Docker-Engine-backed `WorkloadRegistry`, and
// the dashboard / SRS-NOTIF-001 dispatcher wiring of
// `WorkloadEventSink` are all named in
// `architecture/runtime_services.json`'s
// `workload_priority_contract.deferred` list.

/// SRS-ORCH-003 / SyRS SYS-58 (a). Reports the host's currently-available
/// memory in MB. Returns `Result` because the concrete sysinfo /
/// procfs-backed implementation may genuinely fail (sysinfo refresh
/// error, /proc/meminfo unavailable, namespace-unsupported call) — the
/// admission gate cannot safely decide without a valid reading and
/// must fail closed on probe failure rather than admit blindly.
/// Concrete implementations are deferred (no `sysinfo::` /
/// `procfs::` imports inside this crate); tests use stubs.
pub trait HostMemoryProbe {
    fn available_mb(&self) -> Result<u64, HostMemoryProbeError>;
}

/// SRS-ORCH-003 host-memory probe failure surface. Carries a short
/// reason string so the dashboard can render the failure cause; future
/// adapter implementations may add structured variants (e.g.
/// `SysinfoRefreshFailed`, `ProcMeminfoUnavailable`,
/// `NamespaceUnsupported`) when concrete impls land.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostMemoryProbeError {
    pub reason: String,
}

impl HostMemoryProbeError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for HostMemoryProbeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-ORCH-003: HostMemoryProbe::available_mb failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for HostMemoryProbeError {}

/// SRS-ORCH-003 / SyRS SYS-58 (b). The admission gate iterates over
/// `active()` (filtered to `WorkloadKind::Batch`) when arbitration is
/// needed, and calls `terminate(id)` on the lowest-priority batch
/// workload it picks. Both methods return `Result` because the
/// concrete registry implementation (Docker Engine-backed or
/// in-process) may genuinely fail — the gate MUST NOT silently treat
/// a listing failure as "no active workloads" or a failed termination
/// as freed memory. Concrete implementations are deferred; the
/// concrete failure variants will be added when the adapter lands.
pub trait WorkloadRegistry {
    fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError>;
    fn terminate(&self, id: &WorkloadId) -> Result<(), WorkloadTerminationError>;
}

/// SRS-ORCH-003 registry-listing failure surface (e.g. Docker API
/// timeout, in-process registry mutex poisoning, IPC layer
/// disconnection). Carries a short reason string for dashboard
/// rendering; future variants will distinguish Docker / registry
/// specifics when the concrete adapter lands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkloadRegistryError {
    pub reason: String,
}

impl WorkloadRegistryError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for WorkloadRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-ORCH-003: WorkloadRegistry::active failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for WorkloadRegistryError {}

/// SRS-ORCH-003 termination-failure surface. Carries the failed
/// workload id plus a typed reason — registry termination failures
/// (Docker shutdown timeout, cgroup permission denied, registry
/// desync, etc.) must surface the specific cause so the dashboard
/// can render it and operators can diagnose without grepping logs.
/// Future Docker / cgroup-driver variants will be added when the
/// concrete registry lands; until then the reason carries a short
/// human-readable string supplied by the implementation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkloadTerminationError {
    pub workload_id: WorkloadId,
    pub reason: String,
}

impl WorkloadTerminationError {
    pub fn new(workload_id: WorkloadId, reason: impl Into<String>) -> Self {
        Self {
            workload_id,
            reason: reason.into(),
        }
    }
}

impl fmt::Display for WorkloadTerminationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-ORCH-003: WorkloadRegistry::terminate failed for {} — {}",
            self.workload_id.as_str(),
            self.reason,
        )
    }
}

impl std::error::Error for WorkloadTerminationError {}

/// SRS-ORCH-003 / SyRS SYS-58 (c). Dashboard + notification audit
/// channel for admission decisions. Separate trait from
/// `HealthCheckEventSink` because `WorkloadAdmissionEvent` and
/// `ContainerHealthEvent` are disjoint payloads routed to different
/// dispatcher lanes — conflating them would force the dispatcher to
/// peek inside the payload to decide which alert pane to fan to.
///
/// `record` returns `Result` so concrete sink implementations can
/// signal a typed publication failure (queue full, dashboard
/// WebSocket disconnected, audit-log unwritable). The orchestrator's
/// `admit_workload` gate treats emission as **best-effort** by
/// design: the admission decision is irreversible once made (the
/// host is already in the post-decision state), so a sink failure
/// does not abort or roll back the decision. Concrete dispatcher
/// implementations are responsible for durable delivery semantics
/// (bounded queue + retry, persistent journal, fan-out to a
/// secondary channel, etc.) — those concerns belong on the
/// SRS-LOG-001 / SRS-NOTIF-001 / SYS-13 dispatcher block (deferred).
/// The typed `WorkloadEventSinkError` exists so a future caller
/// wrapping the orchestrator can observe sink failures separately
/// from admission outcomes without retrofitting the trait.
pub trait WorkloadEventSink {
    fn record(&self, event: WorkloadAdmissionEvent) -> Result<(), WorkloadEventSinkError>;
}

/// SRS-ORCH-003 event-sink failure surface (dispatcher queue full,
/// dashboard WebSocket disconnected, audit log file unwritable, etc.).
/// Carries a short reason string for now; future variants will be
/// added when the concrete dispatcher lands. The orchestrator's
/// `admit_workload` gate emits alerts as best-effort and does NOT
/// itself capture or surface these errors — the typed surface exists
/// so concrete sink implementations and wrapping callers (a logger
/// that retries, a metrics adapter that counts dropped alerts, the
/// deferred SRS-NOTIF-001 dispatcher with its own durable-delivery
/// semantics) can observe and act on them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkloadEventSinkError {
    pub reason: String,
}

impl WorkloadEventSinkError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for WorkloadEventSinkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-ORCH-003: WorkloadEventSink::record failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for WorkloadEventSinkError {}

// --------------------------------------------------------------------------- //
// Deployed-version registry port (SRS-ORCH-004, SyRS SYS-79 / SYS-41 /
// IF-9 / SYS-21)
// --------------------------------------------------------------------------- //
//
// SRS-ORCH-004 says the orchestrator must record the deployed code
// version for each strategy instance, and that the same version
// identifier must be queryable from the dashboard (SYS-41), the REST
// API (IF-9), and recorded onto backtest result rows (SYS-21).
// `DeployedVersionRegistry` is the port the orchestrator's `launch`
// gate calls on successful deployment. `record` is the write path
// and `lookup` is the read path — concrete implementations of this
// trait will sit behind the dashboard, the REST API handler, and
// the backtest pipeline (all deferred).
//
// Both methods return `Result` because concrete implementations may
// genuinely fail (durable store unavailable, file-system permission,
// in-process registry desync). The orchestrator's `launch` gate
// treats a `record` failure as best-effort — once the container is
// running, refusing the launch retroactively would lie to operators
// and force the orchestrator to also destroy the running container.
// The typed `DeployedVersionRegistryError` lets concrete registries
// and wrapping callers observe the failure separately from the
// launch outcome.
pub trait DeployedVersionRegistry {
    /// SyRS SYS-79 write path. Called by `launch` on the
    /// `ReadyWithinDeadline` arm so the deployed version is recorded
    /// at deployment time exactly once per successful launch.
    fn record(
        &self,
        strategy_id: &StrategyId,
        version: DeployedVersion,
    ) -> Result<(), DeployedVersionRegistryError>;

    /// SyRS SYS-79 read path. Future dashboard / REST API / backtest
    /// readers query this to render the version identifier. Returns
    /// `Ok(None)` if no version has been recorded for `strategy_id`
    /// (e.g. the strategy was never deployed, or the registry is
    /// not durable across restarts and the record was lost).
    fn lookup(
        &self,
        strategy_id: &StrategyId,
    ) -> Result<Option<DeployedVersion>, DeployedVersionRegistryError>;
}

/// SRS-ORCH-004 registry-failure surface (durable-store outage,
/// permission denied, in-process registry mutex poisoning, etc.).
/// Future concrete-registry variants will discriminate the cause;
/// the reason string is the dashboard's rendering source for now.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeployedVersionRegistryError {
    pub reason: String,
}

impl DeployedVersionRegistryError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for DeployedVersionRegistryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-ORCH-004: DeployedVersionRegistry call failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for DeployedVersionRegistryError {}

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

// --------------------------------------------------------------------------- //
// Hot-Swap demotion liquidation-timeout ports (ERR-7, SRS-RESV-004)
// (SyRS SYS-49b / SYS-49c; StRS SN-1.25)
// --------------------------------------------------------------------------- //
//
// SRS-RESV-004 requires Hot-Swap demotion to run before promotion: the
// current live strategy stops new signals, cancels resting IB orders,
// submits liquidation orders, and waits for flat confirmation OR the
// configured 60 s timeout. The orchestrator owns the demotion→promotion
// ordering, so the `resolve_demotion` gate and the ports it consumes live
// here (not in `atp-types`) for the same dependency-direction reason the
// lifecycle ports do (SRS-ARCH-002): placing them in the type crate would
// invert the boundary. Lower-layer crates MUST NOT import
// `atp_orchestrator` (enforced by `tools/dependency_boundary_check.py`).
//
// Four ports mediate the timeout decision:
//
//   * `HotSwapLiquidationProbe` — the timing authority. Returns a
//     `HotSwapDemotionOutcome` discriminating `FlatBeforeTimeout` from
//     `TimedOutDemotionPending` so the gate matches on the decision
//     without re-implementing the 60 s async wait loop (deferred runtime).
//     Read-only — no mutators, so the gate cannot promote through this port.
//
//   * `UnfilledOrderCanceller` — the SRS-RESV-004 "cancel the unfilled
//     liquidation order" action. The concrete impl routes to the IB
//     adapter's `cancel_order` (deferred runtime); the gate calls it ONLY
//     on the timeout branch.
//
//   * `OperatorAlertSink` — the SRS-RESV-004 dashboard/email/SMS
//     notification fan-out. Fire-and-forget (mirrors `HealthCheckEventSink`):
//     the demotion-pending decision is irreversible once the timeout fires,
//     so an alert-dispatch failure does not roll it back. The concrete
//     email/SMS transport is the deferred SRS-NOTIF-001 dispatcher
//     (`atp-notification`, today a stub) — kept behind this port so
//     `atp-orchestrator` does not depend on `atp-notification`.
//
//   * `HotSwapDemotionEventSink` — the structured state-transition audit
//     record for the dashboard/log fan-out (the deferred SRS-LOG-001 /
//     SRS-UI-001 consumers). Recorded on BOTH arms so the dashboard sees
//     "flat, swap proceeded" and "timed out, demotion-pending" alike.
//
// Concrete impls of all four ports are the deferred runtime, enumerated in
// `architecture/runtime_services.json` `hot_swap_demotion_contract.deferred[]`.

pub trait HotSwapLiquidationProbe {
    /// Await flat confirmation OR the configured timeout. Returns
    /// `FlatBeforeTimeout { elapsed_seconds }` if live positions reach
    /// flat within `request.timeout_seconds`, or
    /// `TimedOutDemotionPending { elapsed_seconds, timeout_seconds }` if
    /// the deadline is breached. The orchestrator's `resolve_demotion`
    /// gate matches on the returned `HotSwapDemotionOutcome` so it never
    /// re-implements the wait-loop timing (the probe is the source of
    /// truth). No mutators: the gate cannot promote through this port.
    fn await_flat_or_timeout(&self, request: &HotSwapDemotionRequest) -> HotSwapDemotionOutcome;
}

pub trait UnfilledOrderCanceller {
    /// SRS-RESV-004 "cancel the unfilled liquidation order". Called ONLY on
    /// the `TimedOutDemotionPending` branch of `resolve_demotion`. The
    /// concrete impl routes to the IB adapter's `cancel_order` (deferred
    /// runtime). Returns `Result` so an IB-cancel failure is surfaced rather
    /// than silently dropped: a failed cancel can leave a live liquidation
    /// order, which must be observable to the operator. The gate does NOT
    /// abort on failure (the timeout already blocks promotion) — it records
    /// the outcome on `HotSwapDemotionEvent::liquidation_cancel`.
    fn cancel_unfilled_liquidation_orders(
        &self,
        request: &HotSwapDemotionRequest,
    ) -> Result<(), HotSwapSideEffectError>;
}

pub trait OperatorAlertSink {
    /// SRS-RESV-004 dashboard/email/SMS notification dispatch. Called ONLY
    /// on the `TimedOutDemotionPending` branch. Returns `Result` so a
    /// transport failure (email/SMS unreachable, dashboard channel down) is
    /// surfaced rather than silently dropped — a missed page on a liquidation
    /// timeout is itself a safety event. The gate does NOT abort on failure
    /// (the demotion-pending decision is irreversible once the timeout
    /// fires); it records the outcome on
    /// `HotSwapDemotionEvent::operator_alert`. The concrete email/SMS
    /// transport is the deferred SRS-NOTIF-001 dispatcher.
    fn dispatch(&self, event: OperatorAlertEvent) -> Result<(), HotSwapSideEffectError>;
}

/// ERR-7 / SRS-RESV-004 side-effect failure surface for the timeout-branch
/// IB-cancel and operator-alert ports. Mirrors `WorkloadEventSinkError`:
/// carries a short reason string for now; the typed CONNECTIVITY_BLOCKED /
/// STALE_DATA_BLOCKED / transport-timeout taxonomy is added when the
/// concrete IB-cancel (`atp-adapters`) and email/SMS (`atp-notification`)
/// runtimes land (named in the contract's `deferred[]`). The orchestrator's
/// `resolve_demotion` gate maps an `Err` into
/// `SideEffectOutcome::Failed { reason }` on the demotion event so the
/// failure is observable end to end.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotSwapSideEffectError {
    pub reason: String,
}

impl HotSwapSideEffectError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for HotSwapSideEffectError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-RESV-004: hot-swap demotion side effect failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for HotSwapSideEffectError {}

/// Map a timeout-branch side-effect port result into the observable
/// `SideEffectOutcome` recorded on `HotSwapDemotionEvent`. An `Err` is
/// preserved as `Failed { reason }` (carrying the port's reason string) so
/// the failure is surfaced on the audit event rather than silently dropped.
fn into_outcome(result: Result<(), HotSwapSideEffectError>) -> SideEffectOutcome {
    match result {
        Ok(()) => SideEffectOutcome::Succeeded,
        Err(error) => SideEffectOutcome::Failed {
            reason: error.reason,
        },
    }
}

pub trait HotSwapDemotionEventSink {
    /// Structured demotion state-transition record for the dashboard / log
    /// fan-out (deferred SRS-LOG-001 / SRS-UI-001 consumers). Recorded on
    /// both arms of `resolve_demotion`. Returns `Result` so a concrete sink
    /// cannot silently swallow a publication failure (audit log unwritable,
    /// dashboard channel disconnected, queue full) — the failure surfaces to
    /// the concrete durable sink and any wrapping caller. The gate treats
    /// emission as **best-effort** (mirrors `WorkloadEventSink`): the demotion
    /// decision is already made and the safety side effects (cancel + alert)
    /// have already been attempted, so a sink failure does not roll them back
    /// or change the promotion-block outcome. Durable delivery (bounded queue
    /// + journal/retry) is the deferred SRS-LOG-001 sink's responsibility.
    fn record(&self, event: HotSwapDemotionEvent) -> Result<(), HotSwapSideEffectError>;
}

/// SRS-RESV-004 acceptance evidence: the demotion reached flat before the
/// timeout, so promotion of the candidate is allowed. The `Err` counterpart
/// (`StructuredHotSwapDemotionError`) is the only other outcome of
/// `resolve_demotion`; a caller that promotes ONLY on `Ok` therefore blocks
/// promotion on every timeout. `promotion_allowed` is carried explicitly
/// (always `true` on this struct) so the dashboard / REST surface renders
/// the gate decision without re-deriving it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotSwapDemotionResolved {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    pub promotion_allowed: bool,
    pub elapsed_seconds: u64,
}

// --------------------------------------------------------------------------- //
// Hot-Swap trigger evaluation ports (SRS-RESV-003, SyRS SYS-49a)
// --------------------------------------------------------------------------- //
//
// SRS-RESV-003 is the trigger DECISION + CONFIGURATION + LOGGING layer that
// feeds the SRS-RESV-004 demotion gate (`resolve_demotion`). The evaluator
// (`evaluate_automatic_triggers` / `request_manual_promotion`) consumes three
// injected ports — mirroring the demotion gate's `HotSwapLiquidationProbe` /
// `HotSwapDemotionEventSink` seam so RESV-003 is buildable and testable without
// the unbuilt reservoir/ranking runtime (SRS-RESV-001/002) or durable log store
// (SRS-LOG-001):
//   * `LiveStrategyProbe` — the demoting side + drawdown observation (read-only;
//     the evaluator cannot promote/demote through it).
//   * `ReservoirRankingSource` — the injected ranking snapshot the promotion
//     triggers select candidates from (fail-closed accessors).
//   * `HotSwapTriggerLog` — the best-effort sink recorded on EVERY fired trigger
//     ("all swap triggers shall be logged"), whose durable delivery is the
//     deferred SRS-LOG-001 sink.
// Everything not owned here — demotion execution, promotion, cool-down, the
// durable log store, and the REST/dashboard handlers — is enumerated in
// `architecture/runtime_services.json` `hot_swap_trigger_contract.deferred[]`.

/// SRS-RESV-003 read-only probe for the current live strategy (identity +
/// observed drawdown, in basis points). `None` means no strategy is live — every
/// automatic trigger then fails closed (no demoting side, no fire). Mirrors the
/// no-mutator discipline of `HotSwapLiquidationProbe`: the evaluator observes,
/// it never promotes/demotes through this port.
pub trait LiveStrategyProbe {
    fn current_live(&self) -> Option<LiveStrategyState>;
}

/// SRS-RESV-003 read-only source of the reservoir ranking snapshot (the
/// SRS-RESV-002 / SyRS SYS-48 output, injected). The evaluator selects promotion
/// candidates through the snapshot's fail-closed accessors (`top_by_rank` /
/// `top_by_momentum`).
pub trait ReservoirRankingSource {
    fn snapshot(&self) -> ReservoirRankingSnapshot;
}

/// SRS-RESV-003 best-effort log sink for fired Hot-Swap triggers (SyRS SYS-49a
/// "all swap triggers shall be logged" → SYS-61). The evaluator records EVERY
/// fired trigger through this port in the same code path that produces the
/// proposal, so a proposal cannot exist without a log attempt. Returns `Result`
/// so a concrete sink surfaces a publication failure rather than swallowing it;
/// emission is best-effort (mirrors `HotSwapDemotionEventSink`) — a sink failure
/// does not un-fire the trigger or change the evaluation. Durable delivery is
/// the deferred SRS-LOG-001 sink's responsibility.
pub trait HotSwapTriggerLog {
    fn record(&self, event: HotSwapTriggerEvent) -> Result<(), HotSwapSideEffectError>;
}

/// SRS-RESV-003 result of an automatic-trigger evaluation pass. `fired` is the
/// full, priority-ordered set of triggers that fired this pass (each already
/// logged) — the "all swap triggers are logged" evidence. `selected` is the
/// single highest-priority proposal (drawdown-demotion first, then top-ranked,
/// then highest-momentum) that the SRS-RESV-004/005 execution path should act
/// on: exactly one swap can happen under the single-live-strategy invariant, so
/// the evaluator resolves the priority here rather than pushing "pick one" onto
/// the caller. `selected` is `Some(fired[0])` when any trigger fired, else `None`.
#[derive(Debug, Clone, PartialEq)]
pub struct TriggerEvaluation {
    pub fired: Vec<HotSwapTriggerProposal>,
    pub selected: Option<HotSwapTriggerProposal>,
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
    /// **Required precondition** (SRS-ORCH-003 / SyRS SYS-57 / SYS-58):
    /// callers MUST invoke `Self::admit_workload(...)` first and
    /// proceed to `launch` only when admission returned `Ok(())`. The
    /// admission gate enforces the host-memory safety margin and the
    /// workload-priority hierarchy; calling `launch` without it
    /// bypasses those guarantees. This precondition is contract-level
    /// today (production callers must observe the sequence
    /// `admit_workload → launch`); the typed-coupling work that turns
    /// "must call" into "cannot fail to call" via a typestate /
    /// builder is named in
    /// `workload_priority_contract.deferred` and will be applied
    /// once a production caller of `launch` exists. The contract is
    /// also pinned by the integration tests
    /// (`orch_3_workload_priority_contract`) and the domain test
    /// `tests/domain/test_orchestrator_workload_priority.py`.
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
    ///
    /// **SRS-ORCH-004 / SyRS SYS-79 deployed-version recording**:
    ///
    /// * The gate validates `request.deployment_hash` BEFORE invoking
    ///   the runtime port. A malformed override is refused with
    ///   `OrderErrorCategory::DeployedVersionInvalid` and NO sink
    ///   event (no container exists to destroy; emitting an event
    ///   would lie about a destroy that never happened — same
    ///   pattern as the resource-profile gate).
    /// * On the `ReadyWithinDeadline` arm, the gate records the
    ///   deployed version (hash + `observed_at_seconds`) through the
    ///   `version_registry` port BEFORE constructing the outcome.
    ///   Registry-record failures are best-effort by design: once
    ///   the container is running, refusing the launch retroactively
    ///   would lie to operators and force a destroy. Concrete
    ///   registries surface failures through the typed
    ///   `DeployedVersionRegistryError` for wrapping callers
    ///   (dashboard / REST API / backtest pipeline) to observe.
    /// * On the `DeadlineExceeded` arm, the gate does NOT call
    ///   `version_registry.record` — a version that was never
    ///   deployed must not appear in the active-strategy inventory
    ///   (SYS-41) or the REST API listing (IF-9).
    // StructuredOrchestratorError is an intentionally rich typed error; boxing
    // the Err variant would change the public signature (out of scope here).
    #[allow(clippy::result_large_err)]
    pub fn launch<R, S, V>(
        &self,
        request: StrategyLaunchRequest,
        runtime: &R,
        sink: &S,
        version_registry: &V,
        observed_at_seconds: u64,
    ) -> Result<StrategyLaunchOutcome, StructuredOrchestratorError>
    where
        R: StrategyContainerRuntime,
        S: HealthCheckEventSink,
        V: DeployedVersionRegistry,
    {
        // SRS-ORCH-004 + SyRS SYS-79: validate the source-hash wire
        // form at the orchestrator boundary BEFORE invoking the
        // runtime port. A misconfigured override (missing prefix,
        // wrong algorithm, wrong digest length, non-hex) must never
        // reach `runtime.create` — there is no container to destroy
        // because none was ever created, so this rejection is a pure
        // structured error with NO sink event (an event would lie
        // about a destroy that never happened). Pattern matches the
        // SRS-ORCH-002 resource-profile gate.
        if let Err(violation) = request.deployment_hash.validate() {
            return Err(StructuredOrchestratorError::deployed_version_invalid(
                request, violation,
            ));
        }
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
                // SRS-ORCH-004 / SyRS SYS-79: record the deployed
                // version at the deployment moment so the deferred
                // dashboard (SYS-41), REST API (IF-9), and backtest
                // result rows (SYS-21) can render the same version
                // identifier. Registry failures are best-effort by
                // design (see the launch Rustdoc); concrete
                // registries surface them via the typed
                // DeployedVersionRegistryError so wrapping callers
                // can observe without aborting the launch.
                let deployed_version =
                    DeployedVersion::new(request.deployment_hash.clone(), observed_at_seconds);
                let _ = version_registry.record(&request.strategy_id, deployed_version.clone());
                Ok(StrategyLaunchOutcome {
                    strategy_id: request.strategy_id,
                    ready_within_deadline: true,
                    elapsed_millis,
                    deadline_millis: request.deadline_millis,
                    profile: request.profile,
                    deployed_version,
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

    /// SRS-ORCH-004 / SyRS SYS-79 read-only helper. Constructs a
    /// `DeployedVersion` from the launch envelope without invoking
    /// the runtime port, so callers can preview the version
    /// identifier (e.g. for a dashboard tooltip) before deployment.
    /// The orchestrator's `launch` gate builds the same record on
    /// the `ReadyWithinDeadline` arm.
    pub fn deployed_version_for(
        &self,
        request: &StrategyLaunchRequest,
        observed_at_seconds: u64,
    ) -> DeployedVersion {
        DeployedVersion::new(request.deployment_hash.clone(), observed_at_seconds)
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

    /// ERR-7 / SRS-RESV-004 Hot-Swap demotion liquidation-timeout gate.
    /// Matches on the liquidation probe's `HotSwapDemotionOutcome`
    /// classification of the demotion:
    ///
    ///   * `FlatBeforeTimeout` — live positions reached flat within the
    ///     configured timeout. The swap proceeds to paper: the gate records
    ///     a `HotSwapDemotionEvent` with `promotion_blocked = false` and
    ///     returns `Ok(HotSwapDemotionResolved { promotion_allowed: true, .. })`.
    ///     NO operator alert is dispatched and NO unfilled-order cancel is
    ///     issued (there is nothing unfilled to cancel).
    ///
    ///   * `TimedOutDemotionPending` — the liquidation timed out. Per
    ///     SRS-RESV-004 the gate cancels the unfilled liquidation order,
    ///     dispatches the dashboard/email/SMS operator alert, records a
    ///     `HotSwapDemotionEvent` with `promotion_blocked = true`, and
    ///     refuses with a `StructuredHotSwapDemotionError` whose category is
    ///     `OrderErrorCategory::HotSwapDemotionTimeout` (wire string
    ///     `HOT_SWAP_DEMOTION_TIMEOUT`). Promotion is blocked because the
    ///     caller promotes the candidate ONLY on `Ok`.
    ///
    /// **Invariants** (statically checked by
    /// `tools/hot_swap_demotion_check.py`):
    ///
    /// * The `TimedOutDemotionPending` arm MUST call
    ///   `canceller.cancel_unfilled_liquidation_orders(`,
    ///   `alerts.dispatch(`, and `events.record(`, and MUST produce
    ///   `OrderErrorCategory::HotSwapDemotionTimeout` (directly or via the
    ///   `StructuredHotSwapDemotionError::demotion_timeout(` factory).
    /// * The `TimedOutDemotionPending` arm MUST NOT construct
    ///   `HotSwapDemotionResolved` and MUST NOT call any promotion path
    ///   (`promote`, `complete_swap`, `go_live`, …) — a timed-out demotion
    ///   is not an acceptance and must block promotion.
    /// * The `FlatBeforeTimeout` arm MUST NOT call `alerts.dispatch(` or
    ///   `canceller.cancel_unfilled_liquidation_orders(` — a clean,
    ///   in-time demotion raises no operator alert and cancels nothing.
    /// * `FlatBeforeTimeout` is the only call site of
    ///   `HotSwapDemotionResolved {`.
    ///
    /// The `liquidation` probe is the timing authority for the 60 s async
    /// wait loop (deferred runtime), but the gate does NOT blindly trust its
    /// label: a `FlatBeforeTimeout` whose `elapsed_seconds` exceeds
    /// `request.timeout_seconds` is a probe inconsistency (buggy or
    /// version-skewed) and is **normalised to a timeout** so it cannot bypass
    /// the promotion block (fail closed). This defends the safety-critical
    /// direction — failing closed toward *blocking promotion* is always safe.
    /// The inverse inconsistency (a `TimedOutDemotionPending` reported before
    /// `request.timeout_seconds` elapses, or with a mismatched
    /// `timeout_seconds`) is NOT normalised here: handling it correctly means
    /// blocking promotion *without* firing the premature destructive cancel
    /// and surfacing a distinct probe-inconsistency rejection (not a
    /// misleading "liquidation timeout"), which is the deferred Hot-Swap
    /// runtime's richer demotion semantics. Until then the probe's
    /// outcome-consistency (flat ⟹ within deadline; timeout ⟹ at/after the
    /// deadline with a matching `timeout_seconds`) is the probe's contract
    /// precondition — see `hot_swap_demotion_contract.deferred[]`. (The
    /// shipped slice has no concrete probe, so no real probe can violate
    /// this; the spies in the L7 tests return consistent outcomes.)
    ///
    /// All three side effects are surfaced rather than swallowed: the cancel
    /// and alert ports return `Result` and their outcomes are recorded on the
    /// event (`SideEffectOutcome::Failed` preserves the reason); the event
    /// sink also returns `Result`. Event emission is best-effort (mirrors
    /// `WorkloadEventSink`) — the demotion decision is irreversible once the
    /// timeout fires, so a sink failure does not roll back the cancel/alert
    /// or change the promotion-block outcome; durable delivery is the
    /// deferred SRS-LOG-001 sink's concern.
    ///
    /// **Scope — stateless single-attempt gate.** This gate decides ONE
    /// demotion attempt: a timeout blocks promotion for THAT call (it returns
    /// `Err`, and a caller promotes only on `Ok`). It does NOT persist a
    /// demotion-pending lockout, so SRS-RESV-004's "promotion is blocked
    /// until manual resolution" is not yet enforced across a later retry
    /// whose probe reports flat. The durable demotion-pending store + the
    /// operator manual-resolution command that clears it are the deferred
    /// Hot-Swap runtime (SRS-RESV-003 / SRS-RESV-004 / SRS-RESV-006 — the
    /// promote/demote/rollback subsystem tracked by the skipped
    /// `tests/domain/test_single_live_invariant.py`), recorded in
    /// `hot_swap_demotion_contract.deferred[]`.
    pub fn resolve_demotion<P, C, A, E>(
        &self,
        request: HotSwapDemotionRequest,
        liquidation: &P,
        canceller: &C,
        alerts: &A,
        events: &E,
        observed_at_seconds: u64,
    ) -> Result<HotSwapDemotionResolved, StructuredHotSwapDemotionError>
    where
        P: HotSwapLiquidationProbe,
        C: UnfilledOrderCanceller,
        A: OperatorAlertSink,
        E: HotSwapDemotionEventSink,
    {
        let reported = liquidation.await_flat_or_timeout(&request);
        // Defense-in-depth fail-closed: the probe is the timing authority,
        // but a FlatBeforeTimeout whose elapsed exceeds the configured
        // timeout is a probe inconsistency (buggy / version-skewed). Normalise
        // it to a timeout BEFORE the match so a mislabelled over-deadline
        // demotion cannot bypass the promotion block.
        let outcome = match reported {
            HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds }
                if elapsed_seconds > request.timeout_seconds =>
            {
                HotSwapDemotionOutcome::TimedOutDemotionPending {
                    elapsed_seconds,
                    timeout_seconds: request.timeout_seconds,
                }
            }
            other => other,
        };
        match outcome {
            HotSwapDemotionOutcome::FlatBeforeTimeout { elapsed_seconds } => {
                // SRS-RESV-004: positions reached flat in time — the swap
                // proceeds. Record the audit transition (promotion NOT
                // blocked) and return the acceptance; no alert, no cancel
                // (both side effects are NotAttempted on this branch). Event
                // emission is best-effort (see the gate Rustdoc).
                let _ = events.record(HotSwapDemotionEvent {
                    outcome,
                    demoting_strategy_id: request.demoting_strategy_id.clone(),
                    candidate_strategy_id: request.candidate_strategy_id.clone(),
                    promotion_blocked: false,
                    liquidation_cancel: SideEffectOutcome::NotAttempted,
                    operator_alert: SideEffectOutcome::NotAttempted,
                    observed_at_seconds,
                });
                Ok(HotSwapDemotionResolved {
                    demoting_strategy_id: request.demoting_strategy_id,
                    candidate_strategy_id: request.candidate_strategy_id,
                    promotion_allowed: true,
                    elapsed_seconds,
                })
            }
            HotSwapDemotionOutcome::TimedOutDemotionPending {
                elapsed_seconds,
                timeout_seconds,
            } => {
                // SRS-RESV-004 timeout branch: cancel the unfilled
                // liquidation order, notify the operator over all three
                // channels, record the demotion-pending transition with
                // promotion blocked, and refuse with the structured error.
                // BOTH side effects are attempted unconditionally (a failed
                // cancel must NOT suppress the operator page, and vice
                // versa) and their outcomes are recorded on the event so a
                // failed cancel / missed alert is observable rather than
                // indistinguishable from success. Promotion is blocked
                // regardless via the returned `Err`.
                let liquidation_cancel =
                    into_outcome(canceller.cancel_unfilled_liquidation_orders(&request));
                let operator_alert = into_outcome(alerts.dispatch(OperatorAlertEvent {
                    demoting_strategy_id: request.demoting_strategy_id.clone(),
                    candidate_strategy_id: request.candidate_strategy_id.clone(),
                    channels: vec![
                        OperatorAlertChannel::Dashboard,
                        OperatorAlertChannel::Email,
                        OperatorAlertChannel::Sms,
                    ],
                    elapsed_seconds,
                    timeout_seconds,
                    observed_at_seconds,
                }));
                // Best-effort audit emission (see the gate Rustdoc): a sink
                // failure does not roll back the cancel/alert above or change
                // the promotion-block outcome below.
                let _ = events.record(HotSwapDemotionEvent {
                    outcome,
                    demoting_strategy_id: request.demoting_strategy_id.clone(),
                    candidate_strategy_id: request.candidate_strategy_id.clone(),
                    promotion_blocked: true,
                    liquidation_cancel,
                    operator_alert,
                    observed_at_seconds,
                });
                Err(StructuredHotSwapDemotionError::demotion_timeout(
                    request,
                    elapsed_seconds,
                    timeout_seconds,
                ))
            }
        }
    }

    /// SRS-RESV-003 / SyRS SYS-49a automatic Hot-Swap trigger evaluation. For
    /// each automatic trigger, in a FIXED priority order — drawdown-demotion
    /// (risk control) first, then top-ranked, then highest-momentum — the gate:
    ///   * fires NOTHING and logs NOTHING when the trigger is `Disabled` (the
    ///     SYS-49a "default to disabled" posture: a default config yields an
    ///     empty evaluation even when every input condition is met);
    ///   * when `Enabled`, resolves the demoting side from `live.current_live()`
    ///     and the candidate from the ranking snapshot (drawdown / top-ranked →
    ///     `top_by_rank`; momentum → `top_by_momentum`), and fires ONLY when both
    ///     resolve, the candidate differs from the live strategy, and the
    ///     trigger's own condition holds (drawdown: observed ≥ configured
    ///     threshold).
    ///
    /// Every fired trigger is recorded through `log` in the same path that builds
    /// the proposal (see `fire_trigger`; best-effort — a sink failure does not
    /// un-fire it), so "all swap triggers are logged" holds by construction.
    /// Returns the full priority-ordered `fired` set plus the single
    /// highest-priority `selected` proposal. Read-only w.r.t. execution: it
    /// proposes + logs, never promotes/demotes — the SRS-RESV-004 gate
    /// (`resolve_demotion`) executes a `selected` swap.
    pub fn evaluate_automatic_triggers<L, R, S>(
        &self,
        config: &HotSwapTriggerConfig,
        live: &L,
        ranking: &R,
        log: &S,
        observed_at_seconds: u64,
    ) -> TriggerEvaluation
    where
        L: LiveStrategyProbe,
        R: ReservoirRankingSource,
        S: HotSwapTriggerLog,
    {
        let mut fired: Vec<HotSwapTriggerProposal> = Vec::new();

        // No live strategy ⇒ nothing to demote ⇒ every automatic trigger fails
        // closed. (Manual promotion is a separate, always-available path.)
        let Some(live_state) = live.current_live() else {
            return TriggerEvaluation {
                fired,
                selected: None,
            };
        };
        let snapshot = ranking.snapshot();

        // Priority 1 — drawdown-demotion (SYS-49a(b)): the live strategy breached
        // its configured drawdown threshold. The replacement candidate is the
        // top-ranked reservoir strategy (fail closed to no-fire if none).
        if let DrawdownDemotionTrigger::Enabled { threshold } = config.drawdown_demotion {
            if live_state.drawdown_bps >= threshold.get() {
                if let Some(candidate) = snapshot.top_by_rank() {
                    if candidate.strategy_id != live_state.strategy_id {
                        fired.push(self.fire_trigger(
                            HotSwapTriggerKind::DrawdownDemotion,
                            &live_state.strategy_id,
                            &candidate.strategy_id,
                            TriggerRationale::DrawdownBreached {
                                observed_bps: live_state.drawdown_bps,
                                threshold_bps: threshold.get(),
                            },
                            log,
                            observed_at_seconds,
                        ));
                    }
                }
            }
        }

        // Priority 2 — top-ranked promotion (SYS-49a(c) top-ranked): promote the
        // top-ranked reservoir strategy when it is not already live.
        if config.top_ranked_promotion.is_enabled() {
            if let Some(candidate) = snapshot.top_by_rank() {
                if candidate.strategy_id != live_state.strategy_id {
                    fired.push(self.fire_trigger(
                        HotSwapTriggerKind::TopRankedPromotion,
                        &live_state.strategy_id,
                        &candidate.strategy_id,
                        TriggerRationale::TopRanked {
                            rank: candidate.rank,
                            score: candidate.risk_adjusted_score,
                        },
                        log,
                        observed_at_seconds,
                    ));
                }
            }
        }

        // Priority 3 — highest-momentum promotion (SYS-49a(c) highest momentum):
        // promote the highest-momentum reservoir strategy when not already live.
        if config.highest_momentum_promotion.is_enabled() {
            if let Some(candidate) = snapshot.top_by_momentum() {
                if candidate.strategy_id != live_state.strategy_id {
                    fired.push(self.fire_trigger(
                        HotSwapTriggerKind::HighestMomentumPromotion,
                        &live_state.strategy_id,
                        &candidate.strategy_id,
                        TriggerRationale::HighestMomentum {
                            momentum_score: candidate.momentum_score,
                        },
                        log,
                        observed_at_seconds,
                    ));
                }
            }
        }

        let selected = fired.first().cloned();
        TriggerEvaluation { fired, selected }
    }

    /// SRS-RESV-003 / SyRS SYS-49a(a) manual Hot-Swap promotion. Manual selection
    /// is ALWAYS available — it is not gated by `HotSwapTriggerConfig` and fires
    /// regardless of the automatic-trigger posture. The operator names the
    /// demoting (current live) strategy and the candidate; the trigger fires and
    /// is logged (best-effort) unconditionally. The cool-down confirmation
    /// warning for a manual swap during cool-down (SYS-49e) is the deferred
    /// SRS-RESV-006 concern and is intentionally NOT enforced here.
    pub fn request_manual_promotion<S: HotSwapTriggerLog>(
        &self,
        demoting_strategy_id: StrategyId,
        candidate_strategy_id: StrategyId,
        log: &S,
        observed_at_seconds: u64,
    ) -> HotSwapTriggerProposal {
        self.fire_trigger(
            HotSwapTriggerKind::ManualPromotion,
            &demoting_strategy_id,
            &candidate_strategy_id,
            TriggerRationale::ManualSelection,
            log,
            observed_at_seconds,
        )
    }

    /// SRS-RESV-003 build a fired-trigger proposal AND log it (best-effort) in
    /// one place, so a proposal can never be produced without a paired log
    /// attempt — the mechanical guarantee behind "all swap triggers are logged".
    fn fire_trigger<S: HotSwapTriggerLog>(
        &self,
        kind: HotSwapTriggerKind,
        demoting_strategy_id: &StrategyId,
        candidate_strategy_id: &StrategyId,
        rationale: TriggerRationale,
        log: &S,
        observed_at_seconds: u64,
    ) -> HotSwapTriggerProposal {
        let proposal = HotSwapTriggerProposal {
            kind,
            demoting_strategy_id: demoting_strategy_id.clone(),
            candidate_strategy_id: candidate_strategy_id.clone(),
            rationale,
            observed_at_seconds,
        };
        // Best-effort audit emission (mirrors `resolve_demotion`): a sink failure
        // does not un-fire the trigger or drop it from `fired`.
        let _ = log.record(proposal.to_event());
        proposal
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
            Some(raw) => {
                raw.parse::<u32>()
                    .map_err(|_| ResourceProfileEnvError::UnparseableMem {
                        var: mem_var,
                        raw_value: raw,
                    })?
            }
            None => default.mem_mb,
        };
        // CPU is stored in the catalogue as a float (cores) and on the
        // orchestrator as integer hundredths; convert at this boundary.
        // The contract check (config_catalogue_binding) statically
        // verifies the catalogue defaults convert to the constants so a
        // future tuning change touches both sides consistently.
        let cpu_hundredths = match lookup(cpu_var) {
            Some(raw) => {
                let cores =
                    raw.parse::<f64>()
                        .map_err(|_| ResourceProfileEnvError::UnparseableCpu {
                            var: cpu_var,
                            raw_value: raw.clone(),
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
        let profile = ResourceProfile {
            mem_mb,
            cpu_hundredths,
        };
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

    /// SRS-ORCH-003 / SyRS SYS-57 default host-memory safety margin
    /// (2048 MB). Re-exported so callers populating the admission gate
    /// do not have to reach into `atp_types` — the orchestrator owns
    /// the lifecycle boundary.
    pub const fn host_memory_safety_margin_default(&self) -> HostMemorySafetyMargin {
        HostMemorySafetyMargin::default_margin()
    }

    /// SRS-ORCH-003: build a `HostMemorySafetyMargin` from the
    /// `ATP_HOST_MEMORY_SAFETY_MARGIN_MB` configuration value supplied
    /// by `lookup`. Falls back to the SyRS spec-literal default ONLY
    /// when the lookup returns `None`. A set-but-unparseable value is
    /// returned as `Unparseable` — silently substituting a default for
    /// an invalid override would be the opposite of "configuration
    /// overrides are validated." The resulting margin is then
    /// validated against the SRS-ARCH-005 catalogue bounds.
    pub fn safety_margin_from_lookup<F>(
        &self,
        lookup: F,
    ) -> Result<HostMemorySafetyMargin, HostMemorySafetyMarginEnvError>
    where
        F: Fn(&str) -> Option<String>,
    {
        let var = "ATP_HOST_MEMORY_SAFETY_MARGIN_MB";
        let mb = match lookup(var) {
            Some(raw) => {
                raw.parse::<u32>()
                    .map_err(|_| HostMemorySafetyMarginEnvError::Unparseable {
                        var,
                        raw_value: raw,
                    })?
            }
            None => HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT,
        };
        let margin = HostMemorySafetyMargin { mb };
        margin.validate()?;
        Ok(margin)
    }

    /// SRS-ORCH-003 testable env-var bridge. Distinguishes
    /// `VarError::NotPresent` (silently falls back to the SyRS default)
    /// from `VarError::NotUnicode` (returned as a structured
    /// `Unparseable` error). Mirrors `profile_for_mode_via_env_lookup`.
    pub fn safety_margin_via_env_lookup<F>(
        &self,
        env_lookup: F,
    ) -> Result<HostMemorySafetyMargin, HostMemorySafetyMarginEnvError>
    where
        F: Fn(&str) -> Result<String, std::env::VarError>,
    {
        let var = "ATP_HOST_MEMORY_SAFETY_MARGIN_MB";
        let value = match env_lookup(var) {
            Ok(raw) => Some(raw),
            Err(std::env::VarError::NotPresent) => None,
            Err(std::env::VarError::NotUnicode(raw)) => {
                return Err(HostMemorySafetyMarginEnvError::Unparseable {
                    var,
                    raw_value: raw.to_string_lossy().into_owned(),
                });
            }
        };
        self.safety_margin_from_lookup(|name| if name == var { value.clone() } else { None })
    }

    /// SRS-ORCH-003 production env-var wrapper. Plugs `std::env::var`
    /// into `safety_margin_via_env_lookup`.
    pub fn safety_margin_from_env(
        &self,
    ) -> Result<HostMemorySafetyMargin, HostMemorySafetyMarginEnvError> {
        self.safety_margin_via_env_lookup(|name: &str| std::env::var(name))
    }

    /// SRS-ORCH-003 admission gate (SyRS SYS-57 / SYS-58). Decides
    /// whether a new workload may be deployed against the configured
    /// host-memory safety margin and the SYS-57 priority hierarchy.
    ///
    /// Algorithm (the contract check pins each invariant):
    ///
    /// 1. **Margin validation (defence in depth)**: call
    ///    `safety_margin.validate()`. A programmatically-constructed
    ///    margin that bypassed the env-helper / catalogue validation
    ///    (test fixture, future REST API override) must NOT be allowed
    ///    to disable the gate — refuse synchronously with the breach
    ///    factory so the dashboard surfaces the operator-configuration
    ///    error.
    /// 2. Read `host.available_mb()` once. The value is a snapshot;
    ///    the gate does NOT re-probe inside the arbitration loop
    ///    (re-probing would race with the very evictions the gate is
    ///    deciding about).
    /// 3. Compute the post-admit headroom — `available - needed`. If
    ///    that already leaves room above the safety margin, admit
    ///    immediately with NO events emitted and NO registry mutation.
    /// 4. **Pre-eviction feasibility (no partial-eviction refusal)**:
    ///    walk `registry.active()` filtered to `WorkloadKind::Batch`
    ///    and strictly-lower-priority-than-incoming, sorted by
    ///    descending `rank()`. Sum their `profile.mem_mb`. If even the
    ///    full sum cannot bring the post-admit headroom above the
    ///    safety margin, refuse WITHOUT issuing any `terminate` call
    ///    (SyRS SYS-58 (b): "if a higher-priority workload requires
    ///    resources" — if the resources cannot be assembled, no
    ///    workload is killed).
    /// 5. Otherwise, iterate the sorted candidates and terminate them
    ///    one at a time:
    ///    * **SYS-58 last-clause invariant**: `c.priority` must not be
    ///      `LiveStrategy`. Live is `Continuous` so the filter above
    ///      already excludes it; the `debug_assert!` is a belt-and-
    ///      suspenders that makes the invariant auditable in a stack
    ///      trace if a registry implementation drifts.
    ///    * `registry.terminate(&c.id)` returns `Result`. On `Ok`,
    ///      emit a `Terminated` event AND bank the freed memory. On
    ///      `Err`, do NOT bank the memory (the workload may still be
    ///      consuming its resources) — continue to the next eligible
    ///      candidate. The pre-check above ensures the sum is
    ///      sufficient IF all terminations succeed; if some fail, the
    ///      loop attempts the remaining candidates and refuses if
    ///      still below margin.
    /// 6. If after the loop the host is still below the safety margin,
    ///    emit a `Refused` event AND return
    ///    `StructuredOrchestratorError::host_memory_safety_margin_breach`.
    ///
    /// **Invariants** (statically checked by
    /// `tools/orchestrator_workload_priority_check.py`):
    ///
    /// * The refusal arm MUST emit a `WorkloadAdmissionEvent::Refused`
    ///   through the sink AND return a structured error whose category
    ///   is `OrderErrorCategory::HostMemorySafetyMarginBreach`.
    /// * The refusal arm MUST NOT call `runtime.create(`,
    ///   `runtime.start(`, `runtime.destroy(`, or `runtime.restart(` —
    ///   the gate sits in front of the runtime port.
    /// * The arbitration loop MUST iterate ONLY `WorkloadKind::Batch`
    ///   candidates; the contract check pins the `.kind == WorkloadKind::Batch`
    ///   filter on the iterator.
    /// * The arbitration loop MUST contain a `debug_assert!` that the
    ///   evicted workload's priority is not `LiveStrategy`.
    /// * The happy path (sufficient headroom) MUST NOT call
    ///   `registry.terminate(` and MUST NOT emit any event.
    // Many host/registry/sink generic params by design; rich typed Err variant
    // (boxing it changes the public signature). Both refactors are out of scope here.
    #[allow(clippy::too_many_arguments, clippy::result_large_err)]
    pub fn admit_workload<H, R, S>(
        &self,
        request: &StrategyLaunchRequest,
        new_workload_id: WorkloadId,
        new_workload_priority: WorkloadPriority,
        safety_margin: HostMemorySafetyMargin,
        host: &H,
        registry: &R,
        sink: &S,
        observed_at_seconds: u64,
    ) -> Result<(), StructuredOrchestratorError>
    where
        H: HostMemoryProbe,
        R: WorkloadRegistry,
        S: WorkloadEventSink,
    {
        // (1) Margin validation — defence in depth so a
        // programmatically-constructed invalid margin cannot disable
        // the gate (codex critic: safety:margin-validation-bypass).
        if safety_margin.validate().is_err() {
            // Probe the host so the dashboard sees actual numbers,
            // and report the (invalid) configured margin so operators
            // see what was set. If the probe itself fails here, route
            // through HostProbeFailed (codex critic:
            // adapter:probe-error-swallowed) — never silently treat
            // an Err as available_mb=0.
            match host.available_mb() {
                Ok(available_mb) => {
                    // Audit emission is best-effort per WorkloadEventSink's
                    // trait contract — the admission decision is
                    // irreversible once made, so a sink failure does not
                    // abort or roll back the decision. A future wrapping
                    // caller can observe sink errors through the typed
                    // WorkloadEventSinkError surface; durable delivery
                    // belongs to the deferred SRS-NOTIF-001 dispatcher.
                    let _ = sink.record(WorkloadAdmissionEvent::Refused {
                        workload_id: new_workload_id,
                        priority: new_workload_priority,
                        reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                            available_mb,
                            safety_margin_mb: safety_margin.mb,
                        },
                        observed_at_seconds,
                    });
                    return Err(
                        StructuredOrchestratorError::host_memory_safety_margin_breach(
                            request.clone(),
                            available_mb,
                            safety_margin.mb,
                        ),
                    );
                }
                Err(probe_error) => {
                    let _ = sink.record(WorkloadAdmissionEvent::HostProbeFailed {
                        workload_id: new_workload_id,
                        priority: new_workload_priority,
                        failure_reason: probe_error.to_string(),
                        observed_at_seconds,
                    });
                    return Err(
                        StructuredOrchestratorError::host_memory_safety_margin_breach(
                            request.clone(),
                            0,
                            safety_margin.mb,
                        ),
                    );
                }
            }
        }

        // (2) Host probe — fail closed on a probe error (codex critic
        // adapter:probe-error-surface). The gate cannot safely decide
        // without a valid reading; emit a distinct HostProbeFailed
        // event so the dashboard can render the probe-specific
        // failure cause and refuse the admission.
        let mut available_mb = match host.available_mb() {
            Ok(mb) => mb,
            Err(probe_error) => {
                let _ = sink.record(WorkloadAdmissionEvent::HostProbeFailed {
                    workload_id: new_workload_id,
                    priority: new_workload_priority,
                    failure_reason: probe_error.to_string(),
                    observed_at_seconds,
                });
                return Err(
                    StructuredOrchestratorError::host_memory_safety_margin_breach(
                        request.clone(),
                        0,
                        safety_margin.mb,
                    ),
                );
            }
        };
        let needed_mb = u64::from(request.profile.mem_mb);
        let margin_mb = u64::from(safety_margin.mb);

        // (3) SyRS SYS-58 (a) happy path: if admitting leaves the host
        // above the safety margin, nothing else to do.
        if available_mb.saturating_sub(needed_mb) >= margin_mb {
            return Ok(());
        }

        // (4) SyRS SYS-58 (b) arbitration: walk batch candidates from
        // lowest priority (highest rank) to highest priority. Only
        // candidates strictly lower priority than the incoming
        // workload are eligible for eviction. Fail closed if the
        // registry listing fails (codex critic adapter:error-surface).
        let active = match registry.active() {
            Ok(active) => active,
            Err(registry_error) => {
                // codex critic adapter:registry-error-surface — emit a
                // distinct RegistryListingFailed event so the
                // dashboard surfaces the typed error cause rather
                // than collapsing into a generic margin breach.
                let _ = sink.record(WorkloadAdmissionEvent::RegistryListingFailed {
                    workload_id: new_workload_id,
                    priority: new_workload_priority,
                    failure_reason: registry_error.to_string(),
                    observed_at_seconds,
                });
                return Err(
                    StructuredOrchestratorError::host_memory_safety_margin_breach(
                        request.clone(),
                        available_mb,
                        safety_margin.mb,
                    ),
                );
            }
        };
        let mut eligible_candidates: Vec<RegisteredWorkload> = active
            .into_iter()
            .filter(|workload| workload.kind == WorkloadKind::Batch)
            .filter(|workload| workload.priority.rank() > new_workload_priority.rank())
            .collect();
        eligible_candidates.sort_by_key(|workload| std::cmp::Reverse(workload.priority.rank()));

        // (4) Pre-eviction feasibility: if even the SUM of all
        // eligible candidates' memory cannot bring the host above the
        // safety margin, refuse without any terminate call. This
        // prevents the codex partial-eviction-refusal failure mode:
        // killing lower-priority work and still returning a refusal.
        let recoverable_mb: u64 = eligible_candidates
            .iter()
            .map(|workload| u64::from(workload.profile.mem_mb))
            .sum();
        if available_mb
            .saturating_add(recoverable_mb)
            .saturating_sub(needed_mb)
            < margin_mb
        {
            let _ = sink.record(WorkloadAdmissionEvent::Refused {
                workload_id: new_workload_id,
                priority: new_workload_priority,
                reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                    available_mb,
                    safety_margin_mb: safety_margin.mb,
                },
                observed_at_seconds,
            });
            return Err(
                StructuredOrchestratorError::host_memory_safety_margin_breach(
                    request.clone(),
                    available_mb,
                    safety_margin.mb,
                ),
            );
        }

        // (5) Eviction loop. Each `terminate` returns Result so the
        // gate distinguishes successful eviction from a Docker /
        // registry failure: on Err the candidate is NOT banked as
        // freed memory (the workload may still be running) and the
        // loop continues to the next eligible candidate.
        for candidate in eligible_candidates {
            // SyRS SYS-58 last clause: never terminate the live-trading
            // strategy. Live is Continuous so the kind-filter already
            // excludes it; this debug_assert pins the invariant.
            debug_assert!(
                candidate.priority != WorkloadPriority::LiveStrategy,
                "SyRS SYS-58 invariant: live strategy must never be selected for eviction"
            );
            match registry.terminate(&candidate.id) {
                Ok(()) => {
                    let freed_mb = u64::from(candidate.profile.mem_mb);
                    let _ = sink.record(WorkloadAdmissionEvent::Terminated {
                        terminated_workload_id: candidate.id.clone(),
                        terminated_priority: candidate.priority,
                        admitted_workload_id: new_workload_id.clone(),
                        admitted_priority: new_workload_priority,
                        reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                            available_mb,
                            safety_margin_mb: safety_margin.mb,
                        },
                        observed_at_seconds,
                    });
                    available_mb = available_mb.saturating_add(freed_mb);
                    if available_mb.saturating_sub(needed_mb) >= margin_mb {
                        return Ok(());
                    }
                }
                Err(termination_error) => {
                    // codex critic adapter:silent-failure: emit a
                    // structured TerminationFailed audit event so
                    // operators see the failed eviction (rather than
                    // it disappearing into the gate's loop). Don't
                    // bank the memory — the workload may still be
                    // consuming it.
                    let _ = sink.record(WorkloadAdmissionEvent::TerminationFailed {
                        attempted_workload_id: candidate.id.clone(),
                        attempted_priority: candidate.priority,
                        admitted_workload_id: new_workload_id.clone(),
                        admitted_priority: new_workload_priority,
                        failure_reason: termination_error.to_string(),
                        observed_at_seconds,
                    });
                }
            }
        }

        // (6) Post-loop refusal: all eligible candidates exhausted (or
        // failed) and host is still below margin. Refuse and alert.
        let _ = sink.record(WorkloadAdmissionEvent::Refused {
            workload_id: new_workload_id,
            priority: new_workload_priority,
            reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                available_mb,
                safety_margin_mb: safety_margin.mb,
            },
            observed_at_seconds,
        });
        Err(
            StructuredOrchestratorError::host_memory_safety_margin_breach(
                request.clone(),
                available_mb,
                safety_margin.mb,
            ),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::{SourceHash, StrategyMode};
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

    /// Test fixture: a valid 64-hex SHA-256 wire-form source hash so
    /// SRS-ORCH-004 launch validation passes the gate. The orchestrator
    /// crate's tests share one canonical fixture string so a future
    /// drift in the validator catches every test at once.
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

    fn request(id: &str, mode: StrategyMode) -> StrategyLaunchRequest {
        StrategyLaunchRequest {
            strategy_id: StrategyId::new(id),
            mode,
            deployment_hash: SourceHash::new(TEST_SOURCE_HASH),
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
            deployment_hash: SourceHash::new(TEST_SOURCE_HASH),
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
            atp_types::OrderErrorCategory::StrategyStartupDeadlineExceeded
        );
        assert_eq!(
            error.category.as_str(),
            "STRATEGY_STARTUP_DEADLINE_EXCEEDED"
        );
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
        let state =
            orchestrator.observe_health(StrategyId::new("alpha-1"), &runtime, &sink, 1_715_000_000);
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
        let state =
            orchestrator.observe_health(StrategyId::new("alpha-1"), &runtime, &sink, 1_715_000_000);
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
        let _ =
            orchestrator.observe_health(StrategyId::new("alpha-1"), &runtime, &sink, 1_715_000_000);
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
            LaunchReadiness::ReadyWithinDeadline {
                elapsed_millis: 4_200,
            },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let version_registry = VersionRegistrySpy::default();
        let custom = ResourceProfile {
            mem_mb: 384,
            cpu_hundredths: 20,
        };
        let outcome = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, custom),
                &runtime,
                &sink,
                &version_registry,
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
        let version_registry = ForbiddenVersionRegistry;
        let bad = ResourceProfile {
            mem_mb: 32,
            cpu_hundredths: 25,
        };
        let error = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, bad),
                &runtime,
                &sink,
                &version_registry,
                1_715_000_000,
            )
            .expect_err("below-floor mem must be refused");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::ResourceProfileInvalid
        );
        assert_eq!(error.error_type, "ResourceProfileInvalid::MemBelowFloor");
        assert_eq!(error.original_request.profile, bad);
        assert_eq!(
            runtime.create_calls.get(),
            0,
            "validation gate must short-circuit"
        );
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
        let version_registry = ForbiddenVersionRegistry;
        let bad = ResourceProfile {
            mem_mb: 512,
            cpu_hundredths: 9_999,
        };
        let error = orchestrator
            .launch(
                request_with_profile("alpha-1", StrategyMode::Live, bad),
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
        let env_lookup = |_name: &str| -> Result<String, std::env::VarError> {
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
        let env_lookup = |name: &str| -> Result<String, std::env::VarError> {
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
        let env_lookup = |name: &str| -> Result<String, std::env::VarError> {
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

    // ----------------------------------------------------------------------- //
    // SRS-ORCH-003 workload-priority admission gate
    // ----------------------------------------------------------------------- //

    struct HostMemoryStub {
        available_mb: Cell<u64>,
        probe_calls: Cell<u32>,
    }

    impl HostMemoryStub {
        fn new(available_mb: u64) -> Self {
            Self {
                available_mb: Cell::new(available_mb),
                probe_calls: Cell::new(0),
            }
        }
    }

    impl HostMemoryProbe for HostMemoryStub {
        fn available_mb(&self) -> Result<u64, HostMemoryProbeError> {
            self.probe_calls.set(self.probe_calls.get() + 1);
            Ok(self.available_mb.get())
        }
    }

    #[derive(Default)]
    struct WorkloadRegistrySpy {
        workloads: RefCell<Vec<RegisteredWorkload>>,
        terminate_calls: RefCell<Vec<WorkloadId>>,
    }

    impl WorkloadRegistrySpy {
        fn with(workloads: Vec<RegisteredWorkload>) -> Self {
            Self {
                workloads: RefCell::new(workloads),
                terminate_calls: RefCell::new(Vec::new()),
            }
        }
    }

    impl WorkloadRegistry for WorkloadRegistrySpy {
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
    struct WorkloadEventSpy {
        events: RefCell<Vec<WorkloadAdmissionEvent>>,
    }

    impl WorkloadEventSink for WorkloadEventSpy {
        fn record(&self, event: WorkloadAdmissionEvent) -> Result<(), WorkloadEventSinkError> {
            self.events.borrow_mut().push(event);
            Ok(())
        }
    }

    struct ForbiddenWorkloadEventSink;

    impl WorkloadEventSink for ForbiddenWorkloadEventSink {
        fn record(&self, _event: WorkloadAdmissionEvent) -> Result<(), WorkloadEventSinkError> {
            panic!("happy path must not emit a WorkloadAdmissionEvent");
        }
    }

    struct ForbiddenWorkloadRegistry;

    impl WorkloadRegistry for ForbiddenWorkloadRegistry {
        fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError> {
            Ok(Vec::new())
        }

        fn terminate(&self, _id: &WorkloadId) -> Result<(), WorkloadTerminationError> {
            panic!("happy path must not call WorkloadRegistry::terminate");
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
    fn admit_workload_admits_when_headroom_exceeds_safety_margin() {
        // SyRS SYS-57 happy path: available - needed >= margin →
        // admit silently. No events, no registry mutation.
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(8_192);
        let registry = ForbiddenWorkloadRegistry;
        let sink = ForbiddenWorkloadEventSink;
        let request = request("alpha-paper-1", StrategyMode::Paper);
        let outcome = orchestrator.admit_workload(
            &request,
            WorkloadId::new("alpha-paper-1"),
            WorkloadPriority::PaperStrategy,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        );
        assert!(outcome.is_ok(), "ample headroom must admit");
        assert_eq!(host.probe_calls.get(), 1);
    }

    #[test]
    fn admit_workload_refuses_when_no_batch_evictable_and_emits_refused() {
        // SyRS SYS-58 (a): available below margin AND no batch
        // workloads to evict → refuse with structured error +
        // Refused event.
        let orchestrator = StrategyOrchestrator;
        // Available 2200 MB, paper needs 300 MB → post-admit 1900 MB,
        // which is below the 2048 MB safety margin. No batch workloads
        // in the registry, so no eviction possible.
        let host = HostMemoryStub::new(2_200);
        let registry = WorkloadRegistrySpy::with(vec![]);
        let sink = WorkloadEventSpy::default();
        let request = request("paper-2", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-2"),
                WorkloadPriority::PaperStrategy,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("post-admit below margin with no eviction must refuse");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        assert_eq!(error.category.as_str(), "HOST_MEMORY_SAFETY_MARGIN_BREACH");
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one Refused event must be emitted");
        match &events[0] {
            WorkloadAdmissionEvent::Refused {
                workload_id,
                priority,
                reason,
                ..
            } => {
                assert_eq!(workload_id.as_str(), "paper-2");
                assert_eq!(*priority, WorkloadPriority::PaperStrategy);
                match reason {
                    WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                        available_mb,
                        safety_margin_mb,
                    } => {
                        assert_eq!(*available_mb, 2_200);
                        assert_eq!(*safety_margin_mb, 2_048);
                    }
                }
            }
            other => panic!("expected Refused, got {other:?}"),
        }
        assert!(
            registry.terminate_calls.borrow().is_empty(),
            "refusal with no batch evictable must not call terminate"
        );
    }

    #[test]
    fn admit_workload_admits_after_evicting_lowest_priority_batch() {
        // SyRS SYS-58 (b): incoming MarketData (rank 2) needs headroom;
        // a Research batch (rank 7) sits in the registry → evict
        // Research, admit MarketData. Exactly one Terminated event.
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(2_300);
        let registry = WorkloadRegistrySpy::with(vec![workload(
            "research-jupyter-01",
            WorkloadPriority::Research,
            512,
        )]);
        let sink = WorkloadEventSpy::default();
        // Use a Live request (which trivially outranks Research) and
        // ask the gate to admit it as MarketData priority. The gate
        // only cares about new_workload_priority for the arbitration
        // comparison; the request profile drives `needed_mb`.
        let request = request("md-subscriber", StrategyMode::Paper);
        let outcome = orchestrator.admit_workload(
            &request,
            WorkloadId::new("md-subscriber"),
            WorkloadPriority::MarketDataSubscriptionManager,
            HostMemorySafetyMargin::default_margin(),
            &host,
            &registry,
            &sink,
            1_715_700_000,
        );
        assert!(outcome.is_ok(), "post-eviction headroom must admit");
        let terminate_calls = registry.terminate_calls.borrow();
        assert_eq!(terminate_calls.len(), 1);
        assert_eq!(terminate_calls[0].as_str(), "research-jupyter-01");
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1);
        match &events[0] {
            WorkloadAdmissionEvent::Terminated {
                terminated_workload_id,
                terminated_priority,
                admitted_workload_id,
                admitted_priority,
                ..
            } => {
                assert_eq!(terminated_workload_id.as_str(), "research-jupyter-01");
                assert_eq!(*terminated_priority, WorkloadPriority::Research);
                assert_eq!(admitted_workload_id.as_str(), "md-subscriber");
                assert_eq!(
                    *admitted_priority,
                    WorkloadPriority::MarketDataSubscriptionManager
                );
            }
            other => panic!("expected Terminated, got {other:?}"),
        }
    }

    #[test]
    fn admit_workload_evicts_lowest_priority_first() {
        // SyRS SYS-57 hierarchy ordering: multiple batch workloads
        // present, only the lowest-priority one is evicted (Research,
        // rank 7) and NOT the FactorPipeline (rank 5).
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(2_300);
        let registry = WorkloadRegistrySpy::with(vec![
            workload("factor-nightly", WorkloadPriority::FactorPipeline, 512),
            workload("research-jupyter-01", WorkloadPriority::Research, 512),
            workload("backtest-2026-05-14", WorkloadPriority::Backtest, 512),
        ]);
        let sink = WorkloadEventSpy::default();
        let request = request("md-subscriber", StrategyMode::Paper);
        orchestrator
            .admit_workload(
                &request,
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
            "lowest-priority batch (Research, rank 7) must be evicted first"
        );
    }

    #[test]
    fn admit_workload_skips_continuous_workloads_during_arbitration() {
        // SyRS SYS-58 (b): only BATCH workloads may be terminated.
        // Paper strategies are Continuous (immune from eviction);
        // even if they are the lowest-priority active workload, the
        // arbitration loop must skip them and refuse the admission.
        let orchestrator = StrategyOrchestrator;
        // Only Paper continuous workloads in the registry — none of
        // them are eligible for eviction.
        let host = HostMemoryStub::new(2_200);
        let registry = WorkloadRegistrySpy::with(vec![
            workload("paper-1", WorkloadPriority::PaperStrategy, 300),
            workload("paper-2", WorkloadPriority::PaperStrategy, 300),
        ]);
        let sink = WorkloadEventSpy::default();
        let request = request("md-subscriber", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("md-subscriber"),
                WorkloadPriority::MarketDataSubscriptionManager,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("only-continuous registry must refuse, never evict");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        assert!(
            registry.terminate_calls.borrow().is_empty(),
            "Continuous workloads must never be selected for eviction"
        );
    }

    #[test]
    fn admit_workload_refuses_when_incoming_priority_is_lower_than_all_batch() {
        // SyRS SYS-58 (b) wording: terminate batch workloads ONLY if a
        // higher-priority workload requires resources. A Research
        // (rank 7) workload arriving when only a Backtest (rank 6)
        // batch is active must NOT evict the Backtest — Research
        // doesn't outrank Backtest.
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(2_200);
        let registry = WorkloadRegistrySpy::with(vec![workload(
            "backtest-2026-05-14",
            WorkloadPriority::Backtest,
            512,
        )]);
        let sink = WorkloadEventSpy::default();
        let request = request("research-jupyter-02", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("research-jupyter-02"),
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
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        assert!(
            registry.terminate_calls.borrow().is_empty(),
            "lower-priority incoming must not trigger any eviction"
        );
    }

    #[test]
    fn admit_workload_refuses_without_eviction_when_pre_check_says_insufficient() {
        // SyRS SYS-58 (b) + codex orch:partial-eviction-refusal: if
        // evicting ALL eligible batch workloads cannot free enough
        // memory, the gate must refuse the new workload WITHOUT
        // killing any work. The previous design killed work then
        // refused — a strictly worse outcome (lost work + still no
        // admission).
        let orchestrator = StrategyOrchestrator;
        // Host has 500 MB available, paper needs 300 MB → post-admit
        // 200 MB. Margin is 2048 MB. Sum of 3 batch (each 100 MB) is
        // 300 MB → post-admit-after-eviction 500 MB, still 1548 MB
        // short of the 2048 MB margin.
        let host = HostMemoryStub::new(500);
        let registry = WorkloadRegistrySpy::with(vec![
            workload("factor-1", WorkloadPriority::FactorPipeline, 100),
            workload("backtest-1", WorkloadPriority::Backtest, 100),
            workload("research-1", WorkloadPriority::Research, 100),
        ]);
        let sink = WorkloadEventSpy::default();
        let request = request("paper-md", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-md"),
                WorkloadPriority::MarketDataSubscriptionManager,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("pre-check below margin must refuse without eviction");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        // NO workloads were evicted — the pre-check proved
        // termination would not be enough.
        assert!(
            registry.terminate_calls.borrow().is_empty(),
            "pre-check refusal must not call terminate"
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one Refused event");
        assert!(matches!(events[0], WorkloadAdmissionEvent::Refused { .. }));
    }

    #[test]
    fn admit_workload_validates_safety_margin_before_arbitration() {
        // codex critic safety:margin-validation-bypass: a
        // programmatically-constructed margin below the catalogue
        // floor must NOT silently disable the gate. The gate calls
        // validate() defensively and refuses with the breach factory.
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(8_192);
        // Empty registry — no batch evictions possible.
        let registry = WorkloadRegistrySpy::with(vec![]);
        let sink = WorkloadEventSpy::default();
        let request = request("paper-1", StrategyMode::Paper);
        // Margin 0 would disable the gate; validate() rejects it.
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-1"),
                WorkloadPriority::PaperStrategy,
                HostMemorySafetyMargin { mb: 0 },
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("invalid safety margin must be refused");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        // No terminate, no probe-spy at the arbitration loop — just
        // the early refusal.
        assert!(registry.terminate_calls.borrow().is_empty());
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1);
        assert!(matches!(events[0], WorkloadAdmissionEvent::Refused { .. }));
    }

    #[test]
    fn admit_workload_does_not_bank_memory_for_failed_terminate() {
        // codex critic adapter:error-surface: terminate returns
        // Result so the gate can tell whether the registry actually
        // killed the workload. On Err, the gate must NOT bank the
        // memory (the workload may still be consuming it) and must
        // continue trying other candidates.
        struct FlakyRegistry {
            workloads: RefCell<Vec<RegisteredWorkload>>,
            terminate_calls: RefCell<Vec<WorkloadId>>,
            fail_id: WorkloadId,
        }
        impl WorkloadRegistry for FlakyRegistry {
            fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError> {
                Ok(self.workloads.borrow().clone())
            }
            fn terminate(&self, id: &WorkloadId) -> Result<(), WorkloadTerminationError> {
                self.terminate_calls.borrow_mut().push(id.clone());
                if *id == self.fail_id {
                    return Err(WorkloadTerminationError::new(
                        id.clone(),
                        "docker shutdown timeout",
                    ));
                }
                self.workloads
                    .borrow_mut()
                    .retain(|workload| workload.id != *id);
                Ok(())
            }
        }

        let orchestrator = StrategyOrchestrator;
        // Host has 2300 MB, paper needs 300 → post-admit 2000 (margin
        // 2048 → deficit 48). Each batch workload is 100 MB. Research
        // (rank 7) is the first candidate but terminate fails for it;
        // Backtest (rank 6, the next eligible candidate) succeeds and
        // frees 100 MB → post-admit 2100, above margin.
        let host = HostMemoryStub::new(2_300);
        let registry = FlakyRegistry {
            workloads: RefCell::new(vec![
                workload("backtest-1", WorkloadPriority::Backtest, 100),
                workload("research-1", WorkloadPriority::Research, 100),
            ]),
            terminate_calls: RefCell::new(Vec::new()),
            fail_id: WorkloadId::new("research-1"),
        };
        let sink = WorkloadEventSpy::default();
        let request = request("paper-md", StrategyMode::Paper);
        orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-md"),
                WorkloadPriority::MarketDataSubscriptionManager,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect("post-flaky-eviction headroom must admit via fallback candidate");
        // The gate tried Research first (Err), then Backtest (Ok).
        let calls = registry.terminate_calls.borrow();
        assert_eq!(calls.len(), 2);
        assert_eq!(calls[0].as_str(), "research-1");
        assert_eq!(calls[1].as_str(), "backtest-1");
        // The gate emitted TerminationFailed for Research and
        // Terminated for Backtest (no silent failure).
        let events = sink.events.borrow();
        assert_eq!(events.len(), 2);
        match &events[0] {
            WorkloadAdmissionEvent::TerminationFailed {
                attempted_workload_id,
                failure_reason,
                ..
            } => {
                assert_eq!(attempted_workload_id.as_str(), "research-1");
                // codex adapter:termination-error-surface: the typed
                // reason must propagate so the dashboard can render
                // the specific failure (Docker timeout, permission
                // denied, etc.) rather than collapsing to a generic
                // string.
                assert!(failure_reason.contains("docker shutdown timeout"));
            }
            other => panic!("expected TerminationFailed for research-1, got {other:?}"),
        }
        match &events[1] {
            WorkloadAdmissionEvent::Terminated {
                terminated_workload_id,
                ..
            } => assert_eq!(terminated_workload_id.as_str(), "backtest-1"),
            other => panic!("expected Terminated for backtest-1, got {other:?}"),
        }
    }

    #[test]
    fn admit_workload_routes_probe_error_through_host_probe_failed_even_with_invalid_margin() {
        // codex critic adapter:probe-error-swallowed: when both the
        // configured margin is invalid AND the probe fails, the gate
        // must NOT use unwrap_or(0) — it must emit HostProbeFailed
        // so the dashboard sees the typed probe error cause.
        struct FailingProbe;
        impl HostMemoryProbe for FailingProbe {
            fn available_mb(&self) -> Result<u64, HostMemoryProbeError> {
                Err(HostMemoryProbeError::new("procfs unreadable"))
            }
        }
        let orchestrator = StrategyOrchestrator;
        let host = FailingProbe;
        let registry = WorkloadRegistrySpy::with(vec![]);
        let sink = WorkloadEventSpy::default();
        let request = request("paper-1", StrategyMode::Paper);
        // Invalid margin (mb=0 is below the catalogue floor).
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-1"),
                WorkloadPriority::PaperStrategy,
                HostMemorySafetyMargin { mb: 0 },
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("invalid margin + probe failure must refuse");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1);
        match &events[0] {
            WorkloadAdmissionEvent::HostProbeFailed { failure_reason, .. } => {
                assert!(failure_reason.contains("procfs unreadable"))
            }
            other => panic!(
                "expected HostProbeFailed (probe error must not be silently \
                 unwrap_or(0)'d), got {other:?}"
            ),
        }
    }

    #[test]
    fn admit_workload_fails_closed_on_host_probe_error() {
        // codex critic adapter:error-surface: a probe error must not
        // be silently treated as "available_mb = 0 + admit". The gate
        // refuses with a structured error AND emits a Refused event.
        struct FailingProbe;
        impl HostMemoryProbe for FailingProbe {
            fn available_mb(&self) -> Result<u64, HostMemoryProbeError> {
                Err(HostMemoryProbeError::new("sysinfo refresh failed"))
            }
        }
        let orchestrator = StrategyOrchestrator;
        let host = FailingProbe;
        let registry = WorkloadRegistrySpy::with(vec![]);
        let sink = WorkloadEventSpy::default();
        let request = request("paper-1", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-1"),
                WorkloadPriority::PaperStrategy,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("probe error must refuse");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1);
        match &events[0] {
            WorkloadAdmissionEvent::HostProbeFailed { failure_reason, .. } => {
                assert!(failure_reason.contains("sysinfo refresh failed"));
            }
            other => panic!("expected HostProbeFailed, got {other:?}"),
        }
    }

    #[test]
    fn admit_workload_fails_closed_on_registry_active_error() {
        // codex critic adapter:error-surface: registry listing
        // failure must not silently be treated as "no candidates".
        struct FailingRegistry;
        impl WorkloadRegistry for FailingRegistry {
            fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError> {
                Err(WorkloadRegistryError::new("docker engine timeout"))
            }
            fn terminate(&self, _id: &WorkloadId) -> Result<(), WorkloadTerminationError> {
                panic!("must not call terminate when active() failed");
            }
        }
        let orchestrator = StrategyOrchestrator;
        // Force the arbitration arm: host below margin after admit.
        let host = HostMemoryStub::new(2_200);
        let registry = FailingRegistry;
        let sink = WorkloadEventSpy::default();
        let request = request("paper-1", StrategyMode::Paper);
        let error = orchestrator
            .admit_workload(
                &request,
                WorkloadId::new("paper-1"),
                WorkloadPriority::PaperStrategy,
                HostMemorySafetyMargin::default_margin(),
                &host,
                &registry,
                &sink,
                1_715_700_000,
            )
            .expect_err("registry listing failure must refuse");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1);
        match &events[0] {
            WorkloadAdmissionEvent::RegistryListingFailed { failure_reason, .. } => {
                assert!(failure_reason.contains("docker engine timeout"));
            }
            other => panic!("expected RegistryListingFailed, got {other:?}"),
        }
    }

    #[test]
    fn admit_workload_uses_configurable_safety_margin_override() {
        // SRS-ORCH-003 "configuration overrides are validated": the
        // gate honours a non-default margin. With margin=512 and
        // available=1000, paper needing 300 leaves 700 — above 512,
        // so admit silently.
        let orchestrator = StrategyOrchestrator;
        let host = HostMemoryStub::new(1_000);
        let registry = ForbiddenWorkloadRegistry;
        let sink = ForbiddenWorkloadEventSink;
        let request = request("paper-1", StrategyMode::Paper);
        let outcome = orchestrator.admit_workload(
            &request,
            WorkloadId::new("paper-1"),
            WorkloadPriority::PaperStrategy,
            HostMemorySafetyMargin { mb: 512 },
            &host,
            &registry,
            &sink,
            1_715_700_000,
        );
        assert!(outcome.is_ok(), "headroom above custom margin must admit");
    }

    #[test]
    fn safety_margin_from_lookup_falls_back_to_default_when_absent() {
        let orchestrator = StrategyOrchestrator;
        let margin = orchestrator
            .safety_margin_from_lookup(|_| None)
            .expect("absent override must fall back to SyRS default");
        assert_eq!(margin.mb, 2_048);
    }

    #[test]
    fn safety_margin_from_lookup_honours_in_range_override() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| match name {
            "ATP_HOST_MEMORY_SAFETY_MARGIN_MB" => Some("4096".to_string()),
            _ => None,
        };
        let margin = orchestrator
            .safety_margin_from_lookup(lookup)
            .expect("in-range override must validate");
        assert_eq!(margin.mb, 4_096);
    }

    #[test]
    fn safety_margin_from_lookup_rejects_unparseable_override() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| match name {
            "ATP_HOST_MEMORY_SAFETY_MARGIN_MB" => Some("not-a-number".to_string()),
            _ => None,
        };
        let err = orchestrator
            .safety_margin_from_lookup(lookup)
            .expect_err("unparseable override must surface, not collapse to default");
        assert!(matches!(
            err,
            HostMemorySafetyMarginEnvError::Unparseable { .. }
        ));
    }

    #[test]
    fn safety_margin_from_lookup_rejects_below_floor() {
        let orchestrator = StrategyOrchestrator;
        let lookup = |name: &str| match name {
            "ATP_HOST_MEMORY_SAFETY_MARGIN_MB" => Some("100".to_string()),
            _ => None,
        };
        let err = orchestrator
            .safety_margin_from_lookup(lookup)
            .expect_err("below-floor margin must be rejected");
        assert!(matches!(
            err,
            HostMemorySafetyMarginEnvError::Validation(
                HostMemorySafetyMarginError::BelowFloor { .. }
            )
        ));
    }

    #[test]
    fn safety_margin_via_env_lookup_distinguishes_not_present_from_not_unicode() {
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
        let env_lookup = |name: &str| -> Result<String, std::env::VarError> {
            if name == "ATP_HOST_MEMORY_SAFETY_MARGIN_MB" {
                Err(std::env::VarError::NotUnicode(bad.clone()))
            } else {
                Err(std::env::VarError::NotPresent)
            }
        };
        let err = orchestrator
            .safety_margin_via_env_lookup(env_lookup)
            .expect_err("NotUnicode must surface as Unparseable");
        match err {
            HostMemorySafetyMarginEnvError::Unparseable { var, .. } => {
                assert_eq!(var, "ATP_HOST_MEMORY_SAFETY_MARGIN_MB");
            }
            other => panic!("expected Unparseable, got {other:?}"),
        }
    }

    // ---------------------------------------------------------------- //
    // SRS-ORCH-004 deployed-version recording (SyRS SYS-79)
    // ---------------------------------------------------------------- //

    #[test]
    fn launch_records_deployed_version_on_ready_within_deadline() {
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
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
                1_715_700_000,
            )
            .expect("ReadyWithinDeadline must accept the launch");

        // SRS-ORCH-004 acceptance: the outcome carries the deployed
        // version (hash + deployment timestamp) and the same record
        // appears in the registry.
        assert_eq!(
            outcome.deployed_version.source_hash.as_str(),
            TEST_SOURCE_HASH
        );
        assert_eq!(outcome.deployed_version.deployed_at_seconds, 1_715_700_000);

        let records = version_registry.records.borrow();
        assert_eq!(records.len(), 1, "exactly one record per successful launch");
        assert_eq!(records[0].0.as_str(), "alpha-1");
        assert_eq!(records[0].1, outcome.deployed_version);
    }

    #[test]
    fn launch_does_not_record_version_on_deadline_exceeded() {
        // SRS-ORCH-004: a version that was never deployed must not
        // appear in the active-strategy inventory (SYS-41) or the
        // REST API listing (IF-9). The over-deadline path destroys
        // the container; it must also skip the version record.
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::DeadlineExceeded {
                elapsed_millis: 32_500,
                deadline_millis: 30_000,
            },
            ContainerHealthState::Healthy,
        );
        let sink = SinkSpy::default();
        let version_registry = ForbiddenVersionRegistry;
        let _error = orchestrator
            .launch(
                request("alpha-1", StrategyMode::Live),
                &runtime,
                &sink,
                &version_registry,
                1_715_700_000,
            )
            .expect_err("DeadlineExceeded must refuse the launch");
    }

    #[test]
    fn launch_rejects_malformed_source_hash_without_invoking_runtime() {
        // SRS-ORCH-004: validate-before-create. A malformed hash must
        // never reach `runtime.create` — the gate short-circuits with
        // DeployedVersionInvalid and emits no event.
        let orchestrator = StrategyOrchestrator;
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline { elapsed_millis: 1 },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let version_registry = ForbiddenVersionRegistry;
        let bad_request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: SourceHash::new("sha256:not-a-real-hash"),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::live_default(),
        };
        let error = orchestrator
            .launch(
                bad_request,
                &runtime,
                &sink,
                &version_registry,
                1_715_700_000,
            )
            .expect_err("malformed hash must be refused");
        assert_eq!(
            error.category,
            atp_types::OrderErrorCategory::DeployedVersionInvalid
        );
        assert!(error.error_type.starts_with("DeployedVersionInvalid::"));
        assert_eq!(
            runtime.create_calls.get(),
            0,
            "validation gate must short-circuit"
        );
        assert_eq!(runtime.start_calls.get(), 0);
    }

    #[test]
    fn launch_succeeds_even_when_version_registry_record_fails() {
        // SRS-ORCH-004: the version record is best-effort. Once the
        // container is running, a registry-record failure must NOT
        // abort the launch or destroy the container — the launch
        // outcome carries the deployed version regardless.
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
        let runtime = RuntimeStub::new(
            LaunchReadiness::ReadyWithinDeadline {
                elapsed_millis: 4_200,
            },
            ContainerHealthState::Healthy,
        );
        let sink = ForbiddenSink;
        let version_registry = FlakyRegistry;
        let outcome = orchestrator
            .launch(
                request("alpha-1", StrategyMode::Live),
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
    }

    #[test]
    fn deployed_version_for_helper_builds_same_record_as_launch() {
        // SRS-ORCH-004: callers can preview the deployed version
        // identifier without invoking the runtime port. The helper
        // produces the same record the gate would build.
        let orchestrator = StrategyOrchestrator;
        let req = request("alpha-1", StrategyMode::Live);
        let preview = orchestrator.deployed_version_for(&req, 1_715_700_000);
        assert_eq!(preview.source_hash, req.deployment_hash);
        assert_eq!(preview.deployed_at_seconds, 1_715_700_000);
        assert_eq!(
            preview.version_identifier(),
            format!("{TEST_SOURCE_HASH}@1715700000")
        );
    }
}
