//! SRS-NOTIF-001 integration + fault-injection tests.
//!
//! Drives the [`OperatorNotifier`] dispatcher and the durable
//! [`NotificationEventStore`] through in-process stub channels and an injected
//! clock to prove the acceptance-criterion properties without a live provider:
//!
//!   1. **Dispatch begins within 60 seconds of detection** (NFR-P6 / SYS-46) —
//!      the stored event records the detection→dispatch latency, so the SLA is
//!      measured, not assumed; a breach is recorded evidence, and reversed
//!      timestamps are rejected so the evidence can't be falsified.
//!   2. **Delivery status is stored as a notification event** — every channel's
//!      real outcome (delivered / failed / suppressed) is recorded on the event
//!      and durably round-trips through the store.
//!   3. **Email AND push fan-out is enforced** — the dispatcher fails closed on a
//!      channel set that omits or duplicates a required channel.
//!   4. **A channel's transport timeout can't silence the other** — a channel
//!      that returns a typed `Timeout` error is recorded `Failed` and the other
//!      required channel is still attempted and delivered; the dispatcher also
//!      threads its mandatory per-channel deadline into every send.
//!
//! Fault injection covers a failing channel (transport outage / timeout), the
//! no-fabrication invariant (a failed send is never recorded as delivered), the
//! SYS-75 suppression seam, the "a critical failure is never suppressed"
//! fail-safe, concurrent-writer no-loss, and the fail-closed store codec.

use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use atp_notification::channel::{
    ChannelError, ChannelReceipt, ChannelSendResult, NotificationChannelClient, NotificationMessage,
};
use atp_notification::dispatcher::{
    DispatchError, OperatorNotifier, SharedChannelClient, SuppressionReason,
};
use atp_notification::event::{
    DeliveryOutcome, NotificationChannel, NotificationTrigger, DISPATCH_SLA_MS,
};
use atp_notification::store::{
    NotificationEventStore, NotificationStoreError, NotificationStoreLock, MAGIC, SCHEMA_VERSION,
    STORE_FILENAME,
};

// --------------------------------------------------------------------------- //
// Stub channels (Send + Sync — shared as Arc handles by the composition root)
// --------------------------------------------------------------------------- //

/// A channel that accepts every message, recording the accept id, how many times
/// it was asked to send, and the deadline it was handed (so a test can prove the
/// dispatcher threads its configured deadline into the adapter).
struct AcceptingChannel {
    channel: NotificationChannel,
    reference: String,
    sends: AtomicU32,
    last_deadline_millis: AtomicU64,
}

impl AcceptingChannel {
    fn new(channel: NotificationChannel, reference: &str) -> Self {
        Self {
            channel,
            reference: reference.to_string(),
            sends: AtomicU32::new(0),
            last_deadline_millis: AtomicU64::new(0),
        }
    }
    fn send_count(&self) -> u32 {
        self.sends.load(Ordering::SeqCst)
    }
    fn last_deadline_millis(&self) -> u64 {
        self.last_deadline_millis.load(Ordering::SeqCst)
    }
}

impl NotificationChannelClient for AcceptingChannel {
    fn channel(&self) -> NotificationChannel {
        self.channel
    }
    fn send(&self, _message: &NotificationMessage, deadline: Duration) -> ChannelSendResult {
        self.sends.fetch_add(1, Ordering::SeqCst);
        self.last_deadline_millis
            .store(deadline.as_millis() as u64, Ordering::SeqCst);
        Ok(ChannelReceipt::new(self.reference.clone()))
    }
}

/// A channel that always returns a fixed transport error (fault injection).
struct FailingChannel {
    channel: NotificationChannel,
    error: ChannelError,
    sends: AtomicU32,
}

impl FailingChannel {
    fn new(channel: NotificationChannel, error: ChannelError) -> Self {
        Self {
            channel,
            error,
            sends: AtomicU32::new(0),
        }
    }
    fn send_count(&self) -> u32 {
        self.sends.load(Ordering::SeqCst)
    }
}

