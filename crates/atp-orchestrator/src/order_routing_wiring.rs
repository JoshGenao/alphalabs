//! SRS-EXE-002 — orchestrator wiring of the order-routing dispatch authority
//! (SyRS SYS-2b / SYS-2e, AC-10; StRS SN-1.06 / SN-1.29 / C-11).
//!
//! `atp-execution` owns the routing DECISION (`ExecutionEngine::route_destination`
//! → `dispatch_order`): the single designated live strategy routes to the live
//! IB gate; **every** non-live strategy routes to the internal simulation
//! engine through the [`InternalSimulationSubmit`] port and never reaches an
//! IB-order-creating path. This module is the COMPOSITION half the execution
//! crate deliberately cannot hold (SRS-ARCH-002 keeps `atp-execution`
//! independent of `atp-simulation` and `atp-adapters`; the orchestrator is the
//! one layer allowed to see both sides):
//!
//! * [`WiredPaperSimulation`] — the real SRS-SIM-001 `PaperSimulationEngine`
//!   bound behind the `InternalSimulationSubmit` port, routing every accepted
//!   paper order through [`VirtualOrderBook::place_accepted`] so the book is
//!   the SINGLE order store every accepted order flows through (the
//!   SRS-DATA-021 corporate-action path reads this book; an order can enter it
//!   only via the engine's own intake, never by construction around it).
//! * [`IbBrokerageBridge`] — the real SRS-EXE-006 `InteractiveBrokersBrokerage`
//!   bound behind the `LiveBrokerageSubmit` port, so the live leg of a
//!   dispatch exercises the same adapter validation the operator-gated IB
//!   paper-account test proves against the real gateway.
//! * [`RecordingIbGateway`] — the deterministic mocked-IB transport double for
//!   scenario verification: it records every order-creating wire operation, so
//!   "paper strategy orders never create IB orders" is observable as a
//!   wire-level count of ZERO. The REAL socket transport stays the
//!   operator-gated SRS-EXE-006 `TcpIbGateway` (`ib-live-transport` feature +
//!   `ATP_RUN_INTEGRATION=1` + `--ignored`); nothing in this module dials it.
//! * [`ScriptedIbGateway`] — the SRS-ERR-001 counterpart: a transport that
//!   returns a PROGRAMMED vendor error, making the broker-side reject paths
//!   reachable so the SyRS SYS-64 categories can be proven to arrive inside a
//!   `StructuredOrderError`. It supplies only `code` + `message`; the REAL
//!   adapter classifier and the REAL bridge do the classification.
//! * [`run_routing_scenario`] — the operator VERIFICATION entry the
//!   `exe002_order_routing_cli` binary drives: N paper strategies
//!   (+ optionally the one designated live strategy) submitted through the
//!   REAL `ExecutionEngine::dispatch_order`, returning per-order routing
//!   evidence. This is deterministic fixture verification (the scenario
//!   authors its own synthetic strategy ids) — NOT the deployed
//!   strategy-runtime order path; real strategy-container submissions
//!   through `dispatch_order` stay explicitly deferred to the SRS-SDK
//!   strategy host / SRS-ORCH-* runtime.
//!
//! Trusted-capability binding (adversarial R4 on `order_routing_contract`):
//! [`WiredPaperSimulation`] holds the CONCRETE `PaperSimulationEngine` — a
//! caller cannot substitute a broker-backed `InternalSimulationSubmit`
//! implementation through this wiring, and `atp-simulation` structurally
//! depends on neither `atp-execution` nor `atp-adapters`
//! (`tools/sim_fill_check.py`), so no brokerage adapter is reachable behind
//! the wired engine.
//!
//! Still deferred with named owners (see `order_routing_contract.deferred[]`):
//! the DEPLOYED strategy-runtime order path — real strategy containers
//! submitting through `dispatch_order`, authored by the Python strategy host
//! (SRS-SDK runtime / SRS-ORCH-*), the
//! `ClientCorrelationId` idempotency key on the shared envelope (SRS-EXE-008),
//! the simulated-order stale-data gate (SRS-MD-004), the live composite
//! multi-leg envelope (SRS-EXE-004), and the SIM-002 fill-loop evolution that
//! rests limit/stop orders for later fills (today's shipped SIM-002 flow fills
//! marketable orders instantly; the book rests what routing accepted).

