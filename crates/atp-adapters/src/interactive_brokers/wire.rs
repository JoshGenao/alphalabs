//! TWS wire protocol for the live [`TcpIbGateway`](super::TcpIbGateway)
//! transport — v100+ framing pinned to server version
//! [`IB_PINNED_SERVER_VERSION`](super::IB_PINNED_SERVER_VERSION) (SRS-EXE-006).
//!
//! # Framing
//!
//! Every message is a 4-byte big-endian length prefix followed by
//! NUL-terminated UTF-8 fields (`field\0field\0…\0`). Frames are capped at
//! [`MAX_FRAME`] in both directions: an oversized inbound length fails closed
//! before any allocation (a corrupt peer must never OOM the live path).
//!
//! # Version pinning
//!
//! The handshake offers exactly `v176..176`, so any modern IB Gateway
//! (10.19+) negotiates server version **176** and the byte layout of every
//! message below is deterministic — no per-field server-version conditionals.
//! A gateway that answers any other version fails closed with
//! [`IB_CODE_UNSUPPORTED_SERVER_VERSION`](super::IB_CODE_UNSUPPORTED_SERVER_VERSION)
//! (operator remedy: upgrade the gateway). Field sequences mirror the official
//! `ibapi` 10.19.4 client at that version; the fake-gateway suite
//! (`tests/srs_exe_006_ib_wire.rs`) pins them as golden vectors.
//!
//! # Bounded waits
//!
//! Every read is bounded twice: by the socket read timeout set at connect
//! (`IB_CONNECT_TIMEOUT`, or the session's shorter operation deadline) and by
//! the per-operation deadline. A mute or black-holed gateway FAILS
//! ([`IB_CODE_WIRE_TIMEOUT`](super::IB_CODE_WIRE_TIMEOUT), classified
//! `CONNECTIVITY_BLOCKED`) — it never hangs the live path.
//!
//! # Honesty
//!
//! Inbound `error(4)` frames are classified: connection-farm notices
//! (2100–2169 with request id −1), delayed-market-data notices (10167),
//! order-warning notices (399) and the historical-timezone warning (2174) are
//! skipped; everything else addressed to the in-flight request surfaces as an
//! [`IbApiError`] through the adapter's SYS-64 classifier — never dropped,
//! never fabricated into a success.

use super::{
    IbApiError, IB_CODE_NOT_CONNECTED, IB_CODE_UNSUPPORTED_REQUEST,
    IB_CODE_UNSUPPORTED_SERVER_VERSION, IB_CODE_WIRE_TIMEOUT, IB_PINNED_SERVER_VERSION,
};
use crate::{
    DataBatch, HistoricalBar, HistoricalDataRequest, HistoricalQueryResult, MarketDataChannel,
    MarketDataSubscription, SubscriptionReceipt,
};
use atp_types::{AssetClass, OrderErrorCategory, OrderReceipt, OrderSubmission, OrderType};
use std::io::{ErrorKind, Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};

/// Hard ceiling on one frame in either direction (mirrors the TWS API's
/// 0xFFFFFF maximum message size). An inbound length above this fails closed
/// before any allocation.
pub const MAX_FRAME: usize = 0xFF_FFFF;

/// How long the handshake waits for `nextValidId` before nudging the gateway
/// once with `reqIds` (the documented fallback; TWS normally answers
/// `startApi` unprompted).
const NEXT_VALID_ID_NUDGE: Duration = Duration::from_secs(2);

/// Socket read-timeout tick for an established session: every blocking read
/// wakes at this cadence to re-check its operation deadline, so deadline
/// responsiveness never depends on the (longer) connect timeout.
const READ_TICK: Duration = Duration::from_millis(250);

// ----- inbound message ids (ibapi `message.IN`, server version 176) ----- //
const IN_TICK_PRICE: &str = "1";
const IN_TICK_SIZE: &str = "2";
const IN_ORDER_STATUS: &str = "3";
const IN_ERR_MSG: &str = "4";
const IN_NEXT_VALID_ID: &str = "9";
const IN_MANAGED_ACCTS: &str = "15";
const IN_HISTORICAL_DATA: &str = "17";
const IN_MARKET_DATA_TYPE: &str = "58";
const IN_POSITION_DATA: &str = "61";
const IN_POSITION_END: &str = "62";
const IN_ACCOUNT_SUMMARY: &str = "63";
const IN_ACCOUNT_SUMMARY_END: &str = "64";
const IN_TICK_REQ_PARAMS: &str = "81";

