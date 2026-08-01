//! SRS-NOTIF-001 / IF-11 — the operator **SMS** transport.
//!
//! Posts the alert to the local `phase1-notification-egress` relay over plain
//! HTTP/1.1; the relay owns the authenticated TLS call to the real SMS gateway
//! (see the [module docs](super)).
//!
//! ## The relay contract this speaks
//!
//! `POST <path>` with a JSON body `{"to": "<number>", "text": "<message>"}`, an
//! `Authorization: Bearer <ATP_SMS_API_KEY>` header, and `Connection: close`.
//! A 2xx response body is the gateway's opaque accept id and becomes the
//! [`ChannelReceipt`] reference; any other status maps onto the [`ChannelError`]
//! taxonomy by its class.
//!
//! `Connection: close` is deliberate: it makes "read to end of stream" an exact
//! definition of the response body, so the adapter needs neither chunked-transfer
//! decoding nor trust in a `Content-Length` a broken relay might understate.

use std::io::{BufReader, Write};
use std::net::TcpStream;
use std::time::Duration;

use atp_notification::{
    ChannelError, ChannelReceipt, ChannelSendResult, NotificationChannel,
    NotificationChannelClient, NotificationMessage,
};

use super::{
    arm_socket, fold_protocol_line, io_error_to_channel_error, read_line_budgeted,
    read_to_end_budgeted, EgressEndpoint, SendBudget,
};

const CHANNEL: &str = "sms";

const DEFAULT_RELAY_HOST: &str = "phase1-notification-egress";
const DEFAULT_RELAY_PORT: u16 = 8025;
const DEFAULT_RELAY_PATH: &str = "/sms";

/// Cap on the response the adapter will read. A relay that streams without end
/// must not be able to exhaust memory on the alert path.
const MAX_RESPONSE_BYTES: usize = 64 * 1024;
/// Cap on the HTTP status line specifically, which is short by construction.
const MAX_STATUS_LINE_BYTES: usize = 8 * 1024;
/// Cap on the accept id stored on the delivery record.
const MAX_REFERENCE_CHARS: usize = 256;

/// SMS bodies are hard-limited by the carrier. Truncating here — rather than
/// letting the gateway silently drop or split the message — keeps what the
/// operator receives predictable, and the marker makes the truncation visible
/// instead of passing a cut-off alert off as the whole one.
const MAX_SMS_BODY_CHARS: usize = 480;
const TRUNCATION_MARKER: &str = "…[truncated]";

/// Operator configuration for the SMS transport.
///
/// As with the email transport, the key authenticates to the **relay**; the real
/// gateway credential never enters this process (NFR-S4).
#[derive(Clone, PartialEq, Eq)]
pub struct SmsGatewayConfig {
    endpoint: EgressEndpoint,
    path: String,
    recipient: String,
    api_key: String,
}

impl core::fmt::Debug for SmsGatewayConfig {
    /// Redacts the credential — see [`super::smtp::SmtpRelayConfig`]'s impl for
    /// why this is written by hand rather than derived.
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("SmsGatewayConfig")
            .field("endpoint", &self.endpoint)
            .field("path", &self.path)
            .field("recipient", &self.recipient)
            .field("api_key", &"<redacted>")
            .finish()
    }
}