use std::cell::{Cell, RefCell};
use std::sync::Mutex;

use atp_adapters::{
    AdapterError, DataBatch, HistoricalDataRequest, HistoricalQueryResult, IbApiError,
    IbGatewayConnection, InteractiveBrokersBrokerage, MarketDataSubscription, SubscriptionReceipt,
};
use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, InternalSimulationSubmit,
    LiveBrokerageSubmit, LiveDesignationConfirmation, MarketDataFreshnessProbe,
    OrderRoutingReceipt, SimulatedOrderReceipt, StaleDataEventSink,
};
use atp_simulation::paper_order::{OrderError, OrderLeg, PaperOrderRequest};
use atp_simulation::sim::PaperSimulationEngine;
use atp_simulation::virtual_orders::VirtualOrderBook;
use atp_types::{
    AssetClass, CompositeOrderSubmission, ConnectivityEvent, ConnectivityState,
    MarketDataFreshness, OrderErrorCategory, OrderReceipt, OrderSide, OrderSubmission, OrderType,
    StaleDataEvent, StrategyId, StructuredOrderError,
};

// --------------------------------------------------------------------------- //
// The wired internal simulation engine (the paper side of the dispatch)
// --------------------------------------------------------------------------- //

/// The real SRS-SIM-001 `PaperSimulationEngine` wired behind the
/// `InternalSimulationSubmit` port, with the SRS-DATA-021 [`VirtualOrderBook`]
/// as the single store every accepted order rests in.
///
/// The constructor builds the concrete engine internally: the port
/// implementation is bound to the one type whose intake is structurally
/// broker-free (`OrderRouting` has exactly one variant, `InternalSimulation`),
/// which is the trusted-capability half of the AC-10 guarantee.
#[derive(Debug, Default)]
pub struct WiredPaperSimulation {
    engine: PaperSimulationEngine,
    book: Mutex<VirtualOrderBook>,
}

impl WiredPaperSimulation {
    /// Bind a fresh real `PaperSimulationEngine` + empty order book.
    pub fn new() -> Self {
        Self::default()
    }

    /// The wired engine (read access — e.g. for driving SIM-002 fills over
    /// accepted orders).
    pub fn engine(&self) -> &PaperSimulationEngine {
        &self.engine
    }

    /// Locked access to the single order store — crate-internal on purpose:
    /// the ONLY mutation path is `submit_simulated` →
    /// [`VirtualOrderBook::place_accepted`] (through the engine's own intake).
    /// Exposing a mutable guard publicly would let a caller rest orders
    /// around the intake via `VirtualOrderBook::place`, breaking the
    /// intake-only invariant this wiring proves (adversarial r2). The
    /// SRS-DATA-021 corporate-action apply surface (`apply_and_emit` over
    /// this book) is added as a NARROW method when that owner adopts this
    /// runtime — not as raw book access.
    ///
    /// Panics if the lock is poisoned — a poisoned book means a routing write
    /// died mid-update, and refusing to route further orders is the
    /// fail-closed response.
    fn book_guard(&self) -> std::sync::MutexGuard<'_, VirtualOrderBook> {
        self.book
            .lock()
            .expect("SRS-EXE-002: virtual order book lock poisoned")
    }

    /// Read-only view of the single order store (evidence / inspection). The
    /// closure receives `&VirtualOrderBook`, so a caller can observe resting
    /// orders but cannot insert around the intake.
    pub fn with_book<R>(&self, read: impl FnOnce(&VirtualOrderBook) -> R) -> R {
        read(&self.book_guard())
    }

    /// How many accepted paper orders are resting open in the book.
    pub fn open_resting_orders(&self) -> usize {
        self.book_guard().open_orders().count()
    }
}

