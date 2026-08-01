//! SRS-NOTIF-001 / IF-10 — the operator **email** transport.
//!
//! Speaks RFC 5321 SMTP submission to the local `phase1-notification-egress`
//! relay, which owns the authenticated TLS session to the real provider (see the
//! [module docs](super) for why the TLS boundary is a deployment component).
//!
//! What this transport is responsible for, and what it deliberately is not:
//!
//! * **Is:** a correct, bounded, authenticated SMTP conversation whose every
//!   socket operation is armed from the *remaining* per-channel budget, whose
//!   reply parsing handles multiline responses, and whose reply codes map onto
//!   the [`ChannelError`] taxonomy by their real meaning (4xx transient, 5xx
//!   permanent).
//! * **Is not:** proof of end-user receipt. The [`ChannelReceipt`] carries the
//!   relay's queue id from the final `250`, which is a hand-off acknowledgement —
//!   the same limit [`atp_notification::ChannelReceipt`] already documents.

use std::io::{BufReader, Write};
use std::net::TcpStream;
use std::time::Duration;

use atp_notification::{
    ChannelError, ChannelReceipt, ChannelSendResult, NotificationChannel,
    NotificationChannelClient, NotificationMessage,
};

use super::{
    arm_socket, fold_protocol_line, io_error_to_channel_error, read_line_budgeted, EgressEndpoint,
    SendBudget,
};

/// Channel label used in every error detail, so an operator reading a stored
/// delivery record knows which transport produced it.
const CHANNEL: &str = "email";

/// Default relay host — the compose service name of the egress sidecar.
const DEFAULT_RELAY_HOST: &str = "phase1-notification-egress";
/// Default relay submission port on the internal network.
const DEFAULT_RELAY_PORT: u16 = 1025;

/// Cap on a single SMTP reply line. A hostile or broken relay must not be able to
/// make the adapter allocate without bound; the longest legitimate reply line in
/// this conversation is an EHLO capability line, orders of magnitude below this.
const MAX_REPLY_LINE_BYTES: usize = 4096;
/// Cap on continuation lines in one reply, for the same reason.
const MAX_REPLY_LINES: usize = 64;

/// Operator configuration for the email transport.
///
/// The API key is the credential for the **relay**, not for the upstream
/// provider: the relay maps it onto the real provider secret, so this process
/// never holds a provider credential (NFR-S4). It is stored here and never
/// logged — no `Debug` derive reaches it (see the manual impl below).
#[derive(Clone, PartialEq, Eq)]
pub struct SmtpRelayConfig {
    endpoint: EgressEndpoint,
    sender: String,
    recipient: String,
    username: String,
    api_key: String,
}

impl core::fmt::Debug for SmtpRelayConfig {
    /// Redacts the credential. A `#[derive(Debug)]` here would put
    /// `ATP_SMTP_API_KEY` into any log line, panic message, or test failure that
    /// formats the config — the exact NFR-S4 leak the core avoids by never
    /// holding the key at all.
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        f.debug_struct("SmtpRelayConfig")
            .field("endpoint", &self.endpoint)
            .field("sender", &self.sender)
            .field("recipient", &self.recipient)
            .field("username", &self.username)
            .field("api_key", &"<redacted>")
            .finish()
    }
}

