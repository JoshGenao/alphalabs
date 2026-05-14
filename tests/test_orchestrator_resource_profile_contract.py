"""Contract tests for SRS-ORCH-002 (SyRS SYS-11 / SYS-57 / NFR-SC1;
StRS SN-1.10 / BG-6).

Mirrors ``tests/test_orchestrator_lifecycle_contract.py``: shells out
to ``tools/orchestrator_resource_profile_check.py``, then exercises
each per-check function in-process, including negative spot-checks
that verify the contract actually catches regressions (drifted
constants, missing required fields, leaked float-CPU / vendor /
container-runtime fields, missing validation method, validate-after-
create swap, missing rejection factory, runtime mutation in the
validation rejection arm, outcome.profile re-default, catalogue /
constant drift).
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

from orchestrator_resource_profile_check import (  # noqa: E402
    ResourceProfileCheckError,
    assert_orchestrator_resource_profile_static,
    check_config_catalogue_binding,
    check_launch_validate_guard,
    check_orchestrator_helper_methods,
    check_outcome_profile_equality_guard,
    check_resource_profile_error_enum,
    check_resource_profile_methods,
    check_resource_profile_struct,
    check_spec_constants,
    check_validation_constants,
    load_config,
    orchestrator_source,
    types_source,
)


class ResourceProfileScriptTest(unittest.TestCase):
    def test_srs_orch_002_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/orchestrator_resource_profile_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ORCH-002 PASS", result.stdout)
        for needle in (
            "LIVE_PROFILE_MEM_MB=512",
            "LIVE_PROFILE_CPU_HUNDREDTHS=25",
            "PAPER_PROFILE_MEM_MB=300",
            "PAPER_PROFILE_CPU_HUNDREDTHS=10",
            "mem in [64, 65536] MB",
            "cpu in [5, 1600] hundredths",
            "ResourceProfile with the 2 required fields",
            "(mem_mb, cpu_hundredths)",
            "rejects 10 forbidden vendor/float-CPU fields",
            "ResourceProfileError with 4 validation-failure variants",
            "MemBelowFloor, MemAboveCeiling, CpuBelowFloor, CpuAboveCeiling",
            "ResourceProfile methods (live_default, paper_default, for_mode, validate)",
            "Orchestrator helpers (live_profile_default, paper_profile_default, profile_for_mode, profile_for_mode_from_lookup, profile_for_mode_via_env_lookup, profile_for_mode_from_env)",
            "request.profile.validate",
            "before `runtime.create`",
            "StructuredOrchestratorError::resource_profile_invalid",
            "ResourceProfileInvalid",
            "RESOURCE_PROFILE_INVALID",
            "12 forbidden runtime / sink / registry mutators",
            "profile: request.profile",
            "no silent re-defaulting at the gate",
            "SRS-ARCH-005 catalogue defaults agree",
            "ATP_LIVE_STRATEGY_MEM_MB",
            "orch_2_resource_profile_contract",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class SpecConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_spec_constants_match_syrs_sys_11(self) -> None:
        evidence = check_spec_constants(self.config, self.types_src)
        for needle in ("=512", "=25", "=300", "=10"):
            self.assertIn(needle, evidence)

    def test_drifted_live_mem_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const LIVE_PROFILE_MEM_MB: u32 = 512;",
            "pub const LIVE_PROFILE_MEM_MB: u32 = 511;",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("LIVE_PROFILE_MEM_MB", str(ctx.exception))

    def test_drifted_paper_cpu_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const PAPER_PROFILE_CPU_HUNDREDTHS: u32 = 10;",
            "pub const PAPER_PROFILE_CPU_HUNDREDTHS: u32 = 11;",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("PAPER_PROFILE_CPU_HUNDREDTHS", str(ctx.exception))

    def test_missing_live_cpu_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const LIVE_PROFILE_CPU_HUNDREDTHS: u32 = 25;",
            "// removed for test",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("LIVE_PROFILE_CPU_HUNDREDTHS", str(ctx.exception))


class ValidationConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_validation_bounds_match_catalogue(self) -> None:
        evidence = check_validation_constants(self.config, self.types_src)
        self.assertIn("[64, 65536]", evidence)
        self.assertIn("[5, 1600]", evidence)

    def test_drifted_mem_floor_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const RESOURCE_PROFILE_MEM_FLOOR_MB: u32 = 64;",
            "pub const RESOURCE_PROFILE_MEM_FLOOR_MB: u32 = 32;",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_validation_constants(self.config, mutated)
        self.assertIn("RESOURCE_PROFILE_MEM_FLOOR_MB", str(ctx.exception))


class ResourceProfileStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_two_required_fields(self) -> None:
        evidence = check_resource_profile_struct(self.config, self.types_src)
        for field in ("mem_mb", "cpu_hundredths"):
            self.assertIn(field, evidence)

    def test_missing_mem_mb_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub mem_mb: u32,",
            "pub mem_mb_x: u32,",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_resource_profile_struct(self.config, mutated)
        self.assertIn("mem_mb", str(ctx.exception))

    def test_struct_rejects_leaked_float_cpu_field(self) -> None:
        # Future-proofing: a refactor that re-introduced f32 cores would
        # be caught by the `cpu_cores_f32` token in forbidden_fields.
        mutated = self.types_src.replace(
            "pub struct ResourceProfile {\n    pub mem_mb: u32,",
            "pub struct ResourceProfile {\n    pub cpu_cores_f32: f32,\n    pub mem_mb: u32,",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_resource_profile_struct(self.config, mutated)
        self.assertIn("cpu_cores_f32", str(ctx.exception))

    def test_struct_rejects_leaked_docker_image_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct ResourceProfile {\n    pub mem_mb: u32,",
            "pub struct ResourceProfile {\n    pub docker_image: String,\n    pub mem_mb: u32,",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_resource_profile_struct(self.config, mutated)
        self.assertIn("docker_image", str(ctx.exception))


class ResourceProfileErrorEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_enum_covers_four_validation_variants(self) -> None:
        evidence = check_resource_profile_error_enum(self.config, self.types_src)
        for variant in (
            "MemBelowFloor",
            "MemAboveCeiling",
            "CpuBelowFloor",
            "CpuAboveCeiling",
        ):
            self.assertIn(variant, evidence)

    def test_missing_mem_below_floor_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "MemBelowFloor { mem_mb: u32, floor_mb: u32 },",
            "MemBelowFloorX { mem_mb: u32, floor_mb: u32 },",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_resource_profile_error_enum(self.config, mutated)
        self.assertIn("MemBelowFloor", str(ctx.exception))


class ResourceProfileMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_four_methods_present(self) -> None:
        evidence = check_resource_profile_methods(self.config, self.types_src)
        for method in ("live_default", "paper_default", "for_mode", "validate"):
            self.assertIn(method, evidence)

    def test_missing_validate_method_is_caught(self) -> None:
        mutated = re.sub(
            r"\bpub fn validate\b",
            "pub fn validateX",
            self.types_src,
            count=1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_resource_profile_methods(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))


class OrchestratorHelperMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_helpers_present(self) -> None:
        evidence = check_orchestrator_helper_methods(self.config, self.orch_src)
        for helper in (
            "live_profile_default",
            "paper_profile_default",
            "profile_for_mode",
            "profile_for_mode_from_lookup",
            "profile_for_mode_via_env_lookup",
            "profile_for_mode_from_env",
        ):
            self.assertIn(helper, evidence)

    def test_missing_profile_for_mode_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "pub const fn profile_for_mode",
            "pub const fn profile_for_modeX",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_orchestrator_helper_methods(self.config, mutated)
        self.assertIn("profile_for_mode", str(ctx.exception))

    def test_missing_profile_for_mode_from_env_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "pub fn profile_for_mode_from_env",
            "pub fn profile_for_mode_from_envX",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_orchestrator_helper_methods(self.config, mutated)
        self.assertIn("profile_for_mode_from_env", str(ctx.exception))


class LaunchValidateGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_guard_passes(self) -> None:
        evidence = check_launch_validate_guard(self.config, self.orch_src)
        self.assertIn("request.profile.validate", evidence)
        self.assertIn("before `runtime.create`", evidence)

    def test_missing_validate_call_is_caught(self) -> None:
        # Remove the validation block by replacing the if-let block with
        # a no-op so the validate call disappears from the body.
        mutated = re.sub(
            r"if let Err\(violation\) = request\.profile\.validate\(\) \{[^}]*\}",
            "/* validation removed for test */",
            self.orch_src,
            count=1,
            flags=re.DOTALL,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_launch_validate_guard(self.config, mutated)
        self.assertIn("request.profile.validate", str(ctx.exception))

    def test_validate_after_create_swap_is_caught(self) -> None:
        # Move the validation gate AFTER runtime.create — the positional
        # invariant must catch this. We do it by relocating the if-let
        # block to immediately after the create call.
        validate_block = (
            "if let Err(violation) = request.profile.validate() {\n"
            "            return Err(StructuredOrchestratorError::resource_profile_invalid(\n"
            "                request, violation,\n"
            "            ));\n"
            "        }\n"
        )
        # Remove the original block, leave a marker, then insert after create.
        without_original = re.sub(
            r"if let Err\(violation\) = request\.profile\.validate\(\) \{[^}]*\}",
            "/* moved */",
            self.orch_src,
            count=1,
            flags=re.DOTALL,
        )
        mutated = without_original.replace(
            "runtime.create(&request);",
            "runtime.create(&request);\n        " + validate_block,
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_launch_validate_guard(self.config, mutated)
        self.assertIn("AFTER", str(ctx.exception))

    def test_missing_rejection_factory_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "StructuredOrchestratorError::resource_profile_invalid(",
            "StructuredOrchestratorError::startup_deadline_exceeded(",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_launch_validate_guard(self.config, mutated)
        self.assertIn("resource_profile_invalid", str(ctx.exception))

    def test_runtime_mutation_in_rejection_arm_is_caught(self) -> None:
        # Inject a runtime.destroy call inside the validation rejection
        # arm. The forbidden_calls_on_rejection allowlist must catch it.
        mutated = self.orch_src.replace(
            "if let Err(violation) = request.profile.validate() {\n",
            (
                "if let Err(violation) = request.profile.validate() {\n"
                "            runtime.destroy(&request.strategy_id);\n"
            ),
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_launch_validate_guard(self.config, mutated)
        self.assertIn("runtime.destroy", str(ctx.exception))


class OutcomeProfileEqualityGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_guard_passes(self) -> None:
        evidence = check_outcome_profile_equality_guard(self.config, self.orch_src)
        self.assertIn("profile: request.profile", evidence)

    def test_outcome_profile_re_default_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "profile: request.profile,",
            "profile: ResourceProfile::live_default(),",
            1,
        )
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_outcome_profile_equality_guard(self.config, mutated)
        # Either the missing required token OR the forbidden token
        # firing is acceptable evidence.
        message = str(ctx.exception)
        self.assertTrue(
            "request.profile" in message or "live_default" in message,
            message,
        )


class ConfigCatalogueBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_binding_passes(self) -> None:
        evidence = check_config_catalogue_binding(self.config, self.types_src)
        self.assertIn("4 resource-profile config keys", evidence)

    def test_drifted_catalogue_default_is_caught(self) -> None:
        # Mutate the catalogue's ATP_LIVE_STRATEGY_MEM_MB default to
        # disagree with LIVE_PROFILE_MEM_MB.
        config = load_config()
        for entry in config["configuration"]["keys"]:
            if entry["name"] == "ATP_LIVE_STRATEGY_MEM_MB":
                entry["default"] = "511"
                break
        with self.assertRaises(ResourceProfileCheckError) as ctx:
            check_config_catalogue_binding(config, self.types_src)
        self.assertIn("ATP_LIVE_STRATEGY_MEM_MB", str(ctx.exception))


class StaticAggregatorTest(unittest.TestCase):
    def test_static_aggregator_returns_nine_bullets(self) -> None:
        config = load_config()
        evidence = assert_orchestrator_resource_profile_static(config, ROOT)
        # 9 _STATIC_CHECKS entries (cargo smoke is added by run_checks
        # but excluded from the static aggregator used by
        # tools/architecture_check.py).
        self.assertEqual(len(evidence), 9, evidence)


if __name__ == "__main__":
    unittest.main()
