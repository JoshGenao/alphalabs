//! Interactive Brokers headless **IB Gateway** brokerage adapter — SRS-EXE-006
//! (SyRS SYS-52 brokerage-adapter interface, SYS-65 version management, AC-2
//! headless-via-TWS-API; StRS C-2, SN-3.02).
//!
//! # What this module is, and what stays operator-gated
//!
//! The AC for SRS-EXE-006 is verified by an **integration test** that drives a
//! real IB **paper** account (port `4002`) for order submission, cancellation,
//! market-data subscription, and historical-data retrieval *without the TWS
//! GUI*. The IB paper account is reserved for **operator-initiated** adapter
//! integration testing (SyRS SYS-2e / AC-10) — it binds a fixed shared port and
//! cannot run inside the parallel agent pool, so `paper_account_round_trip`
//! stays `#[ignore]` + `ATP_RUN_INTEGRATION`-gated and is run by the operator.
//!
//! What ships here mirrors the established adapter pattern
//! (`SharadarAdapter::map_fundamentals`): the deterministic classification /
//! config / seam half is tested without a network, and the live TWS wire
//! protocol (the `wire` submodule, pinned to server version
//! [`IB_PINNED_SERVER_VERSION`]) is exercised against a scripted fake gateway
//! in `tests/srs_exe_006_ib_wire.rs` and proven against the real paper account
//! by the operator-run gate.
//!
//! * [`classify_ib_order_error`] — the load-bearing **IB-error → SyRS SYS-64
//!   [`OrderErrorCategory`]** classification (`INVALID_SYMBOL` /
//!   `INSUFFICIENT_BUYING_POWER` / `RATE_LIMITED` / `CONNECTIVITY_BLOCKED`) from
//!   documented IB TWS API codes — the broker categories SRS-ERR-001 was missing.
//! * [`IbGatewayConnection`] — the **transport seam** abstracting the TWS socket
//!   (raw [`IbApiError`] confined here), so every operation is exercised
//!   end-to-end against a deterministic in-memory double, leaving only the real
//!   socket transport operator-gated.
//! * [`InteractiveBrokersBrokerage`] — the adapter, exposed through the **canonical**
//!   [`BrokerageAdapter`] / [`MarketDataAdapter`] / [`HistoricalDataAdapter`]
//!   traits (SYS-52), so callers use the documented adapter interface and every
//!   failure flows through the common [`AdapterError`] taxonomy
//!   ([`AdapterError::Brokerage`], carrying the SyRS category) — never dropped (SYS-64).
//! * [`TcpIbGateway`] — the live transport: a real TCP session to headless IB
//!   Gateway from [`IbConnectionConfig`] with explicit connect/read/write
//!   deadlines, speaking the pinned-version TWS wire protocol (the `wire`
//!   submodule) for all six SRS-EXE-006 operations. Only the SRS-EXE-004
//!   composite (combo/BAG) wire remains operator-gated pending — it still fails
//!   **loudly** via [`IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`], never fabricating.

use crate::{
    AdapterBoundary, AdapterCapability, AdapterError, AdapterResult, AdapterVersion,
    BrokerageAdapter, DataBatch, HistoricalDataAdapter, HistoricalDataRequest,
    HistoricalQueryResult, InteractiveBrokersAdapter, MarketDataAdapter, MarketDataSubscription,
    SubscriptionReceipt, INTERACTIVE_BROKERS_ADAPTER_VERSION, INTERACTIVE_BROKERS_CAPABILITIES,
    INTERACTIVE_BROKERS_PROTOCOL_LABEL, INTERACTIVE_BROKERS_TWS_API_VERSION,
};
use atp_types::{CompositeOrderSubmission, OrderErrorCategory, OrderReceipt, OrderSubmission};
use std::fmt;
#[cfg(feature = "ib-live-transport")]
use std::net::TcpStream;
use std::net::{IpAddr, SocketAddr};
#[cfg(feature = "ib-live-transport")]
use std::sync::Mutex;
#[cfg(feature = "ib-live-transport")]
use std::time::Duration;

#[cfg(feature = "ib-live-transport")]
mod wire;

// --------------------------------------------------------------------------- //
// IB TWS API error wire shape + the documented codes we map onto SYS-64
// --------------------------------------------------------------------------- //

/// One error reported by the IB TWS API, as it arrives on the wire through the
/// `error(reqId, errorCode, errorString)` callback: the numeric `code` plus the
/// human-readable `message`. Modelling connectivity faults as IB error codes
/// (502/504/1100/2110) matches how IB itself reports them, so the adapter has a
/// single failure surface to classify. Raw `IbApiError` stays confined to the
/// [`IbGatewayConnection`] transport seam; the public adapter maps it onto the
/// canonical [`AdapterError`] boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbApiError {
    pub code: i32,
    pub message: String,
}

impl IbApiError {
    pub fn new(code: i32, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }
}