impl NotificationChannelClient for FailingChannel {
    fn channel(&self) -> NotificationChannel {
        self.channel
    }
    fn send(&self, _message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
        self.sends.fetch_add(1, Ordering::SeqCst);
        Err(self.error.clone())
    }
}

/// Build the `&[SharedChannelClient]` slice the dispatcher expects from concrete
/// `Arc`s (each `Arc<Concrete>` unsizes to `Arc<dyn ..>`).
fn channels(clients: Vec<SharedChannelClient>) -> Vec<SharedChannelClient> {
    clients
}

// --------------------------------------------------------------------------- //
// AC property 1 — dispatch begins within 60 seconds of detection
// --------------------------------------------------------------------------- //

#[test]
fn dispatch_within_sla_records_both_channels_delivered() {
    let email = Arc::new(AcceptingChannel::new(
        NotificationChannel::Email,
        "smtp-250-abc",
    ));
    let push = Arc::new(AcceptingChannel::new(
        NotificationChannel::Push,
        "ntfy-msg-id-99",
    ));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    // Detected at t=100_000ms, dispatch begins at t=142_000ms — 42s, within budget.
    let trigger = NotificationTrigger::connectivity_loss("IB Gateway unreachable (1100)", 100_000);
    let event = notifier.dispatch(&trigger, 142_000, &set).unwrap();

    assert_eq!(event.dispatch_latency_millis(), 42_000);
    assert!(event.within_dispatch_sla());
    assert!(event.any_delivered());

    let email_delivery = event.delivery_for(NotificationChannel::Email).unwrap();
    assert_eq!(email_delivery.outcome(), DeliveryOutcome::Delivered);
    assert_eq!(email_delivery.detail(), "smtp-250-abc");
    let push_delivery = event.delivery_for(NotificationChannel::Push).unwrap();
    assert_eq!(push_delivery.outcome(), DeliveryOutcome::Delivered);
    assert_eq!(push_delivery.detail(), "ntfy-msg-id-99");

    assert_eq!(email.send_count(), 1);
    assert_eq!(push.send_count(), 1);
}

#[test]
fn dispatch_at_exactly_60000ms_is_within_sla_but_60001ms_is_a_breach() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "ref"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "ref"));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();

    // Exactly 60,000 ms after detection — the NFR-P6 boundary, still within SLA.
    let at_boundary = notifier
        .dispatch(
            &NotificationTrigger::critical_failure("disk full", 1_000),
            1_000 + DISPATCH_SLA_MS,
            &set,
        )
        .unwrap();
    assert_eq!(at_boundary.dispatch_latency_millis(), 60_000);
    assert!(at_boundary.within_dispatch_sla());

    // 60,001 ms — one millisecond over. Millisecond resolution means this is a
    // recorded breach, not rounded down to a passing 60 s.
    let breach = notifier
        .dispatch(
            &NotificationTrigger::critical_failure("disk full", 1_000),
            1_000 + DISPATCH_SLA_MS + 1,
            &set,
        )
        .unwrap();
    assert_eq!(breach.dispatch_latency_millis(), 60_001);
    assert!(
        !breach.within_dispatch_sla(),
        "a dispatch that begins 60,001 ms after detection must record an SLA breach"
    );
    assert!(breach.any_delivered());
}

#[test]
fn reversed_timestamps_are_rejected_so_sla_evidence_cannot_be_falsified() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "e"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "s"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    // Dispatch "began" BEFORE detection — impossible provenance. A saturating
    // latency would otherwise record 0ms and spuriously pass the SLA.
    let result = notifier.dispatch(
        &NotificationTrigger::connectivity_loss("skewed clock", 100),
        50,
        &set,
    );
    assert_eq!(
        result,
        Err(DispatchError::DispatchBeforeDetection {
            detected_at_millis: 100,
            dispatch_began_at_millis: 50,
        })
    );
    // Fail-closed: no channel was contacted, no event produced.
    assert_eq!(email.send_count(), 0);
    assert_eq!(push.send_count(), 0);
}

// --------------------------------------------------------------------------- //
// AC property 2 + fault injection — delivery status reflects reality
// --------------------------------------------------------------------------- //

