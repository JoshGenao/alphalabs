"""Contract tests for SRS-BT-003 (shared simulation/backtest cost family).

SRS-BT-003 / SyRS SYS-15e / SYS-83d / StRS SN-1.03 / SN-1.29 — use the same
transaction-cost model family for internal simulation and backtesting unless
configured otherwise. Acceptance: a paper strategy and a backtest using identical
cost configuration compute fills and commissions from the same model family. This
slice ships the internal simulation engine's paper-fill path in
``crates/atp-simulation`` (module ``sim``), consuming the same ``cost`` family the
backtest engine applies; the deferred halves (SYS-83 fill models, the full SYS-84
ledger, persistence, the operator override surface, the Python runtime) keep
``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_backtest_cost_contract.py``: shells out to
``tools/sim_cost_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source in memory and assert
the contract actually catches the regression (a dropped Default derive, a renamed
shared cost_breakdown call, a removed cost deduction, a removed fail-closed guard,
a dropped error variant, an injected float, a dropped lib re-export, a leaked
vendor token).
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

from sim_cost_check import (  # noqa: E402
    SimCostCheckError,
    assert_sim_cost_static,
    check_cargo_test_smoke,
    check_engine_struct,
    check_fail_closed,
    check_money_invariant,
    check_paper_fill_struct,
    check_paper_ledger_struct,
    check_shared_entry_point,
    check_shared_family,
    check_sim_error_enum,
    check_vendor_isolation,
    lib_source,
    load_config,
    run_checks,
    sim_source,
)


class SimCostScriptTest(unittest.TestCase):
    def test_srs_bt_003_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_cost_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-003 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "PaperSimulationEngine carrying the shared cost_config: CostConfig, deriving Default "
            "(= CostConfig::default(), SYS-15e), with 4 methods "
            "(new, with_cost_config, cost_config, simulate_fill)",
            "computes the per-fill breakdown via the identical `cost_breakdown(...)` entry point "
            "the backtest engine calls, and subtracts the total (`checked_sub(total_cost_minor)`)",
            "PaperFill with the same 3 cost components "
            "(commission_minor, slippage_minor, spread_impact_minor)",
            "minimal virtual ledger PaperLedger (cash_minor, position, commission_paid_minor) "
            "with apply_fill()",
            "SimError with 5 fail-closed variants "
            "(EmptySymbol, NonPositivePrice, NegativeSpread, Overflow, Cost)",
            "validates the config (`self.cost_config.validate()?`) and rejects an empty symbol",
            "sim money math is integer-only: no f64, i128::from intermediates, i64::try_from "
            "narrowing + checked_sub -> SimError::Overflow",
            "lib.rs re-exports both `pub mod cost;` and `pub mod sim;`",
            "sim module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-BT-003 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.sim_src = sim_source(self.config)
        self.lib_src = lib_source(self.config)


class EngineStructTest(_Fixture):
    def test_fields_methods_and_default_present(self) -> None:
        evidence = check_engine_struct(self.config, self.sim_src)
        self.assertIn("deriving Default", evidence)

    def test_dropped_default_derive_is_caught(self) -> None:
        mutated = self.sim_src.replace(
            "Debug, Default, Clone, PartialEq, Eq", "Debug, Clone, PartialEq, Eq", 1
        )
        with self.assertRaises(SimCostCheckError) as ctx:
            check_engine_struct(self.config, mutated)
        self.assertIn("Default", str(ctx.exception))

    def test_dropped_cost_field_is_caught(self) -> None:
        mutated = self.sim_src.replace("    cost_config: CostConfig,", "", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_engine_struct(self.config, mutated)
        self.assertIn("cost_config", str(ctx.exception))


class SharedEntryPointTest(_Fixture):
    def test_shared_entry_evidence(self) -> None:
        evidence = check_shared_entry_point(self.config, self.sim_src)
        self.assertIn("cost_breakdown", evidence)

    def test_renamed_breakdown_call_is_caught(self) -> None:
        # If the sim engine stops calling the shared entry point, the families
        # diverge — the core SRS-BT-003 regression.
        mutated = self.sim_src.replace(
            ".cost_breakdown(quantity, price_minor, observed_spread_minor)",
            ".other_breakdown(quantity, price_minor, observed_spread_minor)",
            1,
        )
        with self.assertRaises(SimCostCheckError) as ctx:
            check_shared_entry_point(self.config, mutated)
        self.assertIn("cost_breakdown", str(ctx.exception))

    def test_removed_cost_deduction_is_caught(self) -> None:
        mutated = self.sim_src.replace(
            ".checked_sub(total_cost_minor)", ".checked_sub(zero_minor)", 1
        )
        with self.assertRaises(SimCostCheckError) as ctx:
            check_shared_entry_point(self.config, mutated)
        self.assertIn("checked_sub(total_cost_minor)", str(ctx.exception))


class PaperFillStructTest(_Fixture):
    def test_components_present(self) -> None:
        evidence = check_paper_fill_struct(self.config, self.sim_src)
        self.assertIn("commission_minor, slippage_minor, spread_impact_minor", evidence)

    def test_dropped_component_is_caught(self) -> None:
        mutated = self.sim_src.replace("    pub slippage_minor: i64,", "", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_paper_fill_struct(self.config, mutated)
        self.assertIn("slippage_minor", str(ctx.exception))


class PaperLedgerStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_paper_ledger_struct(self.config, self.sim_src)
        self.assertIn("commission_paid_minor", evidence)

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.sim_src.replace("    pub commission_paid_minor: i64,", "", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_paper_ledger_struct(self.config, mutated)
        self.assertIn("commission_paid_minor", str(ctx.exception))


class SimErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_sim_error_enum(self.config, self.sim_src)
        for variant in ("EmptySymbol", "NonPositivePrice", "NegativeSpread", "Overflow", "Cost"):
            self.assertIn(variant, evidence)

    def test_dropped_negative_spread_variant_is_caught(self) -> None:
        mutated = self.sim_src.replace("    NegativeSpread { ts: u64, spread_minor: i64 },", "", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_sim_error_enum(self.config, mutated)
        self.assertIn("NegativeSpread", str(ctx.exception))


class FailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_fail_closed(self.config, self.sim_src)
        self.assertIn("fails closed", evidence)

    def test_removed_validate_is_caught(self) -> None:
        mutated = self.sim_src.replace("self.cost_config.validate()?", "Ok::<(), ()>(())", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))

    def test_removed_price_guard_is_caught(self) -> None:
        # The first SimError::NonPositivePrice occurrence is the simulate_fill
        # guard (the impl precedes the enum definition in source).
        mutated = self.sim_src.replace("SimError::NonPositivePrice", "SimError::Overflow", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("NonPositivePrice", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.sim_src)
        self.assertIn("integer-only", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.sim_src.replace(
            "pub cash_delta_minor: i64,", "pub cash_delta_minor: f64,", 1
        )
        with self.assertRaises(SimCostCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_removed_wide_intermediate_is_caught(self) -> None:
        mutated = self.sim_src.replace("i128::from", "i64::from")
        with self.assertRaises(SimCostCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("i128::from", str(ctx.exception))


class SharedFamilyTest(_Fixture):
    def test_shared_family_evidence(self) -> None:
        evidence = check_shared_family(self.config, self.lib_src)
        self.assertIn("pub mod sim;", evidence)

    def test_missing_sim_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod sim;", "pub mod renamed;", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_shared_family(self.config, mutated)
        self.assertIn("sim", str(ctx.exception))

    def test_missing_cost_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod cost;", "pub mod renamed_cost;", 1)
        with self.assertRaises(SimCostCheckError) as ctx:
            check_shared_family(self.config, mutated)
        self.assertIn("cost", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.sim_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.sim_src + "\n// uses databento under the hood\n"
        with self.assertRaises(SimCostCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("databento", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable shared-cost simulation fill must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("sim_cost_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("sim_cost_check.shutil.which", return_value=None):
            with self.assertRaises(SimCostCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_ten_items(self) -> None:
        # 9 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 10)

    def test_static_evidence_is_nine_items(self) -> None:
        self.assertEqual(len(assert_sim_cost_static(load_config(), ROOT)), 9)


if __name__ == "__main__":
    unittest.main()
