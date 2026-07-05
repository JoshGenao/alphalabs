"""Contract tests for the SRS-SAFE-001 kill-switch ACTIVATION runtime slice.

Mirrors ``tests/test_paper_halt_contract.py``: shells out to
``tools/kill_switch_check.py``, exercises the per-check collectors in-process,
and proves each load-bearing guard non-vacuous with an in-memory mutation —
a reordered phase, a ``Result``-returning gate, a weakened budget, a fleet
that skips engines, a defaulted backend, a success-shaped backend failure, a
replay guard consulted after the backend fires, a reverted owner map, and a
flipped ``passes`` flag must each be CAUGHT.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from kill_switch_check import (  # noqa: E402
    KillSwitchCheckError,
    assert_kill_switch_static,
    backend_source,
    check_backend_fails_closed,
    check_budget_constants,
    check_entry_point_returns_report_not_result,
    check_fleet_halts_every_engine,
    check_handlers_never_fabricate,
    check_owner_repoint,
    check_phase_order,
    check_serialized_honesty,
    check_wiring_requires_explicit_backend,
    fleet_source,
    gate_source,
    handlers_source,
    load_config,
    owners_source,
    types_source,
    wiring_source,
)


class KillSwitchScriptTest(unittest.TestCase):
    def test_srs_safe_001_activation_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/kill_switch_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SAFE-001 ACTIVATION-GATE PASS", result.stdout)
        for needle in (
            "continue-to-safety is structural",
            "halt_all_for_kill_switch -> cancel_resting_order -> "
            "submit_market_liquidation -> disconnect",
            "KILL_SWITCH_ACTIVATION_BUDGET_MS=5000ms",
            "visits every registered engine",
            "required keyword-only argument with no default",
            "never success-shaped",
            "replay guard consulted before the backend",
            "re-pointed to SRS-SAFE-001",
            "stays passes:false",
        ):
            self.assertIn(needle, result.stdout, result.stdout)

    def test_static_checks_pass_against_the_real_sources(self) -> None:
        evidence = assert_kill_switch_static(load_config())
        self.assertTrue(all(isinstance(line, str) and line for line in evidence))


class KillSwitchContractNegativeTest(unittest.TestCase):
    """Each guard must be non-vacuous: a mutated source is caught."""

    def setUp(self) -> None:
        self.config = load_config()
        self.gate_src = gate_source(self.config)
        self.types_src = types_source(self.config)
        self.fleet_src = fleet_source(self.config)
        self.wiring_src = wiring_source(self.config)
        self.backend_src = backend_source(self.config)
        self.handlers_src = handlers_source(self.config)
        self.owners_src = owners_source(self.config)

    def test_result_returning_gate_is_caught(self) -> None:
        mutated = self.gate_src.replace(
            ") -> KillSwitchActivationReport\n    where",
            ") -> Result<KillSwitchActivationReport, KillSwitchSideEffectError>\n    where",
            1,
        )
        self.assertNotEqual(mutated, self.gate_src, "mutation must change the signature")
        with self.assertRaises(KillSwitchCheckError):
            check_entry_point_returns_report_not_result(self.config, mutated)

    def test_early_return_in_the_gate_is_caught(self) -> None:
        mutated = self.gate_src.replace(
            "let halt_completed_ms = elapsed(clock);",
            "let halt_completed_ms = elapsed(clock);\n"
            "        if paper_halt.is_failed() { return report_stub(); }",
            1,
        )
        self.assertNotEqual(mutated, self.gate_src)
        with self.assertRaises(KillSwitchCheckError):
            check_entry_point_returns_report_not_result(self.config, mutated)

    def test_reordered_phases_are_caught(self) -> None:
        # Swap the port names so "disconnect" textually precedes the halt.
        mutated = self.gate_src.replace(
            "let (paper_halt, paper_halt_summary) = match paper_engines.halt_all_for_kill_switch()",
            "let _early = brokerage.disconnect();\n"
            "        let (paper_halt, paper_halt_summary) = "
            "match paper_engines.halt_all_for_kill_switch()",
            1,
        )
        self.assertNotEqual(mutated, self.gate_src)
        with self.assertRaises(KillSwitchCheckError):
            check_phase_order(self.config, mutated)

    def test_weakened_budget_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const KILL_SWITCH_ACTIVATION_BUDGET_MS: u64 = 5_000;",
            "pub const KILL_SWITCH_ACTIVATION_BUDGET_MS: u64 = 50_000;",
            1,
        )
        self.assertNotEqual(mutated, self.types_src)
        with self.assertRaises(KillSwitchCheckError):
            check_budget_constants(self.config, mutated)

    def test_fleet_that_skips_engines_is_caught(self) -> None:
        mutated = self.fleet_src.replace(
            "for (engine_id, engine) in &mut self.engines {",
            "for (engine_id, engine) in self.engines.iter_mut().take(1) {",
            1,
        )
        self.assertNotEqual(mutated, self.fleet_src)
        with self.assertRaises(KillSwitchCheckError):
            check_fleet_halts_every_engine(self.config, mutated)

    def test_leaked_engine_reference_is_caught(self) -> None:
        mutated = self.fleet_src.replace(
            "    pub fn len(&self) -> usize {",
            "    pub fn engine(&self, id: &str) -> Option<&HaltablePaperEngine> {\n"
            "        self.engines.get(id)\n    }\n\n"
            "    pub fn len(&self) -> usize {",
            1,
        )
        self.assertNotEqual(mutated, self.fleet_src)
        with self.assertRaises(KillSwitchCheckError):
            check_fleet_halts_every_engine(self.config, mutated)

    def test_defaulted_backend_is_caught(self) -> None:
        mutated = self.wiring_src.replace(
            "backend: KillSwitchBackend,",
            "backend: KillSwitchBackend = RustCliKillSwitchBackend(),",
            1,
        )
        self.assertNotEqual(mutated, self.wiring_src)
        with self.assertRaises(KillSwitchCheckError):
            check_wiring_requires_explicit_backend(self.config, mutated)

    def test_dropped_missing_binary_guard_is_caught(self) -> None:
        # Silently tolerating a missing CLI binary would leave the kill switch
        # unable to run while looking wired.
        mutated = self.backend_src.replace("kill-switch CLI not found", "binary absent", 1)
        self.assertNotEqual(mutated, self.backend_src)
        with self.assertRaises(KillSwitchCheckError):
            check_backend_fails_closed(self.config, mutated)

    def test_tolerated_usage_exit_code_is_caught(self) -> None:
        # Accepting exit 2 (usage/fixture error, no report) as runnable would
        # let a broken invocation masquerade as an activation attempt.
        mutated = self.backend_src.replace(
            "completed.returncode not in (0, 1)", "completed.returncode not in (0, 1, 2)", 1
        )
        self.assertNotEqual(mutated, self.backend_src)
        with self.assertRaises(KillSwitchCheckError):
            check_backend_fails_closed(self.config, mutated)

    def test_replay_guard_after_backend_is_caught(self) -> None:
        # Move the guard lookup textually after the backend call.
        mutated = self.handlers_src.replace(
            "replay = _load_guard(self._state_dir)",
            "replay = None  # guard moved below",
            1,
        ).replace(
            "response = _response_from_report(outcome.report)",
            "replay = _load_guard(self._state_dir)\n"
            "        response = _response_from_report(outcome.report)",
            1,
        )
        self.assertNotEqual(mutated, self.handlers_src)
        with self.assertRaises(KillSwitchCheckError):
            check_handlers_never_fabricate(self.config, mutated)

    def test_reverted_owner_map_is_caught(self) -> None:
        mutated = self.owners_src.replace(
            '"KILL_SWITCH": "SRS-SAFE-001"', '"KILL_SWITCH": "SRS-EXE-001"', 1
        )
        self.assertNotEqual(mutated, self.owners_src)
        with self.assertRaises(KillSwitchCheckError):
            check_owner_repoint(self.config, mutated)

    def test_flipped_passes_flag_is_caught(self) -> None:
        features = json.loads((ROOT / "feature_list.json").read_text(encoding="utf-8"))
        for feature in features:
            if feature["id"] == "SRS-SAFE-001":
                feature["passes"] = True
        with self.assertRaises(KillSwitchCheckError):
            check_serialized_honesty(self.config, json.dumps(features))


if __name__ == "__main__":
    unittest.main()
