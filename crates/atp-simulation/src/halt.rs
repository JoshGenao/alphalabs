//! Paper-simulation engine **HALTED lifecycle gate** — the paper-engine sub-component of
//! the kill switch, **SRS-SAFE-001** (SyRS SYS-44a; NFR-P3; NFR-SC1; StRS SN-1.11).
//!
//! # The acceptance clause this realizes
//!
//! SRS-SAFE-001's acceptance criterion includes: "paper simulation engines transition to the
//! HALTED state with no further `on_fill` callbacks emitted". On kill-switch activation every
//! paper simulation engine must stop producing fills so no further simulated `on_fill` callback
//! can reach strategy code. This module owns that single transition for one engine.
//!
//! # Domain-level realization (honest scope)
//!
//! There is no callback-emitting runtime loop and no Python strategy host yet (both deferred —
//! see below), so "no further `on_fill` callbacks emitted" is realized at the **domain level**: a
//! HALTED [`HaltablePaperEngine`] **refuses to PRODUCE a [`PaperFill`]** — [`HaltablePaperEngine::simulate_fill`]
//! returns [`HaltError::Halted`] with no fill value. No fill exists, so nothing can drive an
//! `on_fill` callback. This is the strongest honest realization of the clause available without
//! the deferred runtime; it is NOT the full SRS-SAFE-001 sequence.
//!
//! # The sealed gate (and what it does NOT claim)
//!
//! [`HaltablePaperEngine`] OWNS a **private** [`PaperSimulationEngine`]: the `engine` field is not
//! `pub`, there is no accessor returning `&PaperSimulationEngine`, no [`std::ops::Deref`], no
//! `into_inner`, and the gate is not [`Clone`]. So for a given gate value the ONLY path to a paper
//! fill is the gate's own [`HaltablePaperEngine::simulate_fill`], which checks the run state first —
//! once halted, that gate is SEALED: it cannot be coerced into a fill, and no pre-halt copy can
//! outlive the halt. The inner [`PaperSimulationEngine::simulate_fill`] stays a pure, stateless,
//! deterministic cost path and is not modified by this module; the gate only READS its own state
//! (`&self`) to decide, so a fill never mutates the engine and determinism is preserved.
//!
//! This is NOT a claim that every paper fill in the whole system flows through the gate: the bare
//! [`PaperSimulationEngine`] stays a public fill primitive (backtests, the SRS-SIM-001/002/003
//! operator CLIs, and the SRS-SIM-003 virtual-ledger path construct and drive it directly), so a
//! caller can still build a bare engine and fill OUTSIDE the gate. Guaranteeing that every non-live
//! strategy is HOSTED on a halt-aware engine (so a kill switch actually reaches all of them) is the
//! Strategy Orchestrator's routing responsibility (SRS-EXE-002), which is deferred. This slice
//! provides the per-engine sealed-halt primitive that orchestrator will compose.
//!
//! # What is real here vs deferred
//!
//! This module ships ONE named sub-component: the per-engine Running -> Halted transition and the
//! refuse-to-fill gate. **SRS-SAFE-001 stays `passes:false`.** The activation layers above it now
//! exist as the SRS-SAFE-001 runtime slice
//! (`architecture/runtime_services.json#kill_switch_activation_contract`): the multi-engine fan-out
//! is [`crate::halt_fleet::PaperEngineFleet`]; the operator-triggered activation that fans the halt
//! out, cancels/liquidates, measures the 5-second NFR-P3 budget, and stamps the halt mark against
//! the 1-second SRS-LOG-001 observability budget is `atp-execution`'s
//! `kill_switch::activate_kill_switch` gate (composed by the orchestrator + operator runtime).
//! Still genuinely deferred and owned elsewhere
//! (`architecture/runtime_services.json#paper_halt_contract.deferred`): the REAL brokerage
//! transport behind the cancel/disconnect port is the SRS-EXE-006 adapter; hosting every non-live
//! strategy on a fleet-registered gate is the SRS-EXE-002 orchestrator's routing job; the SRS-LOG-001
//! feature's own dashboard-viewing flip ([`HaltTransition`] remains the in-memory groundwork,
//! carrying no wall-clock time); operator email/SMS is SRS-NOTIF-001; the rich dashboard control is
//! UI-4; the `on_fill` callback runtime + Python strategy host are SRS-SDK / SRS-EXE-002.