impl InternalSimulationSubmit for WiredPaperSimulation {
    /// Route a non-live strategy's order into the real simulation intake.
    ///
    /// `dispatch_order` already validated the envelope at the shared entry, so
    /// a rejection here is defense-in-depth (the engine's own intake re-applies
    /// the same fail-closed rules), not the primary gate. Fail-closed and
    /// atomic: a rejected submission rests nothing and returns the structured
    /// error; an accepted one rests in the book and returns the simulation
    /// receipt — a type that cannot carry a `broker_order_id`.
    fn submit_simulated(
        &self,
        submission: OrderSubmission,
    ) -> Result<SimulatedOrderReceipt, StructuredOrderError> {
        let request = PaperOrderRequest::Single(OrderLeg {
            symbol: submission.symbol.clone(),
            asset_class: submission.asset_class,
            side: submission.side,
            quantity: submission.quantity,
            order_type: submission.order_type,
        });
        let placed =
            self.book_guard()
                .place_accepted(&submission.strategy_id, &self.engine, &request);
        match placed {
            Ok(ids) => Ok(SimulatedOrderReceipt {
                // A single-leg request rests exactly one order; its book id is
                // the simulation-local identifier (never a broker order id).
                sim_order_id: format!("paper-{}", ids[0]),
            }),
            Err(err) => Err(paper_intake_error(err, submission)),
        }
    }
}

/// Map the simulation intake's fail-closed [`OrderError`] onto the shared
/// SRS-ERR-001 envelope. Every variant is a malformed-order-parameters failure,
/// so the category is `OrderParametersInvalid` — the same category the live arm
/// uses for the same class of failure, which is what makes the SyRS SYS-64
/// "identical for live and paper execution modes" contract true rather than
/// merely intended. The precise reason is the stable `error_type` discriminator.
fn paper_intake_error(err: OrderError, submission: OrderSubmission) -> StructuredOrderError {
    let error_type = match err {
        OrderError::EmptySymbol => "EmptySymbol",
        OrderError::NonPositiveQuantity { .. } => "NonPositiveQuantity",
        OrderError::NonPositiveLimitPrice { .. } => "NonPositiveLimitPrice",
        OrderError::NonPositiveStopPrice { .. } => "NonPositiveStopPrice",
        OrderError::EmptyMultiLeg => "EmptyMultiLeg",
        OrderError::SingleLegComposite => "SingleLegComposite",
        OrderError::NonOptionCompositeLeg => "NonOptionCompositeLeg",
    };
    StructuredOrderError {
        category: OrderErrorCategory::OrderParametersInvalid,
        error_type: error_type.to_string(),
        message: err.to_string(),
        original_order: submission,
    }
}

// --------------------------------------------------------------------------- //
// The wired live brokerage (the live side of the dispatch)
// --------------------------------------------------------------------------- //

/// The real SRS-EXE-006 `InteractiveBrokersBrokerage` bound behind the
/// `LiveBrokerageSubmit` port. The generic transport keeps the binding honest:
/// scenario verification supplies the deterministic [`RecordingIbGateway`];
/// the live deployment supplies the operator-gated `TcpIbGateway` — the
/// adapter validation in between (SRS-EXE-003 fail-closed `validate` before
/// the gateway) is identical either way.
#[derive(Debug)]
pub struct IbBrokerageBridge<C: IbGatewayConnection> {
    adapter: InteractiveBrokersBrokerage<C>,
}

impl<C: IbGatewayConnection> IbBrokerageBridge<C> {
    pub fn new(gateway: C) -> Self {
        Self {
            adapter: InteractiveBrokersBrokerage::new(gateway),
        }
    }

    /// The wired gateway transport (read access — scenario evidence reads the
    /// recorded wire operations back through this).
    pub fn gateway(&self) -> &C {
        self.adapter.connection()
    }
}

impl<C: IbGatewayConnection> LiveBrokerageSubmit for IbBrokerageBridge<C> {
    fn submit_order(
        &self,
        submission: OrderSubmission,
    ) -> Result<OrderReceipt, StructuredOrderError> {
        use atp_adapters::BrokerageAdapter;
        self.adapter
            .submit_order(submission.clone())
            .map_err(|err| adapter_error_to_structured(err, submission))
    }
}

