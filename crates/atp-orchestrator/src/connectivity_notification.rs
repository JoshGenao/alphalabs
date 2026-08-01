//! SRS-NOTIF-001 detection wiring — IB connectivity loss → operator alert.
//!
//! The dispatcher has always been able to *send* a connectivity-loss alert; what
//! was missing is anything that *detects* one. `atp-execution` already produces
//! the fact: every live submission it blocks records a
//! [`ConnectivityEvent`] through its [`ConnectivityEventSink`] port (the ERR-2 /
//! SRS-SAFE-003 gate in `ExecutionEngine::submit_live_order`). Nothing
//! implemented that port against the notifier.
//!
//! [`ConnectivityNotifierSink`] is that implementation, and it lives **here** —
//! at the composition root — for the SRS-ARCH-002 reason the port's own doc
//! gives: `atp-execution` must not depend on `atp-notification`. This is the same
//! shape as [`crate::kill_switch_timeout::NotifierAlertSink`], which already
//! binds the SRS-SAFE-002 critical-failure path to the same notifier.
//!
//! ## The SYS-75 suppression decision is made here, from the STATE
//!
//! [`ConnectivityState::ScheduledRestartWindow`] *is* the SYS-75 scheduled IB
//! Gateway restart window: a planned disconnect, not a fault, and the one case
//! SRS-MD-005 says to silence. That makes the suppression decision available at
//! this sink with no extra plumbing.
//!
//! [`ConnectivityEvent`] carries **both** a `state` and a `scheduled_restart`
//! boolean, and they can disagree. A bare boolean that silences an operator
//! alert is the forgeable-input shape: whoever constructs the event could
//! suppress a genuine outage by setting it. So suppression requires **both** to
//! agree, and any disagreement notifies. For an alert path the safe direction is
//! to page, never to stay quiet — an extra alert is noise, a missed one is an
//! unnoticed outage.
//!
//! ## Why there is a cool-down
//!
//! The sink is called once per *blocked order*, not once per outage. A strategy
//! retrying into a dead gateway would drive one real SMTP conversation and one
//! real SMS per attempt — an alert storm that pages the operator hundreds of
//! times, burns the SMS budget, and buries the first (useful) alert.
//!
//! So an outage notifies once, and further events inside
//! [`ConnectivityNotifierSink::COOLDOWN`] are **coalesced** rather than
//! dispatched. Coalescing is never silent: the count of suppressed-by-cool-down
//! events is carried in the *next* alert's summary and exposed through
//! [`ConnectivityNotifierSink::coalesced_since_last_dispatch`], so the operator
//! can tell "one blocked order" from "four hundred". A `Connected` observation
//! ends the episode and re-arms immediate notification.

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use atp_execution::ConnectivityEventSink;
use atp_notification::{
    NotificationEvent, NotificationEventStore, NotificationTrigger, OperatorNotifier,
    SharedChannelClient, SuppressionReason,
};
use atp_types::{ConnectivityEvent, ConnectivityState};

/// Millisecond wall clock, injected so the SLA arithmetic is deterministic under
/// test — the same discipline the dispatcher applies to its own clock.
pub trait AlertClock {
    fn now_millis(&self) -> u64;
}

/// The production clock.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemAlertClock;

impl AlertClock for SystemAlertClock {
    fn now_millis(&self) -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|since| since.as_millis().min(u128::from(u64::MAX)) as u64)
            .unwrap_or(0)
    }
}

/// What happened on a `record` call — retained for the operator surface and for
/// tests, since the port's `record` returns `()`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConnectivityAlertOutcome {
    /// A notification was dispatched; the stored event carries the per-channel
    /// delivery status.
    Dispatched(Box<NotificationEvent>),
    /// Inside the cool-down window; folded into the next dispatch's summary.
    Coalesced { since_last_millis: u64 },
    /// The gate reported a healthy state; nothing to alert about.
    NotAnOutage,
    /// The dispatch itself failed (both transports down, a mis-wired channel
    /// set, or a durable-store failure). Retained, never panicked — this runs
    /// inside the execution engine's rejection path.
    Failed { detail: String },
}

/// One class's rate-limit state.
#[derive(Debug, Default)]
struct DispatchWindow {
    last_dispatch_millis: Option<u64>,
    coalesced: u64,
}

