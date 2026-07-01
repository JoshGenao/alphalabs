//! Notification channel transport port (SRS-NOTIF-001).
//!
//! SRS-NOTIF-001 fans an operator notification out over email (IF-10, SMTP or a
//! third-party email API) and SMS (IF-11, a third-party SMS gateway). AGENTS.md
//! forbids vendor SDK logic in the core runtime services and requires every
//! external provider to sit behind an adapter interface. So the dispatcher
//! ([`crate::dispatcher`]) talks only to this **port** — [`NotificationChannelClient`] —
//! and the concrete SMTP client / SMS gateway client are adapters that live in
//! `atp-adapters` (deferred with the real end-to-end integration; see the
//! crate-level scope note). The core never names a vendor and never holds a
//! credential; the adapter reads `ATP_SMTP_API_KEY` / `ATP_SMS_API_KEY`
//! (NFR-S4, encrypted at rest, never logged) and keeps them inside itself.
//!
//! ## Fail-closed, never-dropped transport errors
//!
//! A channel send either accepts the message for delivery ([`ChannelReceipt`],
//! carrying the provider's accept id) or returns a typed [`ChannelError`] naming
//! *why* it could not — the transport-failure taxonomy the deferred
//! consumers (kill-switch / Hot-Swap operator-alert sinks) said would "land with
//! the SRS-NOTIF-001 dispatcher". The dispatcher records the failure on the
//! stored event rather than dropping it, so a channel outage is operator-visible
//! evidence.

use core::fmt;
use std::time::Duration;

use crate::event::NotificationChannel;

/// The operator-facing message a channel delivers. Deliberately holds only
/// non-secret, operator-facing content: a `subject` (used by email; SMS ignores
/// it) and a `body`. It carries **no** recipient credential, provider API key,
/// or auth token — those live inside the concrete adapter (NFR-S4). The
/// dispatcher builds one of these from a [`crate::event::NotificationTrigger`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotificationMessage {
    subject: String,
    body: String,
}

impl NotificationMessage {
    pub fn new(subject: impl Into<String>, body: impl Into<String>) -> Self {
        Self {
            subject: subject.into(),
            body: body.into(),
        }
    }

    pub fn subject(&self) -> &str {
        &self.subject
    }

    pub fn body(&self) -> &str {
        &self.body
    }
}

/// A successful hand-off receipt from a channel adapter — the provider accepted
/// the message for delivery (SMTP `250`, SMS gateway accept). The `reference` is
/// the provider's opaque accept id (a message id / gateway ticket); it is
/// non-secret and is stored on the notification event's delivery `detail` so an
/// operator can correlate with the provider's own logs. It is NOT a proof of
/// end-user receipt (out of the Phase-1 baseline).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelReceipt {
    reference: String,
}

impl ChannelReceipt {
    pub fn new(reference: impl Into<String>) -> Self {
        Self {
            reference: reference.into(),
        }
    }

    pub fn reference(&self) -> &str {
        &self.reference
    }
}

/// Why a channel send could not accept the message — the transport-failure
/// taxonomy. Each variant is a *distinct operator remediation*, not just a
/// string: an `Unconfigured` channel (missing/blank credential) is an operator
/// setup fix; `TransportUnavailable` is a provider outage to retry; `Rejected`
/// is a permanent per-message refusal (bad recipient / malformed) that a retry
/// will not fix. The `detail` is a short, non-secret human string.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChannelError {
    /// The channel has no usable configuration (missing or blank
    /// `ATP_SMTP_API_KEY` / `ATP_SMS_API_KEY`, no sender/recipient). The
    /// operator must configure the channel; a fail-closed setup error, not a
    /// transient one.
    Unconfigured { detail: String },
    /// The provider was unreachable (SMTP connect failure, SMS gateway 5xx).
    /// Transient — a retry / the next detection may succeed.
    TransportUnavailable { detail: String },
    /// The adapter's own cancellable send deadline (the `deadline` passed to
    /// [`NotificationChannelClient::send`]) elapsed before the provider accepted
    /// the message. The typed timeout result the dispatcher records as `Failed`
    /// before continuing to the other required channel.
    Timeout { detail: String },
    /// The provider reached but permanently refused this specific message
    /// (invalid recipient, malformed payload, auth rejected). A retry of the
    /// same message will not succeed.
    Rejected { detail: String },
}

