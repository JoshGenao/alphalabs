//! Notification domain types (SRS-NOTIF-001).
//!
//! SRS-NOTIF-001 ("notify the operator through email and SMS for IB
//! connectivity loss and critical failures") requires the platform to, within
//! 60 seconds of *detecting* a connectivity loss or a critical system failure,
//! begin dispatching an operator notification over **both** email and SMS, and
//! to **store the delivery status as a notification event**. Traces SyRS SYS-46
//! (the 60-second notify obligation), NFR-P6 (the ≤ 60,000 ms dispatch budget),
//! NFR-S4 (channel credentials encrypted / never logged); StRS SN-1.12 (multi-
//! channel operator notification), SN-2.04 (connectivity fail-safes).
//!
//! This module owns the **source-neutral domain vocabulary**: what was detected
//! ([`NotificationTrigger`] / [`TriggerKind`]), how severe it is
//! ([`NotificationSeverity`]), which channels a notification fans out to
//! ([`NotificationChannel`]), the per-channel delivery outcome
//! ([`ChannelDelivery`] / [`DeliveryOutcome`]), and the stored record itself
//! ([`NotificationEvent`]). The dispatch logic lives in
//! [`crate::dispatcher`]; the channel transport port in [`crate::channel`]; the
//! durable event store in [`crate::store`].
//!
//! ## No fabricated delivery status (the integrity invariant)
//!
//! The acceptance criterion is that the *delivery status* is stored. A stored
//! "delivered" that never corresponded to a real channel send would be a lie to
//! the operator — the exact failure the SDK-boundary / callback-authority
//! pattern guards against elsewhere in the tree. So [`ChannelDelivery`] is an
//! **opaque** newtype over a private carrier: it cannot be constructed outside
//! this crate, and even inside the crate only [`crate::dispatcher`] mints one —
//! from the *actual* [`crate::channel::ChannelSendResult`] returned by a real
//! channel send. "No delivery status without a real send attempt" therefore
//! holds by construction, not by convention.
//!
//! ## No secrets on the wire form
//!
//! A [`NotificationEvent`] carries only operator-facing metadata (what failed,
//! when, which channels, the delivery outcome + a short non-secret detail
//! string). It never carries an SMTP password, SMS gateway API key, or message
//! body that could embed one — NFR-S4 keeps channel credentials out of logs and
//! out of the stored event. The credential lives only inside the concrete
//! channel adapter (deferred to the SRS-NOTIF-001 transport adapters).

use core::fmt;

/// The class of condition that triggered an operator notification
/// (SRS-NOTIF-001). The AC names exactly two: IB **connectivity loss** and
/// **critical failures**. Kept `Copy` so a consumer can bucket / count triggers
/// without cloning.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum TriggerKind {
    /// Connectivity to IB Gateway was lost (SyRS SYS-45 / SYS-46, SRS-SAFE-003).
    /// Dispatched at [`NotificationSeverity::Error`] — recoverable via reconnect
    /// but order submission is blocked until it clears.
    ConnectivityLoss,
    /// A critical system failure (SyRS SYS-46 "any critical system failure").
    /// Dispatched at [`NotificationSeverity::Critical`].
    CriticalFailure,
}

impl TriggerKind {
    /// Stable wire string — the cross-surface vocabulary shared with the
    /// dashboard alert pane, the REST `/api/v1/alerts` shape, and the durable
    /// store codec. Never localized.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ConnectivityLoss => "IB_CONNECTIVITY_LOSS",
            Self::CriticalFailure => "CRITICAL_FAILURE",
        }
    }

    /// The severity a notification for this trigger is raised at. Connectivity
    /// loss is an `ERROR` (recoverable); a critical failure is `CRITICAL`. Both
    /// are in the dispatch-worthy subset the notification subsystem subscribes
    /// to (the Python log dispatcher's ERROR/CRITICAL filter, SYS-61).
    pub const fn severity(self) -> NotificationSeverity {
        match self {
            Self::ConnectivityLoss => NotificationSeverity::Error,
            Self::CriticalFailure => NotificationSeverity::Critical,
        }
    }
}

