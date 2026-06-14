//! Source-neutral order-event callback authority (SRS-SDK-004).
//!
//! SRS-SDK-004 ("deliver order event callbacks to Python strategy code") requires
//! the platform to surface fill / partial-fill / cancellation / rejection
//! callbacks to a strategy, identical for live IB execution and internal paper
//! simulation (SRS-SDK-001 / AC-14). Traces SyRS SYS-7 (live callback delivery),
//! SYS-85 (the paper engine reuses the same IF-7 callback surface), NFR-P4 (the
//! p95 latency budgets); StRS SN-1.22 / SN-1.29.
//!
//! This module is the **Rust core side** of that contract. AGENTS.md forbids the
//! Rust core runtime services from depending on the Python Strategy SDK, so the
//! live ([`atp-execution`], SRS-EXE-001) and paper ([`atp-simulation`],
//! SRS-SIM-001) dispatchers — sibling crates that must not depend on one another —
//! need a *shared, source-neutral* authority for **which callback category a
//! dispatcher must emit for which order-lifecycle transition**. That authority
//! lives here in `atp-types`, the leaf crate both siblings already depend on (the
//! same home as the [`OrderState`] machine it builds on), so the live and paper
//! dispatchers derive an *identical* event category from an identical transition
//! by construction — the SRS-SDK-001 parity guarantee, not a runtime coincidence.
//!
//! The single in-code source of truth is [`OrderEventCategory::for_transition`]:
//! it is **fail-closed** — it consults the documented [`OrderState`] graph
//! (`OrderState::allowed_next`, the SRS-EXE-008 authority) and refuses to derive
//! an event for a transition that cannot legally happen. A legal transition into
//! a state that has no strategy-facing callback (an internal lifecycle state)
//! yields `Ok(None)`; a legal transition into a callback-bearing state yields
//! `Ok(Some(category))`. The state→category map is mirrored machine-readably in
//! `architecture/runtime_services.json` (block `order_event_dispatch_contract`)
//! and pinned against this code arm-for-arm by `tools/order_event_dispatch_check.py`.
//!
//! ## Scope (this is the SDK-surface / pure-logic half only)
//!
//! This module decides the event *category* and the per-category *field-presence
//! requirement* (the Rust analog of the Python SDK's `assert_order_event_payload`
//! rules). It deliberately does **not** model money values (no `f64` price or
//! commission), perform delivery, or measure latency. The AC field *values*
//! (fill price, fill quantity, commission), the actual delivery to Python, and
//! the NFR-P4 p95 latency proof require the real runtime dispatchers and are
//! deferred with named owners:
//!
//!   * live broker-event → callback delivery + live p95 proof — SRS-EXE-001 /
//!     SRS-EXE-006 (IB adapter order events);
//!   * simulated fill → callback delivery + paper p95 proof — SRS-SIM-001 /
//!     SRS-SIM-002.
//!
//! `SRS-SDK-004` therefore stays `passes:false` in `feature_list.json` until
//! those land; this module + its contract check are the prerequisite surface the
//! production dispatchers consume.
//!
//! [`atp-execution`]: https://example.invalid/atp-execution
//! [`atp-simulation`]: https://example.invalid/atp-simulation

use core::fmt;

use crate::order_lifecycle::{OrderKey, OrderLifecycleError, OrderState};

/// The order-event callback category delivered to strategy code (SRS-SDK-004).
///
/// The four AC-named categories — fill, partial fill, cancellation, rejection —
/// are the SRS-SDK-004 acceptance-criteria surface; `ACK` and `EXPIRED` cover
/// the broker acknowledgement and time-in-force-lapsed paths for completeness.
/// The wire strings ([`OrderEventCategory::as_str`]) are the stable
/// cross-surface vocabulary shared with the Python `OrderEventType` enum and the
/// REST / WebSocket surfaces (SyRS SYS-64 alignment).
///
/// **Opaque (private construction) on purpose.** This is a newtype around a
/// *private* [`Category`] enum, so a dispatcher in another crate cannot
/// construct a category directly — neither the inner enum nor the field is
/// reachable, and (unlike a bare `#[non_exhaustive]` enum) the unit variants are
/// not nameable for construction. The only way to obtain a value is from
/// [`OrderLedger::transition_with_event`](crate::OrderLedger::transition_with_event),
/// which derives it from a *successful, real* transition of the tracked order.
/// This makes "no callback without an actual order-state mutation" hold by
/// construction, not by convention. The contract check enforces the field and
/// the inner enum stay private.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct OrderEventCategory(Category);

