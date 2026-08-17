//! SRS-NOTIF-001 / IF-11 — the operator **push** transport (ntfy).
//!
//! Publishes the alert to a self-hosted [ntfy](https://ntfy.sh) instance on the
//! LAN over plain HTTP/1.1. Unlike the IF-10 email transport there is **no relay
//! hop**: ntfy is reachable directly and needs no TLS, because
//! [`EgressEndpoint`] admits only loopback and RFC 1918 addresses — the operator
//! reaches it from their phone over the VPN, not across the public internet.
//!
//! ## The ntfy contract this speaks
//!
//! `POST /<topic>` with a **plain-text UTF-8** body, an
//! `Authorization: Bearer <ATP_PUSH_TOKEN>` header, and `Connection: close`.
//! A 2xx reply is a JSON object whose `id` field is ntfy's message id; that id
//! becomes the [`ChannelReceipt`] reference.
//!
//! `Connection: close` is deliberate, for the same reason as the email path: it
//! makes "read to end of stream" an exact definition of the response body, so
//! the adapter needs neither chunked-transfer decoding nor trust in a
//! `Content-Length` a broken server might understate.
//!
//! ## Two silent-failure modes, both verified against a real ntfy
//!
//! These are not theoretical; both were reproduced with `curl -v` against
//! `ntfy.sh` and a local `binwiederhier/ntfy` before this module was written.
//!
//! 1. **Oversized messages become file attachments.** ntfy's documentation says
//!    a message "greater than the maximum message size (4,096 bytes)" is turned
//!    into an attachment. The documentation is wrong by one: a body of *exactly*
//!    4,096 bytes already converts. When it converts, the request still returns
//!    **HTTP 200** with a valid message id, but `message` is replaced by
//!    "You received a file: attachment.txt" — so the alert text never reaches the
//!    operator's lock screen while the transport reports success. That makes
//!    [`MAX_PUSH_BODY_BYTES`] a safety property, not a nicety, and it is why this
//!    module also fails a 2xx that came back carrying an attachment
//!    (see [`PushChannel::post`]).
//! 2. **An empty body becomes the literal word "triggered".** ntfy substitutes
//!    its own placeholder. An operator alert must never degrade into a one-word
//!    notification with no statement of what fired, so an empty composition is
//!    replaced here with an explicit marker instead.
//!
//! ## The topic is a credential
//!
//! On ntfy, publish authority is (topic + token) — anyone holding the topic can
//! publish to it, so `ATP_PUSH_TOPIC` is catalogued `secret` exactly like
//! `ATP_PUSH_TOKEN` (NFR-S4) and is scrubbed with the same force. That matters
//! more here than it looks: the topic is interpolated into the **request line**,
//! and ntfy's own success body **echoes it back**. Every string derived from the
//! server's reply is persisted verbatim by the dispatcher, so both secrets are
//! removed from that reply before it can reach a receipt or an error — on the
//! success path too.

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

const CHANNEL: &str = "push";

const DEFAULT_PUSH_HOST: &str = "127.0.0.1";
const DEFAULT_PUSH_PORT: u16 = 80;

/// Cap on the response the adapter will read. A server that streams without end
/// must not be able to exhaust memory on the alert path.
const MAX_RESPONSE_BYTES: usize = 64 * 1024;
/// Cap on the HTTP status line specifically, which is short by construction.
const MAX_STATUS_LINE_BYTES: usize = 8 * 1024;
/// Cap on the message id stored on the delivery record.
const MAX_REFERENCE_CHARS: usize = 256;

/// The size at which ntfy converts a message into a file attachment, measured in
/// **bytes** — verified empirically to be inclusive (4,096 converts), despite the
/// documentation saying "greater than".
const NTFY_ATTACHMENT_THRESHOLD_BYTES: usize = 4096;

/// Hard cap on the push body, in **bytes**, deliberately far below
/// [`NTFY_ATTACHMENT_THRESHOLD_BYTES`].
///
/// Bytes, not characters: the SMS transport this replaced capped *characters*,
/// and 480 characters of multibyte text is up to 1,920 bytes — a cap expressed
/// in the wrong unit cannot bound the quantity ntfy actually measures.
const MAX_PUSH_BODY_BYTES: usize = 1024;

/// Cap on the `X-Title` header value, in bytes.
const MAX_TITLE_BYTES: usize = 200;

const TRUNCATION_MARKER: &str = "…[truncated]";

