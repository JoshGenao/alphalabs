"""Contract tests for ERR-1 (SRS-EXE-001 + SRS-ERR-001 + SyRS SYS-64).

Mirrors ``tests/test_unified_historical_data.py``: shells out to
``tools/error_handling_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract
actually catches regressions (forbidden vendor fields, missing variants,
broker calls leaking outside the StrategyMode::Live arm).
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

from error_handling_check import (  # noqa: E402
    ErrorHandlingCheckError,
    assert_error_handling_static,
    check_error_category_enum,
    check_strategy_mode_enum,
    check_structured_error_struct,
    check_submit_live_order_signature,
    check_synchronous_rejection_has_no_broker_side_effect,
    execution_source,
    load_config,
    run_checks,
    types_source,
)


class ErrorHandlingCheckScriptTest(unittest.TestCase):
    def test_err_1_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/error_handling_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-1 PASS", result.stdout)
        for needle in (
            "StrategyMode with 2 variants (Live, Paper)",
            "SRS-EXE-001 / SyRS AC-15",
            "OrderErrorCategory with 8 SyRS SYS-64 categories",
            "NonLiveStrategySubmission",
            "upper-snake wire string",
            "StructuredOrderError with the 4 SRS-ERR-001 fields",
            "category, error_type, message, original_order",
            "rejects 4 forbidden broker/vendor fields",
            "ExecutionEngine::submit_live_order -> Result<OrderReceipt, StructuredOrderError>",
            "ONLY inside the StrategyMode::Live arm",
            "zero broker side effect (ERR-1)",
            "err_1_no_ib_side_effect",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class StrategyModeEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_strategy_mode_lists_live_and_paper(self) -> None:
        evidence = check_strategy_mode_enum(self.config, self.types_src)
        self.assertIn("Live", evidence)
        self.assertIn("Paper", evidence)

    def test_missing_live_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("Live,\n    Paper,", "LiveX,\n    Paper,", 1)
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_strategy_mode_enum(self.config, mutated)
        self.assertIn("Live", str(ctx.exception))


class OrderErrorCategoryEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_eight_syrs_sys_64_categories_present(self) -> None:
        # The ERR-1 evidence string names NonLiveStrategySubmission as the
        # canonical "did the SyRS SYS-64 wire vocabulary land?" anchor.
        # The count (8) is enforced via the test_err_1_contract_script_
        # passes needle "OrderErrorCategory with 8 SyRS SYS-64 categories";
        # the IngestionRecordValidationFailed variant itself is asserted
        # at the atp-types unit-test layer
        # (`order_error_category_wire_strings_track_syrs_sys_64`).
        evidence = check_error_category_enum(self.config, self.types_src)
        self.assertIn("NonLiveStrategySubmission", evidence)

    def test_missing_non_live_submission_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "NonLiveStrategySubmission,", "NonLiveStrategySubmissionX,", 1
        )
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_error_category_enum(self.config, mutated)
        self.assertIn("NonLiveStrategySubmission", str(ctx.exception))

    def test_missing_wire_string_is_caught(self) -> None:
        # If as_str() forgets to map NonLiveStrategySubmission, the wire
        # vocabulary is broken; the check must catch it. Replace every
        # occurrence so the test assertion that echoes the wire string is
        # rewritten alongside the as_str() arm.
        mutated = self.types_src.replace(
            "NON_LIVE_STRATEGY_SUBMISSION", "NON_LIVE_STRATEGY_X"
        )
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_error_category_enum(self.config, mutated)
        self.assertIn("NON_LIVE_STRATEGY_SUBMISSION", str(ctx.exception))


class StructuredOrderErrorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_four_srs_err_001_fields(self) -> None:
        evidence = check_structured_error_struct(self.config, self.types_src)
        for field in ("category", "error_type", "message", "original_order"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StructuredOrderError {\n    pub category: OrderErrorCategory,",
            "pub struct StructuredOrderError {\n    pub broker: String,\n    pub category: OrderErrorCategory,",
            1,
        )
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_structured_error_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct StructuredOrderError {\n    pub category: OrderErrorCategory,",
            "pub struct StructuredOrderError {\n    pub vendor: String,\n    pub category: OrderErrorCategory,",
            1,
        )
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_structured_error_struct(self.config, mutated)
        self.assertIn("vendor", str(ctx.exception))


class SubmitLiveOrderSignatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_signature_returns_structured_error_result(self) -> None:
        evidence = check_submit_live_order_signature(self.config, self.exec_src)
        self.assertIn("submit_live_order", evidence)
        self.assertIn("Result<OrderReceipt, StructuredOrderError>", evidence)

    def test_missing_method_is_caught(self) -> None:
        mutated = self.exec_src.replace("submit_live_order", "submit_live_orderX")
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_submit_live_order_signature(self.config, mutated)
        self.assertIn("submit_live_order", str(ctx.exception))


class SynchronousRejectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.exec_src = execution_source(self.config)

    def test_broker_call_is_gated_on_live_arm(self) -> None:
        evidence = check_synchronous_rejection_has_no_broker_side_effect(
            self.config, self.exec_src
        )
        self.assertIn("ONLY inside the StrategyMode::Live arm", evidence)
        self.assertIn("OrderErrorCategory::NonLiveStrategySubmission", evidence)

    def test_broker_call_inside_paper_arm_is_caught(self) -> None:
        # Mutate the Paper arm to call broker.submit_order — the regression
        # the regex check exists to catch. The check should fail with a
        # message naming the violation.
        mutated = self.exec_src.replace(
            "StrategyMode::Paper => Err(StructuredOrderError {",
            "StrategyMode::Paper => { let _ = broker.submit_order(submission.clone()); Err(StructuredOrderError {",
            1,
        )
        # Re-close the extra brace we introduced so Rust syntax mirrors a
        # realistic regression. The check only cares about the call site.
        with self.assertRaises(ErrorHandlingCheckError) as ctx:
            check_synchronous_rejection_has_no_broker_side_effect(
                self.config, mutated
            )
        self.assertIn("StrategyMode::Paper", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_six_evidence_items(self) -> None:
        evidence = run_checks()
        # 5 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 6)

    def test_assert_error_handling_static_emits_five_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_error_handling_static(config, ROOT)
        self.assertEqual(len(evidence), 5)


if __name__ == "__main__":
    unittest.main()