/// `ibapi` 10.19.4 `EClient.placeOrder` at server version 176 — 115 fields.
/// Parameterized slots (1=orderId, 3=symbol, 16=action, 17=quantity,
/// 18=orderType, 19=lmtPrice, 20=auxPrice) are blank in the template and
/// filled per order. The `1.7976931348623157e+308` / `2147483647` literals are
/// ibapi's UNSET_DOUBLE / UNSET_INTEGER sentinels, sent verbatim.
const PLACE_ORDER_TEMPLATE: [&str; 115] = [
    "3",
    "",
    "0",
    "",
    "STK",
    "",
    "0.0",
    "",
    "",
    "SMART",
    "",
    "USD",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "0",
    "",
    "1",
    "0",
    "0",
    "0",
    "0",
    "0",
    "0",
    "0",
    "",
    "0",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "0",
    "",
    "-1",
    "0",
    "",
    "",
    "0",
    "",
    "",
    "0",
    "0",
    "",
    "0",
    "",
    "",
    "",
    "",
    "",
    "0",
    "",
    "",
    "",
    "",
    "0",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "",
    "0",
    "",
    "",
    "0",
    "0",
    "",
    "",
    "0",
    "",
    "0",
    "0",
    "0",
    "0",
    "",
    "1.7976931348623157e+308",
    "1.7976931348623157e+308",
    "1.7976931348623157e+308",
    "1.7976931348623157e+308",
    "1.7976931348623157e+308",
    "0",
    "",
    "",
    "",
    "1.7976931348623157e+308",
    "",
    "",
    "",
    "",
    "0",
    "0",
    "0",
    "",
    "2147483647",
    "2147483647",
    "0",
    "",
    "",
];

// --------------------------------------------------------------------------- //
// Framing
// --------------------------------------------------------------------------- //

fn timeout_error(operation: &str) -> IbApiError {
    IbApiError::new(
        IB_CODE_WIRE_TIMEOUT,
        format!("IB Gateway did not answer `{operation}` within the operation deadline"),
    )
}

fn transport_error(detail: impl Into<String>) -> IbApiError {
    IbApiError::new(IB_CODE_NOT_CONNECTED, detail.into())
}

fn unsupported(detail: impl Into<String>) -> IbApiError {
    IbApiError::new(IB_CODE_UNSUPPORTED_REQUEST, detail.into())
}

/// Encode the one-off handshake payload: 4-byte big-endian length + the RAW
/// version-range string with **no** NUL terminator (ibapi `comm.make_msg` on
/// the bare string — unlike every later message, whose fields are each
/// NUL-terminated via `make_field`). A trailing NUL here makes the real
/// gateway go silent.
fn encode_handshake_frame(text: &str) -> Vec<u8> {
    let mut frame = Vec::with_capacity(4 + text.len());
    frame.extend_from_slice(&(text.len() as u32).to_be_bytes());
    frame.extend_from_slice(text.as_bytes());
    frame
}

/// Encode one outbound frame: 4-byte big-endian length + NUL-terminated fields.
///
/// Every field is validated to contain **no ASCII control byte** (`< 0x20`)
/// before encoding: the frame delimiter is NUL, so a strategy-supplied symbol
/// carrying an embedded `\0` (or any control char) could otherwise shift the
/// remaining fields and send a malformed/misinterpreted broker request. A
/// control byte fails the operation closed here, before any bytes reach the
/// socket — never a silently mangled order.
fn encode_frame(fields: &[String]) -> Result<Vec<u8>, IbApiError> {
    for field in fields {
        if let Some(bad) = field.bytes().find(|&b| b < 0x20) {
            return Err(unsupported(format!(
                "outbound IB field {field:?} contains a control byte (0x{bad:02x}); \
                 refusing to encode a frame that could shift the NUL-delimited layout"
            )));
        }
    }
    let payload_len: usize = fields.iter().map(|f| f.len() + 1).sum();
    if payload_len > MAX_FRAME {
        return Err(unsupported(format!(
            "outbound IB frame of {payload_len} bytes exceeds the {MAX_FRAME} byte ceiling"
        )));
    }
    let mut frame = Vec::with_capacity(4 + payload_len);
    frame.extend_from_slice(&(payload_len as u32).to_be_bytes());
    for field in fields {
        frame.extend_from_slice(field.as_bytes());
        frame.push(0);
    }
    Ok(frame)
}

fn write_all(stream: &mut TcpStream, bytes: &[u8]) -> Result<(), IbApiError> {
    stream
        .write_all(bytes)
        .map_err(|err| transport_error(format!("IB Gateway socket write failed: {err}")))
}

