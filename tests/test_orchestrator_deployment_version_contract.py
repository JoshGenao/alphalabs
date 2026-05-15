"""Contract tests for SRS-ORCH-004 (SyRS SYS-79 / SYS-41 / SYS-21 / IF-9;
StRS SN-1.01 / SN-1.10 / SN-1.02).

Mirrors ``tests/test_orchestrator_workload_priority_contract.py``: shells
out to ``tools/orchestrator_deployment_version_check.py``, then exercises
each per-check function in-process, including negative spot-checks that
verify the contract actually catches regressions (drifted constants,
missing struct fields, missing enum variants, missing port methods, the
launch gate's validate-before-create order, the DeadlineExceeded arm
skipping the version record, etc.).
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

from orchestrator_deployment_version_check import (  # noqa: E402
    DeploymentVersionCheckError,
    assert_orchestrator_deployment_version_static,
    check_deployed_version_invalid_factory,
    check_deployed_version_struct,
    check_launch_deployment_version_guard,
    check_order_error_category_variant_and_wire_string,
    check_orchestrator_helper_methods,
    check_registry_port,
    check_source_hash_error_enum,
    check_source_hash_struct,
    check_spec_constants,
    check_strategy_launch_outcome_deployed_version_field,
    check_strategy_launch_request_deployment_hash_type,
    load_config,
    orchestrator_source,
    types_source,
)


class DeploymentVersionScriptTest(unittest.TestCase):
    def test_srs_orch_004_contract_script_passes(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "tools/orchestrator_deployment_version_check.py",
                "--skip-cargo",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-ORCH-004 PASS", result.stdout)
        for needle in (
            "SOURCE_HASH_ALGORITHM_PREFIX='sha256:'",
            "SOURCE_HASH_DIGEST_HEX_LENGTH=64",
            "SOURCE_HASH_TOTAL_LENGTH=71",
            "SourceHash as a newtype over String",
            "(new, validate, validate_str, as_str, algorithm, digest)",
            "SourceHashError with the 4 validation-failure variants",
            "MissingAlgorithmPrefix, UnknownAlgorithm, InvalidDigestLength, NonHexDigest",
            "DeployedVersion with the 2 required fields",
            "(source_hash, deployed_at_seconds)",
            "rejects 8 forbidden vendor/build-system fields",
            "(new, version_identifier)",
            "StrategyLaunchRequest.deployment_hash as `SourceHash`",
            "StrategyLaunchOutcome.deployed_version: DeployedVersion",
            "OrderErrorCategory::DeployedVersionInvalid",
            "DEPLOYED_VERSION_INVALID",
            "rejection factory `deployed_version_invalid`",
            "category invariant pinned via debug_assert",
            "trait DeployedVersionRegistry with methods (record, lookup)",
            "Result-returning IO signatures",
            "typed failure surface DeployedVersionRegistryError",
            "no vendor-SDK / durable-store / hash-library imports inside the crate",
            "(deployed_version_for)",
            "validate-deployment_hash-before-runtime.create",
            "version_registry.record on the ReadyWithinDeadline arm only",
            "DeadlineExceeded arm skips it",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class SpecConstantsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_spec_constants_pin_sha256_wire_form(self) -> None:
        evidence = check_spec_constants(self.config, self.types_src)
        self.assertIn("'sha256:'", evidence)
        self.assertIn("=64", evidence)
        self.assertIn("=71", evidence)

    def test_drifted_algorithm_prefix_is_caught(self) -> None:
        mutated = self.types_src.replace(
            'pub const SOURCE_HASH_ALGORITHM_PREFIX: &str = "sha256:";',
            'pub const SOURCE_HASH_ALGORITHM_PREFIX: &str = "md5:";',
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("SOURCE_HASH_ALGORITHM_PREFIX", str(ctx.exception))

    def test_drifted_digest_length_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const SOURCE_HASH_DIGEST_HEX_LENGTH: usize = 64;",
            "pub const SOURCE_HASH_DIGEST_HEX_LENGTH: usize = 40;",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("SOURCE_HASH_DIGEST_HEX_LENGTH", str(ctx.exception))

    def test_missing_total_length_constant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub const SOURCE_HASH_TOTAL_LENGTH: usize",
            "pub const SOURCE_HASH_TOTAL_LENGTH_RENAMED: usize",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_spec_constants(self.config, mutated)
        self.assertIn("SOURCE_HASH_TOTAL_LENGTH", str(ctx.exception))


class SourceHashStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_newtype_shape_passes(self) -> None:
        evidence = check_source_hash_struct(self.config, self.types_src)
        self.assertIn("newtype over String", evidence)

    def test_missing_validate_method_is_caught(self) -> None:
        # SourceHash::validate is the third `pub fn validate(` in the
        # file (ResourceProfile and HostMemorySafetyMargin each have
        # their own). The contract check only requires ONE `pub fn
        # validate` to exist — mutating any single occurrence keeps
        # the regex satisfied. To force a regression, rename EVERY
        # `pub fn validate(&self)` so the discriminator
        # disappears.
        mutated = self.types_src.replace(
            "pub fn validate(&self)",
            "pub fn validate_removed(&self)",
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_source_hash_struct(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))

    def test_drifted_newtype_inner_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SourceHash(String);",
            "pub struct SourceHash(Vec<u8>);",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_source_hash_struct(self.config, mutated)
        self.assertIn("SourceHash", str(ctx.exception))


class SourceHashErrorEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_four_variants_present(self) -> None:
        evidence = check_source_hash_error_enum(self.config, self.types_src)
        for variant in (
            "MissingAlgorithmPrefix",
            "UnknownAlgorithm",
            "InvalidDigestLength",
            "NonHexDigest",
        ):
            self.assertIn(variant, evidence)

    def test_missing_non_hex_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "NonHexDigest { found: char },",
            "// removed",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_source_hash_error_enum(self.config, mutated)
        self.assertIn("NonHexDigest", str(ctx.exception))


class DeployedVersionStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_only_two_fields(self) -> None:
        evidence = check_deployed_version_struct(self.config, self.types_src)
        self.assertIn("source_hash", evidence)
        self.assertIn("deployed_at_seconds", evidence)

    def test_missing_source_hash_field_is_caught(self) -> None:
        mutated = re.sub(
            r"pub struct DeployedVersion \{\s*pub source_hash: SourceHash,",
            "pub struct DeployedVersion {",
            self.types_src,
            count=1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_deployed_version_struct(self.config, mutated)
        self.assertIn("source_hash", str(ctx.exception))

    def test_leaked_docker_image_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub deployed_at_seconds: u64,\n}",
            "pub deployed_at_seconds: u64,\n    pub docker_image: String,\n}",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_deployed_version_struct(self.config, mutated)
        self.assertIn("docker_image", str(ctx.exception))

    def test_leaked_git_commit_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub deployed_at_seconds: u64,\n}",
            "pub deployed_at_seconds: u64,\n    pub git_commit: String,\n}",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_deployed_version_struct(self.config, mutated)
        self.assertIn("git_commit", str(ctx.exception))


class StrategyLaunchRequestTypeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_deployment_hash_is_typed_source_hash(self) -> None:
        evidence = check_strategy_launch_request_deployment_hash_type(
            self.config, self.types_src
        )
        self.assertIn("SourceHash", evidence)

    def test_string_type_drift_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub deployment_hash: SourceHash,",
            "pub deployment_hash: String,",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_strategy_launch_request_deployment_hash_type(
                self.config, mutated
            )
        self.assertIn("SourceHash", str(ctx.exception))


class StrategyLaunchOutcomeFieldTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_outcome_carries_deployed_version(self) -> None:
        evidence = check_strategy_launch_outcome_deployed_version_field(
            self.config, self.types_src
        )
        self.assertIn("deployed_version", evidence)

    def test_missing_deployed_version_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub deployed_version: DeployedVersion,",
            "// removed",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_strategy_launch_outcome_deployed_version_field(
                self.config, mutated
            )
        self.assertIn("deployed_version", str(ctx.exception))


class OrderErrorCategoryVariantTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_variant_maps_to_wire_string(self) -> None:
        evidence = check_order_error_category_variant_and_wire_string(
            self.config, self.types_src
        )
        self.assertIn("DEPLOYED_VERSION_INVALID", evidence)

    def test_drifted_wire_string_is_caught(self) -> None:
        mutated = self.types_src.replace(
            'Self::DeployedVersionInvalid => "DEPLOYED_VERSION_INVALID",',
            'Self::DeployedVersionInvalid => "DEPLOYMENT_VERSION_INVALID",',
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_order_error_category_variant_and_wire_string(
                self.config, mutated
            )
        self.assertIn("DEPLOYED_VERSION_INVALID", str(ctx.exception))


class DeployedVersionInvalidFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_factory_pins_category_invariant(self) -> None:
        evidence = check_deployed_version_invalid_factory(
            self.config, self.types_src
        )
        self.assertIn("debug_assert", evidence)

    def test_missing_debug_assert_is_caught(self) -> None:
        # Replace the debug_assert inside `deployed_version_invalid` only.
        mutated = self.types_src.replace(
            "matches!(category, OrderErrorCategory::DeployedVersionInvalid),\n            \"StructuredOrchestratorError must carry DeployedVersionInvalid\"",
            "// removed",
            1,
        )
        # Also remove the debug_assert! call site so the body lacks it.
        mutated = mutated.replace(
            "        debug_assert!(\n            // removed\n        );",
            "",
            1,
        )
        # If the original `debug_assert!` is still present (multiple
        # factories share the pattern), drop the entire block scoped
        # to this factory:
        mutated = re.sub(
            r"(pub fn deployed_version_invalid[^\{]*\{\s*let category = OrderErrorCategory::DeployedVersionInvalid;\s*)debug_assert!\([^;]+;",
            r"\1",
            mutated,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_deployed_version_invalid_factory(self.config, mutated)
        self.assertIn("debug_assert", str(ctx.exception))


class RegistryPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_trait_declares_both_methods(self) -> None:
        evidence = check_registry_port(self.config, self.orch_src)
        self.assertIn("record, lookup", evidence)

    def test_missing_lookup_method_is_caught(self) -> None:
        mutated = re.sub(
            r"fn lookup\([^\)]*\)\s*->\s*Result<Option<DeployedVersion>,\s*DeployedVersionRegistryError>\s*;",
            "// removed",
            self.orch_src,
            count=1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_registry_port(self.config, mutated)
        self.assertIn("lookup", str(ctx.exception))

    def test_forbidden_sha2_import_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "use std::fmt;",
            "use std::fmt;\nuse sha2::Sha256;",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_registry_port(self.config, mutated)
        self.assertIn("sha2", str(ctx.exception))

    def test_bare_unit_record_signature_is_caught(self) -> None:
        # Replace the Result-returning signature with a bare `();`.
        mutated = self.orch_src.replace(
            "fn record(\n        &self,\n        strategy_id: &StrategyId,\n        version: DeployedVersion,\n    ) -> Result<(), DeployedVersionRegistryError>",
            "fn record(&self, strategy_id: &StrategyId, version: DeployedVersion)",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_registry_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class OrchestratorHelperMethodsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_deployed_version_for_helper_is_exposed(self) -> None:
        evidence = check_orchestrator_helper_methods(self.config, self.orch_src)
        self.assertIn("deployed_version_for", evidence)

    def test_missing_helper_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "pub fn deployed_version_for(",
            "pub fn deployed_version_for_removed(",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_orchestrator_helper_methods(self.config, mutated)
        self.assertIn("deployed_version_for", str(ctx.exception))


class LaunchDeploymentVersionGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.orch_src = orchestrator_source(self.config)

    def test_guard_pins_validate_before_create_and_record_on_happy_path(
        self,
    ) -> None:
        evidence = check_launch_deployment_version_guard(
            self.config, self.orch_src
        )
        self.assertIn("validate-deployment_hash-before-runtime.create", evidence)
        self.assertIn("DeadlineExceeded arm skips it", evidence)

    def test_missing_validation_call_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "if let Err(violation) = request.deployment_hash.validate() {",
            "if false {",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_launch_deployment_version_guard(self.config, mutated)
        self.assertIn("request.deployment_hash.validate()", str(ctx.exception))

    def test_missing_record_call_is_caught(self) -> None:
        # Drop the version_registry.record(...) call inside launch.
        # The call spans multiple lines and contains a nested `.clone()`
        # so [^)]* cannot match it; use a more permissive pattern.
        mutated = re.sub(
            r"let _ = version_registry\s*\.\s*record\(",
            "let _ = some_other_call(",
            self.orch_src,
            count=1,
            flags=re.DOTALL,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_launch_deployment_version_guard(self.config, mutated)
        self.assertIn("version_registry.record", str(ctx.exception))

    def test_missing_rejection_factory_is_caught(self) -> None:
        mutated = self.orch_src.replace(
            "StructuredOrchestratorError::deployed_version_invalid",
            "StructuredOrchestratorError::OTHER_FACTORY",
            1,
        )
        with self.assertRaises(DeploymentVersionCheckError) as ctx:
            check_launch_deployment_version_guard(self.config, mutated)
        self.assertIn("deployed_version_invalid", str(ctx.exception))


class AssertOrchestratorDeploymentVersionStaticTest(unittest.TestCase):
    def test_full_static_evidence_returned(self) -> None:
        config = load_config()
        evidence = assert_orchestrator_deployment_version_static(config, ROOT)
        # 11 per-check evidence strings expected (CHECKS list length).
        self.assertEqual(len(evidence), 11)
        for line in evidence:
            self.assertTrue(isinstance(line, str) and line)


if __name__ == "__main__":
    unittest.main()