/// Sent when the composed alert text is empty, so ntfy cannot substitute its own
/// "triggered" placeholder (see the module docs).
const EMPTY_BODY_PLACEHOLDER: &str = "ATP operator alert (no detail supplied)";

/// ntfy priority 5 = "max/urgent": bypasses the phone's do-not-disturb.
///
/// Fixed rather than derived, and that is a deliberate reading of the
/// requirement rather than a missing feature. Every message that reaches this
/// transport is an SRS-NOTIF-001 operator alert, and `NotificationSeverity` on
/// that path is only ever `ERROR` or `CRITICAL` — there is no informational
/// level to de-prioritise. StRS SN-1.12 exists precisely to reach an operator
/// who is not looking, so an alert that a phone silences has failed.
const PUSH_PRIORITY: u8 = 5;

/// Operator configuration for the push transport.
///
/// Carries two secrets: the access token **and** the topic (see the module
/// docs). Both are redacted by the hand-written `Debug` below.
#[derive(Clone, PartialEq, Eq)]
pub struct PushConfig {
    endpoint: EgressEndpoint,
    topic: String,
    token: String,
}

impl core::fmt::Debug for PushConfig {
    /// Redacts BOTH secrets — see [`super::smtp::SmtpRelayConfig`]'s impl for why
    /// this is written by hand rather than derived.
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("PushConfig")
            .field("endpoint", &self.endpoint)
            .field("topic", &"<redacted>")
            .field("token", &"<redacted>")
            .finish()
    }
}

impl PushConfig {
    pub fn new(
        host: impl Into<String>,
        port: u16,
        topic: impl Into<String>,
        token: impl Into<String>,
    ) -> Result<Self, ChannelError> {
        let endpoint = EgressEndpoint::new(host, port, CHANNEL)?;
        let topic = topic.into();
        let token = token.into();

        // NOTE ON EVERY ERROR IN THIS FUNCTION: it names the offending key but
        // never interpolates its VALUE. The transport this replaced wrote
        // `got {path:?}` on a bad path — harmless when the path was the fixed
        // literal "/sms", a credential leak into the persisted audit trail now
        // that the same field carries the topic.
        if topic.trim().is_empty() {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: ATP_PUSH_TOPIC is not configured"),
            });
        }
        if token.trim().is_empty() {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: ATP_PUSH_TOKEN is not configured"),
            });
        }

        // The topic is interpolated into the request line as the URL path. An
        // allow-list (ntfy's own topic alphabet) rather than a deny-list of
        // dangerous characters: whitespace or a control character would split
        // the request line and let the remainder be read as a header or a second
        // request (request smuggling), and `/`, `?` or `#` would silently
        // retarget the publish at a different topic or endpoint. Unlike a
        // message subject, none of that can be repaired by folding.
        if !topic
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        {
            return Err(ChannelError::Unconfigured {
                detail: format!(
                    "{CHANNEL}: ATP_PUSH_TOPIC must be ASCII alphanumeric, '-' or '_' only"
                ),
            });
        }
        // The token is interpolated into a header value.
        if token.chars().any(|c| c.is_control()) {
            return Err(ChannelError::Unconfigured {
                detail: format!("{CHANNEL}: ATP_PUSH_TOKEN contains a control character"),
            });
        }

        Ok(Self {
            endpoint,
            topic,
            token,
        })
    }

    /// Read the configuration from the process environment.
    ///
    /// `ATP_PUSH_TOPIC` and `ATP_PUSH_TOKEN` are both catalogued SRS-SEC-001
    /// secrets (encrypted at rest, redacted from logs); host and port default to
    /// a loopback ntfy.
    pub fn from_env(read: impl Fn(&str) -> Option<String>) -> Result<Self, ChannelError> {
        let host = read("ATP_PUSH_HOST").unwrap_or_else(|| DEFAULT_PUSH_HOST.to_string());
        let port = match read("ATP_PUSH_PORT") {
            Some(raw) => raw
                .trim()
                .parse::<u16>()
                .map_err(|_| ChannelError::Unconfigured {
                    detail: format!("{CHANNEL}: ATP_PUSH_PORT is not a valid port: {raw:?}"),
                })?,
            None => DEFAULT_PUSH_PORT,
        };
        let topic = read("ATP_PUSH_TOPIC").unwrap_or_default();
        let token = read("ATP_PUSH_TOKEN").unwrap_or_default();

        Self::new(host, port, topic, token)
    }
}