/// Map the adapter-boundary [`AdapterError`] taxonomy onto the shared
/// SRS-ERR-001 envelope, preserving the SyRS SYS-64 classification when the
/// vendor error carries one (the SRS-EXE-006 `classify_ib_order_error` decides
/// that; this function never re-classifies a vendor code).
///
/// A rejection the classifier does **not** map carries `BrokerRejected`, with
/// the vendor code and text in `message`: it is surfaced, never dropped — and,
/// equally important, never *mislabelled*. It previously fell back to
/// `InvalidSymbol`, which reported an arbitrary broker rejection as a symbol
/// that does not exist. The SRS-ERR-001 acceptance criterion requires a SyRS
/// category only "when applicable", so borrowing an inapplicable one is a false
/// claim about the failure, not a conservative default.
fn adapter_error_to_structured(
    err: AdapterError,
    submission: OrderSubmission,
) -> StructuredOrderError {
    let (category, error_type) = match &err {
        AdapterError::Brokerage {
            category: Some(category),
            ..
        } => (*category, "IbBrokerageRejection"),
        AdapterError::Brokerage { category: None, .. } => (
            OrderErrorCategory::BrokerRejected,
            "IbUnmappedBrokerageRejection",
        ),
        AdapterError::InvalidOrder { .. } => (
            OrderErrorCategory::OrderParametersInvalid,
            "OrderValidationFailed",
        ),
        AdapterError::NotConfigured { .. } => (
            OrderErrorCategory::ConnectivityBlocked,
            "AdapterNotConfigured",
        ),
        AdapterError::InvalidProviderData { .. } => {
            (OrderErrorCategory::BrokerRejected, "InvalidProviderData")
        }
    };
    StructuredOrderError {
        category,
        error_type: error_type.to_string(),
        message: err.to_string(),
        original_order: submission,
    }
}

// --------------------------------------------------------------------------- //
// Deterministic mocked-IB transport double (scenario verification)
// --------------------------------------------------------------------------- //

const FIXTURE_UNSUPPORTED: i32 = -1;

/// Deterministic mocked-IB gateway for SRS-EXE-002 scenario verification: it
/// accepts order submissions (recording each order-creating wire operation and
/// minting a deterministic broker order id) so the AC-10 evidence — how many
/// IB orders a scenario actually created — is a directly observable count.
/// Every non-order wire operation returns an honest fixture error; the REAL
/// transport is the operator-gated SRS-EXE-006 `TcpIbGateway`.
#[derive(Debug, Default)]
pub struct RecordingIbGateway {
    submits: Cell<u32>,
    calls: RefCell<Vec<String>>,
}

impl RecordingIbGateway {
    pub fn new() -> Self {
        Self::default()
    }

    /// How many order-creating wire submissions reached this gateway.
    pub fn orders_created(&self) -> u32 {
        self.submits.get()
    }

    /// The recorded order-creating wire operations, in call order.
    pub fn recorded_calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }

    fn unsupported(&self, operation: &str) -> IbApiError {
        IbApiError::new(
            FIXTURE_UNSUPPORTED,
            format!("recording gateway: `{operation}` is not part of the SRS-EXE-002 routing path"),
        )
    }
}

impl IbGatewayConnection for RecordingIbGateway {
    fn submit_order(&self, order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        let sequence = self.submits.get() + 1;
        self.submits.set(sequence);
        self.calls.borrow_mut().push(format!(
            "submit:{}:{}",
            order.strategy_id.as_str(),
            order.symbol
        ));
        Ok(OrderReceipt {
            broker_order_id: format!("IB-{sequence}"),
        })
    }

    fn submit_composite_order(
        &self,
        order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        // A composite is still an order-creating wire operation — count it, so
        // no order-creating path through this double is invisible to the
        // AC-10 evidence. (The single-envelope dispatch never calls it; the
        // live composite envelope is the deferred SRS-EXE-004 leg.)
        let sequence = self.submits.get() + 1;
        self.submits.set(sequence);
        self.calls
            .borrow_mut()
            .push(format!("submit-composite:{}", order.strategy_id.as_str()));
        Ok(OrderReceipt {
            broker_order_id: format!("IB-{sequence}"),
        })
    }

    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        Err(self.unsupported("cancel_order"))
    }

    fn subscribe_market_data(
        &self,
        _request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        Err(self.unsupported("subscribe_market_data"))
    }

    fn historical_data(
        &self,
        _request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        Err(self.unsupported("historical_data"))
    }

    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        Err(self.unsupported("account_status"))
    }

    fn positions(&self) -> Result<DataBatch, IbApiError> {
        Err(self.unsupported("positions"))
    }
}

