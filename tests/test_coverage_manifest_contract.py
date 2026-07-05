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
    check_corporate_action_kinds,
    check_coverage_cli,
    check_coverage_kind,
    check_data_layer_rejects_coverage,
    check_gate_applies_dividends,
    check_gate_condition,
    check_ingest_excludes_coverage,
    check_kind_narrowed_gate,
    check_lineage_bounded,
    check_single_public_entry,
    check_terminal_events_surfaced,
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
        # If SCHEMA_VERSION were not bumped to 4, an older reader could not reject a store carrying
        # the corporate-action kinds at the version gate -> the guard must fire.
        mutated = self._mutate(
            "store_source",
            "pub const SCHEMA_VERSION: i64 = 4;",
            "pub const SCHEMA_VERSION: i64 = 3;",
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

    def test_dropped_basis_bound_is_caught(self) -> None:
        # Removing the `event_ts <= adjusted_through` bound would let an event beyond the read's basis
        # adjust the series past the advertised adjusted_through (the as-of contract break Codex
        # flagged for splits, now uniform across all corporate actions).
        mutated = self._mutate("coverage_module", "key.event_ts <= adjusted_through", "true")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_condition(self.config, mutated)

    def test_lookahead_basis_swap_is_caught(self) -> None:
        # If the point-in-time reads adjusted through the FRONTIER instead of the as-of date, a future
        # split/dividend would leak into a historical read (lookahead) -> the guard must fire.
        mutated = self._mutate(
            "coverage_module",
            "AdjustmentBasis::AsOfEnd => query.end_ts",
            "AdjustmentBasis::AsOfEnd => coverage_through",
        )
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


class CorporateActionKindsTest(_Fixture):
    def test_dropped_variant_is_caught(self) -> None:
        mutated = self._mutate("store_source", "CorporateActionDividend", "RenamedDividendKind")
        with self.assertRaises(CoverageManifestCheckError):
            check_corporate_action_kinds(self.config, mutated)

    def test_retagged_codec_is_caught(self) -> None:
        # Reassigning a codec tag would silently corrupt every persisted v4 store on restore.
        mutated = self._mutate(
            "store_source",
            "Self::CorporateActionMerger => 8,",
            "Self::CorporateActionMerger => 18,",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_corporate_action_kinds(self.config, mutated)

    def test_dropped_dividend_amount_validation_is_caught(self) -> None:
        # If validate_record stopped requiring a positive amount_minor, a zero/negative dividend could
        # corrupt the fully-adjusted factor (identity or a price increase) -> the guard must fire.
        mutated = self._mutate(
            "store_source",
            'field.name == "amount_minor" && field.value_minor > 0',
            'field.name == "amount_minor"',
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_corporate_action_kinds(self.config, mutated)

    def test_dropped_self_successor_block_is_caught(self) -> None:
        # If validate_record stopped rejecting successor == own symbol, the trivial lineage self-cycle
        # could enter the store -> the guard must fire.
        mutated = self._mutate(
            "store_source", "successor != record.key.symbol", "!successor.is_empty()"
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_corporate_action_kinds(self.config, mutated)


class GateAppliesDividendsTest(_Fixture):
    def test_dropped_fully_adjusted_read_is_caught(self) -> None:
        mutated = self._mutate("coverage_module", "query_fully_adjusted", "query_dividend_scaled")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_applies_dividends(self.config, mutated)

    def test_dropped_raw_reference_close_resolution_is_caught(self) -> None:
        # The reference close must resolve from the RAW series strictly before the ex-date; dropping
        # the strict bound (e.g. <=) would pick the ex-date bar itself -> the guard must fire.
        mutated = self._mutate("coverage_module", "event_ts < ex_ts", "event_ts <= ex_ts")
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_applies_dividends(self.config, mutated)

    def test_split_only_mode_leaking_dividends_is_caught(self) -> None:
        # The split-adjusted mode must ignore dividend records entirely (mode semantics).
        mutated = self._mutate(
            "coverage_module",
            "AdjustmentMode::SplitOnly => Vec::new()",
            "AdjustmentMode::SplitOnly => todo!()",
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_gate_applies_dividends(self.config, mutated)


class TerminalEventsSurfacedTest(_Fixture):
    def test_dropped_events_field_is_caught(self) -> None:
        mutated = self._mutate(
            "coverage_module", "events: Vec<CorporateActionEvent>", "events_hidden: Vec<()>"
        )
        with self.assertRaises(CoverageManifestCheckError):
            check_terminal_events_surfaced(self.config, mutated)

    def test_dropped_merger_terms_are_caught(self) -> None:
        mutated = self._mutate("coverage_module", "cash_per_share_minor", "cash_leg")
        with self.assertRaises(CoverageManifestCheckError):
            check_terminal_events_surfaced(self.config, mutated)


class LineageBoundedTest(_Fixture):
    def test_dropped_cycle_error_is_caught(self) -> None:
        mutated = self._mutate("coverage_module", "LineageCycle", "LineageLoop")
        with self.assertRaises(CoverageManifestCheckError):
            check_lineage_bounded(self.config, mutated)

    def test_dropped_depth_bound_is_caught(self) -> None:
        mutated = self._mutate("coverage_module", "MAX_LINEAGE_DEPTH", "UNBOUNDED_DEPTH")
        with self.assertRaises(CoverageManifestCheckError):
            check_lineage_bounded(self.config, mutated)


class AggregateEvidenceTest(_Fixture):
    def test_static_check_count_is_pinned(self) -> None:
        # Twelve static guards (coverage kind, gate condition, kind-narrowed gate, single public entry,
        # CLI routing, coverage CLI, ingest-excludes-coverage [data016 CLI], data-layer-rejects-coverage
        # [ingest_market_record], corporate-action kinds, gate-applies-dividends,
        # terminal-events-surfaced, lineage-bounded). A dropped or silently-added guard changes this
        # count — pin it.
        self.assertEqual(len(assert_coverage_manifest_static(self.config, ROOT)), 12)

    def test_block_passes_true_and_names_remaining_owners(self) -> None:
        block = contract_block(self.config)
        # SRS-DATA-011 is CLOSED: all six action types are reflected (splits/reverse-splits and
        # dividends in the served prices; delistings/mergers/symbol-changes as lineage + surfaced
        # events), so the block flips passes:true.
        self.assertTrue(block["passes"])
        self.assertEqual(block["requirement"], "SRS-DATA-011")
        self.assertEqual(block["schema_version"], 4)
        # All six action types are supported; none remain deferred.
        self.assertEqual(
            block["supported_action_types"],
            ["split", "reverse-split", "dividend", "delisting", "merger", "symbol-change"],
        )
        self.assertEqual(block["deferred_action_types"], [])
        # The remaining deferrals are OTHER features' scope, named with their owners.
        deferred = " ".join(block["deferred"]).lower()
        for owner in ("srs-data-012", "srs-data-001", "sys-28b", "srs-data-009"):
            self.assertIn(owner, deferred, f"deferred owners must name {owner!r}")


if __name__ == "__main__":
    unittest.main()
