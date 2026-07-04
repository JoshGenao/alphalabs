"""Contract tests for ERR-5 (SRS-DATA-013 + SyRS SYS-77 + StRS SN-1.26 / SN-1.27).

Mirrors ``tests/test_subscription_limit_contract.py``: shells out to
``tools/ingestion_validation_check.py``, then exercises each per-check
function in-process, including negative spot-checks that verify the
contract actually catches regressions (forbidden vendor / tick fields,
missing variants, dropped ``events.record`` call, primary-storage
mutation sneaking into the Quarantined leaf, drifted wire string,
acceptance leak into the Quarantined leaf).
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

from ingestion_validation_check import (  # noqa: E402
    IngestionValidationCheckError,
    assert_ingestion_validation_static,
    check_event_sink_port,
    check_ingestion_record_submission_struct,
    check_ingestion_validation_event_struct,
    check_ingestion_validation_guard,
    check_quarantine_reason_enum,
    check_record_validation_outcome_enum,
    check_record_validator_port,
    data_source,
    load_config,
    run_checks,
    types_source,
)


class IngestionValidationCheckScriptTest(unittest.TestCase):
    def test_err_5_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/ingestion_validation_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ERR-5 PASS", result.stdout)
        for needle in (
            "IngestionRecordSubmission with the 2 required fields",
            "source, record_hash",
            "RecordValidationOutcome with 2 states",
            "Valid, Quarantined",
            "SRS-DATA-013 / SyRS SYS-77",
            "QuarantineReason with 6 variants",
            "RangeViolation, OhlcOutOfBand, NegativeVolume, NullRequiredField, "
            "DuplicateRecord, OptionFieldMissing",
            "count-and-nature dashboard alert",
            "IngestionValidationEvent with the 5 required fields",
            "state, reason, source, record_hash, observed_at_seconds",
            "rejects 7 forbidden vendor/broker/tick fields",
            "RecordValidator with 1 method",
            "validate",
            "IngestionValidationEventSink with 1 method",
            "RecordValidationOutcome::Valid",
            "RecordValidationOutcome::Quarantined",
            "OrderErrorCategory::IngestionRecordValidationFailed",
            "events.record",
            "writes nothing to the primary storage tier (ERR-5)",
            "err_5_record_validation_blocked",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class IngestionRecordSubmissionStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_source_and_record_hash(self) -> None:
        evidence = check_ingestion_record_submission_struct(self.config, self.types_src)
        for field in ("source", "record_hash"):
            self.assertIn(field, evidence)

    def test_missing_source_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub source: String,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_record_submission_struct(self.config, mutated)
        self.assertIn("source", str(ctx.exception))

    def test_missing_record_hash_field_is_caught(self) -> None:
        # The forbidden-fields check fires before the missing-required
        # check could on Submission, but the required-field branch must
        # still catch missing fields when the body parses cleanly.
        mutated = self.types_src.replace(
            "pub struct IngestionRecordSubmission {\n    pub source: String,\n    pub record_hash: String,\n}",
            "pub struct IngestionRecordSubmission {\n    pub source: String,\n}",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_record_submission_struct(self.config, mutated)
        self.assertIn("record_hash", str(ctx.exception))

    def test_struct_rejects_leaked_databento_dataset_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionRecordSubmission {\n    pub source: String,",
            "pub struct IngestionRecordSubmission {\n    pub databento_dataset: String,\n    pub source: String,",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_record_submission_struct(self.config, mutated)
        self.assertIn("databento_dataset", str(ctx.exception))

    def test_struct_rejects_leaked_vendor_credentials_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionRecordSubmission {\n    pub source: String,",
            "pub struct IngestionRecordSubmission {\n    pub vendor_credentials: String,\n    pub source: String,",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_record_submission_struct(self.config, mutated)
        self.assertIn("vendor_credentials", str(ctx.exception))


class RecordValidationOutcomeEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_both_states_present(self) -> None:
        evidence = check_record_validation_outcome_enum(self.config, self.types_src)
        for variant in ("Valid", "Quarantined"):
            self.assertIn(variant, evidence)

    def test_missing_valid_variant_is_caught(self) -> None:
        mutated = self.types_src.replace("    Valid,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_record_validation_outcome_enum(self.config, mutated)
        self.assertIn("Valid", str(ctx.exception))

    def test_missing_quarantined_variant_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "    Quarantined(QuarantineReason),",
            "    QuarantinedX(QuarantineReason),",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_record_validation_outcome_enum(self.config, mutated)
        self.assertIn("Quarantined", str(ctx.exception))


class QuarantineReasonEnumTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_all_six_sys_77_rules_present(self) -> None:
        evidence = check_quarantine_reason_enum(self.config, self.types_src)
        for variant in (
            "RangeViolation",
            "OhlcOutOfBand",
            "NegativeVolume",
            "NullRequiredField",
            "DuplicateRecord",
            "OptionFieldMissing",
        ):
            self.assertIn(variant, evidence)

    def test_missing_range_violation_is_caught(self) -> None:
        mutated = self.types_src.replace("    RangeViolation,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_quarantine_reason_enum(self.config, mutated)
        self.assertIn("RangeViolation", str(ctx.exception))

    def test_missing_ohlc_out_of_band_is_caught(self) -> None:
        mutated = self.types_src.replace("    OhlcOutOfBand,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_quarantine_reason_enum(self.config, mutated)
        self.assertIn("OhlcOutOfBand", str(ctx.exception))

    def test_missing_duplicate_record_is_caught(self) -> None:
        mutated = self.types_src.replace("    DuplicateRecord,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_quarantine_reason_enum(self.config, mutated)
        self.assertIn("DuplicateRecord", str(ctx.exception))

    def test_missing_option_field_missing_is_caught(self) -> None:
        mutated = self.types_src.replace("    OptionFieldMissing,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_quarantine_reason_enum(self.config, mutated)
        self.assertIn("OptionFieldMissing", str(ctx.exception))


class IngestionValidationEventStructTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)

    def test_struct_carries_the_five_required_fields(self) -> None:
        evidence = check_ingestion_validation_event_struct(self.config, self.types_src)
        for field in ("state", "reason", "source", "record_hash", "observed_at_seconds"):
            self.assertIn(field, evidence)

    def test_struct_rejects_leaked_broker_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionValidationEvent {\n    pub state: RecordValidationOutcome,",
            "pub struct IngestionValidationEvent {\n    pub broker: String,\n    pub state: RecordValidationOutcome,",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_event_struct(self.config, mutated)
        self.assertIn("broker", str(ctx.exception))

    def test_struct_rejects_leaked_tick_id_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionValidationEvent {\n    pub state: RecordValidationOutcome,",
            "pub struct IngestionValidationEvent {\n    pub tick_id: u64,\n    pub state: RecordValidationOutcome,",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_event_struct(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))

    def test_struct_rejects_leaked_sharadar_table_field(self) -> None:
        mutated = self.types_src.replace(
            "pub struct IngestionValidationEvent {\n    pub state: RecordValidationOutcome,",
            "pub struct IngestionValidationEvent {\n    pub sharadar_table: String,\n    pub state: RecordValidationOutcome,",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_event_struct(self.config, mutated)
        self.assertIn("sharadar_table", str(ctx.exception))

    def test_missing_observed_at_seconds_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub observed_at_seconds: u64,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_event_struct(self.config, mutated)
        self.assertIn("observed_at_seconds", str(ctx.exception))

    def test_missing_reason_field_is_caught(self) -> None:
        mutated = self.types_src.replace("pub reason: QuarantineReason,", "", 1)
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_event_struct(self.config, mutated)
        self.assertIn("reason", str(ctx.exception))


class RecordValidatorPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_port_exposes_validate(self) -> None:
        evidence = check_record_validator_port(self.config, self.data_src)
        self.assertIn("validate", evidence)

    def test_missing_validate_method_is_caught(self) -> None:
        mutated = self.data_src.replace(
            "fn validate(",
            "fn dropped_validate(",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_record_validator_port(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))


class IngestionValidationEventSinkPortTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_port_exposes_record(self) -> None:
        evidence = check_event_sink_port(self.config, self.data_src)
        self.assertIn("record", evidence)

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.data_src.replace(
            "fn record(&self, event: IngestionValidationEvent)",
            "fn dropped_record_method(&self, event: IngestionValidationEvent)",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))


class IngestionValidationGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_acceptance_is_gated_on_valid_leaf(self) -> None:
        evidence = check_ingestion_validation_guard(self.config, self.data_src)
        self.assertIn("RecordValidationOutcome::Valid", evidence)
        self.assertIn("RecordValidationOutcome::Quarantined", evidence)
        self.assertIn("OrderErrorCategory::IngestionRecordValidationFailed", evidence)
        self.assertIn("events.record", evidence)
        self.assertIn("writes nothing to the primary storage tier", evidence)

    def test_missing_events_record_call_is_caught(self) -> None:
        # Strip the `events.record(IngestionValidationEvent { ... });`
        # block so the remaining source still parses.
        marker_open = "events.record(IngestionValidationEvent {"
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
        mutated = self.data_src[:start] + "/* event record removed */" + self.data_src[end:]
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_guard(self.config, mutated)
        self.assertIn("events.record", str(ctx.exception))

    def test_primary_write_in_quarantined_leaf_is_caught(self) -> None:
        # Inject a primary.insert(...) call into the Quarantined leaf —
        # the regression the forbidden_mutations list exists to catch.
        mutated = self.data_src.replace(
            "events.record(IngestionValidationEvent {",
            "primary.insert(); events.record(IngestionValidationEvent {",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_guard(self.config, mutated)
        self.assertIn("primary.insert", str(ctx.exception))

    def test_self_write_primary_in_quarantined_leaf_is_caught(self) -> None:
        # A second forbidden-mutation regression — the self.write_primary
        # form is a particularly insidious one because it's grammatically
        # valid Rust at the bare method level.
        mutated = self.data_src.replace(
            "events.record(IngestionValidationEvent {",
            "self.write_primary(); events.record(IngestionValidationEvent {",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_guard(self.config, mutated)
        self.assertIn("self.write_primary", str(ctx.exception))

    def test_acceptance_in_quarantined_leaf_is_caught(self) -> None:
        # Inject an IngestionAccepted { ... } construction inside the
        # Quarantined leaf — the regression the "Quarantined must not
        # produce IngestionAccepted" rule exists to catch.
        mutated = self.data_src.replace(
            "events.record(IngestionValidationEvent {",
            "let _smuggled = IngestionAccepted { source: record.source.clone(), record_hash: record.record_hash.clone() }; events.record(IngestionValidationEvent {",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_guard(self.config, mutated)
        self.assertIn("zero acceptance side effect", str(ctx.exception))

    def test_missing_validate_call_is_caught(self) -> None:
        # Rewrite the match scrutinee so the gate stops calling the
        # validator — the regression the validator-call check exists to
        # catch.
        mutated = self.data_src.replace(
            "match validator.validate(record)",
            "match never_called(record)",
            1,
        )
        with self.assertRaises(IngestionValidationCheckError) as ctx:
            check_ingestion_validation_guard(self.config, mutated)
        self.assertIn("validator.validate", str(ctx.exception))


class IngestionValidationWireStringTest(unittest.TestCase):
    """Wire-string drift is caught by the existing atp-types unit test
    `order_error_category_wire_strings_track_syrs_sys_64`. This test
    exercises the cross-crate linkage by spot-checking that the
    `OrderErrorCategory::IngestionRecordValidationFailed` variant is
    reachable from the atp-data crate's source (which the
    `ingestion_validation_guard` static check pins through the factory
    call)."""

    def setUp(self) -> None:
        self.config = load_config()
        self.data_src = data_source(self.config)

    def test_data_crate_references_canonical_wire_string_source(self) -> None:
        self.assertIn(
            "OrderErrorCategory::IngestionRecordValidationFailed",
            self.data_src,
            "atp-data must reference the canonical wire-string variant",
        )


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_eight_evidence_items(self) -> None:
        evidence = run_checks()
        # 7 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(evidence), 8)

    def test_assert_ingestion_validation_static_emits_seven_evidence_items(self) -> None:
        config = load_config()
        evidence = assert_ingestion_validation_static(config, ROOT)
        self.assertEqual(len(evidence), 7)


if __name__ == "__main__":
    unittest.main()
