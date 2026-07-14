//! SRS-EXE-006 — TWS wire protocol over a scripted fake IB Gateway.
//!
//! Exercises the live [`TcpIbGateway`] transport end-to-end against a real TCP
//! socket served by a deterministic in-process fake gateway (ephemeral loopback
//! port — parallel-safe, no real IB anywhere). Outbound frames are compared
//! against **golden vectors generated from the official `ibapi` 10.19.4 client
//! at the pinned server version 176** (price fields use this adapter's
//! canonical two-decimal form — numerically identical to ibapi's float repr;
//! TWS parses either), so an encoder drift from the real TWS layout fails here
//! before it ever reaches the operator's paper-account run.
//!
//! The suite also proves the fail-closed edges a live path must have: pinned
//! server-version rejection, informational-notice skipping, rejection→SYS-64
//! classification through the canonical adapter boundary, bounded waits (a
//! mute gateway FAILS, never hangs), oversized-frame refusal, and one
//! handshake per cached session.

#![cfg(feature = "ib-live-transport")]

use atp_adapters::interactive_brokers::{
    classify_ib_order_error, IbAccountKind, IbConnectionConfig, IbGatewayConnection,
    InteractiveBrokersBrokerage, TcpIbGateway, IB_CODE_NOT_CONNECTED, IB_CODE_UNSUPPORTED_REQUEST,
    IB_CODE_UNSUPPORTED_SERVER_VERSION, IB_CODE_WIRE_TIMEOUT,
};
use atp_adapters::{
    AdapterError, BrokerageAdapter, HistoricalDataRequest, MarketDataChannel,
    MarketDataSubscription, NormalizationMode,
};
use atp_types::{OrderErrorCategory, OrderSide, OrderSubmission, OrderType, StrategyId};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

// --------------------------------------------------------------------------- //
// Golden vectors — generated from ibapi 10.19.4 EClient at serverVersion 176.
// --------------------------------------------------------------------------- //

