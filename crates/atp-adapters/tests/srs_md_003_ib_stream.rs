//! SRS-MD-003 — the INBOUND streaming surface, over a scripted fake IB Gateway.
//!
//! `srs_exe_006_ib_wire.rs` covers the request/reply operations. This file covers
//! the two additions SRS-MD-003 needs, which behave differently from every op
//! there: a heartbeat round trip whose REPLY is the only acceptable evidence, and
//! a tick drain whose empty result is a normal outcome rather than a failure.
//!
//! It carries its own copy of the scripted-gateway harness on purpose:
//! `srs_exe_006_ib_wire.rs` is one of the three files hashed into
//! `architecture/ib_paper_account_evidence.json` by `tools/ib_adapter_check.py`,
//! so appending tests there would invalidate the recorded live paper-account
//! evidence for reasons that have nothing to do with the wire it proves.
//!
//! Gated to the operator's live transport feature (`ib-live-transport`) exactly
//! like the surface it exercises; the fake gateway is an ephemeral loopback
//! socket, so this still runs solo in the parallel agent pool (no IB ports).

#![cfg(feature = "ib-live-transport")]

use atp_adapters::interactive_brokers::{IbAccountKind, IbConnectionConfig};
use atp_adapters::{IbGatewayConnection, TcpIbGateway};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::thread::JoinHandle;
use std::time::Duration;

// --------------------------------------------------------------------------- //
// Scripted gateway harness (mirrors srs_exe_006_ib_wire.rs)
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

fn server_read_raw_handshake(stream: &mut TcpStream) -> String {
    let mut header = [0u8; 4];
    stream.read_exact(&mut header).expect("handshake header");
    let length = u32::from_be_bytes(header) as usize;
    let mut payload = vec![0u8; length];
    stream.read_exact(&mut payload).expect("handshake payload");
    assert!(
        !payload.contains(&0),
        "the version-range handshake must be RAW (no NUL terminator)"
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

fn server_handshake(stream: &mut TcpStream, captured: &Sender<Vec<String>>) {
    let mut prefix = [0u8; 4];
    stream.read_exact(&mut prefix).expect("API prefix");
    assert_eq!(&prefix, b"API\0");
    captured
        .send(vec![server_read_raw_handshake(stream)])
        .unwrap();
    server_write_frame(stream, &["176", "20260731 22:00:00 UTC"]);
    captured.send(server_read_frame(stream)).unwrap(); // startApi
    server_write_frame(stream, &["15", "1", "DU1234567"]);
    server_write_frame(stream, &["9", "1", "7001"]);
}

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
/// Tick-drain budget for the tests. Comfortably above the 250 ms socket read
/// tick so a poll always gets at least one wake, and short enough to keep the
/// suite fast.
const POLL_BUDGET: Duration = Duration::from_millis(600);
/// How long a scripted gateway holds the socket open after its last write.
/// It MUST outlast `POLL_BUDGET`: closing early would hand the client an EOF
/// (a genuine transport fault) instead of the budget expiry under test.
const HOLD_OPEN: Duration = Duration::from_millis(1_200);

// --------------------------------------------------------------------------- //
// Broker heartbeat — reqCurrentTime(49) -> currentTime(49)
// --------------------------------------------------------------------------- //

#[test]
fn broker_heartbeat_round_trip_returns_the_gateway_clock() {
    let (gateway, captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        sender.send(server_read_frame(&mut stream)).unwrap(); // reqCurrentTime
        server_write_frame(&mut stream, &["49", "1", "1785708000"]);
    });

    let observed = gateway
        .broker_heartbeat_round_trip()
        .expect("currentTime round trip over the handshaken session");
    assert_eq!(observed, 1_785_708_000);

    let _handshake = captured.recv().unwrap();
    let _start_api = captured.recv().unwrap();
    let request = captured.recv().unwrap();
    assert_eq!(
        request.iter().map(String::as_str).collect::<Vec<&str>>(),
        vec!["49", "1"],
        "reqCurrentTime frame drifted from the ibapi-10.19.4 golden"
    );
    finish(handle);
}

#[test]
fn broker_heartbeat_fails_closed_on_an_unparseable_time() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        let _ = server_read_frame(&mut stream);
        // A currentTime frame whose time field is not a number.
        server_write_frame(&mut stream, &["49", "1", "not-a-time"]);
    });

    let error = gateway
        .broker_heartbeat_round_trip()
        .expect_err("a malformed currentTime is not a heartbeat");
    assert!(
        error.message.contains("currentTime"),
        "the failure must name the frame it could not read: {}",
        error.message
    );
    finish(handle);
}

