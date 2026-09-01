//! Operator notification dispatch (SRS-NOTIF-001).
//!
//! The core Rust notification dispatcher (AC-16 / C-12 require it in Rust): it
//! notifies the operator over **email and push** for **IB connectivity loss and
//! critical failures**, begins dispatch within 60 seconds of detection (NFR-P6 /
//! SYS-46), and **stores the delivery status as a notification event** for the
//! operator audit trail. Traces StRS SN-1.12 / SN-2.04, SC-9.
//!
//! ## Module map
//!
//! * [`event`] — the source-neutral domain vocabulary: [`NotificationTrigger`]
//!   (what was detected + the detection instant), [`NotificationEvent`] (the
//!   stored record), [`ChannelDelivery`] (the per-channel outcome — opaque, so a
//!   delivery status cannot be fabricated without a real send).
//! * [`channel`] — the [`NotificationChannelClient`] transport port + the typed
//!   [`ChannelError`] failure taxonomy. The concrete SMTP (IF-10) / ntfy push
//!   (IF-11) adapters live in `atp-adapters::notification`; the core names no
//!   vendor and holds no credential (NFR-S4).
//! * [`dispatcher`] — [`OperatorNotifier`], the detection→dispatch→record
//!   authority. Injected clock (deterministic latency), reversed-timestamp
//!   rejection, required email+push fan-out, and the SYS-75 fail-safe that a
//!   critical failure is never suppressed. It passes a mandatory per-channel
//!   `deadline` to every send; the adapter enforces it via cancellable I/O and
//!   returns a typed timeout, which is recorded `Failed` while the other channel
//!   is still attempted.
//! * [`store`] — [`NotificationEventStore`], the durable append-only audit log
//!   (atomic write + checksummed fail-closed codec).
//!
//! ## Scope (this is the core dispatcher; live delivery is deferred)
//!
//! This crate is the complete, fault-injection-testable core: it proves — with
//! in-process stub channels and an injected clock — that dispatch begins within
//! the 60-second SLA and that the delivery status of every channel is recorded
//! and durably stored. What is **deferred** (and why SRS-NOTIF-001 stays
//! `passes:false` until an operator finishes the integration):
//!
//!   * a REAL destination behind the transports. Both adapters exist in
//!     `atp-adapters::notification` and are verified over real sockets, but the
//!     two now fall short in DIFFERENT ways and the difference matters for what
//!     may be claimed:
//!       * **email (IF-10)** submits to the `phase1-notification-egress`
//!         sidecar, which owns the TLS session to the real SMTP provider.
//!         Neither that sidecar nor a provider account has been stood up, so
//!         email is proven only to the relay boundary.
//!       * **push (IF-11)** posts DIRECTLY to a self-hosted ntfy on the LAN —
//!         there is no relay in the path. It has been driven end to end against
//!         a real ntfy, which accepted the publish and returned its message id.
//!         That is a genuinely stronger hand-off than email's, but acceptance is
//!         still not receipt: it does not prove the operator's phone was
//!         subscribed, online, or that the notification was displayed.
//!
//!     So "email and push are sent" is not yet proven to the OPERATOR on
//!     either channel, for two different reasons;
//!   * an automatic runtime that dispatches without an operator invoking it.
//!     Both detection bindings below are composed and driven by operator CLIs;
//!     `phase1-notification-dispatcher` still runs `core-runtime.Dockerfile`'s
//!     `cargo test` CMD, so no long-running process subscribes to events. That
//!     process needs the SRS-EXE-001 execution runtime (there is no live IB
//!     inbound/streaming surface to subscribe to yet), so it is owned there
//!     rather than stubbed here;
//!   * the `CRITICAL`-severity system-event source (SYS-46 / SYS-61) →
//!     [`NotificationTrigger::critical_failure`]. Note this is a *partial* gap,
//!     not a total one: SRS-SAFE-002's `NotifierAlertSink` already binds the
//!     kill-switch liquidation-timeout critical failure through this dispatcher
//!     over both required channels. What remains unrouted is the general
//!     CRITICAL system-event stream (owners: SRS-LOG-001 for the ERROR/CRITICAL
//!     log filter, SRS-ORCH-003 for workload/health events).
//!
//! What is NO LONGER deferred, and must not be described as such:
//!
//!   * the connectivity-loss detection wiring — `atp-orchestrator`'s
//!     `connectivity_notification::ConnectivityNotifierSink` implements
//!     `atp-execution`'s `ConnectivityEventSink` against this dispatcher, so the
//!     ERR-2 / SRS-SAFE-003 gate now pages;
//!   * the SYS-75 scheduled-restart-window suppression *decision* — that sink
//!     derives it from `ConnectivityState::ScheduledRestartWindow` (requiring the
//!     event's own flag to agree, so a forged flag cannot silence an outage);
//!   * the per-channel cancellable send deadline — the shipped adapters spend it
//!     as one budget across the whole conversation and re-check it between
//!     socket reads (see [`channel`]).
//!
//! Credential encryption at rest stays with NFR-S4 / SRS-SEC-001.
//!
//! The end-to-end proof (real connectivity loss → real email + push delivered →
//! status stored) is the `Fault injection, integration test` method the feature
//! names; it cannot run solo in parallel, so this lands `serialized`.

use atp_types::RuntimeService;

pub mod channel;
pub mod dispatcher;
pub mod event;
pub mod store;

pub use channel::{
    ChannelError, ChannelHandoff, ChannelReceipt, ChannelSendResult, NotificationChannelClient,
    NotificationMessage,
};
pub use dispatcher::{DispatchError, OperatorNotifier, SharedChannelClient, SuppressionReason};
pub use event::{
    ChannelDelivery, DeliveryOutcome, NotificationChannel, NotificationEvent, NotificationSeverity,
    NotificationTrigger, TriggerKind, DISPATCH_SLA_MS, REQUIRED_CHANNELS,
};
pub use store::{NotificationEventStore, NotificationStoreError, NotificationStoreLock};

/// The notification dispatcher runtime-service identity (AC-16). The concrete
/// dispatch authority is [`OperatorNotifier`]; this marker keeps the
/// service-registry identity the orchestrator's readiness check (SYS-76) and the
/// core-service audit consult.
#[derive(Debug, Default)]
pub struct NotificationDispatcher;

impl NotificationDispatcher {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::NotificationDispatcher
    }

    pub fn owns_operator_notifications(&self) -> bool {
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifies_notification_dispatcher() {
        let dispatcher = NotificationDispatcher;
        assert_eq!(dispatcher.service(), RuntimeService::NotificationDispatcher);
        assert!(dispatcher.owns_operator_notifications());
    }
}