const GOLDEN_VERSION_RANGE: [&str; 1] = ["v176..176"];
const GOLDEN_START_API: [&str; 4] = ["71", "2", "101", ""];
const GOLDEN_PLACE_MARKET_BUY: [&str; 115] = [
    "3",
    "5001",
    "0",
    "AAPL",
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
    "BUY",
    "1",
    "MKT",
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
const GOLDEN_PLACE_LIMIT_SELL: [&str; 115] = [
    "3",
    "5002",
    "0",
    "AAPL",
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
    "SELL",
    "2",
    "LMT",
    "123.45",
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
// Price fields below are this adapter's canonical two-decimal form ("99.10");
// ibapi's float repr emits "99.1" — numerically identical on the wire.
const GOLDEN_PLACE_STOP_SELL: [&str; 115] = [
    "3",
    "5003",
    "0",
    "AAPL",
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
    "SELL",
    "1",
    "STP",
    "",
    "99.10",
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
const GOLDEN_PLACE_STOP_LIMIT_BUY: [&str; 115] = [
    "3",
    "5004",
    "0",
    "AAPL",
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
    "BUY",
    "1",
    "STP LMT",
    "101.25",
    "100.50",
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
const GOLDEN_CANCEL_ORDER: [&str; 4] = ["4", "1", "5001", ""];
const GOLDEN_REQ_MARKET_DATA_TYPE: [&str; 3] = ["59", "1", "3"];
const GOLDEN_REQ_MKT_DATA: [&str; 20] = [
    "1", "11", "9000", "0", "AAPL", "STK", "", "0.0", "", "", "SMART", "", "USD", "", "", "0", "",
    "0", "0", "",
];
const GOLDEN_REQ_HISTORICAL: [&str; 23] = [
    "20",
    "9000",
    "0",
    "AAPL",
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
    "20260201-23:59:59",
    "1 day",
    "32 D",
    "1",
    "TRADES",
    "1",
    "0",
    "",
];
const GOLDEN_REQ_ACCOUNT_SUMMARY: [&str; 5] = [
    "62",
    "1",
    "9000",
    "All",
    "NetLiquidation,TotalCashValue,BuyingPower",
];
const GOLDEN_CANCEL_ACCOUNT_SUMMARY: [&str; 3] = ["63", "1", "9000"];
const GOLDEN_REQ_POSITIONS: [&str; 2] = ["61", "1"];
const GOLDEN_CANCEL_POSITIONS: [&str; 2] = ["64", "1"];

// --------------------------------------------------------------------------- //
// Fake gateway harness — an independent framing implementation (deliberately
// NOT the adapter's), so the two sides cross-check each other.
// --------------------------------------------------------------------------- //

fn server_read_frame(stream: &mut TcpStream) -> Vec<String> {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).expect("frame header");
    let length = u32::from_be_bytes(header) as usize;
    let mut payload = vec![0u8; length];
    stream.read_exact(&mut payload).expect("frame payload");
    let mut fields: Vec<String> = payload
        .split(|&b| b == 0)
        .map(|chunk| String::from_utf8(chunk.to_vec()).expect("utf-8 field"))
        .collect();
    assert_eq!(
        fields.pop().as_deref(),
        Some(""),
        "fields are NUL-terminated"
    );
    fields
}

/// Read the one-off handshake payload: a RAW length-prefixed string with **no**
/// NUL terminator (ibapi `comm.make_msg` on the bare version range). The real
/// gateway goes silent on a NUL-terminated handshake, so the fake asserts the
/// raw form strictly.
fn server_read_raw_handshake(stream: &mut TcpStream) -> String {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).expect("handshake header");
    let length = u32::from_be_bytes(header) as usize;
    let mut payload = vec![0u8; length];
    stream.read_exact(&mut payload).expect("handshake payload");
    assert!(
        !payload.contains(&0),
        "the version-range handshake must be RAW (no NUL terminator) — \
         a trailing NUL makes the real IB Gateway go silent"
    );
    String::from_utf8(payload).expect("utf-8 handshake")
}

fn server_write_frame(stream: &mut TcpStream, fields: &[&str]) {
    let payload_len: usize = fields.iter().map(|f| f.len() + 1).sum();
    let mut frame = Vec::with_capacity(4 + payload_len);
    frame.extend_from_slice(&(payload_len as u32).to_be_bytes());
    for field in fields {
        frame.extend_from_slice(field.as_bytes());
        frame.push(0);
    }
    stream.write_all(&frame).expect("frame write");
}

/// Serve the standard v176 handshake: assert the raw `API\0` prefix and the
/// pinned version range, answer version+time, read `startApi` (forwarded to
/// the capture channel), announce accounts + `nextValidId`.
fn server_handshake(stream: &mut TcpStream, captured: &Sender<Vec<String>>, next_valid_id: i64) {
    let mut prefix = [0u8; 4];
    stream.read_exact(&mut prefix).expect("API prefix");
    assert_eq!(
        &prefix, b"API\0",
        "handshake must begin with the raw API prefix"
    );
    captured
        .send(vec![server_read_raw_handshake(stream)])
        .unwrap();
    server_write_frame(stream, &["176", "20260714 22:00:00 UTC"]);
    captured.send(server_read_frame(stream)).unwrap();
    server_write_frame(stream, &["15", "1", "DU1234567"]);
    server_write_frame(stream, &["9", "1", &next_valid_id.to_string()]);
}

/// Drain and discard everything the client sends until it closes — for tests
/// where the operation is expected to fail closed (so the exact post-handshake
/// frames are irrelevant and must not panic the gateway thread on EOF).
fn server_drain(stream: &mut TcpStream) {
    let mut buf = [0u8; 1024];
    while stream.read(&mut buf).map(|n| n > 0).unwrap_or(false) {}
}

/// Spawn a scripted gateway on an ephemeral loopback port. Returns the gateway
/// transport wired at that port, the capture channel of frames the server
/// read, and the server thread handle (joined by `finish`).
fn scripted_gateway(
    op_deadline: Duration,
    script: impl FnOnce(TcpStream, Sender<Vec<String>>) + Send + 'static,
) -> (TcpIbGateway, Receiver<Vec<String>>, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("ephemeral loopback listener");
    let port = listener.local_addr().unwrap().port();
    let (sender, receiver) = channel();
    let handle = std::thread::spawn(move || {
        let (stream, _) = listener.accept().expect("client connection");
        script(stream, sender);
    });
    let config = IbConnectionConfig::new("127.0.0.1", port, port, 101);
    let gateway = TcpIbGateway::with_op_deadline(config, IbAccountKind::Paper, op_deadline);
    (gateway, receiver, handle)
}

fn finish(handle: JoinHandle<()>) {
    handle.join().expect("fake gateway thread panicked");
}

const NORMAL_DEADLINE: Duration = Duration::from_secs(4);

fn market_order(symbol: &str, quantity: i64) -> OrderSubmission {
    OrderSubmission {
        strategy_id: StrategyId::new("live-1"),
        symbol: symbol.to_string(),
        quantity,
        asset_class: atp_types::AssetClass::Equity,
        side: OrderSide::Buy,
        order_type: OrderType::Market,
    }
}

fn quotes(symbol: &str) -> MarketDataSubscription {
    MarketDataSubscription {
        symbol: symbol.to_string(),
        channel: MarketDataChannel::Quotes,
    }
}

fn daily(symbol: &str) -> HistoricalDataRequest {
    HistoricalDataRequest {
        symbol: symbol.to_string(),
        start: "2026-01-01".to_string(),
        end: "2026-02-01".to_string(),
        resolution: "1d".to_string(),
        asset_class: atp_adapters::AssetClass::Equity,
        normalization_mode: NormalizationMode::SplitAdjusted,
    }
}

fn assert_golden(frame: &[String], golden: &[&str], label: &str) {
    let got: Vec<&str> = frame.iter().map(String::as_str).collect();
    assert_eq!(
        got, golden,
        "{label} frame drifted from the ibapi-10.19.4 golden"
    );
}

// --------------------------------------------------------------------------- //
// Handshake
// --------------------------------------------------------------------------- //

#[test]
fn handshake_sends_api_prefix_and_pinned_range_then_start_api() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqPositions
        server_write_frame(&mut stream, &["62", "1"]); // positionEnd
        let _ = server_read_frame(&mut stream); // best-effort cancelPositions
    });
    let batch = gateway
        .positions()
        .expect("positions over the handshaken session");
    assert_eq!(batch.records, 0);
    finish(handle);
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_VERSION_RANGE,
        "version range",
    );
    assert_golden(&captured.recv().unwrap(), &GOLDEN_START_API, "startApi");
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_POSITIONS,
        "reqPositions",
    );
}

