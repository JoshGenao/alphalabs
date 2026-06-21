use std::fmt;

use atp_types::{
    IngestionJobRequest, IngestionRecordSubmission, IngestionValidationEvent,
    OrderErrorCategory, PacingBudgetEvent, PacingBudgetState, RecordValidationOutcome,
    RuntimeService, StrategyId, StructuredIngestionError, StructuredPacingError,
};

pub mod store;
pub mod query;

pub use crate::query::{UnifiedHistoricalQuery, UnifiedHistoricalResult};

use crate::store::{MarketDataRecord, MarketDataStore, StoreError, UpsertOutcome};

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

// --------------------------------------------------------------------------- //
// Idempotent market-record ingestion (SRS-DATA-016 / SyRS NFR-R4)
// --------------------------------------------------------------------------- //
//
// The idempotent write path that COMPOSES the unchanged ERR-5 validation gate
// (`ingest_record`) with the storage substrate (`store::MarketDataStore`). The
// store-mutating `upsert` lives in `store.rs` / this new entry point, NEVER in
// `ingest_record` — whose quarantine arm stays statically read-only
// (`tools/ingestion_validation_check.py` forbidden_mutations). See `store.rs`
// for the natural-key idempotency core and the durable codec.

/// The combined outcome of [`DataLayer::ingest_market_record`]: the ERR-5 admission envelope plus
/// the store's idempotency signal ([`UpsertOutcome::Inserted`] on a fresh key,
/// [`UpsertOutcome::UnchangedDuplicate`] on an idempotent re-ingest).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionOutcome {
    /// The ERR-5 admission envelope returned by the unchanged validation gate.
    pub accepted: IngestionAccepted,
    /// Whether the record was newly inserted or was an idempotent no-op.
    pub applied: UpsertOutcome,
}

/// A failure of the idempotent market-record ingestion path: either the ERR-5 validation gate
/// **quarantined** the record (read-only, no write), or the **store** rejected the write — a
/// conflicting re-ingest ([`StoreError::ConflictingContent`], the "corrupts existing data" guard) or
/// an I/O failure. The two are kept distinct so the operator surface can tell a validation reject
/// apart from a write conflict.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MarketIngestError {
    /// The ERR-5 gate quarantined the record before any write.
    Rejected(StructuredIngestionError),
    /// The store rejected the write (conflicting re-ingest, malformed record, or I/O failure).
    Store(StoreError),
}

impl From<StructuredIngestionError> for MarketIngestError {
    fn from(error: StructuredIngestionError) -> Self {
        Self::Rejected(error)
    }
}

impl From<StoreError> for MarketIngestError {
    fn from(error: StoreError) -> Self {
        Self::Store(error)
    }
}

impl fmt::Display for MarketIngestError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Rejected(error) => write!(f, "ingestion record quarantined: {error}"),
            Self::Store(error) => write!(f, "market-data store write failed: {error}"),
        }
    }
}

impl std::error::Error for MarketIngestError {}

impl DataLayer {
    /// SRS-DATA-016 / SyRS NFR-R4 — the **idempotent** market-record ingestion entry point.
    ///
    /// Composes the unchanged ERR-5 validation gate ([`ingest_record`](Self::ingest_record)) — so an
    /// invalid record is still quarantined read-only before any write — and only on a `Valid`
    /// classification applies the canonical record to `store` via the idempotent
    /// [`MarketDataStore::upsert`]: re-running an ingestion for an already-ingested datum is a no-op
    /// ([`UpsertOutcome::UnchangedDuplicate`], no duplicate record), and a re-ingest carrying
    /// *different* content fails closed ([`StoreError::ConflictingContent`]) without mutating the
    /// store (no corruption). The caller durably persists the mutated store with
    /// [`MarketDataStore::save_to_path`].
    ///
    /// **Validation is bound to the persisted record.** The ERR-5 envelope is *derived from the
    /// record* ([`MarketDataRecord::ingestion_submission`]) rather than supplied independently, so
    /// the gate is always applied to exactly the record that will be written — a caller cannot
    /// validate one payload and store another (there is no separate submission to forge).
    ///
    /// The store mutator lives here, never inside `ingest_record` — whose quarantine arm stays a
    /// statically-verified read-only operation.
    pub fn ingest_market_record<V, S>(
        &self,
        store: &mut MarketDataStore,
        record: MarketDataRecord,
        validator: &V,
        events: &S,
        observed_at_seconds: u64,
    ) -> Result<IngestionOutcome, MarketIngestError>
    where
        V: RecordValidator,
        S: IngestionValidationEventSink,
    {
        // The ERR-5 envelope is DERIVED from the record, binding validation to exactly the record
        // that will be persisted (no independent payload to forge).
        let submission = record.ingestion_submission();
        // 1. ERR-5 validation gate (UNCHANGED) — quarantines an invalid record read-only.
        let accepted = self.ingest_record(submission, validator, events, observed_at_seconds)?;
        // 2. Only reachable on a Valid classification → idempotent store write.
        let applied = store.upsert(record)?;
        Ok(IngestionOutcome { accepted, applied })
    }
}

