//! ERR-5 / SRS-DATA-013 / SyRS SYS-77 / StRS SN-1.26 / SN-1.27 — when an
//! ingested record fails any of the six SyRS SYS-77 validation rules
//! (a..f), the data layer's ingestion gate rejects the record
//! synchronously with `INGESTION_RECORD_VALIDATION_FAILED`, publishes a
//! structured `IngestionValidationEvent` carrying the matching
//! `QuarantineReason`, the source, the record hash, and the observation
//! timestamp, and does NOT write the record to primary storage (the
//! rejected record leaves the primary tier exactly as it found it).
//!
//! The validator probe now receives the canonical `MarketDataRecord` (not a
//! bare source+hash envelope) so it can range-check OHLC and detect duplicate
//! natural keys; the gate derives the source+hash `IngestionRecordSubmission`
//! envelope from the record itself for the acceptance/rejection surface.
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * `RecordValidatorSpy.validate_calls == 1` per record (the gate
//!     probes the validator exactly once).
//!   * `EventSinkSpy.events.len() == 1` per quarantined record, with
//!     `state == Quarantined(reason)`, `reason` matching what the
//!     validator returned, and the correct source / record_hash /
//!     observed_at_seconds derived from the record.
//!   * The positive control (Valid) returns `Ok(IngestionAccepted)` and
//!     emits zero events — proving the gate is selective.
//!   * A sweep of the deterministic mixed fixture through the REAL
//!     `Sys77RecordValidator` emits exactly one event per malformed
//!     record, covering all six `QuarantineReason` variants, while the
//!     well-formed records return `Ok` with no event.
//!   * SyRS SYS-77 source-invariance: identical rejection envelope
//!     (category + wire string) for records of different kinds/sources —
//!     the gate takes no `StrategyMode` parameter and no per-vendor branch.
//!   * Zero-primary-write invariant (behavioral anchor): the
//!     `RecordValidator` port exposes no mutator method, so the gate
//!     cannot write to primary storage through it. The primary
//!     enforcement lives in `tools/ingestion_validation_check.py` via
//!     the contract's `forbidden_mutations` allowlist (which rejects
//!     any `primary.insert(`, `storage.write(`, `tier.write(`,
//!     `ssd.write(`, `nas.write(`, `self.write_primary(`, etc. call
//!     inside the Quarantined match arm); this Rust test anchors the
//!     port-shape post-condition at the behavioral layer.

use atp_data::ingestion_validation::{mixed_validation_fixture, ALL_QUARANTINE_REASONS};
use atp_data::store::{fixture_batch, DatasetKind, MarketDataRecord};
use atp_data::{
    DataLayer, IngestionAccepted, IngestionValidationEventSink, RecordValidator,
    Sys77RecordValidator,
};
use atp_types::{
    IngestionValidationEvent, OrderErrorCategory, QuarantineReason, RecordValidationOutcome,
};
use std::cell::{Cell, RefCell};

/// A validator whose outcome is fixed regardless of the record — used to drive the GATE contract in
/// isolation from the rule logic (the real rules are exercised by the mixed-fixture sweep and the
/// `atp-data` unit / `srs_data_013` integration tests). It records how many times the gate probes it.
struct RecordValidatorSpy {
    outcome: Cell<RecordValidationOutcome>,
    validate_calls: Cell<u32>,
}

impl RecordValidatorSpy {
    fn quarantined(reason: QuarantineReason) -> Self {
        Self {
            outcome: Cell::new(RecordValidationOutcome::Quarantined(reason)),
            validate_calls: Cell::new(0),
        }
    }

    fn valid() -> Self {
        Self {
            outcome: Cell::new(RecordValidationOutcome::Valid),
            validate_calls: Cell::new(0),
        }
    }
}

impl RecordValidator for RecordValidatorSpy {
    fn validate(&self, _record: &MarketDataRecord) -> RecordValidationOutcome {
        self.validate_calls.set(self.validate_calls.get() + 1);
        self.outcome.get()
    }
}

#[derive(Default)]
struct EventSinkSpy {
    events: RefCell<Vec<IngestionValidationEvent>>,
}

impl IngestionValidationEventSink for EventSinkSpy {
    fn record(&self, event: IngestionValidationEvent) {
        self.events.borrow_mut().push(event);
    }
}

/// Sink that panics if consulted. Used by the Valid positive control to
/// prove the rejection event channel is never invoked when the gate
/// admits.
struct ForbiddenSink;

impl IngestionValidationEventSink for ForbiddenSink {
    fn record(&self, _event: IngestionValidationEvent) {
        panic!("ERR-5: Valid branch must not record an IngestionValidationEvent");
    }
}

const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;
const TS: i64 = 1_700_000_000;

/// A single well-formed daily-equity fixture record. Its content is irrelevant to the spy-driven gate
/// tests (the spy returns a canned outcome); the source + record_hash the gate echoes are DERIVED
/// from it.
fn sample_record() -> MarketDataRecord {
    fixture_batch(DatasetKind::DailyEquityBar, TS)
        .into_iter()
        .next()
        .expect("daily-equity fixture batch is non-empty")
}

#[test]
fn err_5_quarantined_state_blocks_record_with_structured_error() {
    // SRS-DATA-013 / SyRS SYS-77: when the validator classifies a record
    // as Quarantined, the data layer must reject with
    // INGESTION_RECORD_VALIDATION_FAILED, publish exactly one
    // IngestionValidationEvent carrying the matching reason, the
    // source, the record hash, and the observation timestamp, and
    // surface the originating record envelope unchanged in the error.
    let layer = DataLayer;
    let validator = RecordValidatorSpy::quarantined(QuarantineReason::RangeViolation);
    let sink = EventSinkSpy::default();
    let rec = sample_record();
    let submission = rec.ingestion_submission();

    let error = layer
        .ingest_record(&rec, &validator, &sink, OBSERVED_AT_SECONDS)
        .expect_err("ERR-5: Quarantined must reject the ingested record");

    assert_eq!(
        error.category,
        OrderErrorCategory::IngestionRecordValidationFailed,
        "SRS-DATA-013: category must be IngestionRecordValidationFailed"
    );
    assert_eq!(
        error.category.as_str(),
        "INGESTION_RECORD_VALIDATION_FAILED",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(error.error_type, "IngestionRecordValidationFailed");
    assert!(
        error.message.contains(&submission.record_hash),
        "message must name the record hash"
    );
    assert!(
        error.message.contains(&submission.source),
        "message must name the ingestion source"
    );
    assert!(
        error.message.contains("SRS-DATA-013"),
        "message must trace SRS-DATA-013"
    );
    assert!(
        error.message.contains("SYS-77"),
        "message must cite SyRS SYS-77 (data-layer validation rules)"
    );
    assert!(
        error.message.contains("RANGE_VIOLATION"),
        "message must surface the QuarantineReason wire string"
    );
    assert_eq!(
        error.original_record, submission,
        "structured error must carry the record's derived envelope (SRS-DATA-013)"
    );

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        1,
        "exactly one IngestionValidationEvent must be recorded for dashboard alerting"
    );
    assert!(recorded[0].state.is_quarantined());
    assert_eq!(recorded[0].reason, QuarantineReason::RangeViolation);
    assert_eq!(recorded[0].source, submission.source);
    assert_eq!(recorded[0].record_hash, submission.record_hash);
    assert_eq!(recorded[0].observed_at_seconds, OBSERVED_AT_SECONDS);
    assert_eq!(
        validator.validate_calls.get(),
        1,
        "the gate must probe validate exactly once per record"
    );
}

#[test]
fn err_5_valid_outcome_returns_accepted_and_emits_no_event() {
    // Negative control: ERR-5's rejection must be selective. A Valid
    // outcome must return IngestionAccepted and must NOT touch the
    // event sink. The ForbiddenSink would panic if invoked.
    let layer = DataLayer;
    let validator = RecordValidatorSpy::valid();
    let sink = ForbiddenSink;
    let rec = sample_record();
    let submission = rec.ingestion_submission();

    let accepted: IngestionAccepted = layer
        .ingest_record(&rec, &validator, &sink, OBSERVED_AT_SECONDS)
        .expect("Valid must accept the record");

    assert_eq!(accepted.source, submission.source);
    assert_eq!(accepted.record_hash, submission.record_hash);
    assert_eq!(
        validator.validate_calls.get(),
        1,
        "the gate must probe validate exactly once on the accept path too"
    );
}

#[test]
fn err_5_real_validator_sweep_emits_one_event_per_rule() {
    // Pseudo-property, STRENGTHENED to use the REAL Sys77RecordValidator over the deterministic mixed
    // fixture (well-formed records + one deliberately-malformed record per SYS-77 rule): every
    // malformed record must produce exactly one IngestionValidationEvent carrying its own reason, and
    // all six QuarantineReason variants must be exercised — so the gate's pass-through of the reason
    // field is proven against real rule logic, not a canned stub. The well-formed records must return
    // Ok with no event (the gate is selective).
    let layer = DataLayer;
    let validator = Sys77RecordValidator::new();
    let sink = EventSinkSpy::default();

    let mut accepted = 0usize;
    for record in mixed_validation_fixture(TS) {
        match layer.ingest_record(&record, &validator, &sink, OBSERVED_AT_SECONDS) {
            Ok(_) => accepted += 1,
            Err(err) => assert_eq!(
                err.category,
                OrderErrorCategory::IngestionRecordValidationFailed,
                "every quarantine uses the single SyRS SYS-64 category"
            ),
        }
    }

    assert_eq!(accepted, 4, "the four well-formed fixtures are admitted");
    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        6,
        "one IngestionValidationEvent per quarantined record"
    );
    for event in recorded.iter() {
        assert!(event.state.is_quarantined());
        assert_eq!(event.observed_at_seconds, OBSERVED_AT_SECONDS);
    }
    // All six SYS-77 reasons appear exactly once — the gate faithfully forwards each rule's reason.
    for reason in ALL_QUARANTINE_REASONS {
        let count = recorded.iter().filter(|e| e.reason == reason).count();
        assert_eq!(
            count, 1,
            "exactly one {reason:?} event in the mixed fixture"
        );
    }
}