#[test]
fn rejects_unpinned_server_version() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, _| {
        let mut prefix = [0u8; 4];
        stream.read_exact(&mut prefix).unwrap();
        let _ = server_read_raw_handshake(&mut stream);
        server_write_frame(&mut stream, &["175", "20260714 22:00:00 UTC"]);
    });
    let err = gateway
        .positions()
        .expect_err("a non-pinned server version must fail closed");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_SERVER_VERSION);
    assert!(
        err.message.contains("176"),
        "message names the pinned version: {err:?}"
    );
    finish(handle);
}

#[test]
fn slow_next_valid_id_is_nudged_with_req_ids() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        let mut prefix = [0u8; 4];
        stream.read_exact(&mut prefix).unwrap();
        sender
            .send(vec![server_read_raw_handshake(&mut stream)])
            .unwrap();
        server_write_frame(&mut stream, &["176", "20260714 22:00:00 UTC"]);
        sender.send(server_read_frame(&mut stream)).unwrap(); // startApi
                                                              // Withhold nextValidId until the client nudges with reqIds(8).
        let nudge = server_read_frame(&mut stream);
        assert_eq!(nudge, vec!["8", "1", "1"], "expected the reqIds nudge");
        server_write_frame(&mut stream, &["9", "1", "5001"]);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqPositions
        server_write_frame(&mut stream, &["62", "1"]);
        let _ = server_read_frame(&mut stream);
    });
    let batch = gateway
        .positions()
        .expect("session established via the reqIds nudge");
    assert_eq!(batch.records, 0);
    finish(handle);
}

// --------------------------------------------------------------------------- //
// Orders
// --------------------------------------------------------------------------- //

#[test]
fn all_order_types_encode_the_pinned_golden_frames() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        for order_id in ["5001", "5002", "5003", "5004"] {
            sender.send(server_read_frame(&mut stream)).unwrap();
            server_write_frame(
                &mut stream,
                &[
                    "3",
                    order_id,
                    "PreSubmitted",
                    "0",
                    "1",
                    "0.0",
                    "1",
                    "0",
                    "0.0",
                    "101",
                    "",
                    "0.0",
                ],
            );
        }
    });

    let mut submissions = [
        market_order("AAPL", 1),
        market_order("AAPL", 2),
        market_order("AAPL", 1),
        market_order("AAPL", 1),
    ];
    submissions[1].side = OrderSide::Sell;
    submissions[1].order_type = OrderType::Limit {
        limit_price_minor: 12_345,
    };
    submissions[2].side = OrderSide::Sell;
    submissions[2].order_type = OrderType::Stop {
        stop_price_minor: 9_910,
    };
    submissions[3].order_type = OrderType::StopLimit {
        stop_price_minor: 10_050,
        limit_price_minor: 10_125,
    };

    for (submission, expected_id) in submissions.iter().zip(["5001", "5002", "5003", "5004"]) {
        let receipt = gateway
            .submit_order(submission)
            .expect("broker acknowledges");
        assert_eq!(receipt.broker_order_id, expected_id);
    }
    finish(handle);

    let _ = captured.recv().unwrap(); // version range
    let _ = captured.recv().unwrap(); // startApi
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_PLACE_MARKET_BUY,
        "placeOrder MKT BUY",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_PLACE_LIMIT_SELL,
        "placeOrder LMT SELL",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_PLACE_STOP_SELL,
        "placeOrder STP SELL",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_PLACE_STOP_LIMIT_BUY,
        "placeOrder STP LMT BUY",
    );
}

#[test]
fn order_rejection_flows_through_the_sys64_classifier() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // placeOrder
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "5001",
                "201",
                "Order rejected - reason:Insufficient buying power to cover the order",
                "",
            ],
        );
    });
    // Through the CANONICAL adapter boundary: the raw wire rejection must
    // surface as AdapterError::Brokerage with the SYS-64 category attached.
    let adapter = InteractiveBrokersBrokerage::new(gateway);
    let err = adapter
        .submit_order(market_order("AAPL", 1))
        .expect_err("the paper rejection must surface");
    match err {
        AdapterError::Brokerage { category, code, .. } => {
            assert_eq!(category, Some(OrderErrorCategory::InsufficientBuyingPower));
            assert_eq!(code, 201);
        }
        other => panic!("expected AdapterError::Brokerage, got {other:?}"),
    }
    finish(handle);
}

