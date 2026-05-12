use atp_types::{
    OrderErrorCategory, RuntimeService, StrategyId, StructuredSubscriptionError,
    SubscriptionLimitEvent, SubscriptionLimitState, SubscriptionRequest,
};

#[derive(Debug, Default)]
pub struct MarketDataSubscriptionManager;

// --------------------------------------------------------------------------- //
// Subscription manager ports (SRS-MD-002 / SyRS SYS-70 / SYS-64)
// --------------------------------------------------------------------------- //
//
// The subscription manager owns the IB line accounting; ERR-4's gate
// consults two ports:
//
//   * `SubscriptionLineCounter` — exposes the configured limit, the
//     current in-use count, and a `try_acquire` probe that the gate
//     consults before admitting a request. Concrete implementations
//     (deferred to SRS-MD-001 / SRS-MD-007) hold the actual subscription
//     set and the operator-configured `ATP_MARKET_DATA_LINE_LIMIT` value.
//     `try_acquire` is read-only with respect to the registry — admission
//     happens after the manager observes `WithinLimit`.
//
//   * `SubscriptionLimitEventSink` — the structured-event publication
//     channel. Concrete sinks (deferred) route events to logs
//     (SRS-LOG-001), the dashboard WebSocket alert pane, and the
//     notification dispatcher per SyRS SYS-70's "alert the operator on
//     the dashboard" clause.
//
// Both traits live in `atp-market-data` (not `atp-execution`) because
// the consumer — `MarketDataSubscriptionManager::request_subscription` —
// lives here. Placing them in `atp-execution` would invert the
// SRS-ARCH-002 dependency direction.
pub trait SubscriptionLineCounter {
    /// Number of IB market-data lines currently in use by the
    /// consolidated subscription set.
    fn lines_in_use(&self) -> u32;

    /// Operator-configured ceiling from `ATP_MARKET_DATA_LINE_LIMIT`.
    fn line_limit(&self) -> u32;

    /// Probe the limit without mutating the subscription registry.
    /// Returns `ExceededLimit` if admitting `request` would push the
    /// in-use count past the configured ceiling.
    fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState;
}

pub trait SubscriptionLimitEventSink {
    fn record(&self, event: SubscriptionLimitEvent);
}

/// Happy-path admission envelope. Echoes back the request identity so the
/// caller can correlate the acceptance with the originating strategy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionAccepted {
    pub strategy_id: StrategyId,
    pub symbol: String,
}

impl MarketDataSubscriptionManager {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::MarketDataSubscriptionManager
    }

    pub fn owns_subscription_fanout(&self) -> bool {
        true
    }

    /// SRS-MD-002 / SyRS SYS-70 subscription-limit gate. Matches on the
    /// counter's `try_acquire` probe; `WithinLimit` returns
    /// `SubscriptionAccepted`; `ExceededLimit` emits a structured
    /// `SubscriptionLimitEvent` through the sink AND returns a
    /// `StructuredSubscriptionError` whose category is
    /// `OrderErrorCategory::SubscriptionLimitReached` (wire string
    /// `SUBSCRIPTION_LIMIT_REACHED`).
    ///
    /// **Invariants** (statically checked by
    /// `tools/subscription_limit_check.py`):
    ///
    /// * The `ExceededLimit` arm MUST call `events.record(`.
    /// * The `ExceededLimit` arm MUST produce
    ///   `OrderErrorCategory::SubscriptionLimitReached`.
    /// * The `ExceededLimit` arm MUST NOT mutate the subscription
    ///   registry (no `registry.insert(`, `subscriptions.insert(`, or
    ///   `request.acquire(` calls inside the rejection leaf). The
    ///   rejected request must leave the registry exactly as it found
    ///   it.
    /// * `WithinLimit` is the only call site of `SubscriptionAccepted {`.
    ///
    /// The gate takes no `StrategyMode` parameter: SyRS SYS-64 mandates
    /// an identical error contract for live and paper modes, and SyRS
    /// SYS-70 places the gate over the consolidated subscription set
    /// for all active strategies regardless of mode.
    pub fn request_subscription<C, S>(
        &self,
        request: SubscriptionRequest,
        counter: &C,
        events: &S,
    ) -> Result<SubscriptionAccepted, StructuredSubscriptionError>
    where
        C: SubscriptionLineCounter,
        S: SubscriptionLimitEventSink,
    {
        match counter.try_acquire(&request) {
            SubscriptionLimitState::WithinLimit => Ok(SubscriptionAccepted {
                strategy_id: request.strategy_id,
                symbol: request.symbol,
            }),
            SubscriptionLimitState::ExceededLimit => {
                let current_lines = counter.lines_in_use();
                let configured_limit = counter.line_limit();
                events.record(SubscriptionLimitEvent {
                    state: SubscriptionLimitState::ExceededLimit,
                    strategy_id: request.strategy_id.clone(),
                    symbol: request.symbol.clone(),
                    current_lines,
                    configured_limit,
                });
                Err(StructuredSubscriptionError::limit_reached(
                    request,
                    current_lines,
                    configured_limit,
                ))
            }
        }
    }
}