#[test]
fn failing_channel_is_recorded_failed_never_fabricated_as_delivered() {
    let email = Arc::new(FailingChannel::new(
        NotificationChannel::Email,
        ChannelError::TransportUnavailable {
            detail: "smtp connect timeout".into(),
        },
    ));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "push-ok"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    let event = notifier
        .dispatch(
            &NotificationTrigger::connectivity_loss("IB down", 10),
            20,
            &set,
        )
        .unwrap();

    let email_delivery = event.delivery_for(NotificationChannel::Email).unwrap();
    assert_eq!(
        email_delivery.outcome(),
        DeliveryOutcome::Failed,
        "a channel that returned Err must never be stored as Delivered"
    );
    assert!(email_delivery.detail().contains("TRANSPORT_UNAVAILABLE"));
    assert!(email_delivery.detail().contains("smtp connect timeout"));

    let push_delivery = event.delivery_for(NotificationChannel::Push).unwrap();
    assert_eq!(push_delivery.outcome(), DeliveryOutcome::Delivered);

    assert!(event.any_delivered());
    assert_eq!(email.send_count(), 1);
    assert_eq!(push.send_count(), 1);
}

#[test]
fn every_channel_failing_still_stores_the_event_with_no_delivery() {
    let email = Arc::new(FailingChannel::new(
        NotificationChannel::Email,
        ChannelError::Unconfigured {
            detail: "ATP_SMTP_API_KEY missing".into(),
        },
    ));
    let push = Arc::new(FailingChannel::new(
        NotificationChannel::Push,
        ChannelError::Rejected {
            detail: "invalid recipient".into(),
        },
    ));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();
    let event = notifier
        .dispatch(
            &NotificationTrigger::critical_failure("total outage", 0),
            5,
            &set,
        )
        .unwrap();

    assert!(!event.any_delivered());
    assert_eq!(event.deliveries().len(), 2);
    assert!(event
        .deliveries()
        .iter()
        .all(|d| d.outcome() == DeliveryOutcome::Failed));
}

// --------------------------------------------------------------------------- //
// AC property 3 — email AND push fan-out is enforced, fail-closed
// --------------------------------------------------------------------------- //

#[test]
fn dispatch_rejects_empty_channel_set() {
    let notifier = OperatorNotifier::new();
    let set = channels(vec![]);
    let result = notifier.dispatch(&NotificationTrigger::critical_failure("x", 0), 1, &set);
    assert_eq!(
        result,
        Err(DispatchError::MissingRequiredChannel {
            channel: NotificationChannel::Email
        })
    );
}

#[test]
fn dispatch_rejects_email_only() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "ref"));
    let set = channels(vec![email.clone()]);
    let notifier = OperatorNotifier::new();
    let result = notifier.dispatch(&NotificationTrigger::critical_failure("x", 0), 1, &set);
    assert_eq!(
        result,
        Err(DispatchError::MissingRequiredChannel {
            channel: NotificationChannel::Push
        }),
        "email-only must fail closed — SRS-NOTIF-001 requires push too"
    );
    assert_eq!(
        email.send_count(),
        0,
        "no channel is sent when validation fails"
    );
}

#[test]
fn dispatch_rejects_push_only() {
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "ref"));
    let set = channels(vec![push]);
    let notifier = OperatorNotifier::new();
    let result = notifier.dispatch(&NotificationTrigger::connectivity_loss("x", 0), 1, &set);
    assert_eq!(
        result,
        Err(DispatchError::MissingRequiredChannel {
            channel: NotificationChannel::Email
        })
    );
}

