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

import shutil
import subprocess
import sys
import tempfile
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
    check_live_host,
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
            "holds the swap guard across a fresh designation read",
            "srs_exe_001_live_host",
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
            '(SRS-EXE-001, SyRS SYS-2a/SYS-2d)",',
            '(SRS-EXE-001, SyRS SYS-2a/SYS-2d) broker.submit_order(",',
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


class LiveHostTest(unittest.TestCase):
    """The live execution host: each guard in check_live_host catches its regression."""

    def setUp(self) -> None:
        self.config = load_config()
        self.spec = self.config["live_designation_contract"]["live_host"]
        self.root = Path(tempfile.mkdtemp(prefix="exe001-contract-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        for key in ("module", "server_module", "protocol_module", "binary", "designation_store"):
            target = self.root / self.spec[key]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / self.spec[key], target)

    def _mutate(self, key: str, old: str, new: str) -> None:
        path = self.root / self.spec[key]
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"mutation anchor missing from {key}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def _caught(self, needle: str) -> None:
        with self.assertRaises(LiveDesignationCheckError) as ctx:
            check_live_host(self.config, self.root)
        self.assertIn(needle, str(ctx.exception))

    def test_the_unmutated_copy_passes(self) -> None:
        self.assertIn("holds the swap guard", check_live_host(self.config, self.root))

    def test_reading_the_designation_before_the_guard_is_caught(self) -> None:
        # A read before the guard can be stale by the time the order reaches the
        # broker: a Hot-Swap may have moved the live slot in between.
        self._mutate(
            "module",
            "        // A poisoned mutex means",
            "        let _early = load_designation(&self.designation_path);\n"
            "        // A poisoned mutex means",
        )
        self._caught("out of order")

    def test_calling_the_caller_trusting_entry_point_is_caught(self) -> None:
        self._mutate(
            "module",
            "        let result = engine.route_order_durably(",
            "        let _ = engine.submit_live_order;\n        let result = engine.route_order_durably(",
        )
        self._caught("calls `submit_live_order`")

    def test_a_private_copy_of_the_snapshot_format_is_caught(self) -> None:
        stray = self.root / "crates/atp-orchestrator/src/bin/stray.rs"
        stray.write_text("fn load_designation(path: &Path) {}\n", encoding="utf-8")
        self._caught("outside crates/atp-orchestrator/src/live_designation_store.rs")

    def test_identity_from_anything_but_the_socket_is_caught(self) -> None:
        self._mutate(
            "server_module",
            "StrategyId::new(id.as_str())",
            "StrategyId::new(strategies[0].as_str())",
        )
        self._caught("derives each connection's strategy")

    def test_an_open_socket_directory_check_removed_is_caught(self) -> None:
        self._mutate("server_module", "mode & 0o077 != 0", "mode & 0o000 != 0")
        self._caught("mode & 0o077 != 0")

    def test_a_shared_strategy_directory_is_caught(self) -> None:
        self._mutate(
            "server_module",
            "fs::Permissions::from_mode(0o700)",
            "fs::Permissions::from_mode(0o755)",
        )
        self._caught("from_mode(0o700)")

    def test_a_strategy_id_field_in_the_frame_is_caught(self) -> None:
        self._mutate(
            "protocol_module", '"correlation_id",\n', '"correlation_id",\n    "strategy_id",\n'
        )
        self._caught("may carry `strategy_id`")

    def test_a_live_tier_that_starts_with_a_fixture_freshness_probe_is_caught(self) -> None:
        self._mutate(
            "binary",
            '            if flags.fixture_wire_ledger.is_some() {\n                return Err("`--fixture-wire-ledger` is fixture-tier only".to_string());',
            "            let _probe = FreshMarketDataFixture;\n"
            '            if flags.fixture_wire_ledger.is_some() {\n                return Err("`--fixture-wire-ledger` is fixture-tier only".to_string());',
        )
        self._caught("uses `FreshMarketDataFixture`")


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_eight_evidence_items(self) -> None:
        evidence = run_checks()
        # 6 static + the live host + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 8)

    def test_assert_live_designation_static_emits_seven_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_live_designation_static(config, ROOT)
        self.assertEqual(len(evidence), 7)


if __name__ == "__main__":
    unittest.main()
