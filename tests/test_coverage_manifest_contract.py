"""SRS-DATA-011 corporate-action coverage — L3 contract test.

Drives ``tools/coverage_manifest_check.py`` (asserting the PASS banner + evidence needles), then imports
each static guard and injects a regression to prove the guard is non-vacuous (it would catch the inverse
of the property it claims). The cargo round-trip is exercised by the script run; the static guards run
import-free.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from coverage_manifest_check import (  # noqa: E402
    CoverageManifestCheckError,
    _read,
    assert_coverage_manifest_static,
    check_cli_routes_gated,
    check_coverage_cli,
    check_coverage_kind,
    check_data_layer_rejects_coverage,
    check_gate_condition,
    check_ingest_excludes_coverage,
    check_kind_narrowed_gate,
    check_single_public_entry,
    contract_block,
    load_config,
)


class ScriptRunTest(unittest.TestCase):
    def test_script_passes_with_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/coverage_manifest_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-011 CORPORATE-ACTION COVERAGE PASS", result.stdout)
        for needle in (
            "coverage kind",
            "gate condition",
            "D >= query.end_ts",
            "kind-narrowed gate",
            "single public entry",
            "CLI routing",
            "coverage CLI",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def _src(self, key: str) -> str:
        return _read(self.config, key, ROOT)

    def _mutate(self, key: str, old: str, new: str) -> str:
        mutated = self._src(key).replace(old, new)
        self.assertNotEqual(mutated, self._src(key), f"mutation no-op for {key}: {old!r}")
        return mutated


class CoverageKindTest(_Fixture):
    def test_dropped_variant_is_caught(self) -> None:
        mutated = self._mutate("store_source", "CorporateActionCoverage", "RenamedCoverageKind")
        with self.assertRaises(CoverageManifestCheckError):
            check_coverage_kind(self.config, mutated)

    def test_unbumped_schema_version_is_caught(self) -> None:
        # If SCHEMA_VERSION were not bumped to 3, a v1/v2 reader could not reject a coverage-bearing
        # store at the version gate -> the guard must fire.
        mutated = self._mutate(
            "store_source",
            "pub const SCHEMA_VERSION: i64 = 3;",
            "pub const SCHEMA_VERSION: i64 = 2;",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_coverage_kind(self.config, mutated)

    def test_dropped_coverage_field_validation_is_caught(self) -> None:
        # If validate_record stopped enforcing complete_through == event_ts, a forged frontier via the
        # public MarketDataRecord::new could grant the gate untrusted coverage -> the guard must fire.
        mutated = self._mutate(
            "store_source",
            "field.value_minor == record.key.event_ts",
            "field.value_minor >= 0",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_coverage_kind(self.config, mutated)


class GateConditionTest(_Fixture):
    def test_loosened_gate_to_start_is_caught(self) -> None:
        # Gating on start_ts instead of end_ts would leave bars near the end under-adjusted -> must fire.
        mutated = self._mutate("coverage_module", "d >= query.end_ts", "d >= query.start_ts")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_condition(self.config, mutated)

    def test_dropped_not_covered_is_caught(self) -> None:
        mutated = self._mutate("coverage_module", "NotCovered", "Permitted")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_condition(self.config, mutated)

    def test_dropped_as_of_d_split_bound_is_caught(self) -> None:
        # Removing the `event_ts <= coverage_through` bound would let a split beyond the frontier adjust
        # the series past the advertised coverage_through (the as-of-D contract break Codex flagged).
        mutated = self._mutate("coverage_module", "key.event_ts <= coverage_through", "true")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_condition(self.config, mutated)


class KindNarrowedGateTest(_Fixture):
    def test_dropped_kind_guard_is_caught(self) -> None:
        mutated = self._mutate("coverage_module", "UnsupportedQueryKind", "RenamedKindError")
        with self.assertRaises(CoverageManifestCheckError):
            check_kind_narrowed_gate(self.config, mutated)


class SinglePublicEntryTest(_Fixture):
    def test_pub_mod_normalization_is_caught(self) -> None:
        # Exposing the split math publicly would create a raw-as-adjusted path bypassing the gate.
        mutated = self._mutate("lib_source", "mod normalization;", "pub mod normalization;")
        with self.assertRaises(CoverageManifestCheckError):
            check_single_public_entry(self.config, mutated)

    def test_unexposed_coverage_module_is_caught(self) -> None:
        mutated = self._mutate("lib_source", "pub mod coverage;", "mod coverage;")
        with self.assertRaises(CoverageManifestCheckError):
            check_single_public_entry(self.config, mutated)


class CliRoutesGatedTest(_Fixture):
    def test_dropped_gate_call_is_caught(self) -> None:
        mutated = self._mutate("cli_source", "query_split_adjusted", "query_unscaled")
        with self.assertRaises(CoverageManifestCheckError):
            check_cli_routes_gated(self.config, mutated)

    def test_dropped_coverage_through_echo_is_caught(self) -> None:
        mutated = self._mutate("cli_source", "coverage_through", "as_of_marker")
        with self.assertRaises(CoverageManifestCheckError):
            check_cli_routes_gated(self.config, mutated)


class CoverageCliTest(_Fixture):
    def test_dropped_lock_is_caught(self) -> None:
        # assert-coverage must hold the StoreLock across the load-modify-save.
        mutated = self._mutate("coverage_cli_source", "StoreLock::acquire", "no_lock_acquire")
        with self.assertRaises(CoverageManifestCheckError):
            check_coverage_cli(self.config, mutated)

    def test_dropped_subcommand_is_caught(self) -> None:
        mutated = self._mutate("coverage_cli_source", '"assert-coverage"', '"set-cov"')
        with self.assertRaises(CoverageManifestCheckError):
            check_coverage_cli(self.config, mutated)


class IngestExcludesCoverageTest(_Fixture):
    def test_data016_accepting_coverage_is_caught(self) -> None:
        # If data016_ingest_cli stopped rejecting the coverage kind, its fixture path would be a second
        # untracked route to grant trusted coverage -> the guard must fire.
        mutated = self._mutate(
            "ingest_cli_source",
            "if kind == DatasetKind::CorporateActionCoverage {",
            "if false {",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_ingest_excludes_coverage(self.config, mutated)


class DataLayerRejectsCoverageTest(_Fixture):
    def test_ingest_market_record_accepting_coverage_is_caught(self) -> None:
        # If DataLayer::ingest_market_record stopped rejecting coverage, a generic ingest path could
        # mint a trusted frontier (the decisive boundary) -> the guard must fire.
        mutated = self._mutate(
            "lib_source",
            "if record.key().kind == DatasetKind::CorporateActionCoverage {",
            "if false {",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_data_layer_rejects_coverage(self.config, mutated)


class AggregateEvidenceTest(_Fixture):
    def test_static_check_count_is_pinned(self) -> None:
        # Eight static guards (coverage kind, gate condition, kind-narrowed gate, single public entry,
        # CLI routing, coverage CLI, ingest-excludes-coverage [data016 CLI], data-layer-rejects-coverage
        # [ingest_market_record]). A dropped or silently-added guard changes this count — pin it.
        self.assertEqual(len(assert_coverage_manifest_static(self.config, ROOT)), 8)

    def test_block_stays_passes_false_and_names_owners(self) -> None:
        block = contract_block(self.config)
        # SRS-DATA-011 is foundational substrate this session, not a feature close.
        self.assertFalse(block["passes"])
        self.assertEqual(block["requirement"], "SRS-DATA-011")
        self.assertEqual(block["schema_version"], 3)
        # The deferred legs that keep it passes:false are named.
        deferred = " ".join(block["deferred"]).lower()
        for owner in ("dividend", "delisting", "merger", "symbol-change"):
            self.assertIn(owner, deferred, f"deferred owners must name {owner!r}")


if __name__ == "__main__":
    unittest.main()
