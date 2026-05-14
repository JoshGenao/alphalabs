"""Contract tests for ERR-6 (SRS-DATA-002 + SRS-DATA-004 + SyRS SYS-31 /
SYS-55 + StRS A-10 / SN-1.26 / SN-1.27).

Mirrors ``tests/test_ingestion_validation_contract.py``: shells out to
``tools/pacing_budget_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the
contract actually catches regressions (forbidden vendor / tick fields,
missing variants, dropped ``events.record`` call, scheduler-mutation
sneaking into the BudgetExceeded leaf, drifted wire string,
acceptance leak into the BudgetExceeded leaf, dropped
projected/permitted reads).
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

from pacing_budget_check import (  # noqa: E402
    PacingBudgetCheckError,
    assert_pacing_budget_static,
    check_event_sink_port,
    check_ingestion_job_request_struct,
    check_pacing_budget_event_struct,
    check_pacing_budget_guard,
    check_pacing_budget_state_enum,
    check_validator_port,
    data_source,
    load_config,
    run_checks,
    types_source,
)


class PacingBudgetCheckScriptTest(unittest.TestCase):
    def test_err_6_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/pacing_budget_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-6 PASS", result.stdout)
        for needle in (
            "IngestionJobRequest with the 2 required fields",
            "job_kind, window_seconds",
            "PacingBudgetState with 2 states",
            "WithinBudget, BudgetExceeded",
            "SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55",
            "PacingBudgetEvent with the 5 required fields",
            "state, job_kind, projected_requests, permitted_requests, observed_at_seconds",
            "rejects 7 forbidden vendor/broker/tick fields",
            "PacingBudgetValidator with 3 methods",
            "projected_requests, permitted_requests, check_budget",
            "PacingBudgetEventSink with 1 method",
            "PacingBudgetState::WithinBudget",
            "PacingBudgetState::BudgetExceeded",
            "OrderErrorCategory::IngestionPacingBudgetExceeded",
            "validator.projected_requests",
            "validator.permitted_requests",
            "events.record",
            "starts nothing on the scheduler (ERR-6)",
            "err_6_pacing_budget_blocked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class IngestionJobRequestStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_job_kind_and_window_seconds(self) -> None:
        evidence = check_ingestion_job_request_struct(self.config, self.types_src)
        for field in ("job_kind", "window_seconds"):
            self.assertIn(field, evidence)

    def test_missing_job_kind_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub job_kind: String,", "", 1)
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_ingestion_job_request_struct(self.config, mutated)
        self.assertIn("job_kind", str(ctx.exception))

    def test_missing_window_seconds_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub window_seconds: u64,", "", 1)
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_ingestion_job_request_struct(self.config, mutated)
        self.assertIn("window_seconds", str(ctx.exception))

    def test_struct_rejects_leaked_databento_dataset_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionJobRequest {\n    pub job_kind: String,",
            "pub struct IngestionJobRequest {\n    pub databento_dataset: String,\n    pub job_kind: String,",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_ingestion_job_request_struct(self.config, mutated)
        self.assertIn("databento_dataset", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_credentials_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionJobRequest {\n    pub job_kind: String,",
            "pub struct IngestionJobRequest {\n    pub vendor_credentials: String,\n    pub job_kind: String,",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_ingestion_job_request_struct(self.config, mutated)
        self.assertIn("vendor_credentials", str(ctx.exception))


class PacingBudgetStateEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_states_present(self) -> None:
        evidence = check_pacing_budget_state_enum(self.config, self.types_src)
        for variant in ("WithinBudget", "BudgetExceeded"):
            self.assertIn(variant, evidence)

    def test_missing_within_budget_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    WithinBudget,", "    WithinBudgetX,", 1)
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_state_enum(self.config, mutated)
        self.assertIn("WithinBudget", str(ctx.exception))

    def test_missing_budget_exceeded_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    BudgetExceeded,", "    BudgetExceededX,", 1
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_state_enum(self.config, mutated)
        self.assertIn("BudgetExceeded", str(ctx.exception))


class PacingBudgetEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_five_required_fields(self) -> None:
        evidence = check_pacing_budget_event_struct(self.config, self.types_src)
        for field in (
            "state",
            "job_kind",
            "projected_requests",
            "permitted_requests",
            "observed_at_seconds",
        ):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct PacingBudgetEvent {\n    pub state: PacingBudgetState,",
            "pub struct PacingBudgetEvent {\n    pub broker: String,\n    pub state: PacingBudgetState,",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_tick_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct PacingBudgetEvent {\n    pub state: PacingBudgetState,",
            "pub struct PacingBudgetEvent {\n    pub tick_id: u64,\n    pub state: PacingBudgetState,",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_event_struct(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))

    def test_struct_rejects_leaked_sharadar_table_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct PacingBudgetEvent {\n    pub state: PacingBudgetState,",
            "pub struct PacingBudgetEvent {\n    pub sharadar_table: String,\n    pub state: PacingBudgetState,",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_event_struct(self.config, mutated)
        self.assertIn("sharadar_table", str(ctx.exception))

    def test_missing_projected_requests_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub projected_requests: u32,", "", 1)
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_event_struct(self.config, mutated)
        self.assertIn("projected_requests", str(ctx.exception))

    def test_missing_permitted_requests_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub permitted_requests: u32,", "", 1)
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_event_struct(self.config, mutated)
        self.assertIn("permitted_requests", str(ctx.exception))


class PacingBudgetValidatorPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_port_exposes_three_methods(self) -> None:
        evidence = check_validator_port(self.config, self.data_src)
        for method in ("projected_requests", "permitted_requests", "check_budget"):
            self.assertIn(method, evidence)

    def test_missing_check_budget_method_is_caught(self) -> None:
        mutated = self.data_src.replace(
            "fn check_budget(",
            "fn dropped_check_budget(",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_validator_port(self.config, mutated)
        self.assertIn("check_budget", str(ctx.exception))

    def test_missing_projected_requests_method_is_caught(self) -> None:
        # There are two `fn projected_requests(` declarations: one on the
        # trait, one inside the in-crate test stub `PacingBudgetStub`.
        # Renaming both keeps the file parseable while removing the
        # trait method.
        mutated = self.data_src.replace(
            "fn projected_requests(",
            "fn dropped_projected_requests(",
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_validator_port(self.config, mutated)
        self.assertIn("projected_requests", str(ctx.exception))


class PacingBudgetEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_event_sink_port(self.config, self.data_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.data_src.replace(
            "fn record(&self, event: PacingBudgetEvent)",
            "fn dropped_record_method(&self, event: PacingBudgetEvent)",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class PacingBudgetGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_acceptance_is_gated_on_within_budget_leaf(self) -> None:
        evidence = check_pacing_budget_guard(self.config, self.data_src)
        self.assertIn("PacingBudgetState::WithinBudget", evidence)
        self.assertIn("PacingBudgetState::BudgetExceeded", evidence)
        self.assertIn("OrderErrorCategory::IngestionPacingBudgetExceeded", evidence)
        self.assertIn("validator.projected_requests", evidence)
        self.assertIn("validator.permitted_requests", evidence)
        self.assertIn("events.record", evidence)
        self.assertIn("starts nothing on the scheduler", evidence)

    def test_missing_events_record_call_is_caught(self) -> None:
        # Strip the `events.record(PacingBudgetEvent { ... });` block so
        # the remaining source still parses.
        marker_open = "events.record(PacingBudgetEvent {"
        start = self.data_src.find(marker_open)
        self.assertGreaterEqual(
            start,
            0,
            "could not locate events.record(...) in data-crate source",
        )
        depth = 0
        index = start + len(marker_open) - 1
        while index < len(self.data_src):
            char = self.data_src[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        end = self.data_src.find(";", index) + 1
        mutated = (
            self.data_src[:start]
            + "/* event record removed */"
            + self.data_src[end:]
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("events.record", str(ctx.exception))

    def test_scheduler_start_in_budget_exceeded_leaf_is_caught(self) -> None:
        # Inject a scheduler.start() call into the BudgetExceeded leaf
        # — the regression the forbidden_mutations list exists to catch.
        mutated = self.data_src.replace(
            "events.record(PacingBudgetEvent {",
            "scheduler.start(); events.record(PacingBudgetEvent {",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("scheduler.start", str(ctx.exception))

    def test_self_start_job_in_budget_exceeded_leaf_is_caught(self) -> None:
        # A second forbidden-mutation regression — the self.start_job
        # form is particularly insidious because it's grammatically
        # valid Rust at the bare method level.
        mutated = self.data_src.replace(
            "events.record(PacingBudgetEvent {",
            "self.start_job(); events.record(PacingBudgetEvent {",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("self.start_job", str(ctx.exception))

    def test_acceptance_in_budget_exceeded_leaf_is_caught(self) -> None:
        # Inject an IngestionJobScheduled { ... } construction inside
        # the BudgetExceeded leaf — the regression the "BudgetExceeded
        # must not produce IngestionJobScheduled" rule exists to catch.
        mutated = self.data_src.replace(
            "events.record(PacingBudgetEvent {",
            "let _smuggled = IngestionJobScheduled { job_kind: schedule.job_kind.clone() }; events.record(PacingBudgetEvent {",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("zero acceptance side effect", str(ctx.exception))

    def test_missing_check_budget_call_is_caught(self) -> None:
        # Rewrite the match scrutinee so the gate stops calling the
        # validator — the regression the validator-call check exists to
        # catch.
        mutated = self.data_src.replace(
            "match validator.check_budget(&schedule)",
            "match never_called(&schedule)",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("validator.check_budget", str(ctx.exception))

    def test_missing_projected_requests_read_is_caught(self) -> None:
        # Strip the `let projected = validator.projected_requests(...);`
        # line — the regression the projected_call check exists to
        # catch. Without this read the event would carry a stale
        # numeric and break the TOCTOU closure.
        mutated = self.data_src.replace(
            "let projected = validator.projected_requests(&schedule);",
            "let projected = 0u32;",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("validator.projected_requests", str(ctx.exception))

    def test_missing_permitted_requests_read_is_caught(self) -> None:
        # Strip the `let permitted = validator.permitted_requests(...);`
        # line — same regression class as projected_requests.
        mutated = self.data_src.replace(
            "let permitted = validator.permitted_requests(&schedule);",
            "let permitted = 0u32;",
            1,
        )
        with self.assertRaises(PacingBudgetCheckError) as ctx:
            check_pacing_budget_guard(self.config, mutated)
        self.assertIn("validator.permitted_requests", str(ctx.exception))


class PacingBudgetWireStringTest(unittest.TestCase):
    """Wire-string drift is caught by the existing atp-types unit test
    `order_error_category_wire_strings_track_syrs_sys_64`. This test
    exercises the cross-crate linkage by spot-checking that the
    `OrderErrorCategory::IngestionPacingBudgetExceeded` variant is
    reachable from the atp-data crate's source (which the
    `pacing_budget_guard` static check pins through the factory call)."""

    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_data_crate_references_canonical_wire_string_source(self) -> None:
        self.assertIn(
            "OrderErrorCategory::IngestionPacingBudgetExceeded",
            self.data_src,
            "atp-data must reference the canonical wire-string variant",
        )


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_seven_evidence_items(self) -> None:
        evidence = run_checks()
        # 6 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 7)

    def test_assert_pacing_budget_static_emits_six_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_pacing_budget_static(config, ROOT)
        self.assertEqual(len(evidence), 6)


if __name__ == "__main__":
    unittest.main()
