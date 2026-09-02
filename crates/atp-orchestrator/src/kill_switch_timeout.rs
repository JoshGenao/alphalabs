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
//!   exactly the required email + push channel pair. Only the channel
//!   *transports* are fixtures ([`FixtureEmailChannel`] /
//!   [`FixturePushChannel`]) — the concrete SMTP/push adapters are the deferred
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
use std::path::PathBuf;
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
    NotificationChannelClient, NotificationEvent, NotificationEventStore, NotificationMessage,
    NotificationTrigger, OperatorNotifier, SharedChannelClient,
};

// One clock abstraction for both notification sinks — the connectivity sink and
// this critical-failure sink measure the same NFR-P6 quantity, so they must not
// grow two definitions of "now".
use crate::connectivity_notification::AlertClock;
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
/// SMTP adapter LANDED with SRS-NOTIF-001
/// (`atp-adapters::notification::SmtpEmailChannel`); this fixture stands in for
/// it so the test stays deterministic and sends nothing.
#[derive(Debug, Default)]
pub struct FixtureEmailChannel {
    pub fail: bool,
    sent: Mutex<Vec<NotificationMessage>>,
}

impl FixtureEmailChannel {
    /// Build one with the transport fault flag set. A sibling composition module cannot use
    /// struct-update syntax here (the recording buffer is private), and this keeps the fault
    /// injectable without widening the field's visibility.
    pub fn with_failure(fail: bool) -> Self {
        Self {
            fail,
            sent: Mutex::new(Vec::new()),
        }
    }

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
        // Mirrors the real IF-10 path: a relay we operate queued it.
        Ok(ChannelReceipt::queued_for_relay("fixture-email-accept"))
    }
}

/// Fixture push transport (see [`FixtureEmailChannel`]).
#[derive(Debug, Default)]
pub struct FixturePushChannel {
    pub fail: bool,
    sent: Mutex<Vec<NotificationMessage>>,
}

impl FixturePushChannel {
    /// See [`FixtureEmailChannel::with_failure`].
    pub fn with_failure(fail: bool) -> Self {
        Self {
            fail,
            sent: Mutex::new(Vec::new()),
        }
    }

    pub fn sent(&self) -> Vec<NotificationMessage> {
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

impl NotificationChannelClient for FixturePushChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Push
    }

    fn send(&self, message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
        if self.fail {
            return Err(ChannelError::TransportUnavailable {
                detail: "fixture: injected push transport outage".to_string(),
            });
        }
        self.sent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push(message.clone());
        Ok(ChannelReceipt::accepted_by_destination(
            "fixture-push-accept",
        ))
    }
}

/// A clock pinned to one instant, for the fixture drill.
///
/// The drill is not NFR-P6 evidence — its transports are fixtures and its
/// outcome self-labels `transports=FIXTURE` — so reading a wall clock here would
/// make the drill's output non-deterministic without making it a measurement.
#[derive(Debug, Clone, Copy)]
pub struct FixedAlertClock(pub u64);

impl AlertClock for FixedAlertClock {
    fn now_millis(&self) -> u64 {
        self.0
    }
}

/// The concrete `KillSwitchOperatorAlertSink`: builds a `CriticalFailure`
/// trigger (never suppressed — the SYS-75 fail-safe, right for a SYS-44b
/// liquidation timeout) carrying the full unfilled-order details and fans it
/// out through the REAL `OperatorNotifier` over exactly the required
/// email + push pair. Succeeds only when BOTH channels delivered; any other
/// outcome surfaces as a `Failed` side effect on the timeout event. Every
/// produced `NotificationEvent` is retained as evidence.
pub struct NotifierAlertSink {
    notifier: OperatorNotifier,
    channels: Vec<SharedChannelClient>,
    clock: Arc<dyn AlertClock>,
    store_dir: Option<PathBuf>,
    events: RefCell<Vec<NotificationEvent>>,
}

