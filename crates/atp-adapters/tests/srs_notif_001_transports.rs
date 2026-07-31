//! L4 boundary — SRS-NOTIF-001 IF-10 / IF-11 transports over REAL sockets.
//!
//! Each test stands up a scripted server on a loopback ephemeral port and drives
//! the adapter through [`NotificationChannelClient::send`], so the SMTP and HTTP
//! conversations are exercised as wire protocols rather than as mocked calls.
//!
//! The load-bearing test here is
//! [`the_send_deadline_bounds_the_whole_conversation_not_each_leg`]: it is the
//! only one that can tell a correct implementation from the plausible-looking
//! wrong one, where each socket operation is armed with the full deadline and a
//! slow relay is therefore granted the deadline once *per round trip*. That
//! version passes every other test in this file.

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use atp_adapters::notification::{
    SmsGatewayChannel, SmsGatewayConfig, SmtpEmailChannel, SmtpRelayConfig,
};
use atp_notification::{
    ChannelError, NotificationChannel, NotificationChannelClient, NotificationMessage,
};

const SENDER: &str = "atp@example.com";
const RECIPIENT: &str = "operator@example.com";
const OPERATOR_SMS: &str = "+15550001111";
const KEY: &str = "relay-secret";

/// The standard reply script for a relay that accepts the message.
fn accepting_script() -> Vec<String> {
    vec![
        "220 relay.internal ESMTP ready".into(),
        // Multiline capability list — the shape that trips a parser which reads
        // only the first line of a reply.
        "250-relay.internal\r\n250-SIZE 10240000\r\n250 AUTH PLAIN LOGIN".into(),
        "235 2.7.0 Authentication successful".into(),
        "250 2.1.0 Sender ok".into(),
        "250 2.1.5 Recipient ok".into(),
        "354 End data with <CR><LF>.<CR><LF>".into(),
        "250 2.0.0 Ok: queued as QUEUE-ID-7788".into(),
    ]
}

struct ScriptedSmtp {
    port: u16,
    handle: JoinHandle<Vec<String>>,
}

/// Serve one lock-step SMTP conversation, replying with `script` in order and
/// sleeping `delay` before each reply. Returns every line the client sent.
fn spawn_smtp(script: Vec<String>, delay: Duration) -> ScriptedSmtp {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local addr").port();

    let handle = thread::spawn(move || {
        let mut received = Vec::new();
        let Ok((stream, _)) = listener.accept() else {
            return received;
        };
        let mut reader = BufReader::new(stream);
        let mut script = script.into_iter();

        // Greeting.
        if !reply(&mut reader, script.next(), delay) {
            return received;
        }

        // EHLO, AUTH, MAIL FROM, RCPT TO, DATA — one line in, one reply out.
        for _ in 0..5 {
            match read_line(&mut reader) {
                Some(line) => received.push(line),
                None => return received,
            }
            if !reply(&mut reader, script.next(), delay) {
                return received;
            }
        }

        // Message payload, terminated by a lone dot.
        loop {
            match read_line(&mut reader) {
                Some(line) if line == "." => break,
                Some(line) => received.push(line),
                None => return received,
            }
        }
        if !reply(&mut reader, script.next(), delay) {
            return received;
        }

        // QUIT (best effort).
        if let Some(line) = read_line(&mut reader) {
            received.push(line);
        }
        received
    });

    ScriptedSmtp { port, handle }
}

fn read_line(reader: &mut BufReader<TcpStream>) -> Option<String> {
    let mut line = String::new();
    match reader.read_line(&mut line) {
        Ok(0) | Err(_) => None,
        Ok(_) => Some(line.trim_end_matches(['\r', '\n']).to_string()),
    }
}

/// Send one scripted reply. `false` means the script is exhausted or the client
/// hung up, and the server should stop.
fn reply(reader: &mut BufReader<TcpStream>, line: Option<String>, delay: Duration) -> bool {
    let Some(line) = line else { return false };
    if !delay.is_zero() {
        thread::sleep(delay);
    }
    let mut socket: &TcpStream = reader.get_ref();
    socket.write_all(format!("{line}\r\n").as_bytes()).is_ok() && socket.flush().is_ok()
}

fn email_channel(port: u16) -> SmtpEmailChannel {
    SmtpEmailChannel::new(
        SmtpRelayConfig::new("127.0.0.1", port, SENDER, RECIPIENT, SENDER, KEY)
            .expect("config is valid"),
    )
}

fn alert() -> NotificationMessage {
    NotificationMessage::new(
        "CRITICAL: IB connectivity lost",
        "IB Gateway unreachable on 127.0.0.1:4002",
    )
}

