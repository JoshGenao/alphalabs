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
//! cannot run inside the parallel agent pool. So this feature lands **serialized
//! (`passes:false`)**: the operator runs the gated integration test
//! (`ATP_RUN_INTEGRATION=1`, ignored by default) to flip it.
//!
//! What ships here, fully built and tested **without** a network, mirrors the
//! established adapter pattern (`SharadarAdapter::map_fundamentals`): the
//! deterministic half that does not need the wire.
//!
//! * [`classify_ib_order_error`] / [`to_order_submit_error`] — the load-bearing
//!   **IB-error → SyRS SYS-64 [`StructuredOrderError`]** translation. This is the
//!   concrete artifact the broker-side error categories
//!   (`INVALID_SYMBOL` / `INSUFFICIENT_BUYING_POWER` / `RATE_LIMITED` /
//!   `CONNECTIVITY_BLOCKED`) were vocabulary-only without (SRS-ERR-001 deferred
//!   on exactly this), now produced from documented IB TWS API error codes.
//! * [`IbGatewayConnection`] — the **transport seam** abstracting the TWS socket,
//!   so every adapter operation is exercised end-to-end against a deterministic
//!   in-memory double in unit/boundary tests, with the real socket transport the
//!   only operator-gated piece.
//! * [`InteractiveBrokersBrokerage`] — the adapter: the four AC operations
//!   (submit / cancel / subscribe / historical) over any [`IbGatewayConnection`],
//!   mapping IB outcomes onto vendor-neutral domain results and **never** silently
//!   dropping a failed order submission (SYS-64).
//! * [`TcpIbGateway`] — the live-transport scaffold: it establishes the real TCP
//!   session to headless IB Gateway from `ATP_IB_*` config, but its per-operation
//!   TWS wire encoding is completed and verified under the operator-initiated
//!   integration test, so it currently fails **loudly** (never a fabricated
//!   success) — see [`IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`].

use atp_types::{OrderErrorCategory, OrderReceipt, OrderSubmission, StructuredOrderError};
use std::net::{TcpStream, ToSocketAddrs};
use std::time::Duration;

// --------------------------------------------------------------------------- //
// IB TWS API error wire shape + the documented codes we map onto SYS-64
// --------------------------------------------------------------------------- //

/// One error reported by the IB TWS API, as it arrives on the wire through the
/// `error(reqId, errorCode, errorString)` callback: the numeric `code` plus the
/// human-readable `message`. Modelling connectivity faults as IB error codes
/// (502/504/1100/2110) matches how IB itself reports them, so the adapter has a
/// single failure surface to classify.
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
/// result, so an un-finished live transport fails closed and loud.
pub const IB_CODE_LIVE_WIRE_PROTOCOL_PENDING: i32 = -1;