/// Fill `buf` from the stream, re-checking the operation deadline on every
/// socket-timeout tick so a mute gateway FAILS instead of hanging.
fn read_exact_deadline(
    stream: &mut TcpStream,
    buf: &mut [u8],
    deadline: Instant,
    operation: &str,
) -> Result<(), IbApiError> {
    let mut filled = 0;
    while filled < buf.len() {
        if Instant::now() >= deadline {
            return Err(timeout_error(operation));
        }
        match stream.read(&mut buf[filled..]) {
            Ok(0) => {
                return Err(transport_error(
                    "IB Gateway closed the connection mid-frame",
                ))
            }
            Ok(n) => filled += n,
            Err(err)
                if err.kind() == ErrorKind::WouldBlock || err.kind() == ErrorKind::TimedOut =>
            {
                // Socket-timeout tick; loop re-checks the operation deadline.
            }
            Err(err) => {
                return Err(transport_error(format!(
                    "IB Gateway socket read failed: {err}"
                )))
            }
        }
    }
    Ok(())
}

/// Read one inbound frame into its NUL-separated fields, deadline-bounded.
fn read_frame(
    stream: &mut TcpStream,
    deadline: Instant,
    operation: &str,
) -> Result<Vec<String>, IbApiError> {
    let mut header = [0u8; 4];
    read_exact_deadline(stream, &mut header, deadline, operation)?;
    let length = u32::from_be_bytes(header) as usize;
    if length > MAX_FRAME {
        // Fail closed BEFORE allocating: a corrupt length must not OOM the
        // live path. Transport-level fault → the session is dropped.
        return Err(transport_error(format!(
            "inbound IB frame length {length} exceeds the {MAX_FRAME} byte ceiling"
        )));
    }
    let mut payload = vec![0u8; length];
    read_exact_deadline(stream, &mut payload, deadline, operation)?;
    // ibapi framing: every field is NUL-terminated, so the payload splits into
    // fields plus one trailing empty chunk (dropped).
    let mut fields: Vec<String> = payload
        .split(|&b| b == 0)
        .map(|chunk| String::from_utf8_lossy(chunk).into_owned())
        .collect();
    if fields.last().is_some_and(String::is_empty) {
        fields.pop();
    }
    Ok(fields)
}

// --------------------------------------------------------------------------- //
// Inbound error(4) classification
// --------------------------------------------------------------------------- //

struct ErrFrame {
    req_id: i64,
    code: i32,
    message: String,
}

/// Parse an inbound `error(4)` frame (`[4, version, reqId, code, message,
/// advancedOrderRejectJson]` at v176). Malformed error frames surface as a
/// transport fault rather than being silently skipped.
fn parse_err_frame(fields: &[String]) -> Result<ErrFrame, IbApiError> {
    let (req_id, code) = match (fields.get(2), fields.get(3)) {
        (Some(req), Some(code)) => (req.parse::<i64>().ok(), code.parse::<i32>().ok()),
        _ => (None, None),
    };
    match (req_id, code) {
        (Some(req_id), Some(code)) => Ok(ErrFrame {
            req_id,
            code,
            message: fields.get(4).cloned().unwrap_or_default(),
        }),
        _ => Err(transport_error(format!(
            "IB Gateway sent a malformed error frame: {fields:?}"
        ))),
    }
}

/// Whether a code is a connectivity fault — one the adapter classifies as
/// [`OrderErrorCategory::ConnectivityBlocked`] (couldn't-connect / not-connected
/// / connectivity-lost 1100 / connectivity-broken 2110 / wire-timeout). Shared
/// by the notice filter and the session-drop decision so they can never
/// disagree about what "the socket is dead" means.
fn is_connectivity_fault(code: i32) -> bool {
    matches!(
        super::classify_ib_order_error(&IbApiError::new(code, String::new())),
        Some(OrderErrorCategory::ConnectivityBlocked)
    )
}

/// Whether an `error(4)` frame is an informational notice (skipped), not a
/// failure of the in-flight request: connection-farm status (2100–2169 with
/// request id −1), "displaying delayed market data" (10167), order warnings
/// (399, e.g. "will not be placed until market open") and the historical
/// end-date timezone warning (2174).
///
/// A connectivity FAULT is never a benign notice, even inside the farm-status
/// range: e.g. 2110 "connectivity between TWS and server is broken" falls in
/// 2100–2169 but means the socket is dead, so it must fail the op (and drop the
/// session) rather than be skipped.
fn is_informational_notice(err: &ErrFrame) -> bool {
    if is_connectivity_fault(err.code) {
        return false;
    }
    (err.req_id == -1 && (2100..=2169).contains(&err.code))
        || err.code == 10_167
        || err.code == 399
        || err.code == 2_174
}

// --------------------------------------------------------------------------- //
// Field helpers
// --------------------------------------------------------------------------- //

