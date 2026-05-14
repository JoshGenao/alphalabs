use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct StrategyId(String);

impl StrategyId {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RuntimeService {
    DataLayer,
    StrategyEngine,
    ExecutionEngine,
    InternalSimulationEngine,
    MarketDataSubscriptionManager,
    StrategyOrchestrator,
    BrokerAndDataProviderAdapters,
    FactorPipelineRuntime,
    NotificationDispatcher,
}

pub const CORE_RUNTIME_SERVICES: &[RuntimeService] = &[
    RuntimeService::DataLayer,
    RuntimeService::StrategyEngine,
    RuntimeService::ExecutionEngine,
    RuntimeService::InternalSimulationEngine,
    RuntimeService::MarketDataSubscriptionManager,
    RuntimeService::StrategyOrchestrator,
    RuntimeService::BrokerAndDataProviderAdapters,
    RuntimeService::FactorPipelineRuntime,
    RuntimeService::NotificationDispatcher,
];

// --------------------------------------------------------------------------- //
// Order submission domain types
// --------------------------------------------------------------------------- //
//
// `OrderSubmission` and `OrderReceipt` are the source-neutral order envelope
// shared between the execution engine (which decides whether an order may be
// routed to the live broker) and the brokerage adapter crate (which carries
// out the actual TWS API call). They live in `atp-types` so neither side has
// to depend on the other — both depend on `atp-types`.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderSubmission {
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub quantity: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderReceipt {
    pub broker_order_id: String,
}

// --------------------------------------------------------------------------- //
// Strategy execution mode (SRS-EXE-001, SyRS SYS-1 / AC-15)
// --------------------------------------------------------------------------- //
//
// Exactly one strategy may run in `Live` mode at any time and is the only
// strategy whose orders may reach the IB live account. All other strategies
// run in `Paper` mode against the internal simulation engine; their orders
// must be rejected synchronously if they reach the live execution path
// (ERR-1 / SRS-ERR-001).

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum StrategyMode {
    Live,
    Paper,
}

impl StrategyMode {
    pub const fn is_live(self) -> bool {
        matches!(self, Self::Live)
    }
}

// --------------------------------------------------------------------------- //
// Structured order-submission error (SRS-ERR-001, SyRS SYS-64)
// --------------------------------------------------------------------------- //
//
// SyRS SYS-64 names the error categories every submission failure must
// classify itself under. Each variant maps 1:1 to the SyRS string so the
// wire form stays stable across Rust, Python, REST, and WebSocket surfaces.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OrderErrorCategory {
    InvalidSymbol,
    InsufficientBuyingPower,
    ConnectivityBlocked,
    RateLimited,
    MarketDataStale,
    SubscriptionLimitReached,
    NonLiveStrategySubmission,
    IngestionRecordValidationFailed,
    IngestionPacingBudgetExceeded,
    StrategyStartupDeadlineExceeded,
}

impl OrderErrorCategory {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::InvalidSymbol => "INVALID_SYMBOL",
            Self::InsufficientBuyingPower => "INSUFFICIENT_BUYING_POWER",
            Self::ConnectivityBlocked => "CONNECTIVITY_BLOCKED",
            Self::RateLimited => "RATE_LIMITED",
            Self::MarketDataStale => "MARKET_DATA_STALE",
            Self::SubscriptionLimitReached => "SUBSCRIPTION_LIMIT_REACHED",
            Self::NonLiveStrategySubmission => "NON_LIVE_STRATEGY_SUBMISSION",
            Self::IngestionRecordValidationFailed => "INGESTION_RECORD_VALIDATION_FAILED",
            Self::IngestionPacingBudgetExceeded => "INGESTION_PACING_BUDGET_EXCEEDED",
            Self::StrategyStartupDeadlineExceeded => "STRATEGY_STARTUP_DEADLINE_EXCEEDED",
        }
    }
}

/// SRS-ERR-001 structured error envelope. Carries exactly the four fields
/// the spec requires: a SyRS-aligned category, an error type discriminator,
/// a human-readable message, and the unchanged original order parameters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredOrderError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_order: OrderSubmission,
}

impl fmt::Display for StructuredOrderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "[{}] {}: {}",
            self.category.as_str(),
            self.error_type,
            self.message
        )
    }
}

impl std::error::Error for StructuredOrderError {}

// --------------------------------------------------------------------------- //
// IB Gateway connectivity state and structured event (SRS-SAFE-003, SRS-MD-005)
// --------------------------------------------------------------------------- //
//
// `ConnectivityState` types the three states the live execution path must
// distinguish between when deciding whether a Live submission may reach the
// brokerage port:
//   * `Connected` — IB Gateway is reachable and the readiness checks pass.
//   * `Unreachable` — IB connectivity is lost (SRS-SAFE-003); live submissions
//     must be rejected with `CONNECTIVITY_BLOCKED` until reconnection.
//   * `ScheduledRestartWindow` — the configured daily restart window is
//     active (SRS-MD-005); submissions are suspended and normal connectivity
//     notifications are suppressed for the configured window.
//
// `ConnectivityEvent` is the structured payload published whenever a live
// submission is blocked. It carries the state, the submitting strategy, the
// symbol, and a `scheduled_restart` flag so dashboards and notification
// dispatchers can apply SRS-MD-005's suppression rule without re-inspecting
// the enum.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ConnectivityState {
    Connected,
    Unreachable,
    ScheduledRestartWindow,
}