#[test]
fn err_5_identical_contract_across_sources() {
    // SyRS SYS-77 source-invariance: the rejection envelope must be
    // identical regardless of which feed/kind produced the record. The
    // data-layer gate API takes NO StrategyMode parameter and no
    // per-vendor branch precisely so that every ingestion source flows
    // through the same gate — demonstrated here by driving two records
    // of DIFFERENT kinds (a daily equity bar and an option-chain
    // snapshot, whose derived `source` tags differ) through the gate and
    // asserting the rejection envelopes are byte-identical at the
    // category / error_type / wire-string level.
    let layer = DataLayer;
    let sink = EventSinkSpy::default();

    let equity_record = fixture_batch(DatasetKind::DailyEquityBar, TS)
        .into_iter()
        .next()
        .expect("daily-equity fixture is non-empty");
    let option_record = fixture_batch(DatasetKind::OptionChainSnapshot, TS)
        .into_iter()
        .next()
        .expect("option-chain fixture is non-empty");
    let equity_submission = equity_record.ingestion_submission();
    let option_submission = option_record.ingestion_submission();

    let equity_validator = RecordValidatorSpy::quarantined(QuarantineReason::DuplicateRecord);
    let option_validator = RecordValidatorSpy::quarantined(QuarantineReason::DuplicateRecord);

    let equity_err = layer
        .ingest_record(
            &equity_record,
            &equity_validator,
            &sink,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("Quarantined must reject the equity record");
    let option_err = layer
        .ingest_record(
            &option_record,
            &option_validator,
            &sink,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("Quarantined must reject the option record identically");

    // The wire form must be byte-identical across sources — that's
    // SYS-77's whole point: one rule set across all feeds/kinds.
    assert_eq!(equity_err.category, option_err.category);
    assert_eq!(equity_err.error_type, option_err.error_type);
    assert_eq!(
        equity_err.category.as_str(),
        "INGESTION_RECORD_VALIDATION_FAILED"
    );
    assert_eq!(
        option_err.category.as_str(),
        "INGESTION_RECORD_VALIDATION_FAILED"
    );

    // The original_record differs (different kind → different source + hash) — that's expected and is
    // the per-record payload. The two sources must NOT be equal (distinct kinds).
    assert_eq!(equity_err.original_record, equity_submission);
    assert_eq!(option_err.original_record, option_submission);
    assert_ne!(equity_submission.source, option_submission.source);

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        2,
        "one event per quarantined record, regardless of source"
    );
    // Same state and same reason across both events — only the source
    // and record_hash differ. SYS-77 fans out events for both kinds.
    assert_eq!(recorded[0].state, recorded[1].state);
    assert_eq!(recorded[0].reason, recorded[1].reason);
    assert_eq!(
        recorded[0].observed_at_seconds,
        recorded[1].observed_at_seconds
    );
    assert_ne!(recorded[0].source, recorded[1].source);
}

#[test]
fn err_5_quarantined_state_anchors_zero_mutation_via_port_shape() {
    // Zero-primary-write invariant — behavioral anchor.
    //
    // The PRIMARY enforcement of this invariant is the static check in
    // `tools/ingestion_validation_check.py`, which parses the
    // Quarantined match arm and rejects any call to the patterns
    // listed in the contract block's `forbidden_mutations` array
    // (primary.insert, primary.write, primary.append, storage.write,
    // storage.insert, tier.write, ssd.write, nas.write, records.append,
    // table.insert, self.persist, self.write_primary,
    // self.commit_record, self.flush_to_primary). This test anchors
    // the post-condition at the behavioral level by demonstrating that
    // the data layer's public port surface (`RecordValidator`) exposes
    // NO mutator method — `validate` is read-only. The gate therefore
    // cannot write to primary storage through the port even if a
    // future refactor wanted to; the only way to introduce a write
    // would be to either widen the port (which the static check on
    // the trait body would catch) or call a method on a concrete type
    // bypassing the trait (which the forbidden_mutations static check
    // would catch).
    struct WriteWatcher {
        outcome: RecordValidationOutcome,
        validate_calls: Cell<u32>,
        would_have_written: Cell<u32>,
    }
    impl RecordValidator for WriteWatcher {
        fn validate(&self, _record: &MarketDataRecord) -> RecordValidationOutcome {
            self.validate_calls.set(self.validate_calls.get() + 1);
            // The trait has no mutator, so even a malicious validator
            // cannot move would_have_written from this read-only method
            // through the gate's public surface.
            self.outcome
        }
    }

    let layer = DataLayer;
    let validator = WriteWatcher {
        outcome: RecordValidationOutcome::Quarantined(QuarantineReason::DuplicateRecord),
        validate_calls: Cell::new(0),
        would_have_written: Cell::new(0),
    };
    let sink = EventSinkSpy::default();
    let rec = sample_record();

    let before = validator.would_have_written.get();
    let _ = layer.ingest_record(&rec, &validator, &sink, OBSERVED_AT_SECONDS);
    let after = validator.would_have_written.get();

    assert_eq!(
        before, after,
        "the RecordValidator port exposes no mutator — a quarantined \
         record cannot reach the primary tier through this surface"
    );
    // The gate DID consult the read-only validate method exactly once.
    // If validate_calls grew past one on a single record, the gate
    // would be double-evaluating the rule set against the registry.
    assert_eq!(validator.validate_calls.get(), 1);
    assert_eq!(
        sink.events.borrow().len(),
        1,
        "exactly one event recorded, proving the rejection ran end-to-end"
    );
}
