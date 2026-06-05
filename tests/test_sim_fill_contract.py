"""Contract tests for SRS-SIM-002 (simulate fills using live market data + fill models).

SRS-SIM-002 / SyRS SYS-83 / SYS-87 / StRS SN-1.29 / SN-1.03 — simulate fills using
live market data and configurable fill models. Acceptance: market, limit, stop, and
stop-limit simulated fills follow SYS-83 defaults and per-strategy configuration;
fill volume constraints are enforced. This slice ships the fill-model / triggering
path in ``crates/atp-simulation`` (module ``fill_model``); the deferred halves
(SYS-87a market hours, SYS-87c stale-data threshold, the SYS-70 live feed, the
SYS-83b fill-probability model, the full SYS-84 ledger, persistence, orchestrator
routing, the Python runtime) keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_sim_order_contract.py``: shells out to
``tools/sim_fill_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / Cargo.toml in memory
and assert the contract actually catches the regression (a dropped snapshot field,
a dropped fill-decision variant, a flipped SYS-83 directional rule, a removed
fail-closed guard, a removed volume cap, an injected float, a dropped lib
re-export, an injected broker dependency, a leaked vendor token).
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

from sim_fill_check import (  # noqa: E402
    SimFillCheckError,
    assert_sim_fill_static,
    cargo_source,
    check_cargo_test_smoke,
    check_evaluate_fill,
    check_fill_decision_enum,
    check_fill_model_config,
    check_fill_model_error_enum,
    check_fill_rules,
    check_market_snapshot_struct,
    check_module_reexport,
    check_money_invariant,
    check_no_broker_dependency,
    check_vendor_isolation,
    check_volume_budget,
    check_volume_cap,
    fill_source,
    lib_source,
    load_config,
    run_checks,
)


class SimFillScriptTest(unittest.TestCase):
    def test_srs_sim_002_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_fill_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SIM-002 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "MarketSnapshot with 4 live-market-data fields "
            "(bid_minor, ask_minor, last_minor, bar_volume)",
            "FillModelConfig (limit_fill) with syrs_defaults() and LimitFillModel "
            "(ImmediateOnCross, RequireThroughCross)",
            "FillDecision (Filled, NoFill) and NoFillReason "
            "(LimitNotCrossed, StopNotTriggered, ZeroVolume)",
            "FillModelError with 7 fail-closed variants "
            "(NonPositiveQuote, CrossedBook, NegativeVolume, NonPositiveQuantity, "
            "NonPositiveLimitPrice, NonPositiveStopPrice, BudgetSnapshotMismatch)",
            "implements the SYS-83 rules",
            "enforces the SYS-87b volume constraint",
            "evaluate_fill delegates to evaluate_fill_against_budget with a fresh per-call budget",
            "declares BarVolumeBudget (remaining + observed_bar_volume: i64)",
            "the aggregate of fills never exceeds the observed volume",
            "is BOUND to its bar (a budget/snapshot mismatch fails closed",
            "fill_model prices are integer minor units: no f64, "
            "bid_minor, ask_minor, last_minor, fill_price_minor typed i64",
            "lib.rs re-exports `pub mod fill_model;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "fill_model module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-SIM-002 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.fill_src = fill_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class MarketSnapshotTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_market_snapshot_struct(self.config, self.fill_src)
        self.assertIn("bid_minor, ask_minor, last_minor, bar_volume", evidence)

    def test_dropped_volume_field_is_caught(self) -> None:
        mutated = self.fill_src.replace("    pub bar_volume: i64,", "", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_market_snapshot_struct(self.config, mutated)
        self.assertIn("bar_volume", str(ctx.exception))


class FillModelConfigTest(_Fixture):
    def test_config_evidence(self) -> None:
        evidence = check_fill_model_config(self.config, self.fill_src)
        self.assertIn("ImmediateOnCross", evidence)
        self.assertIn("RequireThroughCross", evidence)

    def test_dropped_syrs_defaults_is_caught(self) -> None:
        mutated = self.fill_src.replace("pub fn syrs_defaults", "pub fn renamed_defaults", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_model_config(self.config, mutated)
        self.assertIn("syrs_defaults", str(ctx.exception))

    def test_dropped_limit_fill_variant_is_caught(self) -> None:
        mutated = self.fill_src.replace("    ImmediateOnCross,", "    RenamedModel,", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_model_config(self.config, mutated)
        self.assertIn("ImmediateOnCross", str(ctx.exception))

    def test_dropped_through_cross_variant_is_caught(self) -> None:
        # RequireThroughCross is the second behavior-changing model; dropping it
        # would make the "configurable" claim hollow again (the adversarial-review
        # finding). It must be caught.
        mutated = self.fill_src.replace("    RequireThroughCross,", "", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_model_config(self.config, mutated)
        self.assertIn("RequireThroughCross", str(ctx.exception))


class FillDecisionEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_fill_decision_enum(self.config, self.fill_src)
        self.assertIn("LimitNotCrossed, StopNotTriggered, ZeroVolume", evidence)

    def test_dropped_nofill_variant_is_caught(self) -> None:
        mutated = self.fill_src.replace("    NoFill { reason: NoFillReason },", "", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_decision_enum(self.config, mutated)
        self.assertIn("NoFill", str(ctx.exception))

    def test_dropped_zero_volume_reason_is_caught(self) -> None:
        # ZeroVolume is the SYS-87b no-fill reason; dropping it must be caught.
        mutated = self.fill_src.replace("    ZeroVolume,", "    RenamedReason,", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_decision_enum(self.config, mutated)
        self.assertIn("ZeroVolume", str(ctx.exception))


class FillModelErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_fill_model_error_enum(self.config, self.fill_src)
        for variant in (
            "NonPositiveQuote",
            "CrossedBook",
            "NegativeVolume",
            "NonPositiveLimitPrice",
            "NonPositiveStopPrice",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_crossed_book_variant_is_caught(self) -> None:
        mutated = self.fill_src.replace(
            "    CrossedBook { bid_minor: i64, ask_minor: i64 },",
            "",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_model_error_enum(self.config, mutated)
        self.assertIn("CrossedBook", str(ctx.exception))

    def test_dropped_limit_price_variant_is_caught(self) -> None:
        mutated = self.fill_src.replace(
            "    NonPositiveLimitPrice { price_minor: i64 },",
            "",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_model_error_enum(self.config, mutated)
        self.assertIn("NonPositiveLimitPrice", str(ctx.exception))


class FillRulesTest(_Fixture):
    def test_rules_evidence(self) -> None:
        evidence = check_fill_rules(self.config, self.fill_src)
        self.assertIn("SYS-83", evidence)

    def test_flipped_market_rule_is_caught(self) -> None:
        # SYS-83a: a market buy fills at the ask. Flipping it to the bid would
        # systematically misprice every market buy; the contract must catch it.
        mutated = self.fill_src.replace(
            "Side::Buy => snapshot.ask_minor,",
            "Side::Buy => snapshot.bid_minor,",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_rules(self.config, mutated)
        self.assertIn("ask_minor", str(ctx.exception))

    def test_dropped_stop_direction_is_caught(self) -> None:
        mutated = self.fill_src.replace(
            "Side::Buy => snapshot.last_minor >= stop_price_minor,",
            "Side::Buy => false,",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_rules(self.config, mutated)
        self.assertIn("stop", str(ctx.exception))

    def test_dropped_through_cross_rule_is_caught(self) -> None:
        # RequireThroughCross uses a STRICT cross (ask < limit). Collapsing it to
        # the touch comparison (ask <= limit) would silently make the two fill
        # models identical; the contract must catch the missing strict token.
        mutated = self.fill_src.replace(
            "Side::Buy => snapshot.ask_minor < limit_price_minor,",
            "Side::Buy => snapshot.ask_minor <= limit_price_minor,",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_fill_rules(self.config, mutated)
        self.assertIn("RequireThroughCross", str(ctx.exception))


class EvaluateFillTest(_Fixture):
    def test_evaluate_evidence(self) -> None:
        evidence = check_evaluate_fill(self.config, self.fill_src)
        self.assertIn("validates the order type's prices", evidence)
        self.assertIn("validate_snapshot(snapshot)?", evidence)

    def test_removed_validate_call_is_caught(self) -> None:
        mutated = self.fill_src.replace("validate_snapshot(snapshot)?", "()")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_evaluate_fill(self.config, mutated)
        self.assertIn("validate_snapshot", str(ctx.exception))

    def test_removed_quantity_guard_is_caught(self) -> None:
        mutated = self.fill_src.replace("FillModelError::NonPositiveQuantity {", "ignored {")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_evaluate_fill(self.config, mutated)
        self.assertIn("NonPositiveQuantity", str(ctx.exception))

    def test_removed_snapshot_guard_is_caught(self) -> None:
        # Drop the crossed-book guard from validate_snapshot.
        mutated = self.fill_src.replace("FillModelError::CrossedBook {", "ignored {")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_evaluate_fill(self.config, mutated)
        self.assertIn("CrossedBook", str(ctx.exception))

    def test_removed_order_validate_call_is_caught(self) -> None:
        # Dropping the order-price validation lets a negative limit/stop reach the
        # fill path (the adversarial-review regression).
        mutated = self.fill_src.replace("validate_order_type(order_type)?", "()")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_evaluate_fill(self.config, mutated)
        self.assertIn("validate_order_type", str(ctx.exception))

    def test_removed_order_price_guard_is_caught(self) -> None:
        # Drop the non-positive-limit-price guard from validate_order_type.
        mutated = self.fill_src.replace("FillModelError::NonPositiveLimitPrice {", "ignored {")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_evaluate_fill(self.config, mutated)
        self.assertIn("NonPositiveLimitPrice", str(ctx.exception))


class VolumeCapTest(_Fixture):
    def test_cap_evidence(self) -> None:
        evidence = check_volume_cap(self.config, self.fill_src)
        self.assertIn("SYS-87b", evidence)

    def test_removed_cap_is_caught(self) -> None:
        # Removing the min() lets a fill exceed the remaining volume (SYS-87b).
        mutated = self.fill_src.replace(
            "requested_quantity.min(budget.remaining())",
            "requested_quantity",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_cap(self.config, mutated)
        self.assertIn("remaining", str(ctx.exception))

    def test_removed_consume_is_caught(self) -> None:
        # Dropping the budget consumption lets the AGGREGATE of fills exceed the
        # bar volume (the adversarial-review finding). It must be caught.
        mutated = self.fill_src.replace(
            "budget.consume(fill_quantity)",
            "let _ = fill_quantity",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_cap(self.config, mutated)
        self.assertIn("consume", str(ctx.exception))

    def test_removed_zero_volume_guard_is_caught(self) -> None:
        mutated = self.fill_src.replace("NoFillReason::ZeroVolume", "NoFillReason::LimitNotCrossed")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_cap(self.config, mutated)
        self.assertIn("ZeroVolume", str(ctx.exception))


class VolumeBudgetTest(_Fixture):
    def test_budget_evidence(self) -> None:
        evidence = check_volume_budget(self.config, self.fill_src)
        self.assertIn("BarVolumeBudget", evidence)
        self.assertIn("AGGREGATE", evidence)

    def test_dropped_consume_method_is_caught(self) -> None:
        mutated = self.fill_src.replace("fn consume(", "fn renamed_consume(", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_budget(self.config, mutated)
        self.assertIn("consume", str(ctx.exception))

    def test_dropped_negative_volume_guard_is_caught(self) -> None:
        # BarVolumeBudget::new must fail closed on a negative volume. Drop the only
        # NegativeVolume guard reachable from new() and assert it is caught.
        mutated = self.fill_src.replace("FillModelError::NegativeVolume", "FillModelError::Ignored")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_budget(self.config, mutated)
        self.assertIn("NegativeVolume", str(ctx.exception))

    def test_dropped_budget_binding_is_caught(self) -> None:
        # Removing the budget/snapshot binding check lets an oversized budget
        # overfill a thin bar (the adversarial-review finding). It must be caught.
        mutated = self.fill_src.replace(
            "budget.observed_bar_volume() != snapshot.bar_volume",
            "false",
            1,
        )
        with self.assertRaises(SimFillCheckError) as ctx:
            check_volume_budget(self.config, mutated)
        self.assertIn("bound to its bar", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.fill_src)
        self.assertIn("integer minor units", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.fill_src.replace("pub bid_minor: i64", "pub bid_minor: f64", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_renamed_minor_field_is_caught(self) -> None:
        mutated = self.fill_src.replace("fill_price_minor: i64", "fill_price: i64")
        with self.assertRaises(SimFillCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("fill_price_minor", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod fill_model;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod fill_model;", "pub mod renamed_fill;", 1)
        with self.assertRaises(SimFillCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("fill_model", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("no dependency on the live/broker path", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(SimFillCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.fill_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.fill_src + "\n// quotes arrive via ib_insync under the hood\n"
        with self.assertRaises(SimFillCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable fill-model path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("sim_fill_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("sim_fill_check.shutil.which", return_value=None):
            with self.assertRaises(SimFillCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_thirteen_items(self) -> None:
        # 12 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 13)

    def test_static_evidence_is_twelve_items(self) -> None:
        self.assertEqual(len(assert_sim_fill_static(load_config(), ROOT)), 12)


if __name__ == "__main__":
    unittest.main()
