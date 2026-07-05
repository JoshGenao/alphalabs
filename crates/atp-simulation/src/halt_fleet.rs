//! Paper-engine **fleet halt fan-out** — the multi-engine composition of the
//! SRS-SAFE-001 per-engine HALTED gate (SyRS SYS-44a (b); NFR-SC1).
//!
//! [`crate::halt::HaltablePaperEngine`] seals ONE engine; the kill switch
//! must halt **all** paper simulation engines. [`PaperEngineFleet`] owns a
//! set of halt gates (moved in at registration, so no unhalted handle
//! escapes — the same sealed-ownership argument as the per-engine gate) and
//! [`PaperEngineFleet::halt_all`] visits **every** registered engine,
//! idempotently, returning a [`FleetHaltReport`] whose counts satisfy
//! `transitioned + already_halted == engines_total` by construction.
//!
//! Honest scope (unchanged from the per-engine gate): the fleet seals the
//! engines it HOLDS. Guaranteeing every non-live strategy in the system is
//! HOSTED on a fleet-registered gate is the Strategy Orchestrator's routing
//! responsibility (SRS-EXE-002, deferred — see
//! `architecture/runtime_services.json`
//! `kill_switch_activation_contract.deferred[]`). This module provides the
//! fan-out primitive that routing composes; the activation gate
//! (`atp-execution::kill_switch`) consumes it through the `PaperHaltFanout`
//! port, keeping the two crates independent (the orchestrator wires them).

use std::collections::BTreeMap;
use std::fmt;

use crate::halt::{HaltError, HaltOutcome, HaltReason, HaltablePaperEngine, PaperEngineState};
use crate::sim::PaperFill;

/// Why an engine could not be registered on the fleet.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FleetError {
    /// Two engines under one id would make "halt all" silently drop one.
    DuplicateEngineId { engine_id: String },
    /// A blank id cannot be reported on an operator surface.
    BlankEngineId,
}

impl fmt::Display for FleetError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateEngineId { engine_id } => write!(
                formatter,
                "SRS-SAFE-001: duplicate paper-engine id {engine_id:?} — a second engine under \
                 one id would escape the fleet halt",
            ),
            Self::BlankEngineId => write!(
                formatter,
                "SRS-SAFE-001: blank paper-engine id — the halt report could not name the engine",
            ),
        }
    }
}

impl std::error::Error for FleetError {}

/// The result of one fleet-wide halt: per-engine outcomes plus the totals the
/// activation report carries. `transitioned + already_halted == engines_total`
/// always holds — every registered engine is visited, none skipped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FleetHaltReport {
    pub engines_total: u64,
    pub transitioned: u64,
    pub already_halted: u64,
    /// Per-engine outcome, in stable (sorted-id) order.
    pub outcomes: Vec<(String, HaltOutcome)>,
}

/// A fleet of sealed paper-engine halt gates, keyed by engine id.
///
/// Engines are MOVED in: once registered, the only fill path is
/// [`PaperEngineFleet::simulate_fill_on`] (which delegates to the gate, so a
/// halted engine refuses) and the only halt path is fleet-wide
/// [`PaperEngineFleet::halt_all`]. There is no per-engine removal or
/// accessor returning `&HaltablePaperEngine`/`&mut HaltablePaperEngine` — an
/// engine cannot be pulled back out from under the fleet halt.
#[derive(Debug, Default)]
pub struct PaperEngineFleet {
    engines: BTreeMap<String, HaltablePaperEngine>,
}

impl PaperEngineFleet {
    pub fn new() -> Self {
        Self::default()
    }

    /// Register an engine under `engine_id`. Fails closed on a blank or
    /// duplicate id (a silently-replaced engine would escape the halt).
    pub fn register(
        &mut self,
        engine_id: impl Into<String>,
        engine: HaltablePaperEngine,
    ) -> Result<(), FleetError> {
        let engine_id = engine_id.into();
        if engine_id.trim().is_empty() {
            return Err(FleetError::BlankEngineId);
        }
        if self.engines.contains_key(&engine_id) {
            return Err(FleetError::DuplicateEngineId { engine_id });
        }
        self.engines.insert(engine_id, engine);
        Ok(())
    }

    pub fn len(&self) -> usize {
        self.engines.len()
    }

    pub fn is_empty(&self) -> bool {
        self.engines.is_empty()
    }

    /// Halt EVERY registered engine (SYS-44a (b)). Idempotent: an
    /// already-halted engine is counted as `already_halted`, never an error,
    /// and its original transition (reason + sequence) is preserved by the
    /// per-engine gate. An empty fleet returns an all-zero report (nothing to
    /// halt is not a failure — the caller's report still records it).
    pub fn halt_all(&mut self, reason: HaltReason) -> FleetHaltReport {
        let mut transitioned = 0_u64;
        let mut already_halted = 0_u64;
        let mut outcomes = Vec::with_capacity(self.engines.len());
        for (engine_id, engine) in &mut self.engines {
            let outcome = engine.halt(reason);
            match outcome {
                HaltOutcome::Transitioned(_) => transitioned += 1,
                HaltOutcome::AlreadyHalted { .. } => already_halted += 1,
            }
            outcomes.push((engine_id.clone(), outcome));
        }
        FleetHaltReport {
            engines_total: self.engines.len() as u64,
            transitioned,
            already_halted,
            outcomes,
        }
    }

    /// `true` iff every registered engine is HALTED (vacuously true for an
    /// empty fleet).
    pub fn all_halted(&self) -> bool {
        self.engines
            .values()
            .all(|engine| engine.state() == PaperEngineState::Halted)
    }

    /// Drive one registered engine's fill path THROUGH its gate — the test
    /// seam proving no registered engine can fill after `halt_all`. Returns
    /// `None` for an unknown engine id (distinguishable from a halted
    /// refusal, which is `Some(Err(HaltError::Halted { .. }))`).
    #[allow(clippy::too_many_arguments)]
    pub fn simulate_fill_on(
        &self,
        engine_id: &str,
        ts: u64,
        symbol: &str,
        quantity: i64,
        price_minor: i64,
        observed_spread_minor: Option<i64>,
    ) -> Option<Result<PaperFill, HaltError>> {
        self.engines.get(engine_id).map(|engine| {
            engine.simulate_fill(ts, symbol, quantity, price_minor, observed_spread_minor)
        })
    }
}
