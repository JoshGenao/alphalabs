//! SRS-EXE-002 — route all non-live strategy orders to the internal simulation
//! engine (SyRS SYS-2b / SYS-2e, AC-10; StRS SN-1.06 / SN-1.29 / C-11).
//!
//! This is the source-neutral **dispatch authority** that complements the
//! SRS-EXE-001 live-designation authority (`designation.rs`). EXE-001 owns the
//! question *"may this strategy route to IB?"* — and on the live path rejects
//! any non-designated submission (ERR-1). EXE-002 owns the **normal order
//! entry**: every order is dispatched to exactly one destination —
//!
//!   * the single designated live strategy ([`LiveRoutingDecision::Authorized`])
//!     → the live IB gate ([`ExecutionEngine::route_order`], which applies the
//!     ERR-1/2/3 connectivity/freshness safeguards), and
//!   * **every other (non-live) strategy** → the internal simulation engine,
//!     never IB.
//!
//! The routing decision is the [`OrderRoute`] enum, derived **only** from the
//! engine-owned [`LiveRoutingDecision`] authority — it is **not** a new source
//! of truth, so the live/paper split cannot drift from EXE-001. Because only
//! [`LiveRoutingDecision::Authorized`] maps to [`OrderRoute::LiveBrokerage`],
//! the routing *decision* cannot map a non-live strategy to the broker — that
//! is unrepresentable in [`route_destination`] / [`dispatch_order`] by
//! construction (the AC-10 invariant **at the dispatch boundary**).
//!
//! **Scope of that guarantee (honest bound).** "By construction" is about the
//! routing *decision*, not an end-to-end "paper can never reach IB" proof. Two
//! things sit outside this slice and are deferred with named owners (see
//! `order_routing_contract.deferred[]`):
//!   1. [`dispatch_order`] is the intended **sole** production order-entry, but
//!      the lower-level [`submit_live_order`](ExecutionEngine::submit_live_order)
//!      is still `pub` and trusts a caller-supplied `StrategyMode`, so a caller
//!      can bypass this dispatcher. Closing that bypass (crate-private + an
//!      admission token) is the SRS-EXE-001-deferred **sole-entry wiring**; it
//!      is kept open here because the pinned ERR-1/2/3 contract
//!      (`tools/error_handling_check.py` pins `pub fn submit_live_order`; the
//!      err_1/2/3 integration tests call it directly) depends on it — owner
//!      SRS-EXE-006 / SRS-ORCH-*.
//!   2. [`InternalSimulationSubmit`] is an *abstraction* the orchestrator binds
//!      to the real `PaperSimulationEngine`. That no broker adapter hides
//!      behind a supplied impl is established by the deferred orchestrator
//!      wiring + a real-component boundary test, not by this slice's
//!      panic-on-touch stubs (which prove only that the *dispatcher* routes a
//!      non-live order to the sim port and touches no IB port).
//!
//! [`route_destination`]: ExecutionEngine::route_destination
//! [`dispatch_order`]: ExecutionEngine::dispatch_order
//!
//! Dependency direction (SRS-ARCH-002): `atp-execution` must not depend on
//! `atp-simulation`. The simulation destination is reached through the
//! [`InternalSimulationSubmit`] **port** the orchestrator wires to the real
//! `PaperSimulationEngine` — exactly mirroring how [`LiveBrokerageSubmit`]
//! abstracts the IB adapter. This crate never names a simulation or broker
//! type directly.
//!
//! [`LiveBrokerageSubmit`]: crate::LiveBrokerageSubmit
//!
//! ## Scope
//!
//! This module is the routing-authority half: the source-neutral destination
//! decision + the two ports. The COMPOSITION half lives in
//! `atp-orchestrator::order_routing_wiring` (SRS-ARCH-002 keeps this crate
//! independent of `atp-simulation`/`atp-adapters`): the real
//! `PaperSimulationEngine::accept_order` behind [`InternalSimulationSubmit`]
//! (with the `OrderSubmission` → `OrderLeg` mapping and the `VirtualOrderBook`
//! single order store), the real SRS-EXE-006 adapter behind
//! [`LiveBrokerageSubmit`], and the `exe002_order_routing_cli` operator
//! verification workflow (the deployed strategy-runtime order path — real
//! strategy containers submitting through `dispatch_order` — stays deferred
//! to the SRS-SDK strategy host / SRS-ORCH-* runtime). The IB paper account
//! stays reachable only through the
//! operator-initiated SRS-EXE-006 adapter integration test. Still deferred
//! with named owners (`architecture/runtime_services.json`
//! `order_routing_contract.deferred[]`): the Python strategy host (SRS-SDK);
//! live multi-leg composites (SRS-EXE-004); the simulated fill loop behind the
//! port (SRS-SIM-002/003/004); the correlation-id idempotency key
//! (SRS-EXE-008); and the simulated-order stale-data gate (SRS-MD-004).
//!
//! [`LiveBrokerageSubmit`]: crate::LiveBrokerageSubmit

