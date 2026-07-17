//! Kill-switch liquidation-timeout **composition layer** (SRS-SAFE-002, SyRS
//! SYS-44b, StRS SN-1.11) — the orchestrator wiring that binds the
//! execution-layer timeout gate (`atp-execution::resolve_kill_switch_timeout`)
//! to its concrete ports. The orchestrator is the one crate allowed to see
//! the execution gate, the adapter boundary (`atp-adapters`), and the
//! notification dispatcher (`atp-notification`) at once (SRS-ARCH-002 keeps
//! the lower crates independent of each other).
//!
//! Concrete port implementations that live here:
//!
//! * [`RealProbeClock`] / [`SimulatedProbeClock`] — the injected timing
//!   authority for the REAL `PollingLiquidationProbe` wait loop. The CLI and
//!   every test drive the simulated clock, so a full 30 s SYS-44b drill
//!   completes instantly while still executing the real loop.
//! * [`IbGatewayLiquidationCleanup`] — the REAL `IbLiquidationCleanup`: routes
//!   the timeout-branch cancel to `IbGatewayConnection::cancel_order` (by the
//!   broker order id bound at submit time) and the disconnect to
//!   [`IbConnectionControl::disconnect`]. Generic over the gateway, so the
//!   SYS-44b scenario drives it with the deterministic [`FixtureIbGateway`]
//!   and the live runtime binds the operator-gated SRS-EXE-006 transport.
//! * [`NotifierAlertSink`] — the REAL `KillSwitchOperatorAlertSink`: builds a
//!   `CriticalFailure` trigger carrying the full unfilled-order details and
//!   dispatches it through the REAL SRS-NOTIF-001 `OperatorNotifier` over
//!   exactly the required email + SMS channel pair. Only the channel
//!   *transports* are fixtures ([`FixtureEmailChannel`] /
//!   [`FixtureSmsChannel`]) — the concrete SMTP/SMS adapters are the deferred
//!   SRS-NOTIF-001 leg, and these types never claim otherwise.
//! * [`FixtureFillFeed`] — the deterministic **mocked-IB order-state source**
//!   SRS-SAFE-002's own verification Step 2 prescribes ("integration or
//!   fault-injection workflows using mocked IB/data-provider services"):
//!   presents the liquidation order `Acked` until a scripted fill instant,
//!   with injectable coverage and reconcile faults.
//!
//! [`run_fixture_timeout`] drives the REAL gate + the REAL polling probe over
//! these ports; `safe002_liquidation_timeout_cli` exposes it to the operator
//! layer (the Python `atp_safety` timeout backend shells it).

use std::cell::{Cell, RefCell};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use atp_adapters::{
    classify_ib_order_error, AdapterError, AdapterResult, DataBatch, HistoricalDataRequest,
    HistoricalQueryResult, IbApiError, IbConnectionControl, IbGatewayConnection,
    MarketDataSubscription, SubscriptionReceipt,
};
use atp_execution::{
    BrokerOpenOrder, BrokerOpenOrderSnapshot, BrokerOpenOrderSource, BrokerReconcileError,
    ExecutionEngine, IbLiquidationCleanup, KillSwitchLiquidationProbe,
    KillSwitchLiquidationResolved, KillSwitchOperatorAlertSink, KillSwitchProbeClock,
    KillSwitchProbeError, KillSwitchSideEffectError, KillSwitchTimeoutEventSink,
    PollingLiquidationProbe, SnapshotCoverage,
};
use atp_notification::{
    ChannelError, ChannelReceipt, ChannelSendResult, NotificationChannel,
    NotificationChannelClient, NotificationEvent, NotificationMessage, NotificationTrigger,
    OperatorNotifier, SharedChannelClient,
};
use atp_types::{
    ClientCorrelationId, CompositeOrderSubmission, KillSwitchAlertEvent,
    KillSwitchLiquidationOutcome, KillSwitchTimeoutEvent, KillSwitchTimeoutRequest,
    OrderErrorCategory, OrderKey, OrderReceipt, OrderState, OrderSubmission, StrategyId,
    StructuredKillSwitchTimeoutError, UnfilledLiquidationOrder,
    KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
};