// --------------------------------------------------------------------------- //
// Deterministic REJECTING mocked-IB transport double (SRS-ERR-001 evidence)
// --------------------------------------------------------------------------- //

/// Deterministic mocked-IB gateway that returns a PROGRAMMED vendor error from
/// `submit_order`, so the SRS-ERR-001 broker-side reject paths are reachable
/// without a live gateway.
///
/// This is the transport half of the ERR-001 evidence: the classification it
/// drives is NOT this double's — the programmed [`IbApiError`] is handed to the
/// REAL `InteractiveBrokersBrokerage`, whose REAL `classify_ib_order_error`
/// decides the SyRS SYS-64 category, and the REAL [`IbBrokerageBridge`] builds
/// the envelope. The double supplies only what a socket would have carried
/// (`code` + `message`), which is exactly the SRS-EXE-006 seam
/// (`IbApiError` never crosses the canonical trait boundary).
///
/// Kept separate from [`RecordingIbGateway`] on purpose: that double carries
/// SRS-EXE-002's AC-10 "how many IB orders did this scenario create" evidence,
/// and teaching it to reject would blur what a zero count there means.
#[derive(Debug)]
pub struct ScriptedIbGateway {
    error: IbApiError,
    attempts: Cell<u32>,
}

impl ScriptedIbGateway {
    /// Program the vendor error the next `submit_order` will fail with.
    pub fn rejecting(code: i32, message: impl Into<String>) -> Self {
        Self {
            error: IbApiError::new(code, message.into()),
            attempts: Cell::new(0),
        }
    }

    /// How many order-creating wire submissions reached this gateway. A rejected
    /// submission still ATTEMPTED the wire, so this is deliberately not an
    /// "orders created" count — no receipt is ever minted here.
    pub fn attempts(&self) -> u32 {
        self.attempts.get()
    }

    fn reject<T>(&self) -> Result<T, IbApiError> {
        self.attempts.set(self.attempts.get() + 1);
        Err(self.error.clone())
    }
}

impl IbGatewayConnection for ScriptedIbGateway {
    fn submit_order(&self, _order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        self.reject()
    }

    fn submit_composite_order(
        &self,
        _order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        self.reject()
    }

    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        Err(self.error.clone())
    }

    fn subscribe_market_data(
        &self,
        _request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        Err(self.error.clone())
    }

    fn historical_data(
        &self,
        _request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        Err(self.error.clone())
    }

    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        Err(self.error.clone())
    }

    fn positions(&self) -> Result<DataBatch, IbApiError> {
        Err(self.error.clone())
    }
}

// --------------------------------------------------------------------------- //
// Benign read-only gate fixtures (connectivity / freshness)
// --------------------------------------------------------------------------- //

/// Healthy-connectivity fixture for scenario verification (the live-leg ERR-2
/// gate consults it; the real probe is the market-data/connectivity runtime).
#[derive(Debug, Default)]
pub struct HealthyConnectivityFixture;

impl BrokerageConnectivity for HealthyConnectivityFixture {
    fn state(&self) -> ConnectivityState {
        ConnectivityState::Connected
    }

    fn request_reconnect(&self) {}
}

/// Fresh-market-data fixture for scenario verification (the live-leg ERR-3
/// gate consults it; SRS-MD-004 owns the real probe and the deferred
/// simulated-order staleness gate).
#[derive(Debug, Default)]
pub struct FreshMarketDataFixture;

impl MarketDataFreshnessProbe for FreshMarketDataFixture {
    fn freshness(&self, _symbol: &str) -> MarketDataFreshness {
        MarketDataFreshness::Fresh
    }

    fn staleness_seconds(&self, _symbol: &str) -> u64 {
        0
    }
}

/// Collecting connectivity-event sink (scenario evidence: a healthy scenario
/// records zero blocked-submission events).
#[derive(Debug, Default)]
pub struct CollectingConnectivitySink {
    events: RefCell<Vec<ConnectivityEvent>>,
}

impl CollectingConnectivitySink {
    pub fn recorded(&self) -> usize {
        self.events.borrow().len()
    }
}

