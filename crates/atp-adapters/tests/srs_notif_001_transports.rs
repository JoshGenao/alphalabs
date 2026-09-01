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

use atp_adapters::notification::{PushChannel, PushConfig, SmtpEmailChannel, SmtpRelayConfig};
use atp_notification::{
    ChannelError, ChannelHandoff, NotificationChannel, NotificationChannelClient,
    NotificationMessage,
};

const SENDER: &str = "atp@example.com";
const RECIPIENT: &str = "operator@example.com";
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
    // THE KIND OF HAND-OFF, not just its reference. A 250 from
    // phase1-notification-egress means a queue THIS SYSTEM operates took the
    // message; it can still fail entirely at the provider. Without this
    // assertion the adapter could claim a destination acknowledgement and every
    // other test in this file would still pass — verified by mutation.
    assert_eq!(
        receipt.handoff(),
        ChannelHandoff::QueuedForRelay,
        "the IF-10 relay hop is a queue we operate, never a destination ack"
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
/// The credential must not reach the error detail the dispatcher PERSISTS.
///
/// The stored `ChannelDelivery` detail is durable, operator-facing audit data. A
/// benign `535` proves nothing here — the interesting relay is one that echoes
/// the AUTH payload back, which is both a realistic misconfiguration (verbose
/// logging relays quote the offending command) and the obvious hostile move. A
/// relay being on a private network does not make it trusted.
#[test]
fn an_echoing_relay_cannot_get_the_credential_into_the_stored_error() {
    const ENCODED: &str = "AGF0cEBleGFtcGxlLmNvbQByZWxheS1zZWNyZXQ=";

    let mut script = accepting_script();
    // The relay quotes back exactly what it was sent — both the base64 blob and
    // the raw key.
    script[2] = format!("535 5.7.8 rejected: AUTH PLAIN {ENCODED} (key {KEY})");
    let server = spawn_smtp(script, Duration::ZERO);

    match email_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(
                detail.contains("535"),
                "the code must still be reported: {detail}"
            );
            assert!(
                !detail.contains(ENCODED),
                "encoded credential leaked into the stored detail: {detail}"
            );
            assert!(
                !detail.contains(KEY),
                "raw credential leaked into the stored detail: {detail}"
            );
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// The SUCCESS path stores relay text too, and is no safer than the failure path.
///
/// The final `250`'s text becomes the `ChannelReceipt` reference, which the
/// dispatcher persists as the delivery detail. A relay echoing the key there
/// writes it into the audit log along the HAPPY path, where nobody thinks to
/// look for a leak.
#[test]
fn an_echoing_relay_cannot_get_the_credential_into_a_successful_receipt() {
    const ENCODED: &str = "AGF0cEBleGFtcGxlLmNvbQByZWxheS1zZWNyZXQ=";

    let mut script = accepting_script();
    script[6] = format!("250 2.0.0 Ok: queued as Q-1 (auth {ENCODED}, key {KEY})");
    let server = spawn_smtp(script, Duration::ZERO);

    let receipt = email_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("relay accepted the message");

    // Still the EMAIL channel: a relay queue id, so still the weaker hand-off.
    assert_eq!(receipt.handoff(), ChannelHandoff::QueuedForRelay);
    assert!(
        receipt.reference().contains("Q-1"),
        "the queue id must survive: {:?}",
        receipt.reference()
    );
    assert!(
        !receipt.reference().contains(ENCODED),
        "encoded credential reached the stored receipt: {:?}",
        receipt.reference()
    );
    assert!(
        !receipt.reference().contains(KEY),
        "raw credential reached the stored receipt: {:?}",
        receipt.reference()
    );
    let _ = server.handle.join();
}

/// Defence in depth: a relay that echoes the credential at a *later* stage — one
/// the AUTH-stage rule does not cover — must also be scrubbed.
#[test]
fn a_relay_echoing_the_credential_at_a_later_stage_is_also_redacted() {
    const ENCODED: &str = "AGF0cEBleGFtcGxlLmNvbQByZWxheS1zZWNyZXQ=";

    let mut script = accepting_script();
    // Auth succeeds; the credential comes back in the RCPT TO rejection instead.
    script[4] = format!("550 5.1.1 no such user (seen AUTH {ENCODED}, key {KEY})");
    let server = spawn_smtp(script, Duration::ZERO);

    match email_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(detail.contains("550"), "detail: {detail}");
            assert!(!detail.contains(ENCODED), "leaked: {detail}");
            assert!(!detail.contains(KEY), "leaked: {detail}");
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

/// DNS must not be able to eat the send deadline.
///
/// `ToSocketAddrs` is blocking with no timeout, so a sick resolver is the one
/// step most able to wedge an alert send. `.invalid` is reserved by RFC 6761 and
/// must never resolve, which makes a real resolver do real (failing) work here
/// rather than answering from cache.
///
/// The assertion is the *bound*, not the error kind: whether this host fails
/// fast (NXDOMAIN) or slowly (a resolver that retries), the send must return
/// well inside the deadline and never hang.
#[test]
fn a_relay_hostname_that_will_not_resolve_cannot_outlive_the_deadline() {
    let channel = SmtpEmailChannel::new(
        SmtpRelayConfig::new(
            "atp-notification-relay-does-not-exist.invalid",
            1025,
            SENDER,
            RECIPIENT,
            SENDER,
            KEY,
        )
        .expect("config is structurally valid"),
    );

    let started = Instant::now();
    let result = channel.send(&alert(), Duration::from_millis(400));
    let elapsed = started.elapsed();

    match result {
        // Either outcome is correct; both are bounded and neither fabricates a
        // delivery. A resolver that answers NXDOMAIN quickly gives
        // TransportUnavailable; one that stalls past the budget gives Timeout.
        Err(ChannelError::TransportUnavailable { .. }) | Err(ChannelError::Timeout { .. }) => {}
        other => panic!("expected a bounded resolution failure, got {other:?}"),
    }
    assert!(
        elapsed < Duration::from_secs(3),
        "resolution took {elapsed:?} against a 400ms deadline — DNS is outside the budget"
    );
}

/// An IP-literal relay must not spawn a resolver thread at all.
///
/// Verified behaviourally: the connect refusal has to arrive far faster than any
/// resolver round trip, which it cannot if the literal is being pushed through
/// `to_socket_addrs` on a worker.
#[test]
fn an_ip_literal_relay_host_skips_resolution_entirely() {
    let port = {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        listener.local_addr().expect("local addr").port()
    };
    let channel = email_channel(port);

    let started = Instant::now();
    let result = channel.send(&alert(), Duration::from_secs(2));
    let elapsed = started.elapsed();

    assert!(matches!(
        result,
        Err(ChannelError::TransportUnavailable { .. })
    ));
    assert!(
        elapsed < Duration::from_millis(250),
        "an IP literal took {elapsed:?} — it is going through the resolver path"
    );
}

#[test]
fn the_email_channel_reports_its_own_identity() {
    let channel = email_channel(1025);
    assert_eq!(channel.channel(), NotificationChannel::Email);
}

// --------------------------------------------------------------- push / IF-11

struct ScriptedHttp {
    port: u16,
    handle: JoinHandle<String>,
}

/// Serve one HTTP request, sleeping `delay` before the response. Returns the raw
/// request text the client sent.
///
/// Bodies below are the REAL shapes a live ntfy returns, captured with `curl -v`
/// against `ntfy.sh` and a local `binwiederhier/ntfy` before this suite was
/// written — not shapes invented to match the implementation.
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
const PUSH_TOPIC: &str = "atp-alerts-9f8e7d6c5b4a3210";

/// A real ntfy 2xx body. Note it ECHOES the topic — which is a publish
/// credential — so the success path is the one that reliably carries a secret.
const NTFY_OK: &str = concat!(
    r#"{"id":"b19RmMeCXTYy","time":1786939980,"expires":1786983180,"#,
    r#""event":"message","topic":"atp-alerts-9f8e7d6c5b4a3210","message":"ATP alert"}"#
);

fn push_channel(port: u16) -> PushChannel {
    PushChannel::new(PushConfig::new("127.0.0.1", port, PUSH_TOPIC, KEY).expect("config is valid"))
}

#[test]
fn an_accepted_publish_returns_ntfys_message_id_and_posts_the_ntfy_contract() {
    let server = spawn_http("HTTP/1.1 200 OK", NTFY_OK, Duration::ZERO);
    let receipt = push_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("ntfy accepted the publish");
    assert_eq!(receipt.reference(), "b19RmMeCXTYy");
    // ntfy is a destination OUTSIDE this system, so the push leg legitimately
    // claims the stronger hand-off. Pinned so the two channels cannot silently
    // converge on one meaning — the email leg must stay QueuedForRelay.
    assert_eq!(
        receipt.handoff(),
        ChannelHandoff::AcceptedByDestination,
        "ntfy acknowledged the message itself; nothing of ours still holds it"
    );

    let request = server.handle.join().expect("server thread");
    // The topic is the URL PATH on ntfy — there is no relay path to post to.
    assert!(
        request.starts_with(&format!("POST /{PUSH_TOPIC} HTTP/1.1")),
        "{request}"
    );
    assert!(
        request.contains(&format!("Authorization: Bearer {KEY}")),
        "{request}"
    );
    // Plain text, not JSON: ntfy takes the message as the raw body.
    assert!(
        request.contains("Content-Type: text/plain; charset=utf-8"),
        "{request}"
    );
    assert!(
        request.contains("X-Title: CRITICAL: IB connectivity lost"),
        "{request}"
    );
    assert!(request.contains("X-Priority: 5"), "{request}");
    assert!(request.contains("Connection: close"), "{request}");
    // The subject rides in the BODY too, so the alert still states what fired
    // even if the title header is dropped anywhere in the path.
    assert!(
        request.contains("CRITICAL: IB connectivity lost: IB Gateway unreachable"),
        "{request}"
    );
}

#[test]
fn a_refused_token_is_an_operator_setup_fault_not_a_bad_message() {
    // ntfy's real 401: a wrong or revoked token.
    let server = spawn_http(
        "HTTP/1.1 401 Unauthorized",
        r#"{"code":40101,"http":401,"error":"unauthorized"}"#,
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Unconfigured { detail }) => {
            assert!(detail.contains("ATP_PUSH_TOKEN"), "detail: {detail}");
        }
        other => panic!("expected Unconfigured, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// ntfy answers 403, not 401, when the token is valid but has no publish access
/// to this topic (and when no token is sent to a protected topic). Both were
/// confirmed against a live instance; both are setup faults, so both must land
/// on `Unconfigured` rather than being read as a transport outage and retried.
#[test]
fn a_token_without_access_to_the_topic_is_also_a_setup_fault() {
    let server = spawn_http(
        "HTTP/1.1 403 Forbidden",
        r#"{"code":40301,"http":403,"error":"forbidden"}"#,
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Unconfigured { detail }) => {
            assert!(detail.contains("ATP_PUSH_TOPIC"), "detail: {detail}");
        }
        other => panic!("expected Unconfigured, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn rate_limiting_and_outages_are_retryable_but_a_bad_request_is_not() {
    let throttled = spawn_http(
        "HTTP/1.1 429 Too Many Requests",
        "slow down",
        Duration::ZERO,
    );
    match push_channel(throttled.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { .. }) => {}
        other => panic!("expected TransportUnavailable for 429, got {other:?}"),
    }
    let _ = throttled.handle.join();

    let outage = spawn_http("HTTP/1.1 503 Service Unavailable", "down", Duration::ZERO);
    match push_channel(outage.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { .. }) => {}
        other => panic!("expected TransportUnavailable for 503, got {other:?}"),
    }
    let _ = outage.handle.join();

    // ntfy's real 400, e.g. an out-of-range X-Priority.
    let malformed = spawn_http(
        "HTTP/1.1 400 Bad Request",
        r#"{"code":40007,"http":400,"error":"invalid priority parameter"}"#,
        Duration::ZERO,
    );
    match push_channel(malformed.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { .. }) => {}
        other => panic!("expected Rejected for 400, got {other:?}"),
    }
    let _ = malformed.handle.join();
}

#[test]
fn a_stalled_ntfy_is_bounded_by_the_send_deadline() {
    let server = spawn_http("HTTP/1.1 200 OK", NTFY_OK, Duration::from_millis(1500));
    let started = Instant::now();
    let result = push_channel(server.port).send(&alert(), Duration::from_millis(300));
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

/// A 200 whose body says the message became a file attachment must FAIL.
///
/// This is the sharpest failure mode on this transport, because every signal
/// says success: HTTP 200, a valid message id, no error field. What actually
/// happened is that ntfy replaced the alert text with "You received a file",
/// so the operator's phone shows a filename instead of the outage. Recording
/// that as a delivery would put a lie in the audit trail.
#[test]
fn a_2xx_that_converted_the_alert_into_a_file_is_not_a_delivery() {
    let server = spawn_http(
        "HTTP/1.1 200 OK",
        concat!(
            r#"{"id":"KKGUUf398qAB","time":1786939993,"event":"message","#,
            r#""topic":"atp-alerts-9f8e7d6c5b4a3210","#,
            r#""message":"You received a file: attachment.txt","#,
            r#""attachment":{"name":"attachment.txt","type":"text/plain","size":4097}}"#
        ),
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(detail.contains("attachment"), "detail: {detail}");
        }
        other => panic!("expected Rejected for an attachment conversion, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// The bearer token must not survive into the durable store — on EITHER path.
///
/// The 2xx body becomes the stored `ChannelReceipt` reference, so a success is
/// just as capable of carrying the secret as a failure is. A server that echoes
/// the `Authorization` header it was handed is a realistic verbose-logging
/// misconfiguration as well as the obvious hostile move.
#[test]
fn an_echoing_server_cannot_get_the_token_into_a_stored_receipt() {
    let server = spawn_http(
        "HTTP/1.1 200 OK",
        r#"{"id":"ok with Authorization: Bearer relay-secret"}"#,
        Duration::ZERO,
    );
    let receipt = push_channel(server.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("ntfy accepted the publish");
    assert!(
        !receipt.reference().contains(KEY),
        "the bearer token reached the stored receipt: {:?}",
        receipt.reference()
    );
    let _ = server.handle.join();
}

/// The TOPIC must not survive into the durable store either.
///
/// Distinct from the token test on purpose. ntfy echoes the topic in every
/// success body **by design**, so this is not a hostile-server hypothetical —
/// it is the documented happy path, and the topic is a publish credential. This
/// is the test that would have caught treating the topic as an ordinary
/// destination.
#[test]
fn the_ntfy_topic_never_reaches_a_stored_receipt_or_error() {
    let ok = spawn_http("HTTP/1.1 200 OK", NTFY_OK, Duration::ZERO);
    let receipt = push_channel(ok.port)
        .send(&alert(), Duration::from_secs(5))
        .expect("ntfy accepted the publish");
    assert!(
        !receipt.reference().contains(PUSH_TOPIC),
        "the topic reached the stored receipt: {:?}",
        receipt.reference()
    );
    let _ = ok.handle.join();

    let bad = spawn_http(
        "HTTP/1.1 400 Bad Request",
        r#"{"error":"bad topic atp-alerts-9f8e7d6c5b4a3210"}"#,
        Duration::ZERO,
    );
    match push_channel(bad.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(
                detail.contains("400"),
                "the status must still surface: {detail}"
            );
            assert!(
                !detail.contains(PUSH_TOPIC),
                "the topic leaked into: {detail}"
            );
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
    let _ = bad.handle.join();
}

#[test]
fn an_echoing_server_cannot_get_the_token_into_a_stored_error() {
    let server = spawn_http(
        "HTTP/1.1 400 Bad Request",
        "rejected: Bearer relay-secret is malformed",
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(
                detail.contains("400"),
                "the status must still surface: {detail}"
            );
            assert!(
                !detail.contains(KEY),
                "the bearer token leaked into: {detail}"
            );
        }
        other => panic!("expected Rejected, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// A malformed status line is server-controlled text that flows straight into a
/// persisted error, and it is read BEFORE the response body is scrubbed — so the
/// redaction has to happen inside the status parser, not after it.
#[test]
fn a_malformed_status_line_echoing_a_secret_is_redacted_too() {
    let server = spawn_http(
        "GARBAGE Authorization: Bearer relay-secret atp-alerts-9f8e7d6c5b4a3210",
        "",
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(
                !detail.contains(KEY),
                "the bearer token leaked into: {detail}"
            );
            assert!(
                !detail.contains(PUSH_TOPIC),
                "the topic leaked into: {detail}"
            );
        }
        other => panic!("expected TransportUnavailable, got {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn the_push_channel_reports_its_own_identity() {
    assert_eq!(push_channel(18080).channel(), NotificationChannel::Push);
}

/// A 2xx with no ntfy message id is NOT a delivery.
///
/// TIGHTENED (adversarial review round 4). This previously asserted that such a
/// reply produced a synthetic `http-200-no-reference` receipt — which the
/// dispatcher then stored as a DELIVERED page. ntfy returns an id on every 2xx
/// (verified against ntfy.sh and a local instance, attachment conversions
/// included), so a 2xx WITHOUT one did not come from ntfy at all: an
/// intercepting proxy, a captive portal, or a reverse proxy aimed at the wrong
/// upstream will cheerfully answer 200 with an empty or non-JSON body. Storing
/// that as a success is the worst possible audit entry, because it reads as
/// proof the operator was reached.
#[test]
fn a_2xx_without_an_ntfy_message_id_is_not_a_delivery() {
    // A COMPLETE JSON object that simply lacks an id hits the id guard...
    let server = spawn_http("HTTP/1.1 200 OK", r#"{"status":"queued"}"#, Duration::ZERO);
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(detail.contains("no ntfy message id"), "detail: {detail}");
        }
        other => panic!("a 200 without an id must not be a delivery: {other:?}"),
    }
    let _ = server.handle.join();

    // ...and a reply that is not a JSON object at all is refused earlier, by the
    // completeness gate. Either way it is never a delivery, which is the
    // property that matters.
    for body in ["", "OK", "<html>proxy</html>"] {
        let server = spawn_http("HTTP/1.1 200 OK", body, Duration::ZERO);
        match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
            Err(ChannelError::TransportUnavailable { .. }) => {}
            other => panic!("a 200 with body {body:?} must not be a delivery: {other:?}"),
        }
        let _ = server.handle.join();
    }
}

/// A topic that collides with a JSON key ntfy uses must not blind the parser.
///
/// The bug this pins (found by adversarial review): the transport used to
/// redact the response body FIRST and parse the redacted text. Redaction is a
/// blind substring replace and the topic is operator-chosen, so a topic of
/// `attachment` rewrote the JSON KEY `"attachment"` into `"<redacted>"` — and
/// the silent-conversion guard never fired. An alert ntfy had turned into a
/// file would have been recorded as DELIVERED in the durable audit trail.
///
/// Structure is now read from the RAW payload and only extracted values are
/// scrubbed, so a colliding topic changes nothing about what is detected.
#[test]
fn a_topic_named_after_a_json_key_can_not_blind_the_attachment_guard() {
    let channel = PushChannel::new(
        PushConfig::new("127.0.0.1", 1, "attachment", KEY).expect("config is valid"),
    );
    let _ = channel; // config builds; the wire behaviour is asserted below.

    let server = spawn_http(
        "HTTP/1.1 200 OK",
        concat!(
            r#"{"id":"KKGUUf398qAB","event":"message","topic":"attachment","#,
            r#""message":"You received a file: attachment.txt","#,
            r#""attachment":{"name":"attachment.txt","size":4097}}"#
        ),
        Duration::ZERO,
    );
    let colliding = PushChannel::new(
        PushConfig::new("127.0.0.1", server.port, "attachment", KEY).expect("config is valid"),
    );
    match colliding.send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::Rejected { detail }) => {
            assert!(detail.contains("attachment"), "detail: {detail}");
        }
        other => panic!("a colliding topic hid the attachment conversion: {other:?}"),
    }
    let _ = server.handle.join();
}

/// The same collision against the reference lookup: a topic of `id` must not
/// stop the real message id being read, and must still not reach the store.
#[test]
fn a_topic_named_id_still_yields_the_real_message_id_and_is_still_redacted() {
    let server = spawn_http(
        "HTTP/1.1 200 OK",
        r#"{"id":"b19RmMeCXTYy","event":"message","topic":"id"}"#,
        Duration::ZERO,
    );
    let channel = PushChannel::new(
        PushConfig::new("127.0.0.1", server.port, "id", KEY).expect("config is valid"),
    );
    let receipt = channel
        .send(&alert(), Duration::from_secs(5))
        .expect("ntfy accepted the publish");
    assert_eq!(receipt.reference(), "b19RmMeCXTYy");
    let _ = server.handle.join();
}

/// A 2xx too large to read in full must NOT be recorded as a delivery.
///
/// Found by adversarial review. The capped read used to return the truncated
/// prefix as success, and `post` then drew a conclusion from what was ABSENT:
/// a body whose `attachment` key sat past the 64 KiB cap looked exactly like a
/// clean delivery, so an alert ntfy had turned into a file would have been
/// stored as Delivered. A reply the adapter cannot read in full leaves the
/// outcome indeterminate, and on an alert path indeterminate must record as a
/// failure.
#[test]
fn a_2xx_larger_than_the_read_cap_is_a_failure_not_a_delivery() {
    // 128 KiB of padding, with the attachment signal only at the very end —
    // past any cap, exactly the shape that used to slip through.
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
    let port = listener.local_addr().expect("local addr").port();
    let handle = thread::spawn(move || {
        let Ok((stream, _)) = listener.accept() else {
            return;
        };
        let mut reader = BufReader::new(stream);
        loop {
            let mut line = String::new();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            if line.trim().is_empty() {
                break;
            }
        }
        let padding = "x".repeat(128 * 1024);
        let body = format!(
            "{{\"id\":\"EARLY-ID\",\"pad\":\"{padding}\",\"attachment\":{{\"size\":4097}}}}"
        );
        let mut socket: &TcpStream = reader.get_ref();
        let _ = socket.write_all(
            format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
            .as_bytes(),
        );
        let _ = socket.flush();
    });

    let channel = PushChannel::new(
        PushConfig::new("127.0.0.1", port, PUSH_TOPIC, KEY).expect("config is valid"),
    );
    match channel.send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(detail.contains("truncated"), "detail: {detail}");
        }
        other => panic!("an unreadable oversized 2xx must not be a delivery: {other:?}"),
    }
    let _ = handle.join();
}

/// A topic that collides with the STATUS LINE must not turn a success into a
/// failure.
///
/// Found by adversarial review. `read_status_line` redacted before parsing, and
/// the topic is operator-chosen from `[A-Za-z0-9_-]`, so a topic of `HTTP` or
/// `200` rewrote the protocol token or the status code inside
/// `HTTP/1.1 200 OK` — a real ntfy delivery was recorded as "reply is not HTTP".
/// Same rule as the body: parse raw, redact only what reaches an error.
#[test]
fn a_topic_colliding_with_the_status_line_does_not_break_a_successful_publish() {
    for topic in ["HTTP", "200", "OK"] {
        let server = spawn_http(
            "HTTP/1.1 200 OK",
            r#"{"id":"b19RmMeCXTYy","event":"message"}"#,
            Duration::ZERO,
        );
        let channel = PushChannel::new(
            PushConfig::new("127.0.0.1", server.port, topic, KEY).expect("config is valid"),
        );
        let receipt = channel
            .send(&alert(), Duration::from_secs(5))
            .unwrap_or_else(|err| panic!("topic {topic:?} broke a real delivery: {err:?}"));
        assert_eq!(receipt.reference(), "b19RmMeCXTYy");
        let _ = server.handle.join();
    }
}

/// ...and a malformed status line still has its secrets scrubbed.
///
/// The pair matters: fixing the collision by simply dropping the redaction would
/// pass the test above and leak a credential into the persisted error.
#[test]
fn a_malformed_status_line_is_still_scrubbed_after_the_parse_order_fix() {
    let server = spawn_http(
        "NOTHTTP Authorization: Bearer relay-secret atp-alerts-9f8e7d6c5b4a3210",
        "",
        Duration::ZERO,
    );
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(!detail.contains(KEY), "token leaked: {detail}");
            assert!(!detail.contains(PUSH_TOPIC), "topic leaked: {detail}");
        }
        other => panic!("expected TransportUnavailable, got {other:?}"),
    }
    let _ = server.handle.join();
}

/// A 2xx whose body was cut short must be a FAILURE, over a real socket.
///
/// Found by adversarial review. `{"id":"EARLY-ID"` carries a complete string
/// value, so the id scanner accepted it and the adapter recorded Delivered — a
/// reply truncated by a crash or a proxy dropping the connection became a
/// successful operator page in the durable audit trail. Driven over a socket and
/// not only as a unit test, because the bytes that reach the store come off the
/// wire.
/// A raw control character inside a JSON string is not valid JSON, so a 2xx
/// carrying one is not an ntfy reply and must not be a delivery.
///
/// Separate from the case list above because it needs a real newline in the body
/// rather than a raw-string literal.
#[test]
fn a_2xx_with_a_raw_control_character_in_the_id_is_not_a_delivery() {
    let server = spawn_http("HTTP/1.1 200 OK", "{\"id\":\"EARLY\nID\"}", Duration::ZERO);
    match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
        Err(ChannelError::TransportUnavailable { detail }) => {
            assert!(
                detail.contains("not a complete JSON object"),
                "detail: {detail}"
            );
        }
        other => panic!("a raw control character must not be a delivery: {other:?}"),
    }
    let _ = server.handle.join();
}

#[test]
fn a_2xx_with_a_truncated_json_body_is_not_a_delivery() {
    for truncated in [
        r#"{"id":"EARLY-ID""#,
        r#"{"id":"EARLY-ID","attachment":{"size":4097"#,
        r#"{"#,
        // Mismatched delimiters: syntactically invalid but carrying a readable
        // top-level id. A single depth counter accepted these.
        r#"{"id":"EARLY-ID"]"#,
        r#"{"id":"x","actions":[{"y":1}}]"#,
        // Balanced AND well-delimited, but not valid JSON. The guard's question
        // is "is this the document ntfy sends", not "are the brackets tidy".
        r#"{"id":"EARLY-ID" garbage}"#,
        r#"{id:"EARLY-ID"}"#,
        r#"{"id":"EARLY-ID",}"#,
        // Invalid escape and a raw control character inside the id string.
        r#"{"id":"EARLY\qID"}"#,
    ] {
        let server = spawn_http("HTTP/1.1 200 OK", truncated, Duration::ZERO);
        match push_channel(server.port).send(&alert(), Duration::from_secs(5)) {
            Err(ChannelError::TransportUnavailable { detail }) => {
                assert!(
                    detail.contains("not a complete JSON object"),
                    "detail: {detail}"
                );
            }
            other => panic!("truncated body {truncated:?} must not be a delivery: {other:?}"),
        }
        let _ = server.handle.join();
    }
}
