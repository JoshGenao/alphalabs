// `StructuredOrderError` (SRS-ERR-001) carries the unchanged original order for
// the audit trail; as of SRS-EXE-003 the enriched `OrderSubmission` pushes that
// envelope over clippy's `result_large_err` threshold. The error path is cold and
// the envelope's contract is to carry the full order by value — accept the size.
#![allow(clippy::result_large_err)]

use atp_strategy_engine::StrategyRuntimeBoundary;
use atp_types::{
    ConnectivityEvent, ConnectivityState, KillSwitchAlertEvent, KillSwitchCleanupOutcome,
    KillSwitchLiquidationOutcome, KillSwitchTimeoutEvent, KillSwitchTimeoutRequest,
    MarketDataFreshness, OperatorAlertChannel, OrderErrorCategory, OrderReceipt, OrderSubmission,
    RuntimeService, SideEffectOutcome, StaleDataEvent, StrategyMode,
    StructuredKillSwitchTimeoutError, StructuredOrderError,
};
use std::fmt;

pub mod designation;
pub use designation::{
    LiveDesignation, LiveDesignationConfirmation, LiveDesignationError, LiveRoutingDecision,
};

pub mod order_routing;
pub use order_routing::{
    InternalSimulationSubmit, OrderRoute, OrderRoutingReceipt, SimulatedOrderReceipt,
};

/// The execution engine owns the single live-designation authority
/// ([`LiveDesignation`]) — the source of truth for which strategy may route to
/// IB (SRS-EXE-001, SyRS SYS-2a). It is a private field reached only through
/// [`designate`](Self::designate) / [`demote`](Self::demote) /
/// [`route_order`](Self::route_order), so a caller can never supply a forged
/// authority at the routing boundary.
#[derive(Debug, Default)]
pub struct ExecutionEngine {
    designation: LiveDesignation,
}

/// Port trait the execution engine uses to push an order to a live brokerage
/// after it has decided the submission is allowed (mode == Live, connected,
/// data not stale, etc.). Lives at the execution layer — adapter crates do
/// not implement it directly; the orchestrator wires an adapter to it.
///
/// Defining the port here keeps `atp-execution` independent of
/// `atp-adapters` (SRS-ARCH-002 dependency direction: adapters are a
/// sibling crate, not an upstream dep of execution).
pub trait LiveBrokerageSubmit {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError>;
}

/// ERR-2 / SRS-SAFE-003 / SRS-MD-005: port the execution engine consults at
/// every live submission to decide whether the brokerage path is reachable,
/// and to request a reconnect when the gate is closed. The implementation
/// (later: the IB adapter wired by the orchestrator) owns the actual TCP
/// probe / readiness check / restart-window detection. Defining the port
/// here keeps the safety gate observable from execution without pulling
/// `atp-execution` into a dependency on `atp-adapters`.
pub trait BrokerageConnectivity {
    fn state(&self) -> ConnectivityState;
    fn request_reconnect(&self);
}

/// ERR-2 / SRS-SAFE-003 / SRS-MD-005: structured-event sink the execution
/// engine pushes a `ConnectivityEvent` into whenever it blocks a live
/// submission. Concrete implementations (later) route the event to logs,
/// the dashboard WebSocket (`ALERTS` / `ACCOUNT_STATUS` channels), and the
/// notification dispatcher (SRS-NOTIF-001).
pub trait ConnectivityEventSink {
    fn record(&self, event: ConnectivityEvent);
}

/// ERR-3 / SRS-MD-004 / NFR-P5: port the execution engine consults at
/// every live submission to decide whether the subscribed market data for
/// the order's symbol is fresh enough to trade against. The implementation
/// (later: the market-data subscription manager) owns the actual heartbeat
/// timestamp / sequence-gap tracking and the configurable 15s threshold.
/// Defining the port at the execution layer keeps the data-freshness gate
/// observable from execution without pulling `atp-execution` into a
/// dependency on `atp-market-data` (SRS-ARCH-002 dependency direction).
pub trait MarketDataFreshnessProbe {
    fn freshness(&self, symbol: &str) -> MarketDataFreshness;
    fn staleness_seconds(&self, symbol: &str) -> u64;
}

/// ERR-3 / SRS-MD-004: structured-event sink the execution engine pushes a
/// `StaleDataEvent` into whenever it blocks a submission because market
/// data is stale. Concrete implementations (later) route the event to
/// logs (SRS-LOG-001), the dashboard WebSocket, and the notification
/// dispatcher. The sink lives at the execution layer alongside
/// `ConnectivityEventSink` so the staleness gate stays observable from
/// execution without a dependency on `atp-market-data`.
pub trait StaleDataEventSink {
    fn record(&self, event: StaleDataEvent);
}