impl SmsGatewayConfig {
    pub fn new(
        host: impl Into<String>,
        port: u16,
        path: impl Into<String>,
        recipient: impl Into<String>,
        api_key: impl Into<String>,
    ) -> Result<Self, ChannelError> {
        let endpoint = EgressEndpoint::new(host, port, CHANNEL)?;
        let path = path.into();
        let recipient = recipient.into();
        let api_key = api_key.into();

        for (label, value) in [
            ("relay path", &path),
            ("operator number (ATP_OPERATOR_SMS)", &recipient),
            ("relay credential (ATP_SMS_API_KEY)", &api_key),
        ] {
            if value.trim().is_empty() {
                return Err(ChannelError::Unconfigured {
                    detail: format!("{CHANNEL}: {label} is not configured"),
                });
            }
        }

        if !path.starts_with('/') {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: relay path must start with '/', got {path:?}"),
            });
        }
        // The path and the credential are interpolated into the request line and
        // a header respectively. Whitespace or a control character in either
        // would split the line and let the remainder be read as a header or a
        // second request (request smuggling) — and unlike the message subject,
        // neither can be repaired by folding.
        if path.chars().any(|c| c.is_whitespace() || c.is_control()) {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: relay path contains whitespace or a control character"),
            });
        }
        if api_key.chars().any(|c| c.is_control()) {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: relay credential contains a control character"),
            });
        }

        Ok(Self {
            endpoint,
            path,
            recipient,
            api_key,
        })
    }

    /// Read the configuration from the process environment. `ATP_SMS_API_KEY` is
    /// the catalogued SRS-SEC-001 secret; the rest default to the compose sidecar.
    pub fn from_env(read: impl Fn(&str) -> Option<String>) -> Result<Self, ChannelError> {
        let host = read("ATP_SMS_RELAY_HOST").unwrap_or_else(|| DEFAULT_RELAY_HOST.to_string());
        let port = match read("ATP_SMS_RELAY_PORT") {
            Some(raw) => raw
                .trim()
                .parse::<u16>()
                .map_err(|_| ChannelError::Unconfigured {
                    detail: format!("{CHANNEL}: ATP_SMS_RELAY_PORT is not a valid port: {raw:?}"),
                })?,
            None => DEFAULT_RELAY_PORT,
        };
        let path = read("ATP_SMS_RELAY_PATH").unwrap_or_else(|| DEFAULT_RELAY_PATH.to_string());
        let recipient = read("ATP_OPERATOR_SMS").unwrap_or_default();
        let api_key = read("ATP_SMS_API_KEY").unwrap_or_default();

        Self::new(host, port, path, recipient, api_key)
    }
}

/// The IF-11 SMS channel client.
#[derive(Debug, Clone)]
pub struct SmsGatewayChannel {
    config: SmsGatewayConfig,
}

impl SmsGatewayChannel {
    pub fn new(config: SmsGatewayConfig) -> Self {
        Self { config }
    }

    fn post(&self, message: &NotificationMessage, budget: &SendBudget) -> ChannelSendResult {
        let body = json_object(&[("to", &self.config.recipient), ("text", &sms_text(message))]);

        // `Host` carries the configured host verbatim, and the path was validated
        // at construction, so no request-line or header field can be split here.
        let request = format!(
            "POST {} HTTP/1.1\r\n\
             Host: {}:{}\r\n\
             Authorization: Bearer {}\r\n\
             Content-Type: application/json\r\n\
             Content-Length: {}\r\n\
             Connection: close\r\n\
             \r\n\
             {}",
            self.config.path,
            fold_protocol_line(self.config.endpoint.host()),
            self.config.endpoint.port(),
            self.config.api_key,
            body.len(),
            body,
        );

        let stream = self.config.endpoint.connect(budget, CHANNEL)?;
        arm_socket(&stream, budget, CHANNEL, "request write")?;
        {
            let mut socket: &TcpStream = &stream;
            socket
                .write_all(request.as_bytes())
                .and_then(|()| socket.flush())
                .map_err(|err| io_error_to_channel_error(&err, CHANNEL, "request write"))?;
        }

        let mut reader = BufReader::new(stream);
        let status = read_status_line(&mut reader, budget)?;
        let raw_payload = read_to_end_bounded(&mut reader, budget)?;

        // Scrub the bearer token BEFORE the payload can reach a receipt or an
        // error, both of which the dispatcher PERSISTS to the durable
        // notification store. A relay that echoes the Authorization header it
        // was sent -- verbose logging, or hostility -- would otherwise write a
        // recoverable ATP_SMS_API_KEY into the operator's audit trail. Applied to
        // the 2xx path too: the accept id becomes the stored receipt reference,
        // so a success is just as capable of carrying the secret as a failure.
        let payload = redact_secret(&raw_payload, &self.config.api_key);

        match status {
            200..=299 => Ok(ChannelReceipt::new(accept_reference(&payload, status))),
            // 401/403 are an operator setup fault (a wrong or revoked relay
            // credential), not a message the gateway found unacceptable — the
            // remediation is to fix configuration, which is what Unconfigured
            // tells the operator to do.
            401 | 403 => Err(ChannelError::Unconfigured {
                detail: format!(
                    "{CHANNEL}: relay refused the credential (HTTP {status}) — check \
                     ATP_SMS_API_KEY"
                ),
            }),
            // Rate limiting is explicitly transient, and 5xx is a relay/gateway
            // outage: both clear on their own, unlike a malformed request.
            408 | 429 | 500..=599 => Err(ChannelError::TransportUnavailable {
                detail: format!(
                    "{CHANNEL}: relay returned HTTP {status}: {}",
                    body_snippet(&payload)
                ),
            }),
            _ => Err(ChannelError::Rejected {
                detail: format!(
                    "{CHANNEL}: relay returned HTTP {status}: {}",
                    body_snippet(&payload)
                ),
            }),
        }
    }
}