use std::fmt;

use crate::sim::{PaperFill, PaperSimulationEngine, SimError};

/// The run state of a paper simulation engine (SRS-SAFE-001).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PaperEngineState {
    /// Accepting fills normally.
    Running,
    /// Kill-switch halted: the engine produces no further fills.
    Halted,
}

/// Why a paper engine was halted.
///
/// Only the kill switch halts an engine today; the variant set is intentionally closed (a
/// speculative variant would be dead code). New producers add a variant when they are built.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaltReason {
    /// SRS-SAFE-001 kill-switch activation.
    KillSwitch,
}

impl HaltReason {
    /// A stable, lowercase-hyphenated tag for logs / operator surfaces.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::KillSwitch => "kill-switch",
        }
    }
}

/// A recorded HALTED-state transition — the in-memory groundwork for SRS-LOG-001 observability.
///
/// Carries the [`HaltReason`] and a monotonic per-engine `sequence` (NOT a wall-clock timestamp).
/// Emitting it to the persistent log sink within the SRS-LOG-001 1-second budget is the deferred
/// runtime's responsibility; this struct only records that a transition happened and why.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HaltTransition {
    /// Why the engine halted.
    pub reason: HaltReason,
    /// Monotonic per-engine transition sequence (starts at 1 for the first transition).
    pub sequence: u64,
}

/// The outcome of a [`HaltablePaperEngine::halt`] call — the idempotency signal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HaltOutcome {
    /// Running -> Halted: a fresh transition. Carries the recorded [`HaltTransition`].
    Transitioned(HaltTransition),
    /// Already Halted: a no-op. Carries the reason the engine was ALREADY halted for (the first
    /// transition's reason, preserved — a later `halt` never overwrites it).
    AlreadyHalted {
        /// The reason recorded by the original transition.
        reason: HaltReason,
    },
}

/// A fill request to a [`HaltablePaperEngine`] failed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HaltError {
    /// The engine is HALTED, so NO fill was produced — the SRS-SAFE-001 domain realization of
    /// "no further `on_fill` callbacks emitted".
    Halted {
        /// Why the engine is halted.
        reason: HaltReason,
    },
    /// The engine was Running and delegated to the inner [`PaperSimulationEngine`], whose own
    /// fail-closed guard rejected the fill. Carries the [`SimError`] unchanged — the gate never
    /// masks a fill-native error while Running.
    Sim(SimError),
}

impl From<SimError> for HaltError {
    fn from(error: SimError) -> Self {
        Self::Sim(error)
    }
}

impl fmt::Display for HaltError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Halted { reason } => write!(
                f,
                "paper simulation engine is halted ({}); no fill produced",
                reason.as_str()
            ),
            Self::Sim(error) => write!(f, "{error}"),
        }
    }
}

impl std::error::Error for HaltError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Halted { .. } => None,
            Self::Sim(error) => Some(error),
        }
    }
}

/// A [`PaperSimulationEngine`] wrapped in a halt-aware lifecycle (SRS-SAFE-001).
///
/// Owns the inner engine **privately** (the sealed gate, see module docs): for a given gate value
/// the only way to a paper fill is [`HaltablePaperEngine::simulate_fill`], which checks the run
/// state first. Once [`HaltablePaperEngine::halt`] flips the state to [`PaperEngineState::Halted`],
/// every fill request on this gate returns [`HaltError::Halted`] and no [`PaperFill`] is ever
/// produced from it.
///
/// Deliberately **not `Clone`**: a clone would be an independent value whose own state could stay
/// Running after the original is halted, so a pre-halt copy could keep filling — the exact bypass
/// the gate exists to prevent. Without `Clone` (and with the private inner engine, no accessor, no
/// `Deref`, no `into_inner`), a halted gate is sealed: there is no way to obtain a fill from it.
#[derive(Debug)]
pub struct HaltablePaperEngine {
    engine: PaperSimulationEngine,
    state: PaperEngineState,
    last_transition: Option<HaltTransition>,
}

