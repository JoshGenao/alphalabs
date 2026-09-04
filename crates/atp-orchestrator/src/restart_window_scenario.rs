//! SRS-MD-005 / SyRS SYS-75 — the end-to-end scenario the operator CLI and the
//! fault-injection tests drive.
//!
//! One scenario, one instant, all four acceptance clauses observed together:
//!
//! ```text
//!   (a) order submission AND market-data requests suspended from T-60s
//!   (b) connectivity notifications suppressed for the configured window
//!   (c) reconnection attempted
//!   (d) still unavailable after the window -> standard connectivity handling
//! ```
//!
//! Every component in the chain is the production one; the fixtures are named
//! and confined to the transports:
//!
//! ```text
//!   ExecutionEngine::dispatch_order        REAL — the shared order entry
//!     -> route_order                       REAL — resolves live-ness from the
//!                                          engine-owned LiveDesignation, so the
//!                                          single-live invariant is exercised
//!       -> submit_live_order               REAL — the ERR-2 connectivity gate
//!         -> ScheduledRestartConnectivity  REAL — the SRS-MD-005 producer
//!           -> TcpGatewayReachability      REAL — a bounded TCP probe against
//!                                          a REAL endpoint (the fault is a
//!                                          genuinely dead port, not a stub)
//!         -> IbBrokerageBridge/RecordingIbGateway
//!                                          REAL bridge; deterministic mocked
//!                                          transport, the wire-attempt witness
//!   MarketDataSubscriptionManager          REAL — the SYS-75(a) market-data half
//!   ConnectivityNotifierSink               REAL — the SYS-75(b) suppression
//!     -> FixtureEmailChannel/FixturePushChannel
//!                                          FIXTURE transports, so a drill sends
//!                                          nothing and can never be mistaken for
//!                                          a live page
//! ```
//!
//! The SYS-75 clock is injected, so every phase decision, every gate verdict
//! and every published event is reproducible from `RestartWindowScenario`
//! alone — a run at 09:00 and a run at midnight produce identical evidence.
//!
//! One wall-clock read remains, and it is worth naming rather than glossing:
//! `ConnectivityNotifierSink` takes a `SystemAlertClock`, whose `now_millis`
//! calls `SystemTime::now()` to stamp the alert and to compare against its
//! coalescing cool-down. It does not reach the evidence, because each run
//! constructs a FRESH sink with no previous dispatch, so no cool-down can be
//! armed and nothing is ever coalesced. Injecting a fixture clock there would
//! swap the REAL SRS-NOTIF-001 dispatch path for a differently-configured one,
//! a worse trade for a scenario whose whole point is that the suppression
//! decision is taken by production code.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use atp_adapters::gateway_reachability::{ReachabilityOutcome, TcpGatewayReachability};
use atp_execution::{
    BrokerageConnectivity, ConnectivityEventSink, ExecutionEngine, LiveDesignationConfirmation,
    OrderRoutingReceipt,
};
use atp_market_data::MarketDataSubscriptionManager;
use atp_notification::{DeliveryOutcome, OperatorNotifier, SharedChannelClient};
use atp_types::{
    AssetClass, ConnectivityEvent, ConnectivityState, MarketDataAdmission, OrderSide,
    OrderSubmission, OrderType, RestartPhase, RestartWindow, RestartWindowError, StrategyId,
    SubscriptionRequest,
};

use crate::connectivity_notification::{
    ConnectivityAlertOutcome, ConnectivityNotifierSink, SystemAlertClock,
};
use crate::kill_switch_timeout::{FixtureEmailChannel, FixturePushChannel};
use crate::order_routing_wiring::{
    CollectingStaleDataSink, FreshMarketDataFixture, IbBrokerageBridge, RecordingIbGateway,
    WiredPaperSimulation, SCENARIO_LIVE_STRATEGY, SCENARIO_PAPER_CONTRAST_STRATEGY,
};
use crate::restart_window_connectivity::ScheduledRestartConnectivity;

/// The symbol both the order and the subscription use, so one instant produces
/// one consistent verdict about one security.
pub const SCENARIO_SYMBOL: &str = "AAPL";