/// Rate-limit state, split by class.
///
/// Genuine outages and planned-maintenance records get **independent** windows.
/// Sharing one lets a suppressed maintenance record — which sends nothing — arm
/// the window that a real outage would have paged through.
#[derive(Debug, Default)]
struct EpisodeState {
    outage: DispatchWindow,
    maintenance: DispatchWindow,
    outcomes: Vec<ConnectivityAlertOutcome>,
}

/// Binds `atp-execution`'s connectivity gate to the SRS-NOTIF-001 dispatcher.
pub struct ConnectivityNotifierSink<K: AlertClock> {
    notifier: OperatorNotifier,
    channels: Vec<SharedChannelClient>,
    clock: K,
    store_dir: Option<PathBuf>,
    state: Arc<Mutex<EpisodeState>>,
    /// Dispatches handed to a worker and not yet finished.
    in_flight: Arc<AtomicUsize>,
    /// Test-only: force [`Self::spawn_dispatch`] to fail.
    #[cfg(test)]
    force_spawn_failure: std::sync::atomic::AtomicBool,
}

impl<K: AlertClock> ConnectivityNotifierSink<K> {
    /// One outage pages once per this window; the rest are coalesced.
    ///
    /// Five minutes is short enough that a still-broken gateway re-pages while
    /// the operator is still acting on the first alert, and long enough that a
    /// retry loop cannot turn one outage into a page per order.
    pub const COOLDOWN: Duration = Duration::from_secs(300);

    pub fn new(notifier: OperatorNotifier, channels: Vec<SharedChannelClient>, clock: K) -> Self {
        Self {
            notifier,
            channels,
            clock,
            store_dir: None,
            state: Arc::new(Mutex::new(EpisodeState::default())),
            in_flight: Arc::new(AtomicUsize::new(0)),
            #[cfg(test)]
            force_spawn_failure: std::sync::atomic::AtomicBool::new(false),
        }
    }