impl ConnectivityEventSink for CollectingConnectivitySink {
    fn record(&self, event: ConnectivityEvent) {
        self.events.borrow_mut().push(event);
    }
}

/// Collecting stale-data-event sink (scenario evidence: a fresh scenario
/// records zero staleness blocks).
#[derive(Debug, Default)]
pub struct CollectingStaleDataSink {
    events: RefCell<Vec<StaleDataEvent>>,
}

impl CollectingStaleDataSink {
    pub fn recorded(&self) -> usize {
        self.events.borrow().len()
    }
}

impl StaleDataEventSink for CollectingStaleDataSink {
    fn record(&self, event: StaleDataEvent) {
        self.events.borrow_mut().push(event);
    }
}

// --------------------------------------------------------------------------- //
// The routing scenario (the operator verification entry the CLI drives)
// --------------------------------------------------------------------------- //

/// Upper bound on scenario size: large enough for any AC sweep (the SyRS
/// fleet ceiling is 60 strategies), small enough to reject a degenerate
/// operator input before it allocates.
pub const MAX_SCENARIO_PAPER_ORDERS: u32 = 10_000;

/// The strategy id the scenario designates live (with the explicit operator
/// confirmation SRS-EXE-001 requires) when `designate_live` is set.
pub const SCENARIO_LIVE_STRATEGY: &str = "live-alpha";

/// A deterministic routing-verification scenario: `paper_orders` distinct
/// non-live strategies each submit one order through the REAL
/// `ExecutionEngine::dispatch_order`; with `designate_live`, the single
/// designated live strategy submits one more.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutingScenario {
    pub paper_orders: u32,
    pub designate_live: bool,
}

impl RoutingScenario {
    /// Fail-closed scenario validation: at least one paper order (the AC is
    /// about non-live routing), at most [`MAX_SCENARIO_PAPER_ORDERS`].
    pub fn validate(&self) -> Result<(), String> {
        if self.paper_orders == 0 {
            return Err(
                "scenario requires at least 1 paper order (SRS-EXE-002 routes non-live \
                        strategy orders)"
                    .to_string(),
            );
        }
        if self.paper_orders > MAX_SCENARIO_PAPER_ORDERS {
            return Err(format!(
                "scenario paper-order count {} exceeds the {MAX_SCENARIO_PAPER_ORDERS} bound",
                self.paper_orders
            ));
        }
        Ok(())
    }
}

/// One dispatched order's routing evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutedOrderEvidence {
    pub strategy: String,
    pub symbol: String,
    /// `"internal_simulation"` or `"live_brokerage"` — which destination the
    /// engine dispatched to.
    pub route: &'static str,
    /// The destination's receipt id: a `paper-<book-id>` simulation id or an
    /// `IB-<n>` broker order id.
    pub receipt: String,
}

/// The scenario's aggregate AC-10 evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RoutingEvidence {
    pub orders: Vec<RoutedOrderEvidence>,
    /// Order-creating wire operations recorded by the mocked-IB gateway. The
    /// AC: exactly `1` when the designated live strategy submitted, else `0`.
    pub ib_orders_created: u32,
    /// Orders accepted by (and resting in) the wired simulation engine.
    pub simulated_orders_accepted: u32,
    /// Open resting orders in the single order store after the sweep.
    pub resting_orders: usize,
    /// The designated live strategy, if the scenario designated one.
    pub designated: Option<String>,
}