impl NotificationChannelClient for SmsGatewayChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Sms
    }

    fn send(&self, message: &NotificationMessage, deadline: Duration) -> ChannelSendResult {
        let budget = SendBudget::start(deadline);
        self.post(message, &budget)
    }
}

/// Build the SMS text.
///
/// SMS carries no subject line, so the subject is prefixed onto the body — the
/// severity and trigger live there, and dropping it would leave the operator with
/// an alert body and no statement of what fired.
fn sms_text(message: &NotificationMessage) -> String {
    let subject = message.subject().trim();
    let body = message.body().trim();
    let combined = if subject.is_empty() {
        body.to_string()
    } else if body.is_empty() {
        subject.to_string()
    } else {
        format!("{subject}: {body}")
    };
    truncate_chars(&combined, MAX_SMS_BODY_CHARS)
}

/// Truncate on a CHARACTER boundary, never a byte one.
///
/// Slicing a UTF-8 string by byte index panics mid-codepoint; an alert body
/// carrying a non-ASCII character (a symbol name, an em dash from a formatted
/// reason) would take the notification path down at exactly the moment it is
/// needed.
fn truncate_chars(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let keep = limit.saturating_sub(TRUNCATION_MARKER.chars().count());
    let mut out: String = value.chars().take(keep).collect();
    out.push_str(TRUNCATION_MARKER);
    out
}

/// Serialise a flat string map as a JSON object.
///
/// Hand-rolled for the same zero-dependency reason as the base64 encoder. The
/// escaping covers every character JSON requires to be escaped, including the
/// C0 control range that a naive `\"`-only escaper leaves as raw bytes and that
/// would produce a body the relay rejects as malformed.
fn json_object(fields: &[(&str, &str)]) -> String {
    let mut out = String::from("{");
    for (index, (key, value)) in fields.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        out.push_str(&json_string(key));
        out.push(':');
        out.push_str(&json_string(value));
    }
    out.push('}');
    out
}

fn json_string(value: &str) -> String {
    let mut out = String::with_capacity(value.len() + 2);
    out.push('"');
    for c in value.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// Read and parse the HTTP status line.
fn read_status_line(
    reader: &mut BufReader<TcpStream>,
    budget: &SendBudget,
) -> Result<u16, ChannelError> {
    let raw = read_line_budgeted(
        reader,
        budget,
        CHANNEL,
        "response status",
        MAX_STATUS_LINE_BYTES,
    )?;
    let line = String::from_utf8_lossy(&raw).into_owned();
    if raw.is_empty() {
        return Err(ChannelError::TransportUnavailable {
            detail: format!("{CHANNEL}: relay closed the connection before replying"),
        });
    }

    let mut parts = line.split_whitespace();
    let version = parts.next().unwrap_or_default();
    if !version.starts_with("HTTP/") {
        return Err(ChannelError::TransportUnavailable {
            detail: format!(
                "{CHANNEL}: relay reply is not HTTP: {}",
                body_snippet(&line)
            ),
        });
    }
    parts
        .next()
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| ChannelError::TransportUnavailable {
            detail: format!(
                "{CHANNEL}: relay reply has no status code: {}",
                body_snippet(&line)
            ),
        })
}

/// Drain the rest of the response (headers + body) under the budget and the size
/// cap, returning the body.
fn read_to_end_bounded(
    reader: &mut BufReader<TcpStream>,
    budget: &SendBudget,
) -> Result<String, ChannelError> {
    let raw = read_to_end_budgeted(reader, budget, CHANNEL, "response body", MAX_RESPONSE_BYTES)?;
    let text = String::from_utf8_lossy(&raw).into_owned();
    // Split headers from body at the first blank line; if the relay sent no
    // body, the accept id falls back to the status (see `accept_reference`).
    let body = text
        .split_once("\r\n\r\n")
        .or_else(|| text.split_once("\n\n"))
        .map(|(_, body)| body)
        .unwrap_or("");
    Ok(body.trim().to_string())
}

/// The stored accept id: the relay's body, or the status when it sent none.
///
/// Never fabricates an id — an empty body yields an explicit
/// `http-<status>-no-reference` rather than a plausible-looking identifier that
/// an operator would try, and fail, to find in the gateway's own logs.
fn accept_reference(payload: &str, status: u16) -> String {
    let trimmed = payload.trim();
    if trimmed.is_empty() {
        return format!("http-{status}-no-reference");
    }
    truncate_chars(&fold_protocol_line(trimmed), MAX_REFERENCE_CHARS)
}