/// How long the scenario waits for the notification worker to settle. The sink
/// dispatches off the caller's thread on purpose (reporting a problem must not
/// extend it), so the evidence has to join before reading channel buffers.
const ALERT_FLUSH_TIMEOUT: Duration = Duration::from_secs(5);

/// The line ceiling used for the scenario's subscription probe. Generous on
/// purpose: this scenario is about the restart window, and a limit refusal here
/// would be a different feature's evidence wearing this one's label.
const SCENARIO_LINE_LIMIT: u32 = 100;

/// What the live submission did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LiveOrderOutcome {
    Blocked {
        category: String,
        error_type: String,
        message: String,
    },
    RoutedThrough {
        broker_order_id: String,
    },
}

/// What the operator alert path did with the connectivity event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AlertDisposition {
    /// No event was produced (the gate did not block).
    NoEvent,
    /// An event was produced and every required channel recorded
    /// `Suppressed` — SYS-75(b). Nothing was sent, and the record proves the
    /// dispatcher CHOSE silence rather than dropping the alert.
    Suppressed,
    /// An event was produced and handed to the transports — the SYS-45/SYS-46
    /// escalation.
    Dispatched,
    /// Folded into an earlier alert's cool-down.
    Coalesced,
    /// The dispatch itself failed.
    Failed,
}

impl AlertDisposition {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::NoEvent => "NO_EVENT",
            Self::Suppressed => "SUPPRESSED",
            Self::Dispatched => "DISPATCHED",
            Self::Coalesced => "COALESCED",
            Self::Failed => "FAILED",
        }
    }
}

/// The scenario's inputs. All of them explicit — there is no ambient clock and
/// no ambient endpoint, so a run is reproducible from this struct alone.
#[derive(Debug, Clone, Copy)]
pub struct RestartWindowScenario {
    /// The instant to evaluate at, epoch nanoseconds.
    pub now_ns: i64,
    /// The configured restart instant, epoch nanoseconds.
    pub expected_restart_ns: i64,
    /// SYS-75(a) suspension lead.
    pub lead_ns: i64,
    /// SYS-75(b) window duration.
    pub window_ns: i64,
    /// The endpoint the reachability probe targets. Point it at a dead port to
    /// inject the fault the acceptance criterion is verified by.
    pub gateway_endpoint: SocketAddr,
}

impl RestartWindowScenario {
    fn window(&self) -> Result<RestartWindow, RestartWindowError> {
        RestartWindow::new(self.expected_restart_ns, self.lead_ns, self.window_ns)
    }
}

/// Everything one scenario run observed.
#[derive(Debug, Clone)]
pub struct RestartWindowEvidence {
    pub phase: RestartPhase,
    /// `None` when the phase decided without probing (the SYS-75(a) lead).
    /// Distinct from an unreachable answer: "we did not ask" and "it did not
    /// answer" are different facts, and only the second is about the gateway.
    pub reachability: Option<ReachabilityOutcome>,
    pub connectivity_state: ConnectivityState,
    pub designated: String,
    pub live_outcome: LiveOrderOutcome,
    /// Wire attempts. A blocked submission must create ZERO IB orders — that is
    /// what proves the refusal happened at the gate and not at the broker.
    pub ib_orders_created: u32,
    pub reconnects: u64,
    pub events_recorded: usize,
    pub event_scheduled_restart: Option<bool>,
    /// SYS-75(a), market-data half.
    pub market_data_admitted: bool,
    /// WHY the market-data request was refused. Carried separately from the
    /// boolean because "suspended for scheduled maintenance" and "connectivity
    /// is lost" are opposite instructions to an operator.
    pub market_data_admission: MarketDataAdmission,
    pub market_data_refusal: Option<String>,
    /// Upstream IB lines the REGISTRY holds after the run. Zero while suspended
    /// is the witness that the mutating admission point opened none.
    pub lines_opened: u32,
    /// How `ConsolidatedSubscriptionRegistry::subscribe` refused, if it did.
    pub registry_refusal: Option<String>,
    /// SYS-75(b).
    pub alert_disposition: AlertDisposition,
    /// Messages the fixture transports actually received. Zero is the proof
    /// that suppression suppressed; non-zero is the proof that escalation
    /// escalated.
    pub channel_messages_sent: usize,
    /// The non-designated paper contrast, which must simulate in every phase.
    pub non_designated_sim_receipt: String,
}