/// The private variant carrier for [`OrderEventCategory`]. Private so the
/// category cannot be constructed outside this crate (see the opacity note on
/// [`OrderEventCategory`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
enum Category {
    /// The broker acknowledged the order (no fill yet).
    Ack,
    /// The order is fully filled (a terminal fill).
    Fill,
    /// The order is partially filled and still working.
    PartialFill,
    /// The order was cancelled.
    Cancelled,
    /// The order was rejected (by the broker or the engine).
    Rejected,
    /// The order expired (time-in-force lapsed).
    Expired,
}

impl OrderEventCategory {
    /// Stable wire string for this category — must match the Python
    /// `OrderEventType` member values one-for-one (cross-surface vocabulary).
    pub const fn as_str(self) -> &'static str {
        match self.0 {
            Category::Ack => "ACK",
            Category::Fill => "FILL",
            Category::PartialFill => "PARTIAL_FILL",
            Category::Cancelled => "CANCELLED",
            Category::Rejected => "REJECTED",
            Category::Expired => "EXPIRED",
        }
    }

    /// `true` for the four categories named directly in the SRS-SDK-004
    /// acceptance criteria (`FILL`, `PARTIAL_FILL`, `CANCELLED`, `REJECTED`).
    pub const fn is_ac_named(self) -> bool {
        matches!(
            self.0,
            Category::Fill | Category::PartialFill | Category::Cancelled | Category::Rejected
        )
    }

    /// Whether a dispatcher must populate the fill economics (fill price, fill
    /// quantity, commission) on this category's payload. Required for exactly
    /// the four AC-named categories so live / paper P&L reconciles without an
    /// out-of-band lookup — the Rust analog of the Python SDK's
    /// `assert_order_event_payload` field-presence rule. (On a never-filled
    /// cancel / reject a dispatcher populates explicit zeros; the *requirement*
    /// to populate the fields is what this models, not the values.)
    pub const fn requires_fill_economics(self) -> bool {
        self.is_ac_named()
    }

    /// Whether a dispatcher must populate a reason string on this category's
    /// payload. Required for `CANCELLED`, `REJECTED`, and `EXPIRED` so strategy
    /// code can route on the structured-error contract (SyRS SYS-64).
    pub const fn requires_reason(self) -> bool {
        matches!(
            self.0,
            Category::Cancelled | Category::Rejected | Category::Expired
        )
    }

    /// The callback category for an order that *enters* `state`, or `None` when
    /// entering `state` surfaces no strategy-facing callback.
    ///
    /// **Private on purpose.** This is the internal destination-state mapping;
    /// it does *not* check that a legal transition occurred. A caller with
    /// direct access could emit a `FILL` / `REJECTED` callback for an impossible
    /// or terminal-state transition, defeating the fail-closed invariant — so
    /// the only public way to derive a category is [`Self::for_transition`],
    /// which gates on the documented graph first. The contract check enforces
    /// that this stays private (no public bypass).
    ///
    /// The three internal lifecycle states — `NEW` (admitted, not yet sent),
    /// `PENDING_SUBMIT` (in flight to the broker), and `CANCEL_PENDING` (a
    /// cancel request is in flight) — surface no callback: the strategy already
    /// holds its order handle from submission, and an in-flight cancel is not
    /// yet a terminal cancellation. Every one of the nine [`OrderState`] values
    /// is mapped here (totality), pinned against the JSON mirror by the contract
    /// check.
    const fn for_state(state: OrderState) -> Option<Self> {
        match state {
            OrderState::New => None,
            OrderState::PendingSubmit => None,
            OrderState::Acked => Some(Self(Category::Ack)),
            OrderState::PartiallyFilled => Some(Self(Category::PartialFill)),
            OrderState::Filled => Some(Self(Category::Fill)),
            OrderState::CancelPending => None,
            OrderState::Cancelled => Some(Self(Category::Cancelled)),
            OrderState::Rejected => Some(Self(Category::Rejected)),
            OrderState::Expired => Some(Self(Category::Expired)),
        }
    }

    /// Derive the strategy-facing callback for a *modeled* lifecycle transition
    /// `from -> to`, **fail-closed** against the documented [`OrderState`] graph.
    ///
    /// **Crate-internal (`pub(crate)`) on purpose.** A free function over
    /// caller-supplied state pairs would let a dispatcher fabricate a callback
    /// for an order that is not actually in `from` (e.g. derive `ACKED -> FILLED`
    /// for an order still in `NEW`). So this is not public: the public,
    /// order-bound entry point is
    /// [`OrderLedger::transition_with_event`](crate::OrderLedger::transition_with_event),
    /// which feeds the *tracked order's real current state* as `from` and only
    /// returns a callback when the underlying mutation actually succeeds.
    ///
    /// Returns:
    ///   * `Err(`[`OrderLifecycleError::IllegalTransition`]`)` if `from` cannot
    ///     legally transition to `to` — a dispatcher must **never** fabricate a
    ///     callback for a transition that cannot happen (a duplicate, stale, or
    ///     out-of-order broker event that maps to no legal modeled transition is
    ///     rejected here; the dispatcher must dedup / reconcile it, not invent a
    ///     callback);
    ///   * `Ok(None)` for a legal transition into an internal (no-callback)
    ///     state;
    ///   * `Ok(Some(category))` for a legal transition into a callback-bearing
    ///     state.
    ///
    /// Because both the live and paper dispatchers route every transition
    /// through this one function, identical transitions yield identical
    /// categories — the SRS-SDK-001 / AC-14 live-vs-paper parity guarantee.
    ///
    /// ## Hard boundary: this models ONLY transition-derived callbacks
    ///
    /// The callback is derived from the lifecycle *destination state*. By
    /// construction this authority can therefore represent **only** events that
    /// *change* the order's state — it cannot represent a **state-preserving**
    /// event, and it deliberately does not try to:
    ///
    ///   * a **cancel rejection** (the broker refuses a pending cancel and the
    ///     order stays working) produces *no* state change, so there is no
    ///     transition for this authority to classify — and because the category
    ///     is opaque, an adapter cannot mint one either. Representing
    ///     state-preserving rejections needs a **separate, source-neutral
    ///     broker/simulator event-kind API**, which is *out of scope here* and
    ///     owned by the IB adapter (SRS-EXE-006) and sim fill model
    ///     (SRS-SIM-002). It is **not** an `OrderEventCategory`. (`SRS-SDK-004`
    ///     stays `passes:false` precisely because that half is unbuilt.)
    ///   * **duplicate / stale / out-of-order** broker events that map to no
    ///     legal modeled transition fail closed above — the adapter dedups and
    ///     reconciles them against the acknowledged broker id.
    ///
    /// This boundary is honest about what an offline, transition-derived
    /// authority can know: the full broker event-kind taxonomy is authoritatively
    /// defined by the deferred IB adapter, so it is named and deferred rather
    /// than guessed here.
    pub(crate) fn for_transition(
        from: OrderState,
        to: OrderState,
    ) -> Result<Option<Self>, OrderLifecycleError> {
        if from.can_transition_to(to) {
            Ok(Self::for_state(to))
        } else {
            Err(OrderLifecycleError::IllegalTransition { from, to })
        }
    }
}