// --------------------------------------------------------------------------- //
// Kill-switch liquidation-timeout ports (ERR-8, SRS-SAFE-002)
// (SyRS SYS-44b; StRS SN-1.11)
// --------------------------------------------------------------------------- //
//
// SRS-SAFE-002 is the kill-switch error path: when a liquidation order
// submitted by the kill switch (SRS-SAFE-001) stays unfilled past the
// configured timeout (default 30 s), the system logs the unfilled order
// details, notifies the operator by email AND SMS, cancels the unfilled
// liquidation order, and disconnects from IB. The execution engine owns
// kill-switch behavior (SRS-ARCH-001 service map), so the
// `resolve_kill_switch_timeout` gate and the ports it consumes live here —
// NOT in `atp-types` (which would invert the dependency direction
// SRS-ARCH-002) and NOT in `atp-orchestrator` (a higher layer that
// `atp-execution` must not depend on). The ERR-7 Hot-Swap demotion gate
// defines the analogous ports in `atp-orchestrator`; `atp-execution` cannot
// import them, so it declares its own — reusing only the shared `atp-types`
// vocabulary (`OperatorAlertChannel`, `SideEffectOutcome`).
//
// Four ports mediate the timeout decision:
//
//   * `KillSwitchLiquidationProbe` — the timing authority. Returns a
//     `KillSwitchLiquidationOutcome` discriminating `FilledBeforeTimeout`
//     from `TimedOutUnfilled` so the gate matches on the decision without
//     re-implementing the 30 s async wait loop (deferred runtime). Read-only.
//
//   * `KillSwitchOperatorAlertSink` — the SYS-44b email/SMS page. Fallible
//     so a missed page on a liquidation timeout (itself a safety event) is
//     surfaced, not silently dropped.
//
//   * `IbLiquidationCleanup` — the two SYS-44b IB Gateway actions performed
//     by the SAME adapter (deferred SRS-EXE-006), grouped under one owner:
//     `cancel_unfilled_liquidation_order` ("cancel the unfilled liquidation
//     order") and `disconnect` ("disconnect from IB"). Both fallible: a
//     failed cancel can leave a live order and a failed disconnect can leave
//     IB connected, each of which must be observable. (ERR-7 kept these as
//     separate ports, but cancel + disconnect are operations on the one IB
//     connection; grouping them keeps the gate at the same arity as
//     `resolve_demotion` and maps cleanly to the single deferred adapter.)
//
//   * `KillSwitchTimeoutEventSink` — the structured audit record (the logged
//     unfilled-order details + each side-effect outcome) for the deferred
//     SRS-LOG-001 / SRS-UI-001 consumers. Recorded on BOTH arms.
//
// Concrete impls of all four ports are the deferred runtime, enumerated in
// `architecture/runtime_services.json` `kill_switch_timeout_contract
// .deferred[]`.

pub trait KillSwitchLiquidationProbe {
    /// Await fill confirmation OR the configured timeout. On success returns
    /// `FilledBeforeTimeout { elapsed_seconds }` if the liquidation order
    /// fills within `request.timeout_seconds`, or `TimedOutUnfilled
    /// { elapsed_seconds, timeout_seconds }` if the deadline is breached. The
    /// `resolve_kill_switch_timeout` gate matches on the returned
    /// `KillSwitchLiquidationOutcome` so it never re-implements the wait-loop
    /// timing. No mutators: the gate has no side-effecting path through it.
    ///
    /// Returns `Result` because fill confirmation is an IB-touching boundary:
    /// the concrete probe (deferred SRS-EXE-006 runtime) can lose connectivity,
    /// find the order state unavailable, or itself time out while awaiting
    /// confirmation. Those degraded paths MUST surface as a typed error rather
    /// than be misclassified as `FilledBeforeTimeout` (which would pretend a
    /// liquidation that never confirmed succeeded) or `TimedOutUnfilled` (which
    /// would fire the destructive cancel on an unconfirmable order state). The
    /// gate handles the `Err` by failing closed WITHOUT any automated
    /// order/session change — see `resolve_kill_switch_timeout`. The typed
    /// connectivity / order-state / transport-timeout taxonomy (vs today's
    /// reason-string `KillSwitchProbeError`) lands with the concrete probe
    /// runtime (contract `deferred[]`).
    fn await_filled_or_timeout(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<KillSwitchLiquidationOutcome, KillSwitchProbeError>;
}

pub trait KillSwitchOperatorAlertSink {
    /// SYS-44b email/SMS operator page. Called ONLY on the `TimedOutUnfilled`
    /// branch. Returns `Result` so a transport failure (email/SMS
    /// unreachable) is surfaced rather than silently dropped — a missed page
    /// on a liquidation timeout is itself a safety event. The gate does NOT
    /// abort on failure; it records the outcome on
    /// `KillSwitchTimeoutEvent::operator_alert`. The concrete email/SMS
    /// transport is the deferred SRS-NOTIF-001 dispatcher.
    fn dispatch(&self, event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError>;
}

pub trait IbLiquidationCleanup {
    /// SYS-44b "cancel the unfilled liquidation order". Called ONLY on the
    /// `TimedOutUnfilled` branch. The concrete impl routes to the IB
    /// adapter's `cancel_order` (deferred SRS-EXE-006). Returns `Result` so
    /// an IB-cancel failure is surfaced rather than silently dropped: a
    /// failed cancel can leave a live liquidation order. The gate does NOT
    /// abort on failure; it records the outcome on
    /// `KillSwitchTimeoutEvent::liquidation_cancel`.
    fn cancel_unfilled_liquidation_order(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError>;

    /// SYS-44b "disconnect from IB". Called ONLY on the `TimedOutUnfilled`
    /// branch, after the cancel — the final safety action when a liquidation
    /// will not fill. The concrete impl routes to the IB adapter's disconnect
    /// (deferred SRS-EXE-006). Returns `Result` so a disconnect failure (IB
    /// session wedged) is surfaced; the gate records the outcome on
    /// `KillSwitchTimeoutEvent::ib_disconnect`. Distinct from
    /// `BrokerageConnectivity::request_reconnect` (the opposite direction).
    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError>;
}

/// ERR-8 / SRS-SAFE-002 side-effect failure surface for the timeout-branch
/// alert / cancel / disconnect ports. Mirrors `HotSwapSideEffectError`:
/// carries a short reason string for now; the typed CONNECTIVITY_BLOCKED /
/// transport-timeout taxonomy is added when the concrete IB-cancel/disconnect
/// (`atp-adapters`, SRS-EXE-006) and email/SMS (`atp-notification`,
/// SRS-NOTIF-001) runtimes land (named in the contract's `deferred[]`). The
/// gate maps an `Err` into `SideEffectOutcome::Failed { reason }` on the
/// audit event so the failure is observable end to end.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchSideEffectError {
    pub reason: String,
}

impl KillSwitchSideEffectError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for KillSwitchSideEffectError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-SAFE-002: kill-switch liquidation-timeout side effect failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for KillSwitchSideEffectError {}

/// ERR-8 / SRS-SAFE-002 failure surface for the fill-confirmation probe. The
/// concrete probe (deferred SRS-EXE-006 runtime) reaches the IB boundary to
/// confirm liquidation fills, so it can fail (connectivity lost, order state
/// unavailable, probe timeout). Carries a reason string for now; the typed
/// CONNECTIVITY_BLOCKED / order-state / transport-timeout taxonomy lands with
/// that runtime (contract `deferred[]`). When the probe returns this error the
/// gate fails closed WITHOUT any automated order/session change (see
/// `resolve_kill_switch_timeout`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchProbeError {
    pub reason: String,
}

impl KillSwitchProbeError {
    pub fn new(reason: impl Into<String>) -> Self {
        Self {
            reason: reason.into(),
        }
    }
}

impl fmt::Display for KillSwitchProbeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "SRS-SAFE-002: kill-switch liquidation fill-confirmation probe failed — {}",
            self.reason,
        )
    }
}

