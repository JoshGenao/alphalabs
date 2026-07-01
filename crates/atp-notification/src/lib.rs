//! Operator notification dispatch (SRS-NOTIF-001).
//!
//! The core Rust notification dispatcher (AC-16 / C-12 require it in Rust): it
//! notifies the operator over **email and SMS** for **IB connectivity loss and
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
//!   [`ChannelError`] failure taxonomy. Concrete SMTP / SMS gateway adapters
//!   live in `atp-adapters` (deferred with the real end-to-end integration); the
//!   core names no vendor and holds no credential (NFR-S4).
//! * [`dispatcher`] — [`OperatorNotifier`], the detection→dispatch→record
//!   authority. Injected clock (deterministic latency), reversed-timestamp
//!   rejection, required email+SMS fan-out, and the SYS-75 fail-safe that a
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
//!   * the concrete SMTP email adapter (IF-10) and SMS gateway adapter (IF-11),
//!     reading `ATP_SMTP_API_KEY` / `ATP_SMS_API_KEY` — they live in
//!     `atp-adapters` and require a real provider to verify end to end;
//!   * the real detection wiring: the execution engine's connectivity gate
//!     (ERR-2 / SRS-SAFE-003) → [`NotificationTrigger::connectivity_loss`], and
//!     `CRITICAL`-severity system events (SYS-46 / SYS-61) →
//!     [`NotificationTrigger::critical_failure`], bound at the composition root;
//!   * the SYS-75 scheduled-restart-window suppression *decision*
//!     (SRS-MD-005) — the dispatcher honours a [`dispatcher::SuppressionReason`],
//!     but the window logic is that feature's;
//!   * credential encryption at rest (NFR-S4 / SRS-SEC-001).
//!
//! The end-to-end proof (real connectivity loss → real email + SMS delivered →
//! status stored) is the `Fault injection, integration test` method the feature
//! names; it cannot run solo in parallel, so this lands `serialized`.

use atp_types::RuntimeService;

pub mod channel;
pub mod dispatcher;
pub mod event;
pub mod store;

pub use channel::{
    ChannelError, ChannelReceipt, ChannelSendResult, NotificationChannelClient, NotificationMessage,
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