/// The dispatch-worthy subset of the SyRS SYS-61 severity vocabulary
/// (`DEBUG < INFO < WARN < ERROR < CRITICAL`). Only `ERROR` and `CRITICAL`
/// records trigger operator notification (the notification subsystem subscribes
/// to that filtered stream), so those are the only two a `NotificationEvent`
/// can carry. The wire strings match the Python `Severity` enum member values
/// one-for-one (cross-surface vocabulary, SYS-61).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum NotificationSeverity {
    Error,
    Critical,
}

impl NotificationSeverity {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Error => "ERROR",
            Self::Critical => "CRITICAL",
        }
    }
}

/// A channel a notification fans out to (SRS-NOTIF-001). Phase 1 is email and
/// SMS (StRS SN-1.12; push / Telegram / Discord are explicitly future phases).
/// The wire strings feed the durable store codec and the `/api/v1/alerts`
/// `channel` field.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum NotificationChannel {
    Email,
    Sms,
}

impl NotificationChannel {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Email => "EMAIL",
            Self::Sms => "SMS",
        }
    }
}

/// The two channels SRS-NOTIF-001 requires every operator notification to reach
/// ("email **and** SMS"). The dispatcher fans out to exactly these unless a
/// caller narrows the set. Kept as a constant so the "both channels" obligation
/// is a single source of truth.
pub const REQUIRED_CHANNELS: &[NotificationChannel] =
    &[NotificationChannel::Email, NotificationChannel::Sms];

/// What was detected, with the **detection instant** the ≤ 60,000 ms dispatch
/// SLA is measured against (NFR-P6). `detected_at_millis` is an epoch-**millisecond**
/// instant supplied by the caller's clock — millisecond resolution because NFR-P6
/// is specified in milliseconds (≤ 60,000 ms), so a whole-second store would let a
/// 60,001–60,999 ms dispatch round down and wrongly pass. The core takes no
/// `SystemTime` read of its own so dispatch latency is deterministic and testable.
/// The `summary` is a short operator-facing description (e.g. "IB Gateway
/// unreachable: 1100 connectivity lost"); it must not embed a secret.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotificationTrigger {
    kind: TriggerKind,
    summary: String,
    detected_at_millis: u64,
}

impl NotificationTrigger {
    /// Build a connectivity-loss trigger detected at `detected_at_millis`.
    /// The real detection is owned upstream by the execution engine's
    /// connectivity gate (ERR-2 / SRS-SAFE-003), which publishes a
    /// `ConnectivityEvent`; this constructor is the seam that turns that
    /// detection into a notification trigger.
    pub fn connectivity_loss(summary: impl Into<String>, detected_at_millis: u64) -> Self {
        Self {
            kind: TriggerKind::ConnectivityLoss,
            summary: summary.into(),
            detected_at_millis,
        }
    }

    /// Build a critical-failure trigger detected at `detected_at_millis`. The
    /// real detection source is any `CRITICAL`-severity system event (SYS-46 /
    /// SYS-61) — kill-switch liquidation timeout, resource-margin breach, an
    /// unrecoverable service fault — routed here by the composition root.
    pub fn critical_failure(summary: impl Into<String>, detected_at_millis: u64) -> Self {
        Self {
            kind: TriggerKind::CriticalFailure,
            summary: summary.into(),
            detected_at_millis,
        }
    }

    pub fn kind(&self) -> TriggerKind {
        self.kind
    }

    pub fn summary(&self) -> &str {
        &self.summary
    }

    pub fn detected_at_millis(&self) -> u64 {
        self.detected_at_millis
    }

    pub fn severity(&self) -> NotificationSeverity {
        self.kind.severity()
    }
}