/// `100` — "Max rate of messages per second exceeded" → [`OrderErrorCategory::RateLimited`].
pub const IB_CODE_MAX_RATE_EXCEEDED: i32 = 100;
/// `200` — "No security definition has been found for the request" → `INVALID_SYMBOL`.
pub const IB_CODE_NO_SECURITY_DEFINITION: i32 = 200;
/// `201` — "Order rejected - reason:…" (reason-bearing; insufficient buying power /
/// margin surfaces here, classified from the reason text).
pub const IB_CODE_ORDER_REJECTED: i32 = 201;
/// `203` — "The security … is not available or allowed for this account" → `INVALID_SYMBOL`.
pub const IB_CODE_SECURITY_NOT_AVAILABLE: i32 = 203;
/// `502` — "Couldn't connect to TWS." → `CONNECTIVITY_BLOCKED`.
pub const IB_CODE_COULD_NOT_CONNECT: i32 = 502;
/// `504` — "Not connected" → `CONNECTIVITY_BLOCKED`.
pub const IB_CODE_NOT_CONNECTED: i32 = 504;
/// `1100` — "Connectivity between IB and Trader Workstation has been lost." → `CONNECTIVITY_BLOCKED`.
pub const IB_CODE_CONNECTIVITY_LOST: i32 = 1100;
/// `2110` — "Connectivity between TWS and server is broken." → `CONNECTIVITY_BLOCKED`.
pub const IB_CODE_CONNECTIVITY_BROKEN: i32 = 2110;

/// Adapter sentinel — **not** an IB code (negative so it can never collide with a
/// real TWS code). A [`TcpIbGateway`] operation whose live wire encoding is the
/// operator-gated integration deliverable returns this rather than fabricating a
/// result, so an un-finished live transport fails closed and loud. Today only the
/// SRS-EXE-004 composite (combo/BAG) wire still returns it; the six SRS-EXE-006
/// operations are wire-complete (see the `wire` submodule).
pub const IB_CODE_LIVE_WIRE_PROTOCOL_PENDING: i32 = -1;

/// Adapter sentinel — a bounded wire wait expired (mute or black-holed gateway).
/// The live path FAILS with this instead of hanging; classified
/// `CONNECTIVITY_BLOCKED` like the documented IB connectivity codes.
pub const IB_CODE_WIRE_TIMEOUT: i32 = -2;

/// Adapter sentinel — the gateway negotiated a server version other than the
/// pinned [`IB_PINNED_SERVER_VERSION`]; the adapter fails closed rather than
/// guessing at a different wire layout (operator remedy: upgrade IB Gateway).
pub const IB_CODE_UNSUPPORTED_SERVER_VERSION: i32 = -3;

/// Adapter sentinel — the request cannot be encoded honestly by the live wire
/// (e.g. a non-Equity asset class, a non-`1d` historical resolution, or a
/// non-numeric broker order id). Fails closed instead of sending a guess.
pub const IB_CODE_UNSUPPORTED_REQUEST: i32 = -4;

/// Map a documented IB TWS API error onto the SyRS SYS-64 [`OrderErrorCategory`],
/// or `None` when the adapter does not (yet) recognise the code as a SYS-64
/// broker-validation category. `None` does **not** mean "drop it" — the failure is
/// still surfaced through [`AdapterError::Brokerage`] with the raw IB detail; it
/// means "no fabricated category".
///
/// IB code `201` ("Order rejected") is reason-bearing: the same numeric code
/// covers insufficient-buying-power/margin rejections and other rejections, so it
/// is classified from the reason text (deliberately conservative — only the
/// documented buying-power/margin phrasing maps to `INSUFFICIENT_BUYING_POWER`).
pub fn classify_ib_order_error(error: &IbApiError) -> Option<OrderErrorCategory> {
    match error.code {
        IB_CODE_MAX_RATE_EXCEEDED => Some(OrderErrorCategory::RateLimited),
        IB_CODE_NO_SECURITY_DEFINITION | IB_CODE_SECURITY_NOT_AVAILABLE => {
            Some(OrderErrorCategory::InvalidSymbol)
        }
        IB_CODE_COULD_NOT_CONNECT
        | IB_CODE_NOT_CONNECTED
        | IB_CODE_CONNECTIVITY_LOST
        | IB_CODE_CONNECTIVITY_BROKEN
        | IB_CODE_WIRE_TIMEOUT => Some(OrderErrorCategory::ConnectivityBlocked),
        IB_CODE_ORDER_REJECTED => {
            if message_indicates_insufficient_buying_power(&error.message) {
                Some(OrderErrorCategory::InsufficientBuyingPower)
            } else {
                // A generic order rejection we do not map onto a SYS-64 category;
                // still surfaced through AdapterError::Brokerage, never dropped.
                None
            }
        }
        _ => None,
    }
}

/// Conservative reason-text classifier for IB code `201`: an insufficient-funds /
/// margin rejection. Lower-cased substring match on the documented phrasings.
fn message_indicates_insufficient_buying_power(message: &str) -> bool {
    let lower = message.to_ascii_lowercase();
    (lower.contains("insufficient")
        && (lower.contains("buying power")
            || lower.contains("funds")
            || lower.contains("margin")
            || lower.contains("equity")))
        || lower.contains("margin requirement")
}