/// The IF-11 push channel client.
#[derive(Debug, Clone)]
pub struct PushChannel {
    config: PushConfig,
}

impl PushChannel {
    pub fn new(config: PushConfig) -> Self {
        Self { config }
    }

    fn post(&self, message: &NotificationMessage, budget: &SendBudget) -> ChannelSendResult {
        let body = push_body(message);
        let title = push_title(message);

        // The topic was validated to ntfy's topic alphabet and the token to be
        // control-character free at construction, and `Host` carries the
        // configured host folded, so no request-line or header field can be
        // split here.
        let request = format!(
            "POST /{} HTTP/1.1\r\n\
             Host: {}:{}\r\n\
             Authorization: Bearer {}\r\n\
             Content-Type: text/plain; charset=utf-8\r\n\
             Content-Length: {}\r\n\
             X-Title: {}\r\n\
             X-Priority: {}\r\n\
             Connection: close\r\n\
             \r\n\
             {}",
            self.config.topic,
            fold_protocol_line(self.config.endpoint.host()),
            self.config.endpoint.port(),
            self.config.token,
            body.len(),
            title,
            PUSH_PRIORITY,
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

        let secrets = [self.config.token.as_str(), self.config.topic.as_str()];

        let mut reader = BufReader::new(stream);
        let status = read_status_line(&mut reader, budget, &secrets)?;
        let raw_payload = read_to_end_bounded(&mut reader, budget)?;

        // ORDER MATTERS, AND IT IS THE OPPOSITE OF THE OBVIOUS ONE.
        //
        // Every string that reaches a receipt or an error is PERSISTED by the
        // dispatcher, so both secrets must be scrubbed before that happens —
        // ntfy's 2xx body echoes the topic by design, so on this transport the
        // success path is the one that reliably carries a credential, and a
        // server echoing the Authorization header it was sent (verbose logging,
        // or hostility) would write back a recoverable ATP_PUSH_TOKEN.
        //
        // But scrubbing FIRST and parsing the scrubbed text is wrong, and
        // dangerously so. `redact_secrets` is a blind substring replace and the
        // topic is arbitrary — an operator whose topic is literally
        // `attachment` would have the JSON KEY `"attachment"` rewritten to
        // `"<redacted>"`, and the silent-conversion check below would never
        // fire: an alert that ntfy turned into a file would be recorded as
        // DELIVERED. A topic of `id` corrupts the reference lookup the same way.
        //
        // So structure is read from the RAW payload and only the extracted
        // VALUES are redacted. Free text that is never parsed (error snippets)
        // is still scrubbed wholesale.
        let payload = redact_secrets(&raw_payload, &secrets);

        match status {
            200..=299 => {
                // A 2xx that converted the message into a file is a SILENT
                // failure: the operator's phone shows "You received a file"
                // instead of the alert. MAX_PUSH_BODY_BYTES should make this
                // unreachable, so reaching it means the cap or ntfy's threshold
                // is wrong -- exactly the case that must be loud rather than
                // recorded as a delivery. Read from the RAW payload, per above.
                if json_has_key(&raw_payload, "attachment") {
                    return Err(ChannelError::Rejected {
                        detail: format!(
                            "{CHANNEL}: ntfy converted the alert into a file attachment \
                             (body was {} bytes, threshold {NTFY_ATTACHMENT_THRESHOLD_BYTES}) \
                             — the alert text would not reach the operator",
                            body.len()
                        ),
                    });
                }
                // A 2xx MUST carry a parseable top-level `id`, or it is not a
                // delivery.
                //
                // ntfy always returns one — verified on every 2xx probe against
                // both ntfy.sh and a local instance, attachment conversions
                // included. So a 2xx WITHOUT one did not come from ntfy: an
                // intercepting proxy, a captive portal, or a reverse proxy
                // pointed at the wrong upstream will happily answer 200 with an
                // empty or non-JSON body. Recording that as Delivered would put
                // a false operator page in the durable audit trail — the worst
                // kind of entry, because it reads as proof the operator was
                // reached.
                //
                // The transport this replaced fell back to a synthetic
                // `http-<status>-no-reference` here, which was right for an SMS
                // gateway whose accept genuinely carried no body. It is wrong
                // for ntfy, where the id IS the receipt.
                //
                // Extract from the RAW payload (see above), then scrub the
                // extracted value before it becomes the persisted reference.
                match message_reference(&raw_payload) {
                    Some(reference) => {
                        Ok(ChannelReceipt::new(redact_secrets(&reference, &secrets)))
                    }
                    None => Err(ChannelError::TransportUnavailable {
                        detail: format!(
                            "{CHANNEL}: HTTP {status} carried no ntfy message id — the reply did \
                             not come from ntfy (an intercepting proxy or a wrong upstream), so \
                             the alert cannot be treated as delivered"
                        ),
                    }),
                }
            }
            // 401 is a wrong/revoked token; 403 is a missing token or a token
            // without publish access to this topic. Both were confirmed against
            // a real ntfy. Either way the remediation is to fix configuration,
            // which is what Unconfigured tells the operator to do.
            401 | 403 => Err(ChannelError::Unconfigured {
                detail: format!(
                    "{CHANNEL}: ntfy refused the credential (HTTP {status}) — check \
                     ATP_PUSH_TOKEN and that it has publish access to ATP_PUSH_TOPIC"
                ),
            }),
            // Rate limiting is explicitly transient, and 5xx is a server
            // outage: both clear on their own, unlike a malformed request.
            408 | 429 | 500..=599 => Err(ChannelError::TransportUnavailable {
                detail: format!(
                    "{CHANNEL}: ntfy returned HTTP {status}: {}",
                    body_snippet(&payload)
                ),
            }),
            _ => Err(ChannelError::Rejected {
                detail: format!(
                    "{CHANNEL}: ntfy returned HTTP {status}: {}",
                    body_snippet(&payload)
                ),
            }),
        }
    }
}

impl NotificationChannelClient for PushChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Push
    }

    fn send(&self, message: &NotificationMessage, deadline: Duration) -> ChannelSendResult {
        let budget = SendBudget::start(deadline);
        self.post(message, &budget)
    }
}