/// Remove every occurrence of `secret` (bare, and in `Bearer <secret>` form)
/// from relay-controlled text.
///
/// The relay is not trusted merely for being on a private network, and anything
/// derived from its reply is persisted verbatim by the dispatcher.
fn redact_secret(text: &str, secret: &str) -> String {
    if secret.is_empty() {
        return text.to_string();
    }
    text.replace(&format!("Bearer {secret}"), "Bearer <redacted>")
        .replace(secret, "<redacted>")
}

/// A short, single-line excerpt of a relay reply for an error detail.
fn body_snippet(payload: &str) -> String {
    truncate_chars(&fold_protocol_line(payload.trim()), 200)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_escapes_quotes_backslashes_and_control_characters() {
        assert_eq!(json_string("plain"), "\"plain\"");
        assert_eq!(json_string("say \"hi\""), "\"say \\\"hi\\\"\"");
        assert_eq!(json_string("back\\slash"), "\"back\\\\slash\"");
        assert_eq!(json_string("line\nbreak"), "\"line\\nbreak\"");
        assert_eq!(json_string("bell\u{07}"), "\"bell\\u0007\"");
    }

    #[test]
    fn json_object_renders_the_relay_contract() {
        assert_eq!(
            json_object(&[("to", "+15550001111"), ("text", "ATP: down")]),
            "{\"to\":\"+15550001111\",\"text\":\"ATP: down\"}"
        );
    }

    #[test]
    fn the_sms_text_carries_the_subject_because_sms_has_no_subject_line() {
        let message = NotificationMessage::new("CRITICAL: IB connectivity lost", "gateway 4002");
        assert_eq!(
            sms_text(&message),
            "CRITICAL: IB connectivity lost: gateway 4002"
        );
    }

    #[test]
    fn a_long_body_is_truncated_visibly_and_on_a_character_boundary() {
        let message = NotificationMessage::new("ALERT", "é".repeat(1000));
        let text = sms_text(&message);
        assert!(text.chars().count() <= MAX_SMS_BODY_CHARS);
        assert!(text.ends_with(TRUNCATION_MARKER), "{text}");
    }

    #[test]
    fn truncation_never_splits_a_multibyte_character() {
        // Byte-slicing this at the limit would panic mid-codepoint.
        let value = "→".repeat(600);
        let truncated = truncate_chars(&value, MAX_SMS_BODY_CHARS);
        assert!(truncated.chars().count() <= MAX_SMS_BODY_CHARS);
        assert!(truncated.starts_with('→'));
    }

    #[test]
    fn an_empty_relay_body_yields_an_explicit_non_reference() {
        assert_eq!(accept_reference("", 202), "http-202-no-reference");
        assert_eq!(accept_reference("  ", 200), "http-200-no-reference");
        assert_eq!(accept_reference("SM123", 201), "SM123");
    }

    #[test]
    fn a_path_without_a_leading_slash_is_unconfigured() {
        assert!(matches!(
            SmsGatewayConfig::new("127.0.0.1", 8025, "sms", "+15550001111", "key"),
            Err(ChannelError::Unconfigured { .. })
        ));
    }

    #[test]
    fn a_request_splitting_path_or_credential_is_refused() {
        assert!(matches!(
            SmsGatewayConfig::new("127.0.0.1", 8025, "/sms HTTP/1.1\r\nX: y", "+1555", "key"),
            Err(ChannelError::Unconfigured { .. })
        ));
        assert!(matches!(
            SmsGatewayConfig::new("127.0.0.1", 8025, "/sms", "+1555", "key\r\nX-Evil: 1"),
            Err(ChannelError::Unconfigured { .. })
        ));
    }

    #[test]
    fn the_config_debug_impl_never_prints_the_credential() {
        let config = SmsGatewayConfig::new("127.0.0.1", 8025, "/sms", "+15550001111", "shh-secret")
            .expect("config is valid");
        let rendered = format!("{config:?}");
        assert!(!rendered.contains("shh-secret"), "{rendered}");
        assert!(rendered.contains("<redacted>"), "{rendered}");
    }

    #[test]
    fn from_env_without_the_catalogued_secret_is_unconfigured() {
        let result = SmsGatewayConfig::from_env(|key| match key {
            "ATP_OPERATOR_SMS" => Some("+15550001111".into()),
            _ => None,
        });
        assert!(matches!(result, Err(ChannelError::Unconfigured { .. })));
    }
}