use atp_types::{
    OrderErrorCategory, OrderReceipt, OrderSubmission, StrategyId, StructuredOrderError,
};

use crate::designation::LiveRoutingDecision;
use crate::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveBrokerageSubmit,
    MarketDataFreshnessProbe, StaleDataEventSink,
};

/// The source-neutral routing destination for an order (SRS-EXE-002, SyRS
/// SYS-2b / SYS-2e). Derived solely from the engine-owned
/// [`LiveRoutingDecision`] authority: only the single designated live strategy
/// is routed to the live broker; everyone else is routed to the internal
/// simulation engine. There is deliberately **no** third variant — a non-live
/// strategy cannot be routed anywhere but [`InternalSimulation`](OrderRoute::InternalSimulation).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OrderRoute {
    /// The single designated live strategy — its orders route to the live IB
    /// gate ([`ExecutionEngine::route_order`]).
    LiveBrokerage,
    /// Every non-designated (non-live) strategy — its orders route to the
    /// internal simulation engine and never create an IB order (AC-10).
    InternalSimulation,
}

/// Acknowledgement returned by the internal simulation engine for a simulated
/// order. A **distinct** type from [`OrderReceipt`] (which carries a
/// `broker_order_id`): a simulated order never mints a broker order id, so the
/// type system records that no IB order was created.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SimulatedOrderReceipt {
    /// The simulation engine's local identifier for the accepted paper order.
    pub sim_order_id: String,
}

/// The outcome of dispatching an order through [`ExecutionEngine::dispatch_order`]:
/// either a live IB [`OrderReceipt`] or an internal-simulation
/// [`SimulatedOrderReceipt`]. The variant records which destination actually
/// handled the order.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OrderRoutingReceipt {
    /// The order was routed to the live IB gate by the single designated live
    /// strategy.
    Live(OrderReceipt),
    /// The order was routed to the internal simulation engine (a non-live
    /// strategy) — no IB order was created.
    Simulated(SimulatedOrderReceipt),
}

/// Port the execution engine uses to hand a non-live strategy's order to the
/// internal simulation engine (SRS-EXE-002 / SRS-SIM-001). Declared at the
/// execution layer — like [`LiveBrokerageSubmit`] for the IB adapter — so
/// `atp-execution` stays independent of `atp-simulation` (SRS-ARCH-002
/// dependency direction: simulation is a sibling crate, not an upstream dep).
/// The orchestrator wires the real `PaperSimulationEngine` to this port
/// (`atp-orchestrator::order_routing_wiring::WiredPaperSimulation`, which also
/// rests every accepted order in the `VirtualOrderBook` single order store).
///
/// The port carries the same [`OrderSubmission`] envelope the live path uses
/// (see [`LiveBrokerageSubmit`]) — keeping the live and paper intake
/// **symmetric** is the source-neutral invariant (live and paper must be
/// identical). As of SRS-EXE-003 `OrderSubmission` carries full execution
/// intent (`asset_class` / `side` / `order_type` + prices) shared with the
/// simulation engine's `OrderLeg`, plus `OrderSubmission::validate` (the
/// price-positivity rule both paths apply). The `OrderSubmission` →
/// `PaperOrderRequest`/`OrderLeg` mapping the real
/// `PaperSimulationEngine::accept_order` needs is field-for-field in the
/// orchestrator wiring. What is still deferred on this envelope: the
/// SRS-EXE-008 [`ClientCorrelationId`] idempotency key (owner SRS-EXE-008) —
/// it must land for the **live and paper paths together** to keep the
/// symmetry (see `order_type_contract.deferred[]`).
///
/// [`LiveBrokerageSubmit`]: crate::LiveBrokerageSubmit
/// [`ClientCorrelationId`]: atp_types::ClientCorrelationId
pub trait InternalSimulationSubmit {
    fn submit_simulated(
        &self,
        submission: OrderSubmission,
    ) -> Result<SimulatedOrderReceipt, StructuredOrderError>;
}