/// Format a strictly-positive minor-unit price as the decimal-dollar wire
/// field (`12345` → `"123.45"`). TWS parses any decimal representation; two
/// fixed decimals is this adapter's canonical form.
fn price_field(price_minor: i64) -> Result<String, IbApiError> {
    if price_minor <= 0 {
        return Err(unsupported(format!(
            "non-positive minor-unit price {price_minor} reached the IB wire encoder"
        )));
    }
    Ok(format!("{}.{:02}", price_minor / 100, price_minor % 100))
}

/// Day number of a civil date (Howard Hinnant's `days_from_civil`), used to
/// derive the IB `durationStr` from the request's inclusive date range
/// without a calendar dependency.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let adjusted_year = if month <= 2 { year - 1 } else { year };
    let era = if adjusted_year >= 0 {
        adjusted_year
    } else {
        adjusted_year - 399
    } / 400;
    let year_of_era = adjusted_year - era * 400;
    let month_shifted = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * month_shifted + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

/// Days in a civil month (proleptic Gregorian, leap-year aware).
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

/// Parse a strict `YYYY-MM-DD` **civil** date, failing closed on anything
/// else — including impossible dates like `2026-02-31`, which must never be
/// turned into a duration or sent to the broker.
fn parse_ymd(raw: &str) -> Result<(i64, i64, i64), IbApiError> {
    let bad = || unsupported(format!("`{raw}` is not a valid YYYY-MM-DD civil date"));
    let parts: Vec<&str> = raw.split('-').collect();
    if parts.len() != 3 || parts[0].len() != 4 || parts[1].len() != 2 || parts[2].len() != 2 {
        return Err(bad());
    }
    let year: i64 = parts[0].parse().map_err(|_| bad())?;
    let month: i64 = parts[1].parse().map_err(|_| bad())?;
    let day: i64 = parts[2].parse().map_err(|_| bad())?;
    if !(1..=12).contains(&month) || day < 1 || day > days_in_month(year, month) {
        return Err(bad());
    }
    Ok((year, month, day))
}

// --------------------------------------------------------------------------- //
// The live session
// --------------------------------------------------------------------------- //

/// One authenticated IB Gateway API session: the handshaken socket plus the
/// session-scoped id counters. Single-threaded request/reply — the owning
/// [`TcpIbGateway`](super::TcpIbGateway) serializes access through a mutex.
#[derive(Debug)]
pub struct IbSession {
    stream: TcpStream,
    /// Comma-separated managed account list announced at handshake (kept for
    /// diagnostics in the session's `Debug` output; SRS-EXE-001 will consume
    /// it when the live engine selects its designated account).
    accounts: String,
    next_order_id: i64,
    next_req_id: i64,
    market_data_type_sent: bool,
    op_deadline: Duration,
}

impl IbSession {
    /// Perform the v100+ handshake + `startApi` on a connected socket and wait
    /// for `nextValidId` (nudging once with `reqIds` if it is slow). Fails
    /// closed on any version other than the pinned one.
    pub fn establish(
        mut stream: TcpStream,
        client_id: i32,
        op_deadline: Duration,
    ) -> Result<Self, IbApiError> {
        // Fast read tick: blocking reads wake every READ_TICK to re-check the
        // per-operation deadline (the connect-time 5 s socket timeout is too
        // coarse for short deadlines and the nextValidId nudge window).
        stream
            .set_read_timeout(Some(READ_TICK))
            .map_err(|err| transport_error(format!("couldn't set the IB read tick: {err}")))?;
        let deadline = Instant::now() + op_deadline;
        write_all(&mut stream, b"API\0")?;
        let pinned = IB_PINNED_SERVER_VERSION;
        write_all(
            &mut stream,
            &encode_handshake_frame(&format!("v{pinned}..{pinned}")),
        )?;

        let reply = read_frame(&mut stream, deadline, "handshake")?;
        if reply.len() != 2 {
            return Err(transport_error(format!(
                "IB Gateway handshake reply had {} fields (expected server version + time)",
                reply.len()
            )));
        }
        let server_version: i32 = reply[0].parse().map_err(|_| {
            transport_error(format!(
                "IB Gateway sent a non-numeric server version `{}`",
                reply[0]
            ))
        })?;
        if server_version != pinned {
            return Err(IbApiError::new(
                IB_CODE_UNSUPPORTED_SERVER_VERSION,
                format!(
                    "IB Gateway negotiated server version {server_version}, but this adapter \
                     pins {pinned} (TWS API 10.19 line). Upgrade IB Gateway to 10.19+."
                ),
            ));
        }

        let mut session = Self {
            stream,
            accounts: String::new(),
            next_order_id: 0,
            next_req_id: 9_000,
            market_data_type_sent: false,
            op_deadline,
        };
        // startApi (71), version 2, client id, empty optionalCapabilities.
        session.send(&["71", "2", &client_id.to_string(), ""])?;

        // Await nextValidId(9); capture managedAccounts(15); skip notices.
        let mut nudged = false;
        let nudge_at = Instant::now() + NEXT_VALID_ID_NUDGE;
        loop {
            let step_deadline = if nudged {
                deadline
            } else {
                deadline.min(nudge_at)
            };
            match read_frame(&mut session.stream, step_deadline, "startApi") {
                Ok(fields) => match fields.first().map(String::as_str) {
                    Some(IN_NEXT_VALID_ID) => {
                        session.next_order_id =
                            fields.get(2).and_then(|f| f.parse().ok()).ok_or_else(|| {
                                transport_error("IB Gateway sent a malformed nextValidId frame")
                            })?;
                        return Ok(session);
                    }
                    Some(IN_MANAGED_ACCTS) => {
                        session.accounts = fields.get(2).cloned().unwrap_or_default();
                    }
                    Some(IN_ERR_MSG) => {
                        let err = parse_err_frame(&fields)?;
                        if !is_informational_notice(&err) {
                            return Err(IbApiError::new(err.code, err.message));
                        }
                    }
                    _ => {} // unrelated startup traffic — skip whole frames
                },
                Err(err) if err.code == IB_CODE_WIRE_TIMEOUT && !nudged => {
                    // The gateway did not volunteer nextValidId — nudge once.
                    nudged = true;
                    session.send(&["8", "1", "1"])?;
                }
                Err(err) => return Err(err),
            }
        }
    }