impl SmtpRelayConfig {
    /// Build a validated configuration.
    ///
    /// Every missing piece is [`ChannelError::Unconfigured`] — an operator setup
    /// fault that fails closed *before* any socket work, and that the dispatcher
    /// records as a distinct remediation from a provider outage.
    pub fn new(
        host: impl Into<String>,
        port: u16,
        sender: impl Into<String>,
        recipient: impl Into<String>,
        username: impl Into<String>,
        api_key: impl Into<String>,
    ) -> Result<Self, ChannelError> {
        let endpoint = EgressEndpoint::new(host, port, CHANNEL)?;
        let sender = sender.into();
        let recipient = recipient.into();
        let username = username.into();
        let api_key = api_key.into();

        for (label, value) in [
            ("sender address", &sender),
            ("recipient address", &recipient),
            ("relay username", &username),
            ("relay credential (ATP_SMTP_API_KEY)", &api_key),
        ] {
            if value.trim().is_empty() {
                return Err(ChannelError::Unconfigured {
                    detail: format!("{CHANNEL}: {label} is not configured"),
                });
            }
        }

        // An address carrying CR/LF or the SMTP command terminator would break out
        // of the `MAIL FROM:<...>` / `RCPT TO:<...>` envelope command. Unlike the
        // subject (which is folded so the alert still goes out), a malformed
        // address cannot be repaired into a correct one — refuse it.
        for (label, value) in [
            ("sender address", &sender),
            ("recipient address", &recipient),
        ] {
            if value.contains(['\r', '\n', '<', '>']) {
                return Err(ChannelError::Unconfigured {
                    detail: format!(
                        "{CHANNEL}: {label} contains a character that cannot appear in an SMTP \
                         envelope command"
                    ),
                });
            }
        }

        Ok(Self {
            endpoint,
            sender,
            recipient,
            username,
            api_key,
        })
    }

    /// Read the configuration from the process environment.
    ///
    /// `ATP_SMTP_API_KEY` is the catalogued SRS-SEC-001 secret (encrypted at
    /// rest, supplied through the vault mount). The remaining knobs are
    /// non-secret deployment wiring and default to the compose sidecar, so a
    /// standard deployment configures exactly one value: the key.
    pub fn from_env(read: impl Fn(&str) -> Option<String>) -> Result<Self, ChannelError> {
        let host = read("ATP_SMTP_RELAY_HOST").unwrap_or_else(|| DEFAULT_RELAY_HOST.to_string());
        let port = match read("ATP_SMTP_RELAY_PORT") {
            Some(raw) => raw
                .trim()
                .parse::<u16>()
                .map_err(|_| ChannelError::Unconfigured {
                    detail: format!("{CHANNEL}: ATP_SMTP_RELAY_PORT is not a valid port: {raw:?}"),
                })?,
            None => DEFAULT_RELAY_PORT,
        };
        let sender = read("ATP_SMTP_SENDER").unwrap_or_default();
        let recipient = read("ATP_OPERATOR_EMAIL").unwrap_or_default();
        // The relay login defaults to the sender address, the usual SMTP
        // submission convention; an operator whose relay wants a distinct login
        // sets it explicitly.
        let username = read("ATP_SMTP_RELAY_USER").unwrap_or_else(|| sender.clone());
        let api_key = read("ATP_SMTP_API_KEY").unwrap_or_default();

        Self::new(host, port, sender, recipient, username, api_key)
    }
}

/// The IF-10 email channel client.
#[derive(Debug, Clone)]
pub struct SmtpEmailChannel {
    config: SmtpRelayConfig,
}

impl SmtpEmailChannel {
    pub fn new(config: SmtpRelayConfig) -> Self {
        Self { config }
    }

