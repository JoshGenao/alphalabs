//! SRS-NOTIF-001 operator-notification transports (IF-10 email, IF-11 SMS).
//!
//! The core dispatcher ([`atp_notification::OperatorNotifier`]) fans an operator
//! alert out over the two REQUIRED channels through the
//! [`atp_notification::NotificationChannelClient`] port. This module holds the
//! concrete transports behind that port, which AGENTS.md requires to live in the
//! adapter crate: the core names no vendor and holds no provider credential.
//!
//! ## Why both transports speak to a local egress relay
//!
//! Every real provider (SMTP submission, an SMS gateway REST API) requires TLS,
//! and the ATP Rust workspace carries **zero external crates** — there is no TLS
//! implementation available to a std-only adapter, and adding one would break the
//! workspace-wide zero-dependency invariant for the entire tree.
//!
//! So the TLS boundary is a deployment component, not a library: the
//! `phase1-notification-egress` service owns the authenticated TLS session to the
//! real provider, and these adapters speak plaintext to it over an **internal
//! container network**. That split is what keeps the transports std-only while
//! still delivering a real message to a real inbox and a real phone.
//!
//! Two properties make the plaintext hop safe, and both are enforced here rather
//! than documented and hoped for:
//!
//! * [`EgressEndpoint`] refuses any relay host that does not resolve to a
//!   loopback or RFC 1918 address, re-resolving **per connect** so a DNS record
//!   that changes between validation and use cannot move the hop onto a public
//!   network (the same resolve-then-validate discipline as the SRS-RES-001
//!   research proxy). A cleartext credential and an operator-facing alert body
//!   never leave the host's private network.
//! * The relay is **not** an open relay: each adapter authenticates with its own
//!   catalogued secret (`ATP_SMTP_API_KEY` / `ATP_SMS_API_KEY`), so a foreign
//!   container that can route to the relay still cannot send operator alerts.
//!   This is also what keeps [`atp_notification::channel`]'s contract true — the
//!   adapter reads the key and keeps it inside itself; the core never sees it.
//!
//! ## The send deadline is a TOTAL budget
//!
//! [`NotificationChannelClient::send`](atp_notification::NotificationChannelClient::send)
//! passes a per-channel `deadline`, and the port's contract is that the adapter
//! honours it with a cancellable I/O timeout. A socket timeout is *per operation*,
//! so arming `deadline` on each of an SMTP conversation's eight round trips would
//! license eight times the budget — the dispatcher's SLA arithmetic
//! (`MAX_CHANNEL_DEADLINE = DISPATCH_SLA_MS / REQUIRED_CHANNELS.len()`) would be
//! silently wrong, and a wedged relay would blow the NFR-P6 60,000 ms SLA while
//! every individual timeout looked correct.
//!
//! `SendBudget` is therefore the single clock for a whole send: it is created
//! once at entry and every subsequent socket operation is armed with what is
//! *left*, never with the original deadline. Exhaustion is
//! [`atp_notification::ChannelError::Timeout`].
//!
//! This is the residual `atp_notification::channel` documents as "verified at the
//! deferred SMTP/SMS adapter integration": a cancellable socket deadline is the
//! only leak-free bound available to a synchronous zero-dependency core, and it
//! is armed here.

use std::io::{self, BufRead, BufReader};
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use atp_notification::ChannelError;

pub mod sms;
pub mod smtp;

pub use sms::{SmsGatewayChannel, SmsGatewayConfig};
pub use smtp::{SmtpEmailChannel, SmtpRelayConfig};

/// The remaining-time clock for one `send`.
///
/// Constructed once per send and consulted before **every** socket operation, so
/// the port's `deadline` bounds the whole conversation rather than each leg of
/// it. See the module docs for why a per-operation timeout is not equivalent.
#[derive(Debug)]
pub(crate) struct SendBudget {
    started: Instant,
    total: Duration,
}