#[test]
fn cancel_treats_code_202_as_success_and_encodes_the_golden() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // placeOrder
        server_write_frame(
            &mut stream,
            &[
                "3",
                "5001",
                "PreSubmitted",
                "0",
                "1",
                "0.0",
                "1",
                "0",
                "0.0",
                "101",
                "",
                "0.0",
            ],
        );
        sender.send(server_read_frame(&mut stream)).unwrap(); // cancelOrder
        server_write_frame(
            &mut stream,
            &["4", "2", "5001", "202", "Order Canceled - reason:", ""],
        );
    });
    let receipt = gateway
        .submit_order(&market_order("AAPL", 1))
        .expect("accepted");
    gateway
        .cancel_order(&receipt.broker_order_id)
        .expect("IB code 202 is the cancel success signal");
    finish(handle);
    let _ = captured.recv().unwrap();
    let _ = captured.recv().unwrap();
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_CANCEL_ORDER,
        "cancelOrder",
    );
}

// --------------------------------------------------------------------------- //
// Market data + historical
// --------------------------------------------------------------------------- //

#[test]
fn subscribe_confirms_on_protocol_level_ack_and_encodes_the_goldens() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqMarketDataType
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqMktData
                                                              // tickReqParams arrives even with zero market-data entitlements — the
                                                              // protocol-level confirmation the adapter accepts.
        server_write_frame(&mut stream, &["81", "9000", "0.01", "9c0001", "3"]);
    });
    let receipt = gateway
        .subscribe_market_data(&quotes("AAPL"))
        .expect("protocol-level subscription confirmation");
    assert_eq!(receipt.subscription_id, "ib-md-9000");
    finish(handle);
    let _ = captured.recv().unwrap();
    let _ = captured.recv().unwrap();
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_MARKET_DATA_TYPE,
        "reqMarketDataType(delayed)",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_MKT_DATA,
        "reqMktData",
    );
}

#[test]
fn historical_data_decodes_bars_and_encodes_the_golden() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqHistoricalData
        server_write_frame(
            &mut stream,
            &[
                "17",
                "9000",
                "20260101 00:00:00",
                "20260201 00:00:00",
                "2",
                "20260130",
                "100.0",
                "101.5",
                "99.5",
                "101.25",
                "1000",
                "100.9",
                "10",
                "20260131",
                "101.0",
                "102.0",
                "100.0",
                "102.5",
                "900",
                "101.2",
                "9",
            ],
        );
    });
    let result = gateway
        .historical_data(&daily("AAPL"))
        .expect("bars decoded");
    assert_eq!(result.symbol, "AAPL");
    assert_eq!(result.bars.len(), 2);
    assert_eq!(result.bars[0].close, 101.25);
    assert_eq!(result.bars[1].close, 102.5);
    assert!(result.bars.iter().all(|bar| bar.symbol == "AAPL"));
    finish(handle);
    let _ = captured.recv().unwrap();
    let _ = captured.recv().unwrap();
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_HISTORICAL,
        "reqHistoricalData",
    );
}

// --------------------------------------------------------------------------- //
// Account + positions
// --------------------------------------------------------------------------- //

#[test]
fn account_summary_counts_records_and_encodes_the_goldens() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqAccountSummary
        server_write_frame(
            &mut stream,
            &[
                "63",
                "1",
                "9000",
                "DU1234567",
                "NetLiquidation",
                "100000.00",
                "USD",
            ],
        );
        server_write_frame(
            &mut stream,
            &[
                "63",
                "1",
                "9000",
                "DU1234567",
                "BuyingPower",
                "400000.00",
                "USD",
            ],
        );
        server_write_frame(&mut stream, &["64", "1", "9000"]);
        sender.send(server_read_frame(&mut stream)).unwrap(); // cancelAccountSummary
    });
    let batch = gateway.account_status().expect("account summary");
    assert_eq!(batch.records, 2);
    finish(handle);
    let _ = captured.recv().unwrap();
    let _ = captured.recv().unwrap();
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_ACCOUNT_SUMMARY,
        "reqAccountSummary",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_CANCEL_ACCOUNT_SUMMARY,
        "cancelAccountSummary",
    );
}

#[test]
fn positions_empty_book_is_zero_records_ok() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqPositions
        server_write_frame(&mut stream, &["62", "1"]); // positionEnd — flat book
        sender.send(server_read_frame(&mut stream)).unwrap(); // cancelPositions
    });
    let batch = gateway
        .positions()
        .expect("a flat paper book is a valid result");
    assert_eq!(batch.records, 0);
    finish(handle);
    let _ = captured.recv().unwrap();
    let _ = captured.recv().unwrap();
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_REQ_POSITIONS,
        "reqPositions",
    );
    assert_golden(
        &captured.recv().unwrap(),
        &GOLDEN_CANCEL_POSITIONS,
        "cancelPositions",
    );
}

