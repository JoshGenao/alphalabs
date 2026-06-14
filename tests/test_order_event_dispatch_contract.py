"""Contract tests for SRS-SDK-004 (SyRS SYS-7 / SYS-85 / NFR-P4; StRS SN-1.22 /
SN-1.29) — the Rust-core source-neutral order-event category authority.

Mirrors ``tests/test_order_lifecycle_contract.py``: shells out to
``tools/order_event_dispatch_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract actually
catches regressions (a renamed wire string, a drifted state->category arm, a
totality gap, a non-fail-closed ``for_transition``, a drifted field-presence
predicate, a divergent AC-named source, and a latency-budget that disagrees
across the Rust / Python / JSON surfaces).
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

from order_event_dispatch_check import (  # noqa: E402
    OrderEventDispatchCheckError,
    check_ac_named_single_source,
    check_category_enum,
    check_field_requirements,
    check_for_transition_fail_closed,
    check_latency_parity,
    check_no_public_bypass,
    check_state_to_category,
    load_config,
    types_source,
)


class OrderEventDispatchScriptTest(unittest.TestCase):
    def test_srs_sdk_004_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/order_event_dispatch_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # scope-honest header: SDK-surface contract evidence, NOT a full pass
        self.assertIn("SRS-SDK-004 SDK-SURFACE PASS", result.stdout)
        self.assertNotIn("SRS-SDK-004 PASS\n", result.stdout)
        self.assertIn("Deferred end-to-end evidence", result.stdout)
        for needle in (
            "OrderEventCategory declares 6 categories",
            "as_str wire strings == all_categories == Python OrderEventType values",
            "maps all 9 OrderState wire strings to state_to_event_category arm-for-arm",
            "3 internal states -> no callback",
            "for_transition is fail-closed",
            "returns IllegalTransition for an illegal edge",
            "requires_fill_economics == is_ac_named",
            "requires_reason == ['CANCELLED', 'EXPIRED', 'REJECTED']",
            "one source of truth for the AC-named callback set",
            "NFR-P4 budgets are one source of truth",
        ):
            self.assertIn(needle, result.stdout)


class OrderEventDispatchPositiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(ROOT)

    def test_static_collectors_pass_on_real_source(self) -> None:
        self.assertIsInstance(check_category_enum(self.config, self.src, ROOT), str)
        self.assertIsInstance(check_state_to_category(self.config, self.src), str)
        self.assertIsInstance(check_for_transition_fail_closed(self.config, self.src), str)
        self.assertIsInstance(check_no_public_bypass(self.config, self.src), str)
        self.assertIsInstance(check_field_requirements(self.config, self.src), str)
        self.assertIsInstance(check_ac_named_single_source(self.config), str)
        self.assertIsInstance(check_latency_parity(self.config, self.src, ROOT), str)


class OrderEventDispatchNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(ROOT)

    def test_renamed_wire_string_is_caught(self) -> None:
        broken = self.src.replace('Category::Fill => "FILL",', 'Category::Fill => "DONE",')
        with self.assertRaises(OrderEventDispatchCheckError):
            check_category_enum(self.config, broken, ROOT)

    def test_drifted_state_to_category_arm_is_caught(self) -> None:
        # Map a FILLED transition to PARTIAL_FILL instead of FILL.
        broken = self.src.replace(
            "OrderState::Filled => Some(Self(Category::Fill)),",
            "OrderState::Filled => Some(Self(Category::PartialFill)),",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_state_to_category(self.config, broken)

    def test_for_state_totality_gap_is_caught(self) -> None:
        # Remove the Expired arm entirely.
        broken = self.src.replace(
            "OrderState::Expired => Some(Self(Category::Expired)),",
            "",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_state_to_category(self.config, broken)

    def test_non_fail_closed_for_transition_is_caught(self) -> None:
        # Drop the can_transition_to gate so events could be derived for
        # impossible transitions.
        broken = self.src.replace("from.can_transition_to(to)", "true")
        with self.assertRaises(OrderEventDispatchCheckError):
            check_for_transition_fail_closed(self.config, broken)

    def test_public_for_state_bypass_is_caught(self) -> None:
        # Re-exposing the destination-state mapper as public is a fail-closed
        # bypass — the static checker must reject it.
        broken = self.src.replace(
            "const fn for_state(state: OrderState) -> Option<Self> {",
            "pub const fn for_state(state: OrderState) -> Option<Self> {",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_no_public_bypass(self.config, broken)

    def test_public_variant_carrier_is_caught(self) -> None:
        # A public variant carrier would let a dispatcher crate construct a
        # category directly (OrderEventCategory(Category::Fill)), fabricating a
        # callback — the opacity guarantee must reject it.
        broken = self.src.replace(
            "enum Category {",
            "pub enum Category {",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_no_public_bypass(self.config, broken)

    def test_public_wrapped_field_is_caught(self) -> None:
        # A public field on the newtype is equally a construction bypass.
        broken = self.src.replace(
            "pub struct OrderEventCategory(Category);",
            "pub struct OrderEventCategory(pub Category);",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_no_public_bypass(self.config, broken)

    def test_public_for_transition_deriver_is_caught(self) -> None:
        # A public state-pair deriver lets a dispatcher fabricate a callback from
        # arbitrary states (not bound to a tracked order).
        broken = self.src.replace(
            "pub(crate) fn for_transition(",
            "pub fn for_transition(",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_no_public_bypass(self.config, broken)

    def test_drifted_requires_reason_predicate_is_caught(self) -> None:
        # Drop EXPIRED from requires_reason so it no longer matches the contract.
        broken = self.src.replace(
            "Category::Cancelled | Category::Rejected | Category::Expired",
            "Category::Cancelled | Category::Rejected",
        )
        with self.assertRaises(OrderEventDispatchCheckError):
            check_field_requirements(self.config, broken)

    def test_divergent_ac_named_source_is_caught(self) -> None:
        cfg = copy.deepcopy(self.config)
        cfg["order_event_dispatch_contract"]["ac_named_categories"] = ["FILL", "PARTIAL_FILL"]
        with self.assertRaises(OrderEventDispatchCheckError):
            check_ac_named_single_source(cfg)

    def test_latency_budget_disagreement_is_caught(self) -> None:
        cfg = copy.deepcopy(self.config)
        cfg["strategy_api_order_events_contract"]["required_live_callback_latency_p95_ms"] = 2000
        with self.assertRaises(OrderEventDispatchCheckError):
            check_latency_parity(cfg, self.src, ROOT)


if __name__ == "__main__":
    unittest.main()
