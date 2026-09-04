//! SRS-MD-005 / SyRS SYS-75(c) / NFR-R2 — "is the IB Gateway answering?", as a
//! control-plane seam of its own.
//!
//! A SEPARATE module from `interactive_brokers`, for the same reason
//! [`connection_control`](crate::connection_control) is separate: that module
//! is the pinned SRS-EXE-006 transport contract, and
//! `tools/ib_adapter_check.py` binds the operator's paper-account evidence to
//! its exact bytes (`code_digest`). Adding a reachability probe there would
//! invalidate that evidence and flip a closed-green feature red, recoverable
//! only by a fresh live run. So it lands here.
//!
//! It is also deliberately NOT a wire operation. The question SRS-MD-005 asks
//! is "did the daily restart finish", which is answered by whether the gateway
//! accepts a TCP connection at all — no handshake, no session, no client id
//! consumed. That matters twice over:
//!
//!   * the gateway serves ONE API client and leaves a prior connection in
//!     `CLOSE_WAIT`, so a probe that opened a real session would spend the
//!     single slot the reconnect is waiting for; and
//!   * the acceptance criterion's sharpest clause — "if IB Gateway remains
//!     unavailable after the window, standard connectivity loss handling
//!     occurs" — is provable by fault injection against a dead port, with no
//!     gateway involved at all. This module is what the fault is injected into.
//!
//! `atp-execution` and `atp-orchestrator` must not name a vendor module
//! (SRS-ARCH-002 / the adapter-isolation check), so the composition layer
//! consumes the [`GatewayReachability`] trait and never the concrete probe's
//! transport details.

use std::fmt;
use std::io::ErrorKind;
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

/// How long a reachability probe may wait for the TCP handshake.
///
/// Its own constant rather than the transport's `IB_CONNECT_TIMEOUT`, because
/// that one is feature-gated behind `ib-live-transport` and this probe must
/// work in the default build — the deterministic fault-injection path is the
/// whole verification method for SRS-MD-005.
///
/// Two seconds is chosen against the requirement, not by feel: NFR-R2 budgets
/// 15 s from detection to a reconnection ATTEMPT, and SYS-75 asks the question
/// repeatedly across a 5-minute window, so a probe must resolve fast enough to
/// be asked many times inside it. A refused connection returns immediately;
/// this bound only governs a host that silently drops packets.
pub const REACHABILITY_PROBE_TIMEOUT: Duration = Duration::from_secs(2);

/// What one probe observed.
///
/// Three outcomes, not a `bool`, because "the gateway said no" and "we could
/// not ask" are different facts and only the first is evidence about the
/// gateway. Callers still collapse this to a reachable/not decision, but the
/// collapse happens where the reason can be logged rather than at the socket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReachabilityOutcome {
    /// The endpoint accepted a TCP connection. The gateway is listening; this
    /// does NOT claim the API is ready (the gateway accepts TCP before it will
    /// answer a handshake), only that the process is back.
    Reachable,
    /// The endpoint refused, reset, or did not answer within
    /// [`REACHABILITY_PROBE_TIMEOUT`].
    Unreachable { detail: String },
    /// The probe itself could not run — a malformed endpoint, an exhausted
    /// local resource. Distinct from `Unreachable` because absence of evidence
    /// is not evidence of absence: reporting a local failure as "the gateway is
    /// down" would page the operator about our own host.
    ProbeFailed { detail: String },
}

impl ReachabilityOutcome {
    /// Collapse to the reachable/not decision the restart-window policy takes.
    ///
    /// An allowlist: only `Reachable` is reachable. `ProbeFailed` is treated as
    /// NOT reachable, which is the fail-closed direction — during the restart
    /// window that keeps the system suspended (the safe state), and after it
    /// the escalation pages an operator who can see the probe's own failure in
    /// the detail string.
    pub fn is_reachable(&self) -> bool {
        matches!(self, Self::Reachable)
    }

    /// A short, stable label for operator output and structured records.
    pub const fn as_str(&self) -> &'static str {
        match self {
            Self::Reachable => "REACHABLE",
            Self::Unreachable { .. } => "UNREACHABLE",
            Self::ProbeFailed { .. } => "PROBE_FAILED",
        }
    }

    /// The human-readable reason, empty when there is nothing to explain.
    pub fn detail(&self) -> &str {
        match self {
            Self::Reachable => "",
            Self::Unreachable { detail } | Self::ProbeFailed { detail } => detail,
        }
    }
}