#[test]
fn positions_counts_rows_until_position_end() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream);
        server_write_frame(
            &mut stream,
            &[
                "61",
                "3",
                "DU1234567",
                "265598",
                "AAPL",
                "STK",
                "",
                "0.0",
                "",
                "",
                "SMART",
                "USD",
                "AAPL",
                "NMS",
                "5",
                "180.25",
            ],
        );
        server_write_frame(&mut stream, &["62", "1"]);
        let _ = server_read_frame(&mut stream);
    });
    let batch = gateway.positions().expect("positions");
    assert_eq!(batch.records, 1);
    finish(handle);
}

// --------------------------------------------------------------------------- //
// Honesty + bounded-wait edges
// --------------------------------------------------------------------------- //

#[test]
fn informational_notices_are_skipped_not_failures() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // reqPositions
                                                // Connection-farm notices (req id -1, 2100-2169) must be skipped.
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "-1",
                "2104",
                "Market data farm connection is OK:usfarm",
                "",
            ],
        );
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "-1",
                "2158",
                "Sec-def data farm connection is OK:secdefil",
                "",
            ],
        );
        server_write_frame(&mut stream, &["62", "1"]);
        let _ = server_read_frame(&mut stream);
    });
    let batch = gateway
        .positions()
        .expect("informational notices must not fail the in-flight operation");
    assert_eq!(batch.records, 0);
    finish(handle);
}

#[test]
fn silent_server_times_out_instead_of_hanging() {
    let (gateway, _captured, handle) =
        scripted_gateway(Duration::from_millis(400), |mut stream, sender| {
            server_handshake(&mut stream, &sender, 5001);
            let _ = server_read_frame(&mut stream); // reqPositions — then go mute
            std::thread::sleep(Duration::from_secs(3));
        });
    let started = Instant::now();
    let err = gateway
        .positions()
        .expect_err("a mute gateway must FAIL, never hang the live path");
    assert_eq!(err.code, IB_CODE_WIRE_TIMEOUT);
    assert!(
        started.elapsed() < Duration::from_secs(3),
        "timeout must honor the bounded operation deadline, took {:?}",
        started.elapsed()
    );
    finish(handle);
}

#[test]
fn oversized_inbound_frame_fails_closed_before_allocation() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // reqPositions
                                                // A corrupt 4 GiB length header: the client must refuse it outright.
        stream.write_all(&u32::MAX.to_be_bytes()).unwrap();
    });
    let err = gateway
        .positions()
        .expect_err("an oversized frame length must fail closed");
    assert_eq!(err.code, IB_CODE_NOT_CONNECTED);
    assert!(
        err.message.contains("ceiling"),
        "names the ceiling: {err:?}"
    );
    finish(handle);
}

#[test]
fn unencodable_requests_fail_closed_without_guessing() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        // No further frames: every request below is refused before any send.
    });

    let mut option_order = market_order("AAPL", 1);
    option_order.asset_class = atp_types::AssetClass::Option;
    let err = gateway
        .submit_order(&option_order)
        .expect_err("Option not encodable");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let err = gateway
        .cancel_order("not-a-number")
        .expect_err("non-numeric broker order id");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let mut hourly = daily("AAPL");
    hourly.resolution = "1h".to_string();
    let err = gateway
        .historical_data(&hourly)
        .expect_err("non-1d resolution");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let mut option_history = daily("AAPL");
    option_history.asset_class = atp_adapters::AssetClass::Option;
    let err = gateway
        .historical_data(&option_history)
        .expect_err("non-Equity history must not be silently encoded as STK");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let mut chain = quotes("AAPL");
    chain.channel = MarketDataChannel::OptionChain;
    let err = gateway
        .subscribe_market_data(&chain)
        .expect_err("option chain");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    finish(handle);
}

#[test]
fn cached_session_performs_exactly_one_handshake() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        for _ in 0..2 {
            sender.send(server_read_frame(&mut stream)).unwrap(); // reqPositions
            server_write_frame(&mut stream, &["62", "1"]);
            let _ = server_read_frame(&mut stream); // cancelPositions
        }
    });
    assert_eq!(gateway.positions().expect("first op").records, 0);
    assert_eq!(
        gateway
            .positions()
            .expect("second op reuses the session")
            .records,
        0
    );
    finish(handle);
    // Exactly one version-range + one startApi across both operations.
    let frames: Vec<Vec<String>> = captured.iter().collect();
    let handshakes = frames
        .iter()
        .filter(|f| f.first().map(String::as_str) == Some("71"))
        .count();
    assert_eq!(
        handshakes, 1,
        "the cached session must not re-handshake per op"
    );
    assert_eq!(frames.len(), 4, "range + startApi + two reqPositions");
}

#[test]
fn inactive_order_status_fails_closed_not_acknowledged() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // placeOrder
                                                // `Inactive` is a dead order (rejected/halted), NOT an acknowledgment.
        server_write_frame(
            &mut stream,
            &[
                "3", "5001", "Inactive", "0", "1", "0.0", "1", "0", "0.0", "101", "", "0.0",
            ],
        );
    });
    let err = gateway
        .submit_order(&market_order("AAPL", 1))
        .expect_err("an Inactive order must never be reported as accepted");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);
    assert!(
        err.message.contains("Inactive"),
        "names the status: {err:?}"
    );
    finish(handle);
}