/// Run a deterministic routing scenario over the REAL components: real
/// `ExecutionEngine` (real `LiveDesignation` authority), real
/// `PaperSimulationEngine` + `VirtualOrderBook` behind the simulation port,
/// real `InteractiveBrokersBrokerage` behind the live port, mocked-IB
/// transport. Every submission enters through `dispatch_order` — the sole
/// order entry this module wires. This is deterministic fixture
/// VERIFICATION: the scenario authors its own synthetic strategy ids; the
/// deployed strategy-runtime order path (real strategy-container
/// submissions) is the deferred SRS-SDK / SRS-ORCH-* leg.
///
/// The four `OrderType`s are cycled across the paper sweep so the
/// envelope→leg mapping is exercised for every variant.
pub fn run_routing_scenario(scenario: &RoutingScenario) -> Result<RoutingEvidence, String> {
    scenario.validate()?;

    let mut engine = ExecutionEngine::default();
    let designated = if scenario.designate_live {
        let live = StrategyId::new(SCENARIO_LIVE_STRATEGY);
        let confirmation = LiveDesignationConfirmation::from_operator(
            live.clone(),
            format!("operator confirms {SCENARIO_LIVE_STRATEGY} live (SRS-EXE-002 scenario)"),
        )
        .map_err(|err| format!("live designation confirmation rejected: {err:?}"))?;
        engine
            .designate(live.clone(), confirmation)
            .map_err(|err| format!("live designation rejected: {err:?}"))?;
        Some(live)
    } else {
        None
    };

    let simulation = WiredPaperSimulation::new();
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let connectivity = HealthyConnectivityFixture;
    let connectivity_events = CollectingConnectivitySink::default();
    let freshness = FreshMarketDataFixture;
    let stale_events = CollectingStaleDataSink::default();

    let mut orders = Vec::new();
    let mut simulated_orders_accepted = 0u32;

    let order_types = [
        OrderType::Market,
        OrderType::Limit {
            limit_price_minor: 10100,
        },
        OrderType::Stop {
            stop_price_minor: 9900,
        },
        OrderType::StopLimit {
            stop_price_minor: 9900,
            limit_price_minor: 9850,
        },
    ];

    for index in 0..scenario.paper_orders {
        let strategy = StrategyId::new(format!("paper-{:03}", index + 1));
        let symbol = format!("SIM{:03}", index + 1);
        let submission = OrderSubmission::new(
            strategy.clone(),
            symbol.clone(),
            10,
            AssetClass::Equity,
            OrderSide::Buy,
            order_types[(index as usize) % order_types.len()],
        );
        let receipt = engine
            .dispatch_order(
                submission,
                &brokerage,
                &connectivity,
                &connectivity_events,
                &freshness,
                &stale_events,
                &simulation,
            )
            .map_err(|err| format!("paper dispatch for `{}` rejected: {err}", strategy.as_str()))?;
        match receipt {
            OrderRoutingReceipt::Simulated(sim) => {
                simulated_orders_accepted += 1;
                orders.push(RoutedOrderEvidence {
                    strategy: strategy.as_str().to_string(),
                    symbol,
                    route: "internal_simulation",
                    receipt: sim.sim_order_id,
                });
            }
            OrderRoutingReceipt::Live(live) => {
                // Structurally unreachable (a non-designated strategy cannot
                // map to the live route); fail loudly if the authority drifts.
                return Err(format!(
                    "SRS-EXE-002 violation: non-live strategy `{}` was dispatched to the \
                     live brokerage (broker order id {})",
                    strategy.as_str(),
                    live.broker_order_id
                ));
            }
        }
    }

    if let Some(live) = &designated {
        let submission = OrderSubmission::new(
            live.clone(),
            "LIVE001",
            10,
            AssetClass::Equity,
            OrderSide::Buy,
            OrderType::Market,
        );
        let receipt = engine
            .dispatch_order(
                submission,
                &brokerage,
                &connectivity,
                &connectivity_events,
                &freshness,
                &stale_events,
                &simulation,
            )
            .map_err(|err| format!("live dispatch for `{}` rejected: {err}", live.as_str()))?;
        match receipt {
            OrderRoutingReceipt::Live(receipt) => orders.push(RoutedOrderEvidence {
                strategy: live.as_str().to_string(),
                symbol: "LIVE001".to_string(),
                route: "live_brokerage",
                receipt: receipt.broker_order_id,
            }),
            OrderRoutingReceipt::Simulated(sim) => {
                return Err(format!(
                    "SRS-EXE-002 violation: the designated live strategy `{}` was dispatched \
                     to the simulation engine (sim order id {})",
                    live.as_str(),
                    sim.sim_order_id
                ));
            }
        }
    }

    Ok(RoutingEvidence {
        orders,
        ib_orders_created: brokerage.gateway().orders_created(),
        simulated_orders_accepted,
        resting_orders: simulation.open_resting_orders(),
        designated: designated.map(|live| live.as_str().to_string()),
    })
}