/// Map a transport-seam [`IbApiError`] onto the canonical [`AdapterError::Brokerage`]
/// boundary, attaching the SyRS SYS-64 classification. This is the single point
/// where a raw IB error crosses into the common adapter taxonomy, so a failed
/// order submission carries `INVALID_SYMBOL` / `INSUFFICIENT_BUYING_POWER` /
/// `RATE_LIMITED` / `CONNECTIVITY_BLOCKED` and is never silently dropped (SYS-64).
fn brokerage_error(error: IbApiError) -> AdapterError {
    AdapterError::Brokerage {
        adapter: PROVIDER_NAME,
        category: classify_ib_order_error(&error),
        code: error.code,
        message: error.message,
    }
}

const PROVIDER_NAME: &str = "interactive_brokers";

// --------------------------------------------------------------------------- //
// Connection config
// --------------------------------------------------------------------------- //

/// The brokerage account an IB Gateway session targets. The **paper** account is
/// the only one available to operator-initiated adapter integration testing
/// (SyRS SYS-2e / AC-10); the **live** account is reserved for the single
/// designated live strategy (SRS-EXE-001) and is never used by the adapter test
/// surface.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum IbAccountKind {
    Live,
    Paper,
}

/// A malformed `ATP_IB_*` configuration value. Order configuration must **fail
/// closed**: a malformed port is reported, never silently replaced with a default
/// that could connect to an unintended IB Gateway endpoint.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbConnectionConfigError {
    pub variable: &'static str,
    pub value: String,
}

impl fmt::Display for IbConnectionConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            formatter,
            "malformed IB Gateway configuration: {} = `{}` is not a valid TCP port (1..=65535)",
            self.variable, self.value
        )
    }
}

impl std::error::Error for IbConnectionConfigError {}

/// Connection parameters for a headless IB Gateway session, sourced from the
/// `ATP_IB_*` environment (mirrors `.env.example` / `docker-compose.yml`). Held
/// as data so the live transport is constructed without reading the environment
/// at call sites, and so the (host, port, client-id) selection is unit-testable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbConnectionConfig {
    pub host: String,
    pub live_port: u16,
    pub paper_port: u16,
    pub client_id: i32,
}

impl IbConnectionConfig {
    pub const DEFAULT_HOST: &'static str = "127.0.0.1";
    pub const DEFAULT_LIVE_PORT: u16 = 4001;
    pub const DEFAULT_PAPER_PORT: u16 = 4002;

    /// Build from explicit values (use [`from_env`](Self::from_env) for the live
    /// runtime). `client_id` is the IB API client identifier for the session.
    pub fn new(host: impl Into<String>, live_port: u16, paper_port: u16, client_id: i32) -> Self {
        Self {
            host: host.into(),
            live_port,
            paper_port,
            client_id,
        }
    }

    /// Read `ATP_IB_HOST` / `ATP_IB_LIVE_PORT` / `ATP_IB_PAPER_PORT`. A **missing**
    /// port uses the documented `.env.example` default; a **malformed** port
    /// (non-numeric, out of range, or `0`) **fails closed** with an
    /// [`IbConnectionConfigError`] rather than silently connecting to a default
    /// endpoint — brokerage configuration must never resolve to an unintended IB
    /// Gateway on a typo.
    pub fn from_env(client_id: i32) -> Result<Self, IbConnectionConfigError> {
        let host =
            Self::env_value("ATP_IB_HOST")?.unwrap_or_else(|| Self::DEFAULT_HOST.to_string());
        let live_port = Self::port_from_env("ATP_IB_LIVE_PORT", Self::DEFAULT_LIVE_PORT)?;
        let paper_port = Self::port_from_env("ATP_IB_PAPER_PORT", Self::DEFAULT_PAPER_PORT)?;
        let config = Self {
            host,
            live_port,
            paper_port,
            client_id,
        };
        // Validate the host is a LITERAL IP at load (fail closed on a hostname) so a
        // misconfiguration is caught before any IB-touching call — never mid-order.
        config.ip()?;
        Ok(config)
    }

    /// Read an `ATP_IB_*` variable, distinguishing **absent** (`Ok(None)` → use the
    /// documented default) from **present-but-malformed**. A present non-Unicode
    /// value is *not* "missing": it fails closed so a corrupt brokerage setting can
    /// never silently resolve to a default endpoint.
    fn env_value(variable: &'static str) -> Result<Option<String>, IbConnectionConfigError> {
        match std::env::var(variable) {
            Ok(value) => Ok(Some(value)),
            Err(std::env::VarError::NotPresent) => Ok(None),
            Err(std::env::VarError::NotUnicode(_)) => Err(IbConnectionConfigError {
                variable,
                value: "<non-unicode>".to_string(),
            }),
        }
    }

    fn port_from_env(variable: &'static str, default: u16) -> Result<u16, IbConnectionConfigError> {
        // Absent → documented default; present (incl. non-Unicode) → fail closed on malformed.
        match Self::env_value(variable)? {
            None => Ok(default),
            Some(raw) => Self::parse_port(variable, &raw),
        }
    }

    /// Parse a present `ATP_IB_*_PORT` value. A non-numeric, out-of-range, or `0`
    /// port is rejected (`Err`) — never coerced to a default — so a typo cannot
    /// silently redirect the adapter to an unintended IB Gateway endpoint.
    fn parse_port(variable: &'static str, raw: &str) -> Result<u16, IbConnectionConfigError> {
        raw.trim()
            .parse::<u16>()
            .ok()
            .filter(|&port| port != 0)
            .ok_or_else(|| IbConnectionConfigError {
                variable,
                value: raw.to_string(),
            })
    }