/// Build the plain-text push body.
///
/// The subject is prefixed onto the body rather than left to `X-Title` alone: a
/// title is a header, and a header is the part of this request most likely to be
/// dropped, degraded or truncated by anything in the path. Carrying the severity
/// and trigger in the body too means the operator always has a statement of what
/// fired, even if the title never renders.
fn push_body(message: &NotificationMessage) -> String {
    let subject = message.subject().trim();
    let body = message.body().trim();
    let combined = if subject.is_empty() {
        body.to_string()
    } else if body.is_empty() {
        subject.to_string()
    } else {
        format!("{subject}: {body}")
    };
    if combined.is_empty() {
        // Never let ntfy substitute its own "triggered" placeholder.
        return EMPTY_BODY_PLACEHOLDER.to_string();
    }
    truncate_bytes(&combined, MAX_PUSH_BODY_BYTES)
}

/// Build the `X-Title` header value.
///
/// ntfy accepts and round-trips raw UTF-8 here (verified), so the subject is not
/// forced down to ASCII and an alert naming a non-ASCII symbol keeps its title.
/// CR/LF and control characters ARE removed: those would split the header and
/// let the remainder be read as another header or a second request.
fn push_title(message: &NotificationMessage) -> String {
    let folded: String = fold_protocol_line(message.subject().trim())
        .chars()
        .filter(|c| !c.is_control())
        .collect();
    truncate_bytes(folded.trim(), MAX_TITLE_BYTES)
}

/// Truncate to a **byte** budget without splitting a UTF-8 character.
///
/// Two constraints at once. ntfy measures its attachment threshold in bytes, so
/// the limit must be a byte limit; and slicing a UTF-8 string at an arbitrary
/// byte index panics mid-codepoint, so an alert body carrying a non-ASCII
/// character (a symbol name, an em dash from a formatted reason) would take the
/// notification path down at exactly the moment it is needed.
fn truncate_bytes(value: &str, limit: usize) -> String {
    if value.len() <= limit {
        return value.to_string();
    }
    let keep = limit.saturating_sub(TRUNCATION_MARKER.len());
    let mut end = 0;
    for (index, _) in value.char_indices() {
        if index > keep {
            break;
        }
        end = index;
    }
    let mut out = String::with_capacity(limit);
    out.push_str(&value[..end]);
    out.push_str(TRUNCATION_MARKER);
    out
}