    /// Wait for in-flight dispatches to finish, up to `timeout`.
    ///
    /// `record` returns before the network work completes (see its docs), so a
    /// caller that needs the outcome — the operator CLI, and the tests — calls
    /// this first. Returns `false` if the timeout elapsed with work outstanding,
    /// so a caller can report "did not finish" rather than silently reading a
    /// half-populated outcome list.
    pub fn flush(&self, timeout: Duration) -> bool {
        let deadline = Instant::now() + timeout;
        while self.in_flight.load(Ordering::SeqCst) > 0 {
            if Instant::now() >= deadline {
                return false;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        true
    }

    /// Spawn the dispatch worker.
    ///
    /// A seam rather than a direct `thread::Builder` call so the spawn-failure
    /// branch is reachable from a test. That branch decides whether a transient
    /// resource spike silences the operator for five minutes, which is far too
    /// consequential to leave unexercised — and `thread::spawn` cannot be made to
    /// fail on demand any other way.
    fn spawn_dispatch<F>(&self, work: F) -> std::io::Result<()>
    where
        F: FnOnce() + Send + 'static,
    {
        #[cfg(test)]
        if self.force_spawn_failure.load(Ordering::SeqCst) {
            return Err(std::io::Error::other("injected spawn failure"));
        }
        std::thread::Builder::new()
            .name("atp-connectivity-alert".to_string())
            .spawn(work)
            .map(|_| ())
    }

    /// Persist every dispatched event to the durable SRS-NOTIF-001 audit store.
    ///
    /// The AC requires the delivery status to be *stored* as a notification
    /// event, so a deployment without this is only half-wired; it stays optional
    /// because the in-process tests and the CLI drive the same sink.
    pub fn with_store_dir(mut self, dir: impl Into<PathBuf>) -> Self {
        self.store_dir = Some(dir.into());
        self
    }

    /// Every outcome recorded so far, oldest first.
    pub fn outcomes(&self) -> Vec<ConnectivityAlertOutcome> {
        self.lock().outcomes.clone()
    }

    /// How many genuine-outage events have been folded into the next dispatch.
    ///
    /// Reports the OUTAGE window: that is the safety-relevant one. Coalesced
    /// planned-maintenance records are tracked separately and never consume this
    /// budget.
    pub fn coalesced_since_last_dispatch(&self) -> u64 {
        self.lock().outage.coalesced
    }

    /// A poisoned mutex must not take down the execution path: a panic in one
    /// `record` would otherwise make every later blocked order panic too, so a
    /// transport hiccup would escalate into an unusable engine.
    fn lock(&self) -> std::sync::MutexGuard<'_, EpisodeState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// The SYS-75 decision: suppress only when the state says scheduled restart
    /// **and** the event's own flag agrees. See the module docs for why
    /// disagreement pages instead of silencing.
    fn suppression_for(event: &ConnectivityEvent) -> Option<SuppressionReason> {
        match (event.state, event.scheduled_restart) {
            (ConnectivityState::ScheduledRestartWindow, true) => {
                Some(SuppressionReason::ScheduledRestartWindow)
            }
            _ => None,
        }
    }

    fn summary(event: &ConnectivityEvent, coalesced: u64) -> String {
        summary_for(event, coalesced)
    }
}

/// The operator-facing alert text.
fn summary_for(event: &ConnectivityEvent, coalesced: u64) -> String {
    let base = format!(
        "SRS-SAFE-003 / ERR-2: IB Gateway {} — live submission blocked for strategy {} \
             on {}",
        match event.state {
            ConnectivityState::Unreachable => "is UNREACHABLE",
            ConnectivityState::ScheduledRestartWindow => "is in its scheduled restart window",
            ConnectivityState::Connected => "reported connected",
        },
        event.strategy_id.as_str(),
        event.symbol,
    );
    if coalesced == 0 {
        base
    } else {
        // Never let a coalesced storm read as a single isolated block.
        format!(
            "{base} (+{coalesced} further blocked submission(s) coalesced since the last \
                 alert)"
        )
    }
}

impl<K: AlertClock> ConnectivityEventSink for ConnectivityNotifierSink<K> {
    fn record(&self, event: ConnectivityEvent) {
        // A healthy state is not an outage. The engine only calls this sink on
        // its blocked branches today, but the port does not promise that, and
        // fabricating a "connectivity lost" alert from a Connected observation
        // would put a false outage into the operator's audit trail. It also ends
        // the episode, so the next genuine loss pages immediately.
        if !event.state.is_blocked() {
            let mut state = self.lock();
            state.outage = DispatchWindow::default();
            state.maintenance = DispatchWindow::default();
            state.outcomes.push(ConnectivityAlertOutcome::NotAnOutage);
            return;
        }

        let now = self.clock.now_millis();
        // Decide suppression BEFORE consulting any cool-down, and rate-limit the
        // two classes independently.
        //
        // With one shared window, a scheduled-restart event silences the next 5
        // minutes of GENUINE outages: the maintenance record sends nothing but
        // still arms the window, so a real `Unreachable` arriving 60s later is
        // coalesced and the operator is never paged. Planned maintenance is
        // exactly when a real failure is most likely and least distinguishable,
        // so that is the worst possible time to go quiet. Separate windows mean a
        // maintenance record can never consume the outage budget.
        let suppression = Self::suppression_for(&event);
        let mut state = self.lock();
        let window = if suppression.is_some() {
            &mut state.maintenance
        } else {
            &mut state.outage
        };

        if let Some(last) = window.last_dispatch_millis {
            // `saturating_sub` on a clock that went backwards yields 0, which is
            // inside any window — a backwards step must not be readable as "the
            // cool-down expired" and let a storm through.
            let since_last = now.saturating_sub(last);
            if since_last < Self::COOLDOWN.as_millis() as u64 {
                window.coalesced += 1;
                state.outcomes.push(ConnectivityAlertOutcome::Coalesced {
                    since_last_millis: since_last,
                });
                return;
            }
        }

        let coalesced = window.coalesced;
        let trigger = NotificationTrigger::connectivity_loss(Self::summary(&event, coalesced), now);

        let coalesced = window.coalesced;
        let trigger = NotificationTrigger::connectivity_loss(summary_for(&event, coalesced), now);

        // The network work runs OFF this thread.
        //
        // `record` is called from inside `ExecutionEngine::submit_live_order`,
        // which then calls `connectivity.request_reconnect()`. Dispatching inline
        // would put up to two per-channel deadlines (20s each by default) between
        // the detection and the reconnect request, so a slow or dead relay would
        // delay recovery from the very outage it is reporting — and stall the
        // strategy's order path while doing it. Reporting a problem must not
        // extend it.
        //
        // Thread-per-dispatch rather than a queue+worker because the cool-down is
        // already the rate limit: at most one dispatch per class per COOLDOWN, so
        // the thread count is bounded by construction and there is no queue to
        // size, drain, or shut down. Callers that need the outcome call `flush`.
        //
        // The state lock is held ACROSS the spawn so the cool-down can be armed
        // by the same critical section that checked it — concurrent blocked
        // orders therefore coalesce against this dispatch rather than each
        // starting one. The worker blocks on that lock for the microseconds it
        // takes to return.
        let notifier = self.notifier;
        let channels = self.channels.clone();
        let store_dir = self.store_dir.clone();
        let state_handle = Arc::clone(&self.state);
        let in_flight = Arc::clone(&self.in_flight);

        in_flight.fetch_add(1, Ordering::SeqCst);
        let spawned = self.spawn_dispatch(move || {
            let outcome =
                dispatch_and_store(&notifier, &trigger, now, &channels, suppression, &store_dir);
            let mut state = state_handle
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            state.outcomes.push(outcome);
            drop(state);
            in_flight.fetch_sub(1, Ordering::SeqCst);
        });

        match spawned {
            Ok(()) => {
                // Armed ONLY once the worker is running, and on the ATTEMPT
                // rather than on delivery success: a provider outage is exactly
                // when every send fails, and arming on success would leave a
                // broken provider un-rate-limited.
                let window = if suppression.is_some() {
                    &mut state.maintenance
                } else {
                    &mut state.outage
                };
                window.last_dispatch_millis = Some(now);
                window.coalesced = 0;
            }
            Err(err) => {
                // Could NOT spawn. Two things must NOT happen here.
                //
                // No inline dispatch: an earlier revision did that ("a delayed
                // page beats no page"), which reintroduced exactly the block this
                // design removed, and worst precisely when it triggers — spawn
                // failure means resource exhaustion, so it would put ~40s of
                // network I/O in front of the reconnect during a live outage.
                //
                // And no arming: leaving the cool-down set would buy five minutes
                // of silence for a dispatch that never started, so a transient
                // resource spike would suppress the page for the whole window.
                // Unarmed, the next blocked submission retries immediately.
                self.in_flight.fetch_sub(1, Ordering::SeqCst);
                state.outcomes.push(ConnectivityAlertOutcome::Failed {
                    detail: format!(
                        "could not spawn the alert worker ({err}); alert NOT sent and the \
                         cool-down left UNARMED so the next blocked submission retries"
                    ),
                });
            }
        }
    }
}

/// Dispatch and persist, mapping both halves onto an outcome.
fn dispatch_and_store(
    notifier: &OperatorNotifier,
    trigger: &NotificationTrigger,
    now: u64,
    channels: &[SharedChannelClient],
    suppression: Option<SuppressionReason>,
    store_dir: &Option<PathBuf>,
) -> ConnectivityAlertOutcome {
    match notifier.dispatch_with_suppression(trigger, now, channels, suppression) {
        Ok(notification) => {
            // A store failure must be recorded as a FAILURE rather than
            // swallowed: the AC's "delivery status is stored" leg is then unmet,
            // and an audit trail that silently loses records is worse than one
            // that says so.
            if let Some(dir) = store_dir {
                if let Err(err) = NotificationEventStore::append_durably(dir, notification.clone())
                {
                    return ConnectivityAlertOutcome::Failed {
                        detail: format!(
                            "dispatched but could not store the notification event: {err}"
                        ),
                    };
                }
            }
            ConnectivityAlertOutcome::Dispatched(Box::new(notification))
        }
        Err(err) => ConnectivityAlertOutcome::Failed {
            detail: format!("operator notification dispatch failed: {err}"),
        },
    }
}

#[cfg(test)]
mod tests {
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Arc;

    use atp_notification::{
        ChannelError, ChannelReceipt, ChannelSendResult, DeliveryOutcome, NotificationChannel,
        NotificationChannelClient, NotificationMessage,
    };
    use atp_types::StrategyId;

    use super::*;

    struct StepClock(AtomicU64);

    impl StepClock {
        fn at(millis: u64) -> Self {
            Self(AtomicU64::new(millis))
        }
        fn advance(&self, millis: u64) {
            self.0.fetch_add(millis, Ordering::SeqCst);
        }
    }

    impl AlertClock for &StepClock {
        fn now_millis(&self) -> u64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    struct RecordingChannel {
        channel: NotificationChannel,
        sends: AtomicU64,
        fail: bool,
    }

    impl RecordingChannel {
        fn new(channel: NotificationChannel) -> Arc<Self> {
            Arc::new(Self {
                channel,
                sends: AtomicU64::new(0),
                fail: false,
            })
        }
        fn failing(channel: NotificationChannel) -> Arc<Self> {
            Arc::new(Self {
                channel,
                sends: AtomicU64::new(0),
                fail: true,
            })
        }
        fn sends(&self) -> u64 {
            self.sends.load(Ordering::SeqCst)
        }
    }

    impl NotificationChannelClient for RecordingChannel {
        fn channel(&self) -> NotificationChannel {
            self.channel
        }
        fn send(&self, _message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
            self.sends.fetch_add(1, Ordering::SeqCst);
            if self.fail {
                Err(ChannelError::TransportUnavailable {
                    detail: "relay down".into(),
                })
            } else {
                Ok(ChannelReceipt::new("accept-1"))
            }
        }
    }

    /// `record` dispatches off-thread, so tests wait for the work before
    /// asserting. A generous bound: these are loopback stubs, and a timeout here
    /// means a real hang, not a slow machine.
    fn record_and_wait<K: AlertClock>(
        sink: &ConnectivityNotifierSink<K>,
        event: ConnectivityEvent,
    ) {
        sink.record(event);
        assert!(
            sink.flush(Duration::from_secs(10)),
            "alert dispatch did not finish"
        );
    }

    fn blocked(state: ConnectivityState, scheduled_restart: bool) -> ConnectivityEvent {
        ConnectivityEvent {
            state,
            strategy_id: StrategyId::new("alpha-1"),
            symbol: "AAPL".to_string(),
            scheduled_restart,
        }
    }

    fn channels(
        email: Arc<RecordingChannel>,
        sms: Arc<RecordingChannel>,
    ) -> Vec<SharedChannelClient> {
        vec![email as SharedChannelClient, sms as SharedChannelClient]
    }

    #[test]
    fn an_unreachable_gateway_dispatches_over_both_required_channels() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        assert_eq!(email.sends(), 1);
        assert_eq!(sms.sends(), 1);
        match sink.outcomes().as_slice() {
            [ConnectivityAlertOutcome::Dispatched(event)] => {
                assert!(event.within_dispatch_sla());
                assert_eq!(event.deliveries().len(), 2);
                assert!(event
                    .deliveries()
                    .iter()
                    .all(|d| d.outcome() == DeliveryOutcome::Delivered));
            }
            other => panic!("expected one Dispatched, got {other:?}"),
        }
    }

    #[test]
    fn a_scheduled_restart_window_is_suppressed_not_sent() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(
            &sink,
            blocked(ConnectivityState::ScheduledRestartWindow, true),
        );

        // SYS-75: planned maintenance is silenced, but the event still records
        // both required channels as Suppressed — proof the dispatcher CHOSE not
        // to send, which a dropped alert could not produce.
        assert_eq!(email.sends(), 0);
        assert_eq!(sms.sends(), 0);
        match sink.outcomes().as_slice() {
            [ConnectivityAlertOutcome::Dispatched(event)] => {
                assert!(event
                    .deliveries()
                    .iter()
                    .all(|d| d.outcome() == DeliveryOutcome::Suppressed));
            }
            other => panic!("expected a suppressed Dispatched, got {other:?}"),
        }
    }

    /// The forgeable-boolean guard.
    #[test]
    fn a_scheduled_restart_flag_alone_cannot_silence_a_genuine_outage() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        // State says the gateway is genuinely unreachable; only the boolean
        // claims planned maintenance. The alert must go out.
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, true));

        assert_eq!(email.sends(), 1, "a forged flag must not silence an outage");
        assert_eq!(sms.sends(), 1);
    }

    #[test]
    fn a_healthy_state_never_fabricates_an_outage_alert() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Connected, false));

        assert_eq!(email.sends(), 0);
        assert_eq!(sms.sends(), 0);
        assert_eq!(sink.outcomes(), vec![ConnectivityAlertOutcome::NotAnOutage]);
    }

    #[test]
    fn a_retry_storm_pages_once_and_reports_the_coalesced_count() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        for _ in 0..200 {
            record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
            clock.advance(10);
        }

        assert_eq!(
            email.sends(),
            1,
            "200 blocked orders must not send 200 emails"
        );
        assert_eq!(sms.sends(), 1);
        assert_eq!(sink.coalesced_since_last_dispatch(), 199);

        // After the cool-down the next block pages again, and says how many were
        // folded in — a coalesced storm must never read as one isolated block.
        clock.advance(ConnectivityNotifierSink::<&StepClock>::COOLDOWN.as_millis() as u64);
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
        assert_eq!(email.sends(), 2);
        match sink.outcomes().last() {
            Some(ConnectivityAlertOutcome::Dispatched(event)) => {
                assert!(
                    event.summary().contains("+199 further blocked"),
                    "summary hid the storm: {}",
                    event.summary()
                );
            }
            other => panic!("expected a second Dispatched, got {other:?}"),
        }
    }

    /// The false-silence case: planned maintenance must not buy a real outage
    /// five minutes of quiet.
    ///
    /// A scheduled-restart record SENDS NOTHING but was still arming the shared
    /// cool-down, so a genuine `Unreachable` arriving inside the window was
    /// coalesced and the operator was never paged. A restart window is exactly
    /// when a real failure is most likely and least distinguishable from the
    /// planned disconnect — the worst possible moment to go quiet.
    #[test]
    fn a_maintenance_window_cannot_silence_a_real_outage_that_follows_it() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        // Planned maintenance: correctly silent.
        record_and_wait(
            &sink,
            blocked(ConnectivityState::ScheduledRestartWindow, true),
        );
        assert_eq!(email.sends(), 0);

        // 60s later — well inside the cool-down — the gateway is genuinely down.
        clock.advance(60_000);
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        assert_eq!(
            email.sends(),
            1,
            "a real outage during/after a maintenance window was silenced"
        );
        assert_eq!(sms.sends(), 1);
    }

    /// The converse: the two windows are independent, so a genuine outage does
    /// not stop maintenance records from being written either.
    #[test]
    fn an_outage_does_not_consume_the_maintenance_windows_budget() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
        assert_eq!(email.sends(), 1);

        clock.advance(60_000);
        record_and_wait(
            &sink,
            blocked(ConnectivityState::ScheduledRestartWindow, true),
        );

        // Still 1 send (maintenance is suppressed), but it produced its own
        // recorded event rather than being coalesced into the outage window.
        assert_eq!(email.sends(), 1);
        match sink.outcomes().last() {
            Some(ConnectivityAlertOutcome::Dispatched(event)) => {
                assert!(event
                    .deliveries()
                    .iter()
                    .all(|d| d.outcome() == DeliveryOutcome::Suppressed));
            }
            other => panic!("expected a suppressed Dispatched, got {other:?}"),
        }
    }

    /// `record` must not hold up the caller's recovery.
    ///
    /// It runs inside `ExecutionEngine::submit_live_order`, which calls
    /// `connectivity.request_reconnect()` immediately AFTER it. Dispatching
    /// inline would put up to two per-channel deadlines between detecting the
    /// outage and asking to reconnect — so reporting the problem would extend it,
    /// and the strategy's order path would stall for the same span.
    #[test]
    fn record_returns_immediately_even_when_the_channels_are_slow() {
        struct SlowChannel(NotificationChannel);
        impl NotificationChannelClient for SlowChannel {
            fn channel(&self) -> NotificationChannel {
                self.0
            }
            fn send(
                &self,
                _message: &NotificationMessage,
                _deadline: Duration,
            ) -> ChannelSendResult {
                std::thread::sleep(Duration::from_millis(600));
                Ok(ChannelReceipt::new("slow-accept"))
            }
        }

        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            vec![
                Arc::new(SlowChannel(NotificationChannel::Email)) as SharedChannelClient,
                Arc::new(SlowChannel(NotificationChannel::Sms)) as SharedChannelClient,
            ],
            &clock,
        );

        let started = std::time::Instant::now();
        sink.record(blocked(ConnectivityState::Unreachable, false));
        let record_elapsed = started.elapsed();

        assert!(
            record_elapsed < Duration::from_millis(200),
            "record blocked for {record_elapsed:?}; the caller's reconnect is behind it"
        );

        // The work still happens — it is moved, not dropped.
        assert!(
            sink.flush(Duration::from_secs(10)),
            "dispatch did not finish"
        );
        assert!(matches!(
            sink.outcomes().last(),
            Some(ConnectivityAlertOutcome::Dispatched(_))
        ));
    }

    /// A failed worker start must not buy five minutes of silence.
    ///
    /// The cool-down exists to stop a retry storm from paging hundreds of times.
    /// If it is armed before the worker is known to be running, a transient
    /// resource spike suppresses the page for the whole window on a dispatch that
    /// never began — the operator hears nothing about a live outage and no
    /// durable event is written either. Arming only on a successful spawn means
    /// the next blocked submission retries immediately.
    #[test]
    fn a_failed_worker_spawn_does_not_arm_the_cooldown() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        sink.force_spawn_failure.store(true, Ordering::SeqCst);
        sink.record(blocked(ConnectivityState::Unreachable, false));

        assert_eq!(email.sends(), 0, "nothing should have been sent");
        assert!(
            matches!(
                sink.outcomes().last(),
                Some(ConnectivityAlertOutcome::Failed { .. })
            ),
            "the miss must be recorded, not silent"
        );

        // Immediately afterwards — far inside COOLDOWN — a retry must page.
        sink.force_spawn_failure.store(false, Ordering::SeqCst);
        clock.advance(10);
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        assert_eq!(
            email.sends(),
            1,
            "a failed spawn armed the cool-down and silenced the retry"
        );
        assert_eq!(sms.sends(), 1);
    }

    #[test]
    fn recovery_re_arms_immediate_notification() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
        clock.advance(10);
        record_and_wait(&sink, blocked(ConnectivityState::Connected, false));
        clock.advance(10);
        // A NEW outage inside what would have been the cool-down still pages:
        // the episode ended at recovery.
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        assert_eq!(email.sends(), 2);
    }

    #[test]
    fn a_failing_transport_is_recorded_and_never_panics_the_execution_path() {
        let email = RecordingChannel::failing(NotificationChannel::Email);
        let sms = RecordingChannel::failing(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        // Both channels failed, but the dispatch still produced a stored event
        // recording the failures — no fabrication, no panic.
        match sink.outcomes().as_slice() {
            [ConnectivityAlertOutcome::Dispatched(event)] => {
                assert!(event
                    .deliveries()
                    .iter()
                    .all(|d| d.outcome() == DeliveryOutcome::Failed));
            }
            other => panic!("expected Dispatched with failed deliveries, got {other:?}"),
        }
    }

    /// A provider outage is exactly when every send fails, so the cool-down must
    /// be armed by the attempt. Arming it on success would leave a broken
    /// provider un-rate-limited and drive a doomed send pair per blocked order.
    #[test]
    fn the_cooldown_is_armed_by_the_attempt_not_by_success() {
        let email = RecordingChannel::failing(NotificationChannel::Email);
        let sms = RecordingChannel::failing(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        for _ in 0..50 {
            record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
            clock.advance(10);
        }

        assert_eq!(
            email.sends(),
            1,
            "a failing provider must still be rate-limited"
        );
        assert_eq!(sms.sends(), 1);
    }

    #[test]
    fn a_backwards_clock_step_cannot_reopen_the_cooldown() {
        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        // Start high enough that the "rewind" below stays a valid u64.
        let clock = StepClock::at(10_000_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        );

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
        assert_eq!(email.sends(), 1);

        // Clock steps BACKWARDS (NTP correction). `now - last` must not be read
        // as a huge elapsed time that expires the window.
        clock.0.store(9_000_000, Ordering::SeqCst);
        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));
        assert_eq!(email.sends(), 1, "a backwards clock let the storm through");
    }

    #[test]
    fn the_stored_event_is_appended_to_the_durable_audit_store() {
        let dir = std::env::temp_dir().join(format!("atp-notif001-sink-{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("temp store dir");
        let _ = std::fs::remove_file(dir.join("notifications.json"));

        let email = RecordingChannel::new(NotificationChannel::Email);
        let sms = RecordingChannel::new(NotificationChannel::Sms);
        let clock = StepClock::at(1_000);
        let sink = ConnectivityNotifierSink::new(
            OperatorNotifier::new(),
            channels(email.clone(), sms.clone()),
            &clock,
        )
        .with_store_dir(&dir);

        record_and_wait(&sink, blocked(ConnectivityState::Unreachable, false));

        let restored = NotificationEventStore::load_from_path(&dir).expect("store reads back");
        assert_eq!(restored.events().len(), 1);
        std::fs::remove_dir_all(&dir).ok();
    }
}
