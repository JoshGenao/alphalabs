"""Contract tests for SRS-ORCH-003 (SyRS SYS-57 / SYS-58; StRS SN-1.10
/ C-6 / BG-1 / BG-6).

Mirrors ``tests/test_orchestrator_resource_profile_contract.py``: shells
out to ``tools/orchestrator_workload_priority_check.py``, then exercises
each per-check function in-process, including negative spot-checks that
verify the contract actually catches regressions (drifted constants,
missing enum variants, missing required fields, missing port methods,
missing admit-gate ordering, missing live-immunity assertion, refusal
arm calling runtime mutators, catalogue / constant drift).
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from orchestrator_workload_priority_check import (  # noqa: E402
    WorkloadPriorityCheckError,
    assert_orchestrator_workload_priority_static,
    check_admit_workload_guard,
    check_config_catalogue_binding,
    check_host_memory_safety_margin_error_enum,
    check_host_memory_safety_margin_struct,
    check_orchestrator_helper_methods,
    check_ports,
    check_registered_workload_struct,
    check_spec_constants,
    check_validation_constants,
    check_workload_admission_event_enum,
    check_workload_id_newtype,
    check_workload_kind_enum,
    check_workload_priority_enum,
    load_config,
    orchestrator_source,
    types_source,
)


class WorkloadPriorityScriptTest(unittest.TestCase):
    def test_srs_orch_003_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/orchestrator_workload_priority_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ORCH-003 PASS", result.stdout)
        for needle in (
            "HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT=2048",
            "safety margin in [256, 1048576] MB",
            "HostMemorySafetyMargin with the 1 required field(s)",
            "rejects 6 forbidden vendor/unit fields",
            "HostMemorySafetyMarginError with 2 validation-failure variants",
            "BelowFloor, AboveCeiling",
            "WorkloadPriority with the SYS-57 hierarchy in exact order",
            "LiveStrategy, MarketDataSubscriptionManager, PaperStrategy, NightlyDataIngestion, FactorPipeline, Backtest, Research",
            "rank() mapping live=1 → research=7",
            "WorkloadKind with variants (Continuous, Batch)",
            "default_kind routes each SYS-57 priority",
            "WorkloadId as a newtype over String",
            "RegisteredWorkload with the 4 required fields",
            "(id, priority, kind, profile)",
            "rejects 8 forbidden vendor/temporal fields",
            "WorkloadAdmissionEvent with 5 variants",
            "Refused, Terminated, TerminationFailed, HostProbeFailed, RegistryListingFailed",
            "Refused carries 4 fields",
            "Terminated carries 6 fields",
            "TerminationFailed carries 6 fields",
            "HostProbeFailed carries 4 fields",
            "RegistryListingFailed carries 4 fields",
            "HostMemoryProbe, WorkloadRegistry, WorkloadEventSink",
            "no vendor-SDK / OS-API imports inside the crate",
            "Result-returning IO signatures",
            "typed failure surfaces (HostMemoryProbeError, WorkloadRegistryError, WorkloadTerminationError)",
            "Orchestrator helpers (host_memory_safety_margin_default, safety_margin_from_lookup, safety_margin_via_env_lookup, safety_margin_from_env, admit_workload)",
            "host.available_mb → registry.active → registry.terminate",
            "audit emission (sink.record)",
            "filters to `WorkloadKind::Batch`",
            "debug_asserts the live-immunity invariant",
            "validates the safety margin via `safety_margin.validate(",
            "pre-checks total recoverable memory before any eviction",
            "StructuredOrchestratorError::host_memory_safety_margin_breach",
            "HOST_MEMORY_SAFETY_MARGIN_BREACH",
            "invokes none of the 5 forbidden runtime mutators",
            "SRS-ARCH-005 catalogue default + min + max agree",
            "ATP_HOST_MEMORY_SAFETY_MARGIN_MB",
            "orch_3_workload_priority_contract",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class SpecConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_spec_constants_match_syrs_sys_57(self) -> None:
        evidence = check_spec_constants(self.config, self.types_src)
        self.assertIn("=2048", evidence)

    def test_drifted_default_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT: u32 = 2_048;",
            "pub const HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT: u32 = 1_024;",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT", str(ctx.exception))

    def test_missing_default_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT: u32 = 2_048;",
            "// removed for test",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("HOST_MEMORY_SAFETY_MARGIN_MB_DEFAULT", str(ctx.exception))


class ValidationConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_validation_constants_match_catalogue(self) -> None:
        evidence = check_validation_constants(self.config, self.types_src)
        self.assertIn("[256, 1048576]", evidence)

    def test_drifted_floor_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR: u32 = 256;",
            "pub const HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR: u32 = 255;",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_validation_constants(self.config, mutated)
        self.assertIn("HOST_MEMORY_SAFETY_MARGIN_MB_FLOOR", str(ctx.exception))


class HostMemorySafetyMarginStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_required_field(self) -> None:
        evidence = check_host_memory_safety_margin_struct(self.config, self.types_src)
        self.assertIn("mb", evidence)

    def test_missing_mb_field_is_caught(self) -> None:
        mutated = re.sub(
            r"pub struct HostMemorySafetyMargin \{\s*pub mb: u32,\s*\}",
            "pub struct HostMemorySafetyMargin {}",
            self.types_src,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_host_memory_safety_margin_struct(self.config, mutated)
        self.assertIn("mb", str(ctx.exception))

    def test_leaked_bytes_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct HostMemorySafetyMargin {\n    pub mb: u32,\n}",
            "pub struct HostMemorySafetyMargin {\n    pub mb: u32,\n    pub bytes: u64,\n}",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_host_memory_safety_margin_struct(self.config, mutated)
        self.assertIn("bytes", str(ctx.exception))


class HostMemorySafetyMarginErrorEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_enum_lists_both_variants(self) -> None:
        evidence = check_host_memory_safety_margin_error_enum(
            self.config, self.types_src
        )
        self.assertIn("BelowFloor", evidence)
        self.assertIn("AboveCeiling", evidence)

    def test_missing_below_floor_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "BelowFloor { value_mb: u32, floor_mb: u32 },",
            "// removed",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_host_memory_safety_margin_error_enum(self.config, mutated)
        self.assertIn("BelowFloor", str(ctx.exception))


class WorkloadPriorityEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_hierarchy_in_exact_order(self) -> None:
        evidence = check_workload_priority_enum(self.config, self.types_src)
        self.assertIn("LiveStrategy", evidence)
        self.assertIn("research=7", evidence)

    def test_missing_research_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Research,\n", "    // removed\n", 1)
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_workload_priority_enum(self.config, mutated)
        self.assertIn("Research", str(ctx.exception))

    def test_drifted_live_rank_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "Self::LiveStrategy => 1,",
            "Self::LiveStrategy => 2,",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_workload_priority_enum(self.config, mutated)
        self.assertIn("LiveStrategy", str(ctx.exception))

    def test_forbidden_priority_score_token_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "Self::Research => 7,",
            "Self::Research => 7, // priority_score 999",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_workload_priority_enum(self.config, mutated)
        self.assertIn("priority_score", str(ctx.exception))


class WorkloadKindEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_kind_mapping_matches_sys_58(self) -> None:
        evidence = check_workload_kind_enum(self.config, self.types_src)
        self.assertIn("Continuous", evidence)
        self.assertIn("Batch", evidence)

    def test_missing_batch_variant_is_caught(self) -> None:
        mutated = re.sub(
            r"pub enum WorkloadKind \{\s*Continuous,\s*Batch,\s*\}",
            "pub enum WorkloadKind {\n    Continuous,\n}",
            self.types_src,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_workload_kind_enum(self.config, mutated)
        self.assertIn("Batch", str(ctx.exception))


class WorkloadIdNewtypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_newtype_shape_passes(self) -> None:
        evidence = check_workload_id_newtype(self.config, self.types_src)
        self.assertIn("newtype over String", evidence)


class RegisteredWorkloadStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_all_four_fields(self) -> None:
        evidence = check_registered_workload_struct(self.config, self.types_src)
        for field in ("id", "priority", "kind", "profile"):
            self.assertIn(field, evidence)

    def test_leaked_docker_image_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    pub profile: ResourceProfile,\n}",
            "    pub profile: ResourceProfile,\n    pub docker_image: String,\n}",
            1,
        )
        # The replace touches multiple structs; restrict to RegisteredWorkload
        # by reverting unrelated changes if needed.
        if mutated.count("pub docker_image: String") > 1:
            self.skipTest("mutation hit more than RegisteredWorkload — skipping")
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_registered_workload_struct(self.config, mutated)
        self.assertIn("docker_image", str(ctx.exception))


class WorkloadAdmissionEventEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_event_lists_both_variants(self) -> None:
        evidence = check_workload_admission_event_enum(self.config, self.types_src)
        self.assertIn("Refused", evidence)
        self.assertIn("Terminated", evidence)
        self.assertIn("TerminationFailed", evidence)


class PortsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_ports_have_required_methods(self) -> None:
        evidence = check_ports(self.config, self.orch_src)
        for trait in ("HostMemoryProbe", "WorkloadRegistry", "WorkloadEventSink"):
            self.assertIn(trait, evidence)

    def test_sysinfo_import_in_crate_is_caught(self) -> None:
        mutated = self.orch_src + "\nuse sysinfo::System;\n"
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("use sysinfo", str(ctx.exception))

    def test_missing_available_mb_method_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "fn available_mb(&self) -> Result<u64, HostMemoryProbeError>;",
            "// removed",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("available_mb", str(ctx.exception))

    def test_terminate_returning_unit_is_caught(self) -> None:
        # codex critic adapter:error-surface: terminate MUST return
        # Result so the gate can distinguish a successful eviction
        # from a failed registry / Docker termination.
        mutated = self.orch_src.replace(
            "fn terminate(&self, id: &WorkloadId) -> Result<(), WorkloadTerminationError>",
            "fn terminate(&self, id: &WorkloadId)",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("required signature", str(ctx.exception))

    def test_available_mb_returning_bare_u64_is_caught(self) -> None:
        # codex critic adapter:error-surface: available_mb MUST
        # return Result so the gate can fail closed on probe error.
        mutated = self.orch_src.replace(
            "fn available_mb(&self) -> Result<u64, HostMemoryProbeError>",
            "fn available_mb(&self) -> u64",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("required signature", str(ctx.exception))

    def test_active_returning_bare_vec_is_caught(self) -> None:
        # codex critic adapter:error-surface: active MUST return
        # Result so the gate can distinguish an empty active set from
        # a registry listing failure.
        mutated = self.orch_src.replace(
            "fn active(&self) -> Result<Vec<RegisteredWorkload>, WorkloadRegistryError>",
            "fn active(&self) -> Vec<RegisteredWorkload>",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("required signature", str(ctx.exception))

    def test_missing_termination_error_struct_is_caught(self) -> None:
        mutated = re.sub(
            r"pub struct WorkloadTerminationError \{[^}]*\}",
            "// removed",
            self.orch_src,
            count=1,
            flags=re.DOTALL,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_ports(self.config, mutated)
        self.assertIn("WorkloadTerminationError", str(ctx.exception))


class OrchestratorHelperMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_helpers_present(self) -> None:
        evidence = check_orchestrator_helper_methods(self.config, self.orch_src)
        for method in (
            "host_memory_safety_margin_default",
            "safety_margin_from_lookup",
            "safety_margin_via_env_lookup",
            "safety_margin_from_env",
            "admit_workload",
        ):
            self.assertIn(method, evidence)

    def test_missing_admit_workload_is_caught(self) -> None:
        mutated = re.sub(
            r"pub fn admit_workload\b",
            "pub fn admit_workload_disabled",
            self.orch_src,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_orchestrator_helper_methods(self.config, mutated)
        self.assertIn("admit_workload", str(ctx.exception))


class AdmitWorkloadGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_guard_passes(self) -> None:
        evidence = check_admit_workload_guard(self.config, self.orch_src)
        self.assertIn("host_memory_safety_margin_breach", evidence)
        self.assertIn("WorkloadKind::Batch", evidence)

    def test_missing_kind_filter_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "filter(|workload| workload.kind == WorkloadKind::Batch)",
            "filter(|_workload| true)",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("WorkloadKind::Batch", str(ctx.exception))

    def test_missing_live_immunity_assertion_is_caught(self) -> None:
        mutated = re.sub(
            r"debug_assert!\(\s*candidate\.priority != WorkloadPriority::LiveStrategy[^;]+;",
            "// debug_assert removed for test",
            self.orch_src,
            count=1,
            flags=re.DOTALL,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("debug_assert", str(ctx.exception).lower())

    def test_forbidden_runtime_call_in_admit_is_caught(self) -> None:
        # Inject a runtime.destroy call into admit_workload to simulate
        # a drift where the gate starts mutating runtime state.
        mutated = self.orch_src.replace(
            "Err(StructuredOrchestratorError::host_memory_safety_margin_breach(",
            "runtime.destroy(&request.strategy_id);\n        Err(StructuredOrchestratorError::host_memory_safety_margin_breach(",
            1,
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("runtime.destroy", str(ctx.exception))

    def test_missing_rejection_factory_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "StructuredOrchestratorError::host_memory_safety_margin_breach(",
            "StructuredOrchestratorError::not_a_real_factory(",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("host_memory_safety_margin_breach", str(ctx.exception))

    def test_missing_safety_margin_validation_is_caught(self) -> None:
        # codex critic safety:margin-validation-bypass: a refactor that
        # drops the safety_margin.validate() call at the gate entry
        # would let an invalid programmatic margin disable the gate.
        # The contract must catch that.
        mutated = self.orch_src.replace(
            "safety_margin.validate(",
            "safety_margin.skip_validate(",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("safety_margin.validate", str(ctx.exception))

    def test_missing_pre_eviction_feasibility_is_caught(self) -> None:
        # codex critic orch:partial-eviction-refusal: a refactor that
        # removes the pre-eviction sum check would let the gate kill
        # batch workloads and still return a refusal. The contract
        # must catch that.
        mutated = self.orch_src.replace(
            "saturating_add(recoverable_mb)",
            "saturating_add(0)",
        )
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_admit_workload_guard(self.config, mutated)
        self.assertIn("saturating_add(recoverable_mb)", str(ctx.exception))


class ConfigCatalogueBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def test_catalogue_binding_passes(self) -> None:
        evidence = check_config_catalogue_binding(self.config, "")
        self.assertIn("ATP_HOST_MEMORY_SAFETY_MARGIN_MB", evidence)

    def test_drifted_catalogue_default_is_caught(self) -> None:
        config = load_config()
        for entry in config["configuration"]["keys"]:
            if entry["name"] == "ATP_HOST_MEMORY_SAFETY_MARGIN_MB":
                entry["default"] = "4096"
                break
        with self.assertRaises(WorkloadPriorityCheckError) as ctx:
            check_config_catalogue_binding(config, "")
        self.assertIn("ATP_HOST_MEMORY_SAFETY_MARGIN_MB", str(ctx.exception))


class StaticAggregateTest(unittest.TestCase):
    def test_assert_returns_evidence_for_each_check(self) -> None:
        config = load_config()
        evidence = assert_orchestrator_workload_priority_static(config, ROOT)
        # 13 static checks (spec / validation / margin struct / margin
        # error / priority enum / kind enum / workload id / registered
        # workload / event enum / ports / helper methods / admit guard
        # / catalogue binding).
        self.assertEqual(len(evidence), 13)


if __name__ == "__main__":
    unittest.main()
