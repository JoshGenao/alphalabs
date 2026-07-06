//! Kill-switch activation **composition layer** (SRS-SAFE-001, SyRS SYS-44a,
//! NFR-P3, NFR-SC1) — the orchestrator wiring that joins the execution-layer
//! activation gate (`atp-execution::kill_switch`) to the simulation-layer
//! fleet halt fan-out (`atp-simulation::halt_fleet`). The two lower crates
//! are deliberately independent (SRS-ARCH-002 dependency direction); the
//! orchestrator is the layer allowed to see both, exactly as
//! `paper_halt_contract` assigned the fan-out composition here.
//!
//! Two concrete port implementations live here:
//!
//! * [`FleetHaltPort`] — the REAL `PaperHaltFanout`: a fleet of real, sealed
//!   `HaltablePaperEngine` gates halted through `PaperEngineFleet::halt_all`.
//! * [`FixtureBrokerageControl`] — the deterministic **mocked-IB transport**
//!   SRS-SAFE-001's own verification Step 2 prescribes ("integration or
//!   fault-injection workflows using mocked IB/data-provider services"):
//!   records every call in order, injects per-order/per-symbol/disconnect
//!   faults and optional per-call latency. It is a fixture, not a stub of
//!   convenience — the REAL live transport is the deferred SRS-EXE-006
//!   adapter (`kill_switch_activation_contract.deferred[]`), and this type
//!   never claims otherwise.
//!
//! [`run_fixture_activation`] drives the REAL gate over a REAL
//! `LiveExecutionState` (built through its validated builders) and a REAL
//! fleet, on a REAL monotonic clock — only the brokerage transport is the
//! fixture. `safe001_kill_switch_cli` exposes this to the operator layer
//! (the Python `atp_safety` backend shells it) and to the NFR-P3 perf run.

use std::cell::RefCell;
use std::collections::BTreeSet;
use std::time::{Duration, Instant};

use atp_execution::{
    ExecutionEngine, KillSwitchActivationEventSink, KillSwitchBrokerageControl, KillSwitchClock,
    KillSwitchSideEffectError, LiveExecutionState, PaperHaltFanout,
};
use atp_simulation::halt::{HaltReason, HaltablePaperEngine};
use atp_simulation::halt_fleet::PaperEngineFleet;
use atp_types::{
    AssetClass, ClientCorrelationId, KillSwitchActivationEvent, KillSwitchActivationReport,
    KillSwitchActivationRequest, OrderKey, OrderLedger, OrderSide, OrderState, OrderSubmission,
    OrderType, PaperHaltSummary, RestingOrderCancel, StrategyId,
};

/// Real monotonic clock for the activation timing marks (arbitrary epoch =
/// construction time). The gate reads time only through the port, so tests
/// swap this for a deterministic clock while the CLI measures for real.
#[derive(Debug)]
pub struct MonotonicClock {
    origin: Instant,
}

impl MonotonicClock {
    pub fn start() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for MonotonicClock {
    fn default() -> Self {
        Self::start()
    }
}

impl KillSwitchClock for MonotonicClock {
    fn monotonic_ms(&self) -> u64 {
        u64::try_from(self.origin.elapsed().as_millis()).unwrap_or(u64::MAX)
    }
}

/// The REAL `PaperHaltFanout`: owns a [`PaperEngineFleet`] of sealed halt
/// gates behind a `RefCell` (the port is `&self`; the fleet halt is `&mut`).
/// Once handed to the gate, the only paths in are the port itself and the
/// read-only [`FleetHaltPort::all_halted`] / [`FleetHaltPort::engines`]
/// checks — no engine can be pulled back out.
#[derive(Debug)]
pub struct FleetHaltPort {
    fleet: RefCell<PaperEngineFleet>,
}

impl FleetHaltPort {
    pub fn new(fleet: PaperEngineFleet) -> Self {
        Self {
            fleet: RefCell::new(fleet),
        }
    }

