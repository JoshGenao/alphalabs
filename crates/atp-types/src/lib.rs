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