impl fmt::Display for ReachabilityOutcome {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Reachable => formatter.write_str("REACHABLE"),
            Self::Unreachable { detail } => write!(formatter, "UNREACHABLE ({detail})"),
            Self::ProbeFailed { detail } => write!(formatter, "PROBE_FAILED ({detail})"),
        }
    }
}

/// The "is the gateway back?" capability the SRS-MD-005 restart-window
/// producer consults.
pub trait GatewayReachability {
    /// Observe the endpoint once. Implementations must be cheap enough to call
    /// on every state read and must never mutate broker state.
    fn probe(&self) -> ReachabilityOutcome;
}

/// A bounded TCP reachability probe against a literal socket address.
///
/// Takes a [`SocketAddr`], never a hostname, and so never calls
/// `to_socket_addrs`: name resolution is a blocking `getaddrinfo` that sits
/// OUTSIDE `connect_timeout`'s deadline, so a hostname would make the bound a
/// suggestion. The host is validated as an `IpAddr` at configuration load,
/// which is where a typo should fail.
#[derive(Debug, Clone, Copy)]
pub struct TcpGatewayReachability {
    endpoint: SocketAddr,
    timeout: Duration,
}

impl TcpGatewayReachability {
    /// Probe `endpoint` with the default [`REACHABILITY_PROBE_TIMEOUT`].
    pub const fn new(endpoint: SocketAddr) -> Self {
        Self {
            endpoint,
            timeout: REACHABILITY_PROBE_TIMEOUT,
        }
    }

    /// Probe `endpoint` with an explicit deadline.
    ///
    /// A zero timeout is raised to one millisecond rather than passed through:
    /// `connect_timeout` rejects `Duration::ZERO` on some platforms and
    /// instantly times out on others, so a zero would report a healthy gateway
    /// as permanently down — an alarm manufactured by the probe itself.
    pub fn with_timeout(endpoint: SocketAddr, timeout: Duration) -> Self {
        Self {
            endpoint,
            timeout: timeout.max(Duration::from_millis(1)),
        }
    }

    /// The endpoint this probe targets.
    pub const fn endpoint(&self) -> SocketAddr {
        self.endpoint
    }

    /// The deadline this probe applies.
    pub const fn timeout(&self) -> Duration {
        self.timeout
    }
}

impl GatewayReachability for TcpGatewayReachability {
    fn probe(&self) -> ReachabilityOutcome {
        match TcpStream::connect_timeout(&self.endpoint, self.timeout) {
            Ok(stream) => {
                // Close immediately. The gateway serves one API client and
                // leaves a lingering connection in CLOSE_WAIT occupying that
                // slot, so a probe that held the socket open would block the
                // very reconnect it exists to detect.
                drop(stream);
                ReachabilityOutcome::Reachable
            }
            Err(error) => {
                let detail = format!("{} ({})", error.kind_label(), self.endpoint);
                match error.kind() {
                    // The endpoint answered "no" (refused / reset), or stayed
                    // silent until the deadline. Either way the gateway is not
                    // serving — which during a restart is exactly expected.
                    //
                    // Deliberately NOT listing `HostUnreachable`,
                    // `NetworkUnreachable` or `NetworkDown`: those were
                    // stabilised in Rust 1.83 and the workspace declares
                    // `rust-version = "1.75"`, so naming them would make this
                    // crate stop building at its own declared minimum. They
                    // fall through to `ProbeFailed` below, which
                    // `is_reachable()` already treats as NOT reachable — the
                    // fail-closed direction, so the SAFETY answer is identical
                    // and only the operator-facing label is less precise.
                    ErrorKind::ConnectionRefused
                    | ErrorKind::ConnectionReset
                    | ErrorKind::ConnectionAborted
                    | ErrorKind::TimedOut => ReachabilityOutcome::Unreachable { detail },
                    // Anything else is a fact about THIS host, not the gateway.
                    _ => ReachabilityOutcome::ProbeFailed { detail },
                }
            }
        }
    }
}