#[test]
fn dispatch_rejects_duplicate_required_channel() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "a"));
    let email_dup = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "b"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "c"));
    let set = channels(vec![email.clone(), email_dup.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();
    let result = notifier.dispatch(&NotificationTrigger::critical_failure("x", 0), 1, &set);
    assert_eq!(
        result,
        Err(DispatchError::DuplicateChannel {
            channel: NotificationChannel::Email
        })
    );
    assert_eq!(email.send_count(), 0);
    assert_eq!(email_dup.send_count(), 0);
    assert_eq!(push.send_count(), 0);
}

// --------------------------------------------------------------------------- //
// AC property 4 — a channel's transport timeout cannot silence the other
// --------------------------------------------------------------------------- //

#[test]
fn channel_timeout_is_recorded_failed_and_other_channel_still_delivers() {
    // Email's adapter hit its own cancellable send deadline and returned the
    // typed Timeout error (the contract behaviour, not a hang).
    let email = Arc::new(FailingChannel::new(
        NotificationChannel::Email,
        ChannelError::Timeout {
            detail: "smtp send exceeded socket deadline".into(),
        },
    ));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "push-ok"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    let event = notifier
        .dispatch(
            &NotificationTrigger::critical_failure("email adapter timed out", 0),
            1,
            &set,
        )
        .unwrap();

    // The timed-out email is a Failed delivery record carrying the typed reason...
    let email_delivery = event.delivery_for(NotificationChannel::Email).unwrap();
    assert_eq!(
        email_delivery.outcome(),
        DeliveryOutcome::Failed,
        "a channel that returned a timeout error must be recorded Failed"
    );
    assert!(email_delivery.detail().contains("TIMEOUT"));
    // ...and push is still attempted + delivered, and the event is produced.
    let push_delivery = event.delivery_for(NotificationChannel::Push).unwrap();
    assert_eq!(push_delivery.outcome(), DeliveryOutcome::Delivered);
    assert!(event.any_delivered());
    assert_eq!(email.send_count(), 1);
    assert_eq!(push.send_count(), 1);
}

#[test]
fn dispatcher_threads_its_configured_deadline_into_every_channel() {
    // The per-channel deadline is a mandatory part of the send API: the
    // dispatcher hands its configured budget to each adapter.
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "e"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "s"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::with_channel_deadline(Duration::from_secs(7));

    notifier
        .dispatch(&NotificationTrigger::critical_failure("x", 0), 1, &set)
        .unwrap();

    assert_eq!(email.last_deadline_millis(), 7_000);
    assert_eq!(push.last_deadline_millis(), 7_000);
    // The default notifier uses the 20s budget.
    assert_eq!(
        OperatorNotifier::new().channel_deadline(),
        OperatorNotifier::DEFAULT_CHANNEL_DEADLINE
    );
}

#[test]
fn over_large_channel_deadline_is_clamped_to_fit_the_sla_budget() {
    // A caller mis-configures a 120s per-channel deadline. With sequential
    // fan-out that would let the first channel eat the whole 60s window before
    // the second is attempted, so it is clamped to MAX_CHANNEL_DEADLINE (30s,
    // = 60s / 2 required channels) so both attempts fit the budget.
    let notifier = OperatorNotifier::with_channel_deadline(Duration::from_secs(120));
    assert_eq!(
        notifier.channel_deadline(),
        OperatorNotifier::MAX_CHANNEL_DEADLINE
    );
    assert_eq!(
        OperatorNotifier::MAX_CHANNEL_DEADLINE,
        Duration::from_secs(30)
    );
    // A value under the cap is preserved.
    assert_eq!(
        OperatorNotifier::with_channel_deadline(Duration::from_secs(5)).channel_deadline(),
        Duration::from_secs(5)
    );
}

// --------------------------------------------------------------------------- //
// SYS-75 suppression seam + the never-suppress-critical fail-safe
// --------------------------------------------------------------------------- //

#[test]
fn connectivity_loss_is_suppressed_during_scheduled_restart_window() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "ref"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "ref"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    let event = notifier
        .dispatch_with_suppression(
            &NotificationTrigger::connectivity_loss("planned restart disconnect", 0),
            1,
            &set,
            Some(SuppressionReason::ScheduledRestartWindow),
        )
        .unwrap();

    assert_eq!(email.send_count(), 0);
    assert_eq!(push.send_count(), 0);
    assert!(!event.any_delivered());
    assert_eq!(event.deliveries().len(), 2);
    assert!(event
        .deliveries()
        .iter()
        .all(|d| d.outcome() == DeliveryOutcome::Suppressed));
    assert!(event
        .delivery_for(NotificationChannel::Email)
        .unwrap()
        .detail()
        .contains("SCHEDULED_RESTART_WINDOW"));
}

