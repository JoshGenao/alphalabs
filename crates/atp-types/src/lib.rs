use std::fmt;

pub mod order_lifecycle;
pub use order_lifecycle::{
    ClientCorrelationId, OrderKey, OrderLedger, OrderLifecycle, OrderLifecycleError, OrderState,
};

pub mod order_event;
pub use order_event::{
    OrderEvent, OrderEventCategory, LIVE_CALLBACK_LATENCY_P95_MS, PAPER_CALLBACK_LATENCY_P95_MS,
};

pub mod order_type;
pub use order_type::{OrderSide, OrderType, OrderTypeError};

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
    ResourceProfileInvalid,
    HostMemorySafetyMarginBreach,
    DeployedVersionInvalid,
    HotSwapDemotionTimeout,
    KillSwitchLiquidationTimeout,
    KillSwitchLiquidationProbeUnavailable,
    DuplicateClientCorrelationId,
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
            Self::ResourceProfileInvalid => "RESOURCE_PROFILE_INVALID",
            Self::HostMemorySafetyMarginBreach => "HOST_MEMORY_SAFETY_MARGIN_BREACH",
            Self::DeployedVersionInvalid => "DEPLOYED_VERSION_INVALID",
            Self::HotSwapDemotionTimeout => "HOT_SWAP_DEMOTION_TIMEOUT",
            Self::KillSwitchLiquidationTimeout => "KILL_SWITCH_LIQUIDATION_TIMEOUT",
            Self::KillSwitchLiquidationProbeUnavailable => {
                "KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE"
            }
            Self::DuplicateClientCorrelationId => "DUPLICATE_CLIENT_CORRELATION_ID",
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
    pub asset_class: AssetClass,
}

impl SubscriptionRequest {
    /// Canonical security identity for dedup + line-counting (SRS-MD-001),
    /// or a [`SecurityKeyError`] when it cannot be canonicalized (empty
    /// symbol or a not-yet-modeled option contract). The subscription manager
    /// keys the consolidated set on this, so two requests for the same
    /// normalized symbol + asset class share one upstream IB line.
    pub fn security_key(&self) -> Result<SecurityKey, SecurityKeyError> {
        SecurityKey::new(&self.symbol, self.asset_class)
    }
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
// Consolidated market-data subscription fan-out (SRS-MD-001, SyRS SYS-70)
// --------------------------------------------------------------------------- //
//
// SRS-MD-001 requires the subscription manager to CONSOLIDATE duplicate
// real-time subscriptions: when multiple strategy containers subscribe to the
// same security the manager maintains a SINGLE upstream IB market-data
// subscription and FANS the received data out to every subscriber. These
// shared types model that surface; the consolidated registry that owns the
// live subscription set lives in `atp-market-data`
// (`ConsolidatedSubscriptionRegistry`).
//
// `MarketDataTick` is the source-neutral fan-out payload the manager
// distributes. It carries the routing `symbol` and an opaque `tick_seq`
// identifier used to correlate a fan-out delivery with its upstream tick.
// `tick_seq` is a plain delivery counter the registry does not interpret —
// sequence-GAP detection is a DISTINCT concern (SRS-MD-007) and is
// deliberately NOT modeled here. The
// struct carries no broker / vendor / session identifiers: fan-out is a
// source-neutral operation.
//
// `SubscriptionChange` classifies what a subscribe/unsubscribe did to the
// consolidated set, distinguishing the line-affecting transitions
// (`Opened` = first subscriber for a symbol → one NEW upstream IB line;
// `Closed` = last subscriber left → upstream line released) from the dedup
// transitions that consume NO additional line (`SubscriberAdded`,
// `SubscriberRemoved`) and the idempotent no-ops (`AlreadySubscribed`,
// `NotSubscribed`).
//
// `SubscriptionChangeEvent` is the structured payload the manager publishes
// on every line-affecting / dedup transition. It is the SyRS SYS-61 /
// SRS-LOG-001 `subscription_change` event the Source.MARKET_DATA emitter
// logs. It carries the post-transition `subscriber_count` for the affected
// symbol and the post-transition `lines_in_use` (distinct upstream
// subscriptions) so a dashboard / log consumer never re-probes the registry.

/// The tradable / subscribable asset class for a security. SRS-SDK-003
/// constrains a strategy to one tradable asset class (equities OR options);
/// futures / crypto are out of the release baseline per the SyRS. This is
/// the security-identity dimension the market-data subscription manager
/// keys on. It is intentionally narrower than the instrument-catalog
/// `AssetClass` in `atp-adapters` (which also models Future / Etf / Index):
/// that enum lives ABOVE the dependency boundary (adapters depend on
/// `atp-types`, not the reverse), so the core subscription types cannot
/// reuse it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Default)]
pub enum AssetClass {
    #[default]
    Equity,
    Option,
}

impl AssetClass {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Equity => "EQUITY",
            Self::Option => "OPTION",
        }
    }
}

/// Why a [`SecurityKey`] could not be built. Returned by `SecurityKey::new`
/// so the subscription manager fails closed with a precise reason instead of
/// silently dropping or conflating a request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecurityKeyError {
    /// The symbol was empty or whitespace — it can never name a security.
    EmptySymbol,
    /// The request named `AssetClass::Option`, but a canonical option
    /// contract identity (underlying + expiration + strike + call/put right,
    /// or a normalized vendor-neutral contract id) is NOT yet modeled in the
    /// platform — it is owned by SRS-DATA-004 (live option-chain snapshots)
    /// and SRS-EXE-004 (multi-leg option orders). Keying an option by its
    /// underlying symbol alone would conflate distinct contracts onto one
    /// upstream IB line, so the subscription manager fails closed on options
    /// until that contract model lands. Equity keys are unaffected.
    OptionContractIdentityRequired,
}

impl fmt::Display for SecurityKeyError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            Self::EmptySymbol => "SRS-MD-001: security symbol must be non-empty",
            Self::OptionContractIdentityRequired => {
                "SRS-MD-001: option subscriptions require a full contract identity \
                 (deferred to SRS-DATA-004 / SRS-EXE-004); rejected to avoid \
                 conflating distinct contracts on one underlying"
            }
        };
        formatter.write_str(message)
    }
}

impl std::error::Error for SecurityKeyError {}

/// Canonical security identity used to deduplicate market-data subscriptions
/// and route fan-out (SRS-MD-001). Two subscription requests name the SAME
/// security — and therefore share ONE upstream IB line — iff their
/// `SecurityKey`s are equal. The key NORMALIZES the symbol (trimmed +
/// upper-cased) so `AAPL`, `aapl`, and ` AAPL ` resolve to one security, and
/// carries the `asset_class`. The fields are private: a `SecurityKey` can
/// only be built through `new`, which enforces normalization and fails closed
/// on inputs it cannot canonicalize. It carries no broker / vendor / session
/// identifier.
///
/// **Scope (SRS-MD-001 SDK-surface):** only `AssetClass::Equity` is currently
/// representable. `AssetClass::Option` is rejected by `new` because a real
/// option contract is identified by underlying + expiration + strike +
/// call/put, which the platform does not yet model (owned by SRS-DATA-004 /
/// SRS-EXE-004). The `asset_class` field + the `AssetClass::Option` variant
/// are the forward-compatible seam those features extend; until then the
/// manager fails closed on options rather than conflating them.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct SecurityKey {
    symbol: String,
    asset_class: AssetClass,
}