impl NotifierAlertSink {
    /// The PRODUCTION constructor: dispatches and DURABLY STORES the event.
    ///
    /// SRS-NOTIF-001's acceptance criterion is "dispatch begins within 60 seconds
    /// of detection **and delivery status is stored as a notification event**".
    /// The CriticalFailure trigger is half of that requirement, and this sink is
    /// its only production producer, so the store binding belongs here rather
    /// than being left to the caller to remember.
    pub fn with_store(
        notifier: OperatorNotifier,
        channels: Vec<SharedChannelClient>,
        clock: Arc<dyn AlertClock>,
        store_dir: PathBuf,
    ) -> Self {
        Self {
            notifier,
            channels,
            clock,
            store_dir: Some(store_dir),
            events: RefCell::new(Vec::new()),
        }
    }

    /// DRILL ONLY: dispatches without persisting.
    ///
    /// Named rather than defaulted, because "the delivery status was not stored"
    /// has to be a decision somebody can grep for, not the shape you get by
    /// forgetting an argument. The one legitimate use is the fixture drill: its
    /// transports are `FixtureEmailChannel` / `FixturePushChannel`, and writing
    /// that into the real notification store would let drill evidence masquerade
    /// as live SYS-44b evidence — which the rest of this path takes care to
    /// prevent (the outcome self-labels `transports=FIXTURE`, and the Python
    /// backend refuses to log a fixture outcome without an explicit opt-in).
    pub fn without_store(
        notifier: OperatorNotifier,
        channels: Vec<SharedChannelClient>,
        clock: Arc<dyn AlertClock>,
    ) -> Self {
        Self {
            notifier,
            channels,
            clock,
            store_dir: None,
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
        // The dispatch instant is READ FROM THE CLOCK, not reused from the
        // detection stamp. Passing `detected_at_millis` for both made
        // `dispatch_latency_millis()` identically zero, so the stored
        // NFR-P6 evidence asserted a perfect SLA no matter how long the page
        // actually took to start — falsifiable evidence, and worse than none,
        // because the only reason to store the latency is so an operator can
        // trust it. Same defect, and same fix, as the connectivity path.
        let dispatch_began_at_millis = self.clock.now_millis().max(detected_at_millis);
        let notification = self
            .notifier
            .dispatch(&trigger, dispatch_began_at_millis, &self.channels)
            .map_err(|error| {
                KillSwitchSideEffectError::new(format!("SRS-NOTIF-001 dispatch refused: {error}"))
            })?;
        let mut undelivered = Vec::new();
        for channel in [NotificationChannel::Email, NotificationChannel::Push] {
            // is_handed_off, not is_delivered: at dispatch time nothing can know
            // more about the email leg than that our relay took it. Holding it
            // to is_delivered would report every correctly-queued operator page
            // as undelivered.
            let delivered = notification
                .delivery_for(channel)
                .is_some_and(|delivery| delivery.outcome().is_handed_off());
            if !delivered {
                undelivered.push(channel.as_str());
            }
        }
        // PERSIST BEFORE REPORTING THE OUTCOME, and persist a FAILED page too:
        // "email did not deliver" is exactly the delivery status the audit trail
        // exists to record. A store failure is itself a failed side effect — the
        // AC's "delivery status is stored" leg is then unmet, and an audit trail
        // that silently loses records is worse than one that admits it.
        let store_error = match &self.store_dir {
            Some(dir) => NotificationEventStore::append_durably(dir, notification.clone())
                .err()
                .map(|error| format!("notification event not stored durably: {error}")),
            None => None,
        };
        self.events.borrow_mut().push(notification);

        match (undelivered.is_empty(), store_error) {
            (true, None) => Ok(()),
            (delivered, stored) => {
                let mut parts = Vec::new();
                if !delivered {
                    parts.push(format!(
                        "operator page not delivered on required channel(s): {}",
                        undelivered.join(", ")
                    ));
                }
                if let Some(detail) = stored {
                    parts.push(detail);
                }
                Err(KillSwitchSideEffectError::new(parts.join("; ")))
            }
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
    pub fail_push: bool,
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
            fail_push: false,
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
    pub push_pages: Vec<NotificationMessage>,
    pub gateway_calls: Vec<String>,
    pub probe_polls: u32,
    pub simulated_elapsed_ms: u64,
}

/// Drive the REAL `resolve_kill_switch_timeout` gate with the REAL
/// `PollingLiquidationProbe` (on the simulated clock), the REAL
/// `OperatorNotifier` (over fixture email/push transports), and the REAL
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
    let push = Arc::new(FixturePushChannel {
        fail: scenario.fail_push,
        ..FixturePushChannel::default()
    });
    // The operator-facing observation stamp (epoch seconds). Wall-clock,
    // distinct from the monotonic probe clock.
    let observed_at_seconds = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0);

    // DRILL: no durable store, deliberately. See `without_store` — writing
    // fixture-transport evidence into the real notification store would let a
    // drill masquerade as live SYS-44b evidence. The clock is pinned to the
    // observed instant for the same reason: a drill is not NFR-P6 evidence, and
    // a wall-clock reading here would make the drill's output non-deterministic
    // while still not being usable as a measurement.
    let alerts = NotifierAlertSink::without_store(
        OperatorNotifier::new(),
        vec![
            Arc::clone(&email) as SharedChannelClient,
            Arc::clone(&push) as SharedChannelClient,
        ],
        Arc::new(FixedAlertClock(observed_at_seconds.saturating_mul(1_000))),
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
        push_pages: push.sent(),
        gateway_calls: cleanup.gateway().recorded_calls(),
        probe_polls: feed.polls(),
        simulated_elapsed_ms: clock.now_ms(),
    })
}