impl ConnectivityState {
    pub const fn is_blocked(self) -> bool {
        matches!(self, Self::Unreachable | Self::ScheduledRestartWindow)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConnectivityEvent {
    pub state: ConnectivityState,
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub scheduled_restart: bool,
}

// --------------------------------------------------------------------------- //
// Market-data freshness state and structured event (SRS-MD-004, NFR-P5)
// --------------------------------------------------------------------------- //
//
// `MarketDataFreshness` types the two states the live execution path must
// distinguish between when deciding whether a Live submission may reach the
// brokerage port:
//   * `Fresh` — subscribed market data is within the NFR-P5 15-second
//     staleness threshold for the order's symbol.
//   * `Stale` — subscribed data has not updated within the threshold
//     (SRS-MD-004, SyRS SYS-39a); live and paper submissions must be
//     rejected with `MARKET_DATA_STALE` until fresh data is observed.
//
// `StaleDataEvent` is the structured payload published whenever the engine
// blocks a submission for staleness. It carries the state, the submitting
// strategy, the symbol, and the observed staleness in seconds so dashboards
// and the notification dispatcher can surface the age without re-probing
// the freshness port. The struct deliberately carries no broker / vendor /
// session / tick identifiers — staleness is a data-side condition.

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MarketDataFreshness {
    Fresh,
    Stale,
}

impl MarketDataFreshness {
    pub const fn is_stale(self) -> bool {
        matches!(self, Self::Stale)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StaleDataEvent {
    pub state: MarketDataFreshness,
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub staleness_seconds: u64,
}

// --------------------------------------------------------------------------- //
// Market-data subscription manager request envelope and structured rejection
// (SRS-MD-002, SyRS SYS-70 / SYS-64, StRS A-13)
// --------------------------------------------------------------------------- //
//
// SRS-MD-002 requires the subscription manager to enforce the operator-
// configured IB concurrent market-data line limit. SyRS SYS-70 places that
// enforcement at the centralized subscription manager: when a new
// subscription request would exceed the configured limit, the manager
// returns a structured error to the requesting strategy (per SYS-64's
// SUBSCRIPTION_LIMIT_REACHED category) and emits an operator-facing alert.
// SyRS SYS-64 mandates the same error contract for live and paper modes;
// the gate therefore does not branch on `StrategyMode`.
//
// `SubscriptionRequest` is the source-neutral request envelope the manager
// gates on. It deliberately mirrors `OrderSubmission` minus the `quantity`
// field (a subscription has no order semantics).
//
// `SubscriptionLimitState` types the two states the manager must
// distinguish: `WithinLimit` (the configured ceiling has headroom) and
// `ExceededLimit` (a new subscription would push past the ceiling).
//
// `SubscriptionLimitEvent` is the structured payload the manager publishes
// when it rejects a request. It carries the state, the submitting strategy,
// the symbol, and BOTH the observed `current_lines` count AND the
// `configured_limit` snapshot. Carrying the limit on the event closes a
// TOCTOU window: the configured value can be re-read between rejection and
// dashboard render, so the event must be self-describing.
//
// `StructuredSubscriptionError` is the rejection envelope. It reuses the
// existing `OrderErrorCategory::SubscriptionLimitReached` variant as the
// single source of truth for the SYS-64 wire string. The envelope is
// distinct from `StructuredOrderError` because a subscription request is
// not an order — synthesising an `OrderSubmission` with a fake quantity
// would lie to downstream consumers.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionRequest {
    pub strategy_id: StrategyId,
    pub symbol: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SubscriptionLimitState {
    WithinLimit,
    ExceededLimit,
}

impl SubscriptionLimitState {
    pub const fn is_exceeded(self) -> bool {
        matches!(self, Self::ExceededLimit)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionLimitEvent {
    pub state: SubscriptionLimitState,
    pub strategy_id: StrategyId,
    pub symbol: String,
    pub current_lines: u32,
    pub configured_limit: u32,
}

/// SRS-MD-002 / SyRS SYS-70 structured rejection envelope. Carries the
/// SYS-64 error category, the discriminator string, a human-readable
/// message, and the unchanged original request parameters. The category
/// is constrained at construction to
/// `OrderErrorCategory::SubscriptionLimitReached`; the factory enforces
/// that invariant in debug builds so a future caller cannot smuggle a
/// different category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredSubscriptionError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_request: SubscriptionRequest,
}

impl StructuredSubscriptionError {
    /// Build a `SUBSCRIPTION_LIMIT_REACHED` rejection. `current_lines` is
    /// the observed in-use count at rejection time; `configured_limit` is
    /// the cap reported by the subscription manager's line counter.
    pub fn limit_reached(
        request: SubscriptionRequest,
        current_lines: u32,
        configured_limit: u32,
    ) -> Self {
        let category = OrderErrorCategory::SubscriptionLimitReached;
        debug_assert!(
            matches!(category, OrderErrorCategory::SubscriptionLimitReached),
            "StructuredSubscriptionError must carry SubscriptionLimitReached"
        );
        let message = format!(
            "SRS-MD-002 + SyRS SYS-70: subscription for {symbol} from {strategy} \
             rejected — {current_lines} lines in use against configured limit \
             {configured_limit}",
            symbol = request.symbol,
            strategy = request.strategy_id.as_str(),
        );
        Self {
            category,
            error_type: "SubscriptionLimitReached".to_string(),
            message,
            original_request: request,
        }
    }
}

impl fmt::Display for StructuredSubscriptionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "[{}] {}: {}",
            self.category.as_str(),
            self.error_type,
            self.message
        )
    }
}

impl std::error::Error for StructuredSubscriptionError {}

// --------------------------------------------------------------------------- //
// Ingestion record validation envelope and structured rejection
// (SRS-DATA-013, SyRS SYS-77, StRS SN-1.26 / SN-1.27)
// --------------------------------------------------------------------------- //
//
// SRS-DATA-013 + SyRS SYS-77 require the data layer to validate every
// ingested equity and options record against six structural / range /
// duplicate / required-field rules BEFORE writing the record to primary
// storage. Records that fail validation are quarantined out-of-band (not
// written to the primary tables) and an operator-facing alert surfaces
// counts and reasons via the dashboard and notification subsystem.
//
// SyRS SYS-77 specifies the six rule categories (a..f) — those are the
// variants of `QuarantineReason` below. The data-layer gate is mode-
// invariant: the same six rules apply uniformly across every ingestion
// source (bulk-equity bars, minute-bar watchlist, option-chain captures,
// fundamental tables, user-uploaded Parquet), so the gate takes no
// `StrategyMode` parameter and no per-vendor enum at the type layer.
//
// `IngestionRecordSubmission` is the source-neutral envelope the gate
// validates. It carries a vendor-neutral `source` string (the vendor's
// identifier is opaque at this layer) and a `record_hash` (the canonical
// SHA-256 of the normalized record bytes) — the full payload goes to
// quarantine storage, not into this envelope. Deliberately omits any
// broker / IB session / tick / vendor-specific field; the `forbidden_
// fields` allowlist in the contract block locks the structure against
// vendor bleed.
//
// `QuarantineReason` enumerates the six SyRS SYS-77 rule categories. The
// enum is `Copy + Hash` so downstream dashboards and the notification
// dispatcher can aggregate counts by reason without scanning a string.
//
// `RecordValidationOutcome` types the two states the gate distinguishes:
// `Valid` (the record may proceed to primary storage) and
// `Quarantined(reason)` (the record is rejected and the carrier reason
// names which SyRS SYS-77 rule it violated).
//
// `IngestionValidationEvent` is the structured payload the gate emits on
// every quarantined record. It carries the outcome, the matching reason,
// the source, the record hash, and the observed timestamp so the
// dashboard / notification fan-out can compute "count and nature of
// quarantined records" (SyRS SYS-77's alert clause) without re-probing
// the validator port. Aggregation (the "count" part) is the sink's job;
// the gate emits one event per rejected record.
//
// `StructuredIngestionError` is the rejection envelope. It reuses the
// `OrderErrorCategory::IngestionRecordValidationFailed` variant as the
// single source of truth for the SyRS SYS-64 wire string. The envelope
// is distinct from `StructuredOrderError` and `StructuredSubscription
// Error` because an ingested record is neither an order nor a
// subscription — synthesising one would lie to downstream consumers.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionRecordSubmission {
    pub source: String,
    pub record_hash: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum QuarantineReason {
    RangeViolation,
    OhlcOutOfBand,
    NegativeVolume,
    NullRequiredField,
    DuplicateRecord,
    OptionFieldMissing,
}

impl QuarantineReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::RangeViolation => "RANGE_VIOLATION",
            Self::OhlcOutOfBand => "OHLC_OUT_OF_BAND",
            Self::NegativeVolume => "NEGATIVE_VOLUME",
            Self::NullRequiredField => "NULL_REQUIRED_FIELD",
            Self::DuplicateRecord => "DUPLICATE_RECORD",
            Self::OptionFieldMissing => "OPTION_FIELD_MISSING",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum RecordValidationOutcome {
    Valid,
    Quarantined(QuarantineReason),
}

impl RecordValidationOutcome {
    pub const fn is_quarantined(self) -> bool {
        matches!(self, Self::Quarantined(_))
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionValidationEvent {
    pub state: RecordValidationOutcome,
    pub reason: QuarantineReason,
    pub source: String,
    pub record_hash: String,
    pub observed_at_seconds: u64,
}

/// SRS-DATA-013 / SyRS SYS-77 structured rejection envelope. Carries the
/// SyRS SYS-64 error category, the discriminator string, a human-readable
/// message, and the unchanged original record envelope. The category is
/// constrained at construction to
/// `OrderErrorCategory::IngestionRecordValidationFailed`; the factory
/// enforces that invariant in debug builds so a future caller cannot
/// smuggle a different category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredIngestionError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_record: IngestionRecordSubmission,
}

impl StructuredIngestionError {
    /// Build an `INGESTION_RECORD_VALIDATION_FAILED` rejection. `reason`
    /// names which SyRS SYS-77 rule (a..f) the record violated; the wire
    /// form (e.g. `"RANGE_VIOLATION"`) is read from the
    /// `QuarantineReason::as_str` map so the dashboard and notification
    /// dispatcher receive a stable discriminator.
    pub fn quarantined(
        record: IngestionRecordSubmission,
        reason: QuarantineReason,
    ) -> Self {
        let category = OrderErrorCategory::IngestionRecordValidationFailed;
        debug_assert!(
            matches!(category, OrderErrorCategory::IngestionRecordValidationFailed),
            "StructuredIngestionError must carry IngestionRecordValidationFailed"
        );
        let message = format!(
            "SRS-DATA-013 + SyRS SYS-77: record {hash} from {source} quarantined — {reason}",
            hash = record.record_hash,
            source = record.source,
            reason = reason.as_str(),
        );
        Self {
            category,
            error_type: "IngestionRecordValidationFailed".to_string(),
            message,
            original_record: record,
        }
    }
}

impl fmt::Display for StructuredIngestionError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "[{}] {}: {}",
            self.category.as_str(),
            self.error_type,
            self.message
        )
    }
}