#[test]
fn critical_failure_is_never_suppressed_even_when_requested() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "smtp-ok"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "push-ok"));
    let set = channels(vec![email.clone(), push.clone()]);
    let notifier = OperatorNotifier::new();

    let event = notifier
        .dispatch_with_suppression(
            &NotificationTrigger::critical_failure("kill-switch liquidation timeout", 0),
            1,
            &set,
            Some(SuppressionReason::ScheduledRestartWindow),
        )
        .unwrap();

    assert_eq!(
        email.send_count(),
        1,
        "a critical failure must always dispatch, whatever suppression is requested"
    );
    assert_eq!(push.send_count(), 1);
    assert!(event.any_delivered());
    assert_eq!(
        event
            .delivery_for(NotificationChannel::Email)
            .unwrap()
            .outcome(),
        DeliveryOutcome::Delivered
    );
}

// --------------------------------------------------------------------------- //
// End-to-end: detect → dispatch → store → read back
// --------------------------------------------------------------------------- //

#[test]
fn detect_dispatch_store_and_read_back_the_delivery_status() {
    let dir = temp_dir("notif-e2e");
    std::fs::create_dir_all(&dir).unwrap(); // the store directory is provisioned at startup
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "smtp-1"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "push-1"));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();

    let mut store = NotificationEventStore::load_from_path(&dir).unwrap();
    assert!(store.is_empty());

    let trigger = NotificationTrigger::connectivity_loss("IB 1100 lost", 500_000);
    let event = notifier.dispatch(&trigger, 530_000, &set).unwrap();
    store.append(event);
    store.save_to_path(&dir).unwrap();

    let reloaded = NotificationEventStore::load_from_path(&dir).unwrap();
    assert_eq!(reloaded.len(), 1);
    let stored = &reloaded.events()[0];
    assert!(stored.within_dispatch_sla());
    assert_eq!(stored.dispatch_latency_millis(), 30_000);
    assert_eq!(stored.summary(), "IB 1100 lost");
    assert_eq!(
        stored
            .delivery_for(NotificationChannel::Email)
            .unwrap()
            .outcome(),
        DeliveryOutcome::Delivered
    );
    assert_eq!(
        stored
            .delivery_for(NotificationChannel::Push)
            .unwrap()
            .detail(),
        "push-1"
    );

    cleanup(&dir);
}

#[test]
fn store_round_trips_many_events_in_insertion_order() {
    let dir = temp_dir("notif-order");
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "e"));
    let push = Arc::new(FailingChannel::new(
        NotificationChannel::Push,
        ChannelError::TransportUnavailable { detail: "x".into() },
    ));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();

    let mut store = NotificationEventStore::new();
    for i in 0..5u64 {
        let trigger = NotificationTrigger::critical_failure(format!("failure #{i}"), i * 10);
        store.append(notifier.dispatch(&trigger, i * 10 + 3, &set).unwrap());
    }
    store.save_to_path(&dir).unwrap();

    let reloaded = NotificationEventStore::load_from_path(&dir).unwrap();
    assert_eq!(reloaded.len(), 5);
    for (i, event) in reloaded.events().iter().enumerate() {
        assert_eq!(event.summary(), format!("failure #{i}"));
        assert_eq!(event.detected_at_millis(), i as u64 * 10);
    }
    assert_eq!(store.serialize(), reloaded.serialize());

    cleanup(&dir);
}

// --------------------------------------------------------------------------- //
// Concurrent writers — the audit trail must not lose events
// --------------------------------------------------------------------------- //