    /// Run one SMTP submission inside `budget`.
    fn submit(&self, message: &NotificationMessage, budget: &SendBudget) -> ChannelSendResult {
        // The AUTH PLAIN payload is computed BEFORE the session so both it and
        // the raw key can be registered as redactions covering the whole
        // conversation — a relay that echoes either at any stage is scrubbed, not
        // just one that echoes it in the AUTH reply.
        let credential = encode_base64(
            format!("\0{}\0{}", self.config.username, self.config.api_key).as_bytes(),
        );

        let stream = self.config.endpoint.connect(budget, CHANNEL)?;
        let mut session = SmtpSession {
            reader: BufReader::new(stream),
            redactions: vec![credential.clone(), self.config.api_key.clone()],
        };

        // Greeting.
        session.expect(budget, "greeting", &[220])?;

        // EHLO: the capability list decides whether the relay authenticates.
        let ehlo = session.command(budget, "EHLO atp-notification", "EHLO", &[250])?;
        if !ehlo.advertises("AUTH") {
            return Err(ChannelError::Unconfigured {
                detail: format!(
                    "{CHANNEL}: relay {}:{} does not advertise AUTH — refusing to submit \
                     operator alerts through an unauthenticated relay, which any container that \
                     can route to it could also use",
                    self.config.endpoint.host(),
                    self.config.endpoint.port()
                ),
            });
        }

        // AUTH PLAIN. Cleartext is acceptable ONLY because `EgressEndpoint`
        // has already proven this hop terminates on loopback / RFC 1918.
        //
        // A rejected AUTH reports its code and stage only: the relay's reply text
        // is withheld because that is exactly where an echoing relay would put
        // the credential we just sent it, and the dispatcher PERSISTS these
        // details. Pinned by `an_echoing_relay_cannot_get_the_credential_into_the_
        // stored_error`.
        session.command(
            budget,
            &format!("AUTH PLAIN {credential}"),
            AUTH_STAGE,
            &[235],
        )?;

        session.command(
            budget,
            &format!("MAIL FROM:<{}>", self.config.sender),
            "MAIL FROM",
            &[250],
        )?;
        session.command(
            budget,
            &format!("RCPT TO:<{}>", self.config.recipient),
            "RCPT TO",
            &[250, 251],
        )?;
        session.command(budget, "DATA", "DATA", &[354])?;

        let payload = self.render_message(message);
        session.write_line(budget, &payload, "message body")?;
        let accepted = session.command(budget, ".", "end-of-data", &[250])?;

        // The message is accepted at this point. QUIT is courtesy: a failure to
        // send it, or a budget that runs out during it, must NOT turn a delivered
        // alert into a reported failure.
        let _ = session.write_line(budget, "QUIT", "QUIT");

        Ok(ChannelReceipt::new(accepted.text()))
    }

    /// Build the RFC 5322 message.
    ///
    /// `Date` and `Message-ID` are deliberately omitted: RFC 6409 §8.2/§8.3 makes
    /// adding them the *submission server's* job when absent, and generating them
    /// here would mean formatting a wall-clock timestamp inside an adapter whose
    /// correctness is otherwise clock-free.
    fn render_message(&self, message: &NotificationMessage) -> String {
        let mut rendered = String::new();
        rendered.push_str(&format!("From: <{}>\r\n", self.config.sender));
        rendered.push_str(&format!("To: <{}>\r\n", self.config.recipient));
        rendered.push_str(&format!(
            "Subject: {}\r\n",
            fold_protocol_line(message.subject())
        ));
        rendered.push_str("MIME-Version: 1.0\r\n");
        rendered.push_str("Content-Type: text/plain; charset=utf-8\r\n");
        rendered.push_str("\r\n");
        rendered.push_str(&dot_stuff(message.body()));
        rendered
    }
}

impl NotificationChannelClient for SmtpEmailChannel {
    fn channel(&self) -> NotificationChannel {
        NotificationChannel::Email
    }

    fn send(&self, message: &NotificationMessage, deadline: Duration) -> ChannelSendResult {
        let budget = SendBudget::start(deadline);
        self.submit(message, &budget)
    }
}

/// One SMTP reply: its code and the joined human text.
#[derive(Debug, Clone, PartialEq, Eq)]
struct SmtpReply {
    code: u16,
    lines: Vec<String>,
}

impl SmtpReply {
    fn text(&self) -> String {
        self.lines.join(" ")
    }

    /// Whether the EHLO capability list contains `keyword`.
    ///
    /// Matches on the capability TOKEN, not a substring of the whole reply: a
    /// relay whose greeting text merely mentions the word (`250-relay.example.com
    /// says AUTH is disabled`) must not be read as advertising it.
    fn advertises(&self, keyword: &str) -> bool {
        self.lines.iter().any(|line| {
            line.split_whitespace()
                .next()
                .is_some_and(|token| token.eq_ignore_ascii_case(keyword))
        })
    }
}

/// The stage label for the credential-bearing command. Its reply is never
/// quoted back — see [`SmtpSession::expect`].
const AUTH_STAGE: &str = "AUTH PLAIN";