impl fmt::Display for OrderEventCategory {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.as_str())
    }
}

/// One order-event produced by a lifecycle transition: which order
/// ([`OrderKey`]) it concerns, the state it reached, and the strategy-facing
/// callback [`OrderEventCategory`] (or `None` for an internal, no-callback
/// state).
///
/// A single [`OrderLedger::transition_with_event`](crate::OrderLedger::transition_with_event)
/// call returns **every** event the transition produces — the transitioned
/// order *plus* any order whose state cascaded (a held cancel-replace
/// replacement auto-suppressed to `REJECTED` when its original terminates
/// non-cancelled). Returning the cascade atomically is why a strategy never
/// silently loses the rejection callback for its auto-suppressed replacement.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OrderEvent {
    key: OrderKey,
    state: OrderState,
    category: Option<OrderEventCategory>,
}

impl OrderEvent {
    /// Build an event for `key` reaching `state` with callback `category`.
    pub(crate) fn new(
        key: OrderKey,
        state: OrderState,
        category: Option<OrderEventCategory>,
    ) -> Self {
        Self {
            key,
            state,
            category,
        }
    }

    /// The order this event concerns.
    pub fn key(&self) -> &OrderKey {
        &self.key
    }

    /// The state the order reached.
    pub fn state(&self) -> OrderState {
        self.state
    }