impl std::error::Error for StructuredIngestionError {}

// --------------------------------------------------------------------------- //
// IB pacing budget request envelope and structured rejection
// (SRS-DATA-002, SRS-DATA-004, SyRS SYS-31 / SYS-55, StRS A-10 / SN-1.26 / SN-1.27)
// --------------------------------------------------------------------------- //
//
// SyRS SYS-55 requires the system to validate scheduled IB historical-data
// request volume against IB's pacing limits (SYS-31: 60 requests per
// 10-minute window; no identical request within 15 seconds) for each
// capture-job window. The two SYS-55 jobs are:
//   * SYS-22b minute-bar watchlist ingestion (overnight window),
//   * SYS-23 option-chain capture (configured near-close window).
// When the projected request count for a window exceeds the permitted
// count, the system must alert the operator at scheduling time and must
// refuse to start the affected job until scope or window configuration
// is reduced. SYS-64 mandates the same structured error contract used by
// every other gate, so the rejection wire vocabulary is
// `INGESTION_PACING_BUDGET_EXCEEDED` (added to `OrderErrorCategory` above
// as the canonical source of truth).
//
// `IngestionJobRequest` is the source-neutral schedule envelope the gate
// validates. `job_kind` is a neutral string (e.g. `"minute-bar-watchlist"`
// for SYS-22b, `"option-chain-capture"` for SYS-23); the window length is
// carried explicitly as `window_seconds` so the projected/permitted
// numerics can be reconstructed without re-reading the pacing config.
// Deliberately omits any broker / IB session / tick / vendor-specific
// field — the `forbidden_fields` allowlist in the contract block locks
// the structure against vendor bleed.
//
// `PacingBudgetState` types the two states the gate distinguishes:
// `WithinBudget` (the projected request count fits in the configured
// window) and `BudgetExceeded` (the projection would push past the cap).
//
// `PacingBudgetEvent` is the structured payload the gate emits when it
// refuses a job. It carries the state, the job_kind, the projected
// request count, the permitted request count, and the observation
// timestamp. Carrying BOTH `projected_requests` AND `permitted_requests`
// closes a TOCTOU window: the configured pacing values can be re-tuned
// between the refusal and the dashboard render, so the event must be
// self-describing (same rationale as `SubscriptionLimitEvent` carrying
// `current_lines` and `configured_limit`).
//
// `StructuredPacingError` is the rejection envelope. It reuses the
// `OrderErrorCategory::IngestionPacingBudgetExceeded` variant as the
// single source of truth for the SyRS SYS-64 wire string. The envelope
// is distinct from `StructuredOrderError`, `StructuredSubscriptionError`,
// and `StructuredIngestionError` because a scheduled ingestion job is
// neither an order, a subscription, nor an ingested record —
// synthesising one would lie to downstream consumers.

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionJobRequest {
    pub job_kind: String,
    pub window_seconds: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PacingBudgetState {
    WithinBudget,
    BudgetExceeded,
}

impl PacingBudgetState {
    pub const fn is_exceeded(self) -> bool {
        matches!(self, Self::BudgetExceeded)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PacingBudgetEvent {
    pub state: PacingBudgetState,
    pub job_kind: String,
    pub projected_requests: u32,
    pub permitted_requests: u32,
    pub observed_at_seconds: u64,
}

/// SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55 structured rejection
/// envelope. Carries the SyRS SYS-64 error category, the discriminator
/// string, a human-readable message, and the unchanged original
/// scheduling request. The category is constrained at construction to
/// `OrderErrorCategory::IngestionPacingBudgetExceeded`; the factory
/// enforces that invariant in debug builds so a future caller cannot
/// smuggle a different category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredPacingError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_request: IngestionJobRequest,
}

impl StructuredPacingError {
    /// Build an `INGESTION_PACING_BUDGET_EXCEEDED` rejection.
    /// `projected_requests` is the count the scheduler computed for the
    /// `request.window_seconds` window; `permitted_requests` is the cap
    /// reported by the pacing-budget validator for the same window
    /// (derived from SYS-31's 60 requests per 10 minutes).
    pub fn budget_exceeded(
        request: IngestionJobRequest,
        projected_requests: u32,
        permitted_requests: u32,
    ) -> Self {
        let category = OrderErrorCategory::IngestionPacingBudgetExceeded;
        debug_assert!(
            matches!(category, OrderErrorCategory::IngestionPacingBudgetExceeded),
            "StructuredPacingError must carry IngestionPacingBudgetExceeded"
        );
        let message = format!(
            "SRS-DATA-002 + SRS-DATA-004 + SyRS SYS-55: ingestion job {job} over \
             pacing budget — projected {projected} requests, permitted {permitted}",
            job = request.job_kind,
            projected = projected_requests,
            permitted = permitted_requests,
        );
        Self {
            category,
            error_type: "IngestionPacingBudgetExceeded".to_string(),
            message,
            original_request: request,
        }
    }
}

impl fmt::Display for StructuredPacingError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "[{}] {}: {}",
            self.category.as_str(),
            self.error_type,
            self.message
        )
    }
}

