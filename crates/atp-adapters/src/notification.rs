//! SRS-NOTIF-001 operator-notification transports (IF-10 email, IF-11 push).
//!
//! The core dispatcher ([`atp_notification::OperatorNotifier`]) fans an operator
//! alert out over the two REQUIRED channels through the
//! [`atp_notification::NotificationChannelClient`] port. This module holds the
//! concrete transports behind that port, which AGENTS.md requires to live in the
//! adapter crate: the core names no vendor and holds no provider credential.
//!
//! ## Why email needs an egress relay and push does not
//!
//! A real SMTP provider requires TLS, and the ATP Rust workspace carries **zero
//! external crates** — there is no TLS implementation available to a std-only
//! adapter, and adding one would break the workspace-wide zero-dependency
//! invariant for the entire tree.
//!
//! So for IF-10 the TLS boundary is a deployment component, not a library: the
//! `phase1-notification-egress` service owns the authenticated TLS session to the
//! real provider, and [`smtp`] speaks plaintext to it over an **internal
//! container network**. That split is what keeps the transport std-only while
//! still delivering a real message to a real inbox.
//!
//! IF-11 needs no such hop. [`push`] targets a **self-hosted ntfy on the LAN**,
//! which the operator reaches from their phone over the VPN — the publish never
//! crosses the public internet, so there is nothing for TLS to protect against
//! here that the network boundary does not already. Push therefore talks to its
//! server directly, under the same egress allow-list as the relay hop.
//!
//! (SMS was IF-11 through 2026-08-16. It was replaced by push because US A2P
//! 10DLC registration is weeks of lead time with silent carrier filtering, and
//! push preserves StRS SN-1.12's reach-the-operator-when-they-are-not-looking
//! intent without it. SC-9's "at least two configured channels" is unchanged.)
//!
//! Two properties make the plaintext hops safe, and both are enforced here rather
//! than documented and hoped for:
//!
//! * [`EgressEndpoint`] refuses any host that does not resolve to a
//!   loopback or RFC 1918 address, re-resolving **per connect** so a DNS record
//!   that changes between validation and use cannot move the hop onto a public
//!   network (the same resolve-then-validate discipline as the SRS-RES-001
//!   research proxy). A cleartext credential and an operator-facing alert body
//!   never leave the host's private network.
//! * Neither endpoint is anonymous: each adapter authenticates with its own
//!   catalogued secret (`ATP_SMTP_API_KEY` / `ATP_PUSH_TOKEN`), so a foreign
//!   container that can route to them still cannot send operator alerts.
//!   On ntfy the TOPIC is a second credential — holding it is enough to
//!   publish — so `ATP_PUSH_TOPIC` is catalogued secret too (NFR-S4).
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
//! deferred SMTP/push adapter integration": a cancellable socket deadline is the
//! only leak-free bound available to a synchronous zero-dependency core, and it
//! is armed here.

use std::io::{self, BufRead, BufReader};
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use atp_notification::ChannelError;

pub mod push;
pub mod smtp;

pub use push::{PushChannel, PushConfig};
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

        self.connect_validated(&resolved, budget, channel)
    }

    /// Connect to the first reachable address, trying **all** of them.
    ///
    /// A hostname routinely resolves to several addresses — `localhost` is
    /// usually `::1` *and* `127.0.0.1` — and the resolver's ordering is not a
    /// statement about which one is listening. Taking only the first would mean a
    /// relay bound to IPv4, behind a resolver that returns IPv6 first, is
    /// reported unreachable while it is running: the operator gets no page, on
    /// the path whose entire job is to page.
    ///
    /// Every attempt draws from the same [`SendBudget`], so trying several
    /// addresses cannot extend the deadline — when the budget is spent the loop
    /// stops with [`ChannelError::Timeout`] rather than working through the rest.
    ///
    /// Split out from [`connect`](Self::connect) so the fallback can be tested
    /// deterministically: DNS ordering is not controllable from a test, but an
    /// explicit `[dead, live]` list is.
    pub(crate) fn connect_validated(
        &self,
        addresses: &[SocketAddr],
        budget: &SendBudget,
        channel: &str,
    ) -> Result<TcpStream, ChannelError> {
        let mut last_error: Option<ChannelError> = None;

        for address in addresses {
            // Re-checked per attempt: an exhausted budget propagates as Timeout
            // instead of quietly spending what is left on further addresses.
            let timeout = budget.remaining_or_timeout("relay connect")?;
            match TcpStream::connect_timeout(address, timeout) {
                Ok(stream) => return Ok(stream),
                Err(err) => {
                    last_error = Some(if err.kind() == io::ErrorKind::TimedOut {
                        ChannelError::Timeout {
                            detail: format!("{channel}: relay connect to {address} timed out"),
                        }
                    } else {
                        ChannelError::TransportUnavailable {
                            detail: format!("{channel}: relay connect to {address} failed: {err}"),
                        }
                    });
                }
            }
        }

        Err(
            last_error.unwrap_or_else(|| ChannelError::TransportUnavailable {
                detail: format!(
                    "{channel}: relay host {}:{} had no address to connect to",
                    self.host, self.port
                ),
            }),
        )
    }
}