#[test]
fn broker_heartbeat_skips_notices_before_the_reply() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        let _ = server_read_frame(&mut stream);
        // 2104 "market data farm connection is OK" — an informational notice
        // the gateway volunteers constantly. It must not derail the round trip.
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "-1",
                "2104",
                "Market data farm connection is OK",
                "",
            ],
        );
        server_write_frame(&mut stream, &["49", "1", "1785708123"]);
    });

    assert_eq!(
        gateway.broker_heartbeat_round_trip().expect("round trip"),
        1_785_708_123
    );
    finish(handle);
}

#[test]
fn ticks_arriving_during_a_heartbeat_survive_to_the_next_poll() {
    // One session carries both operations, so a tick can land while the
    // heartbeat is waiting for `currentTime`. Dropping it there would destroy
    // freshness evidence for a line that IS flowing, and the monitor would age
    // that line toward a staleness alarm the market never justified.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        let _ = server_read_frame(&mut stream); // reqCurrentTime
                                                // Two ticks jump the queue ahead of the heartbeat reply.
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
        server_write_frame(&mut stream, &["2", "6", "9000", "5", "300"]);
        server_write_frame(&mut stream, &["49", "1", "1785708000"]);
        std::thread::sleep(HOLD_OPEN);
    });

    assert_eq!(
        gateway.broker_heartbeat_round_trip().expect("round trip"),
        1_785_708_000
    );
    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("drain after the heartbeat");
    assert_eq!(
        ticks.len(),
        2,
        "ticks delivered during the heartbeat must still reach the feed loop"
    );
    assert!(ticks.iter().all(|tick| tick.ticker_id == 9000));
    finish(handle);
}

// --------------------------------------------------------------------------- //
// Tick drain — poll_market_data
// --------------------------------------------------------------------------- //

#[test]
fn poll_decodes_delivered_price_and_size_ticks() {
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        // tickPrice(1): [id, version, tickerId, tickType, price, size, attrs]
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
        // tickSize(2): [id, version, tickerId, tickType, size]
        server_write_frame(&mut stream, &["2", "6", "9000", "5", "300"]);
        std::thread::sleep(HOLD_OPEN);
    });

    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("a quiet-then-idle drain is not a failure");
    assert_eq!(
        ticks.len(),
        2,
        "both delivered ticks are freshness evidence"
    );
    assert!(ticks.iter().all(|tick| tick.ticker_id == 9000));
    assert_eq!(ticks[0].tick_type, 4);
    assert_eq!(ticks[1].tick_type, 5);
    finish(handle);
}

#[test]
fn poll_returns_empty_when_the_line_is_silent() {
    // The load-bearing case for SRS-MD-003: a line that delivers nothing is
    // exactly what staleness detection exists to notice, so the transport must
    // report it as an ordinary empty poll. An Err here would make the feed loop
    // treat silence as a fault and never reach its own 15 s verdict.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        std::thread::sleep(HOLD_OPEN);
    });

    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("silence is not an error");
    assert!(ticks.is_empty());
    finish(handle);
}