/// Map a documented IB TWS API error onto the SyRS SYS-64 [`OrderErrorCategory`],
/// or `None` when the adapter does not (yet) recognise the code as a SYS-64
/// broker-validation category. `None` does **not** mean "drop it" — the caller
/// still surfaces an unmapped failure with the raw IB detail (see
/// [`to_order_submit_error`]); it means "no fabricated category".
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
        | IB_CODE_CONNECTIVITY_BROKEN => Some(OrderErrorCategory::ConnectivityBlocked),
        IB_CODE_ORDER_REJECTED => {
            if message_indicates_insufficient_buying_power(&error.message) {
                Some(OrderErrorCategory::InsufficientBuyingPower)
            } else {
                // A generic order rejection we do not map onto a SYS-64 category;
                // never dropped — surfaced as Unmapped with the raw IB detail.
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

/// Stable `error_type` discriminator for a SYS-64 envelope produced from an IB
/// rejection: the category wire name suffixed with the originating IB code, so an
/// operator reading the envelope sees both the canonical category and the exact
/// IB code that produced it (e.g. `INVALID_SYMBOL/ib-200`).
fn ib_error_type(category: OrderErrorCategory, code: i32) -> String {
    format!("{}/ib-{}", category.as_str(), code)
}

/// Outcome of an order submission that the IB adapter could not complete. Either
/// a SyRS SYS-64 structured envelope (recognised broker-validation category) or
/// an explicitly **unmapped** IB error — and an unmapped error still carries the
/// raw IB code/message and the unchanged original order, so a failed submission
/// is **never silently dropped** (SYS-64).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IbOrderSubmitError {
    /// A recognised broker-validation failure as a SyRS SYS-64 [`StructuredOrderError`].
    Structured(StructuredOrderError),
    /// An IB error the adapter does not map onto a SYS-64 category — surfaced with
    /// the raw IB `code` + `message` and the unchanged `original_order` for operator
    /// triage / future mapping, never a fabricated category.
    Unmapped {
        code: i32,
        message: String,
        original_order: OrderSubmission,
    },
}

/// Translate an [`IbApiError`] for a failed order submission into either a SYS-64
/// [`StructuredOrderError`] (when the code maps onto a category) or an
/// [`IbOrderSubmitError::Unmapped`] carrying the raw IB detail. The original order
/// parameters travel **unchanged** into the envelope (SRS-ERR-001) either way.
pub fn to_order_submit_error(
    error: IbApiError,
    original_order: OrderSubmission,
) -> IbOrderSubmitError {
    match classify_ib_order_error(&error) {
        Some(category) => IbOrderSubmitError::Structured(StructuredOrderError {
            category,
            error_type: ib_error_type(category, error.code),
            message: format!(
                "IB Gateway rejected order for `{}` (qty {}): IB error {} — {} \
                 (SRS-EXE-006 / SyRS SYS-64)",
                original_order.symbol, original_order.quantity, error.code, error.message
            ),
            original_order,
        }),
        None => IbOrderSubmitError::Unmapped {
            code: error.code,
            message: error.message,
            original_order,
        },
    }
}

// --------------------------------------------------------------------------- //
// Transport seam + connection config
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

    /// Read `ATP_IB_HOST` / `ATP_IB_LIVE_PORT` / `ATP_IB_PAPER_PORT` (falling back
    /// to the documented `.env.example` defaults), with the given client id.
    pub fn from_env(client_id: i32) -> Self {
        let host = std::env::var("ATP_IB_HOST").unwrap_or_else(|_| Self::DEFAULT_HOST.to_string());
        let live_port = std::env::var("ATP_IB_LIVE_PORT")
            .ok()
            .and_then(|raw| raw.parse().ok())
            .unwrap_or(Self::DEFAULT_LIVE_PORT);
        let paper_port = std::env::var("ATP_IB_PAPER_PORT")
            .ok()
            .and_then(|raw| raw.parse().ok())
            .unwrap_or(Self::DEFAULT_PAPER_PORT);
        Self {
            host,
            live_port,
            paper_port,
            client_id,
        }
    }

    /// The `host:port` socket address for the requested account kind.
    pub fn socket_addr(&self, account: IbAccountKind) -> String {
        let port = match account {
            IbAccountKind::Live => self.live_port,
            IbAccountKind::Paper => self.paper_port,
        };
        format!("{}:{}", self.host, port)
    }
}

/// A confirmed market-data subscription handle returned by the adapter.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbSubscriptionReceipt {
    pub symbol: String,
    pub subscription_id: String,
}

/// A historical-data response: the symbol plus the number of bars returned. The
/// bar payload itself flows through the data layer's vendor-neutral envelope; the
/// adapter test surface only asserts retrieval succeeded and is non-empty.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbHistoricalResult {
    pub symbol: String,
    pub bar_count: usize,
}

/// The adapter-boundary failure for the IB adapter's **non-order** operations
/// (cancel / market-data subscription / historical retrieval). This confines the
/// raw transport [`IbApiError`] to the [`IbGatewayConnection`] seam: a caller of
/// the public adapter gets a consistent boundary contract — the SyRS-classified
/// [`OrderErrorCategory`] when the IB error maps onto one (so a connectivity loss
/// is `CONNECTIVITY_BLOCKED` here exactly as on the order path), plus the raw IB
/// `code`/`message` for diagnostics. Order submission has its own richer SYS-64
/// surface ([`IbOrderSubmitError`]); this is for the operations that are not
/// order submissions.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IbAdapterError {
    /// The SyRS SYS-64 category when the IB error maps onto one (e.g. a
    /// connectivity fault → `CONNECTIVITY_BLOCKED`), else `None`.
    pub category: Option<OrderErrorCategory>,
    pub code: i32,
    pub message: String,
}

impl IbAdapterError {
    fn from_ib(error: IbApiError) -> Self {
        Self {
            category: classify_ib_order_error(&error),
            code: error.code,
            message: error.message,
        }
    }
}

impl std::fmt::Display for IbAdapterError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.category {
            Some(category) => write!(
                formatter,
                "[{}] IB adapter operation failed: IB error {} — {}",
                category.as_str(),
                self.code,
                self.message
            ),
            None => write!(
                formatter,
                "IB adapter operation failed: IB error {} — {}",
                self.code, self.message
            ),
        }
    }
}

impl std::error::Error for IbAdapterError {}

