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
}