#[test]
fn poll_fails_closed_on_an_error_naming_our_ticker() {
    // 10197 — "no market data during competing live session". IB is WITHHOLDING
    // the stream, so the line is inert. Swallowing this would leave the feed
    // silently starved with no fault surfaced anywhere.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
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
        std::thread::sleep(HOLD_OPEN);
    });

    let error = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect_err("a withheld stream on our ticker is a fault");
    assert_eq!(error.code, 10197);
    finish(handle);
}

#[test]
fn poll_ignores_an_error_addressed_to_another_request() {
    // Someone else's failing request is not ours to fail on — the shared
    // session carries traffic for other ticker ids.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        server_write_frame(
            &mut stream,
            &[
                "4",
                "2",
                "4242",
                "200",
                "No security definition has been found",
                "",
            ],
        );
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
        std::thread::sleep(HOLD_OPEN);
    });

    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("another request's error must not fail our drain");
    assert_eq!(ticks.len(), 1);
    assert_eq!(ticks[0].ticker_id, 9000);
    finish(handle);
}

#[test]
fn the_session_generation_advances_on_every_reconnect() {
    // The signal a subscription holder needs. `reqMktData` state lives in the
    // session that opened it, so a transparently-rebuilt session leaves a
    // subscriber polling dead ticker ids — permanently, and looking exactly
    // like a market that went quiet.
    let listener = TcpListener::bind("127.0.0.1:0").expect("ephemeral loopback listener");
    let port = listener.local_addr().unwrap().port();
    let handle = std::thread::spawn(move || {
        for _ in 0..2 {
            let (mut stream, _) = listener.accept().expect("client connection");
            let (sender, _receiver) = channel();
            server_handshake(&mut stream, &sender);
            let _ = server_read_frame(&mut stream); // reqCurrentTime
                                                    // Close without answering: a transport fault, which drops the
                                                    // cached session so the next call rebuilds one.
            drop(stream);
        }
    });
    let config = IbConnectionConfig::new("127.0.0.1", port, port, 101);
    let gateway =
        TcpIbGateway::with_op_deadline(config, IbAccountKind::Paper, Duration::from_millis(700));

    assert_eq!(gateway.session_generation(), 0, "no session opened yet");
    let _ = gateway.broker_heartbeat_round_trip();
    let first = gateway.session_generation();
    assert_eq!(first, 1, "the first call established a session");
    let _ = gateway.broker_heartbeat_round_trip();
    assert!(
        gateway.session_generation() > first,
        "a rebuilt session must be observable — otherwise a subscription holder \
         cannot know its ticker ids died with the old one"
    );
    handle.join().expect("fake gateway thread panicked");
}

#[test]
fn a_frame_split_across_the_budget_fails_closed_instead_of_desyncing() {
    // Bytes read off the socket are gone. If the budget expires partway through
    // a frame, the remainder sits at the head of the stream and the NEXT read
    // would parse it as a fresh frame — every later tick on this session would
    // be garbage. A short poll budget makes that reachable, so it must surface
    // as a transport fault (which drops the session) rather than as a quiet poll.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        // A header promising a payload that never arrives.
        stream
            .write_all(&64u32.to_be_bytes())
            .expect("header write");
        stream.write_all(b"partial").expect("partial payload");
        std::thread::sleep(HOLD_OPEN);
    });

    let error = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect_err("a mid-frame budget expiry is not a quiet line");
    assert!(
        error.message.contains("midway through an inbound frame"),
        "the failure must name the desync it is preventing: {}",
        error.message
    );
    finish(handle);
}