impl SendBudget {
    pub(crate) fn start(total: Duration) -> Self {
        Self {
            started: Instant::now(),
            total,
        }
    }

    /// What is left of the budget, or `None` once it is spent.
    ///
    /// `None` is also returned for a *non-zero but unusable* remainder: a zero
    /// `Duration` handed to [`TcpStream::set_read_timeout`] means "block forever"
    /// on every supported platform, so returning `Some(Duration::ZERO)` here
    /// would disarm the very timeout it is meant to arm — precisely inverting the
    /// deadline contract at the moment the budget runs out.
    pub(crate) fn remaining(&self) -> Option<Duration> {
        self.total
            .checked_sub(self.started.elapsed())
            .filter(|left| !left.is_zero())
    }

    /// The remaining budget, or a typed [`ChannelError::Timeout`] naming `stage`.
    pub(crate) fn remaining_or_timeout(&self, stage: &str) -> Result<Duration, ChannelError> {
        self.remaining().ok_or_else(|| ChannelError::Timeout {
            detail: format!(
                "send deadline of {}ms elapsed during {stage}",
                self.total.as_millis()
            ),
        })
    }
}

/// A validated plaintext egress hop: the local relay an adapter may talk to.
///
/// Holds the operator-configured host and port, and re-resolves + re-validates on
/// every `connect` rather than caching an address, so the
/// loopback/RFC 1918 guarantee is a property of the connection actually made and
/// not of a check performed once at startup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EgressEndpoint {
    host: String,
    port: u16,
}

impl EgressEndpoint {
    /// Build an endpoint from operator configuration.
    ///
    /// A blank host or a zero port is [`ChannelError::Unconfigured`] — an
    /// operator setup fault, fail-closed before any socket work.
    pub fn new(host: impl Into<String>, port: u16, channel: &str) -> Result<Self, ChannelError> {
        let host = host.into();
        if host.trim().is_empty() {
            return Err(ChannelError::Unconfigured {
                detail: format!("{channel}: relay host is blank"),
            });
        }
        if port == 0 {
            return Err(ChannelError::Unconfigured {
                detail: format!("{channel}: relay port is 0"),
            });
        }
        Ok(Self { host, port })
    }