impl std::error::Error for KillSwitchProbeError {}

/// Map a timeout-branch side-effect port result into the observable
/// `SideEffectOutcome` recorded on `KillSwitchTimeoutEvent`. An `Err` is
/// preserved as `Failed { reason }` so the failure is surfaced on the audit
/// event rather than silently dropped.
fn into_outcome(result: Result<(), KillSwitchSideEffectError>) -> SideEffectOutcome {
    match result {
        Ok(()) => SideEffectOutcome::Succeeded,
        Err(error) => SideEffectOutcome::Failed {
            reason: error.reason,
        },
    }
}

pub trait KillSwitchTimeoutEventSink {
    /// Structured kill-switch timeout audit record for the dashboard / log
    /// fan-out (deferred SRS-LOG-001 / SRS-UI-001 consumers). Recorded on
    /// both arms of `resolve_kill_switch_timeout`. Returns `Result` so a
    /// concrete sink cannot silently swallow a publication failure; the gate
    /// treats emission as **best-effort** (mirrors ERR-7's
    /// `HotSwapDemotionEventSink`): the timeout decision is already made and
    /// the safety side effects (alert + cancel + disconnect) have already been
    /// attempted, so a sink failure does not roll them back. Durable delivery
    /// is the deferred SRS-LOG-001 sink's responsibility.
    fn record(&self, event: KillSwitchTimeoutEvent) -> Result<(), KillSwitchSideEffectError>;
}

/// SRS-SAFE-002 acceptance evidence: the kill-switch liquidation reached fill
/// before the timeout, so the SYS-44b error path did not engage. The `Err`
/// counterpart (`StructuredKillSwitchTimeoutError`) is the only other outcome
/// of `resolve_kill_switch_timeout`. `filled_before_timeout` is carried
/// explicitly (always `true` on this struct) so the dashboard / REST surface
/// renders the gate decision without re-deriving it. (The full SRS-SAFE-001
/// liquidate sequence — halt paper engines, always disconnect — is the
/// deferred kill-switch runtime; this slice models only the SRS-SAFE-002
/// timeout decision.)
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchLiquidationResolved {
    pub live_strategy_id: atp_types::StrategyId,
    pub filled_before_timeout: bool,
    pub elapsed_seconds: u64,
}