#[test]
fn the_tick_that_confirms_a_subscription_is_not_lost() {
    // IB may answer `reqMktData` with a tick rather than a tickReqParams frame.
    // That frame is both the confirmation AND a real delivery; dropping it lets
    // an illiquid line read never-observed while data has in fact arrived.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        let _ = server_read_frame(&mut stream); // reqMarketDataType
        let _ = server_read_frame(&mut stream); // reqMktData
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
        std::thread::sleep(HOLD_OPEN);
    });

    let receipt = gateway
        .subscribe_market_data(&atp_adapters::MarketDataSubscription {
            symbol: "AAPL".to_string(),
            channel: atp_adapters::MarketDataChannel::Trades,
        })
        .expect("subscription confirmed by a tick");
    assert_eq!(receipt.subscription_id, "ib-md-9000");

    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("first drain after subscribing");
    assert_eq!(
        ticks.len(),
        1,
        "the confirming tick is freshness evidence and must reach the feed loop"
    );
    assert_eq!(ticks[0].ticker_id, 9000);
    finish(handle);
}

#[test]
fn poll_drops_an_undecodable_tick_rather_than_guessing_its_line() {
    // A tick whose ticker id will not parse cannot name a security. Attributing
    // it to the subscribed line would prove that line fresh on the strength of
    // a frame that may not belong to it at all.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        server_write_frame(
            &mut stream,
            &["1", "6", "not-an-id", "4", "187.25", "3", "1"],
        );
        std::thread::sleep(HOLD_OPEN);
    });

    let ticks = gateway
        .poll_market_data(&[9000], POLL_BUDGET)
        .expect("an undecodable frame is skipped, not fatal");
    assert!(
        ticks.is_empty(),
        "an unattributable frame is not freshness evidence for any line"
    );
    finish(handle);
}

#[test]
fn ticks_for_another_line_survive_a_second_subscribe() {
    // The same shared-socket hazard as the heartbeat case, on the subscribe
    // path. Opening symbol #2 waits for ITS confirmation while symbol #1 is
    // already flowing; ticks for #1 arrive on that same socket meanwhile.
    // Dropping them would age a line that is actively delivering into a false
    // staleness alarm — on every multi-symbol connect, and again on every
    // resubscribe after a reconnect.
    let (gateway, _captured, handle) = scripted_gateway(NORMAL_DEADLINE, |mut stream, sender| {
        server_handshake(&mut stream, &sender);
        let _ = server_read_frame(&mut stream); // reqMarketDataType
        let _ = server_read_frame(&mut stream); // reqMktData -> 9000
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.25", "3", "1"]);
        let _ = server_read_frame(&mut stream); // reqMktData -> 9001
        // The FIRST line keeps delivering while the second waits to confirm.
        server_write_frame(&mut stream, &["1", "6", "9000", "4", "187.30", "3", "1"]);
        server_write_frame(&mut stream, &["2", "6", "9000", "5", "400"]);
        // Only now does the second subscription confirm.
        server_write_frame(&mut stream, &["1", "6", "9001", "4", "410.10", "3", "1"]);
        std::thread::sleep(HOLD_OPEN);
    });

    let first = gateway
        .subscribe_market_data(&atp_adapters::MarketDataSubscription {
            symbol: "AAPL".to_string(),
            channel: atp_adapters::MarketDataChannel::Trades,
        })
        .expect("first subscription confirmed");
    assert_eq!(first.subscription_id, "ib-md-9000");

    let second = gateway
        .subscribe_market_data(&atp_adapters::MarketDataSubscription {
            symbol: "MSFT".to_string(),
            channel: atp_adapters::MarketDataChannel::Trades,
        })
        .expect("second subscription confirmed");
    assert_eq!(second.subscription_id, "ib-md-9001");

    let ticks = gateway
        .poll_market_data(&[9000, 9001], POLL_BUDGET)
        .expect("drain after both subscriptions");
    let first_line = ticks.iter().filter(|t| t.ticker_id == 9000).count();
    assert_eq!(
        first_line, 3,
        "every tick delivered for the already-subscribed line must reach the \
         feed loop: 1 confirming + 2 arriving during the second subscribe"
    );
    assert_eq!(
        ticks.iter().filter(|t| t.ticker_id == 9001).count(),
        1,
        "the tick that confirmed the second line is evidence too"
    );
    finish(handle);
}