#[test]
fn pending_cancel_is_not_terminal_cancel_success() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // placeOrder
        server_write_frame(
            &mut stream,
            &[
                "3",
                "5001",
                "PreSubmitted",
                "0",
                "1",
                "0.0",
                "1",
                "0",
                "0.0",
                "101",
                "",
                "0.0",
            ],
        );
        let _ = server_read_frame(&mut stream); // cancelOrder
                                                // PendingCancel first — the broker may still reject the cancel, so the
                                                // client must keep waiting for the terminal Cancelled.
        server_write_frame(
            &mut stream,
            &[
                "3",
                "5001",
                "PendingCancel",
                "0",
                "1",
                "0.0",
                "1",
                "0",
                "0.0",
                "101",
                "",
                "0.0",
            ],
        );
        server_write_frame(
            &mut stream,
            &[
                "3",
                "5001",
                "Cancelled",
                "0",
                "1",
                "0.0",
                "1",
                "0",
                "0.0",
                "101",
                "",
                "0.0",
            ],
        );
    });
    let receipt = gateway
        .submit_order(&market_order("AAPL", 1))
        .expect("accepted");
    gateway
        .cancel_order(&receipt.broker_order_id)
        .expect("terminal Cancelled after PendingCancel is the success signal");
    finish(handle);
}

#[test]
fn transport_fault_drops_the_cached_session_and_reconnects() {
    // Two-connection listener: the first session dies mid-operation (transport
    // fault -> the gateway must drop it); the next call handshakes anew.
    let listener = TcpListener::bind("127.0.0.1:0").expect("ephemeral loopback listener");
    let port = listener.local_addr().unwrap().port();
    let (sender, captured) = channel();
    let handle = std::thread::spawn(move || {
        // Connection 1: serve one positions cycle, then die mid-request.
        let (mut stream, _) = listener.accept().expect("first connection");
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // reqPositions
        server_write_frame(&mut stream, &["62", "1"]);
        let _ = server_read_frame(&mut stream); // cancelPositions
        let _ = server_read_frame(&mut stream); // next reqPositions...
        drop(stream); // ...answered by closing the socket mid-operation

        // Connection 2: the client must reconnect with a FULL new handshake.
        let (mut stream, _) = listener.accept().expect("reconnection");
        server_handshake(&mut stream, &sender, 6001);
        let _ = server_read_frame(&mut stream); // reqPositions
        server_write_frame(&mut stream, &["62", "1"]);
        let _ = server_read_frame(&mut stream); // cancelPositions
    });
    let config = IbConnectionConfig::new("127.0.0.1", port, port, 101);
    let gateway = TcpIbGateway::with_op_deadline(config, IbAccountKind::Paper, NORMAL_DEADLINE);

    assert_eq!(gateway.positions().expect("first op").records, 0);
    let err = gateway
        .positions()
        .expect_err("a mid-operation disconnect must surface, not hang or fabricate");
    assert_eq!(err.code, IB_CODE_NOT_CONNECTED);
    assert_eq!(
        gateway.positions().expect("reconnected session").records,
        0,
        "the dropped session must be re-established on the next call"
    );
    finish(handle);
    let handshakes = captured
        .iter()
        .filter(|f: &Vec<String>| f.first().map(String::as_str) == Some("71"))
        .count();
    assert_eq!(handshakes, 2, "one handshake per real connection");
}

#[test]
fn impossible_civil_dates_fail_closed_before_any_send() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        // No further frames: every request below is refused before any send.
    });
    for (start, end) in [
        ("2026-02-31", "2026-03-01"), // February 31st does not exist
        ("2026-01-01", "2025-02-29"), // 2025 is not a leap year
        ("2026-04-31", "2026-05-01"), // April has 30 days
        ("2026-00-10", "2026-01-10"), // month 0
        ("2026-1-1", "2026-01-10"),   // not zero-padded YYYY-MM-DD
    ] {
        let mut request = daily("AAPL");
        request.start = start.to_string();
        request.end = end.to_string();
        let err = gateway
            .historical_data(&request)
            .expect_err("an impossible civil date must fail closed, never reach the broker");
        assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST, "{start}..{end}");
    }
    // Leap-day sanity: 2024-02-29 IS a valid civil date (only the range needs
    // the fake gateway, so just prove the date passes validation by reaching
    // the empty-range check instead of date rejection).
    let mut request = daily("AAPL");
    request.start = "2024-02-29".to_string();
    request.end = "2024-02-28".to_string(); // valid dates, empty range
    let err = gateway
        .historical_data(&request)
        .expect_err("empty range fails closed");
    assert!(
        err.message.contains("empty"),
        "leap day must pass date validation and fail on the RANGE: {err:?}"
    );
    finish(handle);
}

