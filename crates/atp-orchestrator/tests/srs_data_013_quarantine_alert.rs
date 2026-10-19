//! SRS-DATA-013 / SyRS SYS-77 — the operator is alerted to quarantined records with their count
//! and nature (L5 integration / L7 domain).
//!
//! Real stores on disk: a real ingest records its run in the data layer's quarantine store, the
//! real dispatcher fans out to fake email + push channels that mirror the real adapters (email
//! queued at our relay, push delivered), and the real notification audit store keeps the events.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use atp_data::ingestion_validation::mixed_validation_fixture;
use atp_data::store::{fixture_batch, DatasetKind};
use atp_data::tiering::{TierConfig, TieredStore, DEFAULT_HOT_RETENTION_DAYS};
use atp_data::{DataLayer, QuarantineSummarySink, Sys77RecordValidator, QUARANTINE_DIRNAME};
use atp_notification::{
    ChannelError, ChannelReceipt, ChannelSendResult, DeliveryOutcome, NotificationChannel,
    NotificationChannelClient, NotificationEventStore, NotificationMessage, NotificationSeverity,
    OperatorNotifier, SharedChannelClient, TriggerKind,
};
use atp_orchestrator::quarantine_notification::{notify_quarantined_runs, NotifyError, RunAlert};

const TS: i64 = 1_700_000_000;
const OBSERVED_AT: u64 = 1_715_000_000;

struct FakeChannel {
    channel: NotificationChannel,
    fail: bool,
    sends: Arc<AtomicUsize>,
}

impl NotificationChannelClient for FakeChannel {
    fn channel(&self) -> NotificationChannel {
        self.channel
    }
    fn send(&self, _message: &NotificationMessage, _deadline: Duration) -> ChannelSendResult {
        self.sends.fetch_add(1, Ordering::SeqCst);
        if self.fail {
            return Err(ChannelError::TransportUnavailable {
                detail: "fault injected".to_string(),
            });
        }
        // Mirror the real adapters: email is queued at our relay, push is delivered to ntfy.
        Ok(match self.channel {
            NotificationChannel::Email => ChannelReceipt::queued_for_relay("relay-ref"),
            NotificationChannel::Push => ChannelReceipt::accepted_by_destination("ntfy-ref"),
        })
    }
}

fn channels(fail: bool) -> (Vec<SharedChannelClient>, Arc<AtomicUsize>) {
    let sends = Arc::new(AtomicUsize::new(0));
    let make = |channel| {
        Arc::new(FakeChannel {
            channel,
            fail,
            sends: sends.clone(),
        }) as SharedChannelClient
    };
    (
        vec![
            make(NotificationChannel::Email),
            make(NotificationChannel::Push),
        ],
        sends,
    )
}

/// A fresh `(ssd, nas, notification store)` under the temp dir.
fn dirs(label: &str) -> (PathBuf, PathBuf, PathBuf) {
    let base =
        std::env::temp_dir().join(format!("atp_data013_alert_{}_{label}", std::process::id()));
    let _ = fs::remove_dir_all(&base);
    let (ssd, nas, store) = (base.join("ssd"), base.join("nas"), base.join("alerts"));
    fs::create_dir_all(&nas).unwrap();
    fs::create_dir_all(&store).unwrap();
    (ssd, nas, store)
}

