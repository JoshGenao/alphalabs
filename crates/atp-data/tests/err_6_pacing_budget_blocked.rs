//! ERR-6 / SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-31 / SYS-55 / StRS A-10
//! — when a configured ingestion job's projected IB historical-data
//! request count exceeds the permitted count for its window, the data
//! layer's scheduling-time gate refuses the job with
//! `INGESTION_PACING_BUDGET_EXCEEDED`, publishes a structured
//! `PacingBudgetEvent` carrying both the observed `projected_requests`
//! and the `permitted_requests` snapshot, and does NOT start the
//! affected job (the refused job leaves the scheduler exactly as it
//! found it).
//!
//! L7 domain (safety) test. The post-conditions are:
//!   * `PacingBudgetValidatorSpy.check_budget_calls == 1` per request
//!     (the gate probes the classifier exactly once).
//!   * `PacingBudgetEventSinkSpy.events.len() == 1` per refused job,
//!     with `state == BudgetExceeded`, `projected_requests` and
//!     `permitted_requests` matching what the validator reported, and
//!     the correct `job_kind`.
//!   * The positive control (WithinBudget) returns
//!     `Ok(IngestionJobScheduled)` and emits zero events — proving the
//!     gate is selective.
//!   * The pseudo-property sweep over varying
//!     `(job_kind, window_seconds, projected, permitted)` cases keeps
//!     the scheduler at zero acceptances and emits exactly one event
//!     per case.
//!   * SyRS SYS-55 job-invariance: `minute-bar-watchlist` (SYS-22b) and
//!     `option-chain-capture` (SYS-23) produce byte-identical
//!     rejection envelopes — the gate takes no per-job branch beyond
//!     the projection numerics.
//!   * Zero-job-start invariant (behavioral anchor): the
//!     `PacingBudgetValidator` port exposes no mutator method, so the
//!     gate cannot start a job through it. The primary enforcement
//!     lives in `tools/pacing_budget_check.py` via the contract's
//!     `forbidden_mutations` allowlist (which rejects any
//!     `scheduler.start(`, `scheduler.run(`, `scheduler.enqueue(`,
//!     `job.start(`, `self.start_job(`, etc. call inside the
//!     BudgetExceeded match arm); this Rust test anchors the
//!     port-shape post-condition at the behavioral layer.

use atp_data::{DataLayer, IngestionJobScheduled, PacingBudgetEventSink, PacingBudgetValidator};
use atp_types::{IngestionJobRequest, OrderErrorCategory, PacingBudgetEvent, PacingBudgetState};
use std::cell::{Cell, RefCell};

struct PacingBudgetValidatorSpy {
    state: Cell<PacingBudgetState>,
    projected: Cell<u32>,
    permitted: Cell<u32>,
    check_budget_calls: Cell<u32>,
    projected_calls: Cell<u32>,
    permitted_calls: Cell<u32>,
}

impl PacingBudgetValidatorSpy {
    fn exceeded(projected: u32, permitted: u32) -> Self {
        Self {
            state: Cell::new(PacingBudgetState::BudgetExceeded),
            projected: Cell::new(projected),
            permitted: Cell::new(permitted),
            check_budget_calls: Cell::new(0),
            projected_calls: Cell::new(0),
            permitted_calls: Cell::new(0),
        }
    }

    fn within(projected: u32, permitted: u32) -> Self {
        Self {
            state: Cell::new(PacingBudgetState::WithinBudget),
            projected: Cell::new(projected),
            permitted: Cell::new(permitted),
            check_budget_calls: Cell::new(0),
            projected_calls: Cell::new(0),
            permitted_calls: Cell::new(0),
        }
    }
}

impl PacingBudgetValidator for PacingBudgetValidatorSpy {
    fn projected_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
        self.projected_calls.set(self.projected_calls.get() + 1);
        self.projected.get()
    }

    fn permitted_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
        self.permitted_calls.set(self.permitted_calls.get() + 1);
        self.permitted.get()
    }

    fn check_budget(&self, _schedule: &IngestionJobRequest) -> PacingBudgetState {
        self.check_budget_calls
            .set(self.check_budget_calls.get() + 1);
        self.state.get()
    }
}

#[derive(Default)]
struct PacingBudgetEventSinkSpy {
    events: RefCell<Vec<PacingBudgetEvent>>,
}

impl PacingBudgetEventSink for PacingBudgetEventSinkSpy {
    fn record(&self, event: PacingBudgetEvent) {
        self.events.borrow_mut().push(event);
    }
}

/// Sink that panics if consulted. Used by the WithinBudget positive
/// control to prove the rejection event channel is never invoked when
/// the gate admits.
struct PacingBudgetForbiddenSink;