impl ExecutionEngine {
    /// SRS-EXE-002 / SyRS SYS-2b / SYS-2e — the pure routing-destination
    /// decision for `strategy_id`. Reuses the engine-owned
    /// [`LiveRoutingDecision`] authority (the SRS-EXE-001 single source of
    /// truth) so the live/paper split cannot drift: the single designated live
    /// strategy ([`LiveRoutingDecision::Authorized`]) maps to
    /// [`OrderRoute::LiveBrokerage`]; **every** other strategy
    /// ([`LiveRoutingDecision::NotDesignated`]) maps to
    /// [`OrderRoute::InternalSimulation`]. A non-live strategy can never be
    /// routed to the broker (AC-10).
    pub fn route_destination(&self, strategy_id: &StrategyId) -> OrderRoute {
        match self.designation.authority_for(strategy_id) {
            LiveRoutingDecision::Authorized => OrderRoute::LiveBrokerage,
            LiveRoutingDecision::NotDesignated => OrderRoute::InternalSimulation,
        }
    }

    /// SRS-EXE-002 / SyRS SYS-2b / SYS-2e / AC-10 — the normal order-entry
    /// dispatch. Routes the submission to exactly one destination derived from
    /// [`route_destination`](Self::route_destination):
    ///
    ///   * [`OrderRoute::LiveBrokerage`] (the single designated live strategy)
    ///     delegates to [`route_order`](Self::route_order) — the SRS-EXE-001
    ///     authority gate plus the ERR-1/2/3 connectivity/freshness
    ///     safeguards — and **never** consults the simulation port; or
    ///   * [`OrderRoute::InternalSimulation`] (every non-live strategy) hands
    ///     the order to the [`InternalSimulationSubmit`] port and **never**
    ///     reaches an IB-order-creating path (no [`route_order`](Self::route_order),
    ///     no broker submit) — so a paper strategy's order never creates an IB
    ///     order (AC-10).
    ///
    /// AC-10 is about **not creating an IB order**; the simulation arm is free
    /// to consult read-only gates. In particular the SRS-MD-004 / SYS-87
    /// simulated-order stale-data gate (block a *simulated* submission when
    /// market data is stale, returning `MARKET_DATA_STALE`) is a shared safety
    /// gate this slice deliberately does **not** forbid on the paper path — but
    /// it also does not yet implement it; that is SRS-MD-004's deliverable
    /// (deferred; `dispatch_order` already receives the `freshness` port for
    /// when MD-004 adds the check). `dispatch_order`'s only caller today is
    /// the operator fixture-verification CLI (`exe002_order_routing_cli`)
    /// over always-fresh fixture probes — the deployed strategy-runtime order
    /// path is still unwired — so there is no live stale-data bypass today.
    #[allow(clippy::too_many_arguments)]
    pub fn dispatch_order<B, C, E, F, S, P>(
        &self,
        submission: OrderSubmission,
        broker: &B,
        connectivity: &C,
        events: &E,
        freshness: &F,
        stale_events: &S,
        simulation: &P,
    ) -> Result<OrderRoutingReceipt, StructuredOrderError>
    where
        B: LiveBrokerageSubmit,
        C: BrokerageConnectivity,
        E: ConnectivityEventSink,
        F: MarketDataFreshnessProbe,
        S: StaleDataEventSink,
        P: InternalSimulationSubmit,
    {
        // SRS-EXE-003 — validate the order at the SHARED entry, BEFORE routing,
        // so both the live and the internal-simulation arms are held to the same
        // well-formedness (non-blank symbol, positive quantity, positive prices)
        // and the simulation port cannot receive a malformed order. (The SyRS
        // OrderErrorCategory taxonomy has no dedicated invalid-order-parameters
        // bucket; InvalidSymbol is the order-rejection category and the precise
        // reason is carried in error_type — a dedicated category is a
        // cross-cutting SRS-ERR-001 taxonomy change, deferred.)
        if let Err(err) = submission.validate() {
            return Err(StructuredOrderError {
                category: OrderErrorCategory::InvalidSymbol,
                error_type: err.error_type().to_string(),
                message: err.to_string(),
                original_order: submission,
            });
        }
        match self.route_destination(&submission.strategy_id) {
            OrderRoute::LiveBrokerage => self
                .route_order(
                    submission,
                    broker,
                    connectivity,
                    events,
                    freshness,
                    stale_events,
                )
                .map(OrderRoutingReceipt::Live),
            OrderRoute::InternalSimulation => simulation
                .submit_simulated(submission)
                .map(OrderRoutingReceipt::Simulated),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::LiveDesignationConfirmation;
    use atp_types::{ConnectivityEvent, ConnectivityState, MarketDataFreshness, StaleDataEvent};
    use std::cell::{Cell, RefCell};

    // A simulation port that records every submission it accepts.
    #[derive(Default)]
    struct SimulationSpy {
        calls: Cell<u32>,
    }

    impl InternalSimulationSubmit for SimulationSpy {
        fn submit_simulated(
            &self,
            submission: OrderSubmission,
        ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
            self.calls.set(self.calls.get() + 1);
            Ok(SimulatedOrderReceipt {
                sim_order_id: format!(
                    "sim-{}-{}",
                    submission.strategy_id.as_str(),
                    submission.symbol
                ),
            })
        }
    }

    // A simulation port that panics if a designated (live) order is ever routed
    // to it — proves the LiveBrokerage arm never touches the simulation port.
    struct ForbiddenSimulation;

    impl InternalSimulationSubmit for ForbiddenSimulation {
        fn submit_simulated(
            &self,
            _submission: OrderSubmission,
        ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
            panic!(
                "SRS-EXE-002: a designated live strategy must never route to the simulation port"
            );
        }
    }

    #[derive(Default)]
    struct BrokerageSpy {
        calls: Cell<u32>,
    }

    impl LiveBrokerageSubmit for BrokerageSpy {
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

    // Broker stub that panics if a non-live order is ever routed to it — proves
    // the InternalSimulation arm never reaches the IB-order-creating port, the
    // AC-10 invariant ("paper orders never create IB orders"). The read-only
    // connectivity/freshness gates are NOT panic-on-touch: the SRS-MD-004
    // simulated-order stale-data gate (deferred) will legitimately consult
    // freshness on the paper path.
    struct ForbiddenBroker;

    impl LiveBrokerageSubmit for ForbiddenBroker {
        fn submit_order(
            &self,
            _submission: OrderSubmission,
        ) -> Result<OrderReceipt, StructuredOrderError> {
            panic!("SRS-EXE-002: a non-live strategy must never reach the brokerage port");
        }
    }

    struct AlwaysConnected;

    impl BrokerageConnectivity for AlwaysConnected {
        fn state(&self) -> ConnectivityState {
            ConnectivityState::Connected
        }

        fn request_reconnect(&self) {}
    }

    struct AlwaysFresh;

    impl MarketDataFreshnessProbe for AlwaysFresh {
        fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
            MarketDataFreshness::Fresh
        }

        fn staleness_seconds(&self, _symbol: &str) -> u64 {
            0
        }
    }

    #[derive(Default)]
    struct EventSinkSpy {
        events: RefCell<Vec<ConnectivityEvent>>,
    }

    impl ConnectivityEventSink for EventSinkSpy {
        fn record(&self, event: ConnectivityEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    #[derive(Default)]
    struct StaleEventSinkSpy {
        events: RefCell<Vec<StaleDataEvent>>,
    }

    impl StaleDataEventSink for StaleEventSinkSpy {
        fn record(&self, event: StaleDataEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    fn submission(strategy: &str, symbol: &str, qty: i64) -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new(strategy),
            symbol: symbol.to_string(),
            quantity: qty,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        }
    }

    fn confirm(strategy: &str) -> LiveDesignationConfirmation {
        LiveDesignationConfirmation::from_operator(
            StrategyId::new(strategy),
            "operator confirmed live designation",
        )
        .expect("non-empty acknowledgement yields a confirmation token")
    }

    #[test]
    fn order_routing_designated_strategy_routes_to_live_brokerage() {
        let mut engine = ExecutionEngine::default();
        engine
            .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
            .expect("designate the single live strategy");

        assert_eq!(
            engine.route_destination(&StrategyId::new("live-alpha")),
            OrderRoute::LiveBrokerage
        );
    }

    #[test]
    fn order_routing_non_designated_strategy_routes_to_internal_simulation() {
        let mut engine = ExecutionEngine::default();
        engine
            .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
            .expect("designate the single live strategy");

        // A different strategy, and the safe default with no designation, both
        // route to the internal simulation engine — never the broker.
        assert_eq!(
            engine.route_destination(&StrategyId::new("paper-mean-rev-7")),
            OrderRoute::InternalSimulation
        );
        let empty = ExecutionEngine::default();
        assert_eq!(
            empty.route_destination(&StrategyId::new("live-alpha")),
            OrderRoute::InternalSimulation
        );
    }

    #[test]
    fn order_routing_dispatch_non_live_hits_simulation_only_never_ib() {
        // A non-live strategy is dispatched: it must hit the simulation port and
        // never reach the IB-order-creating broker port (panic-on-touch
        // `ForbiddenBroker` — the AC-10 invariant). Read-only connectivity/
        // freshness gates are benign (SRS-MD-004's simulated-order stale gate
        // will legitimately consult freshness on the paper path).
        let engine = ExecutionEngine::default();
        let sim = SimulationSpy::default();
        let events = EventSinkSpy::default();
        let stale_events = StaleEventSinkSpy::default();

        let receipt = engine
            .dispatch_order(
                submission("paper-mean-rev-7", "TSLA", 5),
                &ForbiddenBroker,
                &AlwaysConnected,
                &events,
                &AlwaysFresh,
                &stale_events,
                &sim,
            )
            .expect("a non-live strategy routes to the internal simulation engine");

        assert_eq!(
            receipt,
            OrderRoutingReceipt::Simulated(SimulatedOrderReceipt {
                sim_order_id: "sim-paper-mean-rev-7-TSLA".to_string(),
            })
        );
        assert_eq!(sim.calls.get(), 1);
        assert!(events.events.borrow().is_empty());
        assert!(stale_events.events.borrow().is_empty());
    }

    #[test]
    fn order_routing_dispatch_rejects_malformed_order_before_any_port() {
        // SRS-EXE-003 — a malformed order (here a non-positive quantity) is
        // rejected at the shared dispatch entry and reaches NEITHER the broker
        // nor the simulation port. The simulation port (which would otherwise
        // accept it) must record zero calls — live/paper validation parity is
        // enforced HERE, not left to the downstream port impl.
        let engine = ExecutionEngine::default();
        let sim = SimulationSpy::default();
        let events = EventSinkSpy::default();
        let stale_events = StaleEventSinkSpy::default();

        let err = engine
            .dispatch_order(
                submission("paper-mean-rev-7", "TSLA", 0),
                &ForbiddenBroker,
                &AlwaysConnected,
                &events,
                &AlwaysFresh,
                &stale_events,
                &sim,
            )
            .expect_err("a malformed order must fail closed before any routing port");
        assert_eq!(err.error_type, "NonPositiveQuantity");
        assert_eq!(
            sim.calls.get(),
            0,
            "a malformed order must never reach the simulation port"
        );
    }

    #[test]
    fn order_routing_dispatch_designated_hits_ib_only_never_simulation() {
        // The designated live strategy is dispatched: it must reach the broker
        // and NEVER touch the simulation port (panic-on-touch stub).
        let mut engine = ExecutionEngine::default();
        engine
            .designate(StrategyId::new("live-alpha"), confirm("live-alpha"))
            .expect("designate the single live strategy");

        let broker = BrokerageSpy::default();
        let events = EventSinkSpy::default();
        let stale_events = StaleEventSinkSpy::default();

        let receipt = engine
            .dispatch_order(
                submission("live-alpha", "AAPL", 10),
                &broker,
                &AlwaysConnected,
                &events,
                &AlwaysFresh,
                &stale_events,
                &ForbiddenSimulation,
            )
            .expect("the designated live strategy routes to the live IB gate");

        assert_eq!(
            receipt,
            OrderRoutingReceipt::Live(OrderReceipt {
                broker_order_id: "ib-AAPL".to_string(),
            })
        );
        assert_eq!(broker.calls.get(), 1);
    }
}
