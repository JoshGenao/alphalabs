"""Contract tests for SRS-PERF-001 (latency-percentile verification substrate).

SRS-PERF-001 / SyRS §5.1 NFR-P1 / NFR-P4 / NFR-P5 / NFR-P6 / NFR-P9 / NFR-P10 +
SRS-MD-001 fan-out latency / StRS SN-1.01 / SN-2.03.

Mirrors ``tests/test_sequence_gap_contract.py``: shells out to
``tools/perf_measurement_check.py``, then exercises each per-check function
in-process, including negative spot-checks that mutate the Rust source / SyRS
text / metadata in memory and assert the contract actually catches the
regression (a wrong percentile, a renamed NFR id, a dropped fail-closed guard, a
budget that no longer matches the SyRS §5.1 condition, a boundary phrase that
drifts from the SyRS row, a leaked vendor token, a missing offset render).
"""

from __future__ import annotations

import copy
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from perf_measurement_check import (  # noqa: E402
    PerfMeasurementCheckError,
    check_artifact_documents_offset,
    check_artifact_fail_closed,
    check_boundary_matches_syrs,
    check_clock_discipline,
    check_feature_stays_false,
    check_multi_leg_verification,
    check_nfr_catalog,
    check_percentile_set,
    check_rust_thresholds_match_metadata,
    check_stated_percentiles,
    check_thresholds_match_syrs,
    check_vendor_isolation,
    load_config,
    order_event_source,
    perf_source,
    srs_doc_text,
    syrs_text,
    types_lib_source,
)


class PerfMeasurementScriptTest(unittest.TestCase):
    def test_srs_perf_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/perf_measurement_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-PERF-001 PERF-MEASUREMENT PASS", result.stdout)
        for needle in (
            "reported percentiles are exactly p50/p95/p99/p99.9",
            "LatencyNfr catalog covers the 7 AC NFRs",
            "PtpClockDiscipline documents an offset bound",
            "unknown leg, empty/inverted window, window-duration overflow",
            "Display documents max clock offset, the window, and all four percentiles",
            "requires every leg of a multi-leg NFR",
            "with simultaneous windows for NFR-P10",
            "NFR budgets match their SRS/SyRS measurement conditions",
            "boundary phrases match perf.rs and their SRS/SyRS conditions",
            "reuse the authoritative constants",
            "keeps SRS-PERF-001 passes:false",
            "cargo integration test srs_perf_001_latency_percentiles passes",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.perf_src = perf_source(self.config)
        self.order_event_src = order_event_source(self.config)
        self.lib_src = types_lib_source(self.config)
        self.syrs = syrs_text(self.config)
        self.srs = srs_doc_text(self.config)


class PercentileSetTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("p50/p95/p99/p99.9", check_percentile_set(self.config, self.perf_src))

    def test_wrong_per_mille_is_caught(self) -> None:
        mutated = self.perf_src.replace("Self::P999 => 999,", "Self::P999 => 990,", 1)
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_percentile_set(self.config, mutated)
        self.assertIn("p99.9", str(ctx.exception))

    def test_reordered_reported_set_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            "Percentile::P50,\n    Percentile::P95,", "Percentile::P95,\n    Percentile::P50,", 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_percentile_set(self.config, mutated)
        self.assertIn("REPORTED_PERCENTILES", str(ctx.exception))


class NfrCatalogTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("7 AC NFRs", check_nfr_catalog(self.config, self.perf_src))

    def test_renamed_nfr_id_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            'Self::OrderSignalToAck => "NFR-P1",', 'Self::OrderSignalToAck => "NFR-PX",', 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_nfr_catalog(self.config, mutated)
        self.assertIn("NFR-P1", str(ctx.exception))

    def test_dropped_catalog_variant_is_caught(self) -> None:
        mutated = self.perf_src.replace("    LatencyNfr::SubscriptionFanout,\n", "", 1)
        with self.assertRaises(PerfMeasurementCheckError):
            check_nfr_catalog(self.config, mutated)


class StatedPercentileTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("per-leg", check_stated_percentiles(self.config, self.perf_src))

    def test_wrong_stated_percentile_is_caught(self) -> None:
        mutated = self.perf_src.replace("Some(Percentile::P95)", "Some(Percentile::P50)", 1)
        with self.assertRaises(PerfMeasurementCheckError):
            check_stated_percentiles(self.config, mutated)

    def test_dashboard_refresh_leg_cannot_inherit_p95(self) -> None:
        # NFR-P10's dashboard-refresh leg is a flat <=5s max, NOT a p95 budget.
        # Flipping it to p95 must be caught (the exact Codex round-5 finding).
        idx = self.perf_src.index('label: "dashboard_refresh"')
        mutated = self.perf_src[:idx] + self.perf_src[idx:].replace(
            "stated_percentile: None", "stated_percentile: Some(Percentile::P95)", 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_stated_percentiles(self.config, mutated)
        self.assertIn("dashboard_refresh", str(ctx.exception))

    def test_missing_leg_stated_percentile_field_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            "stated_percentile: Option<Percentile>", "sp: Option<Percentile>"
        )
        with self.assertRaises(PerfMeasurementCheckError):
            check_stated_percentiles(self.config, mutated)


class ClockDisciplineTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("offset bound", check_clock_discipline(self.config, self.perf_src))

    def test_dropped_offset_field_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            "Disciplined { max_offset_ns: u64 }", "Disciplined { renamed_offset: u64 }", 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_clock_discipline(self.config, mutated)
        self.assertIn("max_offset_ns", str(ctx.exception))


class ArtifactFailClosedTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("fails closed", check_artifact_fail_closed(self.config, self.perf_src))

    def test_removed_clock_guard_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            ".ok_or(PerfMeasurementError::ClockNotDisciplined)?", ".unwrap_or(0)", 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_artifact_fail_closed(self.config, mutated)
        self.assertIn("non-disciplined clock", str(ctx.exception))

    def test_removed_window_guard_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            "PerfMeasurementError::EmptyMeasurementWindow", "PerfMeasurementError::NoSamples"
        )
        with self.assertRaises(PerfMeasurementCheckError):
            check_artifact_fail_closed(self.config, mutated)

    def test_removed_overflow_guard_is_caught(self) -> None:
        # Drop the checked_sub overflow guard → the window duration could overflow.
        mutated = self.perf_src.replace(".checked_sub(", ".UNCHECKED_sub(", 1)
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_artifact_fail_closed(self.config, mutated)
        self.assertIn("overflow", str(ctx.exception))


class ArtifactDocumentsOffsetTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn(
            "all four percentiles", check_artifact_documents_offset(self.config, self.perf_src)
        )

    def test_missing_offset_render_is_caught(self) -> None:
        mutated = self.perf_src.replace("max clock offset", "max offset")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_artifact_documents_offset(self.config, mutated)
        self.assertIn("max clock offset", str(ctx.exception))

    def test_non_iterated_percentiles_are_caught(self) -> None:
        mutated = self.perf_src.replace(
            "for p in REPORTED_PERCENTILES", "for p in [Percentile::P50]", 1
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_artifact_documents_offset(self.config, mutated)
        self.assertIn("REPORTED_PERCENTILES", str(ctx.exception))


class MultiLegVerificationTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn(
            "requires every leg", check_multi_leg_verification(self.config, self.perf_src)
        )

    def test_missing_leg_field_is_caught(self) -> None:
        mutated = self.perf_src.replace("threshold_label: &'static str", "unlabelled: &'static str")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_multi_leg_verification(self.config, mutated)
        self.assertIn("threshold_label", str(ctx.exception))

    def test_dropped_completeness_guard_is_caught(self) -> None:
        # Remove the incompleteness rejection from NfrVerification::assemble → a
        # bundle could certify an NFR while missing a leg.
        mutated = self.perf_src.replace("IncompleteNfrVerification", "OkAnyway")
        with self.assertRaises(PerfMeasurementCheckError):
            check_multi_leg_verification(self.config, mutated)

    def test_multi_leg_labels_must_be_distinct(self) -> None:
        mutated = copy.deepcopy(self.config)
        p4 = next(n for n in mutated["perf_measurement_contract"]["nfrs"] if n["id"] == "NFR-P4")
        p4["thresholds"][1]["label"] = "live"  # duplicate the "live" label
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_multi_leg_verification(mutated, self.perf_src)
        self.assertIn("NFR-P4", str(ctx.exception))

    def test_dropped_simultaneity_guard_is_caught(self) -> None:
        # Removing the window-overlap enforcement from assemble → NFR-P10 legs
        # from disjoint runs could be certified as simultaneous.
        mutated = self.perf_src.replace("nfr.requires_simultaneous_legs()", "false", 1)
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_multi_leg_verification(self.config, mutated)
        self.assertIn("simultaneity", str(ctx.exception).lower())

    def test_metadata_simultaneous_not_required_in_rust_is_caught(self) -> None:
        # Marking a non-simultaneous NFR simultaneous in metadata without the Rust
        # discriminator agreeing must be caught (drift guard).
        mutated = copy.deepcopy(self.config)
        p4 = next(n for n in mutated["perf_measurement_contract"]["nfrs"] if n["id"] == "NFR-P4")
        p4["simultaneous_legs"] = True
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_multi_leg_verification(mutated, self.perf_src)
        self.assertIn("NFR-P4", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.perf_src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.perf_src + "\n// stray reqMktData reference\n"
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("reqMktData", str(ctx.exception))


class ThresholdsMatchSyrsTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn(
            "NFR budgets match their SRS/SyRS",
            check_thresholds_match_syrs(self.config, self.syrs, self.srs),
        )

    def test_syrs_budget_not_matching_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["perf_measurement_contract"]["nfrs"][0]["thresholds"][0]["bound_ms"] = 999
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_thresholds_match_syrs(mutated, self.syrs, self.srs)
        self.assertIn("NFR-P1", str(ctx.exception))

    def test_fanout_budget_not_matching_srs_is_caught(self) -> None:
        # The SRS-MD-001 100 ms fan-out budget lives in docs/SRS.md (prose); a
        # metadata bound that no longer matches must be caught.
        mutated = copy.deepcopy(self.config)
        fanout = next(
            n for n in mutated["perf_measurement_contract"]["nfrs"] if n["id"] == "SRS-MD-001"
        )
        fanout["thresholds"][0]["bound_ms"] = 999
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_thresholds_match_syrs(mutated, self.syrs, self.srs)
        self.assertIn("999 ms", str(ctx.exception))

    def test_nfr_p10_missing_dashboard_leg_is_caught(self) -> None:
        # NFR-P10 must carry BOTH the order-latency and dashboard-refresh legs;
        # dropping either fails the exact SyRS match (the requirement is a
        # simultaneity property of NFR-P1 and NFR-P2).
        mutated = copy.deepcopy(self.config)
        p10 = next(n for n in mutated["perf_measurement_contract"]["nfrs"] if n["id"] == "NFR-P10")
        p10["thresholds"] = [t for t in p10["thresholds"] if t["label"] != "dashboard_refresh"]
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_thresholds_match_syrs(mutated, self.syrs, self.srs)
        self.assertIn("NFR-P10", str(ctx.exception))

    def test_fanout_budget_absent_from_srs_is_caught(self) -> None:
        # Drop the 100 ms budget from the SRS-MD-001 row → the fan-out budget no
        # longer traces to its SRS measurement condition.
        mutated_srs = self.srs.replace("no more than 100 ms", "with low")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_thresholds_match_syrs(self.config, self.syrs, mutated_srs)
        self.assertIn("SRS-MD-001", str(ctx.exception))


class BoundaryMatchesSyrsTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn(
            "boundary phrases match",
            check_boundary_matches_syrs(self.config, self.perf_src, self.syrs, self.srs),
        )

    def test_phrase_missing_from_perf_boundary_is_caught(self) -> None:
        # Remove the phrase everywhere (it also appears in a constant doc) so the
        # boundary() arm truly loses it.
        mutated_perf = self.perf_src.replace("email and SMS", "e-mail/text")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_boundary_matches_syrs(self.config, mutated_perf, self.syrs, self.srs)
        self.assertIn("email and SMS", str(ctx.exception))

    def test_phrase_absent_from_syrs_row_is_caught(self) -> None:
        # Phrase still in perf.rs but no longer in the SyRS NFR-P6 row → the
        # boundary drifted from the SyRS condition.
        mutated_syrs = self.syrs.replace("email and SMS", "a channel")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_boundary_matches_syrs(self.config, self.perf_src, mutated_syrs, self.srs)
        self.assertIn("spec row", str(ctx.exception))

    def test_fanout_phrase_absent_from_srs_row_is_caught(self) -> None:
        # The SRS-MD-001 fan-out boundary is validated against docs/SRS.md.
        mutated_srs = self.srs.replace("fan-out", "broadcast")
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_boundary_matches_syrs(self.config, self.perf_src, self.syrs, mutated_srs)
        self.assertIn("spec row", str(ctx.exception))


class RustThresholdsMatchMetadataTest(_Fixture):
    def test_passes(self) -> None:
        evidence = check_rust_thresholds_match_metadata(
            self.config, self.perf_src, self.order_event_src, self.lib_src
        )
        self.assertIn("reuse the authoritative constants", evidence)

    def test_mutated_defined_constant_is_caught(self) -> None:
        mutated = self.perf_src.replace(
            "ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS: u64 = 1_000",
            "ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS: u64 = 2_000",
            1,
        )
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_rust_thresholds_match_metadata(
                self.config, mutated, self.order_event_src, self.lib_src
            )
        self.assertIn("ORDER_SIGNAL_TO_ACK_LATENCY_P95_MS", str(ctx.exception))

    def test_dropped_constant_reuse_is_caught(self) -> None:
        mutated = self.perf_src.replace("LIVE_CALLBACK_LATENCY_P95_MS as u64", "1000u64")
        with self.assertRaises(PerfMeasurementCheckError):
            check_rust_thresholds_match_metadata(
                self.config, mutated, self.order_event_src, self.lib_src
            )


class FeatureStaysFalseTest(_Fixture):
    def test_passes(self) -> None:
        self.assertIn("passes:false", check_feature_stays_false(self.config))

    def test_missing_deferred_owners_is_caught(self) -> None:
        mutated = copy.deepcopy(self.config)
        mutated["perf_measurement_contract"].pop("deferred", None)
        with self.assertRaises(PerfMeasurementCheckError) as ctx:
            check_feature_stays_false(mutated)
        self.assertIn("deferred", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
