"""Contract tests for the SRS-SAFE-001 paper-engine HALTED gate sub-component.

SRS-SAFE-001 / SyRS SYS-44a / NFR-P3 / NFR-SC1 / StRS SN-1.11 — the kill switch must, among the
QuantConnect-Liquidate sequence, transition paper simulation engines to the HALTED state "with no
further on_fill callbacks emitted". This slice ships ONE named sub-component of that clause: the
per-engine Running -> Halted transition and the un-bypassable refuse-to-fill gate, in
``crates/atp-simulation`` (module ``halt``). SRS-SAFE-001 STAYS ``passes:false`` -- the full
sequence (IB cancel/disconnect, orchestrated activation + 5s budget, SRS-LOG-001 observability,
email/SMS, dashboard/CLI/REST trigger) is deferred to its named owners.

Mirrors ``tests/test_virtual_ledger_contract.py``: shells out to ``tools/sim_halt_check.py``, then
exercises each per-check function in-process, including negative spot-checks that mutate the Rust
source / Cargo.toml in memory and assert the contract actually catches the regression (a publicly
exposed inner engine, an injected ``fn into_inner`` escape hatch, a gate that delegates before the
halted guard, a dropped idempotency arm, a missing ``From<SimError>``, an injected broker
dependency, a leaked vendor token).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_halt_check import (  # noqa: E402
    SimHaltCheckError,
    assert_sim_halt_static,
    cargo_source,
    check_gate_not_clonable,
    check_gate_unbypassable,
    check_halt_error_enum,
    check_halt_idempotent,
    check_module_reexport,
    check_no_broker_dependency,
    check_simulate_fill_gate,
    check_state_enum,
    check_vendor_isolation,
    halt_source,
    lib_source,
    load_config,
)


class HaltScriptTest(unittest.TestCase):
    def test_srs_safe_001_halt_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_halt_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SAFE-001 HALT-GATE PASS", result.stdout)
        for needle in (
            "PaperEngineState with Running, Halted",
            "HaltError (Halted, Sim) and composes SimError via `impl From<SimError> for HaltError`",
            "PRIVATE engine: PaperSimulationEngine with no public field, accessor, Deref, or "
            "into_inner escape hatch -- a held gate is sealed",
            "does not derive or implement Clone",
            "returns HaltError::Halted BEFORE delegating to self.engine.simulate_fill",
            "halt is idempotent",
        ):
            self.assertIn(needle, result.stdout, result.stdout)
        # The contract must NOT over-claim: SRS-SAFE-001 stays passes:false and the deferred owners
        # of the rest of the sequence are named.
        self.assertIn("SRS-SAFE-001 stays passes:false", result.stdout)
        for owner in ("SRS-EXE-006", "SRS-EXE-002", "SRS-LOG-001", "SRS-NOTIF-001", "SRS-API-001"):
            self.assertIn(owner, result.stdout, result.stdout)

    def test_static_checks_pass_against_the_real_sources(self) -> None:
        config = load_config()
        evidence = assert_sim_halt_static(config)
        self.assertTrue(all(isinstance(line, str) and line for line in evidence))


class HaltContractNegativeTest(unittest.TestCase):
    """Each guard must be non-vacuous: a mutated source is caught."""

    def setUp(self) -> None:
        self.config = load_config()
        self.halt_src = halt_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo = cargo_source(self.config)

    def test_public_inner_engine_is_caught(self) -> None:
        # Exposing the inner engine publicly makes the gate bypassable.
        mutated = self.halt_src.replace(
            "engine: PaperSimulationEngine,", "pub engine: PaperSimulationEngine,", 1
        )
        with self.assertRaises(SimHaltCheckError):
            check_gate_unbypassable(self.config, mutated)

    def test_clone_derive_is_caught(self) -> None:
        # Deriving Clone would let a pre-halt copy keep filling after the original is halted.
        mutated = self.halt_src.replace(
            "#[derive(Debug)]\npub struct HaltablePaperEngine",
            "#[derive(Debug, Clone)]\npub struct HaltablePaperEngine",
            1,
        )
        self.assertNotEqual(mutated, self.halt_src, "mutation must add the Clone derive")
        with self.assertRaises(SimHaltCheckError):
            check_gate_not_clonable(self.config, mutated)

    def test_into_inner_escape_hatch_is_caught(self) -> None:
        # An into_inner that hands out the inner engine is an escape hatch around the gate.
        mutated = self.halt_src.replace(
            "    fn halted_reason(&self) -> HaltReason {",
            "    pub fn into_inner(self) -> PaperSimulationEngine {\n        self.engine\n    }\n\n"
            "    fn halted_reason(&self) -> HaltReason {",
            1,
        )
        with self.assertRaises(SimHaltCheckError):
            check_gate_unbypassable(self.config, mutated)

    def test_gate_missing_halted_guard_is_caught(self) -> None:
        # The gate MUST return HaltError::Halted when halted, before delegating. Breaking the
        # halted-guard token in the gate body (the first occurrence is inside simulate_fill) drops
        # the guard so a halted engine could reach the fill -- the check must catch it.
        # Target the gate's own `Err(HaltError::Halted {` (the first such call is inside
        # simulate_fill; the module doc only mentions the bare type, never `Err(...)`).
        mutated = self.halt_src.replace("Err(HaltError::Halted {", "Err(HaltErrorHalted {", 1)
        self.assertNotEqual(mutated, self.halt_src, "mutation must alter the gate body")
        with self.assertRaises(SimHaltCheckError):
            check_simulate_fill_gate(self.config, mutated)

    def test_dropped_idempotency_arm_is_caught(self) -> None:
        mutated = self.halt_src.replace("HaltOutcome::AlreadyHalted", "HaltOutcome::Transitioned")
        with self.assertRaises(SimHaltCheckError):
            check_halt_idempotent(self.config, mutated)

    def test_missing_from_simerror_is_caught(self) -> None:
        mutated = self.halt_src.replace("impl From<SimError> for HaltError", "impl FromNothing")
        with self.assertRaises(SimHaltCheckError):
            check_halt_error_enum(self.config, mutated)

    def test_dropped_state_variant_is_caught(self) -> None:
        mutated = self.halt_src.replace("    Halted,\n", "    Suspended,\n", 1)
        with self.assertRaises(SimHaltCheckError):
            check_state_enum(self.config, mutated)

    def test_dropped_lib_reexport_is_caught(self) -> None:
        # Rename (not comment out): a compacted `//pubmodhalt;` would still contain the token, so
        # the regression that actually drops the gate from the engine is a renamed module.
        mutated = self.lib_src.replace("pub mod halt;", "pub mod halt_removed;")
        self.assertNotEqual(mutated, self.lib_src, "mutation must alter the re-export")
        with self.assertRaises(SimHaltCheckError):
            check_module_reexport(self.config, mutated)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(SimHaltCheckError):
            check_no_broker_dependency(self.config, mutated)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.halt_src + "\n// fills routed through ib_insync under the hood\n"
        with self.assertRaises(SimHaltCheckError):
            check_vendor_isolation(self.config, mutated)


if __name__ == "__main__":
    unittest.main()
