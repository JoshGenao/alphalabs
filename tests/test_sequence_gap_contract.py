"""Contract tests for SRS-MD-007 (tick-sequence gap detection + staleness).

SRS-MD-007 / SyRS SYS-39 / SYS-39a / SYS-70 / NFR-P5 / StRS SN-2.03 / SN-2.04.

Mirrors ``tests/test_subscription_fanout_contract.py``: shells out to
``tools/sequence_gap_check.py``, then exercises each per-check function
in-process, including negative spot-checks that mutate the Rust source in
memory and assert the contract actually catches the regression (dropped
acceptance field, leaked vendor field, a freshness default that no longer
fails closed, a removed gap publish/stale-marking, a dropped enum variant, a
removed fail-closed canonicalization, a leaked vendor token).
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

from sequence_gap_check import (  # noqa: E402
    SequenceGapCheckError,
    assert_sequence_gap_static,
    check_detector_struct,
    check_freshness_fail_closed,
    check_gap_event_sink_port,
    check_gap_observation_enum,
    check_log_event_pinned,
    check_observe_tick_semantics,
    check_resync_outcome_enum,
    check_sequence_gap_event,
    check_vendor_isolation,
    load_config,
    market_data_source,
    run_checks,
    types_source,
)


class SequenceGapScriptTest(unittest.TestCase):
    def test_srs_md_007_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sequence_gap_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-MD-007 SEQUENCE-GAP PASS", result.stdout)
        for needle in (
            "SequenceGapEvent with the 4 SRS-MD-007 acceptance fields",
            "symbol, expected_sequence, observed_sequence, observed_at_ns",
            "reuses MarketDataFreshness (Fresh, Stale)",
            "SequenceGapEventSink port with 1 fallible method",
            "SequenceGapDetector with the 4 sequence/staleness methods",
            "observe_tick canonicalizes + fails closed",
            "freshness fails closed (unobserved security -> Stale)",
            "GapObservation with 4 outcomes",
            "ResyncOutcome with 2 outcomes",
            "SEQUENCE_GAP log event type pinned under 'market_data'",
            "keeps SRS-MD-007 passes:false",
            "cargo integration test srs_md_007_sequence_gap passes",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.types_src = types_source(self.config)
        self.md_src = market_data_source(self.config)


class SequenceGapEventTest(_Fixture):
    def test_required_fields_present(self) -> None:
        evidence = check_sequence_gap_event(self.config, self.types_src)
        self.assertIn("expected_sequence, observed_sequence, observed_at_ns", evidence)

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.types_src.replace("    pub observed_sequence: u64,", "", 1)
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_sequence_gap_event(self.config, mutated)
        self.assertIn("observed_sequence", str(ctx.exception))

    def test_leaked_tick_id_field_is_caught(self) -> None:
        mutated = self.types_src.replace(
            "pub struct SequenceGapEvent {\n    pub symbol: String,",
            "pub struct SequenceGapEvent {\n    pub tick_id: u64,\n    pub symbol: String,",
            1,
        )
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_sequence_gap_event(self.config, mutated)
        self.assertIn("tick_id", str(ctx.exception))


class GapEventSinkPortTest(_Fixture):
    def test_record_method_present(self) -> None:
        self.assertIn("record", check_gap_event_sink_port(self.config, self.md_src))

    def test_missing_record_method_is_caught(self) -> None:
        mutated = self.md_src.replace(
            "fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError>;",
            "fn dropped(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError>;",
            1,
        )
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_gap_event_sink_port(self.config, mutated)
        self.assertIn("record", str(ctx.exception))

    def test_infallible_record_is_caught(self) -> None:
        # Reverting the sink to infallible (dropping the Result) must be caught:
        # a swallowed SRS-LOG-001 / dashboard failure is the round-2/3 finding.
        mutated = self.md_src.replace(
            "fn record(&self, event: SequenceGapEvent) -> Result<(), SequenceGapPublishError>;",
            "fn record(&self, event: SequenceGapEvent);",
            1,
        )
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_gap_event_sink_port(self.config, mutated)
        self.assertIn("fallible", str(ctx.exception))


class DetectorStructTest(_Fixture):
    def test_methods_present(self) -> None:
        evidence = check_detector_struct(self.config, self.md_src)
        for method in ("observe_tick", "acknowledge_resync", "freshness", "stale_since_ns"):
            self.assertIn(method, evidence)

    def test_dropped_resync_method_is_caught(self) -> None:
        mutated = self.md_src.replace("pub fn acknowledge_resync(", "pub fn dropped_resync(")
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_detector_struct(self.config, mutated)
        self.assertIn("acknowledge_resync", str(ctx.exception))


class ObserveTickSemanticsTest(_Fixture):
    def test_semantics_evidence(self) -> None:
        evidence = check_observe_tick_semantics(self.config, self.md_src)
        self.assertIn("marks Stale", evidence)

    def test_removed_fail_closed_canonicalize_is_caught(self) -> None:
        # Anchor on the observe_tick-specific two-line prologue so the mutation
        # targets observe_tick and not the registry's identical fan_out line.
        mutated = self.md_src.replace(
            "let key = tick.security_key()?;\n        let observed = tick.tick_seq;",
            "let key = tick.security_key().unwrap();\n        let observed = tick.tick_seq;",
            1,
        )
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_observe_tick_semantics(self.config, mutated)
        self.assertIn("security_key", str(ctx.exception))

    def test_removed_gap_publish_is_caught(self) -> None:
        mutated = self.md_src.replace("events.record(SequenceGapEvent {", "/* no publish */ (", 1)
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_observe_tick_semantics(self.config, mutated)
        self.assertIn("SequenceGapEvent", str(ctx.exception))

    def test_removed_stale_onset_guard_is_caught(self) -> None:
        # Dropping the `if was_fresh {` guard (resetting stale_since_ns on every
        # gap) is the round-3 stale-age-underreporting regression.
        mutated = self.md_src.replace("if was_fresh {", "if true {", 1)
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_observe_tick_semantics(self.config, mutated)
        self.assertIn("stale_since_ns", str(ctx.exception))


class FreshnessFailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        self.assertIn("fails closed", check_freshness_fail_closed(self.config, self.md_src))

    def test_fresh_default_breaks_fail_closed_and_is_caught(self) -> None:
        # Flip the unobserved-security default to Fresh — an order on a silent
        # line would no longer be blocked. The check must reject it.
        mutated = self.md_src.replace(
            ".map_or(MarketDataFreshness::Stale, |state| state.freshness)",
            ".map_or(MarketDataFreshness::Fresh, |state| state.freshness)",
            1,
        )
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_freshness_fail_closed(self.config, mutated)
        self.assertIn("fail CLOSED", str(ctx.exception))


class GapObservationEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_gap_observation_enum(self.config, self.md_src)
        for variant in ("Baseline", "InSequence", "Gap", "NonMonotonic"):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.md_src.replace("    NonMonotonic { last: u64, observed: u64 },", "", 1)
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_gap_observation_enum(self.config, mutated)
        self.assertIn("NonMonotonic", str(ctx.exception))


class ResyncOutcomeEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_resync_outcome_enum(self.config, self.md_src)
        self.assertIn("Acknowledged", evidence)
        self.assertIn("NotTracked", evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.md_src.replace("    NotTracked,", "", 1)
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_resync_outcome_enum(self.config, mutated)
        self.assertIn("NotTracked", str(ctx.exception))


class LogEventPinnedTest(_Fixture):
    def test_pinned_in_json_and_python(self) -> None:
        evidence = check_log_event_pinned(self.config)
        self.assertIn("SEQUENCE_GAP", evidence)

    def test_missing_json_pin_is_caught(self) -> None:
        mutated = dict(self.config)
        log_block = dict(mutated["log_record_contract"])
        types = dict(log_block["event_types_by_source"])
        types["market_data"] = ["SUBSCRIPTION_CHANGE"]  # drop SEQUENCE_GAP
        log_block["event_types_by_source"] = types
        mutated["log_record_contract"] = log_block
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_log_event_pinned(mutated)
        self.assertIn("SEQUENCE_GAP", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.md_src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.md_src + "\n// uses interactive_brokers under the hood\n"
        with self.assertRaises(SequenceGapCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("interactive_brokers", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_static_evidence_is_eleven_items(self) -> None:
        self.assertEqual(len(assert_sequence_gap_static(load_config(), ROOT)), 11)

    def test_run_checks_appends_cargo_smoke(self) -> None:
        # 11 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 12)


if __name__ == "__main__":
    unittest.main()
