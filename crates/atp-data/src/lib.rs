use atp_types::{
    IngestionRecordSubmission, IngestionValidationEvent, OrderErrorCategory,
    RecordValidationOutcome, RuntimeService, StrategyId, StructuredIngestionError,
};

#[derive(Debug, Default)]
pub struct DataLayer;

// --------------------------------------------------------------------------- //
// Ingestion validation ports (SRS-DATA-013 / SyRS SYS-77)
// --------------------------------------------------------------------------- //
//
// The data layer owns ingestion + validation per SRS module table line 97
// ("Data Layer | Ingestion, validation, storage catalog, …"). ERR-5's gate
// consults two ports:
//
//   * `RecordValidator` — the read-only probe that classifies an
//     `IngestionRecordSubmission` against the six SyRS SYS-77 rules
//     (a..f). Concrete implementations (deferred to SRS-DATA-001..006 +
//     SRS-DATA-013) own the actual rule logic against equity OHLCV and
//     option-chain payloads. The trait exposes no mutator — the
//     zero-write-to-primary invariant is anchored at the port shape so
//     a concrete validator cannot accidentally commit a record through
//     the probe call.
//
//   * `IngestionValidationEventSink` — the structured-event publication
//     channel. Concrete sinks (deferred to SRS-DATA-014 / SRS-DATA-015 +
//     SRS-NOTIF-001) route events to the quarantine storage backend, the
//     dashboard WebSocket alert pane, and the notification dispatcher
//     per SyRS SYS-77's "alert the operator on the dashboard and
//     notification subsystem with the count and nature of quarantined
//     records" clause. The "count" half is the sink's aggregation
//     responsibility; the gate emits one event per quarantined record.
//
// Both traits live in `atp-data` (not `atp-types`) because the consumer —
// `DataLayer::ingest_record` — lives here. Placing them in `atp-types`
// would force the type crate to know about ports, inverting the dependency
// direction.
pub trait RecordValidator {
    /// Classify a record against the six SyRS SYS-77 rules. Returns
    /// `Valid` if the record may proceed to primary storage, or
    /// `Quarantined(reason)` naming the rule it violated. Read-only with
    /// respect to any primary-storage state — the validator never writes.
    fn validate(&self, record: &IngestionRecordSubmission) -> RecordValidationOutcome;
}

pub trait IngestionValidationEventSink {
    fn record(&self, event: IngestionValidationEvent);
}

/// Happy-path admission envelope. Echoes back the record identity so the
/// caller can correlate the acceptance with the originating ingestion
/// source. Only constructed inside the `Valid` arm of `ingest_record`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionAccepted {
    pub source: String,
    pub record_hash: String,
}

impl DataLayer {
    pub fn service(&self) -> RuntimeService {
        RuntimeService::DataLayer
    }

    pub fn query_owner(&self, _strategy_id: &StrategyId) -> &'static str {
        "data-layer"
    }

    /// SRS-DATA-013 / SyRS SYS-77 ingestion-validation gate. Matches on
    /// the validator's classification of the record; `Valid` returns
    /// `IngestionAccepted`; `Quarantined(reason)` emits a structured
    /// `IngestionValidationEvent` through the sink AND returns a
    /// `StructuredIngestionError` whose category is
    /// `OrderErrorCategory::IngestionRecordValidationFailed` (wire string
    /// `INGESTION_RECORD_VALIDATION_FAILED`).
    ///
    /// **Invariants** (statically checked by
    /// `tools/ingestion_validation_check.py`):
    ///
    /// * The `Quarantined` arm MUST call `events.record(`.
    /// * The `Quarantined` arm MUST produce
    ///   `OrderErrorCategory::IngestionRecordValidationFailed` (directly
    ///   or via the `StructuredIngestionError::quarantined(` factory).
    /// * The `Quarantined` arm MUST NOT write to primary storage
    ///   (no `primary.insert(`, `storage.write(`, `tier.write(`,
    ///   `ssd.write(`, `nas.write(`, `self.write_primary(`, etc.). The
    ///   rejected record must leave the primary tier exactly as it
    ///   found it.
    /// * `Valid` is the only call site of `IngestionAccepted {`.
    ///
    /// The gate takes no `StrategyMode` parameter — SyRS SYS-77 applies
    /// the same six validation rules uniformly across every ingestion
    /// source (Databento, IB, Sharadar, user Parquet), so the rejection
    /// envelope is identical regardless of which feed produced the
    /// record.
    pub fn ingest_record<V, S>(
        &self,
        record: IngestionRecordSubmission,
        validator: &V,
        events: &S,
        observed_at_seconds: u64,
    ) -> Result<IngestionAccepted, StructuredIngestionError>
    where
        V: RecordValidator,
        S: IngestionValidationEventSink,
    {
        match validator.validate(&record) {
            RecordValidationOutcome::Valid => Ok(IngestionAccepted {
                source: record.source,
                record_hash: record.record_hash,
            }),
            RecordValidationOutcome::Quarantined(reason) => {
                events.record(IngestionValidationEvent {
                    state: RecordValidationOutcome::Quarantined(reason),
                    reason,
                    source: record.source.clone(),
                    record_hash: record.record_hash.clone(),
                    observed_at_seconds,
                });
                Err(StructuredIngestionError::quarantined(record, reason))
            }
        }
    }
}