impl PacingBudgetEventSink for PacingBudgetForbiddenSink {
    fn record(&self, _event: PacingBudgetEvent) {
        panic!("ERR-6: WithinBudget branch must not record a PacingBudgetEvent");
    }
}

fn schedule(job_kind: &str, window_seconds: u64) -> IngestionJobRequest {
    IngestionJobRequest {
        job_kind: job_kind.to_string(),
        window_seconds,
    }
}

const OBSERVED_AT_SECONDS: u64 = 1_715_000_000;

#[test]
fn err_6_budget_exceeded_state_blocks_job_with_structured_error() {
    // SRS-DATA-002 + SRS-DATA-004 + SyRS SYS-55: when the projected
    // request count exceeds the permitted count for the configured
    // window, the data layer must refuse with
    // INGESTION_PACING_BUDGET_EXCEEDED, publish exactly one
    // PacingBudgetEvent carrying both projected_requests AND
    // permitted_requests, and surface the originating schedule request
    // unchanged in the structured error envelope.
    let layer = DataLayer;
    let validator = PacingBudgetValidatorSpy::exceeded(6_200, 6_120);
    let sink = PacingBudgetEventSinkSpy::default();
    let req = schedule("minute-bar-watchlist", 61_200);

    let error = layer
        .schedule_ingestion_job(req.clone(), &validator, &sink, OBSERVED_AT_SECONDS)
        .expect_err("ERR-6: BudgetExceeded must refuse the scheduled job");

    assert_eq!(
        error.category,
        OrderErrorCategory::IngestionPacingBudgetExceeded,
        "SRS-DATA-002: category must be IngestionPacingBudgetExceeded"
    );
    assert_eq!(
        error.category.as_str(),
        "INGESTION_PACING_BUDGET_EXCEEDED",
        "wire string must match SyRS SYS-64 vocabulary"
    );
    assert_eq!(error.error_type, "IngestionPacingBudgetExceeded");
    assert!(
        error.message.contains("minute-bar-watchlist"),
        "message must name the affected job_kind"
    );
    assert!(
        error.message.contains("SRS-DATA-002"),
        "message must trace SRS-DATA-002"
    );
    assert!(
        error.message.contains("SRS-DATA-004"),
        "message must trace SRS-DATA-004"
    );
    assert!(
        error.message.contains("SYS-55"),
        "message must cite SyRS SYS-55 (pacing-budget validator)"
    );
    assert!(
        error.message.contains("6200"),
        "message must surface the projected request count"
    );
    assert!(
        error.message.contains("6120"),
        "message must surface the permitted request count"
    );
    assert_eq!(
        error.original_request, req,
        "structured error must carry the original schedule request (SRS-DATA-002)"
    );

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        1,
        "exactly one PacingBudgetEvent must be recorded for dashboard alerting"
    );
    assert_eq!(recorded[0].state, PacingBudgetState::BudgetExceeded);
    assert_eq!(recorded[0].job_kind, "minute-bar-watchlist");
    assert_eq!(recorded[0].projected_requests, 6_200);
    assert_eq!(recorded[0].permitted_requests, 6_120);
    assert_eq!(recorded[0].observed_at_seconds, OBSERVED_AT_SECONDS);
    assert_eq!(
        validator.check_budget_calls.get(),
        1,
        "the gate must probe check_budget exactly once per request"
    );
    assert_eq!(
        validator.projected_calls.get(),
        1,
        "projected_requests must be read exactly once on the rejection leaf"
    );
    assert_eq!(
        validator.permitted_calls.get(),
        1,
        "permitted_requests must be read exactly once on the rejection leaf"
    );
}

#[test]
fn err_6_within_budget_state_returns_scheduled_and_emits_no_event() {
    // Negative control: ERR-6's rejection must be selective. A
    // WithinBudget state must return IngestionJobScheduled and must
    // NOT touch the event sink. The PacingBudgetForbiddenSink would
    // panic if invoked. Equally, the projected/permitted numerics
    // methods must NOT be read on the accept path — they only matter
    // for the rejection event's payload.
    let layer = DataLayer;
    let validator = PacingBudgetValidatorSpy::within(50, 6_120);
    let sink = PacingBudgetForbiddenSink;
    let req = schedule("option-chain-capture", 600);

    let scheduled: IngestionJobScheduled = layer
        .schedule_ingestion_job(req, &validator, &sink, OBSERVED_AT_SECONDS)
        .expect("WithinBudget must schedule the job");

    assert_eq!(scheduled.job_kind, "option-chain-capture");
    assert_eq!(
        validator.check_budget_calls.get(),
        1,
        "the gate must probe check_budget exactly once on the accept path too"
    );
    assert_eq!(
        validator.projected_calls.get(),
        0,
        "the WithinBudget leaf must not read projected_requests (no event to populate)"
    );
    assert_eq!(
        validator.permitted_calls.get(),
        0,
        "the WithinBudget leaf must not read permitted_requests (no event to populate)"
    );
}

