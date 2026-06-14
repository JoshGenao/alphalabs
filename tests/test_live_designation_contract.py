"""Contract tests for SRS-EXE-001 (SyRS SYS-1 / SYS-2a / SYS-2c / SYS-2d /
AC-15; NFR-P1 / NFR-S2; StRS SN-1.01 / SN-1.06 / SN-1.11).

Mirrors ``tests/test_subscription_limit_contract.py``: shells out to
``tools/live_designation_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract actually
catches regressions (a public field / ``Default`` derive on the confirmation
token, a ``Clone`` derive on the authority, the engine not owning the
authority, ``route_order`` accepting a caller-supplied authority, a renamed
registry method, a ``designate`` that drops the confirmation token, a missing
decision/error variant, a forbidden port call in the NotDesignated leaf, and a
dropped authority/delegate call).
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

from live_designation_check import (  # noqa: E402
    LiveDesignationCheckError,
    assert_live_designation_static,
    check_confirmation_token,
    check_designation_error,
    check_engine_ownership,
    check_registry,
    check_route_order_guard,
    check_routing_decision,
    execution_source,
    load_config,
    run_checks,
)


class LiveDesignationScriptTest(unittest.TestCase):
    def test_srs_exe_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/live_designation_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-EXE-001 PASS", result.stdout)
        for needle in (
            "LiveDesignationConfirmation as an explicit-confirmation token",
            "from_operator",
            "no Default derive",
            "LiveDesignation with 5 methods",
            "new, designate, demote, designated, authority_for",
            "designate requires a LiveDesignationConfirmation",
            "no Clone derive",
            "owns the authority as `designation: LiveDesignation`",
            "accepts no caller-supplied LiveDesignation",
            "LiveRoutingDecision with 2 decisions (Authorized, NotDesignated)",
            "LiveDesignationError with 4 variants",
            "MissingConfirmation, ConfirmationMismatch, AlreadyDesignated, NotDesignated",
            "route_order resolves `self.designation.authority_for`",
            "self.submit_live_order",
            "StrategyMode::Live",
            "OrderErrorCategory::NonLiveStrategySubmission",
            "consults none of 6 forbidden ports",
            "srs_exe_001_live_designation",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class ConfirmationTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_token_is_private_named_and_not_default(self) -> None:
        evidence = check_confirmation_token(self.config, self.exec_src)
        self.assertIn("LiveDesignationConfirmation", evidence)
        self.assertIn("from_operator", evidence)

    def test_public_field_on_token_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "    strategy_id: StrategyId,\n    operator_acknowledgement: String,",
            "    pub strategy_id: StrategyId,\n    operator_acknowledgement: String,",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_confirmation_token(self.config, mutated)
        self.assertIn("public field", str(ctx.exception))

    def test_default_derive_on_token_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "#[derive(Debug, Clone, PartialEq, Eq)]\npub struct LiveDesignationConfirmation",
            "#[derive(Debug, Clone, Default, PartialEq, Eq)]\npub struct LiveDesignationConfirmation",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_confirmation_token(self.config, mutated)
        self.assertIn("Default", str(ctx.exception))

    def test_missing_constructor_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn from_operator(", "fn from_operatorX(")
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_confirmation_token(self.config, mutated)
        self.assertIn("from_operator", str(ctx.exception))


class RegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_registry_exposes_all_methods(self) -> None:
        evidence = check_registry(self.config, self.exec_src)
        for method in ("new", "designate", "demote", "designated", "authority_for"):
            self.assertIn(method, evidence)

    def test_missing_authority_for_method_is_caught(self) -> None:
        mutated = self.exec_src.replace("fn authority_for(", "fn authority_forX(")
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_registry(self.config, mutated)
        self.assertIn("authority_for", str(ctx.exception))

    def test_designate_without_confirmation_token_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "        confirmation: LiveDesignationConfirmation,",
            "        confirmation: bool,",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_registry(self.config, mutated)
        self.assertIn("LiveDesignationConfirmation", str(ctx.exception))

    def test_clone_derive_on_authority_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "#[derive(Debug, Default, PartialEq, Eq)]\npub struct LiveDesignation",
            "#[derive(Debug, Default, Clone, PartialEq, Eq)]\npub struct LiveDesignation",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_registry(self.config, mutated)
        self.assertIn("Clone", str(ctx.exception))


class EngineOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_engine_owns_authority_and_route_order_takes_none(self) -> None:
        evidence = check_engine_ownership(self.config, self.exec_src)
        self.assertIn("designation: LiveDesignation", evidence)
        self.assertIn("accepts no caller-supplied LiveDesignation", evidence)

    def test_missing_owned_field_is_caught(self) -> None:
        mutated = self.exec_src.replace("    designation: LiveDesignation,\n", "", 1)
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_engine_ownership(self.config, mutated)
        self.assertIn("designation", str(ctx.exception))

    def test_route_order_accepting_caller_authority_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "        &self,\n        submission: OrderSubmission,\n        broker: &B,",
            "        &self,\n        designation: &LiveDesignation,\n"
            "        submission: OrderSubmission,\n        broker: &B,",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_engine_ownership(self.config, mutated)
        self.assertIn("caller-supplied", str(ctx.exception))


class RoutingDecisionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_both_decisions_present(self) -> None:
        evidence = check_routing_decision(self.config, self.exec_src)
        self.assertIn("Authorized", evidence)
        self.assertIn("NotDesignated", evidence)

    def test_missing_not_designated_decision_is_caught(self) -> None:
        mutated = self.exec_src.replace("    NotDesignated,\n}", "}", 1)
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_routing_decision(self.config, mutated)
        self.assertIn("NotDesignated", str(ctx.exception))


class DesignationErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_all_variants_present(self) -> None:
        evidence = check_designation_error(self.config, self.exec_src)
        for variant in (
            "MissingConfirmation",
            "ConfirmationMismatch",
            "AlreadyDesignated",
            "NotDesignated",
        ):
            self.assertIn(variant, evidence)

    def test_missing_variant_is_caught(self) -> None:
        mutated = self.exec_src.replace("    MissingConfirmation,\n", "", 1)
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_designation_error(self.config, mutated)
        self.assertIn("MissingConfirmation", str(ctx.exception))


class RouteOrderGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_guard_resolves_authority_and_delegates(self) -> None:
        evidence = check_route_order_guard(self.config, self.exec_src)
        self.assertIn("self.designation.authority_for", evidence)
        self.assertIn("self.submit_live_order", evidence)
        self.assertIn("StrategyMode::Live", evidence)
        self.assertIn("OrderErrorCategory::NonLiveStrategySubmission", evidence)

    def test_forbidden_port_in_not_designated_leaf_is_caught(self) -> None:
        # Smuggle a broker call into the NotDesignated leaf via its unique
        # message tail — the rejection must consult no side-effecting port.
        mutated = self.exec_src.replace(
            "(SRS-EXE-001, SyRS SYS-2a/SYS-2d)\",",
            "(SRS-EXE-001, SyRS SYS-2a/SYS-2d) broker.submit_order(\",",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_route_order_guard(self.config, mutated)
        self.assertIn("broker.submit_order", str(ctx.exception))

    def test_missing_authority_call_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "self.designation.authority_for(",
            "self.never_resolves_authority(",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_route_order_guard(self.config, mutated)
        self.assertIn("self.designation.authority_for", str(ctx.exception))

    def test_missing_delegate_call_is_caught(self) -> None:
        mutated = self.exec_src.replace(
            "self.submit_live_order(",
            "self.never_delegates(",
            1,
        )
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_route_order_guard(self.config, mutated)
        self.assertIn("self.submit_live_order", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_seven_evidence_items(self) -> None:
        evidence = run_checks()
        # 6 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 7)

    def test_assert_live_designation_static_emits_six_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_live_designation_static(config, ROOT)
        self.assertEqual(len(evidence), 6)


if __name__ == "__main__":
    unittest.main()