// --------------------------------------------------------------------------- //
// Pacing-budget ports (SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55)
// --------------------------------------------------------------------------- //
//
// SyRS SYS-55 places the pacing-budget validator at scheduling time —
// before either SYS-22b (minute-bar watchlist) or SYS-23 (option-chain
// capture) starts. The validator reads the configured pacing limits
// (SYS-31's 60-requests-per-10-minute ceiling) for the job's window and
// compares them against the projected request count for the job's
// scope. The ports are:
//
//   * `PacingBudgetValidator` — the read-only probe that returns
//     `WithinBudget` if the projected count fits the permitted count or
//     `BudgetExceeded` if it would push past the cap. Concrete impls
//     (deferred to SRS-DATA-002 + SRS-DATA-004 + the orchestrator-side
//     scheduler config) own the actual projection logic against
//     watchlist sizes, expiry chains, and the IB account's pacing tier.
//     The trait exposes only read methods — the zero-job-start
//     invariant is anchored at the port shape so a concrete validator
//     cannot accidentally dispatch the job through the probe call.
//
//   * `PacingBudgetEventSink` — the structured-event publication
//     channel. Concrete sinks (deferred to SRS-NOTIF-001 +
//     SRS-LOG-001 + dashboard alert pane) fan the events into the
//     dashboard's scheduling view and into the notification dispatcher
//     so the operator can reduce scope or widen the window per SYS-55's
//     "until scope or window configuration is reduced" clause.
//
// Both traits live in `atp-data` (not `atp-types`) because the consumer
// — `DataLayer::schedule_ingestion_job` — lives here. Placing them in
// `atp-types` would force the type crate to know about ports,
// inverting the dependency direction.
pub trait PacingBudgetValidator {
    /// Return the count the scheduler currently projects for the
    /// `schedule.window_seconds` window. Read-only; concrete impls
    /// compute the projection from watchlist size × expected request
    /// granularity. Surfaced separately from `check_budget` so the
    /// rejection event can carry the actual count without re-running
    /// the classification.
    fn projected_requests(&self, schedule: &IngestionJobRequest) -> u32;

    /// Return the permitted request count for the
    /// `schedule.window_seconds` window. Derived from IB's pacing tier
    /// (SYS-31: 60 historical requests per 10 minutes). Read-only.
    fn permitted_requests(&self, schedule: &IngestionJobRequest) -> u32;

    /// Classify a scheduled job against the pacing budget. Returns
    /// `WithinBudget` if the projected count fits or `BudgetExceeded`
    /// if the cap would be breached. Read-only with respect to any
    /// scheduler state — the validator never starts the job.
    fn check_budget(&self, schedule: &IngestionJobRequest) -> PacingBudgetState;
}

pub trait PacingBudgetEventSink {
    fn record(&self, event: PacingBudgetEvent);
}

/// Happy-path admission envelope. Echoes back the job_kind so the
/// caller can correlate the acceptance with the originating schedule
/// request. Only constructed inside the `WithinBudget` arm of
/// `schedule_ingestion_job`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct IngestionJobScheduled {
    pub job_kind: String,
}