impl ExecutionEngine {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::ExecutionEngine
    }

    pub fn accepts_live_boundary(&self, boundary: &StrategyRuntimeBoundary) -> String {
        format!("live-order-boundary:{}", boundary.strategy_id().as_str())
    }

    /// SRS-EXE-001 / SyRS SYS-2a / SYS-2d / NFR-S2 — designate `strategy_id` as
    /// the single live strategy on the engine-owned authority. Requires an
    /// explicit, strategy-bound [`LiveDesignationConfirmation`] and rejects a
    /// second concurrent designation (the current live strategy must be
    /// [`demote`](Self::demote)d first). This is the only way to populate the
    /// authority [`route_order`](Self::route_order) consults; the canonical
    /// authority is owned by the engine, never supplied by a caller.
    pub fn designate(
        &mut self,
        strategy_id: atp_types::StrategyId,
        confirmation: LiveDesignationConfirmation,
    ) -> Result<(), LiveDesignationError> {
        self.designation.designate(strategy_id, confirmation)
    }

    /// SRS-EXE-001 / SyRS SYS-2a — demote `strategy_id` from live, clearing the
    /// engine-owned designation. Rejects if `strategy_id` is not the currently
    /// designated strategy.
    pub fn demote(
        &mut self,
        strategy_id: &atp_types::StrategyId,
    ) -> Result<(), LiveDesignationError> {
        self.designation.demote(strategy_id)
    }

    /// The currently designated live strategy on the engine-owned authority, if
    /// any.
    pub fn designated(&self) -> Option<&atp_types::StrategyId> {
        self.designation.designated()
    }

    /// ERR-1 + ERR-2 + ERR-3 / SRS-EXE-001 / SRS-ERR-001 / SRS-SAFE-003 /
    /// SRS-MD-004 / SRS-MD-005: route an order to the live broker only if
    /// (a) the submitting strategy is in `Live` mode AND (b) the IB
    /// Gateway is reachable AND (c) the subscribed market data for the
    /// order's symbol is fresh. Every rejection is synchronous, returns a
    /// `StructuredOrderError` matching the SyRS SYS-64 wire vocabulary,
    /// and produces zero IB order side effect.
    ///
    /// On `Unreachable` or `ScheduledRestartWindow`, the engine publishes
    /// a `ConnectivityEvent` and requests a reconnect — neither side
    /// effect runs the broker port. On `Stale`, the engine publishes a
    /// `StaleDataEvent` carrying the observed staleness in seconds — no
    /// reconnect is requested because staleness is a data-side condition,
    /// not a transport fault.
    ///
    /// **This is the inner ERR-1/2/3 mode/connectivity/freshness gate, not the
    /// single-live authority.** It trusts the caller-supplied `mode` and does not
    /// consult the live-designation authority; production callers reach the live
    /// broker through [`route_order`](Self::route_order), which resolves the
    /// engine-owned [`LiveDesignation`] first. Closing this method off entirely
    /// (so the broker is reachable *only* via `route_order`) re-architects the
    /// pinned ERR-1/2/3 contract and is the deferred SRS-EXE-006 / SRS-ORCH-*
    /// wiring (`live_designation_contract.deferred[]`).
    // Broker/clock/designation generic params are the pinned ERR-1/2/3 contract;
    // collapsing them is the deferred SRS-EXE-006 rework, not this CI-health pass.
    #[allow(clippy::too_many_arguments)]
    pub fn submit_live_order<B, C, E, F, S>(
        &self,
        mode: StrategyMode,
        submission: OrderSubmission,
        broker: &B,
        connectivity: &C,
        events: &E,
        freshness: &F,
        stale_events: &S,
    ) -> Result<OrderReceipt, StructuredOrderError>
    where
        B: LiveBrokerageSubmit,
        C: BrokerageConnectivity,
        E: ConnectivityEventSink,
        F: MarketDataFreshnessProbe,
        S: StaleDataEventSink,
    {
        match mode {
            StrategyMode::Live => match connectivity.state() {
                ConnectivityState::Connected => match freshness.freshness(&submission.symbol) {
                    // SRS-EXE-003 — VALIDATE the order on the live path immediately
                    // before the broker-port call, AFTER the ERR-2/3 connectivity +
                    // freshness reachability gates (those take precedence) but
                    // BEFORE `broker.submit_order`, so a malformed order (blank
                    // symbol / non-positive quantity / non-positive price) can
                    // never reach the live broker even when a caller enters via
                    // `submit_live_order` / `route_order` directly (the adapter
                    // validation is defense-in-depth, not the only guard). The SyRS
                    // taxonomy has no dedicated invalid-order-parameters category;
                    // InvalidSymbol is the order-rejection bucket and the precise
                    // reason is in error_type (a dedicated category is a
                    // cross-cutting SRS-ERR-001 taxonomy change, deferred).
                    MarketDataFreshness::Fresh => match submission.validate() {
                        Ok(()) => broker.submit_order(submission),
                        Err(err) => Err(StructuredOrderError {
                            category: OrderErrorCategory::InvalidSymbol,
                            error_type: err.error_type().to_string(),
                            message: err.to_string(),
                            original_order: submission,
                        }),
                    },
                    MarketDataFreshness::Stale => {
                        let staleness_seconds = freshness.staleness_seconds(&submission.symbol);
                        stale_events.record(StaleDataEvent {
                            state: MarketDataFreshness::Stale,
                            strategy_id: submission.strategy_id.clone(),
                            symbol: submission.symbol.clone(),
                            staleness_seconds,
                        });
                        Err(StructuredOrderError {
                            category: OrderErrorCategory::MarketDataStale,
                            error_type: "MarketDataStale".to_string(),
                            message: format!(
                                "live order submission for strategy `{}` blocked: \
                                 subscribed market data for `{}` is stale ({}s; \
                                 threshold 15s per NFR-P5; SRS-MD-004)",
                                submission.strategy_id.as_str(),
                                submission.symbol,
                                staleness_seconds,
                            ),
                            original_order: submission,
                        })
                    }
                },
                state @ (ConnectivityState::Unreachable
                | ConnectivityState::ScheduledRestartWindow) => {
                    events.record(ConnectivityEvent {
                        state,
                        strategy_id: submission.strategy_id.clone(),
                        symbol: submission.symbol.clone(),
                        scheduled_restart: matches!(
                            state,
                            ConnectivityState::ScheduledRestartWindow
                        ),
                    });
                    connectivity.request_reconnect();
                    Err(StructuredOrderError {
                        category: OrderErrorCategory::ConnectivityBlocked,
                        error_type: "IbGatewayUnreachable".to_string(),
                        message: format!(
                            "live order submission for strategy `{}` blocked: \
                             IB Gateway is unreachable (SRS-SAFE-003)",
                            submission.strategy_id.as_str()
                        ),
                        original_order: submission,
                    })
                }
            },
            StrategyMode::Paper => Err(StructuredOrderError {
                category: OrderErrorCategory::NonLiveStrategySubmission,
                error_type: "NonLiveLiveRouteBlocked".to_string(),
                message: format!(
                    "strategy `{}` is not the designated live strategy; \
                     live IB execution path is reserved for the single \
                     live strategy (SRS-EXE-001)",
                    submission.strategy_id.as_str()
                ),
                original_order: submission,
            }),
        }
    }

    /// SRS-EXE-001 / SyRS SYS-1 / SYS-2a / SYS-2d / AC-15 — the live-routing
    /// **authority** gate, and the designated entry point for routing a
    /// strategy's order to the live broker. Unlike [`submit_live_order`], which
    /// trusts a caller-passed [`StrategyMode`], `route_order` derives live-ness
    /// from the **engine-owned** [`LiveDesignation`] authority (`self.designation`,
    /// populated only through [`designate`](Self::designate)), so that **only the
    /// single designated live strategy** can ever reach IB (AGENTS.md core
    /// invariant). It takes **no** caller-supplied authority — a strategy cannot
    /// hand in a `LiveDesignation` that designates itself.
    ///
    /// A submission from any strategy that is **not** the designated live
    /// strategy ([`LiveRoutingDecision::NotDesignated`]) is rejected
    /// synchronously with a `NON_LIVE_STRATEGY_SUBMISSION` structured error
    /// (ERR-1 / SRS-ERR-001) **before any broker, connectivity, or freshness
    /// port is consulted** — the rejection is independent of connectivity and
    /// freshness state and produces zero IB order side effect.
    ///
    /// On [`LiveRoutingDecision::Authorized`], the order proceeds to the inner
    /// ERR-1/2/3 live gate ([`submit_live_order`] with [`StrategyMode::Live`]
    /// derived here, never trusted from the caller), which applies the
    /// connectivity (SRS-SAFE-003 / SRS-MD-005) and market-data freshness
    /// (SRS-MD-004 / NFR-P5) safeguards.
    ///
    /// **Scope.** `route_order` is the authority gate; `submit_live_order`
    /// remains the lower-level connectivity/freshness/mode primitive it
    /// delegates to, and is kept `pub` because the ERR-1/2/3 contract
    /// (`error_handling_check` + the err_1/2/3 integration tests) pins it as the
    /// synchronous-rejection entry point. Making `submit_live_order`
    /// *unreachable* except through `route_order` (crate-private + an admission
    /// token) re-architects that pinned ERR-1/2/3 contract and is the deferred
    /// orchestrator/adapter wiring — owner SRS-EXE-006 / SRS-ORCH-* — enumerated
    /// in `architecture/runtime_services.json`
    /// `live_designation_contract.deferred[]`. SRS-EXE-001 stays `passes:false`
    /// until that runtime and the NFR-P1 latency proof land.
    ///
    /// [`submit_live_order`]: ExecutionEngine::submit_live_order
    pub fn route_order<B, C, E, F, S>(
        &self,
        submission: OrderSubmission,
        broker: &B,
        connectivity: &C,
        events: &E,
        freshness: &F,
        stale_events: &S,
    ) -> Result<OrderReceipt, StructuredOrderError>
    where
        B: LiveBrokerageSubmit,
        C: BrokerageConnectivity,
        E: ConnectivityEventSink,
        F: MarketDataFreshnessProbe,
        S: StaleDataEventSink,
    {
        match self.designation.authority_for(&submission.strategy_id) {
            LiveRoutingDecision::NotDesignated => Err(StructuredOrderError {
                category: OrderErrorCategory::NonLiveStrategySubmission,
                error_type: "NotDesignatedLiveStrategy".to_string(),
                message: format!(
                    "strategy `{}` is not the designated live strategy; orders \
                     route to IB only for the single designated live strategy \
                     (SRS-EXE-001, SyRS SYS-2a/SYS-2d)",
                    submission.strategy_id.as_str()
                ),
                original_order: submission,
            }),
            LiveRoutingDecision::Authorized => self.submit_live_order(
                StrategyMode::Live,
                submission,
                broker,
                connectivity,
                events,
                freshness,
                stale_events,
            ),
        }
    }

    /// ERR-8 / SRS-SAFE-002 / SyRS SYS-44b — decide a single kill-switch
    /// liquidation-timeout outcome. The probe (`KillSwitchLiquidationProbe`)
    /// is the timing authority: on `FilledBeforeTimeout` the liquidation
    /// reached fill in time and the SYS-44b error path does NOT engage (the
    /// gate records the audit transition and returns `Ok`, no alert / no
    /// cancel / no disconnect). On `TimedOutUnfilled` the gate runs the
    /// SYS-44b sequence — notify the operator by email AND SMS, cancel the
    /// unfilled liquidation order, disconnect from IB — records each
    /// side-effect outcome plus the logged unfilled-order details on the audit
    /// event, and refuses with `OrderErrorCategory::KillSwitchLiquidationTimeout`
    /// (the operator then resolves remaining positions manually).
    ///
    /// **Unconfirmable fill (probe error):** the probe is fallible — if it
    /// cannot confirm whether the liquidation filled (connectivity loss, order
    /// state unavailable, probe timeout) the gate fails closed by refusing with
    /// the DISTINCT `KillSwitchLiquidationProbeUnavailable` category and takes
    /// NO automated order/session action (auto-canceling or auto-disconnecting
    /// on an unconfirmable order state would be a premature destructive
    /// action). Notification and order resolution on that state are the
    /// caller's / deferred runtime's responsibility.
    ///
    /// **Fail closed:** the probe is the timing authority, but a
    /// `FilledBeforeTimeout` whose `elapsed_seconds` exceeds
    /// `request.timeout_seconds` is a probe inconsistency (buggy /
    /// version-skewed) and is normalised to a timeout BEFORE the match so a
    /// mislabelled over-deadline liquidation cannot skip the cancel +
    /// disconnect. Failing closed toward *running the SYS-44b cleanup* is the
    /// safe direction. The inverse inconsistency (a `TimedOutUnfilled`
    /// reported before the deadline, or with a mismatched `timeout_seconds`)
    /// is NOT normalised here — handling it correctly means a distinct
    /// probe-inconsistency rejection that does not fire the premature
    /// destructive cancel/disconnect, which is the deferred kill-switch
    /// runtime's richer semantics. The probe's outcome-consistency (filled ⟹
    /// within deadline; timeout ⟹ at/after the deadline with a matching
    /// `timeout_seconds`) is its contract precondition — see
    /// `kill_switch_timeout_contract.deferred[]`. The shipped slice has no
    /// concrete probe, so none can violate it.
    ///
    /// All three timeout-branch side effects are surfaced rather than
    /// swallowed: the alert / cancel / disconnect ports return `Result` and
    /// their outcomes are recorded on the event (`SideEffectOutcome::Failed`
    /// preserves the reason). Event emission is best-effort — a sink failure
    /// does not roll back the side effects or change the refusal.
    ///
    /// **Scope — stateless single-attempt gate.** This decides ONE timeout
    /// outcome. The full SRS-SAFE-001 kill-switch sequence (cancel all resting
    /// orders, submit liquidation orders, halt paper engines, always
    /// disconnect), the 30 s async wait loop, the real IB cancel/disconnect
    /// (SRS-EXE-006), the real email/SMS transport (SRS-NOTIF-001), and any
    /// durable post-timeout lockout are the deferred runtime, enumerated in
    /// `architecture/runtime_services.json` `kill_switch_timeout_contract
    /// .deferred[]`. ERR-8 stays `passes:false` until that runtime lands.
    // The error is boxed (`Box<StructuredKillSwitchTimeoutError>`): the SYS-44b
    // envelope carries the full unfilled-order details (order id, symbol, side,
    // quantity) the operator needs to resolve positions manually, which makes
    // it exceed clippy's `result_large_err` threshold. ERR-7's smaller demotion
    // error stayed under the threshold and is returned unboxed; boxing here
    // keeps `atp-execution` clippy-clean without dropping `original_request`.
    pub fn resolve_kill_switch_timeout<P, A, C, E>(
        &self,
        request: KillSwitchTimeoutRequest,
        liquidation: &P,
        alerts: &A,
        cleanup: &C,
        events: &E,
        observed_at_seconds: u64,
    ) -> Result<KillSwitchLiquidationResolved, Box<StructuredKillSwitchTimeoutError>>
    where
        P: KillSwitchLiquidationProbe,
        A: KillSwitchOperatorAlertSink,
        C: IbLiquidationCleanup,
        E: KillSwitchTimeoutEventSink,
    {
        let reported = match liquidation.await_filled_or_timeout(&request) {
            Ok(outcome) => outcome,
            Err(probe_error) => {
                // SRS-SAFE-002 fail-closed on an UNCONFIRMABLE fill: the probe
                // could not determine whether the liquidation filled
                // (connectivity loss, order state unavailable, probe timeout).
                // The gate must NOT pretend it filled, and must NOT fire the
                // destructive SYS-44b cleanup (cancel / disconnect) on an order
                // state it cannot confirm. It therefore takes NO automated
                // order/session action and refuses with a distinct, typed
                // probe-unavailable error (its own OrderErrorCategory, never
                // mislabelled as a confirmed timeout). Notifying the operator
                // and resolving the unknown order state are the caller's /
                // deferred kill-switch runtime's responsibility (contract
                // `deferred[]`).
                return Err(Box::new(
                    StructuredKillSwitchTimeoutError::probe_unavailable(
                        request,
                        probe_error.reason,
                    ),
                ));
            }
        };
        // Defense-in-depth fail-closed: normalise a FilledBeforeTimeout whose
        // elapsed exceeds the configured timeout into a timeout BEFORE the
        // match so a mislabelled over-deadline liquidation cannot skip the
        // SYS-44b cancel + disconnect.
        let outcome = match reported {
            KillSwitchLiquidationOutcome::FilledBeforeTimeout { elapsed_seconds }
                if elapsed_seconds > request.timeout_seconds =>
            {
                KillSwitchLiquidationOutcome::TimedOutUnfilled {
                    elapsed_seconds,
                    timeout_seconds: request.timeout_seconds,
                }
            }
            other => other,
        };
        match outcome {
            KillSwitchLiquidationOutcome::FilledBeforeTimeout { elapsed_seconds } => {
                // SRS-SAFE-002: liquidation filled in time — the SYS-44b error
                // path does not engage. Record the audit transition (no manual
                // resolution required) and return acceptance; no alert, no
                // cancel, no disconnect (all NotAttempted). Event emission is
                // best-effort.
                let _ = events.record(KillSwitchTimeoutEvent {
                    outcome,
                    live_strategy_id: request.live_strategy_id.clone(),
                    unfilled_order: request.unfilled_order.clone(),
                    manual_resolution_required: false,
                    operator_alert: SideEffectOutcome::NotAttempted,
                    liquidation_cancel: SideEffectOutcome::NotAttempted,
                    ib_disconnect: SideEffectOutcome::NotAttempted,
                    observed_at_seconds,
                });
                Ok(KillSwitchLiquidationResolved {
                    live_strategy_id: request.live_strategy_id,
                    filled_before_timeout: true,
                    elapsed_seconds,
                })
            }
            KillSwitchLiquidationOutcome::TimedOutUnfilled {
                elapsed_seconds,
                timeout_seconds,
            } => {
                // SRS-SAFE-002 / SyRS SYS-44b timeout branch: notify the
                // operator by email AND SMS, cancel the unfilled liquidation
                // order, and disconnect from IB. ALL THREE are attempted
                // unconditionally (a failed cancel must not suppress the page
                // or the disconnect) and each outcome is recorded on the event
                // so a missed page / failed cancel / failed disconnect is
                // observable rather than indistinguishable from success.
                let operator_alert = into_outcome(alerts.dispatch(KillSwitchAlertEvent {
                    live_strategy_id: request.live_strategy_id.clone(),
                    unfilled_order: request.unfilled_order.clone(),
                    channels: vec![OperatorAlertChannel::Email, OperatorAlertChannel::Sms],
                    elapsed_seconds,
                    timeout_seconds,
                    observed_at_seconds,
                }));
                let liquidation_cancel =
                    into_outcome(cleanup.cancel_unfilled_liquidation_order(&request));
                let ib_disconnect = into_outcome(cleanup.disconnect());
                // Best-effort audit emission: the logged unfilled-order details
                // + each side-effect outcome. A sink failure does not roll back
                // the side effects above or change the refusal below — but it
                // would lose the durable record, so `audit_recorded` captures
                // whether it persisted and the per-side-effect outcomes are ALSO
                // carried on the returned error (`cleanup`) so the
                // recovery-critical facts survive a failed audit emission.
                let timeout_event = KillSwitchTimeoutEvent {
                    outcome,
                    live_strategy_id: request.live_strategy_id.clone(),
                    unfilled_order: request.unfilled_order.clone(),
                    manual_resolution_required: true,
                    operator_alert: operator_alert.clone(),
                    liquidation_cancel: liquidation_cancel.clone(),
                    ib_disconnect: ib_disconnect.clone(),
                    observed_at_seconds,
                };
                let audit_recorded = events.record(timeout_event).is_ok();
                Err(Box::new(
                    StructuredKillSwitchTimeoutError::liquidation_timeout(
                        request,
                        elapsed_seconds,
                        timeout_seconds,
                        KillSwitchCleanupOutcome {
                            operator_alert,
                            liquidation_cancel,
                            ib_disconnect,
                            audit_recorded,
                        },
                    ),
                ))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_data::DataLayer;
    use atp_types::StrategyId;
    use std::cell::{Cell, RefCell};

    struct CountingBroker {
        calls: Cell<u32>,
    }

    impl CountingBroker {
        fn new() -> Self {
            Self {
                calls: Cell::new(0),
            }
        }
    }

    impl LiveBrokerageSubmit for CountingBroker {
        fn submit_order(
            &self,
            submission: OrderSubmission,
        ) -> Result<OrderReceipt, StructuredOrderError> {
            self.calls.set(self.calls.get() + 1);
            Ok(OrderReceipt {
                broker_order_id: format!("ib-{}", submission.symbol),
            })
        }
    }

    struct StubConnectivity {
        state: Cell<ConnectivityState>,
        reconnect_calls: Cell<u32>,
    }

    impl StubConnectivity {
        fn connected() -> Self {
            Self {
                state: Cell::new(ConnectivityState::Connected),
                reconnect_calls: Cell::new(0),
            }
        }

        fn in_state(state: ConnectivityState) -> Self {
            Self {
                state: Cell::new(state),
                reconnect_calls: Cell::new(0),
            }
        }
    }

    impl BrokerageConnectivity for StubConnectivity {
        fn state(&self) -> ConnectivityState {
            self.state.get()
        }

        fn request_reconnect(&self) {
            self.reconnect_calls.set(self.reconnect_calls.get() + 1);
        }
    }

    /// A connectivity stub that panics if the engine consults it. Used to
    /// prove the Paper-mode arm of ERR-1 does not reach the connectivity
    /// gate at all.
    struct ForbiddenConnectivity;

    impl BrokerageConnectivity for ForbiddenConnectivity {
        fn state(&self) -> ConnectivityState {
            panic!("Paper submissions must not consult the connectivity port");
        }

        fn request_reconnect(&self) {
            panic!("Paper submissions must not request a reconnect");
        }
    }

    struct RecordingEvents {
        events: RefCell<Vec<ConnectivityEvent>>,
    }

    impl RecordingEvents {
        fn new() -> Self {
            Self {
                events: RefCell::new(Vec::new()),
            }
        }
    }

    impl ConnectivityEventSink for RecordingEvents {
        fn record(&self, event: ConnectivityEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    /// A freshness probe that always reports `Fresh` (and panics if asked
    /// for an age — staleness_seconds should never be consulted unless the
    /// state was `Stale`). Used by Live-mode happy-path tests.
    struct AlwaysFresh;

    impl MarketDataFreshnessProbe for AlwaysFresh {
        fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
            MarketDataFreshness::Fresh
        }

        fn staleness_seconds(&self, _symbol: &str) -> u64 {
            panic!("staleness_seconds must not be consulted when freshness is Fresh");
        }
    }

    /// A freshness probe that panics on every call. Used to prove that the
    /// Paper and Unreachable branches never consult the freshness gate.
    struct ForbiddenFreshness;

    impl MarketDataFreshnessProbe for ForbiddenFreshness {
        fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
            panic!("freshness must not be consulted on this branch");
        }

        fn staleness_seconds(&self, _symbol: &str) -> u64 {
            panic!("staleness_seconds must not be consulted on this branch");
        }
    }

    struct RecordingStaleEvents {
        events: RefCell<Vec<StaleDataEvent>>,
    }

    impl RecordingStaleEvents {
        fn new() -> Self {
            Self {
                events: RefCell::new(Vec::new()),
            }
        }
    }

    impl StaleDataEventSink for RecordingStaleEvents {
        fn record(&self, event: StaleDataEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    #[test]
    fn is_a_rust_execution_service_boundary() {
        let boundary = StrategyRuntimeBoundary::new(StrategyId::new("live-1"), DataLayer);
        let engine = ExecutionEngine::default();
        assert_eq!(engine.service(), RuntimeService::ExecutionEngine);
        assert_eq!(
            engine.accepts_live_boundary(&boundary),
            "live-order-boundary:live-1"
        );
    }

    #[test]
    fn live_strategy_submission_is_routed_to_the_broker() {
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::connected();
        let events = RecordingEvents::new();
        let freshness = AlwaysFresh;
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 10,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let receipt = engine
            .submit_live_order(
                StrategyMode::Live,
                submission,
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect("live mode + connected + fresh must route through the brokerage port");

        assert_eq!(receipt.broker_order_id, "ib-AAPL");
        assert_eq!(broker.calls.get(), 1);
        assert_eq!(connectivity.reconnect_calls.get(), 0);
        assert!(events.events.borrow().is_empty());
        assert!(stale_events.events.borrow().is_empty());
    }

    #[test]
    fn malformed_live_order_fails_closed_before_the_broker_port() {
        // SRS-EXE-003 — a malformed order (non-positive quantity here) submitted
        // straight down the live path (submit_live_order, the public ERR-1/2/3
        // entry) is validated BEFORE the broker port: it fails closed and the
        // broker is never called, even though connectivity is fresh + connected.
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::connected();
        let events = RecordingEvents::new();
        let freshness = AlwaysFresh;
        let stale_events = RecordingStaleEvents::new();
        let malformed = OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: "AAPL".to_string(),
            quantity: 0,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let err = engine
            .submit_live_order(
                StrategyMode::Live,
                malformed,
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("a malformed order must fail closed before the broker port");
        assert_eq!(err.error_type, "NonPositiveQuantity");
        assert_eq!(
            broker.calls.get(),
            0,
            "a malformed order must never reach the broker port"
        );
    }

    #[test]
    fn paper_strategy_submission_is_rejected_synchronously_with_no_broker_call() {
        // ERR-1: A non-live strategy submitting an order down the live
        // execution path must be rejected synchronously with a structured
        // error AND must produce no IB order side effect. The connectivity
        // port must NOT be consulted — Paper rejection is independent of
        // connectivity state.
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = ForbiddenConnectivity;
        let events = RecordingEvents::new();
        let freshness = ForbiddenFreshness;
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-research-3"),
            symbol: "TSLA".to_string(),
            quantity: 5,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Paper,
                submission.clone(),
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("paper mode must be rejected on the live path");

        assert_eq!(
            error.category,
            OrderErrorCategory::NonLiveStrategySubmission
        );
        assert_eq!(error.error_type, "NonLiveLiveRouteBlocked");
        assert!(error.message.contains("paper-research-3"));
        assert!(error.message.contains("SRS-EXE-001"));
        assert_eq!(error.original_order, submission);
        assert_eq!(
            broker.calls.get(),
            0,
            "the broker port must not be invoked when mode is Paper"
        );
        assert!(events.events.borrow().is_empty());
        assert!(stale_events.events.borrow().is_empty());
    }

    #[test]
    fn structured_error_display_includes_category_wire_string() {
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = ForbiddenConnectivity;
        let events = RecordingEvents::new();
        let freshness = ForbiddenFreshness;
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("paper-x"),
            symbol: "MSFT".to_string(),
            quantity: 1,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };
        let error = engine
            .submit_live_order(
                StrategyMode::Paper,
                submission,
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .unwrap_err();
        assert!(format!("{error}").contains("NON_LIVE_STRATEGY_SUBMISSION"));
    }

    #[test]
    fn live_submission_is_blocked_when_gateway_unreachable() {
        // ERR-2 / SRS-SAFE-003: When IB Gateway is unreachable, a live
        // submission must be rejected with CONNECTIVITY_BLOCKED, no broker
        // call must happen, the connectivity port must be asked to
        // reconnect, exactly one ConnectivityEvent must be recorded, and
        // the freshness port must NOT be consulted (Unreachable short-
        // circuits the inner freshness match).
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::in_state(ConnectivityState::Unreachable);
        let events = RecordingEvents::new();
        let freshness = ForbiddenFreshness;
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            quantity: 10,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Live,
                submission.clone(),
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("Unreachable connectivity must block the live submission");

        assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
        assert_eq!(error.error_type, "IbGatewayUnreachable");
        assert!(error.message.contains("live-alpha"));
        assert!(error.message.contains("SRS-SAFE-003"));
        assert_eq!(error.original_order, submission);
        assert!(format!("{error}").contains("CONNECTIVITY_BLOCKED"));

        assert_eq!(broker.calls.get(), 0, "broker must not be invoked");
        assert_eq!(
            connectivity.reconnect_calls.get(),
            1,
            "the engine must request a reconnect on Unreachable"
        );
        let recorded = events.events.borrow();
        assert_eq!(recorded.len(), 1, "exactly one ConnectivityEvent expected");
        assert_eq!(recorded[0].state, ConnectivityState::Unreachable);
        assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha");
        assert_eq!(recorded[0].symbol, "AAPL");
        assert!(!recorded[0].scheduled_restart);
        assert!(stale_events.events.borrow().is_empty());
    }

    #[test]
    fn live_submission_is_blocked_during_scheduled_restart_window() {
        // ERR-2 / SRS-MD-005: During the configured daily restart window,
        // submissions are suspended; the published event carries
        // scheduled_restart=true so the notification dispatcher can apply
        // the suppression rule. The freshness port must NOT be consulted.
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::in_state(ConnectivityState::ScheduledRestartWindow);
        let events = RecordingEvents::new();
        let freshness = ForbiddenFreshness;
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            quantity: 10,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Live,
                submission,
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("ScheduledRestartWindow must block the live submission");

        assert_eq!(error.category, OrderErrorCategory::ConnectivityBlocked);
        assert_eq!(broker.calls.get(), 0);
        assert_eq!(connectivity.reconnect_calls.get(), 1);
        let recorded = events.events.borrow();
        assert_eq!(recorded.len(), 1);
        assert_eq!(recorded[0].state, ConnectivityState::ScheduledRestartWindow);
        assert!(
            recorded[0].scheduled_restart,
            "SRS-MD-005 suppression flag must be set"
        );
        assert!(stale_events.events.borrow().is_empty());
    }

    /// A freshness probe parameterized over `(state, staleness_seconds)`.
    /// Counts every call so tests can assert the gate is consulted
    /// exactly the expected number of times.
    struct StubFreshness {
        state: MarketDataFreshness,
        staleness_seconds: u64,
        freshness_calls: Cell<u32>,
    }

    impl StubFreshness {
        fn stale(seconds: u64) -> Self {
            Self {
                state: MarketDataFreshness::Stale,
                staleness_seconds: seconds,
                freshness_calls: Cell::new(0),
            }
        }
    }

    impl MarketDataFreshnessProbe for StubFreshness {
        fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
            self.freshness_calls.set(self.freshness_calls.get() + 1);
            self.state
        }

        fn staleness_seconds(&self, _symbol: &str) -> u64 {
            self.staleness_seconds
        }
    }

    #[test]
    fn live_submission_is_blocked_when_market_data_is_stale() {
        // ERR-3 / SRS-MD-004 / NFR-P5: When subscribed market data is
        // stale, a live submission must be rejected with
        // MARKET_DATA_STALE, no broker call must happen, no reconnect
        // request must be issued (staleness is a data-side condition,
        // not a transport fault), and exactly one StaleDataEvent must
        // carry the observed staleness in seconds.
        let engine = ExecutionEngine::default();
        let broker = CountingBroker::new();
        let connectivity = StubConnectivity::connected();
        let events = RecordingEvents::new();
        let freshness = StubFreshness::stale(22);
        let stale_events = RecordingStaleEvents::new();
        let submission = OrderSubmission {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            quantity: 10,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        };

        let error = engine
            .submit_live_order(
                StrategyMode::Live,
                submission.clone(),
                &broker,
                &connectivity,
                &events,
                &freshness,
                &stale_events,
            )
            .expect_err("Stale market data must block the live submission");

        assert_eq!(error.category, OrderErrorCategory::MarketDataStale);
        assert_eq!(error.error_type, "MarketDataStale");
        assert!(error.message.contains("live-alpha"));
        assert!(error.message.contains("AAPL"));
        assert!(error.message.contains("SRS-MD-004"));
        assert!(error.message.contains("NFR-P5"));
        assert_eq!(error.original_order, submission);
        assert!(format!("{error}").contains("MARKET_DATA_STALE"));

        assert_eq!(broker.calls.get(), 0, "broker must not be invoked");
        assert_eq!(
            connectivity.reconnect_calls.get(),
            0,
            "staleness must not trigger a reconnect — that is reserved for transport faults"
        );
        assert!(
            events.events.borrow().is_empty(),
            "no ConnectivityEvent should be published for a data-side rejection"
        );
        let recorded = stale_events.events.borrow();
        assert_eq!(recorded.len(), 1, "exactly one StaleDataEvent expected");
        assert_eq!(recorded[0].state, MarketDataFreshness::Stale);
        assert_eq!(recorded[0].strategy_id.as_str(), "live-alpha");
        assert_eq!(recorded[0].symbol, "AAPL");
        assert_eq!(recorded[0].staleness_seconds, 22);
        assert_eq!(freshness.freshness_calls.get(), 1);
    }
}