/// The outcome of a single channel send. `Delivered` means the channel adapter
/// *accepted the message for delivery* (an SMTP `250 OK`, an SMS gateway accept
/// receipt) — it is a hand-off acknowledgement, not a proof the operator's
/// phone rang; end-to-end read receipts are out of the Phase-1 baseline.
/// `Failed` means the send attempt returned a transport error, and the paired
/// detail names it. `Suppressed` means the channel was intentionally not sent
/// (SYS-75 scheduled-restart-window suppression) — recorded so the stored event
/// distinguishes "we chose not to send" from "we tried and it failed".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum DeliveryOutcome {
    Delivered,
    Failed,
    Suppressed,
}

impl DeliveryOutcome {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Delivered => "DELIVERED",
            Self::Failed => "FAILED",
            Self::Suppressed => "SUPPRESSED",
        }
    }

    /// True only for a real, successful hand-off to the channel. The dispatcher
    /// and dashboard use this to compute "did at least one channel deliver".
    pub const fn is_delivered(self) -> bool {
        matches!(self, Self::Delivered)
    }
}

/// A per-channel delivery record — **opaque**: the inner carrier and its fields
/// are private, so a `ChannelDelivery` cannot be constructed outside this crate,
/// and [`crate::dispatcher`] is the only place inside the crate that mints one,
/// from the *actual* result of a channel send (see the module-level integrity
/// note). This is what makes "no stored delivery status without a real send
/// attempt" hold by construction.
///
/// It carries the channel, the outcome, and a short non-secret `detail` (the
/// accept receipt id on success, the transport-error reason on failure, the
/// suppression reason when suppressed). It deliberately carries **no per-channel
/// timestamp**: all channels of a dispatch are fanned out in one pass whose
/// single, honest anchor is the event's
/// [`dispatch_began_at_millis`](NotificationEvent::dispatch_began_at_millis).
/// A per-channel "attempted at" would be false precision — the core takes no
/// per-channel clock read (the injected-clock discipline), so a made-up
/// per-channel time could misrepresent a late channel as on-time.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ChannelDelivery {
    inner: ChannelDeliveryInner,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ChannelDeliveryInner {
    channel: NotificationChannel,
    outcome: DeliveryOutcome,
    detail: String,
}

impl ChannelDelivery {
    /// Crate-internal constructor. Not `pub`: only [`crate::dispatcher`] (same
    /// crate) may build a delivery record, and only from a real send outcome.
    pub(crate) fn new(
        channel: NotificationChannel,
        outcome: DeliveryOutcome,
        detail: impl Into<String>,
    ) -> Self {
        Self {
            inner: ChannelDeliveryInner {
                channel,
                outcome,
                detail: detail.into(),
            },
        }
    }

    pub fn channel(&self) -> NotificationChannel {
        self.inner.channel
    }

    pub fn outcome(&self) -> DeliveryOutcome {
        self.inner.outcome
    }

    pub fn detail(&self) -> &str {
        &self.inner.detail
    }
}

/// The stored notification event (SRS-NOTIF-001's "delivery status is stored as
/// a notification event"). Matches the data-dictionary shape — event type,
/// timestamp, channels dispatched (email, SMS), delivery status — and aligns
/// with the deferred `/api/v1/alerts` response fields (`raised_at`, `severity`,
/// `channel`, `delivery_status`).
///
/// **Opaque**, same rationale as [`ChannelDelivery`]: only [`crate::dispatcher`]
/// mints one, binding `dispatch_began_at_millis` and the `deliveries` to a real
/// dispatch pass. The `dispatch_began_at_millis` is the anchor for the NFR-P6
/// SLA: [`Self::dispatch_latency_millis`] = it minus the trigger's detection
/// instant, and [`Self::within_dispatch_sla`] is that latency ≤ 60,000 ms.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NotificationEvent {
    inner: NotificationEventInner,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct NotificationEventInner {
    trigger_kind: TriggerKind,
    severity: NotificationSeverity,
    summary: String,
    detected_at_millis: u64,
    dispatch_began_at_millis: u64,
    deliveries: Vec<ChannelDelivery>,
}