    fn port(&self, account: IbAccountKind) -> u16 {
        match account {
            IbAccountKind::Live => self.live_port,
            IbAccountKind::Paper => self.paper_port,
        }
    }

    /// The `host:port` socket address string for the requested account kind (for
    /// diagnostics / log messages).
    pub fn socket_addr(&self, account: IbAccountKind) -> String {
        format!("{}:{}", self.host, self.port(account))
    }

    /// Parse `host` as a **literal** [`IpAddr`]. The host must be a literal IP — a
    /// hostname is rejected — because Phase 1 runs IB Gateway co-located
    /// (`.env.example` `ATP_IB_HOST=127.0.0.1`) and forbidding name resolution keeps
    /// an IB-touching call from hanging on a blocking/degraded DNS lookup that the
    /// [`IB_CONNECT_TIMEOUT`] socket deadline cannot bound.
    pub fn ip(&self) -> Result<IpAddr, IbConnectionConfigError> {
        self.host
            .trim()
            .parse::<IpAddr>()
            .map_err(|_| IbConnectionConfigError {
                variable: "ATP_IB_HOST",
                value: self.host.clone(),
            })
    }

    /// The fully-resolved [`SocketAddr`] for the requested account — built from the
    /// literal IP + port with **no DNS step**, so it is fully covered by the connect
    /// deadline.
    pub fn endpoint(&self, account: IbAccountKind) -> Result<SocketAddr, IbConnectionConfigError> {
        Ok(SocketAddr::new(self.ip()?, self.port(account)))
    }
}

// --------------------------------------------------------------------------- //
// Transport seam
// --------------------------------------------------------------------------- //

/// The transport seam over the IB TWS socket. Every method returns the raw IB
/// outcome (the canonical `Ok` payload or an [`IbApiError`]); the adapter
/// ([`InteractiveBrokersBrokerage`]) owns the mapping of [`IbApiError`] onto the
/// canonical [`AdapterError`] boundary, so raw IB errors never leak past this
/// seam. Abstracting the socket here is what lets the four AC operations be driven
/// end-to-end by a deterministic in-memory double in tests, leaving only the real
/// socket transport ([`TcpIbGateway`]) operator-gated.
pub trait IbGatewayConnection {
    /// Submit an order; returns the [`OrderReceipt`] on acceptance.
    fn submit_order(&self, order: &OrderSubmission) -> Result<OrderReceipt, IbApiError>;
    /// Submit a multi-leg options **composite** order (SRS-EXE-004); returns ONE
    /// [`OrderReceipt`] (one broker order id) for the whole composite on
    /// acceptance — the IB combo/BAG order the spread routes as.
    fn submit_composite_order(
        &self,
        order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError>;
    /// Cancel a resting order by IB broker order id.
    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError>;
    /// Subscribe to streaming market data; returns the [`SubscriptionReceipt`].
    fn subscribe_market_data(
        &self,
        request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError>;
    /// Retrieve historical bars; returns the vendor-neutral [`HistoricalQueryResult`].
    fn historical_data(
        &self,
        request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError>;
    /// Account status (API-5): returns the account-data batch retrieved from IB.
    fn account_status(&self) -> Result<DataBatch, IbApiError>;
    /// Open positions (API-5): returns the positions batch retrieved from IB.
    fn positions(&self) -> Result<DataBatch, IbApiError>;
}

// --------------------------------------------------------------------------- //
// The adapter — exposed through the canonical SYS-52 adapter traits
// --------------------------------------------------------------------------- //

/// The Interactive Brokers brokerage adapter (SRS-EXE-006): the four AC operations
/// over any [`IbGatewayConnection`] transport, exposed through the **canonical**
/// [`BrokerageAdapter`] / [`MarketDataAdapter`] / [`HistoricalDataAdapter`] traits
/// (SYS-52) so callers use the documented adapter interface and every failure
/// flows through the common [`AdapterError`] taxonomy. Generic over the transport
/// so the same adapter logic runs against a deterministic double in tests and
/// against [`TcpIbGateway`] in the operator-gated integration test.
#[derive(Debug, Clone)]
pub struct InteractiveBrokersBrokerage<C: IbGatewayConnection> {
    connection: C,
}

impl<C: IbGatewayConnection> InteractiveBrokersBrokerage<C> {
    pub fn new(connection: C) -> Self {
        Self { connection }
    }

    pub fn connection(&self) -> &C {
        &self.connection
    }
}

impl InteractiveBrokersAdapter {
    /// Bridge the documented zero-config IB provider — [`InteractiveBrokersAdapter`],
    /// the capability/version-discovery handle named in `adapter_contract` — to the
    /// **functional** SRS-EXE-006 runtime by supplying a transport. The
    /// connectionless handle itself returns `NotConfigured` for trading operations
    /// **by design** (a broker adapter with no live session must never fabricate an
    /// order); this is the canonical entry point from discovery to the operating
    /// adapter.
    pub fn with_gateway<C: IbGatewayConnection>(
        self,
        connection: C,
    ) -> InteractiveBrokersBrokerage<C> {
        InteractiveBrokersBrokerage::new(connection)
    }

    /// Build the functional runtime over the live [`TcpIbGateway`] for the given
    /// account (the IB **paper** account for operator-initiated adapter integration
    /// testing — SyRS SYS-2e). Behind the non-default `ib-live-transport` feature:
    /// the live transport is an operator-gated scaffold (its TWS wire encoding is
    /// completed under the gated integration test), so the default public surface
    /// never advertises a half-built live path.
    #[cfg(feature = "ib-live-transport")]
    pub fn connect(
        self,
        config: IbConnectionConfig,
        account: IbAccountKind,
    ) -> InteractiveBrokersBrokerage<TcpIbGateway> {
        self.with_gateway(TcpIbGateway::new(config, account))
    }
}

impl<C: IbGatewayConnection> AdapterBoundary for InteractiveBrokersBrokerage<C> {
    fn provider_name(&self) -> &'static str {
        PROVIDER_NAME
    }

    fn capabilities(&self) -> &'static [AdapterCapability] {
        INTERACTIVE_BROKERS_CAPABILITIES
    }

    fn version(&self) -> AdapterVersion {
        AdapterVersion {
            adapter_version: INTERACTIVE_BROKERS_ADAPTER_VERSION,
            protocol_version: INTERACTIVE_BROKERS_TWS_API_VERSION,
            protocol_label: INTERACTIVE_BROKERS_PROTOCOL_LABEL,
        }
    }
}

impl<C: IbGatewayConnection> BrokerageAdapter for InteractiveBrokersBrokerage<C> {
    fn submit_order(&self, request: OrderSubmission) -> AdapterResult<OrderReceipt> {
        // SRS-EXE-003 — the order is VALIDATED before it can reach the broker.
        // Delegates price positivity to `OrderSubmission::validate` (the SAME
        // rule the paper intake applies, so live and paper cannot drift) and
        // fails closed HERE: an invalid order is never forwarded to the gateway,
        // so a malformed market/limit/stop/stop-limit order can never create a
        // live broker order.
        if let Err(err) = request.validate() {
            return Err(AdapterError::InvalidOrder {
                adapter: self.provider_name(),
                detail: err.to_string(),
            });
        }
        // Any IB rejection maps onto AdapterError::Brokerage (with the SyRS
        // category) — an Err is always returned, the submission is never dropped.
        self.connection
            .submit_order(&request)
            .map_err(brokerage_error)
    }