#[test]
fn transport_seam_enforces_srs_exe_003_validation_directly() {
    // The transport is public: calling it directly (bypassing the canonical
    // adapter) must hit the SAME shared fail-closed validation wall.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        // No further frames: every submission below is refused before any send.
    });

    let mut blank_symbol = market_order("  ", 1);
    blank_symbol.symbol = "  ".to_string();
    let err = gateway
        .submit_order(&blank_symbol)
        .expect_err("blank symbol must fail validation at the wire");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let err = gateway
        .submit_order(&market_order("AAPL", 0))
        .expect_err("non-positive quantity must fail validation at the wire");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    let mut bad_price = market_order("AAPL", 1);
    bad_price.order_type = OrderType::Limit {
        limit_price_minor: 0,
    };
    let err = gateway
        .submit_order(&bad_price)
        .expect_err("non-positive limit price must fail validation at the wire");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);

    finish(handle);
}

#[test]
fn historical_refuses_normalizations_ib_cannot_serve_honestly() {
    // IB daily TRADES bars are split-adjusted; any other requested mode would
    // be mislabeled data, so the wire refuses it before any send.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
    });
    for mode in [
        NormalizationMode::Raw,
        NormalizationMode::FullyAdjusted,
        NormalizationMode::TotalReturn,
    ] {
        let mut request = daily("AAPL");
        request.normalization_mode = mode;
        let err = gateway
            .historical_data(&request)
            .expect_err("non-split-adjusted normalization must fail closed");
        assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST, "{mode:?}");
        assert!(
            err.message.contains("split-adjusted"),
            "names the honest capability: {err:?}"
        );
    }
    finish(handle);
}

#[test]
fn live_account_transport_fails_closed_pending_execution_engine() {
    // No server at all: the SRS-EXE-001 gate must refuse a LIVE-account session
    // BEFORE any socket is opened, so the adapter alone can never place a
    // real live-account order.
    let config = IbConnectionConfig::new("127.0.0.1", 1, 1, 101);
    let gateway =
        TcpIbGateway::with_op_deadline(config, IbAccountKind::Live, Duration::from_millis(200));
    let err = gateway
        .submit_order(&market_order("AAPL", 1))
        .expect_err("a live-account submit must fail closed pending SRS-EXE-001");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);
    assert!(
        err.message.contains("SRS-EXE-001"),
        "names the gating owner: {err:?}"
    );
    let err = gateway
        .positions()
        .expect_err("every live-account operation is gated, not just orders");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);
}

#[test]
fn open_order_echo_is_not_acceptance_rejection_still_surfaces() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // placeOrder
                                                // IB optimistically echoes openOrder(5) BEFORE the rejection lands on
                                                // the error channel — the echo must not be reported as acceptance.
        server_write_frame(&mut stream, &["5", "5001", "AAPL", "STK"]);
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "5001",
                "201",
                "Order rejected - reason:Insufficient buying power",
                "",
            ],
        );
    });
    let err = gateway
        .submit_order(&market_order("AAPL", 1))
        .expect_err("the rejection behind the openOrder echo must surface");
    assert_eq!(err.code, 201);
    finish(handle);
}

#[test]
fn live_account_composite_is_gated_before_any_socket() {
    // The composite path (SRS-EXE-004's future wire) must sit behind the SAME
    // SRS-EXE-001 live-account gate as every other operation — checked before
    // connect, so no server is needed here either.
    let config = IbConnectionConfig::new("127.0.0.1", 1, 1, 101);
    let gateway =
        TcpIbGateway::with_op_deadline(config, IbAccountKind::Live, Duration::from_millis(200));
    let composite = atp_types::CompositeOrderSubmission::new(StrategyId::new("live-1"), Vec::new());
    let err = gateway
        .submit_composite_order(&composite)
        .expect_err("a live-account composite must fail closed pending SRS-EXE-001");
    assert_eq!(err.code, IB_CODE_UNSUPPORTED_REQUEST);
}

#[test]
fn subscribe_fails_closed_on_competing_live_session_no_data() {
    // IB error 10197 "No market data during competing live session" for our
    // ticker means IB is WITHHOLDING the data stream — the subscription is
    // inert, so subscribe must FAIL (never report no-data as success). The
    // operator remedy is to free the competing live session.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // reqMarketDataType
        let _ = server_read_frame(&mut stream); // reqMktData
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "9000",
                "10197",
                "No market data during competing live session",
                "",
            ],
        );
    });
    let err = gateway
        .subscribe_market_data(&quotes("AAPL"))
        .expect_err("10197 (data withheld) must fail closed, not report success");
    assert_eq!(err.code, 10197);
    finish(handle);
}

#[test]
fn subscribe_still_fails_on_a_real_rejection() {
    // A genuine subscribe rejection (354 "requested market data is not
    // subscribed") for our ticker must still surface — 10197 leniency must not
    // swallow real errors.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender, 5001);
        let _ = server_read_frame(&mut stream); // reqMarketDataType
        let _ = server_read_frame(&mut stream); // reqMktData
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "9000",
                "354",
                "Requested market data is not subscribed.",
                "",
            ],
        );
    });
    let err = gateway
        .subscribe_market_data(&quotes("AAPL"))
        .expect_err("a real subscribe rejection must surface");
    assert_eq!(err.code, 354);
    finish(handle);
}