    fn send(&mut self, fields: &[&str]) -> Result<(), IbApiError> {
        let owned: Vec<String> = fields.iter().map(|f| (*f).to_string()).collect();
        self.send_owned(&owned)
    }

    fn send_owned(&mut self, fields: &[String]) -> Result<(), IbApiError> {
        let frame = encode_frame(fields)?;
        write_all(&mut self.stream, &frame)
    }

    fn recv(&mut self, deadline: Instant, operation: &str) -> Result<Vec<String>, IbApiError> {
        read_frame(&mut self.stream, deadline, operation)
    }

    fn deadline(&self) -> Instant {
        Instant::now() + self.op_deadline
    }

    /// Handle an inbound `error(4)` frame inside an op loop: notices are
    /// skipped (`Ok(())`); an error addressed to `req_id` (or a session-level
    /// error with request id −1) fails the operation.
    fn check_err_frame(&self, fields: &[String], req_id: i64) -> Result<(), IbApiError> {
        let err = parse_err_frame(fields)?;
        if is_informational_notice(&err) {
            return Ok(());
        }
        if err.req_id == req_id || err.req_id == -1 {
            return Err(IbApiError::new(err.code, err.message));
        }
        Ok(()) // someone else's request — not ours to fail on
    }

    // ----- operations ----- //