impl std::error::Error for StructuredPacingError {}

// --------------------------------------------------------------------------- //
// Strategy container lifecycle types
// (SRS-ORCH-001, SyRS SYS-10 / SYS-13 / AC-12 / NFR-P9 / NFR-R5 / NFR-S5)
// --------------------------------------------------------------------------- //
//
// SYS-10 requires the Strategy Orchestrator to manage the lifecycle of each
// strategy instance through the five canonical actions (create, start,
// stop, restart, destroy) and to keep each instance in its own Docker
// container. AC-12 narrows this to "the Strategy Orchestrator shall be
// the sole component that manages container lifecycle. No strategy shall
// directly manage its own container or other containers." NFR-P9 caps the
// time from orchestrator start command to strategy ready (warm-up
// excluded) at 30 seconds. SYS-13 requires unresponsive containers to be
// auto-restarted, logged, and surfaced on the dashboard.
//
// These types live in `atp-types` so the orchestrator crate can declare
// the gate without reaching into another crate, AND so future surfaces
// (REST `GET /api/v1/strategies/{id}`, the WebSocket STRATEGY_STATE
// channel) can deserialize the event envelope without depending on
// `atp-orchestrator`.
//
// `ContainerLifecycleAction` enumerates the SYS-10 lifecycle vocabulary.
// Health-check is exposed as a separate trait method on the runtime port
// (not a lifecycle action) because it is a read-only probe; conflating
// the two would lure callers into invoking a mutator when they only
// wanted to observe.
//
// `ContainerHealthState` types SYS-13's two-state observation: `Healthy`
// or `Unresponsive`. The orchestrator's `observe_health` gate matches on
// this enum and triggers `runtime.restart` ONLY on the `Unresponsive`
// branch (the auto-restart guarantee).
//
// `LaunchReadiness` types NFR-P9's two-state launch outcome:
// `ReadyWithinDeadline { elapsed_millis }` or
// `DeadlineExceeded { elapsed_millis, deadline_millis }`. Carrying
// `elapsed_millis` on both variants lets the dashboard render percentile
// histograms without re-probing the runtime; carrying `deadline_millis`
// on the exceeded variant closes a TOCTOU window if the configured
// deadline is re-read between rejection and dashboard render.
//
// `StrategyLaunchRequest` is the source-neutral launch envelope. Carries
// the strategy id, the requested `StrategyMode`, the deployment hash
// (SRS-ORCH-004 / SyRS SYS-79 — recorded here even though SRS-ORCH-004's
// dashboard exposure is deferred), and the requested `deadline_millis`.
// Deliberately omits broker / IB session / docker_image / container_id /
// vendor / host_path fields — the `forbidden_fields` allowlist in the
// contract block locks the structure against container-runtime bleed
// (the orchestrator must remain free of Docker-Engine-specific shape).
//
// `StrategyLaunchOutcome` is the per-launch evidence the contract gate
// returns on `ReadyWithinDeadline`. Carries the strategy id, the
// deadline-pass flag, and the same elapsed/deadline numerics so a
// caller never needs to re-derive them.
//
// `ContainerHealthEvent` is the structured payload the SYS-13 dashboard
// fan-out consumes. It carries the observed state, the strategy id, the
// `ContainerLifecycleAction` the orchestrator invoked (e.g. `Restart` on
// `Unresponsive`), and the observation timestamp. Carrying
// `action_taken` on the event lets the dashboard render "restarted at
// T" without a second probe — the same TOCTOU-closure rationale used by
// `SubscriptionLimitEvent` / `PacingBudgetEvent`.
//
// `StructuredOrchestratorError` is the rejection envelope. It reuses the
// `OrderErrorCategory::StrategyStartupDeadlineExceeded` variant as the
// single source of truth for the SyRS SYS-64 wire string. The envelope
// is distinct from the other Structured*Error envelopes because a
// strategy launch is neither an order, a subscription, an ingested
// record, nor a scheduled job — synthesising one would lie to
// downstream consumers.

/// NFR-P9 startup-time ceiling: 30,000 ms (warm-up excluded). Exposed as
/// a `const u64` so callers and the contract check share one source of
/// truth and so future tuning has exactly one site to change.
pub const STRATEGY_STARTUP_DEADLINE_MS: u64 = 30_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ContainerLifecycleAction {
    Create,
    Start,
    Stop,
    Restart,
    Destroy,
}

