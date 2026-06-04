"""Contract tests for SRS-BT-002 (configurable backtest cost models).

SRS-BT-002 / SyRS SYS-15a / SYS-15b / SYS-15c / SYS-15d / StRS SN-1.03 — apply
configurable commission, slippage, and spread-impact models to backtests, with
defaults matching the SyRS values and per-run overrides that need no strategy
code change. This slice ships the cost-model family in ``crates/atp-simulation``
(module ``cost``) and applies it in the runnable engine (module ``backtest``);
the operator override surface stays deferred (feature_list.json keeps
``passes:false``).

Mirrors ``tests/test_backtest_contract.py``: shells out to
``tools/backtest_cost_check.py``, then exercises each per-check function
in-process, including negative spot-checks that mutate the Rust source in memory
and assert the contract actually catches the regression (a default constant that
no longer matches the SyRS value, a dropped model variant, a float in the cost
path, a removed validation guard, a removed cost deduction in the engine, a
leaked vendor token).
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

from backtest_cost_check import (  # noqa: E402
    BacktestCostCheckError,
    _lib_source,
    assert_backtest_cost_static,
    backtest_source,
    check_cargo_test_smoke,
    check_commission_model,
    check_cost_breakdown_struct,
    check_cost_config_struct,
    check_cost_error_enum,
    check_engine_application,
    check_money_invariant,
    check_shared_family,
    check_slippage_model,
    check_spread_impact_model,
    check_syrs_default_constants,
    check_validate_fail_closed,
    check_vendor_isolation,
    check_wiring,
    cost_source,
    load_config,
    run_checks,
)


class BacktestCostScriptTest(unittest.TestCase):
    def test_srs_bt_002_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/backtest_cost_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-002 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "CommissionModel with 4 models (IbTiered, PerShare, PerTrade, None); default is IbTiered",
            "SlippageModel with 2 models (NotionalBps, None); default is NotionalBps at DEFAULT_SLIPPAGE_BPS",
            "SpreadImpactModel with 3 models (ObservedOrFallbackBps, FixedBps, None)",
            "cost defaults match the SyRS values exactly: DEFAULT_SLIPPAGE_BPS=5 (0.05%), DEFAULT_SPREAD_FALLBACK_BPS=10 (0.10%)",
            "35 centi-minor/share ($0.0035), 35-minor floor ($0.35), 100-bps cap (1%)",
            "CostConfig with the 3 model fields (commission, slippage, spread_impact), derives Default",
            "CostBreakdown with the 3 cost components (commission_minor, slippage_minor, spread_impact_minor)",
            "CostError with 3 fail-closed variants (NegativeParameter, NegativeSpread, Overflow)",
            "CostConfig::validate fails closed on a negative",
            "cost math is integer-only: no f64",
            "subtracts the total from cash (`checked_sub(total_cost_minor)`)",
            "fails closed on a negative observed spread (`BacktestError::NegativeSpread`)",
            "wires the per-run override onto BacktestRequest.cost_config",
            "re-exports `pub mod cost`",
            "free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-002 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.cost_src = cost_source(self.config)
        self.backtest_src = backtest_source(self.config)
        self.lib_src = _lib_source(self.config)


class CommissionModelTest(_Fixture):
    def test_models_and_default_present(self) -> None:
        evidence = check_commission_model(self.config, self.cost_src)
        self.assertIn("default is IbTiered", evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.cost_src.replace("    PerTrade { fee_minor: i64 },", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_commission_model(self.config, mutated)
        self.assertIn("PerTrade", str(ctx.exception))

    def test_changed_default_is_caught(self) -> None:
        # Dropping the #[default] off IbTiered loses the SYS-15a default.
        mutated = self.cost_src.replace("    #[default]\n    IbTiered,", "    IbTiered,", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_commission_model(self.config, mutated)
        self.assertIn("default", str(ctx.exception))


class SlippageModelTest(_Fixture):
    def test_default_present(self) -> None:
        evidence = check_slippage_model(self.config, self.cost_src)
        self.assertIn("DEFAULT_SLIPPAGE_BPS", evidence)

    def test_changed_default_is_caught(self) -> None:
        mutated = self.cost_src.replace("bps: DEFAULT_SLIPPAGE_BPS", "bps: 999", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_slippage_model(self.config, mutated)
        self.assertIn("NotionalBps", str(ctx.exception))


class SpreadImpactModelTest(_Fixture):
    def test_default_present(self) -> None:
        evidence = check_spread_impact_model(self.config, self.cost_src)
        self.assertIn("ObservedOrFallbackBps", evidence)

    def test_dropped_fallback_variant_is_caught(self) -> None:
        mutated = self.cost_src.replace("    FixedBps { bps: u32 },", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_spread_impact_model(self.config, mutated)
        self.assertIn("FixedBps", str(ctx.exception))


class SyrsDefaultConstantsTest(_Fixture):
    def test_constants_match_syrs(self) -> None:
        evidence = check_syrs_default_constants(self.config, self.cost_src)
        self.assertIn("match the SyRS values exactly", evidence)

    def test_wrong_slippage_default_is_caught(self) -> None:
        # The headline regression: a default that no longer equals the SyRS value.
        mutated = self.cost_src.replace(
            "DEFAULT_SLIPPAGE_BPS: u32 = 5", "DEFAULT_SLIPPAGE_BPS: u32 = 6", 1
        )
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_syrs_default_constants(self.config, mutated)
        self.assertIn("DEFAULT_SLIPPAGE_BPS", str(ctx.exception))

    def test_wrong_ib_rate_is_caught(self) -> None:
        mutated = self.cost_src.replace(
            "IB_TIERED_RATE_CENTIMINOR_PER_SHARE: i64 = 35",
            "IB_TIERED_RATE_CENTIMINOR_PER_SHARE: i64 = 50",
            1,
        )
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_syrs_default_constants(self.config, mutated)
        self.assertIn("IB_TIERED_RATE_CENTIMINOR_PER_SHARE", str(ctx.exception))


class CostConfigStructTest(_Fixture):
    def test_fields_and_methods_present(self) -> None:
        evidence = check_cost_config_struct(self.config, self.cost_src)
        self.assertIn("commission, slippage, spread_impact", evidence)

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.cost_src.replace("    pub spread_impact: SpreadImpactModel,", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_cost_config_struct(self.config, mutated)
        self.assertIn("spread_impact", str(ctx.exception))

    def test_removed_validate_method_is_caught(self) -> None:
        mutated = self.cost_src.replace("pub fn validate(", "fn removed_validate(", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_cost_config_struct(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))


class CostBreakdownStructTest(_Fixture):
    def test_components_present(self) -> None:
        evidence = check_cost_breakdown_struct(self.config, self.cost_src)
        self.assertIn("commission_minor, slippage_minor, spread_impact_minor", evidence)

    def test_dropped_component_is_caught(self) -> None:
        mutated = self.cost_src.replace("    pub slippage_minor: i64,", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_cost_breakdown_struct(self.config, mutated)
        self.assertIn("slippage_minor", str(ctx.exception))


class CostErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_cost_error_enum(self.config, self.cost_src)
        for variant in ("NegativeParameter", "NegativeSpread", "Overflow"):
            self.assertIn(variant, evidence)

    def test_dropped_negative_spread_is_caught(self) -> None:
        mutated = self.cost_src.replace("    NegativeSpread { spread_minor: i64 },", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_cost_error_enum(self.config, mutated)
        self.assertIn("NegativeSpread", str(ctx.exception))


class ValidateFailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_validate_fail_closed(self.config, self.cost_src)
        self.assertIn("fails closed", evidence)

    def test_removed_field_guard_is_caught(self) -> None:
        # Renaming the per-share rate parameter drops it from the validate guard.
        mutated = self.cost_src.replace("rate_centiminor_per_share", "rate_x")
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_validate_fail_closed(self.config, mutated)
        self.assertIn("rate_centiminor_per_share", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.cost_src)
        self.assertIn("integer-only", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.cost_src.replace(
            "pub commission_minor: i64,", "pub commission_minor: f64,", 1
        )
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_removed_round_helper_is_caught(self) -> None:
        mutated = self.cost_src.replace("fn div_round_half_up", "fn rounded", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("div_round_half_up", str(ctx.exception))


class EngineApplicationTest(_Fixture):
    def test_application_evidence(self) -> None:
        evidence = check_engine_application(self.config, self.backtest_src)
        self.assertIn("subtracts the total from cash", evidence)

    def test_removed_cost_deduction_is_caught(self) -> None:
        # If the engine stops subtracting the cost total, a backtest silently
        # ignores costs — the core SRS-BT-002 regression.
        mutated = self.backtest_src.replace(
            "checked_sub(total_cost_minor)", "checked_sub(zero_cost_minor)", 1
        )
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_engine_application(self.config, mutated)
        self.assertIn("deduct_token", str(ctx.exception))

    def test_removed_negative_spread_guard_is_caught(self) -> None:
        mutated = self.backtest_src.replace(
            "return Err(BacktestError::NegativeSpread {", "if false {", 1
        )
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_engine_application(self.config, mutated)
        self.assertIn("negative_spread_guard", str(ctx.exception))

    def test_unrecorded_fill_costs_are_caught(self) -> None:
        mutated = self.backtest_src.replace("commission_minor: costs.commission_minor,", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_engine_application(self.config, mutated)
        self.assertIn("commission_minor", str(ctx.exception))


class WiringTest(_Fixture):
    def test_wiring_evidence(self) -> None:
        evidence = check_wiring(self.config, self.backtest_src)
        self.assertIn("BacktestRequest.cost_config", evidence)

    def test_missing_request_cost_config_is_caught(self) -> None:
        mutated = self.backtest_src.replace("pub cost_config: CostConfig,", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_wiring(self.config, mutated)
        self.assertIn("cost_config", str(ctx.exception))

    def test_missing_observed_spread_field_is_caught(self) -> None:
        mutated = self.backtest_src.replace("    pub spread_minor: Option<i64>,", "", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_wiring(self.config, mutated)
        self.assertIn("spread_minor", str(ctx.exception))


class SharedFamilyTest(_Fixture):
    def test_shared_family_evidence(self) -> None:
        evidence = check_shared_family(self.config, self.lib_src)
        self.assertIn("pub mod cost", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod cost;", "pub mod renamed;", 1)
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_shared_family(self.config, mutated)
        self.assertIn("cost", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.cost_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.cost_src + "\n// uses databento under the hood\n"
        with self.assertRaises(BacktestCostCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("databento", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable cost models must compile where it matters (init.sh)."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("backtest_cost_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("backtest_cost_check.shutil.which", return_value=None):
            with self.assertRaises(BacktestCostCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_fourteen_items(self) -> None:
        # 13 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 14)

    def test_static_evidence_is_thirteen_items(self) -> None:
        self.assertEqual(len(assert_backtest_cost_static(load_config(), ROOT)), 13)


if __name__ == "__main__":
    unittest.main()
