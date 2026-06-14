"""Contract tests for SRS-EXE-008 (SyRS SYS-3 / SYS-7 / SYS-64 / SYS-90,
NFR-R3; StRS SN-1.08 / SN-1.22).

Mirrors ``tests/test_live_designation_contract.py``: shells out to
``tools/order_lifecycle_check.py``, then exercises each per-check function
in-process, including negative spot-checks that verify the contract actually
catches regressions (a dropped state, an undocumented or missing transition
edge, a public correlation-id field, a non-fallible ``new``, a missing
duplicate-rejection category, a cancel-replace that does not retain the
original id, and a public lifecycle field).
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

from order_lifecycle_check import (  # noqa: E402
    OrderLifecycleCheckError,
    check_cancel_replace_audit,
    check_correlation_id,
    check_idempotency,
    check_ledger,
    check_lifecycle,
    check_lifecycle_error,
    check_state_enum,
    check_terminal_states,
    check_transition_graph,
    load_config,
    types_source,
)


class OrderLifecycleScriptTest(unittest.TestCase):
    def test_srs_exe_008_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/order_lifecycle_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-EXE-008 PASS", result.stdout)
        for needle in (
            "OrderState with 9 states",
            "New, PendingSubmit, Acked, PartiallyFilled, Filled, CancelPending, "
            "Cancelled, Rejected, Expired",
            "is_terminal covers exactly the 4 terminal states",
            "allowed_next matches order_lifecycle_contract.transitions arm-for-arm",
            "no missing, no undocumented",
            "ClientCorrelationId as a private-field idempotency key",
            "graph-enforcing transition_to",
            "submit returns Result<&OrderLifecycle, StructuredOrderError>",
            "DuplicateClientCorrelationId (wire DUPLICATE_CLIENT_CORRELATION_ID)",
            "cancel-then-new",
            "replaces: Some(..)",
            "OrderLifecycleError with 5 variants",
        ):
            self.assertIn(needle, result.stdout)


class OrderLifecyclePositiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(self.config)

    def test_all_collectors_pass_on_real_source(self) -> None:
        for collector in (
            check_state_enum,
            check_terminal_states,
            check_transition_graph,
            check_correlation_id,
            check_lifecycle,
            check_ledger,
            check_idempotency,
            check_cancel_replace_audit,
            check_lifecycle_error,
        ):
            with self.subTest(collector=collector.__name__):
                self.assertIsInstance(collector(self.config, self.src), str)


class OrderLifecycleNegativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = types_source(self.config)

    def test_dropped_state_variant_is_caught(self) -> None:
        # Remove PartiallyFilled from the OrderState enum body.
        broken = self.src.replace("    PartiallyFilled,\n", "", 1)
        with self.assertRaises(OrderLifecycleCheckError):
            check_state_enum(self.config, broken)

    def test_missing_wire_string_is_caught(self) -> None:
        broken = self.src.replace('Self::Filled => "FILLED",', 'Self::Filled => "DONE",')
        with self.assertRaises(OrderLifecycleCheckError):
            check_state_enum(self.config, broken)

    def test_undocumented_transition_edge_is_caught(self) -> None:
        # The code's New arm has {PendingSubmit, Rejected}; if the contract only
        # documents {PendingSubmit}, the code's Rejected edge is undocumented.
        cfg = copy.deepcopy(self.config)
        cfg["order_lifecycle_contract"]["transitions"]["New"] = ["PendingSubmit"]
        with self.assertRaises(OrderLifecycleCheckError):
            check_transition_graph(cfg, self.src)

    def test_missing_transition_edge_is_caught(self) -> None:
        # Document an edge (New -> Filled) the code does not implement.
        cfg = copy.deepcopy(self.config)
        cfg["order_lifecycle_contract"]["transitions"]["New"] = [
            "PendingSubmit",
            "Rejected",
            "Filled",
        ]
        with self.assertRaises(OrderLifecycleCheckError):
            check_transition_graph(cfg, self.src)

    def test_terminal_state_with_outgoing_edge_is_caught(self) -> None:
        cfg = copy.deepcopy(self.config)
        cfg["order_lifecycle_contract"]["transitions"]["Filled"] = ["New"]
        with self.assertRaises(OrderLifecycleCheckError):
            check_terminal_states(cfg, self.src)

    def test_public_correlation_id_field_is_caught(self) -> None:
        broken = self.src.replace(
            "pub struct ClientCorrelationId(String);",
            "pub struct ClientCorrelationId(pub String);",
        )
        with self.assertRaises(OrderLifecycleCheckError):
            check_correlation_id(self.config, broken)

    def test_non_fallible_constructor_is_caught(self) -> None:
        broken = self.src.replace(
            "pub fn new(value: impl Into<String>) -> Result<Self, OrderLifecycleError> {",
            "pub fn new(value: impl Into<String>) -> Self {",
        )
        with self.assertRaises(OrderLifecycleCheckError):
            check_correlation_id(self.config, broken)

    def test_missing_duplicate_rejection_category_is_caught(self) -> None:
        broken = self.src.replace(
            "category: OrderErrorCategory::DuplicateClientCorrelationId,",
            "category: OrderErrorCategory::InvalidSymbol,",
        )
        with self.assertRaises(OrderLifecycleCheckError):
            check_idempotency(self.config, broken)

    def test_cancel_replace_without_audit_link_is_caught(self) -> None:
        broken = self.src.replace(
            "replaces: Some(original_id.clone()),",
            "replaces: None,",
        )
        with self.assertRaises(OrderLifecycleCheckError):
            check_cancel_replace_audit(self.config, broken)

    def test_public_lifecycle_field_is_caught(self) -> None:
        broken = self.src.replace(
            "    state: OrderState,",
            "    pub state: OrderState,",
        )
        with self.assertRaises(OrderLifecycleCheckError):
            check_lifecycle(self.config, broken)


if __name__ == "__main__":
    unittest.main()