impl RestartWindowEvidence {
    /// Whether this run shows planned maintenance rather than an outage.
    pub fn is_planned_maintenance(&self) -> bool {
        self.connectivity_state == ConnectivityState::ScheduledRestartWindow
    }
}

/// Run the scenario once.
///
/// Returns `Err` on a malformed configuration or on any violation of an
/// invariant this scenario is not allowed to observe (a designated live order
/// reaching the simulation engine, a non-designated order reaching the broker).
/// Those are refusals, not evidence: a scenario that reported them as an
/// outcome would let a broken authority chain print a proof line.
pub fn run_restart_window_scenario(
    scenario: &RestartWindowScenario,
) -> Result<RestartWindowEvidence, String> {
    let window = scenario
        .window()
        .map_err(|err| format!("restart window configuration rejected: {err}"))?;
    let now_ns = scenario.now_ns;

    let connectivity = ScheduledRestartConnectivity::new(
        window,
        TcpGatewayReachability::new(scenario.gateway_endpoint),
        move || now_ns,
    );

    // Observe for the record ONLY when the phase needs the answer. Using the
    // unconditional `observe()` here would make the CLI and the L5 suite probe
    // during the lead — the very thing the gates refuse to do — so the
    // guarantee would hold in production and be broken by the tool that reports
    // on it.
    let reachability = connectivity.observe_if_needed();
    let phase = connectivity.phase();
    let connectivity_state = connectivity.state();

    let mut engine = ExecutionEngine::default();
    let live = StrategyId::new(SCENARIO_LIVE_STRATEGY);
    let confirmation = LiveDesignationConfirmation::from_operator(
        live.clone(),
        format!("operator confirms {SCENARIO_LIVE_STRATEGY} live (SRS-MD-005 scenario)"),
    )
    .map_err(|err| format!("live designation confirmation rejected: {err:?}"))?;
    engine
        .designate(live.clone(), confirmation)
        .map_err(|err| format!("live designation rejected: {err:?}"))?;

    let simulation = WiredPaperSimulation::new();
    let brokerage = IbBrokerageBridge::new(RecordingIbGateway::new());
    let freshness = FreshMarketDataFixture;
    let stale_events = CollectingStaleDataSink::default();

    // The REAL SRS-NOTIF-001 sink over FIXTURE transports, so the SYS-75(b)
    // suppression decision is taken by production code rather than restated.
    let email = Arc::new(FixtureEmailChannel::default());
    let push = Arc::new(FixturePushChannel::default());
    let channels: Vec<SharedChannelClient> = vec![
        Arc::clone(&email) as SharedChannelClient,
        Arc::clone(&push) as SharedChannelClient,
    ];
    let alerts = RecordingConnectivitySink::new(ConnectivityNotifierSink::new(
        OperatorNotifier::new(),
        channels,
        SystemAlertClock,
    ));

    // (1) The designated-live order, through the REAL authority chain.
    let live_result = engine.dispatch_order(
        OrderSubmission::new(
            live.clone(),
            SCENARIO_SYMBOL,
            10,
            AssetClass::Equity,
            OrderSide::Buy,
            OrderType::Market,
        ),
        &brokerage,
        &connectivity,
        &alerts,
        &freshness,
        &stale_events,
        &simulation,
    );
    let live_outcome = match live_result {
        Err(err) => LiveOrderOutcome::Blocked {
            category: err.category.as_str().to_string(),
            error_type: err.error_type,
            message: err.message,
        },
        Ok(OrderRoutingReceipt::Live(receipt)) => LiveOrderOutcome::RoutedThrough {
            broker_order_id: receipt.broker_order_id,
        },
        Ok(OrderRoutingReceipt::Simulated(sim)) => {
            return Err(format!(
                "SRS-MD-005 violation: the designated live strategy `{}` was dispatched to the \
                 simulation engine (sim order id {})",
                live.as_str(),
                sim.sim_order_id
            ));
        }
    };

    // (2) The non-designated paper contrast. It must simulate in EVERY phase:
    //     the restart window is about the IB path, and suspending paper
    //     strategies for it would be a different requirement.
    let paper = StrategyId::new(SCENARIO_PAPER_CONTRAST_STRATEGY);
    let paper_receipt = engine
        .dispatch_order(
            OrderSubmission::new(
                paper.clone(),
                SCENARIO_SYMBOL,
                10,
                AssetClass::Equity,
                OrderSide::Buy,
                OrderType::Market,
            ),
            &brokerage,
            &connectivity,
            &alerts,
            &freshness,
            &stale_events,
            &simulation,
        )
        .map_err(|err| {
            format!(
                "SRS-MD-005: the non-designated paper contrast `{}` unexpectedly rejected: {err}",
                paper.as_str()
            )
        })?;
    let non_designated_sim_receipt = match paper_receipt {
        OrderRoutingReceipt::Simulated(sim) => sim.sim_order_id,
        OrderRoutingReceipt::Live(receipt) => {
            return Err(format!(
                "SRS-MD-005 violation: a non-designated paper strategy `{}` routed to the live \
                 brokerage (broker order id {})",
                paper.as_str(),
                receipt.broker_order_id
            ));
        }
    };

    // (3) SYS-75(a), the market-data half — through the REAL subscription
    //     manager AND the REAL registry, both gated by the SAME producer the
    //     order path just consulted.
    let manager = MarketDataSubscriptionManager;
    let mut registry = atp_market_data::ConsolidatedSubscriptionRegistry::new(SCENARIO_LINE_LIMIT);
    let limit_events = CollectingLimitSink::default();
    let request = SubscriptionRequest {
        strategy_id: live.clone(),
        symbol: SCENARIO_SYMBOL.to_string(),
        asset_class: AssetClass::Equity,
    };
    let subscription =
        manager.request_subscription(request.clone(), &connectivity, &registry, &limit_events);
    let market_data_admission = connectivity.market_data_admission();
    let (market_data_admitted, market_data_refusal) = match subscription {
        Ok(_) => (true, None),
        Err(err) => (false, Some(err.error_type)),
    };

    //     And the MUTATING admission point. This feature repeatedly calls
    //     `subscribe` the load-bearing one — it is the call that actually opens
    //     an upstream IB line — so the evidence has to drive it, not just the
    //     outer manager. `lines_opened` is the witness: zero while suspended is
    //     what proves no line was opened, and it is only meaningful because the
    //     same call opens exactly one outside the window.
    let change_events = CollectingChangeSink::default();
    let subscribe_outcome = registry.subscribe(&request, &connectivity, &change_events);
    let lines_opened = registry.distinct_subscriptions();
    let registry_refusal = subscribe_outcome.err().map(|err| format!("{err:?}"));

    // (4) SYS-75(b) — what the alert path did. Join the worker first: the sink
    //     dispatches off the caller's thread, so reading the buffers without
    //     flushing would race and report a suppression that had not happened.
    if !alerts.inner().flush(ALERT_FLUSH_TIMEOUT) {
        return Err(format!(
            "SRS-MD-005: the connectivity alert worker did not settle within {ALERT_FLUSH_TIMEOUT:?} \
             — the disposition below would be a guess"
        ));
    }
    let published = alerts.events();
    let outcomes = alerts.inner().outcomes();
    let alert_disposition = classify_alert(&outcomes);
    let channel_messages_sent = email.sent().len() + push.sent().len();

    let events_recorded = outcomes
        .iter()
        .filter(|outcome| !matches!(outcome, ConnectivityAlertOutcome::NotAnOutage))
        .count();

    Ok(RestartWindowEvidence {
        phase,
        reachability,
        connectivity_state,
        designated: live.as_str().to_string(),
        live_outcome,
        ib_orders_created: brokerage.gateway().orders_created(),
        reconnects: connectivity.reconnect_count(),
        events_recorded,
        // READ from the event the engine published, never re-derived from the
        // state above: the two are separate claims and the requirement is that
        // they AGREE.
        event_scheduled_restart: published
            .first()
            .map(|event: &ConnectivityEvent| event.scheduled_restart),
        market_data_admitted,
        market_data_admission,
        market_data_refusal,
        lines_opened,
        registry_refusal,
        alert_disposition,
        channel_messages_sent,
        non_designated_sim_receipt,
    })
}