    fn submit_composite_order(
        &self,
        request: CompositeOrderSubmission,
    ) -> AdapterResult<OrderReceipt> {
        // SRS-EXE-004 — the composite is VALIDATED before it can reach the broker:
        // at least two legs (SYS-4), strictly-positive quantities, and positive
        // trigger/limit prices (delegating to the SAME `OrderType::validate_prices`
        // authority the single-leg path uses, so live and paper cannot drift). A
        // malformed composite fails closed HERE and is never forwarded — one bad
        // leg rejects the whole order, so no partial spread reaches the gateway.
        //
        // TRANSPORT SEAM, not the authority gate. Like the single-leg
        // `submit_order` above, this is the low-level broker method; it does shape
        // validation only, NOT the ERR-1/2/3 live safeguards. The engine-owned
        // authority + connectivity + per-contract freshness gate is
        // `ExecutionEngine::route_composite_order` (mirroring `route_order`), and
        // the intended production flow routes through it. Making this adapter method
        // UNREACHABLE except through the execution engine (a crate-private wrapper +
        // an admission token bound to `LiveDesignation`) is the SAME deferred
        // orchestrator/adapter wiring the single-leg path defers — owner
        // SRS-EXE-006 / SRS-ORCH-* (see `route_order`'s scope note and
        // composite_order_contract.deferred[]). There is NO live bypass in the
        // SHIPPED code: over the real `TcpIbGateway` this fails closed with
        // LIVE_WIRE_PROTOCOL_PENDING (the combo wire is operator-gated), and the
        // deterministic gateway double is test-only — a direct adapter call cannot
        // create a real live order until that wire + wiring land.
        if let Err(err) = request.validate() {
            return Err(AdapterError::InvalidOrder {
                adapter: self.provider_name(),
                detail: err.to_string(),
            });
        }
        // Routes as ONE IB combo order → ONE OrderReceipt (one broker order id).
        // Any IB rejection maps onto AdapterError::Brokerage; never dropped.
        self.connection
            .submit_composite_order(&request)
            .map_err(brokerage_error)
    }

    fn cancel_order(&self, broker_order_id: &str) -> AdapterResult<()> {
        self.connection
            .cancel_order(broker_order_id)
            .map_err(brokerage_error)
    }

    fn account_status(&self) -> AdapterResult<DataBatch> {
        self.connection.account_status().map_err(brokerage_error)
    }

