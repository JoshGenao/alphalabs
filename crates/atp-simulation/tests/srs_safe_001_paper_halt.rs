//! Integration coverage for the **SRS-SAFE-001** paper-engine HALTED gate sub-component —
//! "paper simulation engines transition to the HALTED state with no further `on_fill` callbacks
//! emitted" (SyRS SYS-44a; NFR-P3; NFR-SC1; StRS SN-1.11).
//!
//! These tests drive [`HaltablePaperEngine`] end-to-end over the public API: a Running gate fills
//! exactly like the bare [`PaperSimulationEngine`], a halted gate refuses to PRODUCE a fill (so no
//! `on_fill` callback can be driven), `halt` is idempotent, the transition is observable, and the
//! gate never masks a fill-native [`SimError`] while Running. The test names are the `srs_safe_001_*`
//! targets the L7 domain test (`tests/domain/test_paper_halt_lifecycle.py`) shells out to.
//!
//! SRS-SAFE-001 stays `passes:false`: this is one named sub-component; the full kill-switch
//! sequence (IB cancel/disconnect, orchestrated activation + 5s budget, SRS-LOG-001 observability,
//! email/SMS, dashboard/CLI/REST trigger) is deferred to its named owners.

use atp_simulation::halt::{
    HaltError, HaltOutcome, HaltReason, HaltTransition, HaltablePaperEngine, PaperEngineState,
};
use atp_simulation::sim::{PaperSimulationEngine, SimError};

#[test]
fn srs_safe_001_running_engine_fills() {
    // While Running the gate produces the SAME fill the bare engine would, fields and all.
    let bare = PaperSimulationEngine::new();
    let gate = HaltablePaperEngine::running_default();
    assert_eq!(gate.state(), PaperEngineState::Running);

    let bare_fill = bare
        .simulate_fill(1, "AAPL", 100, 10_000, None)
        .expect("bare fill");
    let gate_fill = gate
        .simulate_fill(1, "AAPL", 100, 10_000, None)
        .expect("running gate fills");
    assert_eq!(gate_fill, bare_fill);
    // The SYS-15 defaults flow through unchanged (real cost path, not a stand-in).
    assert_eq!(gate_fill.commission_minor, 35);
    assert_eq!(gate_fill.slippage_minor, 500);
}

#[test]
fn srs_safe_001_halted_engine_emits_no_fill() {
    // The core SRS-SAFE-001 clause: once halted, NO fill is produced for any order that would
    // otherwise fill cleanly (buy, sell, with and without an observed spread).
    let mut gate = HaltablePaperEngine::running_default();
    gate.halt(HaltReason::KillSwitch);
    assert_eq!(gate.state(), PaperEngineState::Halted);

    for (qty, px, spread) in [
        (100, 10_000, None),
        (-100, 11_000, None),
        (50, 9_900, Some(40)),
    ] {
        assert_eq!(
            gate.simulate_fill(2, "AAPL", qty, px, spread),
            Err(HaltError::Halted {
                reason: HaltReason::KillSwitch
            }),
            "a halted engine must produce no fill for ({qty}, {px}, {spread:?})"
        );
    }
}

#[test]
fn srs_safe_001_halt_is_idempotent() {
    // First halt transitions; the second is a no-op that leaves the engine Halted with the
    // original transition (sequence + reason) intact.
    let mut gate = HaltablePaperEngine::running_default();
    let first = gate.halt(HaltReason::KillSwitch);
    assert_eq!(
        first,
        HaltOutcome::Transitioned(HaltTransition {
            reason: HaltReason::KillSwitch,
            sequence: 1,
        })
    );
    let second = gate.halt(HaltReason::KillSwitch);
    assert_eq!(
        second,
        HaltOutcome::AlreadyHalted {
            reason: HaltReason::KillSwitch
        }
    );
    assert_eq!(gate.state(), PaperEngineState::Halted);
    assert_eq!(
        gate.last_transition(),
        Some(HaltTransition {
            reason: HaltReason::KillSwitch,
            sequence: 1,
        })
    );
}

#[test]
fn srs_safe_001_halt_transition_is_observable() {
    // The in-memory SRS-LOG-001 groundwork: the transition records the reason and a monotonic
    // sequence (no wall-clock time — the 1s observability SLA is deferred).
    let mut gate = HaltablePaperEngine::running_default();
    assert!(gate.last_transition().is_none());
    gate.halt(HaltReason::KillSwitch);
    let transition = gate.last_transition().expect("transition recorded");
    assert_eq!(transition.reason, HaltReason::KillSwitch);
    assert_eq!(transition.sequence, 1);
}

#[test]
fn srs_safe_001_gate_does_not_mask_fill_native_errors_while_running() {
    // A Running gate surfaces the inner engine's fail-closed guard UNCHANGED as HaltError::Sim —
    // it never swallows a fill error or misreports it as a halt.
    let gate = HaltablePaperEngine::running_default();
    assert_eq!(
        gate.simulate_fill(7, "AAPL", 100, 0, None),
        Err(HaltError::Sim(SimError::NonPositivePrice {
            ts: 7,
            price_minor: 0
        }))
    );
    assert_eq!(
        gate.simulate_fill(1, "   ", 100, 10_000, None),
        Err(HaltError::Sim(SimError::EmptySymbol))
    );
}

#[test]
fn srs_safe_001_fill_then_halt_then_refuse_sequence() {
    // The negative-control sequence: the SAME order fills while Running and is refused after halt,
    // so the gate flips behavior exactly at the transition.
    let mut gate = HaltablePaperEngine::running_default();
    assert!(gate.simulate_fill(1, "AAPL", 100, 10_000, None).is_ok());
    gate.halt(HaltReason::KillSwitch);
    assert_eq!(
        gate.simulate_fill(1, "AAPL", 100, 10_000, None),
        Err(HaltError::Halted {
            reason: HaltReason::KillSwitch
        })
    );
}

#[test]
fn srs_safe_001_is_deterministic_across_identical_sequences() {
    // No clock/RNG: two engines driven through the identical sequence agree byte-for-byte.
    let drive = || {
        let mut gate = HaltablePaperEngine::running_default();
        let running = gate.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
        gate.halt(HaltReason::KillSwitch);
        let halted = gate.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
        (running, halted, gate.last_transition())
    };
    assert_eq!(drive(), drive());
}