#[test]
fn concurrent_appends_do_not_lose_events() {
    use std::thread;
    let dir = temp_dir("notif-concurrent");
    std::fs::create_dir_all(&dir).unwrap();
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "e"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "s"));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();

    let event_a = notifier
        .dispatch(
            &NotificationTrigger::connectivity_loss("source A", 0),
            1,
            &set,
        )
        .unwrap();
    let event_b = notifier
        .dispatch(
            &NotificationTrigger::critical_failure("source B", 0),
            1,
            &set,
        )
        .unwrap();

    let dir_a = dir.clone();
    let dir_b = dir.clone();
    let handle_a = thread::spawn(move || NotificationEventStore::append_durably(&dir_a, event_a));
    let handle_b = thread::spawn(move || NotificationEventStore::append_durably(&dir_b, event_b));
    handle_a.join().unwrap().unwrap();
    handle_b.join().unwrap().unwrap();

    let store = NotificationEventStore::load_from_path(&dir).unwrap();
    assert_eq!(
        store.len(),
        2,
        "both concurrently-appended events must be retained"
    );
    let summaries: Vec<&str> = store.events().iter().map(|e| e.summary()).collect();
    assert!(
        summaries.contains(&"source A"),
        "lost source A: {summaries:?}"
    );
    assert!(
        summaries.contains(&"source B"),
        "lost source B: {summaries:?}"
    );

    cleanup(&dir);
}

#[test]
fn held_lock_refuses_a_second_writer_then_releases_on_drop() {
    let dir = temp_dir("notif-lock");
    std::fs::create_dir_all(&dir).unwrap();

    let lock = NotificationStoreLock::acquire(&dir).unwrap();
    match NotificationStoreLock::acquire(&dir) {
        Err(NotificationStoreError::Locked) => {}
        other => panic!("a held lock must refuse a second writer, got {other:?}"),
    }
    drop(lock);
    let reacquired = NotificationStoreLock::acquire(&dir).unwrap();
    drop(reacquired);

    cleanup(&dir);
}

// --------------------------------------------------------------------------- //
// Fail-closed store codec
// --------------------------------------------------------------------------- //

#[test]
fn corrupt_blob_fails_closed_with_checksum_mismatch() {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "e"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "s"));
    let set = channels(vec![email, push]);
    let notifier = OperatorNotifier::new();
    let mut store = NotificationEventStore::new();
    store.append(
        notifier
            .dispatch(&NotificationTrigger::connectivity_loss("x", 0), 1, &set)
            .unwrap(),
    );

    let mut bytes = store.serialize().into_bytes();
    let idx = bytes.len() - 3;
    bytes[idx] ^= 0xFF;
    let corrupted = String::from_utf8_lossy(&bytes).to_string();
    match NotificationEventStore::restore(&corrupted) {
        Err(NotificationStoreError::ChecksumMismatch) => {}
        other => panic!("expected ChecksumMismatch, got {other:?}"),
    }
}

#[test]
fn foreign_blob_and_missing_directory_fail_closed() {
    match NotificationEventStore::restore("SOME-OTHER-FORMAT\n0\n1\n0\n") {
        Err(NotificationStoreError::Corrupt { .. }) => {}
        other => panic!("expected Corrupt magic, got {other:?}"),
    }
    let missing = temp_dir("notif-missing").join("does-not-exist");
    match NotificationEventStore::load_from_path(&missing) {
        Err(NotificationStoreError::Io { .. }) => {}
        other => panic!("expected Io for a missing dir, got {other:?}"),
    }
}

#[test]
fn empty_provisioned_directory_restores_empty_not_error() {
    let dir = temp_dir("notif-fresh");
    std::fs::create_dir_all(&dir).unwrap();
    let store = NotificationEventStore::load_from_path(&dir).unwrap();
    assert!(store.is_empty());
    cleanup(&dir);
}

// A checksum-VALID blob whose *contents* are semantically impossible must still
// fail closed on restore (read↔write validation symmetry). These craft a blob
// with a correct FNV-1a checksum but invalid contents.