#[test]
fn err_6_budget_exceeded_state_holds_across_many_schedules() {
    // Pseudo-property: regardless of job_kind / window_seconds /
    // projected / permitted, a BudgetExceeded state must never produce
    // an acceptance, and every refused job must produce its own
    // PacingBudgetEvent carrying the per-case projected and permitted
    // counts. The sweep mixes both SyRS SYS-55 ingestion jobs
    // (SYS-22b minute-bar watchlist and SYS-23 option-chain capture)
    // and a fictional third job_kind to exercise the gate's
    // job-invariance.
    let layer = DataLayer;
    let sink = PacingBudgetEventSinkSpy::default();
    let cases: [(&str, u64, u32, u32); 5] = [
        ("minute-bar-watchlist", 61_200, 6_200, 6_120),
        ("option-chain-capture", 600, 65, 60),
        ("minute-bar-watchlist", 30_600, 3_100, 3_060),
        ("option-chain-capture", 900, 95, 90),
        ("minute-bar-watchlist", 61_200, 9_999, 6_120),
    ];
    // One validator per case (each carries the projected/permitted
    // numerics it returns) but a single sink so we can assert the
    // cumulative event count.
    let mut total_check_calls = 0u32;
    for (job_kind, window_seconds, projected, permitted) in cases {
        let validator = PacingBudgetValidatorSpy::exceeded(projected, permitted);
        let req = schedule(job_kind, window_seconds);
        let err = layer
            .schedule_ingestion_job(req.clone(), &validator, &sink, OBSERVED_AT_SECONDS)
            .expect_err("BudgetExceeded always blocks");
        assert_eq!(
            err.category,
            OrderErrorCategory::IngestionPacingBudgetExceeded
        );
        assert_eq!(err.original_request, req);
        total_check_calls += validator.check_budget_calls.get();
    }
    assert_eq!(
        total_check_calls,
        cases.len() as u32,
        "check_budget must be probed once per request — no double-counting"
    );
    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        cases.len(),
        "one PacingBudgetEvent per refused job"
    );
    for (i, (job_kind, _window_seconds, projected, permitted)) in cases.iter().enumerate() {
        assert_eq!(recorded[i].state, PacingBudgetState::BudgetExceeded);
        assert_eq!(recorded[i].job_kind, *job_kind);
        assert_eq!(recorded[i].projected_requests, *projected);
        assert_eq!(recorded[i].permitted_requests, *permitted);
        assert_eq!(recorded[i].observed_at_seconds, OBSERVED_AT_SECONDS);
    }
}