impl ChannelError {
    /// A stable discriminator string for the failure class (stored on the event
    /// delivery `detail`, surfaced to the dashboard alert pane). Distinct from
    /// the free-form human message.
    pub const fn kind_str(&self) -> &'static str {
        match self {
            Self::Unconfigured { .. } => "UNCONFIGURED",
            Self::TransportUnavailable { .. } => "TRANSPORT_UNAVAILABLE",
            Self::Timeout { .. } => "TIMEOUT",
            Self::Rejected { .. } => "REJECTED",
        }
    }

    /// The non-secret human detail carried by the failure.
    pub fn detail(&self) -> &str {
        match self {
            Self::Unconfigured { detail }
            | Self::TransportUnavailable { detail }
            | Self::Timeout { detail }
            | Self::Rejected { detail } => detail,
        }
    }
}

impl fmt::Display for ChannelError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.kind_str(), self.detail())
    }
}

impl std::error::Error for ChannelError {}

/// The result of a single channel send: an accept receipt or a typed transport
/// failure. Never a silently-dropped outcome — the dispatcher records either arm
/// on the stored notification event.
pub type ChannelSendResult = Result<ChannelReceipt, ChannelError>;

/// The adapter interface every notification channel implements (SRS-NOTIF-001,
/// AGENTS.md adapter-isolation constraint). The dispatcher holds channel clients
/// only as `&dyn NotificationChannelClient` / generic ports, so the core carries
/// no SMTP or SMS vendor dependency.
///
/// ## The send deadline is a mandatory part of the API
///
/// [`send`](NotificationChannelClient::send) takes a `deadline` — the dispatcher
/// **always** passes its configured per-channel budget, so the timeout is a
/// non-optional part of the contract, not a doc-only hope or an opt-in wrapper a
/// caller might forget. The adapter must honour it by arming a *cancellable* I/O
/// timeout on its own socket (a connect/read deadline ≤ `deadline`, the same
/// discipline as the IB adapter's explicit `connect_timeout`) and returning
/// [`ChannelError::Timeout`] when it elapses.
///
/// A cancellable socket deadline is the correct — and only leak-free — way to
/// bound a blocked network call: the core dispatcher stays a simple synchronous
/// fan-out that records a channel returning [`ChannelError::Timeout`] as `Failed`
/// and continues to the other required channel, rather than wrapping each send in
/// a watchdog thread (a detached watchdog cannot cancel a wedged blocking
/// syscall; it would leak one stuck thread per notification). The **residual**
/// case — an adapter that ignores the `deadline` and blocks forever — cannot be
/// force-cancelled in synchronous, zero-dependency std; full hang-proofing would
/// require an async/cancellable transport runtime, which is out of the release
/// baseline. That residual is verified at the deferred SMTP/SMS adapter
/// integration (one reason SRS-NOTIF-001 lands `serialized`).
pub trait NotificationChannelClient {
    /// Which channel this client delivers to. Used by the dispatcher to label
    /// the delivery record and to detect a mismatched client set.
    fn channel(&self) -> NotificationChannel;

    /// Attempt to deliver `message`, honouring `deadline` via a cancellable I/O
    /// timeout on the adapter's own socket. Returns an accept receipt or a typed
    /// transport failure ([`ChannelError::Timeout`] when the deadline elapses).
    /// Must not panic.
    fn send(&self, message: &NotificationMessage, deadline: Duration) -> ChannelSendResult;
}