struct SmtpSession {
    reader: BufReader<TcpStream>,
    /// Strings that must never appear in an error detail: the raw credential and
    /// its base64 AUTH PLAIN encoding.
    ///
    /// A relay is not trusted just because it is on a private network. A hostile
    /// or broken one can echo whatever it likes in a reply, and every reply we
    /// reject is interpolated into a `ChannelError` detail that the dispatcher
    /// **persists** to the durable notification store. Without this, an echoing
    /// relay writes a recoverable `ATP_SMTP_API_KEY` into the operator's audit
    /// trail on every auth failure — a file that exists precisely to be kept and
    /// read later.
    redactions: Vec<String>,
}

impl SmtpSession {
    /// Replace any credential material a relay echoed back at us.
    ///
    /// Defence in depth alongside the AUTH-stage rule below: this covers a relay
    /// that echoes the credential at some *other* stage, which the stage rule
    /// alone would not catch.
    fn redact(&self, text: String) -> String {
        let mut text = text;
        for secret in &self.redactions {
            if !secret.is_empty() && text.contains(secret.as_str()) {
                text = text.replace(secret.as_str(), "<redacted>");
            }
        }
        text
    }

    /// Write one CRLF-terminated line, arming the socket from the live budget.
    fn write_line(
        &mut self,
        budget: &SendBudget,
        line: &str,
        stage: &str,
    ) -> Result<(), ChannelError> {
        arm_socket(self.reader.get_ref(), budget, CHANNEL, stage)?;
        let mut socket: &TcpStream = self.reader.get_ref();
        socket
            .write_all(format!("{line}\r\n").as_bytes())
            .and_then(|()| socket.flush())
            .map_err(|err| io_error_to_channel_error(&err, CHANNEL, stage))
    }

    /// Read one (possibly multiline) reply and require one of `accepted`.
    fn expect(
        &mut self,
        budget: &SendBudget,
        stage: &str,
        accepted: &[u16],
    ) -> Result<SmtpReply, ChannelError> {
        let reply = self.read_reply(budget, stage)?;
        if accepted.contains(&reply.code) {
            return Ok(reply);
        }

        // The AUTH reply is the one we just handed the credential to, so its text
        // is the most likely place to find it echoed. Report the code and stage
        // only — never the relay's words. An operator debugging a rejected login
        // needs to know the login was rejected, not what the relay said about it.
        if stage == AUTH_STAGE {
            let detail = format!(
                "{CHANNEL}: relay rejected the credential with {} during {stage} (reply text \
                 withheld: it can echo the credential)",
                reply.code
            );
            return Err(match reply.code {
                400..=499 => ChannelError::TransportUnavailable { detail },
                _ => ChannelError::Rejected { detail },
            });
        }

        Err(redact_channel_error(classify_reply(&reply, stage), self))
    }

    /// Send a command and require one of `accepted` in reply.
    fn command(
        &mut self,
        budget: &SendBudget,
        line: &str,
        stage: &str,
        accepted: &[u16],
    ) -> Result<SmtpReply, ChannelError> {
        self.write_line(budget, line, stage)?;
        self.expect(budget, stage, accepted)
    }

    /// Read a complete reply, following `NNN-` continuation lines.
    fn read_reply(&mut self, budget: &SendBudget, stage: &str) -> Result<SmtpReply, ChannelError> {
        let mut lines: Vec<String> = Vec::new();
        let mut code: Option<u16> = None;

        loop {
            if lines.len() >= MAX_REPLY_LINES {
                return Err(ChannelError::TransportUnavailable {
                    detail: format!(
                        "{CHANNEL}: relay sent more than {MAX_REPLY_LINES} continuation lines \
                         during {stage}"
                    ),
                });
            }
            let raw = read_line_budgeted(
                &mut self.reader,
                budget,
                CHANNEL,
                stage,
                MAX_REPLY_LINE_BYTES,
            )?;
            let line = String::from_utf8_lossy(&raw).into_owned();
            if raw.is_empty() {
                return Err(ChannelError::TransportUnavailable {
                    detail: format!("{CHANNEL}: relay closed the connection during {stage}"),
                });
            }

            let trimmed = line.trim_end_matches(['\r', '\n']);
            if trimmed.len() < 3 {
                return Err(ChannelError::TransportUnavailable {
                    detail: format!("{CHANNEL}: malformed reply during {stage}"),
                });
            }
            let (digits, rest) = trimmed.split_at(3);
            let parsed = digits
                .parse::<u16>()
                .map_err(|_| ChannelError::TransportUnavailable {
                    detail: format!("{CHANNEL}: reply during {stage} has no status code"),
                })?;

            // Every line of one reply must carry the SAME code (RFC 5321 §4.2.1).
            // A relay that changes it mid-reply is desynchronised, and accepting
            // the last line's code would let a `550` be reported as the `250`
            // that followed it.
            match code {
                None => code = Some(parsed),
                Some(first) if first != parsed => {
                    return Err(ChannelError::TransportUnavailable {
                        detail: format!(
                            "{CHANNEL}: reply during {stage} changed status code {first} -> \
                             {parsed} mid-reply"
                        ),
                    });
                }
                Some(_) => {}
            }

            lines.push(rest.trim_start_matches([' ', '-']).to_string());

            match rest.chars().next() {
                Some('-') => continue,
                None | Some(' ') => break,
                Some(_) => {
                    return Err(ChannelError::TransportUnavailable {
                        detail: format!("{CHANNEL}: malformed reply separator during {stage}"),
                    });
                }
            }
        }

        Ok(SmtpReply {
            code: code.unwrap_or_default(),
            lines,
        })
    }
}