/// Render an IO error without leaking the OS message verbatim into a field an
/// operator record will persist.
trait KindLabel {
    fn kind_label(&self) -> String;
}

impl KindLabel for std::io::Error {
    fn kind_label(&self) -> String {
        format!("{:?}", self.kind())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::{Ipv4Addr, TcpListener};

    fn loopback(port: u16) -> SocketAddr {
        SocketAddr::from((Ipv4Addr::LOCALHOST, port))
    }

    /// Bind an ephemeral port, then release it, so the returned address is
    /// known-dead on this host right now. This is the fault injection the
    /// SRS-MD-005 acceptance criterion is verified by, and it needs no gateway
    /// — which is exactly why the feature's verification method is
    /// `integration` rather than `live-ib`.
    fn dead_port() -> SocketAddr {
        let listener = TcpListener::bind(loopback(0)).expect("bind an ephemeral port");
        let addr = listener.local_addr().expect("read the bound address");
        drop(listener);
        addr
    }

    #[test]
    fn a_listening_endpoint_is_reachable() {
        // The positive control. Without it, every "unreachable" assertion below
        // would pass on a probe that can never connect to anything.
        let listener = TcpListener::bind(loopback(0)).expect("bind an ephemeral port");
        let addr = listener.local_addr().expect("read the bound address");
        let outcome = TcpGatewayReachability::new(addr).probe();
        assert_eq!(outcome, ReachabilityOutcome::Reachable);
        assert!(outcome.is_reachable());
        assert_eq!(outcome.as_str(), "REACHABLE");
        drop(listener);
    }

    #[test]
    fn a_dead_port_is_unreachable_not_a_probe_failure() {
        // A refused connection is evidence ABOUT the gateway, so it must not
        // land in ProbeFailed — that variant means we could not ask, and
        // conflating them would make a real outage read as a local fault.
        let outcome = TcpGatewayReachability::new(dead_port()).probe();
        assert!(
            matches!(outcome, ReachabilityOutcome::Unreachable { .. }),
            "a dead port must classify as Unreachable; got {outcome:?}"
        );
        assert!(!outcome.is_reachable());
        assert_eq!(outcome.as_str(), "UNREACHABLE");
        assert!(
            !outcome.detail().is_empty(),
            "an unreachable outcome must carry a reason the operator can read"
        );
    }

    #[test]
    fn only_a_reachable_outcome_counts_as_reachable() {
        // Allowlist, not denylist: a future outcome variant inherits "not
        // reachable", which keeps the system suspended (safe) rather than
        // resuming on an answer nobody classified.
        assert!(ReachabilityOutcome::Reachable.is_reachable());
        assert!(!ReachabilityOutcome::Unreachable {
            detail: "refused".to_string()
        }
        .is_reachable());
        assert!(!ReachabilityOutcome::ProbeFailed {
            detail: "no file descriptors".to_string()
        }
        .is_reachable());
    }

    #[test]
    fn a_zero_timeout_is_raised_rather_than_passed_through() {
        // A zero deadline reports a healthy gateway as permanently down on
        // some platforms — an alarm the probe manufactures itself.
        let probe = TcpGatewayReachability::with_timeout(loopback(1), Duration::ZERO);
        assert_eq!(probe.timeout(), Duration::from_millis(1));
        // And the non-vacuity partner: a real timeout is preserved exactly.
        let explicit =
            TcpGatewayReachability::with_timeout(loopback(1), Duration::from_millis(250));
        assert_eq!(explicit.timeout(), Duration::from_millis(250));
    }

    #[test]
    fn the_default_probe_timeout_is_bounded_well_inside_the_nfr_r2_budget() {
        // NFR-R2 allows 15 s from detection to a reconnection attempt, and
        // SYS-75 asks this question repeatedly inside a 5-minute window, so a
        // probe deadline anywhere near the budget would let one probe consume
        // it.
        assert_eq!(REACHABILITY_PROBE_TIMEOUT, Duration::from_secs(2));
        assert!(REACHABILITY_PROBE_TIMEOUT < Duration::from_secs(15));
        assert_eq!(
            TcpGatewayReachability::new(loopback(4002)).timeout(),
            REACHABILITY_PROBE_TIMEOUT
        );
    }
}