/// Collapse the sink's outcome list to one disposition.
///
/// `Dispatched` is split by whether EVERY delivery was `Suppressed`, which is
/// the shape SRS-NOTIF-001 stores for a suppressed event: it records all
/// required channels as `Suppressed` so "we chose silence" stays
/// distinguishable from "we tried and failed". A partial split cannot occur —
/// the store refuses to read one back — so an all-or-nothing test is right.
fn classify_alert(outcomes: &[ConnectivityAlertOutcome]) -> AlertDisposition {
    let mut disposition = AlertDisposition::NoEvent;
    for outcome in outcomes {
        disposition = match outcome {
            ConnectivityAlertOutcome::NotAnOutage => continue,
            ConnectivityAlertOutcome::Coalesced { .. } => AlertDisposition::Coalesced,
            ConnectivityAlertOutcome::Failed { .. } => AlertDisposition::Failed,
            ConnectivityAlertOutcome::Dispatched(event) => {
                let deliveries = event.deliveries();
                if !deliveries.is_empty()
                    && deliveries
                        .iter()
                        .all(|d| matches!(d.outcome(), DeliveryOutcome::Suppressed))
                {
                    AlertDisposition::Suppressed
                } else {
                    AlertDisposition::Dispatched
                }
            }
        };
    }
    disposition
}