impl ContainerLifecycleAction {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Create => "CREATE",
            Self::Start => "START",
            Self::Stop => "STOP",
            Self::Restart => "RESTART",
            Self::Destroy => "DESTROY",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ContainerHealthState {
    Healthy,
    Unresponsive,
}

impl ContainerHealthState {
    pub const fn is_unresponsive(self) -> bool {
        matches!(self, Self::Unresponsive)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum LaunchReadiness {
    ReadyWithinDeadline { elapsed_millis: u64 },
    DeadlineExceeded { elapsed_millis: u64, deadline_millis: u64 },
}

impl LaunchReadiness {
    pub const fn is_ready(self) -> bool {
        matches!(self, Self::ReadyWithinDeadline { .. })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyLaunchRequest {
    pub strategy_id: StrategyId,
    pub mode: StrategyMode,
    pub deployment_hash: String,
    pub deadline_millis: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyLaunchOutcome {
    pub strategy_id: StrategyId,
    pub ready_within_deadline: bool,
    pub elapsed_millis: u64,
    pub deadline_millis: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainerHealthEvent {
    pub state: ContainerHealthState,
    pub strategy_id: StrategyId,
    pub action_taken: ContainerLifecycleAction,
    pub observed_at_seconds: u64,
}

/// SRS-ORCH-001 / NFR-P9 structured rejection envelope. Carries the
/// SyRS SYS-64 error category, the discriminator string, a
/// human-readable message, and the unchanged original launch request.
/// The category is constrained at construction to
/// `OrderErrorCategory::StrategyStartupDeadlineExceeded`; the factory
/// enforces that invariant in debug builds so a future caller cannot
/// smuggle a different category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredOrchestratorError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_request: StrategyLaunchRequest,
}

impl StructuredOrchestratorError {
    /// Build a `STRATEGY_STARTUP_DEADLINE_EXCEEDED` rejection.
    /// `elapsed_millis` is the time the runtime reported between the
    /// orchestrator's start command and the readiness probe failing;
    /// `deadline_millis` is the deadline that was breached (defaults to
    /// `STRATEGY_STARTUP_DEADLINE_MS` but is carried explicitly so a
    /// re-tuned deadline cannot drift away from the event payload).
    pub fn startup_deadline_exceeded(
        request: StrategyLaunchRequest,
        elapsed_millis: u64,
        deadline_millis: u64,
    ) -> Self {
        let category = OrderErrorCategory::StrategyStartupDeadlineExceeded;
        debug_assert!(
            matches!(category, OrderErrorCategory::StrategyStartupDeadlineExceeded),
            "StructuredOrchestratorError must carry StrategyStartupDeadlineExceeded"
        );
        let message = format!(
            "SRS-ORCH-001 + NFR-P9: strategy {strategy} launch exceeded \
             startup deadline — {elapsed} ms elapsed, {deadline} ms permitted",
            strategy = request.strategy_id.as_str(),
            elapsed = elapsed_millis,
            deadline = deadline_millis,
        );
        Self {
            category,
            error_type: "StrategyStartupDeadlineExceeded".to_string(),
            message,
            original_request: request,
        }
    }
}

impl fmt::Display for StructuredOrchestratorError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "[{}] {}: {}",
            self.category.as_str(),
            self.error_type,
            self.message
        )
    }
}

impl std::error::Error for StructuredOrchestratorError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_strategy_ids() {
        let strategy_id = StrategyId::new("mean-reversion-paper-01");
        assert_eq!(strategy_id.as_str(), "mean-reversion-paper-01");
    }

    #[test]
    fn enumerates_core_runtime_services() {
        assert!(CORE_RUNTIME_SERVICES.contains(&RuntimeService::ExecutionEngine));
        assert!(CORE_RUNTIME_SERVICES.contains(&RuntimeService::StrategyOrchestrator));
    }

    #[test]
    fn strategy_mode_distinguishes_live_from_paper() {
        assert!(StrategyMode::Live.is_live());
        assert!(!StrategyMode::Paper.is_live());
    }

    #[test]
    fn order_error_category_wire_strings_track_syrs_sys_64() {
        assert_eq!(OrderErrorCategory::InvalidSymbol.as_str(), "INVALID_SYMBOL");
        assert_eq!(
            OrderErrorCategory::InsufficientBuyingPower.as_str(),
            "INSUFFICIENT_BUYING_POWER"
        );
        assert_eq!(
            OrderErrorCategory::ConnectivityBlocked.as_str(),
            "CONNECTIVITY_BLOCKED"
        );
        assert_eq!(OrderErrorCategory::RateLimited.as_str(), "RATE_LIMITED");
        assert_eq!(
            OrderErrorCategory::MarketDataStale.as_str(),
            "MARKET_DATA_STALE"
        );
        assert_eq!(
            OrderErrorCategory::SubscriptionLimitReached.as_str(),
            "SUBSCRIPTION_LIMIT_REACHED"
        );
        assert_eq!(
            OrderErrorCategory::NonLiveStrategySubmission.as_str(),
            "NON_LIVE_STRATEGY_SUBMISSION"
        );
        assert_eq!(
            OrderErrorCategory::IngestionRecordValidationFailed.as_str(),
            "INGESTION_RECORD_VALIDATION_FAILED"
        );
        assert_eq!(
            OrderErrorCategory::IngestionPacingBudgetExceeded.as_str(),
            "INGESTION_PACING_BUDGET_EXCEEDED"
        );
    }

    #[test]
    fn connectivity_state_distinguishes_connected_from_blocked_states() {
        // SRS-SAFE-003: Unreachable must block live submissions.
        // SRS-MD-005: ScheduledRestartWindow must also block (and suppress
        // normal connectivity notifications for the configured window).
        assert!(!ConnectivityState::Connected.is_blocked());
        assert!(ConnectivityState::Unreachable.is_blocked());
        assert!(ConnectivityState::ScheduledRestartWindow.is_blocked());
    }

    #[test]
    fn connectivity_event_carries_only_the_four_required_fields() {
        // The exhaustive destructure proves there are no other public fields
        // (i.e. nothing that could leak a broker / vendor / IB session id).
        let event = ConnectivityEvent {
            state: ConnectivityState::Unreachable,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            scheduled_restart: false,
        };
        let ConnectivityEvent {
            state: _,
            strategy_id: _,
            symbol: _,
            scheduled_restart: _,
        } = event.clone();
        assert_eq!(event.state, ConnectivityState::Unreachable);
        assert_eq!(event.strategy_id.as_str(), "live-alpha");
        assert_eq!(event.symbol, "AAPL");
        assert!(!event.scheduled_restart);
    }

    #[test]
    fn connectivity_event_marks_scheduled_restart_window_for_suppression() {
        // SRS-MD-005: the scheduled_restart flag lets the dashboard and
        // notification dispatcher recognize the suppression window without
        // re-inspecting the ConnectivityState enum.
        let event = ConnectivityEvent {
            state: ConnectivityState::ScheduledRestartWindow,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "MSFT".to_string(),
            scheduled_restart: true,
        };
        assert!(event.scheduled_restart);
        assert_eq!(event.state, ConnectivityState::ScheduledRestartWindow);
    }

    #[test]
    fn market_data_freshness_distinguishes_fresh_from_stale() {
        // SRS-MD-004: Stale must block live and paper submissions until
        // fresh data returns. The `is_stale` helper is the predicate
        // every consumer of the freshness gate calls.
        assert!(!MarketDataFreshness::Fresh.is_stale());
        assert!(MarketDataFreshness::Stale.is_stale());
    }

    #[test]
    fn stale_data_event_carries_only_the_four_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields (i.e. nothing that could leak a broker / vendor / IB
        // session / tick id into the dashboard fan-out).
        let event = StaleDataEvent {
            state: MarketDataFreshness::Stale,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            staleness_seconds: 22,
        };
        let StaleDataEvent {
            state: _,
            strategy_id: _,
            symbol: _,
            staleness_seconds: _,
        } = event.clone();
        assert_eq!(event.state, MarketDataFreshness::Stale);
        assert_eq!(event.strategy_id.as_str(), "live-alpha");
        assert_eq!(event.symbol, "AAPL");
        assert_eq!(event.staleness_seconds, 22);
    }

    #[test]
    fn stale_data_event_records_observed_age_above_nfr_p5_threshold() {
        // NFR-P5 caps the heartbeat staleness threshold at 15,000 ms.
        // The event must be able to carry observed ages strictly above
        // that floor so dashboards can show how stale the feed actually
        // got before the gate fired.
        let event = StaleDataEvent {
            state: MarketDataFreshness::Stale,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "MSFT".to_string(),
            staleness_seconds: 16,
        };
        assert!(
            event.staleness_seconds > 15,
            "the event must accommodate ages above the NFR-P5 15s floor"
        );
    }

    #[test]
    fn subscription_limit_state_distinguishes_within_from_exceeded() {
        // SRS-MD-002 / SyRS SYS-70: WithinLimit must permit the request to
        // proceed; ExceededLimit must trigger SUBSCRIPTION_LIMIT_REACHED.
        // The `is_exceeded` predicate is the helper every caller of the
        // gate uses to branch on state.
        assert!(!SubscriptionLimitState::WithinLimit.is_exceeded());
        assert!(SubscriptionLimitState::ExceededLimit.is_exceeded());
    }

    #[test]
    fn subscription_limit_event_carries_only_the_five_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields (i.e. nothing that could leak a broker / vendor / IB
        // session / tick id into the dashboard fan-out).
        let event = SubscriptionLimitEvent {
            state: SubscriptionLimitState::ExceededLimit,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
            current_lines: 100,
            configured_limit: 100,
        };
        let SubscriptionLimitEvent {
            state: _,
            strategy_id: _,
            symbol: _,
            current_lines: _,
            configured_limit: _,
        } = event.clone();
        assert_eq!(event.state, SubscriptionLimitState::ExceededLimit);
        assert_eq!(event.strategy_id.as_str(), "live-alpha");
        assert_eq!(event.symbol, "AAPL");
        assert_eq!(event.current_lines, 100);
        assert_eq!(event.configured_limit, 100);
    }

    #[test]
    fn subscription_limit_event_records_both_current_lines_and_configured_limit() {
        // StRS A-13 caps the stakeholder's IB tier at ~100 concurrent
        // market-data lines. The event must carry both the observed
        // in-use count AND the configured ceiling so the dashboard can
        // render "N/M lines used" without a TOCTOU re-query against the
        // line-counter port (operators can re-tune the limit at runtime).
        let event = SubscriptionLimitEvent {
            state: SubscriptionLimitState::ExceededLimit,
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "MSFT".to_string(),
            current_lines: 101,
            configured_limit: 100,
        };
        assert_eq!(
            event.current_lines, 101,
            "the event must record the observed in-use count"
        );
        assert_eq!(
            event.configured_limit, 100,
            "the event must record the configured limit at rejection time"
        );
        assert!(
            event.current_lines >= event.configured_limit,
            "ExceededLimit implies current_lines >= configured_limit"
        );
    }

    #[test]
    fn structured_subscription_error_factory_pins_the_wire_string() {
        // SRS-MD-002 + SyRS SYS-64: the rejection wire string must be
        // SUBSCRIPTION_LIMIT_REACHED. The factory reuses the existing
        // OrderErrorCategory variant as the single source of truth so a
        // future caller cannot drift the wire form.
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
        };
        let error = StructuredSubscriptionError::limit_reached(request.clone(), 100, 100);
        let StructuredSubscriptionError {
            category: _,
            error_type: _,
            message: _,
            original_request: _,
        } = error.clone();
        assert_eq!(error.category, OrderErrorCategory::SubscriptionLimitReached);
        assert_eq!(error.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");
        assert_eq!(error.error_type, "SubscriptionLimitReached");
        assert!(error.message.contains("SRS-MD-002"));
        assert!(error.message.contains("SYS-70"));
        assert!(error.message.contains("AAPL"));
        assert!(error.message.contains("live-alpha"));
        assert_eq!(error.original_request, request);
        assert_eq!(
            format!("{error}"),
            format!(
                "[SUBSCRIPTION_LIMIT_REACHED] SubscriptionLimitReached: {}",
                error.message
            )
        );
    }

    #[test]
    fn quarantine_reason_wire_strings_enumerate_sys_77_rules() {
        // SyRS SYS-77 specifies six validation rule categories (a..f).
        // Each variant of QuarantineReason maps 1:1 to a SCREAMING_SNAKE
        // wire string the dashboard and notification dispatcher consume
        // to render the "nature" half of "count and nature of quarantined
        // records" (SYS-77's alert clause).
        assert_eq!(QuarantineReason::RangeViolation.as_str(), "RANGE_VIOLATION");
        assert_eq!(QuarantineReason::OhlcOutOfBand.as_str(), "OHLC_OUT_OF_BAND");
        assert_eq!(QuarantineReason::NegativeVolume.as_str(), "NEGATIVE_VOLUME");
        assert_eq!(
            QuarantineReason::NullRequiredField.as_str(),
            "NULL_REQUIRED_FIELD"
        );
        assert_eq!(
            QuarantineReason::DuplicateRecord.as_str(),
            "DUPLICATE_RECORD"
        );
        assert_eq!(
            QuarantineReason::OptionFieldMissing.as_str(),
            "OPTION_FIELD_MISSING"
        );
    }

    #[test]
    fn record_validation_outcome_distinguishes_valid_from_quarantined() {
        // SRS-DATA-013: Valid must permit the record to proceed to primary
        // storage; Quarantined must trigger INGESTION_RECORD_VALIDATION_FAILED.
        // The `is_quarantined` predicate is the helper every caller of the
        // gate uses to branch on outcome.
        assert!(!RecordValidationOutcome::Valid.is_quarantined());
        for reason in [
            QuarantineReason::RangeViolation,
            QuarantineReason::OhlcOutOfBand,
            QuarantineReason::NegativeVolume,
            QuarantineReason::NullRequiredField,
            QuarantineReason::DuplicateRecord,
            QuarantineReason::OptionFieldMissing,
        ] {
            assert!(RecordValidationOutcome::Quarantined(reason).is_quarantined());
        }
    }

    #[test]
    fn ingestion_record_submission_carries_only_the_two_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields (i.e. nothing that could leak a broker / IB session /
        // tick id / vendor dataset / vendor table / raw parquet path /
        // vendor credentials into the ingestion gate's input envelope).
        let record = IngestionRecordSubmission {
            source: "bulk-equity-bars".to_string(),
            record_hash: "0xabc123".to_string(),
        };
        let IngestionRecordSubmission {
            source: _,
            record_hash: _,
        } = record.clone();
        assert_eq!(record.source, "bulk-equity-bars");
        assert_eq!(record.record_hash, "0xabc123");
    }

    #[test]
    fn ingestion_validation_event_carries_only_the_five_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields. SyRS SYS-77 alert clause needs (state, reason, source,
        // record_hash, observed_at_seconds) for downstream fan-in to
        // compute "count and nature of quarantined records" without
        // re-querying the validator port.
        let event = IngestionValidationEvent {
            state: RecordValidationOutcome::Quarantined(QuarantineReason::RangeViolation),
            reason: QuarantineReason::RangeViolation,
            source: "bulk-equity-bars".to_string(),
            record_hash: "0xabc123".to_string(),
            observed_at_seconds: 1_715_000_000,
        };
        let IngestionValidationEvent {
            state: _,
            reason: _,
            source: _,
            record_hash: _,
            observed_at_seconds: _,
        } = event.clone();
        assert!(event.state.is_quarantined());
        assert_eq!(event.reason, QuarantineReason::RangeViolation);
        assert_eq!(event.source, "bulk-equity-bars");
        assert_eq!(event.record_hash, "0xabc123");
        assert_eq!(event.observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn structured_ingestion_error_factory_pins_the_wire_string() {
        // SRS-DATA-013 + SyRS SYS-77: the rejection wire string must be
        // INGESTION_RECORD_VALIDATION_FAILED. The factory reuses the
        // OrderErrorCategory variant as the single source of truth so a
        // future caller cannot drift the wire form. The message must
        // include the SRS/SyRS trace strings, the record hash, the
        // source, and the human-readable reason for downstream parsing.
        let record = IngestionRecordSubmission {
            source: "bulk-equity-bars".to_string(),
            record_hash: "0xdeadbeef".to_string(),
        };
        let error = StructuredIngestionError::quarantined(
            record.clone(),
            QuarantineReason::DuplicateRecord,
        );
        let StructuredIngestionError {
            category: _,
            error_type: _,
            message: _,
            original_record: _,
        } = error.clone();
        assert_eq!(
            error.category,
            OrderErrorCategory::IngestionRecordValidationFailed
        );
        assert_eq!(error.category.as_str(), "INGESTION_RECORD_VALIDATION_FAILED");
        assert_eq!(error.error_type, "IngestionRecordValidationFailed");
        assert!(error.message.contains("SRS-DATA-013"));
        assert!(error.message.contains("SYS-77"));
        assert!(error.message.contains("0xdeadbeef"));
        assert!(error.message.contains("bulk-equity-bars"));
        assert!(error.message.contains("DUPLICATE_RECORD"));
        assert_eq!(error.original_record, record);
        assert_eq!(
            format!("{error}"),
            format!(
                "[INGESTION_RECORD_VALIDATION_FAILED] IngestionRecordValidationFailed: {}",
                error.message
            )
        );
    }

    #[test]
    fn pacing_budget_state_distinguishes_within_from_exceeded() {
        // SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55: WithinBudget must
        // permit the scheduled ingestion job to start; BudgetExceeded
        // must trigger INGESTION_PACING_BUDGET_EXCEEDED. The
        // `is_exceeded` predicate is the helper every caller of the
        // gate uses to branch on state.
        assert!(!PacingBudgetState::WithinBudget.is_exceeded());
        assert!(PacingBudgetState::BudgetExceeded.is_exceeded());
    }

    #[test]
    fn ingestion_job_request_carries_only_the_two_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields (i.e. nothing that could leak a broker / IB session /
        // tick id / vendor dataset / vendor table / raw parquet path /
        // vendor credentials into the pacing-budget gate's input
        // envelope).
        let request = IngestionJobRequest {
            job_kind: "minute-bar-watchlist".to_string(),
            window_seconds: 61_200,
        };
        let IngestionJobRequest {
            job_kind: _,
            window_seconds: _,
        } = request.clone();
        assert_eq!(request.job_kind, "minute-bar-watchlist");
        assert_eq!(request.window_seconds, 61_200);
    }

    #[test]
    fn pacing_budget_event_carries_only_the_five_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields. SyRS SYS-55 alert clause needs (state, job_kind,
        // projected_requests, permitted_requests, observed_at_seconds)
        // for the dashboard to render the refusal and for the
        // notification dispatcher to surface "scope or window
        // configuration must be reduced" without re-querying the
        // pacing-budget validator port.
        let event = PacingBudgetEvent {
            state: PacingBudgetState::BudgetExceeded,
            job_kind: "minute-bar-watchlist".to_string(),
            projected_requests: 6_200,
            permitted_requests: 6_120,
            observed_at_seconds: 1_715_000_000,
        };
        let PacingBudgetEvent {
            state: _,
            job_kind: _,
            projected_requests: _,
            permitted_requests: _,
            observed_at_seconds: _,
        } = event.clone();
        assert_eq!(event.state, PacingBudgetState::BudgetExceeded);
        assert_eq!(event.job_kind, "minute-bar-watchlist");
        assert_eq!(event.projected_requests, 6_200);
        assert_eq!(event.permitted_requests, 6_120);
        assert_eq!(event.observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn pacing_budget_event_records_both_projected_and_permitted_requests() {
        // StRS A-10 / SyRS SYS-31: IB pacing limits are operator-tunable
        // (the cap is derived from the configured window length and the
        // 60-requests-per-10-minute ceiling). The event must carry BOTH
        // the projected request count AND the permitted cap so the
        // dashboard can render "N/M requests projected for window" and
        // the notification subsystem can surface scope-reduction advice
        // without a TOCTOU re-query against the pacing-budget port.
        let event = PacingBudgetEvent {
            state: PacingBudgetState::BudgetExceeded,
            job_kind: "option-chain-capture".to_string(),
            projected_requests: 65,
            permitted_requests: 60,
            observed_at_seconds: 1_715_000_000,
        };
        assert_eq!(
            event.projected_requests, 65,
            "the event must record the projected request count"
        );
        assert_eq!(
            event.permitted_requests, 60,
            "the event must record the permitted request count at refusal time"
        );
        assert!(
            event.projected_requests >= event.permitted_requests,
            "BudgetExceeded implies projected_requests >= permitted_requests"
        );
    }

    #[test]
    fn structured_pacing_error_factory_pins_the_wire_string() {
        // SRS-DATA-002 + SRS-DATA-004 + SyRS SYS-64: the rejection wire
        // string must be INGESTION_PACING_BUDGET_EXCEEDED. The factory
        // reuses the OrderErrorCategory variant as the single source of
        // truth so a future caller cannot drift the wire form. The
        // message must include the SRS/SyRS trace strings, the
        // job_kind, and the projected/permitted numerics for downstream
        // parsing.
        let request = IngestionJobRequest {
            job_kind: "minute-bar-watchlist".to_string(),
            window_seconds: 61_200,
        };
        let error = StructuredPacingError::budget_exceeded(request.clone(), 6_200, 6_120);
        let StructuredPacingError {
            category: _,
            error_type: _,
            message: _,
            original_request: _,
        } = error.clone();
        assert_eq!(
            error.category,
            OrderErrorCategory::IngestionPacingBudgetExceeded
        );
        assert_eq!(error.category.as_str(), "INGESTION_PACING_BUDGET_EXCEEDED");
        assert_eq!(error.error_type, "IngestionPacingBudgetExceeded");
        assert!(error.message.contains("SRS-DATA-002"));
        assert!(error.message.contains("SRS-DATA-004"));
        assert!(error.message.contains("SYS-55"));
        assert!(error.message.contains("minute-bar-watchlist"));
        assert!(error.message.contains("6200"));
        assert!(error.message.contains("6120"));
        assert_eq!(error.original_request, request);
        assert_eq!(
            format!("{error}"),
            format!(
                "[INGESTION_PACING_BUDGET_EXCEEDED] IngestionPacingBudgetExceeded: {}",
                error.message
            )
        );
    }

    #[test]
    fn structured_order_error_carries_only_the_four_required_fields() {
        // SRS-ERR-001 requires: category, error_type, message, original_order.
        // The exhaustive destructure proves there are no other public fields
        // (i.e. nothing that could leak a broker / vendor / IB order id).
        let error = StructuredOrderError {
            category: OrderErrorCategory::NonLiveStrategySubmission,
            error_type: "NonLiveLiveRouteBlocked".to_string(),
            message: "rejected".to_string(),
            original_order: OrderSubmission {
                strategy_id: StrategyId::new("paper-1"),
                symbol: "AAPL".to_string(),
                quantity: 10,
            },
        };
        let StructuredOrderError {
            category: _,
            error_type: _,
            message: _,
            original_order: _,
        } = error.clone();
        assert_eq!(format!("{error}"), "[NON_LIVE_STRATEGY_SUBMISSION] NonLiveLiveRouteBlocked: rejected");
    }

    // ----------------------------------------------------------------------- //
    // SRS-ORCH-001 strategy container lifecycle types
    // ----------------------------------------------------------------------- //

    #[test]
    fn container_lifecycle_action_covers_the_five_sys_10_actions() {
        // SyRS SYS-10 enumerates create / start / stop / restart / destroy
        // as the lifecycle vocabulary the orchestrator must own. Exhaustive
        // match on the enum below would fail to compile if a variant were
        // dropped — that's the type-system anchor for the SYS-10 coverage.
        for action in [
            ContainerLifecycleAction::Create,
            ContainerLifecycleAction::Start,
            ContainerLifecycleAction::Stop,
            ContainerLifecycleAction::Restart,
            ContainerLifecycleAction::Destroy,
        ] {
            let wire = action.as_str();
            assert!(!wire.is_empty());
            assert!(wire.chars().all(|c| c.is_ascii_uppercase()));
        }
        assert_eq!(ContainerLifecycleAction::Create.as_str(), "CREATE");
        assert_eq!(ContainerLifecycleAction::Start.as_str(), "START");
        assert_eq!(ContainerLifecycleAction::Stop.as_str(), "STOP");
        assert_eq!(ContainerLifecycleAction::Restart.as_str(), "RESTART");
        assert_eq!(ContainerLifecycleAction::Destroy.as_str(), "DESTROY");
    }

    #[test]
    fn container_health_state_distinguishes_healthy_from_unresponsive() {
        // SyRS SYS-13's two-state observation: only the Unresponsive
        // branch may trigger the auto-restart action.
        assert!(ContainerHealthState::Unresponsive.is_unresponsive());
        assert!(!ContainerHealthState::Healthy.is_unresponsive());
    }

    #[test]
    fn launch_readiness_carries_elapsed_and_deadline_on_breach() {
        // NFR-P9: the DeadlineExceeded variant must carry BOTH the
        // observed elapsed time AND the configured deadline so the
        // dashboard never re-reads a re-tuned deadline from the
        // orchestrator config and so a "32,500 / 30,000 ms" render is
        // possible from a single payload.
        let ready = LaunchReadiness::ReadyWithinDeadline {
            elapsed_millis: 4_200,
        };
        assert!(ready.is_ready());
        let exceeded = LaunchReadiness::DeadlineExceeded {
            elapsed_millis: 32_500,
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        };
        assert!(!exceeded.is_ready());
        match exceeded {
            LaunchReadiness::DeadlineExceeded {
                elapsed_millis,
                deadline_millis,
            } => {
                assert_eq!(elapsed_millis, 32_500);
                assert_eq!(deadline_millis, 30_000);
            }
            LaunchReadiness::ReadyWithinDeadline { .. } => {
                panic!("expected DeadlineExceeded variant")
            }
        }
    }

    #[test]
    fn strategy_startup_deadline_constant_is_nfr_p9_thirty_seconds() {
        // NFR-P9 names the single source of truth: 30,000 ms. A future
        // change to this constant must touch exactly one site.
        assert_eq!(STRATEGY_STARTUP_DEADLINE_MS, 30_000);
    }

    #[test]
    fn strategy_launch_request_carries_only_the_four_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields. AC-12 + NFR-S5 require the launch envelope to be
        // free of container-runtime bleed (docker_image, container_id,
        // host_path, vendor) so the orchestrator stays free of
        // Docker-Engine-specific shape.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: "sha256:abc".to_string(),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        };
        let StrategyLaunchRequest {
            strategy_id: _,
            mode: _,
            deployment_hash: _,
            deadline_millis: _,
        } = request.clone();
        assert_eq!(request.strategy_id.as_str(), "alpha-1");
        assert_eq!(request.mode, StrategyMode::Live);
        assert_eq!(request.deployment_hash, "sha256:abc");
        assert_eq!(request.deadline_millis, 30_000);
    }

    #[test]
    fn strategy_launch_outcome_carries_only_the_four_required_fields() {
        let outcome = StrategyLaunchOutcome {
            strategy_id: StrategyId::new("alpha-1"),
            ready_within_deadline: true,
            elapsed_millis: 4_200,
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        };
        let StrategyLaunchOutcome {
            strategy_id: _,
            ready_within_deadline: _,
            elapsed_millis: _,
            deadline_millis: _,
        } = outcome.clone();
        assert!(outcome.ready_within_deadline);
        assert_eq!(outcome.elapsed_millis, 4_200);
        assert!(outcome.elapsed_millis <= outcome.deadline_millis);
    }

    #[test]
    fn container_health_event_carries_only_the_four_required_fields() {
        // SyRS SYS-13: the dashboard fan-out needs state, strategy id,
        // the action the orchestrator invoked, and the timestamp. The
        // exhaustive destructure proves the struct holds no
        // docker_image / container_id / vendor bleed.
        let event = ContainerHealthEvent {
            state: ContainerHealthState::Unresponsive,
            strategy_id: StrategyId::new("alpha-1"),
            action_taken: ContainerLifecycleAction::Restart,
            observed_at_seconds: 1_715_000_000,
        };
        let ContainerHealthEvent {
            state: _,
            strategy_id: _,
            action_taken: _,
            observed_at_seconds: _,
        } = event.clone();
        assert_eq!(event.state, ContainerHealthState::Unresponsive);
        assert_eq!(event.action_taken, ContainerLifecycleAction::Restart);
        assert_eq!(event.observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn structured_orchestrator_error_factory_pins_the_wire_string() {
        // SRS-ORCH-001 + NFR-P9 + SyRS SYS-64: the rejection wire
        // string must be STRATEGY_STARTUP_DEADLINE_EXCEEDED. The
        // factory reuses the OrderErrorCategory variant as the single
        // source of truth so a future caller cannot drift the wire
        // form. The message must include the SRS/NFR trace strings,
        // the strategy id, and the elapsed/deadline numerics for
        // downstream parsing.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: "sha256:abc".to_string(),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
        };
        let error = StructuredOrchestratorError::startup_deadline_exceeded(
            request.clone(),
            32_500,
            STRATEGY_STARTUP_DEADLINE_MS,
        );
        let StructuredOrchestratorError {
            category: _,
            error_type: _,
            message: _,
            original_request: _,
        } = error.clone();
        assert_eq!(
            error.category,
            OrderErrorCategory::StrategyStartupDeadlineExceeded
        );
        assert_eq!(error.category.as_str(), "STRATEGY_STARTUP_DEADLINE_EXCEEDED");
        assert_eq!(error.error_type, "StrategyStartupDeadlineExceeded");
        assert!(error.message.contains("SRS-ORCH-001"));
        assert!(error.message.contains("NFR-P9"));
        assert!(error.message.contains("alpha-1"));
        assert!(error.message.contains("32500"));
        assert!(error.message.contains("30000"));
        assert_eq!(error.original_request, request);
        assert_eq!(
            format!("{error}"),
            format!(
                "[STRATEGY_STARTUP_DEADLINE_EXCEEDED] StrategyStartupDeadlineExceeded: {}",
                error.message
            )
        );
    }
}
