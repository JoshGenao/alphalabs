//! The notification dispatch authority (SRS-NOTIF-001).
//!
//! [`OperatorNotifier`] is the source-neutral engine that turns a *detected*
//! condition ([`NotificationTrigger`]) into a stored [`NotificationEvent`]: it
//! builds the operator message, fans it out over the supplied channel ports
//! (email + SMS), records each channel's real delivery outcome, and stamps the
//! dispatch instant so the ≤ 60-second SLA (NFR-P6 / SYS-46) is measurable from
//! the stored event. It is the concrete fan-out the deferred kill-switch /
//! Hot-Swap / orchestrator operator-alert sinks named as "landing with the
//! SRS-NOTIF-001 dispatcher".
//!
//! ## Clock is injected, not read
//!
//! The dispatcher never calls `SystemTime::now`. The caller passes
//! `dispatch_began_at_millis` — the epoch-millisecond instant it began dispatch,
//! taken from the composition root's clock at (or just after) detection. This
//! keeps dispatch latency deterministic and unit-testable and keeps the core
//! free of ambient time (the same discipline the rest of the tree follows with
//! `observed_at_seconds` on its events). A `dispatch_began_at_millis` earlier
//! than the trigger's detection instant is impossible provenance and is rejected
//! ([`DispatchError::DispatchBeforeDetection`]) so a clock-skew / caller bug can
//! never turn invalid timing into a passing SLA record.
//!
//! ## Per-channel send deadline: mandatory in the API, enforced by the adapter
//!
//! The dispatcher passes its configured `channel_deadline` to **every**
//! [`NotificationChannelClient::send`] — the timeout is a non-optional parameter
//! of the API, not a doc-only hope. The adapter honours it via a *cancellable*
//! I/O timeout on its own socket and returns [`crate::channel::ChannelError::Timeout`]
//! when it elapses; the dispatcher records that as a `Failed` delivery, continues to the
//! other required channel, and still produces the event for storage. A cancellable
//! socket deadline is the only leak-free way to bound a blocked network call, so
//! the core stays a simple synchronous fan-out rather than a watchdog-thread-per-
//! send (which would leak a stuck thread on a wedged adapter). The residual — an
//! adapter that ignores its `deadline` and blocks forever — is unrepresentable
//! without an async/cancellable transport runtime (out of the zero-dep baseline)
//! and is verified at the deferred SMTP/SMS adapter integration, one reason
//! SRS-NOTIF-001 lands `serialized`.
//!
//! ## Safety invariant: a critical failure is never suppressed
//!
//! SYS-75 (SRS-MD-005) suppresses *connectivity-loss* notifications during the
//! scheduled IB Gateway restart window — and only those. A critical system
//! failure must always reach the operator. So [`OperatorNotifier::dispatch`]
//! honours a [`SuppressionReason`] **only** for a [`TriggerKind::ConnectivityLoss`]
//! trigger; a suppression request against a [`TriggerKind::CriticalFailure`] is
//! ignored and the notification is dispatched normally. This is a fail-safe
//! encoded in the dispatcher, not a convention left to callers.

use std::sync::Arc;
use std::time::Duration;

use crate::channel::{NotificationChannelClient, NotificationMessage};
use crate::event::{
    ChannelDelivery, DeliveryOutcome, NotificationChannel, NotificationEvent, NotificationTrigger,
    TriggerKind, DISPATCH_SLA_MS, REQUIRED_CHANNELS,
};

/// A channel client the dispatcher fans out to. Held as a shared `Arc` handle —
/// the composition root constructs each concrete SMTP / SMS adapter once and
/// shares it across the runtime (the notification dispatcher, the readiness
/// probe, etc.). `Send + Sync` keeps the handle usable from whatever runtime
/// task drives dispatch.
pub type SharedChannelClient = Arc<dyn NotificationChannelClient + Send + Sync>;

/// Why a dispatch was refused before any channel was attempted. SRS-NOTIF-001
/// requires notifying over **email and SMS** — the dispatcher owns that fan-out
/// obligation rather than trusting the caller's channel slice, so a mis-wired
/// call cannot silently store an "apparently valid" notification event that
/// never attempted a required channel.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DispatchError {
    /// A channel SRS-NOTIF-001 requires (email or SMS) was absent from the
    /// client set. Fail-closed: no event is produced.
    MissingRequiredChannel { channel: NotificationChannel },
    /// A channel appeared more than once in the client set — a mis-wiring that
    /// would double-send and record ambiguous delivery status. Fail-closed.
    DuplicateChannel { channel: NotificationChannel },
    /// `dispatch_began_at_millis` preceded the trigger's detection instant —
    /// impossible provenance (dispatch cannot begin before detection). Rejected
    /// so a reversed-timestamp / clock-skew bug can never record a fake
    /// zero-millisecond latency that spuriously passes the SLA.
    DispatchBeforeDetection {
        detected_at_millis: u64,
        dispatch_began_at_millis: u64,
    },
}