impl SecurityKey {
    /// Build a canonical key, normalizing the symbol (trim + upper-case).
    /// Fails closed with [`SecurityKeyError::EmptySymbol`] on an empty symbol
    /// and [`SecurityKeyError::OptionContractIdentityRequired`] on an option
    /// (whose full contract identity is deferred — see the type docs).
    pub fn new(symbol: &str, asset_class: AssetClass) -> Result<Self, SecurityKeyError> {
        let normalized = symbol.trim().to_uppercase();
        if normalized.is_empty() {
            return Err(SecurityKeyError::EmptySymbol);
        }
        if matches!(asset_class, AssetClass::Option) {
            return Err(SecurityKeyError::OptionContractIdentityRequired);
        }
        Ok(Self {
            symbol: normalized,
            asset_class,
        })
    }

    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    pub fn asset_class(&self) -> AssetClass {
        self.asset_class
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MarketDataTick {
    pub symbol: String,
    pub asset_class: AssetClass,
    pub tick_seq: u64,
}

impl MarketDataTick {
    /// Canonical security identity this tick routes to, or a
    /// [`SecurityKeyError`] when it cannot be canonicalized (empty symbol or
    /// a not-yet-modeled option contract).
    pub fn security_key(&self) -> Result<SecurityKey, SecurityKeyError> {
        SecurityKey::new(&self.symbol, self.asset_class)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SubscriptionChange {
    Opened,
    SubscriberAdded,
    AlreadySubscribed,
    SubscriberRemoved,
    Closed,
    NotSubscribed,
}

impl SubscriptionChange {
    /// True when the transition changed the number of distinct upstream IB
    /// subscriptions: `Opened` adds one line, `Closed` releases one. Dedup
    /// transitions and idempotent no-ops leave the line count unchanged —
    /// this is the SRS-MD-001 consolidation property in one predicate.
    pub const fn changes_line_count(self) -> bool {
        matches!(self, Self::Opened | Self::Closed)
    }

    /// True when the transition actually mutated the consolidated set and
    /// must therefore be published as a `SubscriptionChangeEvent`
    /// (SRS-LOG-001 `subscription_change`). The idempotent no-ops
    /// (`AlreadySubscribed` / `NotSubscribed`) are not published.
    pub const fn is_published(self) -> bool {
        !matches!(self, Self::AlreadySubscribed | Self::NotSubscribed)
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Opened => "OPENED",
            Self::SubscriberAdded => "SUBSCRIBER_ADDED",
            Self::AlreadySubscribed => "ALREADY_SUBSCRIBED",
            Self::SubscriberRemoved => "SUBSCRIBER_REMOVED",
            Self::Closed => "CLOSED",
            Self::NotSubscribed => "NOT_SUBSCRIBED",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionChangeEvent {
    pub change: SubscriptionChange,
    pub strategy_id: StrategyId,
    pub symbol: String,
    /// The asset class of the affected line. The registry keys on a
    /// `SecurityKey` (normalized symbol + asset class), so the event carries
    /// `asset_class` alongside `symbol` to keep the SRS-LOG-001 /
    /// dashboard line-usage stream unambiguous when distinct securities share
    /// a display ticker.
    pub asset_class: AssetClass,
    pub subscriber_count: u32,
    pub lines_in_use: u32,
}

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

// --------------------------------------------------------------------------- //
// Resource profile (SRS-ORCH-002, SyRS SYS-11 / SYS-57, NFR-SC1)
// --------------------------------------------------------------------------- //
//
// SRS-ORCH-002 requires the orchestrator to enforce per-container resource
// limits at launch with two named profiles:
//   * Live (IB execution path): default ≤ 512 MB RAM, ≤ 0.25 CPU cores.
//   * Paper (internal simulation path): default ≤ 300 MB RAM, ≤ 0.10 CPU cores.
//
// CPU is carried as integer hundredths (0.25 cores → 25, 0.10 cores → 10)
// rather than f32 because the wire form must compare for equality in the
// contract check and the spy assertions, and float equality is fragile
// (NaN, denormals, ULP rounding). The forbidden_fields allowlist on the
// struct locks `cpu_cores_f32` etc. out so a future refactor cannot
// silently re-introduce float drift.
//
// The validation bounds (MEM_FLOOR / MEM_CEILING / CPU_FLOOR / CPU_CEILING)
// mirror the catalogue min/max in `architecture/runtime_services.json`'s
// `configuration.keys` block (ATP_LIVE_STRATEGY_MEM_MB / _CPU and
// ATP_PAPER_STRATEGY_MEM_MB / _CPU). The catalogue is the single source
// of truth at the configuration boundary; the constants here are the
// single source of truth at the orchestrator boundary; the contract
// check (`tools/orchestrator_resource_profile_check.py`) cross-checks
// that the two agree so a future tuning has exactly two sites that must
// match, and the gate proves they do.

/// SRS-ORCH-002 / SyRS SYS-11 default live-container memory cap (MB).
pub const LIVE_PROFILE_MEM_MB: u32 = 512;

/// SRS-ORCH-002 / SyRS SYS-11 default live-container CPU cap, in
/// hundredths of a core (0.25 cores → 25).
pub const LIVE_PROFILE_CPU_HUNDREDTHS: u32 = 25;

/// SRS-ORCH-002 / SyRS SYS-11 default paper-container memory cap (MB).
pub const PAPER_PROFILE_MEM_MB: u32 = 300;

/// SRS-ORCH-002 / SyRS SYS-11 default paper-container CPU cap, in
/// hundredths of a core (0.10 cores → 10).
pub const PAPER_PROFILE_CPU_HUNDREDTHS: u32 = 10;

/// Validation floor on memory MB. Mirrors the catalogue
/// `ATP_*_STRATEGY_MEM_MB.min` value (64).
pub const RESOURCE_PROFILE_MEM_FLOOR_MB: u32 = 64;

/// Validation ceiling on memory MB. Mirrors the catalogue
/// `ATP_*_STRATEGY_MEM_MB.max` value (65536).
pub const RESOURCE_PROFILE_MEM_CEILING_MB: u32 = 65_536;

/// Validation floor on CPU hundredths. Mirrors the catalogue
/// `ATP_*_STRATEGY_CPU.min` value (0.05 cores → 5 hundredths).
pub const RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS: u32 = 5;

/// Validation ceiling on CPU hundredths. Mirrors the catalogue
/// `ATP_*_STRATEGY_CPU.max` value (16.0 cores → 1600 hundredths).
pub const RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS: u32 = 1_600;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ResourceProfile {
    pub mem_mb: u32,
    pub cpu_hundredths: u32,
}

impl ResourceProfile {
    /// SyRS SYS-11 default live-container profile (512 MB / 0.25 CPU).
    pub const fn live_default() -> Self {
        Self {
            mem_mb: LIVE_PROFILE_MEM_MB,
            cpu_hundredths: LIVE_PROFILE_CPU_HUNDREDTHS,
        }
    }

    /// SyRS SYS-11 default paper-container profile (300 MB / 0.10 CPU).
    pub const fn paper_default() -> Self {
        Self {
            mem_mb: PAPER_PROFILE_MEM_MB,
            cpu_hundredths: PAPER_PROFILE_CPU_HUNDREDTHS,
        }
    }

    /// Mode-keyed dispatch. Live → `live_default()`, Paper → `paper_default()`.
    /// The match is exhaustive on `StrategyMode` so a future variant addition
    /// would fail to compile here — the dispatch cannot silently fall through
    /// to a wrong profile.
    pub const fn for_mode(mode: StrategyMode) -> Self {
        match mode {
            StrategyMode::Live => Self::live_default(),
            StrategyMode::Paper => Self::paper_default(),
        }
    }

    /// SRS-ORCH-002 "configuration overrides are validated" — enforce the
    /// SRS-ARCH-005 catalogue min/max bounds at the orchestrator boundary
    /// so the wire type cannot carry an out-of-range value past `launch`.
    /// The bounds are the SAME values the catalogue validator enforces at
    /// the configuration boundary; this is the second gate (defence in
    /// depth) so a programmatically-constructed `ResourceProfile` that
    /// bypassed the catalogue (e.g. a test fixture, a future REST API
    /// override) is still rejected before it reaches the runtime port.
    pub fn validate(&self) -> Result<(), ResourceProfileError> {
        if self.mem_mb < RESOURCE_PROFILE_MEM_FLOOR_MB {
            return Err(ResourceProfileError::MemBelowFloor {
                mem_mb: self.mem_mb,
                floor_mb: RESOURCE_PROFILE_MEM_FLOOR_MB,
            });
        }
        if self.mem_mb > RESOURCE_PROFILE_MEM_CEILING_MB {
            return Err(ResourceProfileError::MemAboveCeiling {
                mem_mb: self.mem_mb,
                ceiling_mb: RESOURCE_PROFILE_MEM_CEILING_MB,
            });
        }
        if self.cpu_hundredths < RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS {
            return Err(ResourceProfileError::CpuBelowFloor {
                cpu_hundredths: self.cpu_hundredths,
                floor_hundredths: RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS,
            });
        }
        if self.cpu_hundredths > RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS {
            return Err(ResourceProfileError::CpuAboveCeiling {
                cpu_hundredths: self.cpu_hundredths,
                ceiling_hundredths: RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS,
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ResourceProfileError {
    MemBelowFloor { mem_mb: u32, floor_mb: u32 },
    MemAboveCeiling { mem_mb: u32, ceiling_mb: u32 },
    CpuBelowFloor { cpu_hundredths: u32, floor_hundredths: u32 },
    CpuAboveCeiling { cpu_hundredths: u32, ceiling_hundredths: u32 },
}

impl ResourceProfileError {
    /// Short discriminator string used by the rejection wire form.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::MemBelowFloor { .. } => "MemBelowFloor",
            Self::MemAboveCeiling { .. } => "MemAboveCeiling",
            Self::CpuBelowFloor { .. } => "CpuBelowFloor",
            Self::CpuAboveCeiling { .. } => "CpuAboveCeiling",
        }
    }
}

impl fmt::Display for ResourceProfileError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MemBelowFloor { mem_mb, floor_mb } => write!(
                formatter,
                "memory {mem_mb} MB is below the configured floor of {floor_mb} MB",
            ),
            Self::MemAboveCeiling { mem_mb, ceiling_mb } => write!(
                formatter,
                "memory {mem_mb} MB exceeds the configured ceiling of {ceiling_mb} MB",
            ),
            Self::CpuBelowFloor {
                cpu_hundredths,
                floor_hundredths,
            } => write!(
                formatter,
                "CPU {cpu_hundredths} hundredths is below the configured floor of {floor_hundredths} hundredths",
            ),
            Self::CpuAboveCeiling {
                cpu_hundredths,
                ceiling_hundredths,
            } => write!(
                formatter,
                "CPU {cpu_hundredths} hundredths exceeds the configured ceiling of {ceiling_hundredths} hundredths",
            ),
        }
    }
}

impl std::error::Error for ResourceProfileError {}

// --------------------------------------------------------------------------- //
// Workload priority + host memory safety margin (SRS-ORCH-003,
// SyRS SYS-57 / SYS-58)
// --------------------------------------------------------------------------- //
//
// SRS-ORCH-003 says: "enforce workload priority when the configured host
// memory safety margin would be breached." SyRS SYS-57 names the workload
// priority hierarchy (highest → lowest):
//
//   1. live strategy + execution engine
//   2. market-data subscription manager
//   3. paper strategy containers
//   4. nightly data ingestion
//   5. factor pipeline
//   6. backtesting engine
//   7. research / Jupyter
//
// SyRS SYS-58 adds the invariants the orchestrator's admission gate
// must enforce:
//   * refuse to deploy new strategy containers when available host
//     memory would dip below the configured safety margin;
//   * if a higher-priority workload needs resources, terminate the
//     lowest-priority active *batch* workload (the SYS-58 wording);
//   * never terminate the live-trading strategy for lower-priority
//     work — even if a stale registry projection were to list it as
//     the "lowest priority" item, it must be skipped.
//
// `WorkloadPriority` encodes the SYS-57 hierarchy as an ordinal enum
// rather than a numeric score because the spec wording is categorical
// (the priorities are *kinds* of workload, not weighted positions). The
// `rank()` method projects the ordinal onto `1..=7` (lower = higher
// priority) so the arbitration loop can compare them as integers
// without committing to a stable wire form for the numeric value.
//
// `WorkloadKind` is orthogonal to priority: it splits workloads into
// `Continuous` (live strategy, market data, paper strategies — these
// are long-running and SYS-58 (b) protects them from eviction) and
// `Batch` (nightly ingestion, factor pipeline, backtest, research —
// SYS-58 (b) explicitly says "terminate the lowest-priority active
// BATCH workload"). The arbitration loop only walks `Batch` candidates.
//
// `HostMemorySafetyMargin` is the configured floor on available host
// memory (default 2048 MB per SyRS SYS-57). It is validated against the
// SRS-ARCH-005 catalogue bounds the same way `ResourceProfile` is —
// the catalogue is the single source of truth at the configuration
// boundary; the constants here are the single source of truth at the
// orchestrator boundary; `tools/orchestrator_workload_priority_check.py`
// cross-checks them.
//
// `RegisteredWorkload` is the registry projection the orchestrator's
// `admit_workload` gate iterates over. Carrying `profile` lets the
// arbitration loop estimate how much memory each candidate eviction
// would free without re-querying the runtime port.
//
// `WorkloadAdmissionEvent` is the audit / dashboard / notification
// payload published whenever a refusal or termination happens. It is a
// separate event family from `ContainerHealthEvent` (which carries
// `ContainerLifecycleAction`) because the two events are routed to
// different alert lanes in the deferred dispatcher and conflating them
// would force the dispatcher to peek inside the payload to discover
// which lane to fan to.

/// SRS-ORCH-003 / SyRS SYS-57 default host-memory safety margin (MB).
/// Mirrors the catalogue `ATP_HOST_MEMORY_SAFETY_MARGIN_MB.default`.
pub const HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT: u32 = 2_048;

/// SRS-ARCH-005 catalogue floor for the safety margin (MB). Mirrors
/// `ATP_HOST_MEMORY_SAFETY_MARGIN_MB.min`.
pub const HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR: u32 = 256;

/// SRS-ARCH-005 catalogue ceiling for the safety margin (MB). Mirrors
/// `ATP_HOST_MEMORY_SAFETY_MARGIN_MB.max`.
pub const HOST_MEMORY_SAFETY_MARGIN_MB_CEILING: u32 = 1_048_576;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct HostMemorySafetyMargin {
    pub mb: u32,
}

impl HostMemorySafetyMargin {
    pub const fn new(mb: u32) -> Self {
        Self { mb }
    }

    /// SyRS SYS-57 default (2048 MB).
    pub const fn default_margin() -> Self {
        Self {
            mb: HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT,
        }
    }

    /// SRS-ORCH-003 "configuration overrides are validated" — enforce
    /// the SRS-ARCH-005 catalogue min/max bounds at the orchestrator
    /// boundary. Defence in depth: a programmatically-constructed
    /// margin that bypassed the catalogue (test fixture, future REST
    /// API override) is still rejected before it reaches `admit_workload`.
    pub fn validate(&self) -> Result<(), HostMemorySafetyMarginError> {
        if self.mb < HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR {
            return Err(HostMemorySafetyMarginError::BelowFloor {
                value_mb: self.mb,
                floor_mb: HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR,
            });
        }
        if self.mb > HOST_MEMORY_SAFETY_MARGIN_MB_CEILING {
            return Err(HostMemorySafetyMarginError::AboveCeiling {
                value_mb: self.mb,
                ceiling_mb: HOST_MEMORY_SAFETY_MARGIN_MB_CEILING,
            });
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HostMemorySafetyMarginError {
    BelowFloor { value_mb: u32, floor_mb: u32 },
    AboveCeiling { value_mb: u32, ceiling_mb: u32 },
}

impl HostMemorySafetyMarginError {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::BelowFloor { .. } => "BelowFloor",
            Self::AboveCeiling { .. } => "AboveCeiling",
        }
    }
}

impl fmt::Display for HostMemorySafetyMarginError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::BelowFloor { value_mb, floor_mb } => write!(
                formatter,
                "host memory safety margin {value_mb} MB is below the configured floor of {floor_mb} MB",
            ),
            Self::AboveCeiling {
                value_mb,
                ceiling_mb,
            } => write!(
                formatter,
                "host memory safety margin {value_mb} MB exceeds the configured ceiling of {ceiling_mb} MB",
            ),
        }
    }
}

impl std::error::Error for HostMemorySafetyMarginError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WorkloadPriority {
    LiveStrategy,
    MarketDataSubscriptionManager,
    PaperStrategy,
    NightlyDataIngestion,
    FactorPipeline,
    Backtest,
    Research,
}

impl WorkloadPriority {
    /// SYS-57 ordinal rank: 1 = highest priority, 7 = lowest. Lower
    /// numeric value means higher priority. The match is exhaustive so
    /// a future variant addition would fail to compile here — the
    /// hierarchy cannot silently fall through to an unranked value.
    pub const fn rank(self) -> u8 {
        match self {
            Self::LiveStrategy => 1,
            Self::MarketDataSubscriptionManager => 2,
            Self::PaperStrategy => 3,
            Self::NightlyDataIngestion => 4,
            Self::FactorPipeline => 5,
            Self::Backtest => 6,
            Self::Research => 7,
        }
    }

    /// SyRS SYS-58 (b): only batch workloads may be terminated for
    /// lower-priority arbitration. Continuous workloads (live, market
    /// data, paper) are immune from eviction.
    pub const fn default_kind(self) -> WorkloadKind {
        match self {
            Self::LiveStrategy
            | Self::MarketDataSubscriptionManager
            | Self::PaperStrategy => WorkloadKind::Continuous,
            Self::NightlyDataIngestion
            | Self::FactorPipeline
            | Self::Backtest
            | Self::Research => WorkloadKind::Batch,
        }
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::LiveStrategy => "LIVE_STRATEGY",
            Self::MarketDataSubscriptionManager => "MARKET_DATA_SUBSCRIPTION_MANAGER",
            Self::PaperStrategy => "PAPER_STRATEGY",
            Self::NightlyDataIngestion => "NIGHTLY_DATA_INGESTION",
            Self::FactorPipeline => "FACTOR_PIPELINE",
            Self::Backtest => "BACKTEST",
            Self::Research => "RESEARCH",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WorkloadKind {
    Continuous,
    Batch,
}

impl WorkloadKind {
    pub const fn is_batch(self) -> bool {
        matches!(self, Self::Batch)
    }

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Continuous => "CONTINUOUS",
            Self::Batch => "BATCH",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct WorkloadId(String);

impl WorkloadId {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RegisteredWorkload {
    pub id: WorkloadId,
    pub priority: WorkloadPriority,
    pub kind: WorkloadKind,
    pub profile: ResourceProfile,
}

/// SyRS SYS-58 reason payload carried on every refusal and termination
/// event. Currently single-variant because SYS-57 / SYS-58 name exactly
/// one trigger (host memory below the safety margin); future SyRS
/// revisions adding e.g. CPU-margin breaches would extend this enum.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WorkloadAdmissionReason {
    HostMemoryBelowSafetyMargin {
        available_mb: u64,
        safety_margin_mb: u32,
    },
}

impl WorkloadAdmissionReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::HostMemoryBelowSafetyMargin { .. } => "HostMemoryBelowSafetyMargin",
        }
    }
}

impl fmt::Display for WorkloadAdmissionReason {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::HostMemoryBelowSafetyMargin {
                available_mb,
                safety_margin_mb,
            } => write!(
                formatter,
                "host memory {available_mb} MB available falls below safety margin {safety_margin_mb} MB",
            ),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkloadAdmissionEvent {
    /// SyRS SYS-58 (a): a new lower-priority workload was refused
    /// because admitting it would breach the safety margin and no
    /// batch workload could be evicted to make room.
    Refused {
        workload_id: WorkloadId,
        priority: WorkloadPriority,
        reason: WorkloadAdmissionReason,
        observed_at_seconds: u64,
    },
    /// SyRS SYS-58 (b): a lower-priority batch workload was terminated
    /// to free memory for a higher-priority arriving workload.
    Terminated {
        terminated_workload_id: WorkloadId,
        terminated_priority: WorkloadPriority,
        admitted_workload_id: WorkloadId,
        admitted_priority: WorkloadPriority,
        reason: WorkloadAdmissionReason,
        observed_at_seconds: u64,
    },
    /// SyRS SYS-58 audit completeness: a registry termination call
    /// returned `Err` (Docker/cgroup failure, registry desync). The
    /// gate did NOT bank the workload's memory; operators see the
    /// failure here rather than silently in logs.
    TerminationFailed {
        attempted_workload_id: WorkloadId,
        attempted_priority: WorkloadPriority,
        admitted_workload_id: WorkloadId,
        admitted_priority: WorkloadPriority,
        failure_reason: String,
        observed_at_seconds: u64,
    },
    /// The host-memory probe (sysinfo / procfs / future adapter)
    /// returned `Err`. The gate fails closed and refuses the
    /// admission — operators see the probe failure here distinctly
    /// from a normal memory-margin breach.
    HostProbeFailed {
        workload_id: WorkloadId,
        priority: WorkloadPriority,
        failure_reason: String,
        observed_at_seconds: u64,
    },
    /// The workload registry's `active()` call returned `Err` (Docker
    /// Engine timeout, in-process registry desync, etc.). The gate
    /// fails closed and refuses the admission — operators see the
    /// listing failure here distinctly from a normal memory-margin
    /// breach.
    RegistryListingFailed {
        workload_id: WorkloadId,
        priority: WorkloadPriority,
        failure_reason: String,
        observed_at_seconds: u64,
    },
}

impl WorkloadAdmissionEvent {
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Refused { .. } => "REFUSED",
            Self::Terminated { .. } => "TERMINATED",
            Self::TerminationFailed { .. } => "TERMINATION_FAILED",
            Self::HostProbeFailed { .. } => "HOST_PROBE_FAILED",
            Self::RegistryListingFailed { .. } => "REGISTRY_LISTING_FAILED",
        }
    }
}

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

// --------------------------------------------------------------------------- //
// Deployed-version recording (SRS-ORCH-004, SyRS SYS-79)
// --------------------------------------------------------------------------- //
//
// SyRS SYS-79: "The Strategy Orchestrator shall record the deployed
// version of each strategy container's code at deployment time, using
// at minimum: a hash of the strategy source file(s) and the deployment
// timestamp. The deployed version shall be displayed on the dashboard
// (SYS-41) and queryable via the REST API (IF-9). Backtest results
// (SYS-21) shall record the strategy code version used for each
// backtest run."
//
// `SourceHash` is a newtype around `String` that pins the wire format
// of the strategy source hash. The format is `sha256:<64-hex-chars>`
// (SHA-256 produces 32 bytes = 64 hex characters). The newtype marks
// intent at the type level (a free-form `String` cannot drift into a
// docker-image tag, a git commit SHA, or some vendor-specific
// identifier); the validation lives in `validate()` and is called at
// the orchestrator launch boundary so a programmatically-constructed
// malformed hash cannot reach the runtime port. This mirrors the
// SRS-ORCH-002 `ResourceProfile` / `validate()` pattern — fields are
// public for ergonomic destructure, but the construction path does
// not pre-validate so test fixtures and env-var parsers can build
// the value the same way the gate does.
//
// `DeployedVersion` is the audit-trail record: hash + deployment
// timestamp. The two together form the "version identifier" SYS-79
// names; `version_identifier()` projects them onto a canonical string
// suitable for the dashboard (SYS-41), the REST API (IF-9), and
// backtest result rows (SYS-21) so the three surfaces render the
// same identifier without each computing its own (SRS-ORCH-004
// acceptance criterion: "display or return the same version
// identifier").
//
// The forbidden-fields allowlist on `DeployedVersion` keeps the
// audit shape free of container-runtime / vendor / git bleed: no
// docker_image, container_id, vendor, git_commit, git_branch,
// build_number, ci_run_id. The hash is the single source of truth
// for the source-file fingerprint; coupling to a build-system
// artifact would force the audit trail to track multiple identifiers
// for the same code, breaking SYS-79's "same version identifier"
// guarantee.

/// SyRS SYS-79 hash algorithm prefix. Pins the wire form so a
/// future caller cannot drift to `md5:` / `sha1:` / `blake3:` etc.
/// without an explicit spec change.
pub const SOURCE_HASH_ALGORITHM_PREFIX: &str = "sha256:";

/// Hex digest length for SHA-256. 32 bytes × 2 hex chars per byte = 64.
pub const SOURCE_HASH_DIGEST_HEX_LENGTH: usize = 64;

/// Total expected length of a serialized `SourceHash`: `"sha256:"` (7)
/// + 64 hex chars = 71. Exposed as a const so callers and the contract
/// check share one source of truth.
pub const SOURCE_HASH_TOTAL_LENGTH: usize = 7 + SOURCE_HASH_DIGEST_HEX_LENGTH;

/// SRS-ORCH-004 / SyRS SYS-79 source-file fingerprint. Wraps a
/// validated `sha256:<64-hex>` string. Construction does NOT validate
/// (test fixtures and env-var parsers may build the value the same
/// way the gate does); `validate()` enforces the format at the
/// orchestrator launch boundary.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct SourceHash(String);

impl SourceHash {
    /// Wraps `raw` as a `SourceHash` without validating. Callers must
    /// invoke `validate()` at the trust boundary (the orchestrator's
    /// launch gate does this) — a programmatically-constructed
    /// malformed hash will be rejected there.
    pub fn new(raw: impl Into<String>) -> Self {
        Self(raw.into())
    }

    /// SyRS SYS-79 / SRS-ORCH-004 wire-form validation. Enforces the
    /// `sha256:` prefix, the 64-hex-char digest length, and the
    /// lower-case-hex alphabet. The orchestrator's `launch` gate
    /// calls this BEFORE invoking the runtime port so a misformed
    /// hash never reaches `runtime.create`.
    pub fn validate(&self) -> Result<(), SourceHashError> {
        Self::validate_str(&self.0)
    }

    /// Stand-alone validator over a borrowed string. Used by
    /// `validate()` and by env-var / config parsers that want to
    /// reject a bad value before constructing the newtype.
    pub fn validate_str(raw: &str) -> Result<(), SourceHashError> {
        if !raw.starts_with(SOURCE_HASH_ALGORITHM_PREFIX) {
            // Discriminate between "missing prefix entirely" and
            // "wrong algorithm prefix" so the dashboard can render
            // the cause precisely.
            if let Some((prefix, _)) = raw.split_once(':') {
                if !prefix.is_empty() {
                    return Err(SourceHashError::UnknownAlgorithm {
                        found: prefix.to_string(),
                    });
                }
            }
            return Err(SourceHashError::MissingAlgorithmPrefix);
        }
        let digest = &raw[SOURCE_HASH_ALGORITHM_PREFIX.len()..];
        if digest.len() != SOURCE_HASH_DIGEST_HEX_LENGTH {
            return Err(SourceHashError::InvalidDigestLength {
                found: digest.len(),
                expected: SOURCE_HASH_DIGEST_HEX_LENGTH,
            });
        }
        for ch in digest.chars() {
            if !ch.is_ascii_hexdigit() || ch.is_ascii_uppercase() {
                // Reject upper-case hex too: the wire form is
                // lower-case so a re-serialization round-trip is
                // stable across producers (SHA-256 implementations
                // commonly emit lower-case; pinning the case avoids
                // a future drift on the dashboard / REST surface).
                return Err(SourceHashError::NonHexDigest { found: ch });
            }
        }
        Ok(())
    }

    /// Borrow the full `sha256:<digest>` wire form.
    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// Borrow the algorithm name (currently always `"sha256"`). Does
    /// not validate; assumes the value was already accepted by
    /// `validate()`. Returns an empty string if the prefix is
    /// missing (so callers don't have to handle `Option` for the
    /// happy path).
    pub fn algorithm(&self) -> &str {
        match self.0.split_once(':') {
            Some((prefix, _)) => prefix,
            None => "",
        }
    }

    /// Borrow the hex-digest portion (everything after the
    /// `sha256:` prefix). Returns an empty string if the prefix is
    /// missing.
    pub fn digest(&self) -> &str {
        match self.0.split_once(':') {
            Some((_, digest)) => digest,
            None => "",
        }
    }
}

impl fmt::Display for SourceHash {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

/// SRS-ORCH-004 source-hash validation failure surface. Each variant
/// carries the offending value so the rejection message can render
/// it for operators.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SourceHashError {
    MissingAlgorithmPrefix,
    UnknownAlgorithm { found: String },
    InvalidDigestLength { found: usize, expected: usize },
    NonHexDigest { found: char },
}

impl SourceHashError {
    /// Short discriminator string for the rejection wire form.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::MissingAlgorithmPrefix => "MissingAlgorithmPrefix",
            Self::UnknownAlgorithm { .. } => "UnknownAlgorithm",
            Self::InvalidDigestLength { .. } => "InvalidDigestLength",
            Self::NonHexDigest { .. } => "NonHexDigest",
        }
    }
}

impl fmt::Display for SourceHashError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MissingAlgorithmPrefix => write!(
                formatter,
                "source hash is missing the `sha256:` algorithm prefix",
            ),
            Self::UnknownAlgorithm { found } => write!(
                formatter,
                "source hash uses unknown algorithm `{found}` — only `sha256` is supported (SyRS SYS-79)",
            ),
            Self::InvalidDigestLength { found, expected } => write!(
                formatter,
                "source hash digest is {found} hex characters, expected {expected} (SHA-256 = 32 bytes = 64 hex chars)",
            ),
            Self::NonHexDigest { found } => write!(
                formatter,
                "source hash digest contains non-lower-case-hex character `{found}`",
            ),
        }
    }
}