    /// The strategy-facing callback category, or `None` for an internal
    /// (no-callback) state.
    pub fn category(&self) -> Option<OrderEventCategory> {
        self.category
    }
}

/// p95 budget for **live** order-event callback delivery, in milliseconds
/// (SyRS NFR-P4; SRS-SDK-004 acceptance criterion). Measured from broker fill
/// acknowledgement (IB Gateway) to the strategy callback returning. The
/// cross-language source of truth is `architecture/runtime_services.json`
/// (`strategy_api_order_events_contract.required_live_callback_latency_p95_ms`);
/// this constant is the Rust-core view kept in lock-step with that metadata —
/// and with the Python SDK's `LIVE_CALLBACK_LATENCY_P95_MS` — by
/// `tools/order_event_dispatch_check.py`, so a future NFR-P4 revision changes
/// the number in exactly one place. The end-to-end p95 *proof* is deferred to
/// SRS-EXE-001 (it needs a running IB Gateway).
pub const LIVE_CALLBACK_LATENCY_P95_MS: u32 = 1000;

/// p95 budget for **paper** order-event callback delivery, in milliseconds
/// (SyRS NFR-P4; SRS-SDK-004 acceptance criterion). Measured from the internal
/// simulation engine's simulated fill to the strategy callback returning.
/// Source of truth and parity-pinning are identical to
/// [`LIVE_CALLBACK_LATENCY_P95_MS`]
/// (`strategy_api_order_events_contract.required_paper_callback_latency_p95_ms`).
/// The end-to-end p95 *proof* is deferred to SRS-SIM-001 (it needs a running
/// simulation engine).
pub const PAPER_CALLBACK_LATENCY_P95_MS: u32 = 100;

#[cfg(test)]
mod tests {
    use super::*;

    const ALL_STATES: [OrderState; 9] = [
        OrderState::New,
        OrderState::PendingSubmit,
        OrderState::Acked,
        OrderState::PartiallyFilled,
        OrderState::Filled,
        OrderState::CancelPending,
        OrderState::Cancelled,
        OrderState::Rejected,
        OrderState::Expired,
    ];

    /// The callback category a callback-bearing state maps to (via the private
    /// mapper — this is an in-crate test, so it may reach `for_state`).
    fn category(state: OrderState) -> OrderEventCategory {
        OrderEventCategory::for_state(state).expect("callback-bearing state")
    }

