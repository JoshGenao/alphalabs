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
    check_binding_defers_split_adjusted,
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
            "serves --normalization raw ONLY",
            "serves RAW only",
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
    def test_serving_split_adjusted_is_caught(self) -> None:
        # If the CLI accepted split-adjusted (Ok) instead of rejecting it (Err), the guard must fire --
        # the operator surface must never emit a split-adjusted label without proven coverage.
        mutated = self._src("cli_source").replace(
            '"split-adjusted" => Err(', '"split-adjusted" => Ok(('
        )
        self.assertNotEqual(mutated, self._src("cli_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_cli_flag(self.config, mutated)


class BindingTest(_Fixture):
    def test_serving_split_adjusted_is_caught(self) -> None:
        # If the binding mapped SPLIT_ADJUSTED to a CLI label (i.e. SERVED it), the guard must fire --
        # serving split-adjusted as a strategy-facing default is raw-as-adjusted without coverage.
        mutated = self._src("binding_source").replace(
            "NormalizationMode.RAW: \"raw\",",
            "NormalizationMode.RAW: \"raw\",\n    NormalizationMode.SPLIT_ADJUSTED: \"split-adjusted\",",
        )
        self.assertNotEqual(mutated, self._src("binding_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_binding_defers_split_adjusted(self.config, mutated)

    def test_reverting_default_to_raw_is_caught(self) -> None:
        # A RAW default would not fail closed on the bare-default consumer call.
        mutated = self._src("binding_source").replace(
            "normalization: NormalizationMode = NormalizationMode.SPLIT_ADJUSTED",
            "normalization: NormalizationMode = NormalizationMode.RAW",
        )
        self.assertNotEqual(mutated, self._src("binding_source"))
        with self.assertRaises(NormalizationModesCheckError):
            check_binding_defers_split_adjusted(self.config, mutated)


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
        # Public surfaces (CLI + binding) serve RAW only; the Rust core math additionally implements
        # SPLIT_ADJUSTED; SPLIT_ADJUSTED is explicitly deferred from public exposure (no advertised mode
        # a consumer cannot actually select).
        self.assertEqual(block["public_request_modes"], ["RAW"])
        self.assertEqual(block["core_library_modes"], ["RAW", "SPLIT_ADJUSTED"])
        self.assertIn("SPLIT_ADJUSTED", block["deferred_public_modes"])
        self.assertNotIn("SPLIT_ADJUSTED", block["public_request_modes"])


if __name__ == "__main__":
    unittest.main()