impl HaltablePaperEngine {
    /// Wrap an engine in the [`PaperEngineState::Running`] state.
    pub fn new(engine: PaperSimulationEngine) -> Self {
        Self {
            engine,
            state: PaperEngineState::Running,
            last_transition: None,
        }
    }

    /// A Running gate over the shared SyRS-default [`PaperSimulationEngine`].
    pub fn running_default() -> Self {
        Self::new(PaperSimulationEngine::new())
    }

    /// Simulate a paper fill — ONLY while [`PaperEngineState::Running`].
    ///
    /// While Running, delegates to the inner engine's pure [`PaperSimulationEngine::simulate_fill`]
    /// and returns the identical [`PaperFill`] (a fill-native [`SimError`] surfaces as
    /// [`HaltError::Sim`], unmasked). Once [`PaperEngineState::Halted`], returns
    /// [`HaltError::Halted`] BEFORE touching the inner engine — no fill is produced, so no `on_fill`
    /// callback can be driven from it (the SRS-SAFE-001 clause). `&self`: the gate only READS its
    /// state to decide, so a fill never mutates the engine and determinism is preserved.
    pub fn simulate_fill(
        &self,
        ts: u64,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        observed_spread_minor: Option<i64>,
    ) -> Result<PaperFill, HaltError> {
        match self.state {
            PaperEngineState::Halted => Err(HaltError::Halted {
                reason: self.halted_reason(),
            }),
            PaperEngineState::Running => self
                .engine
                .simulate_fill(ts, symbol, quantity, price_minor, observed_spread_minor)
                .map_err(HaltError::from),
        }
    }

    /// Halt the engine (Running -> Halted). **Idempotent.**
    ///
    /// The FIRST call flips the state, records a [`HaltTransition`] with an incremented `sequence`,
    /// and returns [`HaltOutcome::Transitioned`]. Any later call is a no-op returning
    /// [`HaltOutcome::AlreadyHalted`]: the state stays Halted, the recorded transition (sequence and
    /// reason) is left untouched. There is no resume (Halted -> Running) today — that lifecycle is
    /// deferred to the orchestrator runtime — so `sequence` is 1 in practice; the derivation is
    /// monotonic groundwork for when resume lands.
    pub fn halt(&mut self, reason: HaltReason) -> HaltOutcome {
        match self.state {
            PaperEngineState::Halted => HaltOutcome::AlreadyHalted {
                reason: self.halted_reason(),
            },
            PaperEngineState::Running => {
                let sequence = self.last_transition.map_or(1, |prev| prev.sequence + 1);
                let transition = HaltTransition { reason, sequence };
                self.state = PaperEngineState::Halted;
                self.last_transition = Some(transition);
                HaltOutcome::Transitioned(transition)
            }
        }
    }

    /// The current run state.
    pub fn state(&self) -> PaperEngineState {
        self.state
    }

    /// True iff the engine is [`PaperEngineState::Halted`].
    pub fn is_halted(&self) -> bool {
        matches!(self.state, PaperEngineState::Halted)
    }

    /// The most recent HALTED-state transition, or `None` while Running and never halted.
    pub fn last_transition(&self) -> Option<HaltTransition> {
        self.last_transition
    }

