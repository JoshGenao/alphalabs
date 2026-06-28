"""SRS-DATA-012 split-adjusted normalization — L3 contract test.

Drives ``tools/normalization_modes_check.py`` (asserting the PASS banner + evidence needles), then
imports each static guard and injects a regression to prove the guard is non-vacuous (it would catch
the inverse of the property it claims). The cargo round-trip is exercised by the script run; the static
guards run import-free.
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

from normalization_modes_check import (  # noqa: E402
    NormalizationModesCheckError,
    _read,
    assert_normalization_modes_static,
    check_binding_serves_split_adjusted,
    check_cli_flag,
    check_not_publicly_exported,
    check_ohlc_and_volume_factors,
    check_rust_math,
    check_split_kind,
    contract_block,
    load_config,
)


class ScriptRunTest(unittest.TestCase):
    def test_script_passes_with_evidence(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/normalization_modes_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-012 SPLIT-ADJUSTED NORMALIZATION PASS", result.stdout)
        for needle in (
            "split kind",
            "money math",
            "compose-then-divide",
            "round-half-to-even",
            "CLI surface",
            "coverage-enforcing gate",
            "serves RAW and the gated SPLIT_ADJUSTED",
            "gate-integrity",
            "crate-internal API",
            "generative property test",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()

    def _src(self, key: str) -> str:
        return _read(self.config, key, ROOT)


class SplitKindTest(_Fixture):
    def test_dropped_variant_is_caught(self) -> None:
        mutated = self._src("store_source").replace("CorporateActionSplit", "RenamedKind")
        with self.assertRaises(NormalizationModesCheckError):
            check_split_kind(self.config, mutated)


class RustMathTest(_Fixture):
    def test_dropped_overflow_guard_is_caught(self) -> None:
        # Remove checked_mul (the fail-closed overflow guard) → the money math is no longer safe.
        mutated = self._src("normalization_module").replace("checked_mul", "wrapping_mul")
        with self.assertRaises(NormalizationModesCheckError):
            check_rust_math(self.config, mutated)

    def test_relaxed_boundary_is_caught(self) -> None:
        # Flipping the strict effective_ts > event_ts boundary to >= would adjust the on-date bar.
        mutated = self._src("normalization_module").replace(
            "effective_ts > event_ts", "effective_ts >= event_ts"
        )
        with self.assertRaises(NormalizationModesCheckError):
            check_rust_math(self.config, mutated)

    def test_dropped_property_test_is_caught(self) -> None:
        # Removing the generative property test must fail the guard (the money math needs generated
        # coverage, not just fixed examples).
        mutated = self._src("normalization_module").replace(
            "property_split_adjustment_invariants", "removed_property_test"
        )
        with self.assertRaises(NormalizationModesCheckError):
            check_rust_math(self.config, mutated)


class FactorsTest(_Fixture):
    def test_dropped_volume_field_is_caught(self) -> None:
        mutated = self._src("normalization_module").replace("VOLUME_FIELD", "RENAMED_FIELD")
        with self.assertRaises(NormalizationModesCheckError):
            check_ohlc_and_volume_factors(self.config, mutated)


class CliFlagTest(_Fixture):
    def test_reverting_split_adjusted_to_parse_reject_is_caught(self) -> None:
        # split-adjusted must be ROUTED through the coverage gate, not rejected at parse. If the CLI
        # reverted to rejecting split-adjusted at parse (the pre-coverage behavior), the guard must fire
        # (the `"split-adjusted" => Ok` routing token disappears).
        mutated = self._src("cli_source").replace(
            '"split-adjusted" => Ok(Normalization::SplitAdjusted),',
            '"split-adjusted" => Err("deferred".to_string()),',
        )
        self.assertNotEqual(mutated, self._src("cli_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_cli_flag(self.config, mutated)

    def test_dropping_the_gate_call_is_caught(self) -> None:
        # If the CLI stopped routing split-adjusted through MarketDataStore::query_split_adjusted (the
        # single gated path), the guard must fire -- there must be no CLI-side split math.
        mutated = self._src("cli_source").replace("query_split_adjusted", "query_unscaled")
        self.assertNotEqual(mutated, self._src("cli_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_cli_flag(self.config, mutated)


class BindingTest(_Fixture):
    def test_dropping_split_adjusted_serving_is_caught(self) -> None:
        # If the binding stopped mapping SPLIT_ADJUSTED to the 'split-adjusted' CLI label (i.e. no longer
        # SERVED it), the guard must fire -- the binding serves the gated split-adjusted series.
        mutated = self._src("binding_source").replace(
            '    NormalizationMode.SPLIT_ADJUSTED: "split-adjusted",\n',
            "",
        )
        self.assertNotEqual(mutated, self._src("binding_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_binding_serves_split_adjusted(self.config, mutated)

    def test_reverting_default_to_raw_is_caught(self) -> None:
        # A RAW default would serve raw bars on the bare-default consumer call where the Protocol
        # promises adjusted -- the guard must fire.
        mutated = self._src("binding_source").replace(
            "normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED",
            "normalization: NormalizationMode = NormalizationMode.RAW",
        )
        self.assertNotEqual(mutated, self._src("binding_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_binding_serves_split_adjusted(self.config, mutated)

    def test_dropping_gate_integrity_is_caught(self) -> None:
        # Gate-integrity: if the binding stopped validating the echoed coverage_through frontier on a
        # split-adjusted response, an un-gated 'adjusted' response could slip through -- the guard fires.
        mutated = self._src("binding_source").replace("coverage_through", "ignored_frontier")
        self.assertNotEqual(mutated, self._src("binding_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_binding_serves_split_adjusted(self.config, mutated)


class NotPubliclyExportedTest(_Fixture):
    def test_pub_mod_is_caught(self) -> None:
        # If lib.rs exposed the module publicly, a Rust consumer could obtain raw-as-adjusted output.
        mutated = self._src("lib_source").replace("mod normalization;", "pub mod normalization;")
        self.assertNotEqual(mutated, self._src("lib_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_not_publicly_exported(self.config, mutated)

    def test_reexport_is_caught(self) -> None:
        mutated = self._src("lib_source").replace(
            "mod normalization;",
            "mod normalization;\npub use crate::normalization::split_adjust_records;",
        )
        with self.assertRaises(NormalizationModesCheckError):
            check_not_publicly_exported(self.config, mutated)


class AggregateEvidenceTest(_Fixture):
    def test_static_check_count_is_pinned(self) -> None:
        # Six static guards (split kind, rust math, factors, CLI flag, binding, not-publicly-exported).
        # A dropped guard or a silently-added one changes this count — pin it like store-history.
        self.assertEqual(len(assert_normalization_modes_static(self.config, ROOT)), 6)

    def test_public_vs_library_modes_are_separated(self) -> None:
        block = contract_block(self.config)
        # split-adjusted is served on the operator CLI AND the strategy binding behind the SRS-DATA-011
        # coverage gate, so it is both a public_request_mode and a binding_request_mode; FULLY_ADJUSTED /
        # TOTAL_RETURN stay deferred from public exposure (dividend data).
        self.assertEqual(block["public_request_modes"], ["RAW", "SPLIT_ADJUSTED"])
        self.assertEqual(block["binding_request_modes"], ["RAW", "SPLIT_ADJUSTED"])
        self.assertEqual(block["core_library_modes"], ["RAW", "SPLIT_ADJUSTED"])
        self.assertIn("FULLY_ADJUSTED", block["deferred_public_modes"])
        self.assertIn("TOTAL_RETURN", block["deferred_public_modes"])
        self.assertNotIn("SPLIT_ADJUSTED", block["deferred_public_modes"])


if __name__ == "__main__":
    unittest.main()