/// The NFR-P6 dispatch budget in **milliseconds** (≤ 60,000 ms). Stored and
/// compared at millisecond resolution so a 60,001–60,999 ms dispatch is a
/// recorded breach, not rounded down to a passing 60 s. A dispatch that begins
/// within this many ms of detection meets SYS-46 / SC-9.
pub const DISPATCH_SLA_MS: u64 = 60_000;

impl NotificationEvent {
    /// Crate-internal constructor. Not `pub`: only [`crate::dispatcher`] builds
    /// an event, from a real trigger + the dispatch instant + the actual
    /// per-channel deliveries.
    pub(crate) fn new(
        trigger: &NotificationTrigger,
        dispatch_began_at_millis: u64,
        deliveries: Vec<ChannelDelivery>,
    ) -> Self {
        Self {
            inner: NotificationEventInner {
                trigger_kind: trigger.kind(),
                severity: trigger.severity(),
                summary: trigger.summary().to_string(),
                detected_at_millis: trigger.detected_at_millis(),
                dispatch_began_at_millis,
                deliveries,
            },
        }
    }

    pub fn trigger_kind(&self) -> TriggerKind {
        self.inner.trigger_kind
    }

    pub fn severity(&self) -> NotificationSeverity {
        self.inner.severity
    }

    pub fn summary(&self) -> &str {
        &self.inner.summary
    }

    pub fn detected_at_millis(&self) -> u64 {
        self.inner.detected_at_millis
    }

    pub fn dispatch_began_at_millis(&self) -> u64 {
        self.inner.dispatch_began_at_millis
    }

    /// The per-channel delivery records, in the order the dispatcher attempted
    /// them (email before SMS, the canonical `REQUIRED_CHANNELS` order).
    pub fn deliveries(&self) -> &[ChannelDelivery] {
        &self.inner.deliveries
    }

    /// Milliseconds between detection and the start of dispatch — the quantity
    /// NFR-P6 bounds. Saturating: a `dispatch_began_at` that (impossibly)
    /// precedes detection yields 0 rather than underflowing. The dispatcher
    /// rejects such an ordering before ever constructing the event, but the
    /// accessor stays total.
    pub fn dispatch_latency_millis(&self) -> u64 {
        self.inner
            .dispatch_began_at_millis
            .saturating_sub(self.inner.detected_at_millis)
    }

    /// True when dispatch began within the NFR-P6 / SYS-46 60,000 ms budget.
    /// This is the measurable half of the acceptance criterion — the stored
    /// event records the millisecond latency, so a breach (including 60,001 ms)
    /// is evidence, not a silent miss.
    pub fn within_dispatch_sla(&self) -> bool {
        self.dispatch_latency_millis() <= DISPATCH_SLA_MS
    }

    /// The lookup for a specific channel's delivery record, if that channel was
    /// part of this dispatch.
    pub fn delivery_for(&self, channel: NotificationChannel) -> Option<&ChannelDelivery> {
        self.inner
            .deliveries
            .iter()
            .find(|delivery| delivery.channel() == channel)
    }

    /// True when at least one channel accepted the message for delivery. A
    /// notification with every channel failed is still stored (the operator
    /// needs the evidence), but this predicate lets a caller escalate.
    pub fn any_delivered(&self) -> bool {
        self.inner
            .deliveries
            .iter()
            .any(|delivery| delivery.outcome().is_delivered())
    }
}

impl fmt::Display for NotificationEvent {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "[{}] {} ({}ms→dispatch): {}",
            self.severity().as_str(),
            self.trigger_kind().as_str(),
            self.dispatch_latency_millis(),
            self.summary(),
        )
    }
}