// --------------------------------------------------------------------------- //
// Probe clocks
// --------------------------------------------------------------------------- //

/// Real monotonic probe clock: `wait_ms` sleeps. Production binding for the
/// 30 s wait loop; never used by tests or the CLI (they must not sleep).
#[derive(Debug)]
pub struct RealProbeClock {
    origin: Instant,
}

impl RealProbeClock {
    pub fn start() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for RealProbeClock {
    fn default() -> Self {
        Self::start()
    }
}

impl KillSwitchProbeClock for RealProbeClock {
    fn monotonic_ms(&self) -> u64 {
        u64::try_from(self.origin.elapsed().as_millis()).unwrap_or(u64::MAX)
    }

    fn wait_ms(&self, ms: u64) {
        std::thread::sleep(Duration::from_millis(ms));
    }
}

/// Simulated probe clock: `wait_ms` advances the reading instead of sleeping,
/// so the REAL wait loop runs its full 30 s window instantly. The CLI and the
/// SYS-44b scenario both use this.
#[derive(Debug, Default)]
pub struct SimulatedProbeClock {
    now_ms: Cell<u64>,
}

impl SimulatedProbeClock {
    pub fn now_ms(&self) -> u64 {
        self.now_ms.get()
    }
}

impl KillSwitchProbeClock for SimulatedProbeClock {
    fn monotonic_ms(&self) -> u64 {
        self.now_ms.get()
    }

    fn wait_ms(&self, ms: u64) {
        self.now_ms.set(self.now_ms.get() + ms);
    }
}

// --------------------------------------------------------------------------- //
// Mocked-IB order-state source (the probe's fill feed)
// --------------------------------------------------------------------------- //

/// Deterministic mocked-IB broker order-state source: the liquidation order
/// presents `Acked` until `fill_at_ms` on the shared simulated clock, then
/// `Filled`. Injectable coverage + reconcile fault; records the poll count.
pub struct FixtureFillFeed<'a> {
    clock: &'a SimulatedProbeClock,
    order_key: OrderKey,
    broker_order_id: String,
    fill_at_ms: Option<u64>,
    coverage: SnapshotCoverage,
    error: Option<BrokerReconcileError>,
    polls: Cell<u32>,
}

impl<'a> FixtureFillFeed<'a> {
    pub fn new(
        clock: &'a SimulatedProbeClock,
        order_key: OrderKey,
        broker_order_id: impl Into<String>,
        fill_at_ms: Option<u64>,
    ) -> Self {
        Self {
            clock,
            order_key,
            broker_order_id: broker_order_id.into(),
            fill_at_ms,
            coverage: SnapshotCoverage::OpenAndRecentlyCompleted,
            error: None,
            polls: Cell::new(0),
        }
    }

    pub fn with_error(mut self, error: BrokerReconcileError) -> Self {
        self.error = Some(error);
        self
    }

    pub fn polls(&self) -> u32 {
        self.polls.get()
    }
}

impl BrokerOpenOrderSource for FixtureFillFeed<'_> {
    fn open_orders(&self) -> Result<BrokerOpenOrderSnapshot, BrokerReconcileError> {
        self.polls.set(self.polls.get() + 1);
        if let Some(error) = &self.error {
            return Err(error.clone());
        }
        let filled = self
            .fill_at_ms
            .is_some_and(|at| self.clock.monotonic_ms() >= at);
        let state = if filled {
            OrderState::Filled
        } else {
            OrderState::Acked
        };
        Ok(BrokerOpenOrderSnapshot::new(
            vec![BrokerOpenOrder {
                key: self.order_key.clone(),
                broker_order_id: self.broker_order_id.clone(),
                state,
            }],
            self.coverage,
        ))
    }
}