    fn positions(&self) -> AdapterResult<DataBatch> {
        self.connection.positions().map_err(brokerage_error)
    }
}

impl<C: IbGatewayConnection> MarketDataAdapter for InteractiveBrokersBrokerage<C> {
    fn subscribe_market_data(
        &self,
        request: MarketDataSubscription,
    ) -> AdapterResult<SubscriptionReceipt> {
        self.connection
            .subscribe_market_data(&request)
            .map_err(brokerage_error)
    }
}

impl<C: IbGatewayConnection> HistoricalDataAdapter for InteractiveBrokersBrokerage<C> {
    fn historical_data(
        &self,
        request: HistoricalDataRequest,
    ) -> AdapterResult<HistoricalQueryResult> {
        self.connection
            .historical_data(&request)
            .map_err(brokerage_error)
    }
}

// --------------------------------------------------------------------------- //
// Live transport scaffold (operator-gated)
// --------------------------------------------------------------------------- //

/// The live IB Gateway transport: a real TCP session to headless IB Gateway
/// (`host:port` from [`IbConnectionConfig`]) speaking the TWS wire protocol
/// pinned to server version [`IB_PINNED_SERVER_VERSION`] (see the `wire`
/// submodule for framing, handshake, and per-operation encodings — golden-pinned
/// against `ibapi` 10.19.4 by `tests/srs_exe_006_ib_wire.rs`, and proven against
/// the real IB **paper** account by the operator-initiated `paper_account_round_trip`
/// integration test, SyRS SYS-2e).
///
/// The session is established lazily on the first operation and cached behind a
/// mutex (trait methods take `&self`); a transport-level fault drops the cached
/// session so the next call reconnects cleanly. Only the SRS-EXE-004 composite
/// (combo/BAG) wire remains operator-gated pending —
/// [`submit_composite_order`](IbGatewayConnection::submit_composite_order) still
/// fails closed with [`IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`].
///
/// Behind the non-default `ib-live-transport` cargo feature so the default public
/// adapter surface never advertises a live path it cannot verify solo.
#[cfg(feature = "ib-live-transport")]
#[derive(Debug)]
pub struct TcpIbGateway {
    config: IbConnectionConfig,
    account: IbAccountKind,
    session: Mutex<Option<wire::IbSession>>,
    op_deadline: Duration,
}

#[cfg(feature = "ib-live-transport")]
impl TcpIbGateway {
    /// Construct against the given config + account. The adapter test surface
    /// always uses [`IbAccountKind::Paper`] (SyRS SYS-2e / AC-10); the live
    /// account is reserved for SRS-EXE-001.
    pub fn new(config: IbConnectionConfig, account: IbAccountKind) -> Self {
        Self::with_op_deadline(config, account, IB_OP_DEADLINE)
    }

    /// Construct with a custom per-operation wire deadline. Exposed for the
    /// fake-gateway test suite (short deadlines keep the timeout tests fast);
    /// production callers use [`new`](Self::new) with [`IB_OP_DEADLINE`].
    #[doc(hidden)]
    pub fn with_op_deadline(
        config: IbConnectionConfig,
        account: IbAccountKind,
        op_deadline: Duration,
    ) -> Self {
        Self {
            config,
            account,
            session: Mutex::new(None),
            op_deadline,
        }
    }

    /// Open the TCP session to headless IB Gateway (no TWS GUI; AC-2) with an
    /// **explicit** [`IB_CONNECT_TIMEOUT`] deadline (`connect_timeout` plus read/write
    /// timeouts on the stream) so a black-holed Gateway fails the adapter's budget
    /// instead of hanging — a live-execution call must never block unbounded. The
    /// endpoint is a **literal** [`SocketAddr`] (the host is a validated literal IP,
    /// [`IbConnectionConfig::ip`]), so there is **no DNS step** that could hang
    /// outside the deadline. Any failure is a SYS-64-classifiable
    /// `CONNECTIVITY_BLOCKED` IB code (`502`).
    pub fn connect(&self) -> Result<TcpStream, IbApiError> {
        let blocked = |detail: String| IbApiError::new(IB_CODE_COULD_NOT_CONNECT, detail);
        let socket = self
            .config
            .endpoint(self.account)
            .map_err(|err| blocked(err.to_string()))?;
        let stream = TcpStream::connect_timeout(&socket, IB_CONNECT_TIMEOUT).map_err(|err| {
            blocked(format!(
                "couldn't connect to headless IB Gateway at {socket} within {:?}: {err}",
                IB_CONNECT_TIMEOUT
            ))
        })?;
        // Bound subsequent IB reads/writes to the same budget so a half-open
        // session cannot hang the live path either.
        stream
            .set_read_timeout(Some(IB_CONNECT_TIMEOUT))
            .and_then(|()| stream.set_write_timeout(Some(IB_CONNECT_TIMEOUT)))
            .map_err(|err| blocked(format!("couldn't set IB Gateway socket timeouts: {err}")))?;
        Ok(stream)
    }

    fn live_wire_pending(operation: &str) -> IbApiError {
        IbApiError::new(
            IB_CODE_LIVE_WIRE_PROTOCOL_PENDING,
            format!(
                "IB TWS wire protocol for `{operation}` is completed and verified under the \
                 operator-initiated IB paper-account integration test (SyRS SYS-2e; \
                 SRS-EXE-004 composite wire still operator-gated)"
            ),
        )
    }