/// Run a [`ChannelError`]'s detail through the session's redactions, preserving
/// the variant (each one is a distinct operator remediation).
fn redact_channel_error(error: ChannelError, session: &SmtpSession) -> ChannelError {
    match error {
        ChannelError::Unconfigured { detail } => ChannelError::Unconfigured {
            detail: session.redact(detail),
        },
        ChannelError::TransportUnavailable { detail } => ChannelError::TransportUnavailable {
            detail: session.redact(detail),
        },
        ChannelError::Timeout { detail } => ChannelError::Timeout {
            detail: session.redact(detail),
        },
        ChannelError::Rejected { detail } => ChannelError::Rejected {
            detail: session.redact(detail),
        },
    }
}

/// Map an unexpected reply code onto the taxonomy by its RFC 5321 class.
///
/// 4xx is a transient negative completion the provider expects to be retried;
/// 5xx is permanent. Collapsing both into one error would tell the operator to
/// wait out a failure that will never clear.
fn classify_reply(reply: &SmtpReply, stage: &str) -> ChannelError {
    let detail = format!(
        "{CHANNEL}: relay replied {} during {stage}: {}",
        reply.code,
        reply.text()
    );
    match reply.code {
        400..=499 => ChannelError::TransportUnavailable { detail },
        500..=599 => ChannelError::Rejected { detail },
        _ => ChannelError::TransportUnavailable { detail },
    }
}

/// Apply RFC 5321 §4.5.2 transparency and normalise line endings.
///
/// A body line consisting of a single `.` is the end-of-data marker; without
/// stuffing, an alert body containing one would truncate the message at that
/// point and the operator would read a *partial* alert as a complete one.
fn dot_stuff(body: &str) -> String {
    let mut out = String::with_capacity(body.len() + 16);
    for line in body.split('\n') {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if line.starts_with('.') {
            out.push('.');
        }
        out.push_str(line);
        out.push_str("\r\n");
    }
    out
}