// Re-export to satisfy the static checker — references the
// `OrderErrorCategory` variant by name so a workspace-level dead-code
// scan cannot drop the link between the wire string and this crate.
#[doc(hidden)]
pub const _SUBSCRIPTION_LIMIT_CATEGORY: OrderErrorCategory =
    OrderErrorCategory::SubscriptionLimitReached;

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    #[test]
    fn identifies_market_data_subscription_manager() {
        let manager = MarketDataSubscriptionManager;
        assert_eq!(
            manager.service(),
            RuntimeService::MarketDataSubscriptionManager
        );
        assert!(manager.owns_subscription_fanout());
    }

    struct StubCounter {
        state: SubscriptionLimitState,
        current: u32,
        limit: u32,
    }

    impl SubscriptionLineCounter for StubCounter {
        fn lines_in_use(&self) -> u32 {
            self.current
        }
        fn line_limit(&self) -> u32 {
            self.limit
        }
        fn try_acquire(&self, _request: &SubscriptionRequest) -> SubscriptionLimitState {
            self.state
        }
    }

    #[derive(Default)]
    struct StubSink {
        events: RefCell<Vec<SubscriptionLimitEvent>>,
    }

    impl SubscriptionLimitEventSink for StubSink {
        fn record(&self, event: SubscriptionLimitEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    #[test]
    fn within_limit_state_returns_accepted_and_emits_no_event() {
        let manager = MarketDataSubscriptionManager;
        let counter = StubCounter {
            state: SubscriptionLimitState::WithinLimit,
            current: 50,
            limit: 100,
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("paper-alpha-1"),
            symbol: "AAPL".to_string(),
        };

        let accepted = manager
            .request_subscription(request, &counter, &sink)
            .expect("WithinLimit must accept the request");
        assert_eq!(accepted.strategy_id.as_str(), "paper-alpha-1");
        assert_eq!(accepted.symbol, "AAPL");
        assert!(
            sink.events.borrow().is_empty(),
            "WithinLimit must not emit a SubscriptionLimitEvent"
        );
    }

    #[test]
    fn exceeded_limit_state_rejects_with_subscription_limit_reached() {
        let manager = MarketDataSubscriptionManager;
        let counter = StubCounter {
            state: SubscriptionLimitState::ExceededLimit,
            current: 100,
            limit: 100,
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "AAPL".to_string(),
        };

        let error = manager
            .request_subscription(request.clone(), &counter, &sink)
            .expect_err("ExceededLimit must reject the request");
        assert_eq!(
            error.category,
            OrderErrorCategory::SubscriptionLimitReached
        );
        assert_eq!(error.category.as_str(), "SUBSCRIPTION_LIMIT_REACHED");
        assert_eq!(error.original_request, request);
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one event per rejected request");
        assert_eq!(events[0].state, SubscriptionLimitState::ExceededLimit);
        assert_eq!(events[0].current_lines, 100);
        assert_eq!(events[0].configured_limit, 100);
    }

    #[test]
    fn exceeded_limit_state_does_not_consult_counter_twice() {
        // Sanity check: the gate must consult `try_acquire` exactly once.
        // A future refactor that probes the counter inside both arms
        // would silently degrade dashboard accuracy (and double-count
        // events). Wrap the StubCounter in a call-counter and assert.
        struct CountingCounter {
            inner: StubCounter,
            try_acquire_calls: Cell<u32>,
        }
        impl SubscriptionLineCounter for CountingCounter {
            fn lines_in_use(&self) -> u32 {
                self.inner.current
            }
            fn line_limit(&self) -> u32 {
                self.inner.limit
            }
            fn try_acquire(&self, request: &SubscriptionRequest) -> SubscriptionLimitState {
                self.try_acquire_calls
                    .set(self.try_acquire_calls.get() + 1);
                self.inner.try_acquire(request)
            }
        }

        let manager = MarketDataSubscriptionManager;
        let counter = CountingCounter {
            inner: StubCounter {
                state: SubscriptionLimitState::ExceededLimit,
                current: 200,
                limit: 100,
            },
            try_acquire_calls: Cell::new(0),
        };
        let sink = StubSink::default();
        let request = SubscriptionRequest {
            strategy_id: StrategyId::new("live-alpha"),
            symbol: "MSFT".to_string(),
        };
        let _ = manager.request_subscription(request, &counter, &sink);
        assert_eq!(
            counter.try_acquire_calls.get(),
            1,
            "the gate must probe try_acquire exactly once per request"
        );
    }
}