    /// Run one wire operation over the cached session, establishing it lazily
    /// (connect + pinned-version handshake + `startApi`). A transport-level
    /// fault (connect/read/write failure, wire timeout, version mismatch)
    /// drops the cached session so the next call reconnects cleanly instead of
    /// reusing a dead socket.
    fn with_session<T>(
        &self,
        operate: impl FnOnce(&mut wire::IbSession) -> Result<T, IbApiError>,
    ) -> Result<T, IbApiError> {
        // SRS-EXE-001 gate: the LIVE account is reserved for the execution
        // engine's admission path (live-strategy registry, stale-data gate,
        // kill-switch). Until that wiring exists, the transport serves ONLY the
        // paper account — a live-account session must fail closed here, before
        // any socket is opened, so the adapter alone can never place a real
        // live-account order.
        if self.account != IbAccountKind::Paper {
            return Err(IbApiError::new(
                IB_CODE_UNSUPPORTED_REQUEST,
                "the live IB account is gated on the SRS-EXE-001 execution-engine admission \
                 (live-strategy registry, stale-data gate, kill-switch); only \
                 IbAccountKind::Paper is served by the transport today",
            ));
        }
        let mut guard = self
            .session
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if guard.is_none() {
            let stream = self.connect()?;
            // `establish` re-arms the socket read timeout to its fast deadline
            // tick, so every wire read honors `op_deadline` promptly.
            *guard = Some(wire::IbSession::establish(
                stream,
                self.config.client_id,
                self.op_deadline,
            )?);
        }
        let session = guard.as_mut().expect("session established above");
        let result = operate(session);
        if let Err(err) = &result {
            if wire::is_transport_fault(err.code) {
                *guard = None;
            }
        }
        result
    }
}

#[cfg(feature = "ib-live-transport")]
impl IbGatewayConnection for TcpIbGateway {
    fn submit_order(&self, order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        self.with_session(|session| session.submit_order(order))
    }

    fn submit_composite_order(
        &self,
        _order: &CompositeOrderSubmission,
    ) -> Result<OrderReceipt, IbApiError> {
        // The SAME SRS-EXE-001 live-account gate as every session operation:
        // when SRS-EXE-004 completes this wire, the admission boundary is
        // already in place (and today a live-account composite never even
        // opens a socket).
        if self.account != IbAccountKind::Paper {
            return Err(IbApiError::new(
                IB_CODE_UNSUPPORTED_REQUEST,
                "the live IB account is gated on the SRS-EXE-001 execution-engine admission; \
                 only IbAccountKind::Paper is served by the transport today",
            ));
        }
        // The IB combo/BAG wire encoding for the composite is completed under the
        // operator-initiated IB paper-account integration test (SYS-2e; SRS-EXE-004
        // lands serialized), so this fails closed rather than fabricating a receipt.
        let _stream = self.connect()?;
        Err(Self::live_wire_pending("submit_composite_order"))
    }

    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError> {
        self.with_session(|session| session.cancel_order(broker_order_id))
    }

    fn subscribe_market_data(
        &self,
        request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        self.with_session(|session| session.subscribe_market_data(request))
    }

    fn historical_data(
        &self,
        request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        self.with_session(|session| session.historical_data(request))
    }

    fn account_status(&self) -> Result<DataBatch, IbApiError> {
        self.with_session(wire::IbSession::account_status)
    }