#[test]
fn an_accepted_email_returns_the_relays_queue_id_as_the_receipt() {
    let server = spawn_smtp(accepting_script(), Duration::ZERO);
    let receipt = email_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("relay accepted the message");

    assert!(
        receipt.reference().contains("QUEUE-ID-7788"),
        "receipt should carry the relay's queue id, got {:?}",
        receipt.reference()
    );

    let received = server.handle.join().expect("server thread");
    let conversation = received.join("\n");
    assert!(conversation.contains("EHLO"), "{conversation}");
    assert!(
        conversation.contains(&format!("MAIL FROM:<{SENDER}>")),
        "{conversation}"
    );
    assert!(
        conversation.contains(&format!("RCPT TO:<{RECIPIENT}>")),
        "{conversation}"
    );
    assert!(
        conversation.contains("Subject: CRITICAL: IB connectivity lost"),
        "{conversation}"
    );
    assert!(
        conversation.contains("IB Gateway unreachable on 127.0.0.1:4002"),
        "{conversation}"
    );
}

#[test]
fn the_relay_credential_is_presented_as_base64_auth_plain() {
    let server = spawn_smtp(accepting_script(), Duration::ZERO);
    email_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("relay accepted the message");

    let received = server.handle.join().expect("server thread");
    let auth = received
        .iter()
        .find(|line| line.starts_with("AUTH PLAIN "))
        .expect("the adapter authenticated");

    // base64("\0atp@example.com\0relay-secret") — the credential must be encoded,
    // and must NOT appear in the clear anywhere in the conversation.
    assert_eq!(auth, "AUTH PLAIN AGF0cEBleGFtcGxlLmNvbQByZWxheS1zZWNyZXQ=");
    assert!(
        !received.join("\n").contains(KEY),
        "the credential must never appear in the clear"
    );
}