    /// `placeOrder(3)` → wait for an `orderStatus(3)` carrying our order id.
    /// Only a documented acknowledged/working status (`PendingSubmit` /
    /// `ApiPending` / `PreSubmitted` / `Submitted` / `Filled` — an
    /// out-of-hours market order parks `PreSubmitted`) yields a receipt; any
    /// terminal or unrecognized status fails closed, and an optimistic
    /// `openOrder(5)` echo is never acceptance (a rejection can still follow
    /// on the error channel), so the execution layer can never believe a dead
    /// order is live.
    pub fn submit_order(&mut self, order: &OrderSubmission) -> Result<OrderReceipt, IbApiError> {
        // SRS-EXE-003 — the shared fail-closed validation runs at EVERY live
        // entry point. The canonical adapter validates before reaching this
        // seam, but the transport is public: a direct caller must hit the same
        // wall (blank symbol, non-positive quantity, non-positive prices,
        // identity-less option orders) before any bytes reach the broker.
        if let Err(err) = order.validate() {
            return Err(unsupported(format!(
                "order failed SRS-EXE-003 validation at the live wire: {err}"
            )));
        }
        if order.asset_class != AssetClass::Equity {
            return Err(unsupported(format!(
                "the IB live wire encoder supports Equity (STK) orders; got {:?}",
                order.asset_class
            )));
        }
        let ib_type = match order.order_type {
            OrderType::Market => "MKT",
            OrderType::Limit { .. } => "LMT",
            OrderType::Stop { .. } => "STP",
            OrderType::StopLimit { .. } => "STP LMT",
        };
        let limit = match order.order_type.limit_price_minor() {
            Some(minor) => price_field(minor)?,
            None => String::new(),
        };
        let stop = match order.order_type.stop_price_minor() {
            Some(minor) => price_field(minor)?,
            None => String::new(),
        };

        let order_id = self.next_order_id;
        self.next_order_id += 1;

        let mut fields: Vec<String> = PLACE_ORDER_TEMPLATE
            .iter()
            .map(|f| (*f).to_string())
            .collect();
        fields[1] = order_id.to_string();
        fields[3] = order.symbol.clone();
        fields[16] = order.side.as_str().to_string();
        fields[17] = order.quantity.to_string();
        fields[18] = ib_type.to_string();
        fields[19] = limit;
        fields[20] = stop;
        self.send_owned(&fields)?;

        let deadline = self.deadline();
        let id_field = order_id.to_string();
        loop {
            let frame = self.recv(deadline, "placeOrder")?;
            match frame.first().map(String::as_str) {
                Some(IN_ORDER_STATUS) if frame.get(1) == Some(&id_field) => {
                    let status = frame.get(2).map(String::as_str).unwrap_or_default();
                    // Fail closed on anything that is not a documented
                    // acknowledged/working status: a terminal or unknown
                    // status must never be reported upstream as an accepted
                    // live order.
                    if matches!(
                        status,
                        "PendingSubmit" | "ApiPending" | "PreSubmitted" | "Submitted" | "Filled"
                    ) {
                        return Ok(OrderReceipt {
                            broker_order_id: id_field,
                        });
                    }
                    return Err(unsupported(format!(
                        "IB reported non-acknowledged status `{status}` for order {order_id} \
                         after submission"
                    )));
                }
                // openOrder(5) is NOT acceptance: IB emits it optimistically
                // before a rejection can arrive on the error channel, so only
                // an acknowledged orderStatus (above) or an error settles the
                // submission — an openOrder frame is skipped like other traffic.
                Some(IN_ERR_MSG) => self.check_err_frame(&frame, order_id)?,
                _ => {} // unrelated traffic (openOrder, ticks, other clients' events)
            }
        }
    }

    /// `cancelOrder(4)` → success ONLY on a **terminal** `Cancelled` /
    /// `ApiCancelled` order status or IB code 202 ("Order Canceled", reported
    /// through the error channel by design). `PendingCancel` is NOT success —
    /// the broker may still reject the cancellation — so the wait continues
    /// until a terminal signal or the bounded deadline.
    pub fn cancel_order(&mut self, broker_order_id: &str) -> Result<(), IbApiError> {
        let order_id: i64 = broker_order_id.parse().map_err(|_| {
            unsupported(format!(
                "`{broker_order_id}` is not a numeric IB broker order id"
            ))
        })?;
        self.send(&["4", "1", &order_id.to_string(), ""])?;

        let deadline = self.deadline();
        let id_field = order_id.to_string();
        loop {
            let frame = self.recv(deadline, "cancelOrder")?;
            match frame.first().map(String::as_str) {
                Some(IN_ORDER_STATUS) if frame.get(1) == Some(&id_field) => {
                    let status = frame.get(2).map(String::as_str).unwrap_or_default();
                    if matches!(status, "Cancelled" | "ApiCancelled") {
                        return Ok(());
                    }
                    // PendingCancel / working statuses: not terminal — wait.
                }
                Some(IN_ERR_MSG) => {
                    let err = parse_err_frame(&frame)?;
                    if err.req_id == order_id && err.code == 202 {
                        return Ok(()); // "Order Canceled" — the success signal
                    }
                    if is_informational_notice(&err) {
                        continue;
                    }
                    if err.req_id == order_id || err.req_id == -1 {
                        return Err(IbApiError::new(err.code, err.message));
                    }
                }
                _ => {}
            }
        }
    }