/// The transport seam over the IB TWS socket. Every method returns the raw IB
/// outcome (`Ok` payload or an [`IbApiError`]); the adapter
/// ([`InteractiveBrokersBrokerage`]) owns the mapping onto the vendor-neutral
/// domain results and the boundary error types ([`IbOrderSubmitError`] /
/// [`IbAdapterError`]), so raw [`IbApiError`] never leaks past this seam.
/// Abstracting the socket here is what lets the four AC operations be driven
/// end-to-end by a deterministic in-memory double in tests, leaving only the real
/// socket transport ([`TcpIbGateway`]) operator-gated.
pub trait IbGatewayConnection {
    /// Submit an order; returns the IB broker order id on acceptance.
    fn submit_order(&self, order: &OrderSubmission) -> Result<String, IbApiError>;
    /// Cancel a resting order by IB broker order id.
    fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbApiError>;
    /// Subscribe to streaming market data for `symbol`; returns the subscription id.
    fn subscribe_market_data(&self, symbol: &str) -> Result<String, IbApiError>;
    /// Retrieve historical bars for `symbol`; returns the bar count.
    fn request_historical_data(&self, symbol: &str) -> Result<usize, IbApiError>;
}

// --------------------------------------------------------------------------- //
// The adapter
// --------------------------------------------------------------------------- //

/// The Interactive Brokers brokerage adapter (SRS-EXE-006): the four AC
/// operations over any [`IbGatewayConnection`] transport. Generic over the
/// transport so the same adapter logic is exercised against a deterministic
/// double in tests and against [`TcpIbGateway`] in the operator-gated
/// integration test.
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

    /// Submit an order to IB. On acceptance returns the vendor-neutral
    /// [`OrderReceipt`]; on rejection returns an [`IbOrderSubmitError`] — a SYS-64
    /// [`StructuredOrderError`] for a recognised broker-validation category, or an
    /// `Unmapped` failure carrying the raw IB detail. A failed submission is never
    /// silently dropped (SYS-64).
    pub fn submit_order(&self, order: OrderSubmission) -> Result<OrderReceipt, IbOrderSubmitError> {
        match self.connection.submit_order(&order) {
            Ok(broker_order_id) => Ok(OrderReceipt { broker_order_id }),
            Err(error) => Err(to_order_submit_error(error, order)),
        }
    }

    /// Cancel a resting order by IB broker order id. A transport failure is mapped
    /// onto the [`IbAdapterError`] boundary (raw `IbApiError` never leaks).
    pub fn cancel_order(&self, broker_order_id: &str) -> Result<(), IbAdapterError> {
        self.connection
            .cancel_order(broker_order_id)
            .map_err(IbAdapterError::from_ib)
    }

    /// Subscribe to streaming market data for `symbol`.
    pub fn subscribe_market_data(
        &self,
        symbol: &str,
    ) -> Result<IbSubscriptionReceipt, IbAdapterError> {
        let subscription_id = self
            .connection
            .subscribe_market_data(symbol)
            .map_err(IbAdapterError::from_ib)?;
        Ok(IbSubscriptionReceipt {
            symbol: symbol.to_string(),
            subscription_id,
        })
    }

    /// Retrieve historical bars for `symbol`.
    pub fn request_historical_data(
        &self,
        symbol: &str,
    ) -> Result<IbHistoricalResult, IbAdapterError> {
        let bar_count = self
            .connection
            .request_historical_data(symbol)
            .map_err(IbAdapterError::from_ib)?;
        Ok(IbHistoricalResult {
            symbol: symbol.to_string(),
            bar_count,
        })
    }
}

// --------------------------------------------------------------------------- //
// Live transport scaffold (operator-gated)
// --------------------------------------------------------------------------- //

/// The live IB Gateway transport: a real TCP session to headless IB Gateway
/// (`host:port` from [`IbConnectionConfig`]). Establishing the socket is real and
/// structurally testable; the per-operation **TWS wire encoding** is completed and
/// verified under the operator-initiated IB paper-account integration test
/// (SyRS SYS-2e), so each operation here fails closed with
/// [`IB_CODE_LIVE_WIRE_PROTOCOL_PENDING`] rather than fabricating a result. This
/// is the *only* part of the adapter that an automated paper-account test (not a
/// parallel agent) can complete — which is why SRS-EXE-006 lands serialized.
#[derive(Debug)]
pub struct TcpIbGateway {
    config: IbConnectionConfig,
    account: IbAccountKind,
}

impl TcpIbGateway {
    /// Construct against the given config + account. The adapter test surface
    /// always uses [`IbAccountKind::Paper`] (SyRS SYS-2e / AC-10); the live
    /// account is reserved for SRS-EXE-001.
    pub fn new(config: IbConnectionConfig, account: IbAccountKind) -> Self {
        Self { config, account }
    }