#[test]
fn a_relay_that_does_not_advertise_auth_is_refused_as_an_open_relay() {
    let mut script = accepting_script();
    script[1] = "250-relay.internal\r\n250 SIZE 10240000".into();
    let server = spawn_smtp(script, Duration::ZERO);

    let result = email_channel(server.port).send(&alert(), Duration::from_secs(5));
    match result {
        Err(ChannelError::Unconfigured { detail }) => {
            assert!(detail.contains("AUTH"), "detail: {detail}");
        }
        other => panic!("expected Unconfigured, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// A refused credential must not put that credential into the error the
/// dispatcher stores.
///
/// The stored `ChannelDelivery` detail is operator-facing and durable, so a
/// failure detail that echoed the command line would write `ATP_SMTP_API_KEY`
/// (base64, but recoverable) into the audit log on every auth failure — the
/// NFR-S4 leak the core avoids by never holding the key at all.
#[test]
fn a_refused_credential_never_appears_in_the_error_detail() {
    let mut script = accepting_script();
    script[2] = "535 5.7.8 Authentication credentials invalid".into();
    let server = spawn_smtp(script, Duration::ZERO);

    match email_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(detail.contains("535"), "detail: {detail}");
            assert!(!detail.contains(KEY), "credential leaked into: {detail}");
            // The base64 form must not leak either.
            assert!(
                !detail.contains("AGF0cEBleGFtcGxlLmNvbQByZWxheS1zZWNyZXQ="),
                "encoded credential leaked into: {detail}"
            );
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn a_transient_4xx_is_reported_as_a_retryable_transport_failure() {
    let mut script = accepting_script();
    script[3] = "451 4.3.0 Temporary local problem".into();
    let server = spawn_smtp(script, Duration::ZERO);

    match email_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(detail.contains("451"), "detail: {detail}");
        }
        other => panic!("expected TransportUnavailable, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn a_permanent_5xx_is_reported_as_a_rejection_a_retry_will_not_fix() {
    let mut script = accepting_script();
    script[4] = "550 5.1.1 No such recipient".into();
    let server = spawn_smtp(script, Duration::ZERO);

    match email_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(detail.contains("550"), "detail: {detail}");
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn an_unreachable_relay_is_a_transport_failure_not_a_rejection() {
    // Bind and immediately drop, so the port is almost certainly closed.
    let port = {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        listener.local_addr().expect("local addr").port()
    };

    match email_channel(port).send(&alert(), Duration::from_secs(2)) {
        Err(ChannelError::TransportUnavailable { .. }) => {}
        other => panic!("expected TransportUnavailable, got {other:?}"),
    }
}

/// The discriminating test for the deadline contract.
///
/// The relay answers every step, but takes `STEP` to do it. Seven round trips at
/// `STEP` each is `7 * STEP` of wall clock — comfortably more than the deadline,
/// but each *individual* operation finishes well inside it. An adapter that arms
/// the full deadline per operation therefore sails through and reports success;
/// only an adapter that treats the deadline as one budget for the whole
/// conversation reports `Timeout`.
///
/// This is what makes `MAX_CHANNEL_DEADLINE = DISPATCH_SLA_MS / 2` mean anything:
/// without it, two channels could between them spend many multiples of the
/// NFR-P6 60,000 ms SLA while every socket timeout looked correctly armed.
#[test]
fn the_send_deadline_bounds_the_whole_conversation_not_each_leg() {
    const STEP: Duration = Duration::from_millis(120);
    const DEADLINE: Duration = Duration::from_millis(360);

    let server = spawn_smtp(accepting_script(), STEP);
    let started = Instant::now();
    let result = email_channel(server.port).send(&alert(), DEADLINE);
    let elapsed = started.elapsed();

    match result {
        Err(ChannelError::Timeout { .. }) => {}
        other => panic!(
            "expected Timeout after {elapsed:?}; a per-operation timeout would have \
             let this succeed. Got {other:?}"
        ),
    }

    // 7 replies x 120ms = 840ms if each leg got its own full deadline. Landing
    // under 700ms proves the budget is shared, with enough headroom that a loaded
    // CI host does not turn a correct implementation red.
    assert!(
        elapsed < Duration::from_millis(700),
        "send took {elapsed:?} — the deadline is being armed per operation, not per send"
    );
    let _ = server.handle.join();
}

/// A relay that is never idle long enough to trip a socket timeout must still be
/// cut off at the deadline.
///
/// This is the slowloris shape, and it is invisible to every stall-based test: a
/// peer that emits one byte every `DRIBBLE` restarts the socket's timeout
/// countdown on each read, so an implementation that arms the deadline once and
/// then calls `read_line` waits for as long as the peer cares to dribble —
/// forever, in the limit, with an operator alert stuck behind it and no timeout
/// ever firing.
///
/// The bound has to come from *elapsed time re-checked between reads*, which is
/// what `read_line_budgeted` does.
#[test]
fn a_relay_that_dribbles_bytes_still_cannot_outlive_the_deadline() {
    const DRIBBLE: Duration = Duration::from_millis(40);
    const DEADLINE: Duration = Duration::from_millis(300);

    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local addr").port();
    let handle = thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else {
            return;
        };
        // A greeting delivered one byte at a time, each well inside the deadline,
        // but totalling far more than it.
        let greeting = b"220 relay.internal ESMTP ready\r\n";
        for byte in greeting {
            let mut socket: &TcpStream = &stream;
            if socket.write_all(&[*byte]).is_err() || socket.flush().is_err() {
                return;
            }
            thread::sleep(DRIBBLE);
        }
    });

    let started = Instant::now();
    let result = email_channel(port).send(&alert(), DEADLINE);
    let elapsed = started.elapsed();

    match result {
        Err(ChannelError::Timeout { .. }) => {}
        other => panic!(
            "expected Timeout after {elapsed:?}; the relay never idled long enough to \
             trip a socket timeout, so only an elapsed-time budget can stop it. Got {other:?}"
        ),
    }
    // 31 bytes x 40ms = ~1.24s of dribbling if nothing bounds it.
    assert!(
        elapsed < Duration::from_millis(700),
        "send took {elapsed:?} — the budget is not re-checked between reads"
    );
    let _ = handle.join();
}

#[test]
fn a_public_relay_address_is_refused_before_any_packet_is_sent() {
    let channel = SmtpEmailChannel::new(
        SmtpRelayConfig::new("8.8.8.8", 25, SENDER, RECIPIENT, SENDER, KEY)
            .expect("config is structurally valid"),
    );
    match channel.send(&alert(), Duration::from_secs(2)) {
        Err(ChannelError::Unconfigured { detail }) => {
            assert!(detail.contains("non-private"), "detail: {detail}");
        }
        other => panic!("expected Unconfigured, got {other:?}"),
    }
}

#[test]
fn the_email_channel_reports_its_own_identity() {
    let channel = email_channel(1025);
    assert_eq!(channel.channel(), NotificationChannel::Email);
}

// ---------------------------------------------------------------- SMS / IF-11

struct ScriptedHttp {
    port: u16,
    handle: JoinHandle<String>,
}

/// Serve one HTTP request, sleeping `delay` before the response. Returns the raw
/// request text the client sent.
fn spawn_http(status_line: &'static str, body: &'static str, delay: Duration) -> ScriptedHttp {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local addr").port();

    let handle = thread::spawn(move || {
        let mut request = String::new();
        let Ok((stream, _)) = listener.accept() else {
            return request;
        };
        let mut reader = BufReader::new(stream);

        // Headers.
        let mut content_length = 0usize;
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => return request,
                Ok(_) => {}
            }
            if let Some(value) = line
                .to_ascii_lowercase()
                .strip_prefix("content-length:")
                .map(str::trim)
                .and_then(|v| v.parse::<usize>().ok())
            {
                content_length = value;
            }
            let done = line.trim().is_empty();
            request.push_str(&line);
            if done {
                break;
            }
        }

        // Body.
        if content_length > 0 {
            let mut body = vec![0u8; content_length];
            if reader.read_exact(&mut body).is_ok() {
                request.push_str(&String::from_utf8_lossy(&body));
            }
        }

        if !delay.is_zero() {
            thread::sleep(delay);
        }
        let mut socket: &TcpStream = reader.get_ref();
        let response = format!(
            "{status_line}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = socket.write_all(response.as_bytes());
        let _ = socket.flush();
        request
    });

    ScriptedHttp { port, handle }
}

fn sms_channel(port: u16) -> SmsGatewayChannel {
    SmsGatewayChannel::new(
        SmsGatewayConfig::new("127.0.0.1", port, "/sms", OPERATOR_SMS, KEY)
            .expect("config is valid"),
    )
}

#[test]
fn an_accepted_sms_returns_the_gateway_accept_id_and_posts_the_relay_contract() {
    let server = spawn_http("HTTP/1.1 202 Accepted", "SM-ACCEPT-4242", Duration::ZERO);
    let receipt = sms_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("relay accepted the message");
    assert_eq!(receipt.reference(), "SM-ACCEPT-4242");

    let request = server.handle.join().expect("server thread");
    assert!(request.starts_with("POST /sms HTTP/1.1"), "{request}");
    assert!(
        request.contains(&format!("Authorization: Bearer {KEY}")),
        "{request}"
    );
    assert!(request.contains("Connection: close"), "{request}");
    assert!(
        request.contains(&format!("\"to\":\"{OPERATOR_SMS}\"")),
        "{request}"
    );
    // SMS has no subject line, so the subject must ride in the text.
    assert!(
        request.contains("CRITICAL: IB connectivity lost: IB Gateway unreachable"),
        "{request}"
    );
}

#[test]
fn a_rejected_relay_credential_is_an_operator_setup_fault_not_a_bad_message() {
    let server = spawn_http("HTTP/1.1 401 Unauthorized", "bad token", Duration::ZERO);
    match sms_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Unconfigured { detail }) => {
            assert!(detail.contains("ATP_SMS_API_KEY"), "detail: {detail}");
        }
        other => panic!("expected Unconfigured, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn gateway_rate_limiting_and_outages_are_retryable_but_a_bad_request_is_not() {
    let throttled = spawn_http(
        "HTTP/1.1 429 Too Many Requests",
        "slow down",
        Duration::ZERO,
    );
    match sms_channel(throttled.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { .. }) => {}
        other => panic!("expected TransportUnavailable for 429, got {other:?}"),
    }
    let _ = throttled.handle.join();

    let outage = spawn_http("HTTP/1.1 503 Service Unavailable", "down", Duration::ZERO);
    match sms_channel(outage.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { .. }) => {}
        other => panic!("expected TransportUnavailable for 503, got {other:?}"),
    }
    let _ = outage.handle.join();

    let malformed = spawn_http("HTTP/1.1 400 Bad Request", "no such number", Duration::ZERO);
    match sms_channel(malformed.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { .. }) => {}
        other => panic!("expected Rejected for 400, got {other:?}"),
    }
    let _ = malformed.handle.join();
}

#[test]
fn a_stalled_sms_gateway_is_bounded_by_the_send_deadline() {
    let server = spawn_http(
        "HTTP/1.1 202 Accepted",
        "SM-LATE",
        Duration::from_millis(1500),
    );
    let started = Instant::now();
    let result = sms_channel(server.port).send(&alert(), Duration::from_millis(300));
    let elapsed = started.elapsed();

    match result {
        Err(ChannelError::Timeout { .. }) => {}
        other => panic!("expected Timeout, got {other:?}"),
    }
    assert!(
        elapsed < Duration::from_millis(900),
        "send took {elapsed:?}, well past its 300ms deadline"
    );
    let _ = server.handle.join();
}

#[test]
fn an_accepted_sms_with_no_body_never_fabricates_an_accept_id() {
    let server = spawn_http("HTTP/1.1 204 No Content", "", Duration::ZERO);
    let receipt = sms_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("relay accepted the message");
    assert_eq!(receipt.reference(), "http-204-no-reference");
    let _ = server.handle.join();
}

#[test]
fn the_sms_channel_reports_its_own_identity() {
    let channel = sms_channel(8025);
    assert_eq!(channel.channel(), NotificationChannel::Sms);
}