impl DataLayer {
    /// SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55 pacing-budget gate at
    /// scheduling time. Matches on the validator's classification of
    /// the scheduled job; `WithinBudget` returns
    /// `IngestionJobScheduled`; `BudgetExceeded` reads the projected
    /// and permitted counts off the validator, emits a structured
    /// `PacingBudgetEvent` through the sink, AND returns a
    /// `StructuredPacingError` whose category is
    /// `OrderErrorCategory::IngestionPacingBudgetExceeded` (wire string
    /// `INGESTION_PACING_BUDGET_EXCEEDED`).
    ///
    /// **Invariants** (statically checked by
    /// `tools/pacing_budget_check.py`):
    ///
    /// * The `BudgetExceeded` arm MUST call `events.record(`.
    /// * The `BudgetExceeded` arm MUST produce
    ///   `OrderErrorCategory::IngestionPacingBudgetExceeded` (directly
    ///   or via the `StructuredPacingError::budget_exceeded(` factory).
    /// * The `BudgetExceeded` arm MUST NOT start the affected job
    ///   (no `jobs.insert(`, `scheduler.start(`, `scheduler.run(`,
    ///   `scheduler.enqueue(`, `scheduler.schedule(`, `job.start(`,
    ///   `job.run(`, `self.start_job(`, etc.). The refused job must
    ///   leave the scheduler exactly as it found it.
    /// * `WithinBudget` is the only call site of
    ///   `IngestionJobScheduled {`.
    ///
    /// The gate takes no `StrategyMode` parameter — SyRS SYS-55
    /// applies the same pacing-budget validation independent of which
    /// strategy is live; the scheduled ingestion jobs precede mode
    /// selection.
    pub fn schedule_ingestion_job<V, S>(
        &self,
        schedule: IngestionJobRequest,
        validator: &V,
        events: &S,
        observed_at_seconds: u64,
    ) -> Result<IngestionJobScheduled, StructuredPacingError>
    where
        V: PacingBudgetValidator,
        S: PacingBudgetEventSink,
    {
        match validator.check_budget(&schedule) {
            PacingBudgetState::WithinBudget => Ok(IngestionJobScheduled {
                job_kind: schedule.job_kind,
            }),
            PacingBudgetState::BudgetExceeded => {
                let projected = validator.projected_requests(&schedule);
                let permitted = validator.permitted_requests(&schedule);
                events.record(PacingBudgetEvent {
                    state: PacingBudgetState::BudgetExceeded,
                    job_kind: schedule.job_kind.clone(),
                    projected_requests: projected,
                    permitted_requests: permitted,
                    observed_at_seconds,
                });
                Err(StructuredPacingError::budget_exceeded(
                    schedule, projected, permitted,
                ))
            }
        }
    }
}