    /// The reason this engine is halted. Defaults to [`HaltReason::KillSwitch`] if (impossibly)
    /// the state is Halted with no recorded transition — defensive, never reached in practice
    /// because the only writer of `Halted` ([`Self::halt`]) always records a transition first.
    fn halted_reason(&self) -> HaltReason {
        self.last_transition
            .map_or(HaltReason::KillSwitch, |t| t.reason)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn running() -> HaltablePaperEngine {
        HaltablePaperEngine::running_default()
    }

    #[test]
    fn fresh_engine_is_running() {
        let engine = running();
        assert_eq!(engine.state(), PaperEngineState::Running);
        assert!(!engine.is_halted());
        assert!(engine.last_transition().is_none());
    }

    #[test]
    fn running_engine_fills_identically_to_the_bare_engine() {
        // Invariant C/D: while Running, the gate produces EXACTLY the bare engine's fill.
        let bare = PaperSimulationEngine::new();
        let gated = HaltablePaperEngine::new(PaperSimulationEngine::new());
        let bare_fill = bare
            .simulate_fill(1, "AAPL", 100, 10_000, Some(40))
            .unwrap();
        let gated_fill = gated
            .simulate_fill(1, "AAPL", 100, 10_000, Some(40))
            .expect("running gate fills");
        assert_eq!(gated_fill, bare_fill);
    }

    #[test]
    fn halt_transitions_running_to_halted_and_records_transition() {
        let mut engine = running();
        let outcome = engine.halt(HaltReason::KillSwitch);
        assert_eq!(
            outcome,
            HaltOutcome::Transitioned(HaltTransition {
                reason: HaltReason::KillSwitch,
                sequence: 1,
            })
        );
        assert_eq!(engine.state(), PaperEngineState::Halted);
        assert!(engine.is_halted());
        assert_eq!(
            engine.last_transition(),
            Some(HaltTransition {
                reason: HaltReason::KillSwitch,
                sequence: 1,
            })
        );
    }

    #[test]
    fn halted_engine_produces_no_fill() {
        // Invariant A — the core SRS-SAFE-001 clause: once halted, NO fill is produced,
        // for inputs that would otherwise fill cleanly.
        let mut engine = running();
        engine.halt(HaltReason::KillSwitch);
        for (qty, px, spread) in [
            (100, 10_000, None),
            (-50, 9_900, Some(20)),
            (10, 12_345, Some(0)),
        ] {
            assert_eq!(
                engine.simulate_fill(1, "AAPL", qty, px, spread),
                Err(HaltError::Halted {
                    reason: HaltReason::KillSwitch
                })
            );
        }
    }

    #[test]
    fn halt_is_idempotent() {
        // Invariant B: a second halt is a no-op; state stays Halted and the recorded
        // transition (sequence + reason) is unchanged.
        let mut engine = running();
        engine.halt(HaltReason::KillSwitch);
        let again = engine.halt(HaltReason::KillSwitch);
        assert_eq!(
            again,
            HaltOutcome::AlreadyHalted {
                reason: HaltReason::KillSwitch
            }
        );
        assert_eq!(engine.state(), PaperEngineState::Halted);
        assert_eq!(
            engine.last_transition(),
            Some(HaltTransition {
                reason: HaltReason::KillSwitch,
                sequence: 1,
            })
        );
    }

    #[test]
    fn running_gate_does_not_mask_fill_native_errors() {
        // While Running, the inner engine's fail-closed guard surfaces UNCHANGED as HaltError::Sim
        // (the gate never swallows a fill error or turns it into a halt).
        let engine = running();
        assert_eq!(
            engine.simulate_fill(7, "AAPL", 100, 0, None),
            Err(HaltError::Sim(SimError::NonPositivePrice {
                ts: 7,
                price_minor: 0
            }))
        );
        assert_eq!(
            engine.simulate_fill(1, "   ", 100, 10_000, None),
            Err(HaltError::Sim(SimError::EmptySymbol))
        );
    }

    #[test]
    fn fill_then_halt_then_refuse() {
        // The single negative-control sequence: a fill succeeds while Running, then the SAME
        // order is refused after halt — the gate flips behavior exactly at the transition.
        let mut engine = running();
        let before = engine.simulate_fill(1, "AAPL", 100, 10_000, None);
        assert!(before.is_ok());
        engine.halt(HaltReason::KillSwitch);
        let after = engine.simulate_fill(1, "AAPL", 100, 10_000, None);
        assert_eq!(
            after,
            Err(HaltError::Halted {
                reason: HaltReason::KillSwitch
            })
        );
    }

    #[test]
    fn deterministic_running_and_halted() {
        // No clock/RNG: identical call sequences on two gates yield identical results.
        let build = || {
            let mut e = running();
            let a = e.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
            e.halt(HaltReason::KillSwitch);
            let b = e.simulate_fill(1, "AAPL", 137, 9_973, Some(13));
            (a, b, e.last_transition())
        };
        assert_eq!(build(), build());
    }

    #[test]
    fn halt_error_displays_without_leaking_internals() {
        let halted = HaltError::Halted {
            reason: HaltReason::KillSwitch,
        };
        assert!(halted.to_string().contains("halted"));
        assert!(halted.to_string().contains("kill-switch"));
    }
}