fn ingest_mixed(ssd: &Path, nas: &Path) {
    let tier = TieredStore::new(TierConfig::new(ssd, nas, DEFAULT_HOT_RETENTION_DAYS).unwrap());
    DataLayer
        .ingest_market_records_quarantining(
            &tier,
            mixed_validation_fixture(TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .unwrap();
}

fn now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
}

#[test]
fn a_quarantine_run_is_alerted_once_with_its_count_and_nature() {
    let (ssd, nas, store) = dirs("once");
    ingest_mixed(&ssd, &nas);
    let (channels, sends) = channels(false);
    let notifier = OperatorNotifier::new();

    let report = notify_quarantined_runs(&ssd, &store, &notifier, &channels, now()).unwrap();
    assert_eq!(report.alerts.len(), 1);
    let RunAlert::Stored { event, .. } = &report.alerts[0] else {
        panic!("expected a stored alert, got {:?}", report.alerts[0]);
    };
    assert_eq!(event.trigger_kind(), TriggerKind::DataQuarantine);
    assert_eq!(event.severity(), NotificationSeverity::Error);
    let summary = event.summary();
    assert!(summary.contains("6 of 10 records quarantined"), "{summary}");
    for reason in [
        "RANGE_VIOLATION=1",
        "OHLC_OUT_OF_BAND=1",
        "NEGATIVE_VOLUME=1",
        "NULL_REQUIRED_FIELD=1",
        "DUPLICATE_RECORD=1",
        "OPTION_FIELD_MISSING=1",
    ] {
        assert!(summary.contains(reason), "{reason} missing from {summary}");
    }
    assert!(
        summary.contains("WRITTEN (4 valid records stored)"),
        "{summary}"
    );
    assert_eq!(
        event
            .delivery_for(NotificationChannel::Email)
            .unwrap()
            .outcome(),
        DeliveryOutcome::Queued
    );
    assert_eq!(
        event
            .delivery_for(NotificationChannel::Push)
            .unwrap()
            .outcome(),
        DeliveryOutcome::Delivered
    );
    // Durably in the audit store.
    let audit = NotificationEventStore::load_from_path(&store).unwrap();
    assert_eq!(audit.events().len(), 1);
    assert_eq!(audit.events()[0].summary(), summary);
    assert_eq!(sends.load(Ordering::SeqCst), 2);

    // A second pass alerts nothing more: the audit store is the cursor.
    let again = notify_quarantined_runs(&ssd, &store, &notifier, &channels, now()).unwrap();
    assert!(again.alerts.is_empty(), "{again:?}");
    assert_eq!(again.already_alerted, 1);
    assert_eq!(sends.load(Ordering::SeqCst), 2, "no second send");
}

#[test]
fn a_clean_run_is_not_alerted() {
    let (ssd, nas, store) = dirs("clean");
    let tier = TieredStore::new(TierConfig::new(&ssd, &nas, DEFAULT_HOT_RETENTION_DAYS).unwrap());
    DataLayer
        .ingest_market_records_tiered(
            &tier,
            fixture_batch(DatasetKind::DailyEquityBar, TS),
            &Sys77RecordValidator::new(),
            &QuarantineSummarySink::new(),
            OBSERVED_AT,
        )
        .unwrap();
    let (channels, sends) = channels(false);
    let report =
        notify_quarantined_runs(&ssd, &store, &OperatorNotifier::new(), &channels, now()).unwrap();
    assert!(report.alerts.is_empty());
    assert_eq!(report.clean, 1);
    assert_eq!(sends.load(Ordering::SeqCst), 0);
}

#[test]
fn an_alert_that_reached_no_channel_is_retried() {
    let (ssd, nas, store) = dirs("retry");
    ingest_mixed(&ssd, &nas);
    let notifier = OperatorNotifier::new();
    let (failing, _) = channels(true);
    let first = notify_quarantined_runs(&ssd, &store, &notifier, &failing, now()).unwrap();
    let RunAlert::Stored { event, .. } = &first.alerts[0] else {
        panic!(
            "expected a stored (failed-delivery) alert, got {:?}",
            first.alerts[0]
        );
    };
    assert!(event
        .deliveries()
        .iter()
        .all(|d| d.outcome() == DeliveryOutcome::Failed));

    // The run is not marked alerted by an event no channel carried: the next pass sends again.
    let (working, sends) = channels(false);
    let second = notify_quarantined_runs(&ssd, &store, &notifier, &working, now()).unwrap();
    assert_eq!(second.alerts.len(), 1, "{second:?}");
    assert!(matches!(second.alerts[0], RunAlert::Stored { .. }));
    assert_eq!(sends.load(Ordering::SeqCst), 2);
    let third = notify_quarantined_runs(&ssd, &store, &notifier, &working, now()).unwrap();
    assert!(third.alerts.is_empty(), "now it is alerted");
}

#[test]
fn an_unreadable_run_is_alerted_as_unreadable() {
    let (ssd, nas, store) = dirs("unreadable");
    ingest_mixed(&ssd, &nas);
    let quarantine = ssd.join(QUARANTINE_DIRNAME);
    let run_file = fs::read_dir(&quarantine)
        .unwrap()
        .map(|e| e.unwrap().path())
        .find(|p| p.extension().is_some_and(|x| x == "qrun"))
        .unwrap();
    let text = fs::read_to_string(&run_file).unwrap();
    fs::write(
        &run_file,
        text.replacen("NEGATIVE_VOLUME", "NEGATIVE_VOLUMF", 1),
    )
    .unwrap();
    let (channels, _) = channels(false);
    let report =
        notify_quarantined_runs(&ssd, &store, &OperatorNotifier::new(), &channels, now()).unwrap();
    let RunAlert::Stored { event, .. } = &report.alerts[0] else {
        panic!("expected a stored alert, got {:?}", report.alerts[0]);
    };
    assert!(
        event.summary().contains("UNREADABLE"),
        "{}",
        event.summary()
    );
    assert!(
        event.summary().contains("checksum mismatch"),
        "{}",
        event.summary()
    );
}

#[test]
fn an_unreadable_store_is_an_error_not_nothing_due() {
    let (ssd, nas, store) = dirs("errors");
    ingest_mixed(&ssd, &nas);
    let (channels, sends) = channels(false);
    let notifier = OperatorNotifier::new();
    let missing_store = store.join("not-there");
    assert!(matches!(
        notify_quarantined_runs(&ssd, &missing_store, &notifier, &channels, now()),
        Err(NotifyError::NotificationStore(_))
    ));
    let missing_ssd = ssd.join("not-mounted");
    assert!(matches!(
        notify_quarantined_runs(&missing_ssd, &store, &notifier, &channels, now()),
        Err(NotifyError::Quarantine(_))
    ));
    assert_eq!(
        sends.load(Ordering::SeqCst),
        0,
        "nothing sent without knowing what is due"
    );
}