fn fnv1a(bytes: &[u8]) -> u64 {
    let mut hash: u64 = 0xcbf29ce484222325;
    for &byte in bytes {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

/// Craft a single-event store blob with a correct checksum. `trigger_tag` is
/// "C" (connectivity loss) or "F" (critical failure); `deliveries` are
/// (channel_tag, outcome_tag, detail) triples.
fn craft_event_blob(
    trigger_tag: &str,
    detected_millis: u64,
    dispatch_began_millis: u64,
    deliveries: &[(&str, &str, &str)],
) -> String {
    fn push_lp(s: &mut String, value: &str) {
        s.push_str(&value.len().to_string());
        s.push('\n');
        s.push_str(value);
        s.push('\n');
    }
    let mut body = String::new();
    // Built at the CURRENT schema version on purpose: these tests exercise the
    // SEMANTIC restore rejections, so pinning a literal version here would turn
    // them into version-mismatch tests the moment the schema is bumped, and
    // silently stop proving that a checksum-valid blob cannot lie.
    body.push_str(&format!("{SCHEMA_VERSION}\n")); // schema version
    body.push_str("1\n"); // event count
    body.push_str(trigger_tag);
    body.push('\n'); // trigger tag
    push_lp(&mut body, "x"); // summary
    body.push_str(&format!("{detected_millis}\n"));
    body.push_str(&format!("{dispatch_began_millis}\n"));
    body.push_str(&format!("{}\n", deliveries.len()));
    for (channel, outcome, detail) in deliveries {
        body.push_str(channel);
        body.push('\n');
        body.push_str(outcome);
        body.push('\n');
        push_lp(&mut body, detail);
    }
    format!("{MAGIC}\n{}\n{}", fnv1a(body.as_bytes()), body)
}

#[test]
fn restore_rejects_checksum_valid_blob_with_reversed_timestamps() {
    // Sanity: a well-formed crafted blob restores.
    let ok = craft_event_blob("C", 100, 200, &[("E", "D", "ok"), ("P", "D", "ok")]);
    assert_eq!(NotificationEventStore::restore(&ok).unwrap().len(), 1);

    // Reversed timestamps (dispatch before detection) — impossible SLA evidence.
    let reversed = craft_event_blob("C", 200, 100, &[("E", "D", "ok"), ("P", "D", "ok")]);
    match NotificationEventStore::restore(&reversed) {
        Err(NotificationStoreError::Corrupt { .. }) => {}
        other => panic!("expected Corrupt for reversed timestamps, got {other:?}"),
    }
}

#[test]
fn restore_rejects_checksum_valid_blob_missing_a_required_channel() {
    // Only email present — a real dispatch always fans out email + push.
    let missing = craft_event_blob("C", 100, 200, &[("E", "D", "ok")]);
    match NotificationEventStore::restore(&missing) {
        Err(NotificationStoreError::Corrupt { .. }) => {}
        other => panic!("expected Corrupt for a missing required channel, got {other:?}"),
    }
}

#[test]
fn restore_rejects_suppressed_critical_failure() {
    // A CRITICAL failure is never suppressed (SYS-75 fail-safe). A blob claiming a
    // critical failure with SUPPRESSED ("X") deliveries is impossible provenance —
    // restoring it would falsely say the system chose not to notify.
    let suppressed_critical =
        craft_event_blob("F", 100, 200, &[("E", "X", "win"), ("S", "X", "win")]);
    match NotificationEventStore::restore(&suppressed_critical) {
        Err(NotificationStoreError::Corrupt { .. }) => {}
        other => panic!("expected Corrupt for a suppressed critical failure, got {other:?}"),
    }
}

#[test]
fn restore_rejects_mixed_suppression() {
    // A suppressed dispatch suppresses EVERY required channel (all-or-nothing);
    // a mix of suppressed + sent is impossible provenance.
    let mixed = craft_event_blob("C", 100, 200, &[("E", "X", "win"), ("P", "D", "ok")]);
    match NotificationEventStore::restore(&mixed) {
        Err(NotificationStoreError::Corrupt { .. }) => {}
        other => panic!("expected Corrupt for mixed suppression, got {other:?}"),
    }
}

// --------------------------------------------------------------------------- //
// Test helpers
// --------------------------------------------------------------------------- //

fn temp_dir(tag: &str) -> std::path::PathBuf {
    let mut dir = std::env::temp_dir();
    use std::sync::atomic::AtomicU64;
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let seq = SEQ.fetch_add(1, Ordering::Relaxed);
    dir.push(format!("atp-{tag}-{}-{seq}", std::process::id()));
    dir
}

fn cleanup(dir: &std::path::Path) {
    let _ = std::fs::remove_dir_all(dir);
}

/// A store too OLD to read must not block the NEXT alert from being stored.
///
/// Found by adversarial review. `append_durably` loads the existing store before
/// appending, so raising MIN_SUPPORTED_SCHEMA_VERSION to 2 meant a pre-v2 file on
/// disk made every subsequent append fail — an upgraded deployment carrying
/// earlier notification evidence could not record the next operator page at all.
/// On an alert path that is the worst available outcome.
#[test]
fn an_obsolete_store_is_superseded_so_the_next_alert_can_still_be_stored() {
    let dir = temp_dir("notif-obsolete");
    std::fs::create_dir_all(&dir).unwrap();
    // A v1 blob: the old schema version, with the old "S" (SMS) channel tag.
    let v1 = {
        let mut body = String::new();
        body.push_str("1\n1\nC\n");
        body.push_str("1\nx\n");
        body.push_str("100\n200\n2\n");
        body.push_str("E\nD\n2\nok\n");
        body.push_str("S\nD\n2\nok\n");
        format!("{MAGIC}\n{}\n{}", fnv1a(body.as_bytes()), body)
    };
    let live = dir.join(STORE_FILENAME);
    std::fs::write(&live, &v1).expect("write v1 store");

    // Reading it directly still refuses, loudly and by VERSION.
    match NotificationEventStore::load_from_path(&dir) {
        Err(NotificationStoreError::UnknownSchemaVersion { found: 1 }) => {}
        other => panic!("expected UnknownSchemaVersion{{1}}, got {other:?}"),
    }

    // But an append succeeds: the obsolete file is moved aside, not deleted.
    NotificationEventStore::append_durably(&dir, one_event())
        .expect("the next alert must be storable");

    let retired = dir.join(format!("{STORE_FILENAME}.v1.superseded"));
    assert!(
        retired.is_file(),
        "the old evidence must be kept, not deleted"
    );
    assert_eq!(
        std::fs::read_to_string(&retired).expect("read retired"),
        v1,
        "the retired bytes must be untouched"
    );
    let reloaded = NotificationEventStore::load_from_path(&dir).expect("fresh store reads back");
    assert_eq!(
        reloaded.len(),
        1,
        "exactly the new event — v1 is NOT migrated"
    );
}

/// Corruption must STILL fail closed — only a too-old version is superseded.
///
/// The distinction is the whole point: a bad checksum means tampering or disk
/// rot, and moving that evidence out of the way to keep writing would be exactly
/// the silent-loss behaviour the store is built to prevent.
#[test]
fn a_corrupt_store_is_never_superseded_it_still_fails_closed() {
    let dir = temp_dir("notif-corrupt-keep");
    std::fs::create_dir_all(&dir).unwrap();
    let body = "2\n0\n";
    // Valid magic + structure, deliberately WRONG checksum.
    std::fs::write(
        dir.join(STORE_FILENAME),
        format!("{MAGIC}\n{}\n{}", 12345_u64, body),
    )
    .expect("write corrupt store");

    match NotificationEventStore::append_durably(&dir, one_event()) {
        Err(NotificationStoreError::ChecksumMismatch) => {}
        other => panic!("corruption must fail closed, got {other:?}"),
    }
    assert!(
        !dir.join(format!("{STORE_FILENAME}.v2.superseded")).exists(),
        "corruption must never be moved aside"
    );
    assert!(
        dir.join(STORE_FILENAME).is_file(),
        "the corrupt file stays put"
    );
}

/// One real dispatched event, for the store tests above.
fn one_event() -> atp_notification::NotificationEvent {
    let email = Arc::new(AcceptingChannel::new(NotificationChannel::Email, "smtp-1"));
    let push = Arc::new(AcceptingChannel::new(NotificationChannel::Push, "ntfy-1"));
    let set = channels(vec![email, push]);
    let trigger = NotificationTrigger::connectivity_loss("IB gateway unreachable", 500_000);
    OperatorNotifier::new()
        .dispatch(&trigger, 510_000, &set)
        .expect("dispatch")
}