// --------------------------------------------------------------------------- //
// Fixture IB gateway (cancel + disconnect transport double)
// --------------------------------------------------------------------------- //

const FIXTURE_UNSUPPORTED: i32 = -1;

/// Deterministic mocked-IB gateway for the SYS-44b cleanup path: records the
/// cancel/disconnect call order, injects per-call faults. The wire-operation
/// methods outside the timeout path return an honest fixture error — the REAL
/// transport is the operator-gated SRS-EXE-006 `TcpIbGateway`.
#[derive(Default)]
pub struct FixtureIbGateway {
    pub fail_cancel: Option<String>,
    pub fail_disconnect: Option<String>,
    calls: RefCell<Vec<String>>,
}

impl FixtureIbGateway {
    pub fn recorded_calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }

    fn unsupported(&self, operation: &str) -> IbApiError {
        IbApiError::new(
            FIXTURE_UNSUPPORTED,
            format!("fixture gateway: `{operation}` is not part of the SYS-44b timeout path"),
        )
    }
}

impl IbGatewayConnection for FixtureIbGateway {
    fn submit_order(&self, _order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        Err(self.unsupported("submit_order"))
    }

    fn submit_composite_order(
        &self,
        _order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        Err(self.unsupported("submit_composite_order"))
    }

    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError> {
        self.calls
            .borrow_mut()
            .push(format!("cancel:{broker_order_id}"));
        match &self.fail_cancel {
            Some(reason) => Err(IbApiError::new(
                FIXTURE_UNSUPPORTED,
                format!("fixture: injected cancel failure — {reason}"),
            )),
            None => Ok(()),
        }
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

impl IbConnectionControl for FixtureIbGateway {
    fn disconnect(&self) -> AdapterResult<()> {
        self.calls.borrow_mut().push("disconnect".to_string());
        match &self.fail_disconnect {
            // The seam's contract: failures cross the adapter boundary as the
            // canonical AdapterError taxonomy with the SYS-64 classification
            // (a wedged/unreachable session is the connectivity family).
            Some(reason) => Err(AdapterError::Brokerage {
                adapter: "fixture_gateway",
                category: Some(OrderErrorCategory::ConnectivityBlocked),
                code: FIXTURE_UNSUPPORTED,
                message: format!("fixture: injected disconnect failure — {reason}"),
            }),
            None => Ok(()),
        }
    }
}

// --------------------------------------------------------------------------- //
// The REAL IbLiquidationCleanup over the adapter boundary
// --------------------------------------------------------------------------- //

/// The concrete `IbLiquidationCleanup`: cancel → `IbGatewayConnection::
/// cancel_order` (via the submit-time broker-order-id binding), disconnect →
/// [`IbConnectionControl::disconnect`]. The domain→broker order-id map is the
/// same binding `LiveExecutionState::broker_id` holds on the live path; the
/// scenario/CLI supplies it explicitly.
pub struct IbGatewayLiquidationCleanup<C: IbGatewayConnection + IbConnectionControl> {
    gateway: C,
    broker_order_ids: BTreeMap<String, String>,
}

impl<C: IbGatewayConnection + IbConnectionControl> IbGatewayLiquidationCleanup<C> {
    pub fn new(gateway: C, broker_order_ids: BTreeMap<String, String>) -> Self {
        Self {
            gateway,
            broker_order_ids,
        }
    }

    pub fn gateway(&self) -> &C {
        &self.gateway
    }

    /// Map a raw wire-seam failure onto the canonical adapter taxonomy first
    /// (`classify_ib_order_error` → `AdapterError::Brokerage`), THEN reduce to
    /// the gate's side-effect reason — so the SYS-64 classification (e.g.
    /// `CONNECTIVITY_BLOCKED`) survives onto the safety event instead of being
    /// laundered into an unclassified string.
    fn cancel_side_effect_error(error: IbApiError) -> KillSwitchSideEffectError {
        let classified = AdapterError::Brokerage {
            // Vendor-neutral composition label — the vendor identity lives in
            // the adapter crate, not this core path.
            adapter: "liquidation_cleanup_gateway",
            category: classify_ib_order_error(&error),
            code: error.code,
            message: error.message,
        };
        KillSwitchSideEffectError::new(format!("IB cancel_order failed: {classified}"))
    }
}

impl<C: IbGatewayConnection + IbConnectionControl> IbLiquidationCleanup
    for IbGatewayLiquidationCleanup<C>
{
    fn cancel_unfilled_liquidation_order(
        &self,
        request: &KillSwitchTimeoutRequest,
    ) -> Result<(), KillSwitchSideEffectError> {
        let order_id = request.unfilled_order.order_id.as_str();
        // A missing binding is an OBSERVABLE failure (recorded as Failed on the
        // timeout event) — never a silent skip. The gate still disconnects.
        let broker_order_id = self.broker_order_ids.get(order_id).ok_or_else(|| {
            KillSwitchSideEffectError::new(format!(
                "no broker order id bound for liquidation order {order_id} — cannot cancel on IB"
            ))
        })?;
        self.gateway
            .cancel_order(broker_order_id)
            .map_err(Self::cancel_side_effect_error)
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        // The IbConnectionControl seam already speaks the canonical
        // AdapterError taxonomy — its Display carries the SYS-64 category.
        self.gateway.disconnect().map_err(|error| {
            KillSwitchSideEffectError::new(format!("IB disconnect failed: {error}"))
        })
    }
}

// --------------------------------------------------------------------------- //
// The REAL KillSwitchOperatorAlertSink over the SRS-NOTIF-001 dispatcher
// --------------------------------------------------------------------------- //

/// Fixture email transport: records every accepted message (subject + body)
/// so evidence can assert the page content; injectable failure. The concrete
/// SMTP adapter is the deferred SRS-NOTIF-001 leg.
#[derive(Debug, Default)]
pub struct FixtureEmailChannel {
    pub fail: bool,
    sent: Mutex<Vec<NotificationMessage>>,
}

impl FixtureEmailChannel {
    pub fn sent(&self) -> Vec<NotificationMessage> {
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

impl NotificationChannelClient for FixtureEmailChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Email
    }

    fn send(&self, message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
        if self.fail {
            return Err(ChannelError::TransportUnavailable {
                detail: "fixture: injected email transport outage".to_string(),
            });
        }
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(message.clone());
        Ok(ChannelReceipt::new("fixture-email-accept"))
    }
}

/// Fixture SMS transport (see [`FixtureEmailChannel`]).
#[derive(Debug, Default)]
pub struct FixtureSmsChannel {
    pub fail: bool,
    sent: Mutex<Vec<NotificationMessage>>,
}

impl FixtureSmsChannel {
    pub fn sent(&self) -> Vec<NotificationMessage> {
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

impl NotificationChannelClient for FixtureSmsChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Sms
    }

    fn send(&self, message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
        if self.fail {
            return Err(ChannelError::TransportUnavailable {
                detail: "fixture: injected SMS transport outage".to_string(),
            });
        }
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(message.clone());
        Ok(ChannelReceipt::new("fixture-sms-accept"))
    }
}

/// The concrete `KillSwitchOperatorAlertSink`: builds a `CriticalFailure`
/// trigger (never suppressed — the SYS-75 fail-safe, right for a SYS-44b
/// liquidation timeout) carrying the full unfilled-order details and fans it
/// out through the REAL `OperatorNotifier` over exactly the required
/// email + SMS pair. Succeeds only when BOTH channels delivered; any other
/// outcome surfaces as a `Failed` side effect on the timeout event. Every
/// produced `NotificationEvent` is retained as evidence.
pub struct NotifierAlertSink {
    notifier: OperatorNotifier,
    channels: Vec<SharedChannelClient>,
    events: RefCell<Vec<NotificationEvent>>,
}

impl NotifierAlertSink {
    pub fn new(notifier: OperatorNotifier, channels: Vec<SharedChannelClient>) -> Self {
        Self {
            notifier,
            channels,
            events: RefCell::new(Vec::new()),
        }
    }

    pub fn events(&self) -> Vec<NotificationEvent> {
        self.events.borrow().clone()
    }

    fn page_summary(event: &KillSwitchAlertEvent) -> String {
        format!(
            "SRS-SAFE-002 + SyRS SYS-44b: kill-switch liquidation order {order} \
             ({side} {quantity} {symbol}) for live strategy {strategy} stayed \
             UNFILLED past the {timeout} s timeout ({elapsed} s elapsed); the \
             order is being canceled and IB disconnected — positions require \
             MANUAL resolution",
            order = event.unfilled_order.order_id,
            side = event.unfilled_order.side,
            quantity = event.unfilled_order.quantity,
            symbol = event.unfilled_order.symbol,
            strategy = event.live_strategy_id.as_str(),
            timeout = event.timeout_seconds,
            elapsed = event.elapsed_seconds,
        )
    }
}

impl KillSwitchOperatorAlertSink for NotifierAlertSink {
    fn dispatch(&self, event: KillSwitchAlertEvent) -> Result<(), KillSwitchSideEffectError> {
        let detected_at_millis = event.observed_at_seconds.saturating_mul(1_000);
        let trigger =
            NotificationTrigger::critical_failure(Self::page_summary(&event), detected_at_millis);
        let notification = self
            .notifier
            .dispatch(&trigger, detected_at_millis, &self.channels)
            .map_err(|error| {
                KillSwitchSideEffectError::new(format!("SRS-NOTIF-001 dispatch refused: {error}"))
            })?;
        let mut undelivered = Vec::new();
        for channel in [NotificationChannel::Email, NotificationChannel::Sms] {
            let delivered = notification
                .delivery_for(channel)
                .is_some_and(|delivery| delivery.outcome().is_delivered());
            if !delivered {
                undelivered.push(channel.as_str());
            }
        }
        self.events.borrow_mut().push(notification);
        if undelivered.is_empty() {
            Ok(())
        } else {
            Err(KillSwitchSideEffectError::new(format!(
                "operator page not delivered on required channel(s): {}",
                undelivered.join(", ")
            )))
        }
    }
}

// --------------------------------------------------------------------------- //
// Timeout-event sink + scenario driver
// --------------------------------------------------------------------------- //

/// Best-effort in-memory timeout-event sink; the durable SRS-LOG-001 write
/// happens at the Python operator layer (`atp_safety.timeout`).
#[derive(Debug, Default)]
pub struct CollectingTimeoutEventSink {
    events: RefCell<Vec<KillSwitchTimeoutEvent>>,
}

impl CollectingTimeoutEventSink {
    pub fn recorded(&self) -> Vec<KillSwitchTimeoutEvent> {
        self.events.borrow().clone()
    }
}

impl KillSwitchTimeoutEventSink for CollectingTimeoutEventSink {
    fn record(&self, event: KillSwitchTimeoutEvent) -> Result<(), KillSwitchSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// Which probe degradation to inject (fault-injection surface of the
/// scenario; maps onto the typed `BrokerReconcileError` → `KillSwitchProbeError`
/// taxonomy).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProbeFault {
    Connectivity,
    OrderState,
    ProbeTimeout,
}

/// A deterministic SYS-44b timeout scenario: the liquidation order, the
/// deadline, when (if ever) the mocked IB fills it, and which faults to
/// inject on each leg.
#[derive(Debug, Clone)]
pub struct TimeoutScenario {
    pub live_strategy_id: String,
    pub order_correlation_id: String,
    pub symbol: String,
    pub side: String,
    pub quantity: u64,
    pub timeout_seconds: u64,
    pub broker_order_id: String,
    /// `None` → the liquidation never fills (the SYS-44b path engages).
    pub fill_after_seconds: Option<u64>,
    /// Inject a probe failure (fail-closed path; nothing destructive runs).
    pub probe_fault: Option<ProbeFault>,
    /// Inject a lying probe that reports `TimedOutUnfilled` at this many
    /// seconds BEFORE the deadline (the gate must reject it as inconsistent).
    pub premature_timeout_at: Option<u64>,
    pub fail_email: bool,
    pub fail_sms: bool,
    pub fail_cancel: bool,
    pub fail_disconnect: bool,
    /// `false` → simulate a missing domain→broker order-id binding.
    pub bind_broker_order_id: bool,
}

impl TimeoutScenario {
    /// The SYS-44b reference drill: a SELL 250 AAPL liquidation that never
    /// fills inside the default 30 s window; no injected faults.
    pub fn reference_unfilled() -> Self {
        Self {
            live_strategy_id: "live-momentum".to_string(),
            order_correlation_id: "ks-liq-0001".to_string(),
            symbol: "AAPL".to_string(),
            side: "SELL".to_string(),
            quantity: 250,
            timeout_seconds: KILL_SWITCH_LIQUIDATION_TIMEOUT_SECONDS,
            broker_order_id: "B-0001".to_string(),
            fill_after_seconds: None,
            probe_fault: None,
            premature_timeout_at: None,
            fail_email: false,
            fail_sms: false,
            fail_cancel: false,
            fail_disconnect: false,
            bind_broker_order_id: true,
        }
    }

    fn order_key(&self) -> Result<OrderKey, String> {
        let correlation = ClientCorrelationId::new(self.order_correlation_id.clone())
            .map_err(|error| format!("scenario correlation id: {error:?}"))?;
        Ok(OrderKey::new(
            StrategyId::new(self.live_strategy_id.clone()),
            correlation,
        ))
    }

    fn request(&self) -> Result<KillSwitchTimeoutRequest, String> {
        Ok(KillSwitchTimeoutRequest {
            live_strategy_id: StrategyId::new(self.live_strategy_id.clone()),
            unfilled_order: UnfilledLiquidationOrder {
                // The SAFE-001 binding convention: the domain order_id is the
                // OrderKey Display form ("strategy/correlation").
                order_id: self.order_key()?.to_string(),
                symbol: self.symbol.clone(),
                side: self.side.clone(),
                quantity: self.quantity,
            },
            timeout_seconds: self.timeout_seconds,
        })
    }
}

/// A scripted lying probe for the premature-timeout injection — the ONLY
/// place a probe inconsistency can originate (the real polling probe cannot
/// produce one by construction, which is exactly what the gate's hardening
/// pins).
struct LyingProbe {
    reported_elapsed_seconds: u64,
    reported_timeout_seconds: u64,
}

impl KillSwitchLiquidationProbe for LyingProbe {
    fn await_filled_or_timeout(
        &self,
        _request: &KillSwitchTimeoutRequest,
    ) -> Result<KillSwitchLiquidationOutcome, KillSwitchProbeError> {
        Ok(KillSwitchLiquidationOutcome::TimedOutUnfilled {
            elapsed_seconds: self.reported_elapsed_seconds,
            timeout_seconds: self.reported_timeout_seconds,
        })
    }
}

/// Everything a scenario run produces: the gate's result plus the
/// composition-level evidence (notification deliveries, gateway call order,
/// poll count, recorded timeout events).
pub struct FixtureTimeoutRun {
    pub result: Result<KillSwitchLiquidationResolved, Box<StructuredKillSwitchTimeoutError>>,
    pub timeout_events: Vec<KillSwitchTimeoutEvent>,
    pub notifications: Vec<NotificationEvent>,
    pub email_pages: Vec<NotificationMessage>,
    pub sms_pages: Vec<NotificationMessage>,
    pub gateway_calls: Vec<String>,
    pub probe_polls: u32,
    pub simulated_elapsed_ms: u64,
}

/// Drive the REAL `resolve_kill_switch_timeout` gate with the REAL
/// `PollingLiquidationProbe` (on the simulated clock), the REAL
/// `OperatorNotifier` (over fixture email/SMS transports), and the REAL
/// `IbGatewayLiquidationCleanup` (over the fixture gateway).
pub fn run_fixture_timeout(scenario: &TimeoutScenario) -> Result<FixtureTimeoutRun, String> {
    let request = scenario.request()?;
    let order_key = scenario.order_key()?;

    let clock = SimulatedProbeClock::default();
    let mut feed = FixtureFillFeed::new(
        &clock,
        order_key,
        scenario.broker_order_id.clone(),
        scenario
            .fill_after_seconds
            .map(|seconds| seconds.saturating_mul(1_000)),
    );
    if let Some(fault) = scenario.probe_fault {
        feed = feed.with_error(match fault {
            ProbeFault::Connectivity => {
                BrokerReconcileError::connectivity_blocked("fixture: IB gateway unreachable")
            }
            ProbeFault::OrderState => {
                BrokerReconcileError::unavailable("fixture: broker order-state service down")
            }
            ProbeFault::ProbeTimeout => {
                BrokerReconcileError::timeout("fixture: order-state query deadline elapsed")
            }
        });
    }

    let email = Arc::new(FixtureEmailChannel {
        fail: scenario.fail_email,
        ..FixtureEmailChannel::default()
    });
    let sms = Arc::new(FixtureSmsChannel {
        fail: scenario.fail_sms,
        ..FixtureSmsChannel::default()
    });
    let alerts = NotifierAlertSink::new(
        OperatorNotifier::new(),
        vec![
            Arc::clone(&email) as SharedChannelClient,
            Arc::clone(&sms) as SharedChannelClient,
        ],
    );

    let mut bindings = BTreeMap::new();
    if scenario.bind_broker_order_id {
        bindings.insert(
            request.unfilled_order.order_id.clone(),
            scenario.broker_order_id.clone(),
        );
    }
    let cleanup = IbGatewayLiquidationCleanup::new(
        FixtureIbGateway {
            fail_cancel: scenario
                .fail_cancel
                .then(|| "fixture cancel fault".to_string()),
            fail_disconnect: scenario
                .fail_disconnect
                .then(|| "fixture disconnect fault".to_string()),
            ..FixtureIbGateway::default()
        },
        bindings,
    );
    let events = CollectingTimeoutEventSink::default();
    let engine = ExecutionEngine::default();
    // The operator-facing observation stamp (epoch seconds). Wall-clock,
    // distinct from the monotonic probe clock.
    let observed_at_seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0);

    let result = match scenario.premature_timeout_at {
        Some(reported_elapsed_seconds) => {
            let lying_probe = LyingProbe {
                reported_elapsed_seconds,
                reported_timeout_seconds: scenario.timeout_seconds,
            };
            engine.resolve_kill_switch_timeout(
                request,
                &lying_probe,
                &alerts,
                &cleanup,
                &events,
                observed_at_seconds,
            )
        }
        None => {
            let probe = PollingLiquidationProbe::new(&clock, &feed);
            engine.resolve_kill_switch_timeout(
                request,
                &probe,
                &alerts,
                &cleanup,
                &events,
                observed_at_seconds,
            )
        }
    };

    Ok(FixtureTimeoutRun {
        result,
        timeout_events: events.recorded(),
        notifications: alerts.events(),
        email_pages: email.sent(),
        sms_pages: sms.sent(),
        gateway_calls: cleanup.gateway().recorded_calls(),
        probe_polls: feed.polls(),
        simulated_elapsed_ms: clock.now_ms(),
    })
}