// Re-export to satisfy the static checker — references the
// `OrderErrorCategory` variant by name so a workspace-level dead-code
// scan cannot drop the link between the wire string and this crate.
#[doc(hidden)]
pub const _PACING_BUDGET_CATEGORY: OrderErrorCategory =
    OrderErrorCategory::IngestionPacingBudgetExceeded;

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

    // ----------------------------------------------------------------------- //
    // ERR-6 in-crate unit tests (SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55)
    // ----------------------------------------------------------------------- //

    struct PacingBudgetStub {
        state: PacingBudgetState,
        projected: u32,
        permitted: u32,
    }

    impl PacingBudgetValidator for PacingBudgetStub {
        fn projected_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
            self.projected
        }
        fn permitted_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
            self.permitted
        }
        fn check_budget(&self, _schedule: &IngestionJobRequest) -> PacingBudgetState {
            self.state
        }
    }

    #[derive(Default)]
    struct PacingBudgetSink {
        events: RefCell<Vec<PacingBudgetEvent>>,
    }

    impl PacingBudgetEventSink for PacingBudgetSink {
        fn record(&self, event: PacingBudgetEvent) {
            self.events.borrow_mut().push(event);
        }
    }

    struct PacingBudgetForbiddenSink;

    impl PacingBudgetEventSink for PacingBudgetForbiddenSink {
        fn record(&self, _event: PacingBudgetEvent) {
            panic!("WithinBudget outcome must not record a PacingBudgetEvent");
        }
    }

    fn schedule(job_kind: &str, window_seconds: u64) -> IngestionJobRequest {
        IngestionJobRequest {
            job_kind: job_kind.to_string(),
            window_seconds,
        }
    }

    #[test]
    fn within_budget_state_returns_scheduled_and_emits_no_event() {
        let layer = DataLayer;
        let validator = PacingBudgetStub {
            state: PacingBudgetState::WithinBudget,
            projected: 50,
            permitted: 60,
        };
        let sink = PacingBudgetForbiddenSink;

        let scheduled = layer
            .schedule_ingestion_job(
                schedule("minute-bar-watchlist", 61_200),
                &validator,
                &sink,
                1_715_000_000,
            )
            .expect("WithinBudget must schedule the job");
        assert_eq!(scheduled.job_kind, "minute-bar-watchlist");
    }

    #[test]
    fn budget_exceeded_state_rejects_with_ingestion_pacing_budget_exceeded() {
        let layer = DataLayer;
        let validator = PacingBudgetStub {
            state: PacingBudgetState::BudgetExceeded,
            projected: 6_200,
            permitted: 6_120,
        };
        let sink = PacingBudgetSink::default();

        let error = layer
            .schedule_ingestion_job(
                schedule("minute-bar-watchlist", 61_200),
                &validator,
                &sink,
                1_715_000_000,
            )
            .expect_err("BudgetExceeded must refuse the scheduled job");
        assert_eq!(
            error.category,
            OrderErrorCategory::IngestionPacingBudgetExceeded
        );
        assert_eq!(error.category.as_str(), "INGESTION_PACING_BUDGET_EXCEEDED");
        assert_eq!(error.original_request.job_kind, "minute-bar-watchlist");
        let events = sink.events.borrow();
        assert_eq!(events.len(), 1, "exactly one event per refused job");
        assert_eq!(events[0].state, PacingBudgetState::BudgetExceeded);
        assert_eq!(events[0].job_kind, "minute-bar-watchlist");
        assert_eq!(events[0].projected_requests, 6_200);
        assert_eq!(events[0].permitted_requests, 6_120);
        assert_eq!(events[0].observed_at_seconds, 1_715_000_000);
    }

    #[test]
    fn budget_exceeded_outcome_consults_validator_exactly_once_per_method() {
        // Sanity check: the gate must consult `check_budget` exactly
        // once (classification), and on the BudgetExceeded leaf must
        // also read `projected_requests` and `permitted_requests`
        // exactly once each (event population). A future refactor that
        // double-probes any of these would silently distort the
        // dashboard fan-out.
        struct CountingValidator {
            state: PacingBudgetState,
            projected: u32,
            permitted: u32,
            check_calls: Cell<u32>,
            projected_calls: Cell<u32>,
            permitted_calls: Cell<u32>,
        }
        impl PacingBudgetValidator for CountingValidator {
            fn projected_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
                self.projected_calls.set(self.projected_calls.get() + 1);
                self.projected
            }
            fn permitted_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
                self.permitted_calls.set(self.permitted_calls.get() + 1);
                self.permitted
            }
            fn check_budget(&self, _schedule: &IngestionJobRequest) -> PacingBudgetState {
                self.check_calls.set(self.check_calls.get() + 1);
                self.state
            }
        }

        let layer = DataLayer;
        let validator = CountingValidator {
            state: PacingBudgetState::BudgetExceeded,
            projected: 65,
            permitted: 60,
            check_calls: Cell::new(0),
            projected_calls: Cell::new(0),
            permitted_calls: Cell::new(0),
        };
        let sink = PacingBudgetSink::default();
        let _ = layer.schedule_ingestion_job(
            schedule("option-chain-capture", 600),
            &validator,
            &sink,
            1_715_000_000,
        );
        assert_eq!(validator.check_calls.get(), 1);
        assert_eq!(validator.projected_calls.get(), 1);
        assert_eq!(validator.permitted_calls.get(), 1);
    }
}