impl core::fmt::Display for DispatchError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::MissingRequiredChannel { channel } => write!(
                f,
                "SRS-NOTIF-001 requires email and SMS; required channel {} was not supplied",
                channel.as_str()
            ),
            Self::DuplicateChannel { channel } => {
                write!(
                    f,
                    "channel {} was supplied more than once",
                    channel.as_str()
                )
            }
            Self::DispatchBeforeDetection {
                detected_at_millis,
                dispatch_began_at_millis,
            } => write!(
                f,
                "dispatch_began_at_millis {dispatch_began_at_millis} precedes detected_at_millis \
                 {detected_at_millis} (dispatch cannot begin before detection)"
            ),
        }
    }
}

impl std::error::Error for DispatchError {}

/// Why a notification dispatch was suppressed. Phase 1 has exactly one reason:
/// the SYS-75 / SRS-MD-005 scheduled IB Gateway restart window, during which
/// connectivity-loss notifications are intentionally silenced (the disconnect is
/// planned maintenance, not a fault). The restart-window *decision* is owned by
/// SRS-MD-005; this enum is the seam the dispatcher honours.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum SuppressionReason {
    ScheduledRestartWindow,
}

impl SuppressionReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ScheduledRestartWindow => "SCHEDULED_RESTART_WINDOW",
        }
    }
}

/// The source-neutral operator-notification dispatch authority (SRS-NOTIF-001).
/// Holds no channel handles and no clock — both are passed per dispatch — but
/// carries the per-channel send `channel_deadline` it passes to every
/// [`NotificationChannelClient::send`], so the timeout is a mandatory part of the
/// contract rather than a caller convention.
#[derive(Debug, Clone, Copy)]
pub struct OperatorNotifier {
    channel_deadline: Duration,
}

impl Default for OperatorNotifier {
    fn default() -> Self {
        Self::new()
    }
}

impl OperatorNotifier {
    /// The largest per-channel send deadline that still lets **every** required
    /// channel be attempted within the NFR-P6 dispatch budget in the worst case
    /// (sequential fan-out): `DISPATCH_SLA_MS / REQUIRED_CHANNELS.len()`. With the
    /// Phase-1 email+SMS pair that is 30,000 ms, so two back-to-back timed-out
    /// sends still fit inside the 60,000 ms budget.
    /// [`with_channel_deadline`](Self::with_channel_deadline) clamps to this so a
    /// mis-configured over-large deadline can never let the second required
    /// channel slip past the budget.
    pub const MAX_CHANNEL_DEADLINE: Duration =
        Duration::from_millis(DISPATCH_SLA_MS / REQUIRED_CHANNELS.len() as u64);

    /// The default per-channel send deadline — well under
    /// [`MAX_CHANNEL_DEADLINE`](Self::MAX_CHANNEL_DEADLINE) so both required
    /// channels can be attempted within budget.
    pub const DEFAULT_CHANNEL_DEADLINE: Duration = Duration::from_secs(20);

    /// A notifier with the [`DEFAULT_CHANNEL_DEADLINE`](Self::DEFAULT_CHANNEL_DEADLINE).
    pub const fn new() -> Self {
        Self {
            channel_deadline: Self::DEFAULT_CHANNEL_DEADLINE,
        }
    }

    /// A notifier with an explicit per-channel send deadline, **clamped** to
    /// [`MAX_CHANNEL_DEADLINE`](Self::MAX_CHANNEL_DEADLINE) so a configured value
    /// too large for the required-channel count can never push a later channel's
    /// attempt past the 60,000 ms dispatch budget.
    pub const fn with_channel_deadline(channel_deadline: Duration) -> Self {
        let channel_deadline =
            if channel_deadline.as_millis() > Self::MAX_CHANNEL_DEADLINE.as_millis() {
                Self::MAX_CHANNEL_DEADLINE
            } else {
                channel_deadline
            };
        Self { channel_deadline }
    }

    /// The per-channel send deadline this notifier passes to each adapter.
    pub const fn channel_deadline(&self) -> Duration {
        self.channel_deadline
    }

    /// Dispatch a notification for `trigger`, fanning out over `channels`, and
    /// return the [`NotificationEvent`] recording the outcome.
    /// `dispatch_began_at_millis` is the instant dispatch began (the SLA
    /// anchor). Suppression is not applied — equivalent to
    /// [`dispatch_with_suppression`](Self::dispatch_with_suppression) with `None`.
    ///
    /// **Fail-closed on the channel set:** SRS-NOTIF-001 requires email *and*
    /// SMS, so `channels` must contain each required channel
    /// ([`REQUIRED_CHANNELS`]) exactly once; a missing or duplicated required
    /// channel returns a [`DispatchError`] and produces **no** event, so a
    /// mis-wired caller can never store an "apparently valid" notification that
    /// skipped a required channel.
    ///
    /// The event is returned, not persisted: storing it durably is the caller's
    /// step via [`crate::store::NotificationEventStore`]. Splitting dispatch from
    /// storage keeps the SLA measurement (dispatch) independent of I/O latency
    /// (storage).
    pub fn dispatch(
        &self,
        trigger: &NotificationTrigger,
        dispatch_began_at_millis: u64,
        channels: &[SharedChannelClient],
    ) -> Result<NotificationEvent, DispatchError> {
        self.dispatch_with_suppression(trigger, dispatch_began_at_millis, channels, None)
    }