    #[test]
    fn wire_strings_match_the_documented_vocabulary() {
        assert_eq!(category(OrderState::Acked).as_str(), "ACK");
        assert_eq!(category(OrderState::Filled).as_str(), "FILL");
        assert_eq!(
            category(OrderState::PartiallyFilled).as_str(),
            "PARTIAL_FILL"
        );
        assert_eq!(category(OrderState::Cancelled).as_str(), "CANCELLED");
        assert_eq!(category(OrderState::Rejected).as_str(), "REJECTED");
        assert_eq!(category(OrderState::Expired).as_str(), "EXPIRED");
        // Display agrees with as_str.
        assert_eq!(category(OrderState::Filled).to_string(), "FILL");
    }

    #[test]
    fn for_state_covers_every_state_and_maps_the_callback_states() {
        // Totality: for_state is defined for all nine states (no panic, no gap).
        let mapped: Vec<Option<&'static str>> = ALL_STATES
            .into_iter()
            .map(|s| OrderEventCategory::for_state(s).map(OrderEventCategory::as_str))
            .collect();
        assert_eq!(
            mapped,
            vec![
                None,                 // New
                None,                 // PendingSubmit
                Some("ACK"),          // Acked
                Some("PARTIAL_FILL"), // PartiallyFilled
                Some("FILL"),         // Filled
                None,                 // CancelPending
                Some("CANCELLED"),    // Cancelled
                Some("REJECTED"),     // Rejected
                Some("EXPIRED"),      // Expired
            ]
        );
    }

    #[test]
    fn for_transition_matches_for_state_on_every_legal_edge() {
        for from in ALL_STATES {
            for to in ALL_STATES {
                let result = OrderEventCategory::for_transition(from, to);
                if from.can_transition_to(to) {
                    assert_eq!(
                        result,
                        Ok(OrderEventCategory::for_state(to)),
                        "legal edge {from} -> {to} must derive for_state(to)"
                    );
                } else {
                    assert_eq!(
                        result,
                        Err(OrderLifecycleError::IllegalTransition { from, to }),
                        "illegal edge {from} -> {to} must fail closed (no event)"
                    );
                }
            }
        }
    }

    #[test]
    fn for_transition_is_fail_closed_on_a_terminal_origin() {
        // No transition leaves a terminal state, so no event may be derived.
        for from in [
            OrderState::Filled,
            OrderState::Cancelled,
            OrderState::Rejected,
            OrderState::Expired,
        ] {
            for to in ALL_STATES {
                assert_eq!(
                    OrderEventCategory::for_transition(from, to),
                    Err(OrderLifecycleError::IllegalTransition { from, to })
                );
            }
        }
    }

    #[test]
    fn ac_named_categories_are_exactly_the_four() {
        let mut ac_named: Vec<&'static str> = ALL_STATES
            .into_iter()
            .filter_map(OrderEventCategory::for_state)
            .filter(|c| c.is_ac_named())
            .map(OrderEventCategory::as_str)
            .collect();
        ac_named.sort_unstable();
        assert_eq!(
            ac_named,
            vec!["CANCELLED", "FILL", "PARTIAL_FILL", "REJECTED"]
        );
    }

    #[test]
    fn field_presence_requirements_match_the_sdk_contract() {
        // Fill economics required for exactly the four AC-named categories.
        for category in ALL_STATES
            .into_iter()
            .filter_map(OrderEventCategory::for_state)
        {
            assert_eq!(category.requires_fill_economics(), category.is_ac_named());
        }
        // Reason required for CANCELLED / REJECTED / EXPIRED.
        assert!(category(OrderState::Cancelled).requires_reason());
        assert!(category(OrderState::Rejected).requires_reason());
        assert!(category(OrderState::Expired).requires_reason());
        assert!(!category(OrderState::Acked).requires_reason());
        assert!(!category(OrderState::Filled).requires_reason());
        assert!(!category(OrderState::PartiallyFilled).requires_reason());
    }

    #[test]
    fn latency_budgets_are_the_nfr_p4_numbers() {
        assert_eq!(LIVE_CALLBACK_LATENCY_P95_MS, 1000);
        assert_eq!(PAPER_CALLBACK_LATENCY_P95_MS, 100);
    }
}