// Re-export to satisfy the static checker — references the
// `OrderErrorCategory` variant by name so a workspace-level dead-code
// scan cannot drop the link between the wire string and this crate.
#[doc(hidden)]
pub const _INGESTION_VALIDATION_CATEGORY: OrderErrorCategory =
    OrderErrorCategory::IngestionRecordValidationFailed;

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::QuarantineReason;
    use std::cell::{Cell, RefCell};

    #[test]
    fn identifies_data_layer_service() {
        let layer = DataLayer;
        assert_eq!(layer.service(), RuntimeService::DataLayer);
    }

    struct StubValidator {
        outcome: RecordValidationOutcome,
    }

    impl RecordValidator for StubValidator {
        fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
            self.outcome
        }
    }

    #[derive(Default)]
    struct StubSink {
        events: RefCell<Vec<IngestionValidationEvent>>,
    }

    impl IngestionValidationEventSink for StubSink {
        fn record(&self, event: IngestionValidationEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    struct ForbiddenSink;

    impl IngestionValidationEventSink for ForbiddenSink {
        fn record(&self, _event: IngestionValidationEvent) {
            panic!("Valid outcome must not record an IngestionValidationEvent");
        }
    }

    fn record(source: &str, hash: &str) -> IngestionRecordSubmission {
        IngestionRecordSubmission {
            source: source.to_string(),
            record_hash: hash.to_string(),
        }
    }

    #[test]
    fn valid_outcome_returns_accepted_and_emits_no_event() {
        let layer = DataLayer;
        let validator = StubValidator {
            outcome: RecordValidationOutcome::Valid,
        };
        let sink = ForbiddenSink;

        let accepted = layer
            .ingest_record(
                record("bulk-equity-bars", "0xabc"),
                &validator,
                &sink,
                1_715_000_000,
            )
            .expect("Valid outcome must accept the record");
        assert_eq!(accepted.source, "bulk-equity-bars");
        assert_eq!(accepted.record_hash, "0xabc");
    }

    #[test]
    fn quarantined_outcome_rejects_with_ingestion_record_validation_failed() {
        let layer = DataLayer;
        let validator = StubValidator {
            outcome: RecordValidationOutcome::Quarantined(QuarantineReason::RangeViolation),
        };
        let sink = StubSink::default();

        let error = layer
            .ingest_record(
                record("bulk-equity-bars", "0xdeadbeef"),
                &validator,
                &sink,
                1_715_000_000,
            )
            .expect_err("Quarantined outcome must reject the record");
        assert_eq!(
            error.category,
            OrderErrorCategory::IngestionRecordValidationFailed
        );
        assert_eq!(error.category.as_str(), "INGESTION_RECORD_VALIDATION_FAILED");
        assert_eq!(error.original_record.record_hash, "0xdeadbeef");
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one event per rejected record");
        assert_eq!(events[0].reason, QuarantineReason::RangeViolation);
        assert_eq!(events[0].source, "bulk-equity-bars");
        assert_eq!(events[0].record_hash, "0xdeadbeef");
        assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
        assert!(events[0].state.is_quarantined());
    }

    #[test]
    fn quarantined_outcome_consults_validator_exactly_once() {
        // Sanity check: the gate must consult `validate` exactly once.
        // A future refactor that probes the validator inside both arms
        // would silently degrade dashboard accuracy (and double-count
        // events). Wrap a StubValidator in a call counter and assert.
        struct CountingValidator {
            outcome: RecordValidationOutcome,
            validate_calls: Cell<u32>,
        }
        impl RecordValidator for CountingValidator {
            fn validate(&self, _record: &IngestionRecordSubmission) -> RecordValidationOutcome {
                self.validate_calls.set(self.validate_calls.get() + 1);
                self.outcome
            }
        }

        let layer = DataLayer;
        let validator = CountingValidator {
            outcome: RecordValidationOutcome::Quarantined(QuarantineReason::DuplicateRecord),
            validate_calls: Cell::new(0),
        };
        let sink = StubSink::default();
        let _ = layer.ingest_record(
            record("fundamental-records", "0x123"),
            &validator,
            &sink,
            1_715_000_000,
        );
        assert_eq!(
            validator.validate_calls.get(),
            1,
            "the gate must probe validate exactly once per record"
        );
    }
}
