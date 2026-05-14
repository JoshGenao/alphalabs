"""Contract tests for SRS-ORCH-001 (SyRS SYS-10 / SYS-13 / AC-12 /
NFR-P9 / NFR-R5 / NFR-S5; StRS SN-1.10 / SN-2.03 / SN-2.05).

Mirrors ``tests/test_pacing_budget_contract.py``: shells out to
``tools/orchestrator_lifecycle_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the
contract actually catches regressions (forbidden vendor / container-
runtime fields, missing variants, dropped ``sink.record`` call,
acceptance leak into the DeadlineExceeded leaf, dropped restart call,
silent dashboard fan-out on the Unresponsive leaf, drifted wire
string, dropped NFR-P9 constant).
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

from orchestrator_lifecycle_check import (  # noqa: E402
    OrchestratorLifecycleCheckError,
    assert_orchestrator_lifecycle_static,
    check_container_health_event_struct,
    check_container_health_state_enum,
    check_container_runtime_port,
    check_health_event_sink_port,
    check_health_observe_guard,
    check_launch_guard,
    check_launch_readiness_enum,
    check_lifecycle_action_enum,
    check_startup_deadline_constant,
    check_strategy_launch_outcome_struct,
    check_strategy_launch_request_struct,
    load_config,
    orchestrator_source,
    run_checks,
    types_source,
)


class OrchestratorLifecycleCheckScriptTest(unittest.TestCase):
    def test_srs_orch_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/orchestrator_lifecycle_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ORCH-001 PASS", result.stdout)
        for needle in (
            "ContainerLifecycleAction with 5 actions",
            "Create, Start, Stop, Restart, Destroy",
            "ContainerHealthState with 2 states",
            "Healthy, Unresponsive",
            "LaunchReadiness with 2 variants",
            "ReadyWithinDeadline, DeadlineExceeded",
            "StrategyLaunchRequest with the 4 required fields",
            "strategy_id, mode, deployment_hash, deadline_millis",
            "StrategyLaunchOutcome with the 4 required fields",
            "strategy_id, ready_within_deadline, elapsed_millis, deadline_millis",
            "ContainerHealthEvent with the 4 required fields",
            "state, strategy_id, action_taken, observed_at_seconds",
            "rejects 6 forbidden vendor/container-runtime fields",
            "STRATEGY_STARTUP_DEADLINE_MS = 30000",
            "StrategyContainerRuntime with 6 methods",
            "create, start, stop, restart, destroy, health",
            "HealthCheckEventSink with 1 method",
            "LaunchReadiness::ReadyWithinDeadline",
            "LaunchReadiness::DeadlineExceeded",
            "OrderErrorCategory::StrategyStartupDeadlineExceeded",
            "ContainerHealthState::Healthy",
            "ContainerHealthState::Unresponsive",
            "runtime.start",
            "runtime.destroy",
            "runtime.restart",
            "sink.record",
            "ContainerLifecycleAction::Destroy",
            "mutates nothing on the orchestrator registry",
            "auto-restart + dashboard fan-out",
            "orch_1_lifecycle_contract",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class LifecycleActionEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_enum_covers_all_five_sys_10_actions(self) -> None:
        evidence = check_lifecycle_action_enum(self.config, self.types_src)
        for variant in ("Create", "Start", "Stop", "Restart", "Destroy"):
            self.assertIn(variant, evidence)

    def test_missing_restart_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Restart,", "    RestartX,", 1)
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_lifecycle_action_enum(self.config, mutated)
        self.assertIn("Restart", str(ctx.exception))

    def test_missing_destroy_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Destroy,", "    DestroyX,", 1)
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_lifecycle_action_enum(self.config, mutated)
        self.assertIn("Destroy", str(ctx.exception))


class ContainerHealthStateEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_health_states_present(self) -> None:
        evidence = check_container_health_state_enum(self.config, self.types_src)
        for variant in ("Healthy", "Unresponsive"):
            self.assertIn(variant, evidence)

    def test_missing_unresponsive_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Unresponsive,", "    UnresponsiveX,", 1)
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_container_health_state_enum(self.config, mutated)
        self.assertIn("Unresponsive", str(ctx.exception))


class LaunchReadinessEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_readiness_variants_present(self) -> None:
        evidence = check_launch_readiness_enum(self.config, self.types_src)
        for variant in ("ReadyWithinDeadline", "DeadlineExceeded"):
            self.assertIn(variant, evidence)

    def test_missing_deadline_exceeded_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    DeadlineExceeded { elapsed_millis: u64, deadline_millis: u64 },",
            "    DeadlineExceededX { elapsed_millis: u64, deadline_millis: u64 },",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_readiness_enum(self.config, mutated)
        self.assertIn("DeadlineExceeded", str(ctx.exception))


class StrategyLaunchRequestStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_strategy_launch_request_struct(self.config, self.types_src)
        for field in ("strategy_id", "mode", "deployment_hash", "deadline_millis"):
            self.assertIn(field, evidence)

    def test_missing_deadline_millis_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub deadline_millis: u64,\n}\n\n#[derive(Debug, Clone, PartialEq, Eq)]\npub struct StrategyLaunchOutcome",
            "}\n\n#[derive(Debug, Clone, PartialEq, Eq)]\npub struct StrategyLaunchOutcome",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_strategy_launch_request_struct(self.config, mutated)
        self.assertIn("deadline_millis", str(ctx.exception))

    def test_struct_rejects_leaked_docker_image_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StrategyLaunchRequest {\n    pub strategy_id: StrategyId,",
            "pub struct StrategyLaunchRequest {\n    pub docker_image: String,\n    pub strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_strategy_launch_request_struct(self.config, mutated)
        self.assertIn("docker_image", str(ctx.exception))

    def test_struct_rejects_leaked_container_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StrategyLaunchRequest {\n    pub strategy_id: StrategyId,",
            "pub struct StrategyLaunchRequest {\n    pub container_id: String,\n    pub strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_strategy_launch_request_struct(self.config, mutated)
        self.assertIn("container_id", str(ctx.exception))

    def test_struct_rejects_leaked_host_path_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StrategyLaunchRequest {\n    pub strategy_id: StrategyId,",
            "pub struct StrategyLaunchRequest {\n    pub host_path: String,\n    pub strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_strategy_launch_request_struct(self.config, mutated)
        self.assertIn("host_path", str(ctx.exception))


class StrategyLaunchOutcomeStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_strategy_launch_outcome_struct(self.config, self.types_src)
        for field in (
            "strategy_id",
            "ready_within_deadline",
            "elapsed_millis",
            "deadline_millis",
        ):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StrategyLaunchOutcome {\n    pub strategy_id: StrategyId,",
            "pub struct StrategyLaunchOutcome {\n    pub broker: String,\n    pub strategy_id: StrategyId,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_strategy_launch_outcome_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))


class ContainerHealthEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_required_fields(self) -> None:
        evidence = check_container_health_event_struct(self.config, self.types_src)
        for field in (
            "state",
            "strategy_id",
            "action_taken",
            "observed_at_seconds",
        ):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_vendor_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ContainerHealthEvent {\n    pub state: ContainerHealthState,",
            "pub struct ContainerHealthEvent {\n    pub vendor: String,\n    pub state: ContainerHealthState,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_container_health_event_struct(self.config, mutated)
        self.assertIn("vendor", str(ctx.exception))

    def test_missing_action_taken_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub action_taken: ContainerLifecycleAction,\n    pub observed_at_seconds: u64,",
            "pub observed_at_seconds: u64,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_container_health_event_struct(self.config, mutated)
        self.assertIn("action_taken", str(ctx.exception))


class StartupDeadlineConstantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_constant_pinned_at_thirty_seconds(self) -> None:
        evidence = check_startup_deadline_constant(self.config, self.types_src)
        self.assertIn("STRATEGY_STARTUP_DEADLINE_MS", evidence)
        self.assertIn("30000", evidence)

    def test_drifted_constant_value_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const STRATEGY_STARTUP_DEADLINE_MS: u64 = 30_000;",
            "pub const STRATEGY_STARTUP_DEADLINE_MS: u64 = 45_000;",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_startup_deadline_constant(self.config, mutated)
        self.assertIn("NFR-P9", str(ctx.exception))

    def test_missing_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const STRATEGY_STARTUP_DEADLINE_MS: u64 = 30_000;",
            "// removed",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_startup_deadline_constant(self.config, mutated)
        self.assertIn("STRATEGY_STARTUP_DEADLINE_MS", str(ctx.exception))


class ContainerRuntimePortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_port_exposes_six_lifecycle_methods(self) -> None:
        evidence = check_container_runtime_port(self.config, self.orch_src)
        for method in ("create", "start", "stop", "restart", "destroy", "health"):
            self.assertIn(method, evidence)

    def test_missing_restart_method_is_caught(self) -> None:
        # `restart` is declared on the trait AND on the test stub. We
        # only need to remove the trait declaration to demonstrate the
        # check catches the drop — rename the first declaration only.
        marker = "fn restart(&self, strategy_id: &StrategyId);"
        mutated = self.orch_src.replace(
            marker, "fn dropped_restart(&self, strategy_id: &StrategyId);", 1
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_container_runtime_port(self.config, mutated)
        self.assertIn("restart", str(ctx.exception))

    def test_missing_health_method_is_caught(self) -> None:
        marker = "fn health(&self, strategy_id: &StrategyId) -> ContainerHealthState;"
        mutated = self.orch_src.replace(
            marker,
            "fn dropped_health(&self, strategy_id: &StrategyId) -> ContainerHealthState;",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_container_runtime_port(self.config, mutated)
        self.assertIn("health", str(ctx.exception))


class HealthEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_health_event_sink_port(self.config, self.orch_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "fn record(&self, event: ContainerHealthEvent);",
            "fn dropped_record(&self, event: ContainerHealthEvent);",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_health_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class LaunchGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_acceptance_is_gated_on_ready_within_deadline_leaf(self) -> None:
        evidence = check_launch_guard(self.config, self.orch_src)
        self.assertIn("LaunchReadiness::ReadyWithinDeadline", evidence)
        self.assertIn("LaunchReadiness::DeadlineExceeded", evidence)
        self.assertIn("OrderErrorCategory::StrategyStartupDeadlineExceeded", evidence)
        self.assertIn("runtime.start", evidence)
        self.assertIn("runtime.destroy", evidence)
        self.assertIn("sink.record", evidence)
        self.assertIn("ContainerLifecycleAction::Destroy", evidence)
        self.assertIn("mutates nothing on the orchestrator registry", evidence)

    def test_missing_sink_record_call_on_deadline_exceeded_is_caught(self) -> None:
        # Drop the sink.record(ContainerHealthEvent { ... }); block in
        # the DeadlineExceeded arm. The rest of the file still parses
        # because the closing brace count is unaffected.
        marker_open = "sink.record(ContainerHealthEvent {"
        start = self.orch_src.find(marker_open)
        self.assertGreaterEqual(start, 0)
        # Find the matching `);` semicolon by scanning brace + paren depth.
        depth = 0
        index = start
        while index < len(self.orch_src):
            char = self.orch_src[index]
            if char in ("{", "("):
                depth += 1
            elif char in ("}", ")"):
                depth -= 1
                if depth == 0:
                    break
            index += 1
        # Step past the closing `;`.
        end = self.orch_src.find(";", index) + 1
        mutated = self.orch_src[:start] + self.orch_src[end:]
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("sink.record", str(ctx.exception))

    def test_acceptance_in_deadline_exceeded_arm_is_caught(self) -> None:
        # Insert a `StrategyLaunchOutcome {` construction into the
        # DeadlineExceeded arm to simulate a regression where the
        # over-deadline path leaks an acceptance.
        # `Err(...)` only appears in the gate body, never in the
        # surrounding docstring — that pins the mutation to the
        # actual match arm.
        marker = "Err(StructuredOrchestratorError::startup_deadline_exceeded("
        self.assertIn(marker, self.orch_src)
        mutated = self.orch_src.replace(
            marker,
            "let _illegal = StrategyLaunchOutcome { ready_within_deadline: false };\n                "
            + marker,
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("zero acceptance", str(ctx.exception))

    def test_missing_destroy_call_on_deadline_exceeded_is_caught(self) -> None:
        # Drop the `runtime.destroy(&request.strategy_id);` line in the
        # DeadlineExceeded arm. The check must catch this — without
        # destroy, the over-deadline container is orphaned and the
        # event's action_taken=Destroy payload becomes a lie.
        mutated = self.orch_src.replace(
            "runtime.destroy(&request.strategy_id);",
            "// removed destroy call",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("runtime.destroy", str(ctx.exception))

    def test_missing_action_taken_destroy_token_is_caught(self) -> None:
        # If a future refactor swaps the event's action_taken payload
        # away from Destroy while keeping the destroy call, the audit
        # log would record the wrong action. Drop the
        # `ContainerLifecycleAction::Destroy` literal inside the event
        # block to simulate that drift.
        mutated = self.orch_src.replace(
            "action_taken: ContainerLifecycleAction::Destroy,",
            "action_taken: ContainerLifecycleAction::Restart,",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("action_taken", str(ctx.exception))

    def test_destroy_after_event_record_is_caught(self) -> None:
        # The order matters: destroy must precede sink.record so a sink
        # failure cannot mask the resource release. Swapping the order
        # should be caught.
        original = (
            "                runtime.destroy(&request.strategy_id);\n"
            "                sink.record(ContainerHealthEvent {\n"
            "                    state: ContainerHealthState::Unresponsive,\n"
            "                    strategy_id: request.strategy_id.clone(),\n"
            "                    action_taken: ContainerLifecycleAction::Destroy,\n"
            "                    observed_at_seconds,\n"
            "                });"
        )
        swapped = (
            "                sink.record(ContainerHealthEvent {\n"
            "                    state: ContainerHealthState::Unresponsive,\n"
            "                    strategy_id: request.strategy_id.clone(),\n"
            "                    action_taken: ContainerLifecycleAction::Destroy,\n"
            "                    observed_at_seconds,\n"
            "                });\n"
            "                runtime.destroy(&request.strategy_id);"
        )
        self.assertIn(original, self.orch_src)
        mutated = self.orch_src.replace(original, swapped, 1)
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("destroy to precede", str(ctx.exception))

    def test_forbidden_registry_mutation_on_refusal_is_caught(self) -> None:
        # Insert a registry mutator into the DeadlineExceeded arm to
        # simulate a regression where the over-deadline path silently
        # persists a half-launched container. Use the `Err(...)`
        # marker so the mutation lands in the gate body, not in the
        # surrounding docstring.
        marker = "Err(StructuredOrchestratorError::startup_deadline_exceeded("
        self.assertIn(marker, self.orch_src)
        mutated = self.orch_src.replace(
            marker,
            "self.spawn_container(&request);\n                " + marker,
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_launch_guard(self.config, mutated)
        self.assertIn("self.spawn_container", str(ctx.exception))


class HealthObserveGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_unresponsive_arm_calls_both_restart_and_record(self) -> None:
        evidence = check_health_observe_guard(self.config, self.orch_src)
        self.assertIn("runtime.restart", evidence)
        self.assertIn("sink.record", evidence)
        self.assertIn("ContainerHealthState::Healthy", evidence)
        self.assertIn("ContainerHealthState::Unresponsive", evidence)

    def test_missing_restart_call_on_unresponsive_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "runtime.restart(&strategy_id);",
            "// removed restart call",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_health_observe_guard(self.config, mutated)
        self.assertIn("runtime.restart", str(ctx.exception))

    def test_side_effect_on_healthy_arm_is_caught(self) -> None:
        # Insert a runtime.restart call into the Healthy leaf to
        # simulate a regression where a healthy probe still triggers
        # a side effect (the dashboard would distort).
        mutated = self.orch_src.replace(
            "ContainerHealthState::Healthy => state,",
            "ContainerHealthState::Healthy => { runtime.restart(&strategy_id); state },",
            1,
        )
        with self.assertRaises(OrchestratorLifecycleCheckError) as ctx:
            check_health_observe_guard(self.config, mutated)
        self.assertIn("read-only", str(ctx.exception))


class StaticAssertWiringTest(unittest.TestCase):
    def test_static_helper_returns_evidence_list(self) -> None:
        config = load_config()
        evidence = assert_orchestrator_lifecycle_static(config, ROOT)
        # 11 static checks, all return one bullet each.
        self.assertEqual(
            len(evidence),
            11,
            f"expected 11 static evidence bullets, got {len(evidence)}: {evidence}",
        )
        # The cargo smoke bullet is NOT in the static set — that's the
        # whole point of `assert_*_static`: usable from architecture_check.py
        # without invoking cargo.
        joined = "\n".join(evidence)
        self.assertNotIn("cargo test", joined)


class RunChecksTest(unittest.TestCase):
    def test_run_checks_produces_twelve_bullets(self) -> None:
        # 11 static + 1 cargo smoke = 12 bullets.
        evidence = run_checks()
        self.assertEqual(
            len(evidence),
            12,
            f"expected 12 evidence bullets, got {len(evidence)}: {evidence}",
        )


if __name__ == "__main__":
    unittest.main()