/// Tees every `ConnectivityEvent` the engine publishes into a buffer, then
/// forwards it to the real notifier.
///
/// Without this the scenario would DERIVE `scheduled_restart` from the state it
/// already computed, which is a tautology dressed as evidence: the flag would
/// agree with the state by construction, and a gate that published the wrong
/// flag would still pass. The whole point of SRS-NOTIF-001's anti-forgery rule
/// is that the flag and the state are two separate claims which must AGREE, so
/// the evidence has to read the one the engine actually emitted.
#[derive(Debug)]
struct RecordingConnectivitySink<S: ConnectivityEventSink> {
    inner: S,
    events: std::sync::Mutex<Vec<ConnectivityEvent>>,
}

impl<S: ConnectivityEventSink> RecordingConnectivitySink<S> {
    fn new(inner: S) -> Self {
        Self {
            inner,
            events: std::sync::Mutex::new(Vec::new()),
        }
    }

    fn events(&self) -> Vec<ConnectivityEvent> {
        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }

    fn inner(&self) -> &S {
        &self.inner
    }
}

impl<S: ConnectivityEventSink> ConnectivityEventSink for RecordingConnectivitySink<S> {
    fn record(&self, event: ConnectivityEvent) {
        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(event.clone());
        self.inner.record(event);
    }
}

/// Collects registry change events (the mutating admission point's channel).
#[derive(Debug, Default)]
struct CollectingChangeSink {
    events: std::sync::Mutex<Vec<atp_types::SubscriptionChangeEvent>>,
}

impl atp_market_data::SubscriptionChangeSink for CollectingChangeSink {
    fn record(&self, event: atp_types::SubscriptionChangeEvent) {
        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(event);
    }
}

/// Collects subscription-limit events so the scenario can report them rather
/// than panicking inside a shared code path.
#[derive(Debug, Default)]
struct CollectingLimitSink {
    events: std::sync::Mutex<Vec<atp_types::SubscriptionLimitEvent>>,
}

impl atp_market_data::SubscriptionLimitEventSink for CollectingLimitSink {
    fn record(&self, event: atp_types::SubscriptionLimitEvent) {
        self.events
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(event);
    }
}
