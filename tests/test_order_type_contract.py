"""Contract tests for SRS-EXE-003 (SyRS SYS-3 / SYS-82; StRS SN-1.08 / BG-1) —
the source-neutral order-type vocabulary + price-validation authority.

Mirrors ``tests/test_order_event_dispatch_contract.py``: shells out to
``tools/order_type_check.py``, exercises each per-check function in-process, and
adds negative spot-checks that verify the contract actually catches regressions
(a renamed wire string, a drifted price-requirement matrix, a non-fail-closed
``validate_prices``, a re-introduced local order-type copy in the paper crate, a
dropped re-export, a drifted paper-intake validation rule, and a contract/code
wire-string disagreement).
"""

from __future__ import annotations

import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from order_type_check import (  # noqa: E402
    OrderTypeCheckError,
    check_asset_class,
    check_cargo_test_smoke,
    check_fill_model_delegation,
    check_order_side_enum,
    check_order_type_enum,
    check_paper_reexport,
    check_paper_validate_parity,
    check_price_matrix,
    check_validate_prices_fail_closed,
    fill_model_source,
    load_config,
    paper_order_source,
    types_source,
)


class OrderTypeScriptTest(unittest.TestCase):
    def test_srs_exe_003_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/order_type_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # scope-honest header: SDK-surface contract evidence, NOT a full pass
        self.assertIn("SRS-EXE-003 SDK-SURFACE PASS", result.stdout)
        self.assertNotIn("SRS-EXE-003 PASS\n", result.stdout)
        self.assertIn("Deferred end-to-end evidence", result.stdout)
        for needle in (
            "OrderType declares 4 order types",
            "as_str wire strings == order_types[].wire == Python OrderType values",
            "OrderSide::as_str == sides == Python OrderSide values",
            "AssetClass::as_str == asset_classes == Python AssetClass values",
            "price-requirement matrix matches order_types[] arm-for-arm over all 4 types",
            "validate_prices is fail-closed",
            "re-exports atp-types' AssetClass, OrderSide, OrderType and defines no local copy",
            "DELEGATES price positivity to OrderType::validate_prices()",
            "fill_model.rs::validate_order_type DELEGATES price positivity to "
            "OrderType::validate_prices()",
        ):
            self.assertIn(needle, result.stdout)


class OrderTypePositiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(ROOT)
        self.paper = paper_order_source(ROOT)
        self.fill = fill_model_source(ROOT)

    def test_static_collectors_pass_on_real_source(self) -> None:
        self.assertIsInstance(check_order_type_enum(self.config, self.src, ROOT), str)
        self.assertIsInstance(check_order_side_enum(self.config, self.src, ROOT), str)
        self.assertIsInstance(check_asset_class(self.config, self.src, ROOT), str)
        self.assertIsInstance(check_price_matrix(self.config, self.src), str)
        self.assertIsInstance(check_validate_prices_fail_closed(self.config, self.src), str)
        self.assertIsInstance(check_paper_reexport(self.config, self.paper), str)
        self.assertIsInstance(check_paper_validate_parity(self.config, self.paper), str)
        self.assertIsInstance(check_fill_model_delegation(self.config, self.fill), str)


class OrderTypeNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(ROOT)
        self.paper = paper_order_source(ROOT)
        self.fill = fill_model_source(ROOT)

    def test_renamed_order_type_wire_string_is_caught(self) -> None:
        broken = self.src.replace('Self::Limit { .. } => "LIMIT",', 'Self::Limit { .. } => "LMT",')
        with self.assertRaises(OrderTypeCheckError):
            check_order_type_enum(self.config, broken, ROOT)

    def test_drifted_price_matrix_is_caught(self) -> None:
        # Drop StopLimit from requires_limit_price so the matrix disagrees with
        # the contract (StopLimit must require a limit price).
        broken = self.src.replace(
            "matches!(self, Self::Limit { .. } | Self::StopLimit { .. })",
            "matches!(self, Self::Limit { .. })",
        )
        with self.assertRaises(OrderTypeCheckError):
            check_price_matrix(self.config, broken)

    def test_non_fail_closed_validate_prices_is_caught(self) -> None:
        # Weaken the positivity guard so a zero price would slip through.
        broken = self.src.replace("<= 0", "< 0")
        with self.assertRaises(OrderTypeCheckError):
            check_validate_prices_fail_closed(self.config, broken)

    def test_disabled_validate_prices_guard_is_caught(self) -> None:
        # A disabled/unreachable guard is fail-OPEN even though the `<= 0` token and
        # the error names are still present — the structural check must reject it.
        broken = self.src.replace("if price_minor <= 0 {", "if false && price_minor <= 0 {")
        with self.assertRaises(OrderTypeCheckError):
            check_validate_prices_fail_closed(self.config, broken)

    def test_unreachable_validate_prices_wrap_is_caught(self) -> None:
        # Guards left intact but the function made fail-OPEN by an always-false block.
        broken = self.src.replace(
            "pub fn validate_prices(self) -> Result<(), OrderTypeError> {",
            "pub fn validate_prices(self) -> Result<(), OrderTypeError> {\n        if false {}",
        )
        with self.assertRaises(OrderTypeCheckError):
            check_validate_prices_fail_closed(self.config, broken)

    def test_dropped_paper_reexport_is_caught(self) -> None:
        broken = self.paper.replace("pub use atp_types::order_type::OrderType;", "")
        with self.assertRaises(OrderTypeCheckError):
            check_paper_reexport(self.config, broken)

    def test_local_order_type_copy_in_paper_is_caught(self) -> None:
        # A reintroduced local enum copy can drift from the live authority — the
        # single-authority guarantee must reject it.
        broken = self.paper + "\npub enum Side { Buy, Sell }\n"
        with self.assertRaises(OrderTypeCheckError):
            check_paper_reexport(self.config, broken)

    def test_paper_intake_not_delegating_is_caught(self) -> None:
        # Re-implementing validation instead of delegating to the shared authority
        # (so the two could drift) must be caught.
        broken = self.paper.replace("validate_prices()", "validate_nothing()")
        with self.assertRaises(OrderTypeCheckError):
            check_paper_validate_parity(self.config, broken)

    def test_fill_model_not_delegating_is_caught(self) -> None:
        # The fill path re-checking prices on its own (a copy that can drift from
        # intake validation) must be caught.
        broken = self.fill.replace("validate_prices()", "validate_nothing()")
        with self.assertRaises(OrderTypeCheckError):
            check_fill_model_delegation(self.config, broken)

    def test_contract_code_wire_disagreement_is_caught(self) -> None:
        cfg = copy.deepcopy(self.config)
        cfg["order_type_contract"]["order_types"][0]["wire"] = "BOGUS"
        with self.assertRaises(OrderTypeCheckError):
            check_order_type_enum(cfg, self.src, ROOT)


class OrderTypeCargoGateTest(unittest.TestCase):
    def test_missing_cargo_fails_closed(self) -> None:
        # The fail-closed proof is the executable Rust test; with no cargo the gate
        # must FAIL CLOSED, not skip-and-PASS (a static regex alone can be fooled).
        with mock.patch("order_type_check.shutil.which", return_value=None):
            with self.assertRaises(OrderTypeCheckError):
                check_cargo_test_smoke(load_config())


if __name__ == "__main__":
    unittest.main()