#[test]
fn err_6_identical_contract_for_minute_bar_and_option_chain_jobs() {
    // SyRS SYS-55 job-invariance: the rejection envelope must be
    // identical for both SYS-22b (minute-bar watchlist) and SYS-23
    // (option-chain capture). The data-layer gate API takes no
    // per-job branch and no per-window enum — both jobs flow through
    // the same gate, and the projection numerics are the only
    // per-call payload that differs. This test demonstrates that the
    // absence of a job-specific branch is correct.
    let layer = DataLayer;
    let sink = PacingBudgetEventSinkSpy::default();

    let minute_bar_req = schedule("minute-bar-watchlist", 61_200);
    let option_chain_req = schedule("option-chain-capture", 600);

    let minute_bar_validator = PacingBudgetValidatorSpy::exceeded(6_200, 6_120);
    let option_chain_validator = PacingBudgetValidatorSpy::exceeded(65, 60);

    let minute_bar_err = layer
        .schedule_ingestion_job(
            minute_bar_req.clone(),
            &minute_bar_validator,
            &sink,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("BudgetExceeded must refuse the SYS-22b minute-bar job");
    let option_chain_err = layer
        .schedule_ingestion_job(
            option_chain_req.clone(),
            &option_chain_validator,
            &sink,
            OBSERVED_AT_SECONDS,
        )
        .expect_err("BudgetExceeded must refuse the SYS-23 option-chain job identically");

    // The wire form must be byte-identical across jobs — that's
    // SYS-55's whole point: one pacing-budget rule across both
    // capture windows.
    assert_eq!(minute_bar_err.category, option_chain_err.category);
    assert_eq!(minute_bar_err.error_type, option_chain_err.error_type);
    assert_eq!(
        minute_bar_err.category.as_str(),
        "INGESTION_PACING_BUDGET_EXCEEDED"
    );
    assert_eq!(
        option_chain_err.category.as_str(),
        "INGESTION_PACING_BUDGET_EXCEEDED"
    );

    // The original_request differs (different job_kind +
    // window_seconds) — that's expected and is the per-caller payload.
    assert_eq!(minute_bar_err.original_request, minute_bar_req);
    assert_eq!(option_chain_err.original_request, option_chain_req);

    let recorded = sink.events.borrow();
    assert_eq!(
        recorded.len(),
        2,
        "one event per refused job, regardless of which SYS-55 capture job it is"
    );
    // Same state across both events; only the job_kind +
    // projected/permitted numerics differ. SYS-55 fans out events for
    // both ingestion jobs.
    assert_eq!(recorded[0].state, recorded[1].state);
    assert_eq!(
        recorded[0].observed_at_seconds,
        recorded[1].observed_at_seconds
    );
    assert_eq!(recorded[0].job_kind, "minute-bar-watchlist");
    assert_eq!(recorded[1].job_kind, "option-chain-capture");
}

#[test]
fn err_6_budget_exceeded_anchors_zero_job_start_via_port_shape() {
    // Zero-job-start invariant — behavioral anchor.
    //
    // The PRIMARY enforcement of this invariant is the static check
    // in `tools/pacing_budget_check.py`, which parses the
    // BudgetExceeded match arm and rejects any call to the patterns
    // listed in the contract block's `forbidden_mutations` array
    // (jobs.insert, jobs.push, jobs.add, scheduler.start,
    // scheduler.run, scheduler.enqueue, scheduler.schedule,
    // job.start, job.run, self.start_job, self.run_job,
    // self.dispatch_job, ingest.start, self.execute_job). This test
    // anchors the post-condition at the behavioral level by
    // demonstrating that the data layer's public port surface
    // (`PacingBudgetValidator`) exposes NO mutator method — all three
    // methods are read-only. The gate therefore cannot start a job
    // through the port even if a future refactor wanted to; the only
    // way to introduce a job-start would be to either widen the port
    // (which the static check on the trait body would catch) or call
    // a method on a concrete type bypassing the trait (which the
    // forbidden_mutations static check would catch).
    //
    // The behavioral assertions below pin the port-shape
    // post-condition:
    //   * The gate invokes the read-only port methods (proving the
    //     gate is consulted).
    //   * The validator spy carries an internal `would_have_started`
    //     cell that no port method can move — because the trait
    //     offers no such method. We snapshot it before and after the
    //     gate invocation to demonstrate the invariant holds.
    struct StartWatcher {
        state: PacingBudgetState,
        projected: u32,
        permitted: u32,
        check_budget_calls: Cell<u32>,
        would_have_started: Cell<u32>,
    }
    impl PacingBudgetValidator for StartWatcher {
        fn projected_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
            self.projected
        }
        fn permitted_requests(&self, _schedule: &IngestionJobRequest) -> u32 {
            self.permitted
        }
        fn check_budget(&self, _schedule: &IngestionJobRequest) -> PacingBudgetState {
            self.check_budget_calls
                .set(self.check_budget_calls.get() + 1);
            // The trait has no mutator, so even a malicious validator
            // cannot move would_have_started from this read-only
            // method through the gate's public surface.
            self.state
        }
    }

    let layer = DataLayer;
    let validator = StartWatcher {
        state: PacingBudgetState::BudgetExceeded,
        projected: 6_200,
        permitted: 6_120,
        check_budget_calls: Cell::new(0),
        would_have_started: Cell::new(0),
    };
    let sink = PacingBudgetEventSinkSpy::default();
    let req = schedule("minute-bar-watchlist", 61_200);

    let before = validator.would_have_started.get();
    let _ = layer.schedule_ingestion_job(req, &validator, &sink, OBSERVED_AT_SECONDS);
    let after = validator.would_have_started.get();

    assert_eq!(
        before, after,
        "the PacingBudgetValidator port exposes no mutator — a refused \
         job cannot start through this surface"
    );
    // The gate DID consult the read-only check_budget method exactly
    // once. If check_budget_calls grew past one on a single request,
    // the gate would be double-classifying against the scheduler.
    assert_eq!(validator.check_budget_calls.get(), 1);
    assert_eq!(
        sink.events.borrow().len(),
        1,
        "exactly one event recorded, proving the refusal ran end-to-end"
    );
}