impl std::error::Error for SourceHashError {}

/// SRS-ORCH-004 / SyRS SYS-79 deployed-version record. The audit
/// trail published at deployment time, queryable by the deferred
/// dashboard (SYS-41), REST API (IF-9), and backtest result rows
/// (SYS-21). `version_identifier()` is the single canonical string
/// rendered across all three surfaces.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeployedVersion {
    pub source_hash: SourceHash,
    pub deployed_at_seconds: u64,
}

impl DeployedVersion {
    pub fn new(source_hash: SourceHash, deployed_at_seconds: u64) -> Self {
        Self {
            source_hash,
            deployed_at_seconds,
        }
    }

    /// SRS-ORCH-004 acceptance criterion: dashboard, REST API, and
    /// backtest results "display or return the same version
    /// identifier". This is the canonical string form — the hash
    /// (which already encodes the source) followed by the deployment
    /// timestamp. The `@` separator avoids collision with the
    /// algorithm-prefix `:` and the hex alphabet.
    pub fn version_identifier(&self) -> String {
        format!("{}@{}", self.source_hash.as_str(), self.deployed_at_seconds)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyLaunchRequest {
    pub strategy_id: StrategyId,
    pub mode: StrategyMode,
    /// SRS-ORCH-004 / SyRS SYS-79 source-file fingerprint. A
    /// `SourceHash` is a typed wrapper around `sha256:<64-hex>` —
    /// the orchestrator's launch gate calls `validate()` before
    /// invoking the runtime port so a malformed override is
    /// refused with `OrderErrorCategory::DeployedVersionInvalid`
    /// instead of reaching `runtime.create`.
    pub deployment_hash: SourceHash,
    pub deadline_millis: u64,
    /// SRS-ORCH-002 / SyRS SYS-11 resource profile carried on the launch
    /// envelope so the orchestrator can validate it once at the gate and
    /// the runtime port can apply it inside `create`. Mode-keyed defaults
    /// (live: 512 MB / 0.25 CPU; paper: 300 MB / 0.10 CPU) are produced by
    /// `ResourceProfile::for_mode(mode)`.
    pub profile: ResourceProfile,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StrategyLaunchOutcome {
    pub strategy_id: StrategyId,
    pub ready_within_deadline: bool,
    pub elapsed_millis: u64,
    pub deadline_millis: u64,
    /// SRS-ORCH-002 evidence: the resource profile the orchestrator
    /// actually allocated, copied verbatim from `request.profile`. The
    /// audit log records what was applied; the contract check enforces
    /// `outcome.profile == request.profile` (no silent re-defaulting at
    /// the gate).
    pub profile: ResourceProfile,
    /// SRS-ORCH-004 / SyRS SYS-79 evidence: the deployed version
    /// the orchestrator recorded at deployment time. Carries the
    /// source hash from `request.deployment_hash` plus the
    /// deployment timestamp the gate observed. The contract check
    /// enforces `outcome.deployed_version.source_hash == request.deployment_hash`
    /// so the gate cannot silently re-hash or substitute.
    pub deployed_version: DeployedVersion,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainerHealthEvent {
    pub state: ContainerHealthState,
    pub strategy_id: StrategyId,
    pub action_taken: ContainerLifecycleAction,
    pub observed_at_seconds: u64,
}

/// SRS-ORCH-001..004 structured rejection envelope. Carries the
/// SyRS SYS-64 error category, the discriminator string, a
/// human-readable message, and the unchanged original launch request.
/// The category is constrained at construction to one of the
/// orchestrator-rejection categories — currently
/// `StrategyStartupDeadlineExceeded` (SRS-ORCH-001 / NFR-P9),
/// `ResourceProfileInvalid` (SRS-ORCH-002 / SyRS SYS-11),
/// `HostMemorySafetyMarginBreach` (SRS-ORCH-003 / SyRS SYS-57), or
/// `DeployedVersionInvalid` (SRS-ORCH-004 / SyRS SYS-79). Each
/// factory enforces its category invariant in debug builds so a
/// future caller cannot smuggle a different category through this
/// envelope.
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

    /// Build a `RESOURCE_PROFILE_INVALID` rejection. SRS-ORCH-002 +
    /// SyRS SYS-11: a launch whose resource profile fails validation
    /// at the orchestrator boundary must be refused without invoking
    /// the runtime port (no `create`, no `start`) so a misconfigured
    /// override never reaches the host. The error carries the original
    /// launch request unchanged AND a discriminator string for the
    /// specific validation failure (MemBelowFloor / MemAboveCeiling /
    /// CpuBelowFloor / CpuAboveCeiling).
    pub fn resource_profile_invalid(
        request: StrategyLaunchRequest,
        violation: ResourceProfileError,
    ) -> Self {
        let category = OrderErrorCategory::ResourceProfileInvalid;
        debug_assert!(
            matches!(category, OrderErrorCategory::ResourceProfileInvalid),
            "StructuredOrchestratorError must carry ResourceProfileInvalid"
        );
        let message = format!(
            "SRS-ORCH-002 + SyRS SYS-11: strategy {strategy} launch refused — {violation}",
            strategy = request.strategy_id.as_str(),
        );
        Self {
            category,
            error_type: format!("ResourceProfileInvalid::{}", violation.as_str()),
            message,
            original_request: request,
        }
    }

    /// Build a `DEPLOYED_VERSION_INVALID` rejection. SRS-ORCH-004 +
    /// SyRS SYS-79: a launch whose source-hash fails wire-form
    /// validation at the orchestrator boundary must be refused
    /// without invoking the runtime port (no `create`, no `start`)
    /// so a misconfigured override never reaches the host. The
    /// error carries the original launch request unchanged AND a
    /// discriminator string for the specific validation failure
    /// (MissingAlgorithmPrefix / UnknownAlgorithm / InvalidDigestLength
    /// / NonHexDigest). The rejection is a pure structured error
    /// with NO sink event (no container exists to destroy; emitting
    /// an event would lie about a destroy that never happened).
    pub fn deployed_version_invalid(
        request: StrategyLaunchRequest,
        violation: SourceHashError,
    ) -> Self {
        let category = OrderErrorCategory::DeployedVersionInvalid;
        debug_assert!(
            matches!(category, OrderErrorCategory::DeployedVersionInvalid),
            "StructuredOrchestratorError must carry DeployedVersionInvalid"
        );
        let message = format!(
            "SRS-ORCH-004 + SyRS SYS-79: strategy {strategy} launch refused — {violation}",
            strategy = request.strategy_id.as_str(),
        );
        let discriminator = violation.as_str().to_string();
        Self {
            category,
            error_type: format!("DeployedVersionInvalid::{discriminator}"),
            message,
            original_request: request,
        }
    }

    /// Build a `HOST_MEMORY_SAFETY_MARGIN_BREACH` rejection. SRS-ORCH-003
    /// + SyRS SYS-57 / SYS-58: a launch refused because admitting the
    /// workload would push available host memory below the configured
    /// safety margin AND no lower-priority batch workload was evictable
    /// to free enough memory. The error carries the original launch
    /// request unchanged plus the two numerics the dashboard needs to
    /// render the refusal cause (`available_mb` at the time of the
    /// probe, `safety_margin_mb` the configured floor).
    pub fn host_memory_safety_margin_breach(
        request: StrategyLaunchRequest,
        available_mb: u64,
        safety_margin_mb: u32,
    ) -> Self {
        let category = OrderErrorCategory::HostMemorySafetyMarginBreach;
        debug_assert!(
            matches!(category, OrderErrorCategory::HostMemorySafetyMarginBreach),
            "StructuredOrchestratorError must carry HostMemorySafetyMarginBreach"
        );
        let message = format!(
            "SRS-ORCH-003 + SyRS SYS-57 / SYS-58: strategy {strategy} launch refused — host memory {available} MB available falls below safety margin {margin} MB and no batch workload was evictable",
            strategy = request.strategy_id.as_str(),
            available = available_mb,
            margin = safety_margin_mb,
        );
        Self {
            category,
            error_type: "HostMemorySafetyMarginBreach".to_string(),
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

// --------------------------------------------------------------------------- //
// Hot-Swap demotion liquidation-timeout types (ERR-7, SRS-RESV-004)
// (SyRS SYS-49b / SYS-49c; StRS SN-1.25)
// --------------------------------------------------------------------------- //
//
// SRS-RESV-004 requires Hot-Swap demotion to run before promotion: the
// current live strategy stops new signals, cancels resting IB orders,
// submits liquidation orders, and waits for flat confirmation OR a
// configured timeout (default 60 seconds). On flat-before-timeout the swap
// proceeds to paper normally; ON TIMEOUT the swap enters demotion-pending
// state, dashboard/email/SMS notifications are sent, the unfilled
// liquidation order is canceled, and promotion is blocked until manual
// resolution.
//
// This SDK surface models only the timeout *decision point* — a binary
// outcome mirroring `LaunchReadiness` (the unbuilt 60 s async wait loop,
// the real IB cancel, and the real email/SMS transport are the deferred
// runtime, enumerated in `architecture/runtime_services.json`
// `hot_swap_demotion_contract.deferred[]`). The orchestrator's
// `resolve_demotion` gate matches on `HotSwapDemotionOutcome` and triggers
// the demotion-pending side effects (cancel + alert + promotion-block)
// ONLY on the `TimedOutDemotionPending` branch.
//
// `HotSwapDemotionRequest` is the source-neutral demotion envelope: it
// names the demoting (currently-live) strategy and the candidate awaiting
// promotion, plus the configured timeout. It deliberately omits every
// broker / IB-order / vendor / container identifier — the unfilled-order
// cancel flows through the `UnfilledOrderCanceller` port, not a field on
// the envelope.
//
// `OperatorAlertChannel` types SRS-RESV-004's dashboard/email/SMS triad;
// `OperatorAlertEvent` is the structured notification payload carrying all
// three channels so the contract can assert the full fan-out was
// requested. `HotSwapDemotionEvent` is the structured state-transition
// record for the dashboard/log fan-out, carrying the outcome and the
// `promotion_blocked` flag.
//
// `StructuredHotSwapDemotionError` is the rejection envelope. It reuses
// `OrderErrorCategory::HotSwapDemotionTimeout` as the single source of
// truth for the SyRS SYS-64 wire string. The envelope is distinct from the
// order / subscription / ingestion / pacing / orchestrator-launch errors
// because a Hot-Swap demotion is none of those — synthesising one would
// lie to downstream consumers.

/// SRS-RESV-004 default Hot-Swap demotion liquidation timeout (seconds).
/// Carried explicitly on every request/event so a re-tuned timeout cannot
/// drift away from the payload the dashboard renders.
pub const HOT_SWAP_DEMOTION_TIMEOUT_SECONDS: u64 = 60;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum HotSwapDemotionOutcome {
    FlatBeforeTimeout { elapsed_seconds: u64 },
    TimedOutDemotionPending { elapsed_seconds: u64, timeout_seconds: u64 },
}

impl HotSwapDemotionOutcome {
    pub const fn is_demotion_pending(self) -> bool {
        matches!(self, Self::TimedOutDemotionPending { .. })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotSwapDemotionRequest {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    pub timeout_seconds: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum OperatorAlertChannel {
    Dashboard,
    Email,
    Sms,
}

impl OperatorAlertChannel {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Dashboard => "DASHBOARD",
            Self::Email => "EMAIL",
            Self::Sms => "SMS",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorAlertEvent {
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    pub channels: Vec<OperatorAlertChannel>,
    pub elapsed_seconds: u64,
    pub timeout_seconds: u64,
    pub observed_at_seconds: u64,
}

/// Observable outcome of a timeout-branch side effect (the unfilled-order
/// cancel and the operator-alert dispatch). Per SRS-RESV-004 the cancel
/// routes to the IB adapter and the alert to the email/SMS transport —
/// both are fallible IO in the deferred runtime, so the gate records the
/// outcome on `HotSwapDemotionEvent` rather than treating the side effect
/// as infallible: a failed cancel could otherwise leave a live liquidation
/// order, and a missed alert could leave the operator unpaged, each
/// indistinguishable from success. `NotAttempted` is the flat-branch value
/// (no cancel / no alert is required when the demotion reaches flat in
/// time). The typed CONNECTIVITY_BLOCKED / STALE_DATA_BLOCKED / timeout
/// failure taxonomy is the deferred runtime's concern; this enum records
/// only whether the side effect was not attempted, succeeded, or failed
/// (carrying the reason so the failure is observable end to end).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SideEffectOutcome {
    NotAttempted,
    Succeeded,
    Failed { reason: String },
}

impl SideEffectOutcome {
    pub fn is_failed(&self) -> bool {
        matches!(self, Self::Failed { .. })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HotSwapDemotionEvent {
    pub outcome: HotSwapDemotionOutcome,
    pub demoting_strategy_id: StrategyId,
    pub candidate_strategy_id: StrategyId,
    pub promotion_blocked: bool,
    /// SRS-RESV-004 observability: the outcome of the unfilled-liquidation-
    /// order cancel on the timeout branch (`NotAttempted` on the flat
    /// branch). A `Failed` value means a live order may remain — the
    /// dashboard / log surface must not read a timeout demotion as "cleanly
    /// liquidated" without inspecting this field.
    pub liquidation_cancel: SideEffectOutcome,
    /// SRS-RESV-004 observability: the outcome of the dashboard/email/SMS
    /// operator-alert dispatch on the timeout branch (`NotAttempted` on the
    /// flat branch). A `Failed` value means the operator may not have been
    /// paged.
    pub operator_alert: SideEffectOutcome,
    pub observed_at_seconds: u64,
}

/// SRS-RESV-004 / SyRS SYS-49b / SYS-49c structured rejection envelope.
/// Carries the SyRS SYS-64 error category, the discriminator string, a
/// human-readable message, and the unchanged original demotion request.
/// The category is constrained at construction to
/// `OrderErrorCategory::HotSwapDemotionTimeout`; the factory enforces that
/// invariant in debug builds so a future caller cannot smuggle a different
/// category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredHotSwapDemotionError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_request: HotSwapDemotionRequest,
}

impl StructuredHotSwapDemotionError {
    /// Build a `HOT_SWAP_DEMOTION_TIMEOUT` rejection. `elapsed_seconds` is
    /// the time the liquidation probe reported before the deadline fired;
    /// `timeout_seconds` is the configured timeout that was breached
    /// (defaults to `HOT_SWAP_DEMOTION_TIMEOUT_SECONDS` but is carried
    /// explicitly so a re-tuned timeout cannot drift away from the payload).
    pub fn demotion_timeout(
        request: HotSwapDemotionRequest,
        elapsed_seconds: u64,
        timeout_seconds: u64,
    ) -> Self {
        let category = OrderErrorCategory::HotSwapDemotionTimeout;
        debug_assert!(
            matches!(category, OrderErrorCategory::HotSwapDemotionTimeout),
            "StructuredHotSwapDemotionError must carry HotSwapDemotionTimeout"
        );
        let message = format!(
            "SRS-RESV-004 + SyRS SYS-49b / SYS-49c: hot-swap demotion of strategy \
             {demoting} (candidate {candidate}) liquidation timed out — {elapsed} s \
             elapsed, {timeout} s permitted; entering demotion-pending, promotion blocked",
            demoting = request.demoting_strategy_id.as_str(),
            candidate = request.candidate_strategy_id.as_str(),
            elapsed = elapsed_seconds,
            timeout = timeout_seconds,
        );
        Self {
            category,
            error_type: "HotSwapDemotionTimeout".to_string(),
            message,
            original_request: request,
        }
    }
}

impl fmt::Display for StructuredHotSwapDemotionError {
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

impl std::error::Error for StructuredHotSwapDemotionError {}

// --------------------------------------------------------------------------- //
// Kill-switch liquidation-timeout domain types (ERR-8, SRS-SAFE-002)
// (SyRS SYS-44b; StRS SN-1.11)
// --------------------------------------------------------------------------- //
//
// SRS-SAFE-001 (SyRS SYS-44a) defines the kill switch's QuantConnect
// Liquidate sequence: cancel all resting IB orders for the live strategy,
// submit market liquidation orders to flatten every open live-strategy
// position, halt the paper simulation engines, and disconnect from IB.
// SRS-SAFE-002 (SyRS SYS-44b) is the error-path companion: if any liquidation
// order is still unfilled after the configured timeout (default 30 s), the
// system LOGS the unfilled order details, NOTIFIES the operator by email AND
// SMS, CANCELS the unfilled liquidation order, and DISCONNECTS from IB; the
// operator then resolves remaining positions manually.
//
// These types model the SRS-SAFE-002 timeout *decision point* as a binary
// outcome mirroring `HotSwapDemotionOutcome` (ERR-7). The 30 s async wait
// loop, the real SRS-SAFE-001 liquidate sequence, the real IB cancel /
// disconnect (SRS-EXE-006 adapter), and the real email/SMS transport
// (SRS-NOTIF-001) are the deferred runtime, enumerated in
// `architecture/runtime_services.json` `kill_switch_timeout_contract
// .deferred[]`. The execution engine's `resolve_kill_switch_timeout` gate
// (in `atp-execution`, which owns kill-switch behavior per SRS-ARCH-001)
// matches on `KillSwitchLiquidationOutcome` and fires the SRS-SAFE-002 side
// effects ONLY on the `TimedOutUnfilled` branch.
//
// `KillSwitchTimeoutRequest` is the source-neutral envelope: it names the
// live strategy and carries the unfilled order's DOMAIN details (via
// `UnfilledLiquidationOrder`) so "log the unfilled order details" is
// satisfiable without leaking vendor IB-order identifiers — the real cancel /
// disconnect flow through ports, never fields on the envelope.
//
// `OperatorAlertChannel` and `SideEffectOutcome` are REUSED from the ERR-7
// seam above (the shared notification-channel and side-effect-observability
// vocabulary). The alert and audit *payloads* are kill-switch specific
// (`KillSwitchAlertEvent` / `KillSwitchTimeoutEvent`): unlike a Hot-Swap
// demotion there is no demoting/candidate pair, so reusing `OperatorAlertEvent`
// would force meaningless Hot-Swap fields onto the kill-switch path.

/// SRS-SAFE-002 / SyRS SYS-44b default kill-switch liquidation timeout
/// (seconds). Carried explicitly on every request/event so a re-tuned timeout
/// cannot drift away from the payload the dashboard renders.
pub const KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS: u64 = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum KillSwitchLiquidationOutcome {
    FilledBeforeTimeout { elapsed_seconds: u64 },
    TimedOutUnfilled { elapsed_seconds: u64, timeout_seconds: u64 },
}

impl KillSwitchLiquidationOutcome {
    pub const fn is_timed_out(self) -> bool {
        matches!(self, Self::TimedOutUnfilled { .. })
    }
}

/// SRS-SAFE-002 / SyRS SYS-44b unfilled liquidation order details — the
/// payload "the unfilled order details are logged" requires. Carries only
/// DOMAIN identifiers (a domain `order_id`, the symbol, the closing side, and
/// the still-open quantity); the vendor IB order id stays behind the cancel
/// port (the contract forbids `ib_order_id` here).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnfilledLiquidationOrder {
    pub order_id: String,
    pub symbol: String,
    pub side: String,
    pub quantity: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchTimeoutRequest {
    pub live_strategy_id: StrategyId,
    pub unfilled_order: UnfilledLiquidationOrder,
    pub timeout_seconds: u64,
}

/// SRS-SAFE-002 operator-alert payload for the kill-switch timeout. Reuses
/// `OperatorAlertChannel` (SYS-44b fans the page to email AND SMS) but carries
/// the live strategy + unfilled-order details rather than the Hot-Swap
/// demoting/candidate pair.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchAlertEvent {
    pub live_strategy_id: StrategyId,
    pub unfilled_order: UnfilledLiquidationOrder,
    pub channels: Vec<OperatorAlertChannel>,
    pub elapsed_seconds: u64,
    pub timeout_seconds: u64,
    pub observed_at_seconds: u64,
}

/// SRS-SAFE-002 structured state-transition record for the dashboard / log
/// fan-out (the deferred SRS-LOG-001 / SRS-UI-001 consumers). Carries the
/// outcome, the logged unfilled-order details, the SYS-44b
/// `manual_resolution_required` decision flag, and the observable outcome of
/// each timeout-branch side effect (alert / cancel / disconnect) so a failed
/// cancel, missed page, or failed disconnect is distinguishable from success.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchTimeoutEvent {
    pub outcome: KillSwitchLiquidationOutcome,
    pub live_strategy_id: StrategyId,
    pub unfilled_order: UnfilledLiquidationOrder,
    /// SYS-44b: a timeout leaves positions open for the operator to resolve
    /// manually (`true` on the timeout branch, `false` when liquidation filled
    /// in time). Mirrors ERR-7's `promotion_blocked` decision flag so the
    /// dashboard renders the safety posture without re-matching the outcome.
    pub manual_resolution_required: bool,
    /// SRS-SAFE-002 observability: outcome of the email/SMS operator-alert
    /// dispatch (`NotAttempted` on the filled branch).
    pub operator_alert: SideEffectOutcome,
    /// SRS-SAFE-002 observability: outcome of the unfilled-liquidation-order
    /// cancel (`NotAttempted` on the filled branch). A `Failed` value means a
    /// live order may remain.
    pub liquidation_cancel: SideEffectOutcome,
    /// SRS-SAFE-002 observability: outcome of the IB-disconnect
    /// (`NotAttempted` on the filled branch). A `Failed` value means IB may
    /// still be connected after a timed-out liquidation.
    pub ib_disconnect: SideEffectOutcome,
    pub observed_at_seconds: u64,
}

/// SRS-SAFE-002 / SyRS SYS-44b recovery-critical record of each cleanup side
/// effect, carried ON the rejection error so the outcomes survive even when the
/// best-effort `KillSwitchTimeoutEvent` audit emission fails. The audit event is
/// the durable record; this in-error copy is the fallback so an operator /
/// REST consumer reading only the structured error still sees whether the
/// unfilled-order cancel and the IB disconnect actually succeeded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KillSwitchCleanupOutcome {
    pub operator_alert: SideEffectOutcome,
    pub liquidation_cancel: SideEffectOutcome,
    pub ib_disconnect: SideEffectOutcome,
    /// `false` if the best-effort `KillSwitchTimeoutEvent` emission failed — the
    /// durable audit record may be missing, so these in-error outcomes are then
    /// the only surviving recovery-critical facts.
    pub audit_recorded: bool,
}

impl KillSwitchCleanupOutcome {
    /// The "no cleanup attempted" value — used on the probe-unavailable path,
    /// where the gate takes no automated order/session action.
    pub fn not_attempted() -> Self {
        Self {
            operator_alert: SideEffectOutcome::NotAttempted,
            liquidation_cancel: SideEffectOutcome::NotAttempted,
            ib_disconnect: SideEffectOutcome::NotAttempted,
            audit_recorded: false,
        }
    }
}

/// SRS-SAFE-002 / SyRS SYS-44b structured rejection envelope. Carries the
/// SyRS SYS-64 error category, the discriminator string, a human-readable
/// message, the unchanged original request, and the per-side-effect cleanup
/// outcomes (so the recovery-critical facts survive a failed audit emission).
/// The category is constrained at construction to a kill-switch-timeout-family
/// variant; each factory enforces its invariant in debug builds so a future
/// caller cannot smuggle a different category through this envelope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StructuredKillSwitchTimeoutError {
    pub category: OrderErrorCategory,
    pub error_type: String,
    pub message: String,
    pub original_request: KillSwitchTimeoutRequest,
    pub cleanup: KillSwitchCleanupOutcome,
}

impl StructuredKillSwitchTimeoutError {
    /// Build a `KILL_SWITCH_LIQUIDATION_TIMEOUT` rejection. `elapsed_seconds`
    /// is the time the liquidation probe reported before the deadline fired;
    /// `timeout_seconds` is the configured timeout that was breached (defaults
    /// to `KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS` but is carried explicitly
    /// so a re-tuned timeout cannot drift away from the payload).
    pub fn liquidation_timeout(
        request: KillSwitchTimeoutRequest,
        elapsed_seconds: u64,
        timeout_seconds: u64,
        cleanup: KillSwitchCleanupOutcome,
    ) -> Self {
        let category = OrderErrorCategory::KillSwitchLiquidationTimeout;
        debug_assert!(
            matches!(category, OrderErrorCategory::KillSwitchLiquidationTimeout),
            "StructuredKillSwitchTimeoutError must carry KillSwitchLiquidationTimeout"
        );
        // Describe the SYS-44b cleanup as ATTEMPTED, not succeeded: the cancel /
        // disconnect / page ports can fail. The per-side-effect outcome is
        // carried on `self.cleanup` (and, when the audit sink accepted it, on
        // the durable `KillSwitchTimeoutEvent`). This envelope must not claim
        // the order was canceled or IB disconnected when those side effects
        // returned `Failed`.
        let message = format!(
            "SRS-SAFE-002 + SyRS SYS-44b: kill-switch liquidation order {order} \
             ({side} {quantity} {symbol}) for live strategy {strategy} stayed \
             unfilled — {elapsed} s elapsed, {timeout} s permitted; the SYS-44b \
             cleanup was attempted (operator page over email + SMS, \
             unfilled-order cancel, and IB disconnect dispatched — see this \
             error's `cleanup` outcomes, also mirrored on the kill-switch \
             timeout event when the audit sink accepted it); positions await \
             manual resolution",
            order = request.unfilled_order.order_id,
            side = request.unfilled_order.side,
            quantity = request.unfilled_order.quantity,
            symbol = request.unfilled_order.symbol,
            strategy = request.live_strategy_id.as_str(),
            elapsed = elapsed_seconds,
            timeout = timeout_seconds,
        );
        Self {
            category,
            error_type: "KillSwitchLiquidationTimeout".to_string(),
            message,
            original_request: request,
            cleanup,
        }
    }

    /// Build a `KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE` rejection for the
    /// case where the fill-confirmation probe could not determine whether the
    /// liquidation filled (connectivity loss, order state unavailable, probe
    /// timeout). This is a DISTINCT category from a confirmed timeout — it must
    /// never be mislabelled as `KillSwitchLiquidationTimeout` — and the gate
    /// takes NO automated cancel/disconnect on the unconfirmable order state
    /// (see `ExecutionEngine::resolve_kill_switch_timeout`).
    pub fn probe_unavailable(request: KillSwitchTimeoutRequest, reason: impl Into<String>) -> Self {
        let category = OrderErrorCategory::KillSwitchLiquidationProbeUnavailable;
        debug_assert!(
            matches!(
                category,
                OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
            ),
            "probe_unavailable must carry KillSwitchLiquidationProbeUnavailable"
        );
        let message = format!(
            "SRS-SAFE-002 + SyRS SYS-44b: kill-switch liquidation fill \
             confirmation unavailable for order {order} ({side} {quantity} \
             {symbol}), live strategy {strategy} (probe error: {reason}); no \
             automated cancel/disconnect taken on the unconfirmable order \
             state; positions await manual resolution",
            order = request.unfilled_order.order_id,
            side = request.unfilled_order.side,
            quantity = request.unfilled_order.quantity,
            symbol = request.unfilled_order.symbol,
            strategy = request.live_strategy_id.as_str(),
            reason = reason.into(),
        );
        Self {
            category,
            error_type: "KillSwitchLiquidationProbeUnavailable".to_string(),
            message,
            original_request: request,
            // No automated action was taken on the unconfirmable state.
            cleanup: KillSwitchCleanupOutcome::not_attempted(),
        }
    }
}

impl fmt::Display for StructuredKillSwitchTimeoutError {
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

impl std::error::Error for StructuredKillSwitchTimeoutError {}

#[cfg(test)]
mod tests {
    use super::*;

    /// Test fixture: a valid 64-hex SHA-256 wire-form source hash for
    /// strategy "alpha" — 64 lower-case `a` characters after the
    /// `sha256:` prefix. Chosen so test fixtures stay readable while
    /// satisfying the SRS-ORCH-004 wire-form validator.
    const SAMPLE_SOURCE_HASH_ALPHA: &str =
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    /// Test fixture: a second valid 64-hex SHA-256 wire-form hash for
    /// fixtures that need a distinct value from `SAMPLE_SOURCE_HASH_ALPHA`.
    const SAMPLE_SOURCE_HASH_BETA: &str =
        "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

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
        assert_eq!(
            OrderErrorCategory::HotSwapDemotionTimeout.as_str(),
            "HOT_SWAP_DEMOTION_TIMEOUT"
        );
        assert_eq!(
            OrderErrorCategory::KillSwitchLiquidationTimeout.as_str(),
            "KILL_SWITCH_LIQUIDATION_TIMEOUT"
        );
        assert_eq!(
            OrderErrorCategory::KillSwitchLiquidationProbeUnavailable.as_str(),
            "KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE"
        );
    }

    #[test]
    fn hot_swap_demotion_outcome_distinguishes_flat_from_timeout() {
        // SRS-RESV-004: only the timeout branch enters demotion-pending.
        let flat = HotSwapDemotionOutcome::FlatBeforeTimeout {
            elapsed_seconds: 12,
        };
        let timed_out = HotSwapDemotionOutcome::TimedOutDemotionPending {
            elapsed_seconds: 60,
            timeout_seconds: HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
        };
        assert!(!flat.is_demotion_pending());
        assert!(timed_out.is_demotion_pending());
        assert_eq!(HOT_SWAP_DEMOTION_TIMEOUT_SECONDS, 60);
    }

    #[test]
    fn operator_alert_channel_wire_strings_cover_the_resv_004_triad() {
        // SRS-RESV-004: notifications are sent over dashboard, email, and SMS.
        assert_eq!(OperatorAlertChannel::Dashboard.as_str(), "DASHBOARD");
        assert_eq!(OperatorAlertChannel::Email.as_str(), "EMAIL");
        assert_eq!(OperatorAlertChannel::Sms.as_str(), "SMS");
    }

    #[test]
    fn structured_hot_swap_demotion_error_pins_category_and_traces_srs() {
        let request = HotSwapDemotionRequest {
            demoting_strategy_id: StrategyId::new("live-momentum"),
            candidate_strategy_id: StrategyId::new("paper-reversal"),
            timeout_seconds: HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
        };
        let error = StructuredHotSwapDemotionError::demotion_timeout(
            request.clone(),
            72,
            HOT_SWAP_DEMOTION_TIMEOUT_SECONDS,
        );
        assert_eq!(error.category, OrderErrorCategory::HotSwapDemotionTimeout);
        assert_eq!(error.category.as_str(), "HOT_SWAP_DEMOTION_TIMEOUT");
        assert_eq!(error.error_type, "HotSwapDemotionTimeout");
        assert_eq!(error.original_request, request);
        assert!(error.message.contains("SRS-RESV-004"));
        assert!(error.message.contains("SYS-49b"));
        assert!(error.message.contains("SYS-49c"));
        assert!(error.message.contains("live-momentum"));
        assert!(error.message.contains("paper-reversal"));
        assert!(error.message.contains("72"));
        assert!(error.message.contains("60"));
        // Display renders the SyRS SYS-64 wire string in the bracket prefix.
        assert!(error.to_string().starts_with("[HOT_SWAP_DEMOTION_TIMEOUT]"));
    }

    #[test]
    fn side_effect_outcome_makes_a_failed_cancel_observable() {
        // SRS-RESV-004: a failed timeout-branch side effect must be
        // distinguishable from success on the demotion event.
        assert!(!SideEffectOutcome::NotAttempted.is_failed());
        assert!(!SideEffectOutcome::Succeeded.is_failed());
        let failed = SideEffectOutcome::Failed {
            reason: "IB cancel timed out".to_string(),
        };
        assert!(failed.is_failed());
        assert_ne!(failed, SideEffectOutcome::Succeeded);
    }

    #[test]
    fn kill_switch_liquidation_outcome_distinguishes_filled_from_timeout() {
        // SRS-SAFE-002: only the timeout branch fires the SYS-44b side effects.
        let filled = KillSwitchLiquidationOutcome::FilledBeforeTimeout {
            elapsed_seconds: 8,
        };
        let timed_out = KillSwitchLiquidationOutcome::TimedOutUnfilled {
            elapsed_seconds: 30,
            timeout_seconds: KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
        };
        assert!(!filled.is_timed_out());
        assert!(timed_out.is_timed_out());
        assert_eq!(KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS, 30);
    }

    #[test]
    fn structured_kill_switch_timeout_error_pins_category_and_traces_srs() {
        let request = KillSwitchTimeoutRequest {
            live_strategy_id: StrategyId::new("live-momentum"),
            unfilled_order: UnfilledLiquidationOrder {
                order_id: "ord-7791".to_string(),
                symbol: "AAPL".to_string(),
                side: "SELL".to_string(),
                quantity: 250,
            },
            timeout_seconds: KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
        };
        let error = StructuredKillSwitchTimeoutError::liquidation_timeout(
            request.clone(),
            41,
            KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
            KillSwitchCleanupOutcome {
                operator_alert: SideEffectOutcome::Succeeded,
                liquidation_cancel: SideEffectOutcome::Failed {
                    reason: "IB cancel_order unreachable".to_string(),
                },
                ib_disconnect: SideEffectOutcome::Succeeded,
                audit_recorded: false,
            },
        );
        assert_eq!(
            error.category,
            OrderErrorCategory::KillSwitchLiquidationTimeout
        );
        assert_eq!(error.category.as_str(), "KILL_SWITCH_LIQUIDATION_TIMEOUT");
        assert_eq!(error.error_type, "KillSwitchLiquidationTimeout");
        assert_eq!(error.original_request, request);
        assert!(error.message.contains("SRS-SAFE-002"));
        assert!(error.message.contains("SYS-44b"));
        assert!(error.message.contains("live-momentum"));
        assert!(error.message.contains("ord-7791"));
        assert!(error.message.contains("AAPL"));
        assert!(error.message.contains("41"));
        assert!(error.message.contains("30"));
        // The message describes the cleanup as ATTEMPTED, not succeeded — it
        // must not claim the order was canceled / IB disconnected.
        assert!(error.message.contains("attempted"));
        assert!(!error.message.contains("order canceled"));
        // The per-side-effect outcomes are carried ON the error so they survive
        // even when the audit sink failed (audit_recorded == false here).
        assert!(error.cleanup.liquidation_cancel.is_failed());
        assert_eq!(error.cleanup.ib_disconnect, SideEffectOutcome::Succeeded);
        assert!(!error.cleanup.audit_recorded);
        // Display renders the SyRS SYS-64 wire string in the bracket prefix.
        assert!(error
            .to_string()
            .starts_with("[KILL_SWITCH_LIQUIDATION_TIMEOUT]"));
    }

    #[test]
    fn structured_kill_switch_probe_unavailable_is_a_distinct_category() {
        // finding-2: a fill-confirmation probe failure must NOT be mislabelled
        // as a confirmed timeout — it carries its own category and never claims
        // any automated cleanup ran.
        let request = KillSwitchTimeoutRequest {
            live_strategy_id: StrategyId::new("live-momentum"),
            unfilled_order: UnfilledLiquidationOrder {
                order_id: "ord-7791".to_string(),
                symbol: "AAPL".to_string(),
                side: "SELL".to_string(),
                quantity: 250,
            },
            timeout_seconds: KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
        };
        let error = StructuredKillSwitchTimeoutError::probe_unavailable(
            request.clone(),
            "IB fill-confirmation stream lost",
        );
        assert_eq!(
            error.category,
            OrderErrorCategory::KillSwitchLiquidationProbeUnavailable
        );
        assert_ne!(
            error.category,
            OrderErrorCategory::KillSwitchLiquidationTimeout
        );
        assert_eq!(error.error_type, "KillSwitchLiquidationProbeUnavailable");
        assert_eq!(error.original_request, request);
        assert!(error.message.contains("IB fill-confirmation stream lost"));
        assert!(error.message.contains("no automated cancel/disconnect"));
        assert!(error
            .to_string()
            .starts_with("[KILL_SWITCH_LIQUIDATION_PROBE_UNAVAILABLE]"));
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
            asset_class: AssetClass::Equity,
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
    fn market_data_tick_carries_only_symbol_asset_class_and_seq() {
        // SRS-MD-001 fan-out payload: routing symbol + asset class + opaque
        // delivery counter, nothing else. The exhaustive destructure proves
        // no broker / vendor / session field can ride along on the fan-out.
        let tick = MarketDataTick {
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
            tick_seq: 7,
        };
        let MarketDataTick {
            symbol: _,
            asset_class: _,
            tick_seq: _,
        } = tick.clone();
        assert_eq!(tick.symbol, "AAPL");
        assert_eq!(tick.asset_class, AssetClass::Equity);
        assert_eq!(tick.tick_seq, 7);
        assert_eq!(
            tick.security_key(),
            SecurityKey::new("AAPL", AssetClass::Equity)
        );
    }

    #[test]
    fn security_key_normalizes_symbol_and_fails_closed() {
        // Case + whitespace variants resolve to ONE equity security ...
        let a = SecurityKey::new("AAPL", AssetClass::Equity).unwrap();
        let b = SecurityKey::new("  aapl ", AssetClass::Equity).unwrap();
        assert_eq!(a, b, "case/whitespace variants must canonicalize equal");
        assert_eq!(a.symbol(), "AAPL");
        assert_eq!(a.asset_class(), AssetClass::Equity);
        // ... an empty / whitespace symbol cannot name a security ...
        assert_eq!(
            SecurityKey::new("", AssetClass::Equity),
            Err(SecurityKeyError::EmptySymbol)
        );
        assert_eq!(
            SecurityKey::new("   ", AssetClass::Equity),
            Err(SecurityKeyError::EmptySymbol)
        );
        // ... and options fail closed: a real option contract needs
        // underlying + expiration + strike + right, which is deferred to
        // SRS-DATA-004 / SRS-EXE-004. Keying by underlying alone would
        // conflate distinct contracts, so `new` refuses it.
        assert_eq!(
            SecurityKey::new("AAPL", AssetClass::Option),
            Err(SecurityKeyError::OptionContractIdentityRequired)
        );
    }

    #[test]
    fn asset_class_wire_strings_are_screaming_snake() {
        assert_eq!(AssetClass::Equity.as_str(), "EQUITY");
        assert_eq!(AssetClass::Option.as_str(), "OPTION");
        assert_eq!(AssetClass::default(), AssetClass::Equity);
    }

    #[test]
    fn subscription_change_line_count_predicate_is_open_close_only() {
        // SRS-MD-001 consolidation property: only Opened/Closed move the
        // distinct upstream subscription count; dedup transitions and
        // no-ops never do.
        assert!(SubscriptionChange::Opened.changes_line_count());
        assert!(SubscriptionChange::Closed.changes_line_count());
        assert!(!SubscriptionChange::SubscriberAdded.changes_line_count());
        assert!(!SubscriptionChange::SubscriberRemoved.changes_line_count());
        assert!(!SubscriptionChange::AlreadySubscribed.changes_line_count());
        assert!(!SubscriptionChange::NotSubscribed.changes_line_count());
    }

    #[test]
    fn subscription_change_publishes_everything_but_the_idempotent_noops() {
        // Only the idempotent no-ops are suppressed from the
        // SRS-LOG-001 subscription_change stream.
        assert!(SubscriptionChange::Opened.is_published());
        assert!(SubscriptionChange::SubscriberAdded.is_published());
        assert!(SubscriptionChange::SubscriberRemoved.is_published());
        assert!(SubscriptionChange::Closed.is_published());
        assert!(!SubscriptionChange::AlreadySubscribed.is_published());
        assert!(!SubscriptionChange::NotSubscribed.is_published());
    }

    #[test]
    fn subscription_change_wire_strings_are_screaming_snake() {
        assert_eq!(SubscriptionChange::Opened.as_str(), "OPENED");
        assert_eq!(
            SubscriptionChange::SubscriberAdded.as_str(),
            "SUBSCRIBER_ADDED"
        );
        assert_eq!(
            SubscriptionChange::AlreadySubscribed.as_str(),
            "ALREADY_SUBSCRIBED"
        );
        assert_eq!(
            SubscriptionChange::SubscriberRemoved.as_str(),
            "SUBSCRIBER_REMOVED"
        );
        assert_eq!(SubscriptionChange::Closed.as_str(), "CLOSED");
        assert_eq!(SubscriptionChange::NotSubscribed.as_str(), "NOT_SUBSCRIBED");
    }

    #[test]
    fn subscription_change_event_carries_only_the_six_required_fields() {
        // Exhaustive destructure pins the field set: change discriminant,
        // strategy_id, symbol, asset_class (so the line is unambiguous when
        // securities share a ticker), and the post-transition
        // subscriber_count + lines_in_use the dashboard / log consumer reads
        // without re-probing the registry.
        let event = SubscriptionChangeEvent {
            change: SubscriptionChange::SubscriberAdded,
            strategy_id: StrategyId::new("paper-bravo-2"),
            symbol: "AAPL".to_string(),
            asset_class: AssetClass::Equity,
            subscriber_count: 3,
            lines_in_use: 1,
        };
        let SubscriptionChangeEvent {
            change: _,
            strategy_id: _,
            symbol: _,
            asset_class: _,
            subscriber_count: _,
            lines_in_use: _,
        } = event.clone();
        assert_eq!(event.change, SubscriptionChange::SubscriberAdded);
        assert_eq!(event.strategy_id.as_str(), "paper-bravo-2");
        assert_eq!(event.symbol, "AAPL");
        assert_eq!(event.asset_class, AssetClass::Equity);
        assert_eq!(event.subscriber_count, 3);
        // Dedup: three subscribers, still exactly one upstream line.
        assert_eq!(event.lines_in_use, 1);
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
    fn strategy_launch_request_carries_only_the_five_required_fields() {
        // The exhaustive destructure proves there are no other public
        // fields. AC-12 + NFR-S5 require the launch envelope to be
        // free of container-runtime bleed (docker_image, container_id,
        // host_path, vendor) so the orchestrator stays free of
        // Docker-Engine-specific shape. The `profile` field carries the
        // SRS-ORCH-002 / SyRS SYS-11 resource limits.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::live_default(),
        };
        let StrategyLaunchRequest {
            strategy_id: _,
            mode: _,
            deployment_hash: _,
            deadline_millis: _,
            profile: _,
        } = request.clone();
        assert_eq!(request.strategy_id.as_str(), "alpha-1");
        assert_eq!(request.mode, StrategyMode::Live);
        assert_eq!(request.deployment_hash.as_str(), SAMPLE_SOURCE_HASH_ALPHA);
        assert_eq!(request.deadline_millis, 30_000);
        assert_eq!(request.profile, ResourceProfile::live_default());
    }

    #[test]
    fn strategy_launch_outcome_carries_only_the_six_required_fields() {
        let outcome = StrategyLaunchOutcome {
            strategy_id: StrategyId::new("alpha-1"),
            ready_within_deadline: true,
            elapsed_millis: 4_200,
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::live_default(),
            deployed_version: DeployedVersion::new(
                SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
                1_715_000_000,
            ),
        };
        let StrategyLaunchOutcome {
            strategy_id: _,
            ready_within_deadline: _,
            elapsed_millis: _,
            deadline_millis: _,
            profile: _,
            deployed_version: _,
        } = outcome.clone();
        assert!(outcome.ready_within_deadline);
        assert_eq!(outcome.elapsed_millis, 4_200);
        assert!(outcome.elapsed_millis <= outcome.deadline_millis);
        assert_eq!(outcome.profile, ResourceProfile::live_default());
        assert_eq!(
            outcome.deployed_version.source_hash.as_str(),
            SAMPLE_SOURCE_HASH_ALPHA
        );
        assert_eq!(outcome.deployed_version.deployed_at_seconds, 1_715_000_000);
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
            deployment_hash: SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::live_default(),
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

    // ----------------------------------------------------------------------- //
    // SRS-ORCH-002 resource profile types (SyRS SYS-11 / SYS-57, NFR-SC1)
    // ----------------------------------------------------------------------- //

    #[test]
    fn resource_profile_constants_match_syrs_sys_11_defaults() {
        // SyRS SYS-11 names exact spec literals: live ≤ 512 MB / 0.25 cores;
        // paper ≤ 300 MB / 0.10 cores. The constants are the single
        // source of truth — a future tuning has exactly one site to
        // touch and the contract check pins these values.
        assert_eq!(LIVE_PROFILE_MEM_MB, 512);
        assert_eq!(LIVE_PROFILE_CPU_HUNDREDTHS, 25);
        assert_eq!(PAPER_PROFILE_MEM_MB, 300);
        assert_eq!(PAPER_PROFILE_CPU_HUNDREDTHS, 10);
    }

    #[test]
    fn resource_profile_struct_carries_only_two_fields() {
        // Exhaustive destructure proves no other public fields. The
        // contract check's `forbidden_fields` allowlist locks
        // `cpu_cores_f32`, `docker_image`, `container_id`, etc. out.
        let profile = ResourceProfile {
            mem_mb: 256,
            cpu_hundredths: 15,
        };
        let ResourceProfile {
            mem_mb: _,
            cpu_hundredths: _,
        } = profile;
        assert_eq!(profile.mem_mb, 256);
        assert_eq!(profile.cpu_hundredths, 15);
    }

    #[test]
    fn resource_profile_live_default_matches_spec_literal() {
        let profile = ResourceProfile::live_default();
        assert_eq!(profile.mem_mb, LIVE_PROFILE_MEM_MB);
        assert_eq!(profile.cpu_hundredths, LIVE_PROFILE_CPU_HUNDREDTHS);
    }

    #[test]
    fn resource_profile_paper_default_matches_spec_literal() {
        let profile = ResourceProfile::paper_default();
        assert_eq!(profile.mem_mb, PAPER_PROFILE_MEM_MB);
        assert_eq!(profile.cpu_hundredths, PAPER_PROFILE_CPU_HUNDREDTHS);
    }

    #[test]
    fn resource_profile_for_mode_dispatches_by_strategy_mode() {
        // SyRS SYS-11 binding: Live → live profile, Paper → paper profile.
        // The match is exhaustive on StrategyMode so a future variant
        // would force a compile-time fix here, not silent fall-through.
        assert_eq!(
            ResourceProfile::for_mode(StrategyMode::Live),
            ResourceProfile::live_default()
        );
        assert_eq!(
            ResourceProfile::for_mode(StrategyMode::Paper),
            ResourceProfile::paper_default()
        );
    }

    #[test]
    fn resource_profile_validate_accepts_defaults() {
        assert!(ResourceProfile::live_default().validate().is_ok());
        assert!(ResourceProfile::paper_default().validate().is_ok());
    }

    #[test]
    fn resource_profile_validate_rejects_below_floor_memory() {
        let profile = ResourceProfile {
            mem_mb: RESOURCE_PROFILE_MEM_FLOOR_MB - 1,
            cpu_hundredths: 25,
        };
        let err = profile.validate().expect_err("below-floor mem must be rejected");
        assert!(matches!(err, ResourceProfileError::MemBelowFloor { .. }));
        assert_eq!(err.as_str(), "MemBelowFloor");
    }

    #[test]
    fn resource_profile_validate_rejects_above_ceiling_memory() {
        let profile = ResourceProfile {
            mem_mb: RESOURCE_PROFILE_MEM_CEILING_MB + 1,
            cpu_hundredths: 25,
        };
        let err = profile
            .validate()
            .expect_err("above-ceiling mem must be rejected");
        assert!(matches!(err, ResourceProfileError::MemAboveCeiling { .. }));
        assert_eq!(err.as_str(), "MemAboveCeiling");
    }

    #[test]
    fn resource_profile_validate_rejects_below_floor_cpu() {
        let profile = ResourceProfile {
            mem_mb: 512,
            cpu_hundredths: RESOURCE_PROFILE_CPU_FLOOR_HUNDREDTHS - 1,
        };
        let err = profile.validate().expect_err("below-floor cpu must be rejected");
        assert!(matches!(err, ResourceProfileError::CpuBelowFloor { .. }));
        assert_eq!(err.as_str(), "CpuBelowFloor");
    }

    #[test]
    fn resource_profile_validate_rejects_above_ceiling_cpu() {
        let profile = ResourceProfile {
            mem_mb: 512,
            cpu_hundredths: RESOURCE_PROFILE_CPU_CEILING_HUNDREDTHS + 1,
        };
        let err = profile
            .validate()
            .expect_err("above-ceiling cpu must be rejected");
        assert!(matches!(err, ResourceProfileError::CpuAboveCeiling { .. }));
        assert_eq!(err.as_str(), "CpuAboveCeiling");
    }

    #[test]
    fn resource_profile_invalid_factory_pins_the_wire_string() {
        // SRS-ORCH-002 / SyRS SYS-64: the rejection wire string must
        // be RESOURCE_PROFILE_INVALID and the error_type discriminator
        // must encode the specific validation failure variant.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile {
                mem_mb: 32,
                cpu_hundredths: 25,
            },
        };
        let violation = request.profile.validate().expect_err("must be invalid");
        let error =
            StructuredOrchestratorError::resource_profile_invalid(request.clone(), violation);
        assert_eq!(error.category, OrderErrorCategory::ResourceProfileInvalid);
        assert_eq!(error.category.as_str(), "RESOURCE_PROFILE_INVALID");
        assert_eq!(error.error_type, "ResourceProfileInvalid::MemBelowFloor");
        assert!(error.message.contains("SRS-ORCH-002"));
        assert!(error.message.contains("SYS-11"));
        assert!(error.message.contains("alpha-1"));
        assert_eq!(error.original_request, request);
    }

    #[test]
    fn order_error_category_resource_profile_invalid_wire_string() {
        assert_eq!(
            OrderErrorCategory::ResourceProfileInvalid.as_str(),
            "RESOURCE_PROFILE_INVALID"
        );
    }

    // ----------------------------------------------------------------------- //
    // SRS-ORCH-003 workload priority + host memory safety margin
    // (SyRS SYS-57 / SYS-58)
    // ----------------------------------------------------------------------- //

    #[test]
    fn host_memory_safety_margin_constants_match_syrs_sys_57_default() {
        // SyRS SYS-57 names the default safety margin as 2 GB. The
        // catalogue exposes the same value (and the floor / ceiling) as
        // the single source of truth at the configuration boundary; the
        // constants here are the single source of truth at the
        // orchestrator boundary; the contract check cross-verifies.
        assert_eq!(HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT, 2_048);
        assert_eq!(HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR, 256);
        assert_eq!(HOST_MEMORY_SAFETY_MARGIN_MB_CEILING, 1_048_576);
    }

    #[test]
    fn host_memory_safety_margin_default_matches_spec_literal() {
        let margin = HostMemorySafetyMargin::default_margin();
        assert_eq!(margin.mb, HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT);
        assert!(margin.validate().is_ok());
    }

    #[test]
    fn host_memory_safety_margin_validate_rejects_below_floor() {
        let margin = HostMemorySafetyMargin {
            mb: HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR - 1,
        };
        let err = margin
            .validate()
            .expect_err("below-floor margin must be rejected");
        assert!(matches!(err, HostMemorySafetyMarginError::BelowFloor { .. }));
        assert_eq!(err.as_str(), "BelowFloor");
    }

    #[test]
    fn host_memory_safety_margin_validate_rejects_above_ceiling() {
        let margin = HostMemorySafetyMargin {
            mb: HOST_MEMORY_SAFETY_MARGIN_MB_CEILING + 1,
        };
        let err = margin
            .validate()
            .expect_err("above-ceiling margin must be rejected");
        assert!(matches!(
            err,
            HostMemorySafetyMarginError::AboveCeiling { .. }
        ));
        assert_eq!(err.as_str(), "AboveCeiling");
    }

    #[test]
    fn host_memory_safety_margin_validate_accepts_floor_and_ceiling_exactly() {
        // Boundary inclusion: exactly-floor and exactly-ceiling must
        // pass — the rejection is on strictly-below / strictly-above.
        assert!(HostMemorySafetyMargin {
            mb: HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR
        }
        .validate()
        .is_ok());
        assert!(HostMemorySafetyMargin {
            mb: HOST_MEMORY_SAFETY_MARGIN_MB_CEILING
        }
        .validate()
        .is_ok());
    }

    #[test]
    fn workload_priority_rank_orders_sys_57_hierarchy() {
        // SYS-57 hierarchy: live (1) > market data (2) > paper (3) >
        // nightly ingestion (4) > factor (5) > backtest (6) > research (7).
        assert_eq!(WorkloadPriority::LiveStrategy.rank(), 1);
        assert_eq!(WorkloadPriority::MarketDataSubscriptionManager.rank(), 2);
        assert_eq!(WorkloadPriority::PaperStrategy.rank(), 3);
        assert_eq!(WorkloadPriority::NightlyDataIngestion.rank(), 4);
        assert_eq!(WorkloadPriority::FactorPipeline.rank(), 5);
        assert_eq!(WorkloadPriority::Backtest.rank(), 6);
        assert_eq!(WorkloadPriority::Research.rank(), 7);
        // Strictly monotonic — no ties, no holes.
        assert!(
            WorkloadPriority::LiveStrategy.rank()
                < WorkloadPriority::MarketDataSubscriptionManager.rank()
        );
        assert!(WorkloadPriority::Backtest.rank() < WorkloadPriority::Research.rank());
    }

    #[test]
    fn workload_priority_default_kind_matches_sys_58_clause_b() {
        // SyRS SYS-58 (b): "terminate the lowest-priority active BATCH
        // workload" — only the bottom four priorities are batch; the
        // top three (live, market data, paper) are continuous and
        // immune from eviction.
        assert_eq!(
            WorkloadPriority::LiveStrategy.default_kind(),
            WorkloadKind::Continuous
        );
        assert_eq!(
            WorkloadPriority::MarketDataSubscriptionManager.default_kind(),
            WorkloadKind::Continuous
        );
        assert_eq!(
            WorkloadPriority::PaperStrategy.default_kind(),
            WorkloadKind::Continuous
        );
        assert_eq!(
            WorkloadPriority::NightlyDataIngestion.default_kind(),
            WorkloadKind::Batch
        );
        assert_eq!(
            WorkloadPriority::FactorPipeline.default_kind(),
            WorkloadKind::Batch
        );
        assert_eq!(
            WorkloadPriority::Backtest.default_kind(),
            WorkloadKind::Batch
        );
        assert_eq!(
            WorkloadPriority::Research.default_kind(),
            WorkloadKind::Batch
        );
    }

    #[test]
    fn workload_priority_wire_strings_are_stable() {
        assert_eq!(WorkloadPriority::LiveStrategy.as_str(), "LIVE_STRATEGY");
        assert_eq!(
            WorkloadPriority::MarketDataSubscriptionManager.as_str(),
            "MARKET_DATA_SUBSCRIPTION_MANAGER"
        );
        assert_eq!(WorkloadPriority::PaperStrategy.as_str(), "PAPER_STRATEGY");
        assert_eq!(
            WorkloadPriority::NightlyDataIngestion.as_str(),
            "NIGHTLY_DATA_INGESTION"
        );
        assert_eq!(WorkloadPriority::FactorPipeline.as_str(), "FACTOR_PIPELINE");
        assert_eq!(WorkloadPriority::Backtest.as_str(), "BACKTEST");
        assert_eq!(WorkloadPriority::Research.as_str(), "RESEARCH");
    }

    #[test]
    fn workload_kind_is_batch_distinguishes_continuous() {
        assert!(WorkloadKind::Batch.is_batch());
        assert!(!WorkloadKind::Continuous.is_batch());
    }

    #[test]
    fn workload_id_carries_its_value() {
        let id = WorkloadId::new("backtest-2026-05-14-001");
        assert_eq!(id.as_str(), "backtest-2026-05-14-001");
    }

    #[test]
    fn registered_workload_carries_only_four_fields() {
        // Exhaustive destructure proves no other public fields. The
        // contract check pins these four — id, priority, kind, profile —
        // so future drift (adding a `vendor` / `docker_image` /
        // `cgroup_path` would couple the registry projection to the
        // container runtime) is caught at the parse level.
        let workload = RegisteredWorkload {
            id: WorkloadId::new("factor-pipeline-nightly"),
            priority: WorkloadPriority::FactorPipeline,
            kind: WorkloadKind::Batch,
            profile: ResourceProfile {
                mem_mb: 1_024,
                cpu_hundredths: 100,
            },
        };
        let RegisteredWorkload {
            id: _,
            priority: _,
            kind: _,
            profile: _,
        } = workload.clone();
        assert_eq!(workload.priority, WorkloadPriority::FactorPipeline);
        assert_eq!(workload.kind, WorkloadKind::Batch);
        assert_eq!(workload.profile.mem_mb, 1_024);
    }

    #[test]
    fn workload_admission_event_variants_carry_distinct_payloads() {
        let refused = WorkloadAdmissionEvent::Refused {
            workload_id: WorkloadId::new("research-jupyter-01"),
            priority: WorkloadPriority::Research,
            reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                available_mb: 1_500,
                safety_margin_mb: 2_048,
            },
            observed_at_seconds: 1_715_700_000,
        };
        assert_eq!(refused.as_str(), "REFUSED");

        let terminated = WorkloadAdmissionEvent::Terminated {
            terminated_workload_id: WorkloadId::new("research-jupyter-01"),
            terminated_priority: WorkloadPriority::Research,
            admitted_workload_id: WorkloadId::new("backtest-priority-2"),
            admitted_priority: WorkloadPriority::Backtest,
            reason: WorkloadAdmissionReason::HostMemoryBelowSafetyMargin {
                available_mb: 1_500,
                safety_margin_mb: 2_048,
            },
            observed_at_seconds: 1_715_700_000,
        };
        assert_eq!(terminated.as_str(), "TERMINATED");
    }

    #[test]
    fn host_memory_safety_margin_breach_factory_pins_the_wire_string() {
        // SRS-ORCH-003 / SyRS SYS-64: the rejection wire string must be
        // HOST_MEMORY_SAFETY_MARGIN_BREACH and the error_type
        // discriminator must be the canonical PascalCase form. The
        // message must carry the SRS-ORCH-003 anchor and the available
        // / safety-margin numerics for dashboard rendering.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("research-jupyter-01"),
            mode: StrategyMode::Paper,
            deployment_hash: SourceHash::new(SAMPLE_SOURCE_HASH_BETA),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::paper_default(),
        };
        let error = StructuredOrchestratorError::host_memory_safety_margin_breach(
            request.clone(),
            1_500,
            2_048,
        );
        assert_eq!(
            error.category,
            OrderErrorCategory::HostMemorySafetyMarginBreach
        );
        assert_eq!(error.category.as_str(), "HOST_MEMORY_SAFETY_MARGIN_BREACH");
        assert_eq!(error.error_type, "HostMemorySafetyMarginBreach");
        assert!(error.message.contains("SRS-ORCH-003"));
        assert!(error.message.contains("SYS-57"));
        assert!(error.message.contains("research-jupyter-01"));
        assert!(error.message.contains("1500"));
        assert!(error.message.contains("2048"));
        assert_eq!(error.original_request, request);
    }

    #[test]
    fn order_error_category_host_memory_safety_margin_breach_wire_string() {
        assert_eq!(
            OrderErrorCategory::HostMemorySafetyMarginBreach.as_str(),
            "HOST_MEMORY_SAFETY_MARGIN_BREACH"
        );
    }

    // ----------------------------------------------------------------------- //
    // SRS-ORCH-004 source-hash + deployed-version (SyRS SYS-79)
    // ----------------------------------------------------------------------- //

    #[test]
    fn source_hash_constants_match_sha256_wire_form() {
        // SyRS SYS-79 names "a hash of the strategy source file(s)"
        // without prescribing the algorithm; SHA-256 is the chosen
        // wire form (32 bytes = 64 hex chars + `sha256:` prefix = 71
        // chars). Pin the constants so a future drift to MD5 / SHA-1
        // requires touching these literals AND the catalogue.
        assert_eq!(SOURCE_HASH_ALGORITHM_PREFIX, "sha256:");
        assert_eq!(SOURCE_HASH_DIGEST_HEX_LENGTH, 64);
        assert_eq!(SOURCE_HASH_TOTAL_LENGTH, 71);
    }

    #[test]
    fn source_hash_validate_accepts_canonical_wire_form() {
        let hash = SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA);
        assert!(hash.validate().is_ok());
        assert_eq!(hash.as_str(), SAMPLE_SOURCE_HASH_ALPHA);
        assert_eq!(hash.algorithm(), "sha256");
        assert_eq!(hash.digest().len(), SOURCE_HASH_DIGEST_HEX_LENGTH);
    }

    #[test]
    fn source_hash_validate_rejects_missing_prefix() {
        let hash = SourceHash::new(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        );
        let err = hash
            .validate()
            .expect_err("missing prefix must be rejected");
        assert!(matches!(err, SourceHashError::MissingAlgorithmPrefix));
        assert_eq!(err.as_str(), "MissingAlgorithmPrefix");
    }

    #[test]
    fn source_hash_validate_rejects_unknown_algorithm() {
        let hash = SourceHash::new(
            "md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        );
        let err = hash
            .validate()
            .expect_err("unknown algorithm must be rejected");
        assert!(matches!(
            err,
            SourceHashError::UnknownAlgorithm { ref found } if found == "md5"
        ));
        assert_eq!(err.as_str(), "UnknownAlgorithm");
    }

    #[test]
    fn source_hash_validate_rejects_short_digest() {
        let hash = SourceHash::new("sha256:abc");
        let err = hash
            .validate()
            .expect_err("short digest must be rejected");
        assert!(matches!(
            err,
            SourceHashError::InvalidDigestLength {
                found: 3,
                expected: 64
            }
        ));
        assert_eq!(err.as_str(), "InvalidDigestLength");
    }

    #[test]
    fn source_hash_validate_rejects_long_digest() {
        // 65 hex chars after the prefix → off-by-one, must be rejected.
        let hash = SourceHash::new(format!(
            "sha256:{}",
            "a".repeat(SOURCE_HASH_DIGEST_HEX_LENGTH + 1)
        ));
        let err = hash
            .validate()
            .expect_err("long digest must be rejected");
        assert!(matches!(
            err,
            SourceHashError::InvalidDigestLength {
                found: 65,
                expected: 64
            }
        ));
    }

    #[test]
    fn source_hash_validate_rejects_non_hex_digest() {
        // Replace one char in the otherwise-valid digest with a
        // non-hex character.
        let hash = SourceHash::new(format!(
            "sha256:{}{}",
            "a".repeat(SOURCE_HASH_DIGEST_HEX_LENGTH - 1),
            "z"
        ));
        let err = hash
            .validate()
            .expect_err("non-hex digest must be rejected");
        assert!(matches!(
            err,
            SourceHashError::NonHexDigest { found: 'z' }
        ));
        assert_eq!(err.as_str(), "NonHexDigest");
    }

    #[test]
    fn source_hash_validate_rejects_upper_case_hex_for_stable_wire_form() {
        // Upper-case hex is *technically* valid hex, but the SyRS
        // SYS-79 wire form is lower-case so re-serialization round-trips
        // are stable. The validator must reject upper-case so future
        // producers don't drift.
        let hash = SourceHash::new(format!(
            "sha256:{}{}",
            "a".repeat(SOURCE_HASH_DIGEST_HEX_LENGTH - 1),
            "A"
        ));
        let err = hash
            .validate()
            .expect_err("upper-case hex must be rejected");
        assert!(matches!(
            err,
            SourceHashError::NonHexDigest { found: 'A' }
        ));
    }

    #[test]
    fn deployed_version_carries_only_two_required_fields() {
        let version = DeployedVersion::new(
            SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
            1_715_700_000,
        );
        let DeployedVersion {
            source_hash: _,
            deployed_at_seconds: _,
        } = version.clone();
        assert_eq!(version.source_hash.as_str(), SAMPLE_SOURCE_HASH_ALPHA);
        assert_eq!(version.deployed_at_seconds, 1_715_700_000);
    }

    #[test]
    fn deployed_version_identifier_is_stable_canonical_string() {
        // SRS-ORCH-004 acceptance: dashboard, REST API, and backtest
        // results "display or return the same version identifier".
        // The canonical form is `<hash>@<timestamp>`. Pinning this
        // here is the single source of truth — future surfaces must
        // render this exact string.
        let version = DeployedVersion::new(
            SourceHash::new(SAMPLE_SOURCE_HASH_ALPHA),
            1_715_700_000,
        );
        assert_eq!(
            version.version_identifier(),
            format!("{SAMPLE_SOURCE_HASH_ALPHA}@1715700000")
        );
    }

    #[test]
    fn deployed_version_invalid_factory_pins_the_wire_string() {
        // SRS-ORCH-004 / SyRS SYS-64: the rejection wire string must
        // be DEPLOYED_VERSION_INVALID and the error_type discriminator
        // must encode the specific validation failure variant.
        let request = StrategyLaunchRequest {
            strategy_id: StrategyId::new("alpha-1"),
            mode: StrategyMode::Live,
            deployment_hash: SourceHash::new("sha256:abc"),
            deadline_millis: STRATEGY_STARTUP_DEADLINE_MS,
            profile: ResourceProfile::live_default(),
        };
        let violation = request
            .deployment_hash
            .validate()
            .expect_err("must be invalid");
        let error =
            StructuredOrchestratorError::deployed_version_invalid(request.clone(), violation);
        assert_eq!(error.category, OrderErrorCategory::DeployedVersionInvalid);
        assert_eq!(error.category.as_str(), "DEPLOYED_VERSION_INVALID");
        assert_eq!(error.error_type, "DeployedVersionInvalid::InvalidDigestLength");
        assert!(error.message.contains("SRS-ORCH-004"));
        assert!(error.message.contains("SYS-79"));
        assert!(error.message.contains("alpha-1"));
        assert_eq!(error.original_request, request);
    }

    #[test]
    fn order_error_category_deployed_version_invalid_wire_string() {
        assert_eq!(
            OrderErrorCategory::DeployedVersionInvalid.as_str(),
            "DEPLOYED_VERSION_INVALID"
        );
    }
}