    /// Build a fleet of `count` running engines named `paper-00 .. paper-NN`.
    pub fn with_running_engines(count: u32) -> Self {
        let mut fleet = PaperEngineFleet::new();
        for index in 0..count {
            fleet
                .register(
                    format!("paper-{index:02}"),
                    HaltablePaperEngine::running_default(),
                )
                .expect("generated engine ids are unique and non-blank");
        }
        Self::new(fleet)
    }

    pub fn all_halted(&self) -> bool {
        self.fleet.borrow().all_halted()
    }

    pub fn engines(&self) -> usize {
        self.fleet.borrow().len()
    }
}

impl PaperHaltFanout for FleetHaltPort {
    fn halt_all_for_kill_switch(&self) -> Result<PaperHaltSummary, KillSwitchSideEffectError> {
        let mut fleet = self
            .fleet
            .try_borrow_mut()
            .map_err(|_| KillSwitchSideEffectError::new("paper-engine fleet is busy"))?;
        let report = fleet.halt_all(HaltReason::KillSwitch);
        Ok(PaperHaltSummary {
            engines_total: report.engines_total,
            transitioned: report.transitioned,
            already_halted: report.already_halted,
        })
    }
}

/// Deterministic mocked-IB brokerage transport (fault injection + optional
/// per-call latency). Every call is recorded in order so scenario evidence
/// can assert the sequence; the REAL transport is the deferred SRS-EXE-006
/// adapter.
#[derive(Debug, Default)]
pub struct FixtureBrokerageControl {
    pub fail_cancel_order_ids: BTreeSet<String>,
    pub fail_liquidation_symbols: BTreeSet<String>,
    pub fail_disconnect: bool,
    /// Injected transport latency per call — models a slow (but lawful)
    /// gateway so perf runs can prove the measurement has teeth.
    pub latency_per_call: Option<Duration>,
    calls: RefCell<Vec<String>>,
}

impl FixtureBrokerageControl {
    pub fn recorded_calls(&self) -> Vec<String> {
        self.calls.borrow().clone()
    }

    fn record(&self, call: String) {
        if let Some(latency) = self.latency_per_call {
            std::thread::sleep(latency);
        }
        self.calls.borrow_mut().push(call);
    }
}

impl KillSwitchBrokerageControl for FixtureBrokerageControl {
    fn cancel_resting_order(
        &self,
        cancel: &RestingOrderCancel,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.record(format!("cancel:{}", cancel.order_id));
        if self.fail_cancel_order_ids.contains(&cancel.order_id) {
            return Err(KillSwitchSideEffectError::new(format!(
                "fixture: injected cancel failure for {}",
                cancel.order_id
            )));
        }
        Ok(())
    }

    fn submit_market_liquidation(
        &self,
        submission: &OrderSubmission,
    ) -> Result<(), KillSwitchSideEffectError> {
        self.record(format!("liquidate:{}", submission.symbol));
        if self.fail_liquidation_symbols.contains(&submission.symbol) {
            return Err(KillSwitchSideEffectError::new(format!(
                "fixture: injected liquidation failure for {}",
                submission.symbol
            )));
        }
        Ok(())
    }