/// Read and parse the HTTP status line.
fn read_status_line(
    reader: &mut BufReader<TcpStream>,
    budget: &SendBudget,
    secrets: &[&str],
) -> Result<u16, ChannelError> {
    let raw = read_line_budgeted(
        reader,
        budget,
        CHANNEL,
        "response status",
        MAX_STATUS_LINE_BYTES,
    )?;
    // Redact BEFORE any error is constructed from this line. A malformed status
    // line is server-controlled text that flows straight into a persisted
    // ChannelError detail, so scrubbing it later in `post` is too late.
    let line = redact_secrets(&String::from_utf8_lossy(&raw), secrets);
    if raw.is_empty() {
        return Err(ChannelError::TransportUnavailable {
            detail: format!("{CHANNEL}: ntfy closed the connection before replying"),
        });
    }

    let mut parts = line.split_whitespace();
    let version = parts.next().unwrap_or_default();
    if !version.starts_with("HTTP/") {
        return Err(ChannelError::TransportUnavailable {
            detail: format!("{CHANNEL}: ntfy reply is not HTTP: {}", body_snippet(&line)),
        });
    }
    parts
        .next()
        .and_then(|code| code.parse::<u16>().ok())
        .ok_or_else(|| ChannelError::TransportUnavailable {
            detail: format!(
                "{CHANNEL}: ntfy reply has no status code: {}",
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
    // Split headers from body at the first blank line; if ntfy sent no body, the
    // reference falls back to the status (see `message_reference`).
    let body = text
        .split_once("\r\n\r\n")
        .or_else(|| text.split_once("\n\n"))
        .map(|(_, body)| body)
        .unwrap_or("");
    Ok(body.trim().to_string())
}

/// ntfy's message id, or `None` when the reply carries no usable one.
///
/// Never fabricates: `None` is returned rather than a synthetic placeholder, and
/// the caller turns that into a FAILED delivery. A plausible-looking identifier
/// an operator would try, and fail, to find in ntfy's own logs is worse than no
/// identifier — and on this transport a missing id also means the reply did not
/// come from ntfy at all, which is a failure in its own right.
fn message_reference(payload: &str) -> Option<String> {
    let id = json_string_field(payload, "id")?;
    let trimmed = id.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(truncate_chars(
        &fold_protocol_line(trimmed),
        MAX_REFERENCE_CHARS,
    ))
}

/// Extract a top-level JSON string field.
///
/// Hand-rolled for the same zero-dependency reason as the email transport's
/// base64 encoder. **Depth-aware on purpose**: ntfy nests an `attachment` object
/// inside the reply, so a scanner that took the first `"id"` anywhere in the text
/// would happily read a nested object's field and store the wrong reference. Only
/// a key at object depth 1 is accepted.
fn json_string_field(payload: &str, key: &str) -> Option<String> {
    let bytes: Vec<char> = payload.chars().collect();
    let mut index = 0usize;
    let mut depth = 0usize;

    while index < bytes.len() {
        match bytes[index] {
            '{' => {
                depth += 1;
                index += 1;
            }
            '}' => {
                depth = depth.saturating_sub(1);
                index += 1;
            }
            '[' | ']' => {
                // Arrays cannot hold a bare key, and anything inside one is
                // deeper than the top level either way; step over the bracket
                // and let the depth counter keep tracking objects within it.
                index += 1;
            }
            '"' => {
                let (text, next) = read_json_string(&bytes, index)?;
                index = next;
                // Is this string a KEY (followed by ':') rather than a value?
                let mut probe = index;
                while probe < bytes.len() && bytes[probe].is_whitespace() {
                    probe += 1;
                }
                if probe < bytes.len() && bytes[probe] == ':' {
                    probe += 1;
                    while probe < bytes.len() && bytes[probe].is_whitespace() {
                        probe += 1;
                    }
                    if depth == 1 && text == key {
                        if probe < bytes.len() && bytes[probe] == '"' {
                            return read_json_string(&bytes, probe).map(|(value, _)| value);
                        }
                        // The key exists but its value is not a string.
                        return None;
                    }
                    index = probe;
                }
            }
            _ => index += 1,
        }
    }
    None
}

/// Whether the payload carries a top-level key, whatever its value type.
fn json_has_key(payload: &str, key: &str) -> bool {
    let bytes: Vec<char> = payload.chars().collect();
    let mut index = 0usize;
    let mut depth = 0usize;

    while index < bytes.len() {
        match bytes[index] {
            '{' => {
                depth += 1;
                index += 1;
            }
            '}' => {
                depth = depth.saturating_sub(1);
                index += 1;
            }
            '"' => {
                let Some((text, next)) = read_json_string(&bytes, index) else {
                    return false;
                };
                index = next;
                let mut probe = index;
                while probe < bytes.len() && bytes[probe].is_whitespace() {
                    probe += 1;
                }
                if probe < bytes.len() && bytes[probe] == ':' {
                    if depth == 1 && text == key {
                        return true;
                    }
                    index = probe + 1;
                }
            }
            _ => index += 1,
        }
    }
    false
}

/// Read a JSON string literal starting at the opening quote, honouring escapes.
///
/// Returns the decoded text and the index just past the closing quote. `\uXXXX`
/// is decoded only far enough to keep scanning correct — the escape is consumed
/// so its digits can never be mistaken for structure.
fn read_json_string(chars: &[char], start: usize) -> Option<(String, usize)> {
    debug_assert_eq!(chars.get(start), Some(&'"'));
    let mut out = String::new();
    let mut index = start + 1;
    while index < chars.len() {
        match chars[index] {
            '"' => return Some((out, index + 1)),
            '\\' => {
                let escaped = *chars.get(index + 1)?;
                match escaped {
                    'n' => out.push('\n'),
                    'r' => out.push('\r'),
                    't' => out.push('\t'),
                    'b' => out.push('\u{08}'),
                    'f' => out.push('\u{0c}'),
                    'u' => {
                        let hex: String = chars.get(index + 2..index + 6)?.iter().collect();
                        let code = u32::from_str_radix(&hex, 16).ok()?;
                        out.push(char::from_u32(code).unwrap_or('\u{fffd}'));
                        index += 6;
                        continue;
                    }
                    other => out.push(other),
                }
                index += 2;
            }
            c => {
                out.push(c);
                index += 1;
            }
        }
    }
    // Unterminated string: fail closed rather than returning a partial value.
    None
}

/// Truncate on a CHARACTER boundary, for values already known to be short.
fn truncate_chars(value: &str, limit: usize) -> String {
    if value.chars().count() <= limit {
        return value.to_string();
    }
    let keep = limit.saturating_sub(TRUNCATION_MARKER.chars().count());
    let mut out: String = value.chars().take(keep).collect();
    out.push_str(TRUNCATION_MARKER);
    out
}

/// Remove every occurrence of each secret (bare, and in `Bearer <secret>` form)
/// from server-controlled text.
///
/// ntfy is not trusted merely for being on a private network, and anything
/// derived from its reply is persisted verbatim by the dispatcher.
fn redact_secrets(text: &str, secrets: &[&str]) -> String {
    let mut out = text.to_string();
    for secret in secrets {
        if secret.is_empty() {
            continue;
        }
        out = out
            .replace(&format!("Bearer {secret}"), "Bearer <redacted>")
            .replace(secret, "<redacted>");
    }
    out
}

/// A short, single-line excerpt of an ntfy reply for an error detail.
fn body_snippet(payload: &str) -> String {
    truncate_chars(&fold_protocol_line(payload.trim()), 200)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> PushConfig {
        PushConfig::new("127.0.0.1", 18080, "atp-alerts-topic", "tk_secrettoken")
            .expect("config is valid")
    }

    #[test]
    fn the_body_carries_the_subject_because_a_header_can_be_dropped() {
        let message = NotificationMessage::new("CRITICAL: IB connectivity lost", "gateway 4002");
        assert_eq!(
            push_body(&message),
            "CRITICAL: IB connectivity lost: gateway 4002"
        );
    }

    #[test]
    fn an_empty_message_never_degrades_into_ntfys_triggered_placeholder() {
        // ntfy substitutes the literal word "triggered" for an empty body
        // (verified against a real server), which would page the operator with
        // no statement of what fired.
        let message = NotificationMessage::new("   ", "  ");
        assert_eq!(push_body(&message), EMPTY_BODY_PLACEHOLDER);
        assert!(!push_body(&message).is_empty());
    }

    #[test]
    fn the_body_is_capped_in_bytes_far_below_the_attachment_threshold() {
        let message = NotificationMessage::new("ALERT", "é".repeat(4000));
        let body = push_body(&message);
        assert!(
            body.len() <= MAX_PUSH_BODY_BYTES,
            "body was {} bytes",
            body.len()
        );
        assert!(body.len() < NTFY_ATTACHMENT_THRESHOLD_BYTES);
        assert!(body.ends_with(TRUNCATION_MARKER), "{body}");
    }

    /// The cap must be measured in BYTES, not characters.
    ///
    /// This input is chosen to sit in the only band where the two units
    /// disagree: 700 'é' is 700 characters but 1,400 bytes. A character-counting
    /// cap of the same number waves it straight through at 1,400 bytes; a byte
    /// cap truncates it. Anything much longer (4,000 characters, say) exceeds
    /// BOTH limits and so passes under either implementation — which is why the
    /// test above cannot catch this on its own, and why this case is separate.
    ///
    /// Verified discriminating: swapping `truncate_bytes`' guard to
    /// `value.chars().count()` fails this test and no other in the workspace.
    #[test]
    fn a_character_cap_would_not_bound_what_ntfy_actually_measures() {
        let multibyte = "é".repeat(700);
        assert_eq!(multibyte.chars().count(), 700, "under a char cap of 1024");
        assert_eq!(multibyte.len(), 1400, "but over the 1024-BYTE cap");

        let body = push_body(&NotificationMessage::new("", &multibyte));
        assert!(
            body.len() <= MAX_PUSH_BODY_BYTES,
            "body was {} bytes — the cap is counting characters, not bytes",
            body.len()
        );
        assert!(body.ends_with(TRUNCATION_MARKER), "{body}");
    }

    #[test]
    fn truncation_never_splits_a_multibyte_character() {
        // Byte-slicing this at the limit would panic mid-codepoint.
        let value = "→".repeat(2000);
        let truncated = truncate_bytes(&value, MAX_PUSH_BODY_BYTES);
        assert!(truncated.len() <= MAX_PUSH_BODY_BYTES);
        assert!(truncated.starts_with('→'));
        assert!(truncated.ends_with(TRUNCATION_MARKER));
    }

    #[test]
    fn a_title_can_not_inject_a_header_or_a_second_request() {
        let message =
            NotificationMessage::new("ALERT\r\nX-Priority: 1\r\n\r\nPOST /other HTTP/1.1", "body");
        let title = push_title(&message);
        assert!(!title.contains('\r'), "{title}");
        assert!(!title.contains('\n'), "{title}");
    }

    #[test]
    fn a_title_keeps_non_ascii_because_ntfy_round_trips_utf8() {
        let message = NotificationMessage::new("CRITICAL — café ↑", "body");
        assert_eq!(push_title(&message), "CRITICAL — café ↑");
    }

    #[test]
    fn the_message_id_is_read_from_the_real_ntfy_success_body() {
        let payload = r#"{"id":"b19RmMeCXTYy","time":1786939980,"expires":1786983180,"event":"message","topic":"atp-alerts-topic","message":"ATP probe"}"#;
        assert_eq!(message_reference(payload).as_deref(), Some("b19RmMeCXTYy"));
    }

    #[test]
    fn a_nested_id_is_never_mistaken_for_the_message_id() {
        // ntfy nests an `attachment` object in the reply. A scanner that took
        // the first "id" anywhere would store the wrong reference.
        let payload =
            r#"{"event":"message","attachment":{"id":"WRONG","name":"a.txt"},"id":"RIGHT"}"#;
        assert_eq!(json_string_field(payload, "id").as_deref(), Some("RIGHT"));
    }

    /// A reply with no usable id yields None — which the caller turns into a
    /// FAILED delivery, not a synthetic reference.
    ///
    /// TIGHTENED (adversarial review round 4): this used to assert a
    /// `http-<status>-no-reference` fallback that was then stored as a
    /// DELIVERED receipt. ntfy always returns an id, so a 2xx without one did
    /// not come from ntfy — an intercepting proxy or a wrong upstream answering
    /// 200 would have been recorded as a successful operator page.
    #[test]
    fn a_body_without_a_usable_id_is_not_a_reference_at_all() {
        assert_eq!(message_reference(""), None);
        assert_eq!(message_reference("not json at all"), None);
        assert_eq!(message_reference(r#"{"id":""}"#), None);
        assert_eq!(message_reference(r#"{"id":"   "}"#), None);
        // An unterminated string must fail closed, not return a partial id.
        assert_eq!(message_reference(r#"{"id":"trunca"#), None);
        // And the happy path still reads the id.
        assert_eq!(
            message_reference(r#"{"id":"b19RmMeCXTYy"}"#).as_deref(),
            Some("b19RmMeCXTYy")
        );
    }

    #[test]
    fn an_attachment_reply_is_detected_so_a_silent_conversion_can_not_pass() {
        let converted = r#"{"id":"KKGUUf398qAB","event":"message","topic":"t","message":"You received a file: attachment.txt","attachment":{"name":"attachment.txt","size":4097}}"#;
        assert!(json_has_key(converted, "attachment"));
        let normal = r#"{"id":"KKGUUf398qAB","event":"message","message":"real alert"}"#;
        assert!(!json_has_key(normal, "attachment"));
    }

    #[test]
    fn a_nested_attachment_key_does_not_trip_the_top_level_detector() {
        let nested = r#"{"id":"x","meta":{"attachment":"mentioned"}}"#;
        assert!(!json_has_key(nested, "attachment"));
    }

    #[test]
    fn both_the_token_and_the_topic_are_scrubbed_from_server_text() {
        // ntfy's success body ECHOES the topic, so the happy path is the one
        // that reliably carries a credential on this transport.
        let echoed = "{\"topic\":\"atp-alerts-topic\",\"auth\":\"Bearer tk_secrettoken\"}";
        let scrubbed = redact_secrets(&echoed, &["tk_secrettoken", "atp-alerts-topic"]);
        assert!(!scrubbed.contains("tk_secrettoken"), "{scrubbed}");
        assert!(!scrubbed.contains("atp-alerts-topic"), "{scrubbed}");
        assert!(scrubbed.contains("Bearer <redacted>"), "{scrubbed}");
    }

    #[test]
    fn the_config_debug_impl_prints_neither_the_token_nor_the_topic() {
        let rendered = format!("{:?}", config());
        assert!(!rendered.contains("tk_secrettoken"), "{rendered}");
        assert!(!rendered.contains("atp-alerts-topic"), "{rendered}");
        assert_eq!(rendered.matches("<redacted>").count(), 2, "{rendered}");
    }

    #[test]
    fn a_topic_outside_ntfys_alphabet_is_unconfigured_and_is_not_echoed() {
        // The rejection must name the KEY, never the VALUE: this field is a
        // credential, and the detail is persisted to the audit trail.
        for bad in [
            "topic with spaces",
            "topic/with/slash",
            "topic?query",
            "topic\r\nX-Evil: 1",
            "topic#frag",
        ] {
            let error = PushConfig::new("127.0.0.1", 18080, bad, "tk_x")
                .expect_err("topic must be refused");
            let ChannelError::Unconfigured { detail } = error else {
                panic!("expected Unconfigured for {bad:?}");
            };
            assert!(detail.contains("ATP_PUSH_TOPIC"), "{detail}");
            assert!(
                !detail.contains(bad),
                "topic leaked into the detail: {detail}"
            );
        }
    }

    #[test]
    fn a_blank_topic_or_token_is_unconfigured() {
        assert!(matches!(
            PushConfig::new("127.0.0.1", 18080, "  ", "tk_x"),
            Err(ChannelError::Unconfigured { .. })
        ));
        assert!(matches!(
            PushConfig::new("127.0.0.1", 18080, "topic", "  "),
            Err(ChannelError::Unconfigured { .. })
        ));
    }

    #[test]
    fn a_header_splitting_token_is_refused() {
        assert!(matches!(
            PushConfig::new("127.0.0.1", 18080, "topic", "tk_x\r\nX-Evil: 1"),
            Err(ChannelError::Unconfigured { .. })
        ));
    }

    #[test]
    fn from_env_without_the_catalogued_secrets_is_unconfigured() {
        let result = PushConfig::from_env(|key| match key {
            "ATP_PUSH_HOST" => Some("127.0.0.1".into()),
            _ => None,
        });
        assert!(matches!(result, Err(ChannelError::Unconfigured { .. })));
    }

    #[test]
    fn from_env_reads_the_catalogued_keys() {
        let config = PushConfig::from_env(|key| match key {
            "ATP_PUSH_HOST" => Some("127.0.0.1".into()),
            "ATP_PUSH_PORT" => Some("18080".into()),
            "ATP_PUSH_TOPIC" => Some("atp-alerts-topic".into()),
            "ATP_PUSH_TOKEN" => Some("tk_secrettoken".into()),
            _ => None,
        })
        .expect("config is valid");
        assert_eq!(config.endpoint.port(), 18080);
    }
}