    pub fn host(&self) -> &str {
        &self.host
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    /// Resolve the relay host **within the budget**.
    ///
    /// `ToSocketAddrs` is a blocking call with no timeout, so simply checking the
    /// budget before and after it would leave the deadline unenforced across the
    /// one step most able to wedge: a sick resolver holds the whole alert send
    /// for however long the C library feels like, and the connectivity-loss page
    /// never leaves the host. Checking the clock either side of an unbounded call
    /// documents the hole rather than closing it.
    ///
    /// So resolution runs on a worker thread and the *wait* is bounded by the
    /// remaining budget. If it overruns we return [`ChannelError::Timeout`] and
    /// let the worker finish into a dropped channel — it terminates on its own
    /// when the OS resolver gives up, holds no lock, and touches none of our
    /// sockets.
    ///
    /// This is not the detached-watchdog anti-pattern
    /// [`atp_notification::channel`] warns against. That one tries to cancel a
    /// wedged socket syscall it cannot reach, and leaks a thread per notification
    /// on a path with no rate limit. This is a self-terminating DNS lookup, on a
    /// path the SRS-NOTIF-001 sink already rate-limits to one dispatch per outage
    /// per cool-down, and it is skipped entirely when the host is an IP literal
    /// (which needs no resolver at all).
    fn resolve_within_budget(
        &self,
        budget: &SendBudget,
        channel: &str,
    ) -> Result<Vec<SocketAddr>, ChannelError> {
        // An IP literal needs no resolver: parse it directly and spawn nothing.
        if let Ok(ip) = self.host.parse::<IpAddr>() {
            return Ok(vec![SocketAddr::new(ip, self.port)]);
        }

        let remaining = budget.remaining_or_timeout("relay address resolution")?;
        let (sender, receiver) = mpsc::channel();
        let host = self.host.clone();
        let port = self.port;
        std::thread::Builder::new()
            .name("atp-notify-resolve".to_string())
            .spawn(move || {
                // A send failure means the requester already timed out and
                // dropped the receiver; nothing to do but exit.
                let _ = sender.send(
                    (host.as_str(), port)
                        .to_socket_addrs()
                        .map(|addresses| addresses.collect::<Vec<_>>())
                        .map_err(|err| err.to_string()),
                );
            })
            .map_err(|err| ChannelError::TransportUnavailable {
                detail: format!("{channel}: cannot start relay address resolution: {err}"),
            })?;

        match receiver.recv_timeout(remaining) {
            Ok(Ok(addresses)) => Ok(addresses),
            Ok(Err(err)) => Err(ChannelError::TransportUnavailable {
                detail: format!(
                    "{channel}: cannot resolve relay host {}:{}: {err}",
                    self.host, self.port
                ),
            }),
            Err(mpsc::RecvTimeoutError::Timeout) => Err(ChannelError::Timeout {
                detail: format!(
                    "{channel}: resolving relay host {} did not finish within the remaining \
                     send budget",
                    self.host
                ),
            }),
            // The worker panicked. Fail closed rather than treat a crashed
            // resolver as "no addresses".
            Err(mpsc::RecvTimeoutError::Disconnected) => Err(ChannelError::TransportUnavailable {
                detail: format!(
                    "{channel}: relay address resolution for {} failed unexpectedly",
                    self.host
                ),
            }),
        }
    }

    /// Resolve, validate, and connect within `budget`.
    pub(crate) fn connect(
        &self,
        budget: &SendBudget,
        channel: &str,
    ) -> Result<TcpStream, ChannelError> {
        let resolved = self.resolve_within_budget(budget, channel)?;

        if resolved.is_empty() {
            return Err(ChannelError::TransportUnavailable {
                detail: format!(
                    "{channel}: relay host {}:{} resolved to no addresses",
                    self.host, self.port
                ),
            });
        }

        // Validate EVERY resolved address, not just the one we connect to. A host
        // that resolves to [private, public] would otherwise pass the guard on the
        // first address and silently fall through to the public one on retry.
        for address in &resolved {
            if !is_private_egress_address(&address.ip()) {
                return Err(ChannelError::Unconfigured {
                    detail: format!(
                        "{channel}: relay host {} resolves to non-private address {} — the \
                         plaintext egress hop is confined to loopback / RFC 1918 (the TLS \
                         session to the real provider is the relay's job)",
                        self.host,
                        address.ip()
                    ),
                });
            }
        }

        let timeout = budget.remaining_or_timeout("relay connect")?;
        let address = resolved[0];
        let stream = TcpStream::connect_timeout(&address, timeout).map_err(|err| {
            if err.kind() == io::ErrorKind::TimedOut {
                ChannelError::Timeout {
                    detail: format!("{channel}: relay connect to {address} timed out"),
                }
            } else {
                ChannelError::TransportUnavailable {
                    detail: format!("{channel}: relay connect to {address} failed: {err}"),
                }
            }
        })?;
        Ok(stream)
    }
}

/// Whether `address` is on the host or a private network — the only places the
/// plaintext egress hop may go.
///
/// IPv4: loopback plus the three RFC 1918 blocks plus RFC 3927 link-local.
/// IPv6: loopback, unique-local (`fc00::/7`), link-local (`fe80::/10`), and
/// IPv4-mapped addresses re-checked as IPv4 — an IPv4-mapped public address
/// (`::ffff:93.184.216.34`) is a public address wearing an IPv6 shape, and
/// missing that unwrap is how this class of guard is usually bypassed.
fn is_private_egress_address(address: &IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_private() || v4.is_link_local(),
        IpAddr::V6(v6) => {
            if let Some(mapped) = v6.to_ipv4_mapped() {
                return mapped.is_loopback() || mapped.is_private() || mapped.is_link_local();
            }
            let segments = v6.segments();
            v6.is_loopback() || (segments[0] & 0xfe00) == 0xfc00 || (segments[0] & 0xffc0) == 0xfe80
        }
    }
}