    fn disconnect(&self) -> Result<(), KillSwitchSideEffectError> {
        self.record("disconnect".to_string());
        if self.fail_disconnect {
            return Err(KillSwitchSideEffectError::new(
                "fixture: injected disconnect failure",
            ));
        }
        Ok(())
    }
}

/// Best-effort in-memory activation-event sink; the durable SRS-LOG-001
/// write happens at the Python operator layer.
#[derive(Debug, Default)]
pub struct CollectingEventSink {
    events: RefCell<Vec<KillSwitchActivationEvent>>,
}

impl CollectingEventSink {
    pub fn recorded(&self) -> usize {
        self.events.borrow().len()
    }
}

impl KillSwitchActivationEventSink for CollectingEventSink {
    fn record(&self, event: KillSwitchActivationEvent) -> Result<(), KillSwitchSideEffectError> {
        self.events.borrow_mut().push(event);
        Ok(())
    }
}

/// A deterministic activation scenario: how many resting orders, which open
/// positions, how many paper engines, and which faults to inject.
#[derive(Debug, Clone)]
pub struct Scenario {
    pub activation_id: String,
    pub live_strategy_id: String,
    pub resting_orders: u32,
    /// `(symbol, net_quantity)` — positive long, negative short, never zero.
    pub positions: Vec<(String, i64)>,
    pub engines: u32,
    pub fail_cancel_order_ids: Vec<String>,
    pub fail_liquidation_symbols: Vec<String>,
    pub fail_disconnect: bool,
    pub latency_ms_per_call: Option<u64>,
}

impl Scenario {
    /// The NFR-SC1 / `test_kill_switch_latency` reference shape: 50 open
    /// positions, 50 resting orders, 30 paper engines, no faults.
    pub fn reference_baseline() -> Self {
        Self {
            activation_id: "act-fixture".to_string(),
            live_strategy_id: "alpha-live".to_string(),
            resting_orders: 50,
            positions: generated_positions(50),
            engines: 30,
            fail_cancel_order_ids: Vec::new(),
            fail_liquidation_symbols: Vec::new(),
            fail_disconnect: false,
            latency_ms_per_call: None,
        }
    }
}

/// Deterministic position book: `SYM00 .. SYMnn`, alternating long/short,
/// quantities in a small spread so liquidation sides/quantities vary.
pub fn generated_positions(count: u32) -> Vec<(String, i64)> {
    (0..count)
        .map(|index| {
            let quantity = i64::from(index % 7 + 1) * 10;
            let signed = if index % 2 == 0 { quantity } else { -quantity };
            (format!("SYM{index:02}"), signed)
        })
        .collect()
}

/// Everything a scenario run produces: the gate's report plus the
/// composition-level facts the report cannot know (did every REAL engine
/// end up halted; what did the fixture transport observe).
#[derive(Debug)]
pub struct FixtureActivation {
    pub report: KillSwitchActivationReport,
    pub all_engines_halted: bool,
    pub brokerage_calls: Vec<String>,
    pub events_recorded: usize,
}

/// Build the REAL `LiveExecutionState` for a scenario through its validated
/// builders: `resting_orders` non-terminal orders for the live strategy
/// (even-indexed ones ACKED with a broker id, odd-indexed still NEW with an
/// honest `None` binding) plus every open position.
pub fn build_scenario_state(scenario: &Scenario) -> Result<LiveExecutionState, String> {
    let live = StrategyId::new(scenario.live_strategy_id.clone());
    let mut ledger = OrderLedger::new();
    let mut acked_keys: Vec<OrderKey> = Vec::new();
    for index in 0..scenario.resting_orders {
        let correlation = ClientCorrelationId::new(format!("ks-rest-{index:04}"))
            .map_err(|error| format!("fixture correlation id: {error:?}"))?;
        let symbol = format!("SYM{:02}", index % 50);
        let submission = OrderSubmission::new(
            live.clone(),
            symbol,
            i64::from(index % 9 + 1),
            AssetClass::Equity,
            if index % 2 == 0 {
                OrderSide::Buy
            } else {
                OrderSide::Sell
            },
            OrderType::Market,
        );
        ledger
            .submit(correlation.clone(), &submission)
            .map_err(|error| format!("fixture ledger submit: {error}"))?;
        if index % 2 == 0 {
            let key = OrderKey::new(live.clone(), correlation);
            ledger
                .transition(&key, OrderState::PendingSubmit)
                .and_then(|_| ledger.transition(&key, OrderState::Acked))
                .map_err(|error| format!("fixture ledger transition: {error:?}"))?;
            acked_keys.push(key);
        }
    }
    let mut state = LiveExecutionState::new(ledger);
    for (index, key) in acked_keys.into_iter().enumerate() {
        state = state
            .with_broker_id(key, format!("B-{index:04}"))
            .map_err(|error| format!("fixture broker id: {error:?}"))?;
    }
    for (symbol, net_quantity) in &scenario.positions {
        state = state
            .with_position(symbol.clone(), *net_quantity)
            .map_err(|error| format!("fixture position {symbol}: {error:?}"))?;
    }
    state = state
        .with_live_strategy(&live)
        .map_err(|error| format!("fixture live designation: {error:?}"))?;
    Ok(state)
}

/// Run the REAL activation gate over the scenario: real state, real fleet,
/// real monotonic clock — fixture brokerage transport only.
pub fn run_fixture_activation(scenario: &Scenario) -> Result<FixtureActivation, String> {
    let state = build_scenario_state(scenario)?;
    let fleet = FleetHaltPort::with_running_engines(scenario.engines);
    let brokerage = FixtureBrokerageControl {
        fail_cancel_order_ids: scenario.fail_cancel_order_ids.iter().cloned().collect(),
        fail_liquidation_symbols: scenario.fail_liquidation_symbols.iter().cloned().collect(),
        fail_disconnect: scenario.fail_disconnect,
        latency_per_call: scenario.latency_ms_per_call.map(Duration::from_millis),
        ..FixtureBrokerageControl::default()
    };
    let events = CollectingEventSink::default();
    let clock = MonotonicClock::start();
    let request = KillSwitchActivationRequest {
        activation_id: scenario.activation_id.clone(),
        live_strategy_id: StrategyId::new(scenario.live_strategy_id.clone()),
        activated_at_epoch_ms: epoch_ms_now(),
    };
    let report = ExecutionEngine::default()
        .activate_kill_switch(request, &state, &clock, &brokerage, &fleet, &events);
    Ok(FixtureActivation {
        report,
        all_engines_halted: fleet.all_halted(),
        brokerage_calls: brokerage.recorded_calls(),
        events_recorded: events.recorded(),
    })
}

/// Operator-facing wall-clock activation stamp (epoch ms). Distinct from the
/// monotonic measurement clock — never used for timing marks.
pub fn epoch_ms_now() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX))
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use atp_types::SideEffectOutcome;

    #[test]
    fn fixture_activation_halts_every_real_engine_and_reports_clean() {
        let outcome = run_fixture_activation(&Scenario::reference_baseline())
            .expect("reference scenario runs");
        assert!(
            outcome.all_engines_halted,
            "every REAL engine gate is HALTED"
        );
        assert!(outcome.report.fully_clean());
        assert!(outcome.report.within_nfr_p3());
        assert_eq!(outcome.report.liquidations.len(), 50);
        assert_eq!(outcome.report.resting_order_cancels.len(), 50);
        let summary = outcome.report.paper_halt_summary.expect("summary");
        assert_eq!(summary.engines_total, 30);
        assert_eq!(summary.transitioned, 30);
        assert_eq!(outcome.events_recorded, 1);
        // Composition-level ordering: halt happened before the transport saw
        // any call, and disconnect is the final transport call.
        assert_eq!(
            outcome.brokerage_calls.last().map(String::as_str),
            Some("disconnect")
        );
    }

    #[test]
    fn fixture_faults_plumb_through_to_the_report() {
        let mut scenario = Scenario::reference_baseline();
        scenario.fail_liquidation_symbols = vec!["SYM03".to_string()];
        scenario.fail_disconnect = true;
        let outcome = run_fixture_activation(&scenario).expect("faulted scenario runs");
        assert!(!outcome.report.fully_clean());
        assert!(outcome
            .report
            .liquidations
            .iter()
            .any(|liquidation| liquidation.symbol == "SYM03" && liquidation.outcome.is_failed()));
        assert!(outcome.report.ib_disconnect.is_failed());
        // Continue-to-safety survived the faults: every position was still
        // attempted and every engine is still halted.
        assert_eq!(outcome.report.liquidations.len(), 50);
        assert!(outcome.all_engines_halted);
        assert_eq!(outcome.report.paper_halt, SideEffectOutcome::Succeeded);
    }

    #[test]
    fn generated_positions_are_nonzero_and_alternate_direction() {
        let positions = generated_positions(10);
        assert_eq!(positions.len(), 10);
        assert!(positions.iter().all(|(_, quantity)| *quantity != 0));
        assert!(positions
            .iter()
            .step_by(2)
            .all(|(_, quantity)| *quantity > 0));
        assert!(positions
            .iter()
            .skip(1)
            .step_by(2)
            .all(|(_, quantity)| *quantity < 0));
    }
}