/// Minimal RFC 4648 base64 for the AUTH PLAIN payload.
///
/// Hand-rolled because the workspace carries no external crates; the input is a
/// short, known-shape credential blob.
fn encode_base64(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3f) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3f) as usize] as char);
        out.push(if chunk.len() > 1 {
            ALPHABET[((triple >> 6) & 0x3f) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            ALPHABET[(triple & 0x3f) as usize] as char
        } else {
            '='
        });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64_matches_rfc_4648_vectors() {
        assert_eq!(encode_base64(b""), "");
        assert_eq!(encode_base64(b"f"), "Zg==");
        assert_eq!(encode_base64(b"fo"), "Zm8=");
        assert_eq!(encode_base64(b"foo"), "Zm9v");
        assert_eq!(encode_base64(b"foob"), "Zm9vYg==");
        assert_eq!(encode_base64(b"fooba"), "Zm9vYmE=");
        assert_eq!(encode_base64(b"foobar"), "Zm9vYmFy");
        assert_eq!(encode_base64(b"\0user\0secret"), "AHVzZXIAc2VjcmV0");
    }

    #[test]
    fn a_lone_dot_line_is_stuffed_so_the_body_cannot_terminate_early() {
        assert_eq!(dot_stuff("before\n.\nafter"), "before\r\n..\r\nafter\r\n");
        assert_eq!(dot_stuff(".hidden"), "..hidden\r\n");
        assert_eq!(dot_stuff("plain"), "plain\r\n");
    }

    #[test]
    fn crlf_bodies_are_not_double_terminated() {
        assert_eq!(dot_stuff("one\r\ntwo"), "one\r\ntwo\r\n");
    }

    #[test]
    fn capability_detection_matches_tokens_not_substrings() {
        let advertising = SmtpReply {
            code: 250,
            lines: vec!["relay.internal".into(), "AUTH PLAIN LOGIN".into()],
        };
        assert!(advertising.advertises("AUTH"));

        let merely_mentioning = SmtpReply {
            code: 250,
            lines: vec!["relay.internal".into(), "SIZE 10240000".into()],
        };
        assert!(!merely_mentioning.advertises("AUTH"));
    }

    #[test]
    fn transient_and_permanent_reply_codes_map_to_different_remediations() {
        let transient = SmtpReply {
            code: 451,
            lines: vec!["try later".into()],
        };
        assert!(matches!(
            classify_reply(&transient, "MAIL FROM"),
            ChannelError::TransportUnavailable { .. }
        ));

        let permanent = SmtpReply {
            code: 550,
            lines: vec!["no such user".into()],
        };
        assert!(matches!(
            classify_reply(&permanent, "RCPT TO"),
            ChannelError::Rejected { .. }
        ));
    }

    #[test]
    fn a_missing_credential_is_unconfigured_before_any_socket_work() {
        let result = SmtpRelayConfig::new(
            "127.0.0.1",
            1025,
            "atp@example.com",
            "operator@example.com",
            "atp@example.com",
            "   ",
        );
        assert!(matches!(result, Err(ChannelError::Unconfigured { .. })));
    }

    #[test]
    fn an_envelope_breaking_address_is_refused() {
        let result = SmtpRelayConfig::new(
            "127.0.0.1",
            1025,
            "atp@example.com\r\nRCPT TO:<attacker@example.com>",
            "operator@example.com",
            "atp@example.com",
            "key",
        );
        assert!(matches!(result, Err(ChannelError::Unconfigured { .. })));
    }

    #[test]
    fn the_config_debug_impl_never_prints_the_credential() {
        let config = SmtpRelayConfig::new(
            "127.0.0.1",
            1025,
            "atp@example.com",
            "operator@example.com",
            "atp@example.com",
            "super-secret-key",
        )
        .expect("config is valid");
        let rendered = format!("{config:?}");
        assert!(!rendered.contains("super-secret-key"), "{rendered}");
        assert!(rendered.contains("<redacted>"), "{rendered}");
    }

    #[test]
    fn from_env_defaults_to_the_compose_sidecar() {
        let config = SmtpRelayConfig::from_env(|key| match key {
            "ATP_SMTP_SENDER" => Some("atp@example.com".into()),
            "ATP_OPERATOR_EMAIL" => Some("operator@example.com".into()),
            "ATP_SMTP_API_KEY" => Some("key".into()),
            _ => None,
        })
        .expect("config is valid");
        assert_eq!(config.endpoint.host(), DEFAULT_RELAY_HOST);
        assert_eq!(config.endpoint.port(), DEFAULT_RELAY_PORT);
        assert_eq!(config.username, "atp@example.com");
    }

    #[test]
    fn from_env_without_the_catalogued_secret_is_unconfigured() {
        let result = SmtpRelayConfig::from_env(|key| match key {
            "ATP_SMTP_SENDER" => Some("atp@example.com".into()),
            "ATP_OPERATOR_EMAIL" => Some("operator@example.com".into()),
            _ => None,
        });
        assert!(matches!(result, Err(ChannelError::Unconfigured { .. })));
    }
}