#[test]
fn control_bytes_in_symbols_fail_closed_before_any_send() {
    // A strategy-supplied symbol carrying an embedded NUL/newline must never be
    // encoded into a NUL-delimited TWS frame (it could shift fields) — every
    // op fails closed with IB_CODE_UNSUPPORTED_REQUEST before the poisoned
    // frame reaches the socket. Each op runs against its own gateway that is
    // explicitly dropped before `finish` (so `server_drain` sees the close and
    // the join does not block).
    fn handshake_then_drain(mut s: TcpStream, sender: Sender<Vec<String>>) {
        server_handshake(&mut s, &sender, 5001);
        server_drain(&mut s);
    }
    for bad_symbol in ["AA\0PL", "AAPL\n201", "A\tB"] {
        let (gateway, _c, handle) = scripted_gateway(NORMAL_DEADLINE, handshake_then_drain);
        let err = gateway
            .submit_order(&market_order(bad_symbol, 1))
            .expect_err("order with a control-char symbol must fail closed");
        assert_eq!(
            err.code, IB_CODE_UNSUPPORTED_REQUEST,
            "order {bad_symbol:?}"
        );
        drop(gateway);
        finish(handle);

        let (gateway, _c, handle) = scripted_gateway(NORMAL_DEADLINE, handshake_then_drain);
        let err = gateway
            .subscribe_market_data(&quotes(bad_symbol))
            .expect_err("subscribe with a control-char symbol must fail closed");
        assert_eq!(
            err.code, IB_CODE_UNSUPPORTED_REQUEST,
            "subscribe {bad_symbol:?}"
        );
        drop(gateway);
        finish(handle);

        let (gateway, _c, handle) = scripted_gateway(NORMAL_DEADLINE, handshake_then_drain);
        let mut request = daily(bad_symbol);
        request.symbol = bad_symbol.to_string();
        let err = gateway
            .historical_data(&request)
            .expect_err("historical with a control-char symbol must fail closed");
        assert_eq!(
            err.code, IB_CODE_UNSUPPORTED_REQUEST,
            "historical {bad_symbol:?}"
        );
        drop(gateway);
        finish(handle);
    }
}

#[test]
fn connectivity_loss_errors_drop_the_session_and_reconnect() {
    // IB 1100 "connectivity lost" and 2110 "connectivity broken" both classify
    // CONNECTIVITY_BLOCKED — the socket is dead, so the op must FAIL and the
    // cached session must be dropped so the next call performs a fresh
    // handshake. 2110 sits inside the 2100–2169 farm-status range, so this also
    // guards that a real connectivity fault is NOT masked as a benign notice.
    for (code, message) in [
        (
            1100,
            "Connectivity between IB and Trader Workstation has been lost.",
        ),
        (
            2110,
            "Connectivity between TWS and server is broken. It will be restored automatically.",
        ),
    ] {
        let listener = TcpListener::bind("127.0.0.1:0").expect("ephemeral loopback listener");
        let port = listener.local_addr().unwrap().port();
        let (sender, captured) = channel();
        let code_str = code.to_string();
        let handle = std::thread::spawn(move || {
            // Connection 1: handshake, then answer reqPositions with the fault.
            let (mut stream, _) = listener.accept().expect("first connection");
            server_handshake(&mut stream, &sender, 5001);
            let _ = server_read_frame(&mut stream); // reqPositions
            server_write_frame(&mut stream, &["4", "2", "-1", &code_str, message, ""]);
            server_drain(&mut stream);

            // Connection 2: the client must reconnect with a FULL new handshake.
            let (mut stream, _) = listener.accept().expect("reconnection");
            server_handshake(&mut stream, &sender, 6001);
            let _ = server_read_frame(&mut stream); // reqPositions
            server_write_frame(&mut stream, &["62", "1"]); // positionEnd
            let _ = server_read_frame(&mut stream); // cancelPositions
        });
        let config = IbConnectionConfig::new("127.0.0.1", port, port, 101);
        let gateway = TcpIbGateway::with_op_deadline(config, IbAccountKind::Paper, NORMAL_DEADLINE);

        let err = gateway
            .positions()
            .expect_err("a connectivity fault must surface, not be skipped");
        assert_eq!(err.code, code, "code {code} must fail the op");
        assert_eq!(
            classify_ib_order_error(&err),
            Some(OrderErrorCategory::ConnectivityBlocked)
        );
        assert_eq!(
            gateway
                .positions()
                .expect("session reconnected after connectivity fault")
                .records,
            0,
            "the dead session must be dropped and re-established (code {code})"
        );
        finish(handle);
        let handshakes = captured
            .iter()
            .filter(|f: &Vec<String>| f.first().map(String::as_str) == Some("71"))
            .count();
        assert_eq!(
            handshakes, 2,
            "connectivity fault {code} must force a fresh handshake"
        );
    }
}