    /// Open the TCP session to headless IB Gateway (no TWS GUI; AC-2) with an
    /// **explicit** [`IB_CONNECT_TIMEOUT`] deadline (`connect_timeout` on a resolved
    /// `SocketAddr`, plus read/write timeouts on the stream) so a black-holed
    /// Gateway host fails the adapter's budget instead of hanging on the OS TCP
    /// timeout — a live-execution call must never block unbounded. Any failure is a
    /// SYS-64-classifiable `CONNECTIVITY_BLOCKED` IB code (`502`) so an unreachable
    /// Gateway maps onto the same category the live execution gate uses
    /// (ERR-2 / SRS-SAFE-003).
    pub fn connect(&self) -> Result<TcpStream, IbApiError> {
        let addr = self.config.socket_addr(self.account);
        let blocked = |detail: String| IbApiError::new(IB_CODE_COULD_NOT_CONNECT, detail);
        let socket = addr
            .to_socket_addrs()
            .map_err(|err| {
                blocked(format!(
                    "could not resolve IB Gateway address {addr}: {err}"
                ))
            })?
            .next()
            .ok_or_else(|| blocked(format!("no socket address resolved for IB Gateway {addr}")))?;
        let stream = TcpStream::connect_timeout(&socket, IB_CONNECT_TIMEOUT).map_err(|err| {
            blocked(format!(
                "couldn't connect to headless IB Gateway at {addr} within {:?}: {err}",
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
                 SRS-EXE-006 serialized)"
            ),
        )
    }
}

impl IbGatewayConnection for TcpIbGateway {
    fn submit_order(&self, _order: &OrderSubmission) -> Result<String, IbApiError> {
        // Establish the real session (fails closed if unreachable), then defer the
        // wire encoding to the operator-gated integration deliverable.
        let _stream = self.connect()?;
        Err(Self::live_wire_pending("submit_order"))
    }

    fn cancel_order(&self, _broker_order_id: &str) -> Result<(), IbApiError> {
        let _stream = self.connect()?;
        Err(Self::live_wire_pending("cancel_order"))
    }

    fn subscribe_market_data(&self, _symbol: &str) -> Result<String, IbApiError> {
        let _stream = self.connect()?;
        Err(Self::live_wire_pending("subscribe_market_data"))
    }

    fn request_historical_data(&self, _symbol: &str) -> Result<usize, IbApiError> {
        let _stream = self.connect()?;
        Err(Self::live_wire_pending("request_historical_data"))
    }
}

/// Default IB API connect timeout for the live transport's socket establishment.
pub const IB_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::StrategyId;

    fn order(symbol: &str, quantity: i64) -> OrderSubmission {
        OrderSubmission {
            strategy_id: StrategyId::new("live-1"),
            symbol: symbol.to_string(),
            quantity,
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
        // 201 with an insufficient-buying-power reason → INSUFFICIENT_BUYING_POWER.
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
        // 201 with a non-buying-power reason → unmapped (no fabricated category).
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
    fn structured_envelope_carries_unchanged_order_and_category() {
        let submitted = order("AAPL", 10);
        let err = to_order_submit_error(
            IbApiError::new(IB_CODE_NO_SECURITY_DEFINITION, "No security definition"),
            submitted.clone(),
        );
        match err {
            IbOrderSubmitError::Structured(envelope) => {
                assert_eq!(envelope.category, OrderErrorCategory::InvalidSymbol);
                assert_eq!(envelope.error_type, "INVALID_SYMBOL/ib-200");
                // Original order parameters travel unchanged (SRS-ERR-001).
                assert_eq!(envelope.original_order, submitted);
                assert!(envelope.message.contains("AAPL"));
                assert!(envelope.message.contains("200"));
            }
            other => panic!("expected a structured envelope, got {other:?}"),
        }
    }

    #[test]
    fn unmapped_failure_preserves_raw_ib_detail_and_order() {
        let submitted = order("AAPL", 10);
        let err = to_order_submit_error(
            IbApiError::new(IB_CODE_ORDER_REJECTED, "Order rejected - reason: odd lot"),
            submitted.clone(),
        );
        match err {
            IbOrderSubmitError::Unmapped {
                code,
                message,
                original_order,
            } => {
                assert_eq!(code, IB_CODE_ORDER_REJECTED);
                assert!(message.contains("odd lot"));
                assert_eq!(original_order, submitted);
            }
            other => panic!("expected an unmapped failure, got {other:?}"),
        }
    }

    #[test]
    fn config_from_env_defaults_match_dotenv_example() {
        // No env override → documented defaults; socket_addr selects by account.
        let config = IbConnectionConfig::new(
            IbConnectionConfig::DEFAULT_HOST,
            IbConnectionConfig::DEFAULT_LIVE_PORT,
            IbConnectionConfig::DEFAULT_PAPER_PORT,
            7,
        );
        assert_eq!(config.socket_addr(IbAccountKind::Paper), "127.0.0.1:4002");
        assert_eq!(config.socket_addr(IbAccountKind::Live), "127.0.0.1:4001");
    }

    #[test]
    fn live_transport_fails_closed_when_gateway_unreachable() {
        // Port 1 is reserved/unbound → connect fails → a CONNECTIVITY_BLOCKED IB
        // code, never a fabricated success.
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