    /// `reqMktData(1)` (preceded once per session by `reqMarketDataType(59)`
    /// selecting delayed data — paper accounts often lack live entitlements).
    /// Confirmation is **protocol-level**: `marketDataType(58)`,
    /// `tickReqParams(81)`, or any tick for our ticker id — these arrive even
    /// with zero-entitlement delayed data, so an entitlement gap cannot fake a
    /// failure. A real subscribe error on our ticker still surfaces, including
    /// 10197 "no market data during competing live session" (IB is withholding
    /// the stream; the operator must free the competing session).
    pub fn subscribe_market_data(
        &mut self,
        request: &MarketDataSubscription,
    ) -> Result<SubscriptionReceipt, IbApiError> {
        match request.channel {
            MarketDataChannel::Trades | MarketDataChannel::Quotes | MarketDataChannel::Bars => {}
            MarketDataChannel::OptionChain => {
                return Err(unsupported(
                    "the IB live wire encoder does not yet subscribe option chains",
                ));
            }
        }
        if !self.market_data_type_sent {
            self.send(&["59", "1", "3"])?; // 3 = delayed
            self.market_data_type_sent = true;
        }
        let ticker_id = self.next_req_id;
        self.next_req_id += 1;
        let id_field = ticker_id.to_string();
        // ibapi 10.19.4 reqMktData at v176 (version 11), STK contract, no
        // generic ticks, not a snapshot.
        self.send(&[
            "1",
            "11",
            &id_field,
            "0",
            &request.symbol,
            "STK",
            "",
            "0.0",
            "",
            "",
            "SMART",
            "",
            "USD",
            "",
            "",
            "0",
            "",
            "0",
            "0",
            "",
        ])?;

        let deadline = self.deadline();
        loop {
            let frame = self.recv(deadline, "reqMktData")?;
            match frame.first().map(String::as_str) {
                Some(IN_MARKET_DATA_TYPE) if frame.get(2) == Some(&id_field) => break,
                Some(IN_TICK_REQ_PARAMS) if frame.get(1) == Some(&id_field) => break,
                Some(IN_TICK_PRICE | IN_TICK_SIZE) if frame.get(2) == Some(&id_field) => break,
                Some(IN_ERR_MSG) => {
                    let err = parse_err_frame(&frame)?;
                    if is_informational_notice(&err) {
                        continue;
                    }
                    // Any real error on our ticker fails the subscribe — including
                    // 10197 "no market data during competing live session", which
                    // means IB is WITHHOLDING the data stream (the subscription is
                    // inert), NOT a confirmation. The operator remedy is to close
                    // the competing live session, not to treat no-data as success.
                    if err.req_id == ticker_id || err.req_id == -1 {
                        return Err(IbApiError::new(err.code, err.message));
                    }
                }
                _ => {}
            }
        }
        Ok(SubscriptionReceipt {
            subscription_id: format!("ib-md-{ticker_id}"),
        })
    }

    /// `reqHistoricalData(20)` for daily TRADES bars over the request's
    /// inclusive `YYYY-MM-DD` range → decode `historicalData(17)`.
    pub fn historical_data(
        &mut self,
        request: &HistoricalDataRequest,
    ) -> Result<HistoricalQueryResult, IbApiError> {
        if request.asset_class != crate::AssetClass::Equity {
            return Err(unsupported(format!(
                "the IB live wire encoder encodes Equity (STK) historical requests; got {:?} — \
                 encoding it as STK would silently fetch the wrong instrument",
                request.asset_class
            )));
        }
        if request.resolution != "1d" {
            return Err(unsupported(format!(
                "the IB live wire encoder supports resolution `1d`; got `{}`",
                request.resolution
            )));
        }
        // IB daily TRADES bars are split-adjusted — that is the ONLY
        // normalization this wire can deliver honestly. Echoing any other
        // requested mode would mislabel raw split-adjusted closes as
        // dividend-adjusted / raw / total-return data.
        if request.normalization_mode != crate::NormalizationMode::SplitAdjusted {
            return Err(unsupported(format!(
                "IB daily TRADES bars are split-adjusted; the live wire cannot honestly serve \
                 {:?} (route adjusted reads through the point-in-time store instead)",
                request.normalization_mode
            )));
        }
        let (start_y, start_m, start_d) = parse_ymd(&request.start)?;
        let (end_y, end_m, end_d) = parse_ymd(&request.end)?;
        let span_days =
            days_from_civil(end_y, end_m, end_d) - days_from_civil(start_y, start_m, start_d) + 1;
        if span_days < 1 {
            return Err(unsupported(format!(
                "historical range `{}`..`{}` is empty",
                request.start, request.end
            )));
        }
        let req_id = self.next_req_id;
        self.next_req_id += 1;
        let id_field = req_id.to_string();
        let end_date_time = format!("{end_y:04}{end_m:02}{end_d:02}-23:59:59");
        let duration = format!("{span_days} D");
        // ibapi 10.19.4 reqHistoricalData at v176 (no version field), STK
        // contract, useRTH=1, whatToShow=TRADES, formatDate=1, keepUpToDate=0.
        self.send(&[
            "20",
            &id_field,
            "0",
            &request.symbol,
            "STK",
            "",
            "0.0",
            "",
            "",
            "SMART",
            "",
            "USD",
            "",
            "",
            "0",
            &end_date_time,
            "1 day",
            &duration,
            "1",
            "TRADES",
            "1",
            "0",
            "",
        ])?;

        let deadline = self.deadline();
        loop {
            let frame = self.recv(deadline, "reqHistoricalData")?;
            match frame.first().map(String::as_str) {
                Some(IN_HISTORICAL_DATA) if frame.get(1) == Some(&id_field) => {
                    return decode_historical_bars(&frame, request);
                }
                Some(IN_ERR_MSG) => self.check_err_frame(&frame, req_id)?,
                _ => {}
            }
        }
    }