#[cfg(test)]
mod notifier_alert_sink_tests {
    use super::*;
    use atp_types::{OperatorAlertChannel, StrategyId, UnfilledLiquidationOrder};

    fn store_dir(label: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("atp_safe002_notif_{label}"));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("create store dir");
        dir
    }

    fn alert_event(observed_at_seconds: u64) -> KillSwitchAlertEvent {
        KillSwitchAlertEvent {
            live_strategy_id: StrategyId::new("alpha-1"),
            unfilled_order: UnfilledLiquidationOrder {
                order_id: "LIQ-1".to_string(),
                symbol: "AAPL".to_string(),
                side: "SELL".to_string(),
                quantity: 100,
            },
            channels: vec![OperatorAlertChannel::Email, OperatorAlertChannel::Push],
            elapsed_seconds: 31,
            timeout_seconds: 30,
            observed_at_seconds,
        }
    }

    fn delivering_channels() -> Vec<SharedChannelClient> {
        vec![
            Arc::new(FixtureEmailChannel::default()) as SharedChannelClient,
            Arc::new(FixturePushChannel::default()) as SharedChannelClient,
        ]
    }

    /// SRS-NOTIF-001 AC, CriticalFailure half: "delivery status is stored as a
    /// notification event".
    ///
    /// Found by adversarial review. This sink dispatched the SYS-44b page and
    /// kept the event only in an in-memory RefCell, so the kill-switch
    /// liquidation-timeout page — the most serious alert the system sends — left
    /// nothing in the durable audit trail.
    #[test]
    fn a_critical_failure_page_is_stored_durably() {
        let dir = store_dir("stored");
        let sink = NotifierAlertSink::with_store(
            OperatorNotifier::new(),
            delivering_channels(),
            Arc::new(FixedAlertClock(1_000_000)),
            dir.clone(),
        );

        sink.dispatch(alert_event(900)).expect("page delivered");

        let stored = NotificationEventStore::load_from_path(&dir).expect("store reads back");
        assert_eq!(
            stored.len(),
            1,
            "the page must reach the durable audit trail"
        );
        let event = &stored.events()[0];
        assert!(event.summary().contains("SRS-SAFE-002"));
        for channel in [NotificationChannel::Email, NotificationChannel::Push] {
            assert!(
                event
                    .delivery_for(channel)
                    .is_some_and(|d| d.outcome().is_handed_off()),
                "{channel:?} delivery status missing from the stored event"
            );
        }
    }

    /// A FAILED page must be stored too — that is the delivery status.
    #[test]
    fn a_failed_page_is_still_stored_and_still_reported_failed() {
        let dir = store_dir("failed");
        let channels = vec![
            Arc::new(FixtureEmailChannel {
                fail: true,
                ..FixtureEmailChannel::default()
            }) as SharedChannelClient,
            Arc::new(FixturePushChannel::default()) as SharedChannelClient,
        ];
        let sink = NotifierAlertSink::with_store(
            OperatorNotifier::new(),
            channels,
            Arc::new(FixedAlertClock(1_000_000)),
            dir.clone(),
        );

        let error = sink.dispatch(alert_event(900)).expect_err("email failed");
        assert!(format!("{error:?}").contains("EMAIL"), "{error:?}");
        assert_eq!(
            NotificationEventStore::load_from_path(&dir)
                .expect("store reads back")
                .len(),
            1,
            "a failed page is exactly the delivery status the trail must keep"
        );
    }

    /// A store failure is itself a failed side effect, never swallowed.
    #[test]
    fn a_store_failure_surfaces_even_when_both_channels_delivered() {
        let missing = std::env::temp_dir().join("atp_safe002_notif_absent/nope");
        let _ = std::fs::remove_dir_all(missing.parent().expect("parent"));
        let sink = NotifierAlertSink::with_store(
            OperatorNotifier::new(),
            delivering_channels(),
            Arc::new(FixedAlertClock(1_000_000)),
            missing,
        );

        let error = sink
            .dispatch(alert_event(900))
            .expect_err("an unstorable page is not a success");
        assert!(
            format!("{error:?}").contains("not stored durably"),
            "{error:?}"
        );
    }

    /// The stored NFR-P6 latency must come from the clock, not be fabricated.
    ///
    /// Found by adversarial review: `observed_at_seconds * 1000` was passed as
    /// BOTH the detection stamp and the dispatch-began stamp, so
    /// `dispatch_latency_millis()` was identically zero and the stored evidence
    /// asserted a perfect SLA however long the page really took to start.
    #[test]
    fn the_stored_latency_is_measured_not_fabricated_zero() {
        let dir = store_dir("latency");
        // Observed at t=900s; the dispatch does not begin until t=1000s — 100s
        // later, past the 60s NFR-P6 budget.
        let sink = NotifierAlertSink::with_store(
            OperatorNotifier::new(),
            delivering_channels(),
            Arc::new(FixedAlertClock(1_000_000)),
            dir,
        );

        sink.dispatch(alert_event(900)).expect("page delivered");

        let event = &sink.events()[0];
        assert_eq!(event.dispatch_latency_millis(), 100_000);
        assert!(
            !event.within_dispatch_sla(),
            "a page that began 100s after detection must NOT pass NFR-P6"
        );
    }

    /// A clock reading BEFORE detection cannot manufacture a negative latency.
    ///
    /// The dispatcher rejects dispatch-before-detection outright, so a backwards
    /// clock step would turn a real page into a refused side effect. Clamping to
    /// the detection instant keeps the alert going out; the worst it can do is
    /// under-report latency, which is the safe direction here because the page
    /// itself matters more than its timing evidence.
    #[test]
    fn a_backwards_clock_cannot_refuse_the_page() {
        let dir = store_dir("backwards");
        let sink = NotifierAlertSink::with_store(
            OperatorNotifier::new(),
            delivering_channels(),
            Arc::new(FixedAlertClock(1)),
            dir,
        );

        sink.dispatch(alert_event(900))
            .expect("a backwards clock must not stop the page");
        assert_eq!(sink.events()[0].dispatch_latency_millis(), 0);
    }
}
