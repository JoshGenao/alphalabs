//! SRS-SAFE-001 / SyRS SYS-44a (b) / NFR-SC1 — the paper-engine FLEET halt
//! fan-out: `PaperEngineFleet::halt_all` must reach EVERY registered engine
//! (the reference baseline is 30 concurrent paper strategies), idempotently,
//! and once halted no registered engine can produce a fill — so no further
//! `on_fill` callback can be driven from any of them.
//!
//! L7 domain (safety) suite over the REAL per-engine gates (no doubles).
//! Post-conditions:
//!   * A 30-engine fleet halt transitions all 30; every engine subsequently
//!     REFUSES to fill (`HaltError::Halted`) — no escape.
//!   * Count invariant: `transitioned + already_halted == engines_total` on
//!     every call.
//!   * Idempotent: a second `halt_all` reports 30 `already_halted`, 0
//!     transitioned, and preserves each engine's original transition.
//!   * Empty fleet: an all-zero report, `all_halted()` vacuously true, not an
//!     error.
//!   * Registration fails closed on a duplicate or blank engine id (a
//!     silently-replaced engine would escape the halt).
//!   * Before the halt, registered engines fill normally (positive control —
//!     the fleet gate does not break the SIM-001/002 fill path).

use atp_simulation::halt::{HaltError, HaltOutcome, HaltReason, HaltablePaperEngine};
use atp_simulation::halt_fleet::{FleetError, PaperEngineFleet};

const REFERENCE_BASELINE_ENGINES: usize = 30;

fn fleet_of(count: usize) -> PaperEngineFleet {
    let mut fleet = PaperEngineFleet::new();
    for index in 0..count {
        fleet
            .register(
                format!("paper-{index:02}"),
                HaltablePaperEngine::running_default(),
            )
            .expect("register engine");
    }
    fleet
}

fn fill_on(fleet: &PaperEngineFleet, engine_id: &str) -> Result<(), HaltError> {
    fleet
        .simulate_fill_on(engine_id, 1_700_000_000, "AAPL", 10, 150_00, Some(2))
        .expect("engine id is registered")
        .map(|_| ())
}

#[test]
fn srs_safe_001_running_fleet_fills_normally_before_halt() {
    let fleet = fleet_of(REFERENCE_BASELINE_ENGINES);
    for index in 0..REFERENCE_BASELINE_ENGINES {
        assert!(
            fill_on(&fleet, &format!("paper-{index:02}")).is_ok(),
            "a running engine fills normally (positive control)"
        );
    }
    assert!(!fleet.all_halted());
}

#[test]
fn srs_safe_001_halt_all_reaches_every_engine_and_no_fill_escapes() {
    let mut fleet = fleet_of(REFERENCE_BASELINE_ENGINES);
    let report = fleet.halt_all(HaltReason::KillSwitch);

    assert_eq!(report.engines_total, REFERENCE_BASELINE_ENGINES as u64);
    assert_eq!(report.transitioned, REFERENCE_BASELINE_ENGINES as u64);
    assert_eq!(report.already_halted, 0);
    assert_eq!(report.outcomes.len(), REFERENCE_BASELINE_ENGINES);
    assert!(fleet.all_halted());

    for index in 0..REFERENCE_BASELINE_ENGINES {
        let engine_id = format!("paper-{index:02}");
        match fill_on(&fleet, &engine_id) {
            Err(HaltError::Halted { reason }) => assert_eq!(reason, HaltReason::KillSwitch),
            other => panic!("engine {engine_id} must refuse to fill after halt_all, got {other:?}"),
        }
    }
}

#[test]
fn srs_safe_001_count_invariant_holds_on_every_call() {
    let mut fleet = fleet_of(7);
    for _ in 0..3 {
        let report = fleet.halt_all(HaltReason::KillSwitch);
        assert_eq!(
            report.transitioned + report.already_halted,
            report.engines_total,
            "every registered engine is visited, none skipped"
        );
    }
}

#[test]
fn srs_safe_001_second_halt_is_idempotent_and_preserves_original_transitions() {
    let mut fleet = fleet_of(REFERENCE_BASELINE_ENGINES);
    let first = fleet.halt_all(HaltReason::KillSwitch);
    let first_sequences: Vec<u64> = first
        .outcomes
        .iter()
        .map(|(engine_id, outcome)| match outcome {
            HaltOutcome::Transitioned(transition) => transition.sequence,
            other => panic!("first halt of {engine_id} must transition, got {other:?}"),
        })
        .collect();

    let second = fleet.halt_all(HaltReason::KillSwitch);
    assert_eq!(second.transitioned, 0);
    assert_eq!(second.already_halted, REFERENCE_BASELINE_ENGINES as u64);
    for (engine_id, outcome) in &second.outcomes {
        match outcome {
            HaltOutcome::AlreadyHalted { reason } => assert_eq!(*reason, HaltReason::KillSwitch),
            other => panic!("second halt of {engine_id} must be a no-op, got {other:?}"),
        }
    }
    assert_eq!(
        first_sequences,
        vec![1; REFERENCE_BASELINE_ENGINES],
        "original transitions (sequence 1) recorded once, never overwritten"
    );
    assert!(fleet.all_halted());
}

#[test]
fn srs_safe_001_empty_fleet_reports_zeros_not_an_error() {
    let mut fleet = PaperEngineFleet::new();
    assert!(fleet.is_empty());
    let report = fleet.halt_all(HaltReason::KillSwitch);
    assert_eq!(
        (
            report.engines_total,
            report.transitioned,
            report.already_halted
        ),
        (0, 0, 0)
    );
    assert!(report.outcomes.is_empty());
    assert!(fleet.all_halted(), "vacuously true — nothing can fill");
}

#[test]
fn srs_safe_001_registration_fails_closed_on_duplicate_and_blank_ids() {
    let mut fleet = PaperEngineFleet::new();
    fleet
        .register("paper-00", HaltablePaperEngine::running_default())
        .expect("first registration");

    match fleet.register("paper-00", HaltablePaperEngine::running_default()) {
        Err(FleetError::DuplicateEngineId { engine_id }) => assert_eq!(engine_id, "paper-00"),
        other => panic!("a duplicate id must be rejected, got {other:?}"),
    }
    match fleet.register("   ", HaltablePaperEngine::running_default()) {
        Err(FleetError::BlankEngineId) => {}
        other => panic!("a blank id must be rejected, got {other:?}"),
    }
    assert_eq!(fleet.len(), 1, "failed registrations change nothing");
}

#[test]
fn srs_safe_001_unknown_engine_id_is_distinguishable_from_a_halted_refusal() {
    let fleet = fleet_of(1);
    assert!(
        fleet
            .simulate_fill_on("no-such-engine", 1, "AAPL", 1, 100, None)
            .is_none(),
        "an unknown id is None, never a fabricated refusal or fill"
    );
}
