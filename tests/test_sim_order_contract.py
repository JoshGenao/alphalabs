"""Contract tests for SRS-SIM-001 (simulate paper orders locally, no broker routing).

SRS-SIM-001 / SyRS SYS-82 / SYS-3 / SYS-4 / StRS SN-1.29 / SN-1.08 / SN-1.24 —
simulate paper strategy orders locally without routing to any brokerage.
Acceptance: market, limit, stop, stop-limit, equity, option, and multi-leg orders
are processed by the simulation engine and create no IB API order calls. This
slice ships the paper order-intake path in ``crates/atp-simulation`` (module
``paper_order``); the deferred halves (SYS-83 fill triggering, the full SYS-84
ledger, persistence, orchestrator routing, the Python runtime) keep
``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_sim_cost_contract.py``: shells out to
``tools/sim_order_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / Cargo.toml in memory
and assert the contract actually catches the regression (a dropped order-type
variant, an injected ``Broker`` routing variant, a dropped composite marker, a
removed fail-closed guard, an injected float, a dropped lib re-export, an injected
broker dependency, a leaked vendor token).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_order_check import (  # noqa: E402
    SimOrderCheckError,
    assert_sim_order_static,
    cargo_source,
    check_cargo_test_smoke,
    check_fail_closed,
    check_module_reexport,
    check_money_invariant,
    check_no_broker_dependency,
    check_order_error_enum,
    check_order_leg_struct,
    check_order_request_enum,
    check_order_types,
    check_routing_internal_only,
    check_vendor_isolation,
    lib_source,
    load_config,
    order_source,
    run_checks,
)


class SimOrderScriptTest(unittest.TestCase):
    def test_srs_sim_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_order_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SIM-001 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "paper_order models AssetClass (Equity, Option); Side (Buy, Sell); "
            "OrderType (Market, Limit, Stop, StopLimit)",
            "OrderLeg with 5 fields (symbol, asset_class, side, quantity, order_type)",
            "PaperOrderRequest (Single, MultiLeg)",
            "OrderRouting has exactly the internal-only variant(s) (InternalSimulation) and NO "
            "broker variant (Broker, Brokerage, Ib, InteractiveBrokers, Gateway)",
            "OrderError with 7 fail-closed variants "
            "(EmptySymbol, NonPositiveQuantity, NonPositiveLimitPrice, NonPositiveStopPrice, "
            "EmptyMultiLeg, SingleLegComposite, NonOptionCompositeLeg)",
            "paper_order prices are integer minor units: no f64, "
            "limit_price_minor, stop_price_minor typed i64",
            "lib.rs re-exports `pub mod paper_order;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "paper_order module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-SIM-001 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.order_src = order_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class OrderTypesTest(_Fixture):
    def test_enums_present(self) -> None:
        evidence = check_order_types(self.config, self.order_src)
        self.assertIn("OrderType (Market, Limit, Stop, StopLimit)", evidence)

    def test_dropped_asset_class_variant_is_caught(self) -> None:
        mutated = self.order_src.replace("    Equity,", "    Spot,", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_order_types(self.config, mutated)
        self.assertIn("Equity", str(ctx.exception))

    def test_dropped_order_type_variant_is_caught(self) -> None:
        mutated = self.order_src.replace(
            "    StopLimit {\n        stop_price_minor: i64,\n        limit_price_minor: i64,\n    },",
            "",
            1,
        )
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_order_types(self.config, mutated)
        self.assertIn("StopLimit", str(ctx.exception))


class OrderLegStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_order_leg_struct(self.config, self.order_src)
        self.assertIn("quantity", evidence)

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.order_src.replace("    pub quantity: i64,", "", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_order_leg_struct(self.config, mutated)
        self.assertIn("quantity", str(ctx.exception))


class OrderRequestEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_order_request_enum(self.config, self.order_src)
        self.assertIn("Single, MultiLeg", evidence)

    def test_dropped_multileg_variant_is_caught(self) -> None:
        mutated = self.order_src.replace("    MultiLeg { legs: Vec<OrderLeg> },", "", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_order_request_enum(self.config, mutated)
        self.assertIn("MultiLeg", str(ctx.exception))


class RoutingInternalOnlyTest(_Fixture):
    def test_routing_evidence(self) -> None:
        evidence = check_routing_internal_only(self.config, self.order_src)
        self.assertIn("compile-time guarantee", evidence)

    def test_injected_broker_variant_is_caught(self) -> None:
        # The core SRS-SIM-001 regression: a brokerage routing variant would let a
        # paper order reach IB. The contract must reject it.
        mutated = self.order_src.replace(
            "    InternalSimulation {",
            "    Broker { order_id: String },\n    InternalSimulation {",
            1,
        )
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_routing_internal_only(self.config, mutated)
        self.assertIn("Broker", str(ctx.exception))

    def test_removed_internal_variant_is_caught(self) -> None:
        mutated = self.order_src.replace("    InternalSimulation {", "    RenamedRoute {", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_routing_internal_only(self.config, mutated)
        self.assertIn("InternalSimulation", str(ctx.exception))

    def test_dropped_composite_marker_is_caught(self) -> None:
        mutated = self.order_src.replace("composite: true", "composite: false", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_routing_internal_only(self.config, mutated)
        self.assertIn("composite", str(ctx.exception))


class OrderErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_order_error_enum(self.config, self.order_src)
        for variant in ("EmptySymbol", "NonPositiveQuantity", "EmptyMultiLeg"):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.order_src.replace("    EmptyMultiLeg,", "", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_order_error_enum(self.config, mutated)
        self.assertIn("EmptyMultiLeg", str(ctx.exception))


class FailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_fail_closed(self.config, self.order_src)
        self.assertIn("validates each leg", evidence)

    def test_removed_validate_call_is_caught(self) -> None:
        mutated = self.order_src.replace("validate_leg(leg)?", "noop_validate()")
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))

    def test_removed_empty_multileg_guard_is_caught(self) -> None:
        mutated = self.order_src.replace(
            "return Err(OrderError::EmptyMultiLeg);",
            "{}",
            1,
        )
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("EmptyMultiLeg", str(ctx.exception))

    def test_removed_leg_guard_is_caught(self) -> None:
        # Drop the non-positive-quantity guard from validate_leg.
        mutated = self.order_src.replace("OrderError::NonPositiveQuantity {", "ignored {")
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("NonPositiveQuantity", str(ctx.exception))

    def test_removed_single_leg_composite_guard_is_caught(self) -> None:
        mutated = self.order_src.replace(
            "return Err(OrderError::SingleLegComposite);",
            "{}",
            1,
        )
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("SingleLegComposite", str(ctx.exception))

    def test_removed_non_option_composite_guard_is_caught(self) -> None:
        mutated = self.order_src.replace(
            "return Err(OrderError::NonOptionCompositeLeg);",
            "{}",
            1,
        )
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("NonOptionCompositeLeg", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.order_src)
        self.assertIn("integer minor units", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.order_src.replace("limit_price_minor: i64", "limit_price_minor: f64", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_renamed_minor_field_is_caught(self) -> None:
        mutated = self.order_src.replace("stop_price_minor: i64", "stop_price: i64")
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("stop_price_minor", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod paper_order;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod paper_order;", "pub mod renamed_order;", 1)
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("paper_order", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("no dependency on the live/broker path", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.order_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.order_src + "\n// routes through ib_insync under the hood\n"
        with self.assertRaises(SimOrderCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable paper order-intake path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("sim_order_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("sim_order_check.shutil.which", return_value=None):
            with self.assertRaises(SimOrderCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_eleven_items(self) -> None:
        # 10 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 11)

    def test_static_evidence_is_ten_items(self) -> None:
        self.assertEqual(len(assert_sim_order_static(load_config(), ROOT)), 10)


if __name__ == "__main__":
    unittest.main()