    fn positions(&self) -> Result<DataBatch, IbApiError> {
        self.with_session(wire::IbSession::positions)
    }
}

/// Default IB API connect timeout for the live transport's socket establishment.
#[cfg(feature = "ib-live-transport")]
pub const IB_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

/// Default per-operation wire deadline: each live operation (submit / cancel /
/// subscribe / historical / account / positions) must complete its request +
/// reply exchange inside this budget or FAIL with [`IB_CODE_WIRE_TIMEOUT`] —
/// a live-execution call never blocks unbounded (bounded-wait norm).
#[cfg(feature = "ib-live-transport")]
pub const IB_OP_DEADLINE: Duration = Duration::from_secs(15);

/// The pinned TWS server version: the handshake offers exactly
/// `v176..176`, so every wire layout in the `wire` submodule is deterministic
/// (`ibapi` 10.19 line). Any other negotiated version fails closed with
/// [`IB_CODE_UNSUPPORTED_SERVER_VERSION`].
#[cfg(feature = "ib-live-transport")]
pub const IB_PINNED_SERVER_VERSION: i32 = 176;

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(feature = "ib-live-transport")]
    use atp_types::StrategyId;

    // Only the (feature-gated) live-transport test builds an OrderSubmission here.
    #[cfg(feature = "ib-live-transport")]
    fn order(symbol: &str, quantity: i64) -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: symbol.to_string(),
            quantity,
            asset_class: atp_types::AssetClass::Equity,
            side: atp_types::OrderSide::Buy,
            order_type: atp_types::OrderType::Market,
        }
    }

    #[test]
    fn classifies_documented_ib_codes_onto_syrs64_categories() {
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_MAX_RATE_EXCEEDED,
                "Max rate of messages per second exceeded"
            )),
            Some(OrderErrorCategory::RateLimited)
        );
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_NO_SECURITY_DEFINITION,
                "No security definition has been found for the request"
            )),
            Some(OrderErrorCategory::InvalidSymbol)
        );
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_SECURITY_NOT_AVAILABLE,
                "The security ZZZZ is not available or allowed for this account"
            )),
            Some(OrderErrorCategory::InvalidSymbol)
        );
        for code in [
            IB_CODE_COULD_NOT_CONNECT,
            IB_CODE_NOT_CONNECTED,
            IB_CODE_CONNECTIVITY_LOST,
            IB_CODE_CONNECTIVITY_BROKEN,
        ] {
            assert_eq!(
                classify_ib_order_error(&IbApiError::new(code, "connectivity")),
                Some(OrderErrorCategory::ConnectivityBlocked),
                "code {code} must classify as CONNECTIVITY_BLOCKED"
            );
        }
    }

    #[test]
    fn classifies_order_rejected_by_reason_text() {
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_ORDER_REJECTED,
                "Order rejected - reason: Insufficient buying power for this order"
            )),
            Some(OrderErrorCategory::InsufficientBuyingPower)
        );
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_ORDER_REJECTED,
                "Order rejected - reason: insufficient margin to execute order"
            )),
            Some(OrderErrorCategory::InsufficientBuyingPower)
        );
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(
                IB_CODE_ORDER_REJECTED,
                "Order rejected - reason: price does not conform to minimum tick"
            )),
            None
        );
    }

    #[test]
    fn unrecognised_codes_are_unmapped_not_fabricated() {
        assert_eq!(
            classify_ib_order_error(&IbApiError::new(999_999, "some novel IB condition")),
            None
        );
    }

    #[test]
    fn brokerage_error_carries_category_and_raw_detail() {
        let err = brokerage_error(IbApiError::new(
            IB_CODE_NO_SECURITY_DEFINITION,
            "No security definition",
        ));
        match err {
            AdapterError::Brokerage {
                adapter,
                category,
                code,
                message,
            } => {
                assert_eq!(adapter, "interactive_brokers");
                assert_eq!(category, Some(OrderErrorCategory::InvalidSymbol));
                assert_eq!(code, IB_CODE_NO_SECURITY_DEFINITION);
                assert!(message.contains("No security definition"));
            }
            other => panic!("expected AdapterError::Brokerage, got {other:?}"),
        }
    }

    #[test]
    fn unmapped_failure_is_surfaced_with_no_category_not_dropped() {
        let err = brokerage_error(IbApiError::new(
            IB_CODE_ORDER_REJECTED,
            "Order rejected - reason: odd lot",
        ));
        match err {
            AdapterError::Brokerage {
                category, message, ..
            } => {
                assert_eq!(category, None);
                assert!(message.contains("odd lot"));
            }
            other => panic!("expected AdapterError::Brokerage, got {other:?}"),
        }
    }

    #[test]
    fn config_socket_addr_selects_account_port() {
        let config = IbConnectionConfig::new(
            IbConnectionConfig::DEFAULT_HOST,
            IbConnectionConfig::DEFAULT_LIVE_PORT,
            IbConnectionConfig::DEFAULT_PAPER_PORT,
            7,
        );
        assert_eq!(config.socket_addr(IbAccountKind::Paper), "127.0.0.1:4002");
        assert_eq!(config.socket_addr(IbAccountKind::Live), "127.0.0.1:4001");
        // The literal-IP endpoint carries no DNS step.
        assert_eq!(
            config.endpoint(IbAccountKind::Paper).unwrap(),
            "127.0.0.1:4002".parse().unwrap()
        );
    }

    #[test]
    fn hostname_host_fails_closed_literal_ip_only() {
        // A hostname (not a literal IP) is rejected: name resolution could hang an
        // IB-touching call outside the connect deadline, so it must fail at config.
        let bad = IbConnectionConfig::new("ib-gateway.local", 4001, 4002, 1);
        assert!(bad.ip().is_err());
        assert!(bad.endpoint(IbAccountKind::Paper).is_err());
        // An IPv6 literal is accepted (it is still literal, no DNS).
        let v6 = IbConnectionConfig::new("::1", 4001, 4002, 1);
        assert!(v6.endpoint(IbAccountKind::Paper).is_ok());
    }

    #[test]
    fn malformed_port_env_fails_closed() {
        // A non-numeric / zero / out-of-range port must fail closed, never fall back
        // to a default endpoint. (Uses the value parser so the test does not mutate env.)
        assert!(IbConnectionConfig::parse_port("ATP_IB_PAPER_PORT", "abc").is_err());
        assert!(IbConnectionConfig::parse_port("ATP_IB_PAPER_PORT", "0").is_err());
        assert!(IbConnectionConfig::parse_port("ATP_IB_PAPER_PORT", "70000").is_err());
        // A valid value parses (whitespace trimmed).
        assert_eq!(
            IbConnectionConfig::parse_port("ATP_IB_PAPER_PORT", "  4002 ").unwrap(),
            4002
        );
    }

    #[cfg(feature = "ib-live-transport")]
    #[test]
    fn live_transport_fails_closed_when_gateway_unreachable() {
        let config = IbConnectionConfig::new("127.0.0.1", 1, 1, 1);
        let transport = TcpIbGateway::new(config, IbAccountKind::Paper);
        let err = transport
            .submit_order(&order("AAPL", 1))
            .expect_err("an unreachable gateway must fail closed");
        assert_eq!(
            classify_ib_order_error(&err),
            Some(OrderErrorCategory::ConnectivityBlocked)
        );
    }
}