/// Arm both socket timeouts with what is left of `budget`.
///
/// Called before every read and every write, so a relay that accepts the
/// connection and then stalls mid-conversation is bounded by the deadline that is
/// actually left rather than by a fresh full deadline per leg.
pub(crate) fn arm_socket(
    stream: &TcpStream,
    budget: &SendBudget,
    channel: &str,
    stage: &str,
) -> Result<(), ChannelError> {
    let remaining = budget.remaining_or_timeout(stage)?;
    stream
        .set_read_timeout(Some(remaining))
        .and_then(|()| stream.set_write_timeout(Some(remaining)))
        .map_err(|err| ChannelError::TransportUnavailable {
            detail: format!("{channel}: cannot arm socket deadline before {stage}: {err}"),
        })
}

/// Read one `\n`-terminated line, re-checking the budget before **every**
/// underlying socket read.
///
/// A socket timeout restarts its countdown on each `read` syscall, and both
/// [`std::io::BufRead::read_line`] and [`std::io::Read::read_to_end`] issue many
/// of them. Arming the deadline once and then calling either would let a relay
/// that dribbles a byte just inside the timeout hold the connection open for
/// (bytes x deadline) — the whole per-send budget defeated by a peer that is
/// never idle long enough to trip a single timeout. That is the slowloris shape,
/// and on this path it would stall an operator alert indefinitely while every
/// individual socket operation looked healthy.
///
/// Driving [`std::io::BufRead::fill_buf`] directly puts the budget check between
/// consecutive reads, so the elapsed-time bound — not the per-syscall timeout —
/// is what actually terminates the loop.
pub(crate) fn read_line_budgeted(
    reader: &mut BufReader<TcpStream>,
    budget: &SendBudget,
    channel: &str,
    stage: &str,
    max_bytes: usize,
) -> Result<Vec<u8>, ChannelError> {
    let mut line = Vec::new();
    loop {
        arm_socket(reader.get_ref(), budget, channel, stage)?;
        let available = match reader.fill_buf() {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(io_error_to_channel_error(&err, channel, stage)),
        };
        if available.is_empty() {
            break; // end of stream
        }
        match available.iter().position(|byte| *byte == b'\n') {
            Some(index) => {
                line.extend_from_slice(&available[..=index]);
                reader.consume(index + 1);
                break;
            }
            None => {
                let consumed = available.len();
                line.extend_from_slice(available);
                reader.consume(consumed);
            }
        }
        if line.len() > max_bytes {
            return Err(ChannelError::TransportUnavailable {
                detail: format!("{channel}: relay sent an over-long line during {stage}"),
            });
        }
    }
    Ok(line)
}

/// Read to end of stream under the same per-read budget discipline as
/// [`read_line_budgeted`], stopping at `max_bytes`.
pub(crate) fn read_to_end_budgeted(
    reader: &mut BufReader<TcpStream>,
    budget: &SendBudget,
    channel: &str,
    stage: &str,
    max_bytes: usize,
) -> Result<Vec<u8>, ChannelError> {
    let mut collected = Vec::new();
    loop {
        arm_socket(reader.get_ref(), budget, channel, stage)?;
        let available = match reader.fill_buf() {
            Ok(bytes) => bytes,
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(io_error_to_channel_error(&err, channel, stage)),
        };
        if available.is_empty() {
            break;
        }
        let room = max_bytes.saturating_sub(collected.len());
        let take = room.min(available.len());
        collected.extend_from_slice(&available[..take]);
        reader.consume(take);
        if collected.len() >= max_bytes {
            break;
        }
    }
    Ok(collected)
}