/// Whether `address` is on the host or a private network — the only places the
/// plaintext egress hop may go.
///
/// IPv4: loopback plus the three RFC 1918 blocks. IPv6: loopback, unique-local
/// (`fc00::/7`), and IPv4-mapped addresses re-checked as IPv4 — an IPv4-mapped
/// public address (`::ffff:93.184.216.34`) is a public address wearing an IPv6
/// shape, and missing that unwrap is how this class of guard is usually bypassed.
///
/// **Link-local is deliberately NOT allowed**, in either family. It looks
/// private and is not a safe target: `169.254.169.254` is the cloud instance
/// metadata endpoint, and both adapters send their bearer credential and the
/// alert body immediately after connecting. A relay hostname resolving to a
/// link-local address would hand those to whatever answers there. Nothing in
/// this deployment needs it — the egress sidecar is reached over loopback or a
/// Docker bridge, both of which are covered above — so the safe default is to
/// refuse, not to widen the boundary for a case that does not arise.
fn is_private_egress_address(address: &IpAddr) -> bool {
    match address {
        IpAddr::V4(v4) => v4.is_loopback() || v4.is_private(),
        IpAddr::V6(v6) => {
            if let Some(mapped) = v6.to_ipv4_mapped() {
                return mapped.is_loopback() || mapped.is_private();
            }
            let segments = v6.segments();
            v6.is_loopback() || (segments[0] & 0xfe00) == 0xfc00
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

    /// Link-local looks private and is not a safe egress target: both adapters
    /// hand over a bearer credential and the alert body right after connecting,
    /// and `169.254.169.254` is the cloud instance-metadata endpoint.
    #[test]
    fn link_local_addresses_are_refused_despite_looking_private() {
        for address in [
            "169.254.169.254",
            "169.254.0.1",
            "fe80::1",
            "::ffff:169.254.169.254",
        ] {
            let parsed: IpAddr = address.parse().expect("test address parses");
            assert!(
                !is_private_egress_address(&parsed),
                "{address} must not be a permitted egress target"
            );
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
    fn connect_falls_through_a_dead_address_to_a_live_one() {
        use std::net::TcpListener;

        // The live relay.
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
        let live = listener.local_addr().expect("local addr");

        // A dead address: bound then dropped, so the port is closed.
        let dead = {
            let doomed = TcpListener::bind("127.0.0.1:0").expect("bind loopback");
            doomed.local_addr().expect("local addr")
        };

        let endpoint =
            EgressEndpoint::new("relay.internal", live.port(), "email").expect("endpoint is valid");
        let budget = SendBudget::start(Duration::from_secs(5));

        // Dead first — exactly the `::1` before `127.0.0.1` shape, made
        // deterministic. Taking only addresses[0] would report the running relay
        // as unreachable.
        let stream = endpoint
            .connect_validated(&[dead, live], &budget, "email")
            .expect("must fall through to the live address");
        assert_eq!(stream.peer_addr().expect("peer addr").port(), live.port());
    }

    #[test]
    fn connect_reports_the_last_failure_when_every_address_is_dead() {
        let dead_one = {
            let doomed = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
            doomed.local_addr().expect("addr")
        };
        let dead_two = {
            let doomed = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
            doomed.local_addr().expect("addr")
        };

        let endpoint =
            EgressEndpoint::new("relay.internal", dead_one.port(), "email").expect("valid");
        let budget = SendBudget::start(Duration::from_secs(2));

        match endpoint.connect_validated(&[dead_one, dead_two], &budget, "email") {
            Err(ChannelError::TransportUnavailable { detail }) => {
                assert!(detail.contains(&dead_two.port().to_string()), "{detail}");
            }
            other => panic!("expected TransportUnavailable, got {other:?}"),
        }
    }

    #[test]
    fn trying_several_addresses_cannot_extend_the_deadline() {
        let dead = {
            let doomed = std::net::TcpListener::bind("127.0.0.1:0").expect("bind");
            doomed.local_addr().expect("addr")
        };
        let endpoint = EgressEndpoint::new("relay.internal", dead.port(), "email").expect("valid");
        // Already spent: the very first attempt must refuse, not work the list.
        let budget = SendBudget::start(Duration::ZERO);

        assert!(matches!(
            endpoint.connect_validated(&[dead, dead, dead], &budget, "email"),
            Err(ChannelError::Timeout { .. })
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