    /// `reqAccountSummary(62)` → count `accountSummary(63)` rows until
    /// `accountSummaryEnd(64)`, then cancel the subscription (best-effort).
    pub fn account_status(&mut self) -> Result<DataBatch, IbApiError> {
        let req_id = self.next_req_id;
        self.next_req_id += 1;
        let id_field = req_id.to_string();
        self.send(&[
            "62",
            "1",
            &id_field,
            "All",
            "NetLiquidation,TotalCashValue,BuyingPower",
        ])?;

        let deadline = self.deadline();
        let mut records = 0usize;
        loop {
            let frame = self.recv(deadline, "reqAccountSummary")?;
            match frame.first().map(String::as_str) {
                Some(IN_ACCOUNT_SUMMARY) if frame.get(2) == Some(&id_field) => records += 1,
                Some(IN_ACCOUNT_SUMMARY_END) if frame.get(2) == Some(&id_field) => break,
                Some(IN_ERR_MSG) => self.check_err_frame(&frame, req_id)?,
                _ => {}
            }
        }
        // Unsubscribe is part of the operation: a failed cancel write means the
        // session is broken (the subscription would silently stream on), so the
        // failure propagates and the owning transport drops the session.
        self.send(&["63", "1", &id_field])?;
        Ok(DataBatch { records })
    }

    /// `reqPositions(61)` → count `position(61)` rows until `positionEnd(62)`
    /// (zero rows is an honest flat book), then cancel (best-effort).
    pub fn positions(&mut self) -> Result<DataBatch, IbApiError> {
        self.send(&["61", "1"])?;
        let deadline = self.deadline();
        let mut records = 0usize;
        loop {
            let frame = self.recv(deadline, "reqPositions")?;
            match frame.first().map(String::as_str) {
                Some(IN_POSITION_DATA) => records += 1,
                Some(IN_POSITION_END) => break,
                Some(IN_ERR_MSG) => self.check_err_frame(&frame, -1)?,
                _ => {}
            }
        }
        // As above: a failed cancelPositions write is a transport fault, not a
        // silently-ignored cleanup.
        self.send(&["64", "1"])?;
        Ok(DataBatch { records })
    }
}

/// Decode a `historicalData(17)` frame at v176:
/// `[17, reqId, startDate, endDate, barCount, {date, open, high, low, close,
/// volume, wap, count} × barCount]`. Malformed frames fail closed.
fn decode_historical_bars(
    frame: &[String],
    request: &HistoricalDataRequest,
) -> Result<HistoricalQueryResult, IbApiError> {
    const HEADER: usize = 5;
    const PER_BAR: usize = 8;
    const CLOSE_OFFSET: usize = 4;
    let malformed = || transport_error("IB Gateway sent a malformed historicalData frame");
    let count: usize = frame
        .get(4)
        .and_then(|f| f.parse().ok())
        .ok_or_else(malformed)?;
    if frame.len() != HEADER + count * PER_BAR {
        return Err(malformed());
    }
    let mut bars = Vec::with_capacity(count);
    for index in 0..count {
        let close_field = &frame[HEADER + index * PER_BAR + CLOSE_OFFSET];
        let close: f64 = close_field.parse().map_err(|_| malformed())?;
        bars.push(HistoricalBar {
            symbol: request.symbol.clone(),
            close,
        });
    }
    Ok(HistoricalQueryResult {
        symbol: request.symbol.clone(),
        asset_class: request.asset_class,
        normalization_mode: request.normalization_mode,
        bars,
    })
}

/// Whether an error code indicates the underlying session is unusable, so the
/// owning transport drops it and reconnects on the next call.
///
/// Any [`is_connectivity_fault`] code (couldn't-connect 502, not-connected 504,
/// connectivity-lost 1100, connectivity-broken 2110, wire-timeout) means the
/// socket is dead. Deriving it from the classifier — rather than a
/// hand-maintained list — guarantees the drop-and-reconnect set can never
/// drift from what the adapter calls a connectivity fault. A pinned-version
/// mismatch is added explicitly: it is not a connectivity category but still
/// requires a fresh (re-pinned) handshake.
pub fn is_transport_fault(code: i32) -> bool {
    is_connectivity_fault(code) || code == IB_CODE_UNSUPPORTED_SERVER_VERSION
}