/// Map an I/O failure during the conversation onto the channel taxonomy.
///
/// A blocking socket whose armed timeout elapses reports `WouldBlock` or
/// `TimedOut` depending on platform and operation; both mean the deadline was hit
/// and must surface as [`ChannelError::Timeout`], not as a generic transport
/// fault — the dispatcher records the two differently and an operator reads them
/// as different remediations.
pub(crate) fn io_error_to_channel_error(
    err: &io::Error,
    channel: &str,
    stage: &str,
) -> ChannelError {
    match err.kind() {
        io::ErrorKind::TimedOut | io::ErrorKind::WouldBlock => ChannelError::Timeout {
            detail: format!("{channel}: relay timed out during {stage}"),
        },
        _ => ChannelError::TransportUnavailable {
            detail: format!("{channel}: relay I/O failed during {stage}: {err}"),
        },
    }
}

/// Fold CR and LF out of a single-line protocol field.
///
/// Both transports interpolate operator-facing text into a line-oriented
/// protocol (an SMTP header, an HTTP header). A `\r` or `\n` inside that text
/// would end the line early and let the remainder be read as protocol — an
/// injected `Bcc:` header on the email path, an injected header or a smuggled
/// request on the HTTP path.
///
/// The fold replaces rather than rejects, deliberately. This runs on the alert
/// path for connectivity loss and critical failures: refusing to deliver an
/// operator alert because its subject picked up a stray control character would
/// convert a cosmetic problem into a silent safety failure. The alert goes out;
/// only its whitespace changes.
pub(crate) fn fold_protocol_line(value: &str) -> String {
    value
        .chars()
        .map(|c| if c == '\r' || c == '\n' { ' ' } else { c })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budget_reports_remaining_time_then_expires() {
        let budget = SendBudget::start(Duration::from_millis(50));
        assert!(budget.remaining().is_some());
        assert!(budget.remaining_or_timeout("stage").is_ok());
    }

    #[test]
    fn an_exhausted_budget_is_a_typed_timeout_naming_the_stage() {
        let budget = SendBudget::start(Duration::ZERO);
        assert!(budget.remaining().is_none());
        match budget.remaining_or_timeout("relay connect") {
            Err(ChannelError::Timeout { detail }) => {
                assert!(detail.contains("relay connect"), "detail: {detail}");
            }
            other => panic!("expected Timeout, got {other:?}"),
        }
    }

    #[test]
    fn loopback_and_rfc1918_addresses_are_permitted() {
        for address in [
            "127.0.0.1",
            "10.0.0.5",
            "172.16.4.1",
            "172.31.255.254",
            "192.168.1.10",
            "::1",
            "fd00::1",
            "::ffff:192.168.1.10",
        ] {
            let parsed: IpAddr = address.parse().expect("test address parses");
            assert!(is_private_egress_address(&parsed), "{address} should pass");
        }
    }

    #[test]
    fn public_addresses_are_refused_including_ipv4_mapped_ones() {
        for address in [
            "8.8.8.8",
            "93.184.216.34",
            "172.32.0.1",
            "2606:4700:4700::1111",
            "::ffff:93.184.216.34",
        ] {
            let parsed: IpAddr = address.parse().expect("test address parses");
            assert!(!is_private_egress_address(&parsed), "{address} should fail");
        }
    }

    #[test]
    fn a_blank_host_or_zero_port_is_unconfigured() {
        assert!(matches!(
            EgressEndpoint::new("   ", 25, "email"),
            Err(ChannelError::Unconfigured { .. })
        ));
        assert!(matches!(
            EgressEndpoint::new("relay", 0, "email"),
            Err(ChannelError::Unconfigured { .. })
        ));
    }

    #[test]
    fn control_characters_are_folded_out_of_protocol_lines() {
        assert_eq!(
            fold_protocol_line("ATP alert\r\nBcc: attacker@example.com"),
            "ATP alert  Bcc: attacker@example.com"
        );
        assert_eq!(fold_protocol_line("plain subject"), "plain subject");
    }
}