    /// Dispatch, honouring an optional [`SuppressionReason`].
    ///
    /// Enforces the same required-channel contract as [`dispatch`](Self::dispatch)
    /// (email + SMS, each exactly once — even a suppressed dispatch records both
    /// required channels) and the same reversed-timestamp guard.
    ///
    /// When `suppression` is `Some` **and** the trigger is a connectivity loss,
    /// no channel send is attempted; instead every required channel is recorded
    /// with [`DeliveryOutcome::Suppressed`] so the stored event proves the
    /// dispatcher *chose* not to send (distinct from a failed send). When the
    /// trigger is a [`TriggerKind::CriticalFailure`], suppression is ignored (a
    /// critical failure always notifies — the SYS-75 fail-safe) and the send
    /// proceeds normally.
    pub fn dispatch_with_suppression(
        &self,
        trigger: &NotificationTrigger,
        dispatch_began_at_millis: u64,
        channels: &[SharedChannelClient],
        suppression: Option<SuppressionReason>,
    ) -> Result<NotificationEvent, DispatchError> {
        Self::validate_required_channels(channels)?;
        // Impossible provenance: dispatch cannot begin before detection. Reject so
        // the stored latency / SLA verdict is always trustworthy.
        if dispatch_began_at_millis < trigger.detected_at_millis() {
            return Err(DispatchError::DispatchBeforeDetection {
                detected_at_millis: trigger.detected_at_millis(),
                dispatch_began_at_millis,
            });
        }

        // A critical failure is never suppressed, whatever the caller passes.
        let effective_suppression = match trigger.kind() {
            TriggerKind::CriticalFailure => None,
            TriggerKind::ConnectivityLoss => suppression,
        };

        let deliveries: Vec<ChannelDelivery> = if let Some(reason) = effective_suppression {
            channels
                .iter()
                .map(|client| {
                    ChannelDelivery::new(
                        client.channel(),
                        DeliveryOutcome::Suppressed,
                        reason.as_str(),
                    )
                })
                .collect()
        } else {
            let message = Self::build_message(trigger);
            channels
                .iter()
                .map(|client| self.send_one(client, &message))
                .collect()
        };

        Ok(NotificationEvent::new(
            trigger,
            dispatch_began_at_millis,
            deliveries,
        ))
    }

    /// Fail closed unless every [`REQUIRED_CHANNELS`] entry (email + SMS) is
    /// present exactly once. Because the channel enum *is* email + SMS and both
    /// are required, this forces the supplied set to be exactly those two in some
    /// order: a missing one is [`DispatchError::MissingRequiredChannel`], a
    /// repeated one is [`DispatchError::DuplicateChannel`].
    fn validate_required_channels(channels: &[SharedChannelClient]) -> Result<(), DispatchError> {
        for &required in REQUIRED_CHANNELS {
            let count = channels
                .iter()
                .filter(|client| client.channel() == required)
                .count();
            match count {
                0 => return Err(DispatchError::MissingRequiredChannel { channel: required }),
                1 => {}
                _ => return Err(DispatchError::DuplicateChannel { channel: required }),
            }
        }
        Ok(())
    }

    /// Build the operator-facing message from a trigger. Non-secret by
    /// construction: the subject is the severity + trigger kind, the body is the
    /// human summary. No credential, recipient, or provider token appears here
    /// (NFR-S4 keeps those inside the concrete channel adapter).
    fn build_message(trigger: &NotificationTrigger) -> NotificationMessage {
        let subject = format!(
            "[{}] {}",
            trigger.severity().as_str(),
            trigger.kind().as_str()
        );
        let body = format!(
            "{summary} (detected at t={detected}ms)",
            summary = trigger.summary(),
            detected = trigger.detected_at_millis(),
        );
        NotificationMessage::new(subject, body)
    }

    /// Send to one channel under the notifier's `channel_deadline` and map the
    /// real result onto a delivery record. A transport error (including the
    /// adapter's [`crate::channel::ChannelError::Timeout`] when the deadline
    /// elapses) is recorded as [`DeliveryOutcome::Failed`] with the typed error's
    /// kind + detail — never dropped, never turned into a false success — and the
    /// caller's fan-out loop then continues to the other required channel.
    fn send_one(
        &self,
        client: &SharedChannelClient,
        message: &NotificationMessage,
    ) -> ChannelDelivery {
        let channel: NotificationChannel = client.channel();
        match client.send(message, self.channel_deadline) {
            Ok(receipt) => {
                ChannelDelivery::new(channel, DeliveryOutcome::Delivered, receipt.reference())
            }
            Err(error) => ChannelDelivery::new(
                channel,
                DeliveryOutcome::Failed,
                format!("{}: {}", error.kind_str(), error.detail()),
            ),
        }
    }
}
