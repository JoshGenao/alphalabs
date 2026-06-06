"""Contract tests for SRS-SIM-003 (independent virtual position ledger per paper strategy).

SRS-SIM-003 / SyRS SYS-84 / StRS SN-1.29 / SN-1.07 — maintain an independent
virtual position ledger for each paper strategy. Acceptance: quantity, average
cost, unrealized P&L, realized P&L, and commission paid are isolated per paper
strategy and independent of IB account positions. This slice ships the ledger
math + per-strategy isolation in ``crates/atp-simulation`` (module
``virtual_ledger``); the deferred halves (the SYS-70 live feed, SYS-88 corporate
actions / SRS-DATA-021, SYS-89 persistence / SRS-SIM-004, SYS-85 paper metrics,
SRS-EXE-002 orchestrator routing, the Python runtime) keep ``feature_list.json``
at ``passes:false``.

Mirrors ``tests/test_sim_fill_contract.py``: shells out to
``tools/sim_ledger_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / Cargo.toml in memory
and assert the contract actually catches the regression (a dropped money field, a
dropped error variant, a flipped average-cost formula, a removed mark-to-market
guard, a removed proportional-basis release, a removed isolation route, an
injected float, a dropped lib re-export, an injected broker dependency, a leaked
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

from sim_ledger_check import (  # noqa: E402
    SimLedgerCheckError,
    assert_sim_ledger_static,
    cargo_source,
    check_apply_fill_accounting,
    check_average_cost,
    check_cargo_test_smoke,
    check_cash_delta,
    check_cost_tracking,
    check_ledger_book_isolation,
    check_ledger_error_enum,
    check_mark_surface,
    check_module_reexport,
    check_money_invariant,
    check_no_broker_dependency,
    check_strategy_ledger_struct,
    check_symbol_normalization,
    check_unrealized,
    check_vendor_isolation,
    check_virtual_position_struct,
    ledger_source,
    lib_source,
    load_config,
    run_checks,
)


class SimLedgerScriptTest(unittest.TestCase):
    def test_srs_sim_003_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_ledger_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SIM-003 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "VirtualPosition with a signed quantity: i64 and 5 i128 minor-unit money fields "
            "(cost_basis_minor, realized_pnl_minor, commission_paid_minor, slippage_paid_minor, "
            "spread_impact_paid_minor)",
            "StrategyLedger holding one strategy's positions keyed by symbol "
            "(positions: HashMap<String, VirtualPosition>)",
            "VirtualLedgerBook keyed by StrategyId and routes each fill to only the named "
            "strategy's ledger (self.ledgers.get_mut(strategy) / self.ledgers.insert(strategy.clone())",
            "no phantom strategy on a rejected first fill",
            "canonicalizes symbols via canonical_symbol (symbol.trim().to_uppercase()",
            "stay in ONE position rather than splitting across aliases",
            "LedgerError with 7 fail-closed variants "
            "(EmptySymbol, NonPositiveFillPrice, ZeroQuantityFill, NegativeCost, "
            "InconsistentCashDelta, NonPositiveMark, Overflow)",
            "average_cost_minor derives average cost as cost_basis / quantity",
            "unrealized_pnl_minor marks the open position to market",
            "accumulates commission separately from realized P&L",
            "reopens the remainder on a flip through zero",
            "tracks the FULL transaction-cost decomposition (commission_paid_minor, "
            "slippage_paid_minor, spread_impact_paid_minor) and sums it via "
            "transaction_cost_paid_minor",
            "reconciles exactly with the simulator's cash_delta_minor",
            "validates the public cash_delta_minor equals -(notional) - total cost before mutating",
            "exposes a symbol-keyed marking surface",
            "money is integer minor units: no f64, quantity typed i64, "
            "cost_basis_minor, realized_pnl_minor, commission_paid_minor, slippage_paid_minor, "
            "spread_impact_paid_minor typed i128",
            "lib.rs re-exports `pub mod virtual_ledger;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "virtual_ledger module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-SIM-003 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.ledger_src = ledger_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class VirtualPositionStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_virtual_position_struct(self.config, self.ledger_src)
        self.assertIn("cost_basis_minor, realized_pnl_minor, commission_paid_minor", evidence)

    def test_dropped_money_field_is_caught(self) -> None:
        mutated = self.ledger_src.replace("    cost_basis_minor: i128,", "", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_virtual_position_struct(self.config, mutated)
        self.assertIn("cost_basis_minor", str(ctx.exception))

    def test_unsigned_quantity_is_caught(self) -> None:
        # SYS-84 needs a SIGNED quantity (longs and shorts). An unsigned quantity
        # could not represent a short; the contract must catch it.
        mutated = self.ledger_src.replace("    quantity: i64,", "    quantity: u64,", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_virtual_position_struct(self.config, mutated)
        self.assertIn("quantity", str(ctx.exception))


class StrategyLedgerStructTest(_Fixture):
    def test_map_present(self) -> None:
        evidence = check_strategy_ledger_struct(self.config, self.ledger_src)
        self.assertIn("positions: HashMap<String, VirtualPosition>", evidence)

    def test_dropped_symbol_keying_is_caught(self) -> None:
        # Keying positions by something other than the symbol would merge symbols.
        mutated = self.ledger_src.replace(
            "positions: HashMap<String, VirtualPosition>",
            "positions: Vec<VirtualPosition>",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_strategy_ledger_struct(self.config, mutated)
        self.assertIn("HashMap", str(ctx.exception))


class LedgerBookIsolationTest(_Fixture):
    def test_isolation_evidence(self) -> None:
        evidence = check_ledger_book_isolation(self.config, self.ledger_src)
        self.assertIn("per-strategy isolation", evidence)

    def test_dropped_strategy_keying_is_caught(self) -> None:
        # Keying the book by anything other than StrategyId breaks per-strategy
        # isolation (SYS-84). It must be caught.
        mutated = self.ledger_src.replace(
            "ledgers: HashMap<StrategyId, StrategyLedger>",
            "ledgers: StrategyLedger",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_ledger_book_isolation(self.config, mutated)
        self.assertIn("StrategyId", str(ctx.exception))

    def test_removed_per_strategy_route_is_caught(self) -> None:
        # Inserting a new ledger under a shared key instead of strategy.clone()
        # would let a new strategy's first fill land in a shared ledger, merging
        # strategies. It must be caught.
        mutated = self.ledger_src.replace(
            "self.ledgers.insert(strategy.clone()",
            'self.ledgers.insert(StrategyId::new("shared")',
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_ledger_book_isolation(self.config, mutated)
        self.assertIn("insert", str(ctx.exception))

    def test_removed_no_phantom_guard_is_caught(self) -> None:
        # Dropping the `?` that propagates a rejected fresh-ledger fill BEFORE the
        # insert would leave a phantom strategy behind (the adversarial-review
        # finding). It must be caught.
        mutated = self.ledger_src.replace(
            "ledger.apply_fill(fill)?;", "let _ = ledger.apply_fill(fill);", 1
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_ledger_book_isolation(self.config, mutated)
        self.assertIn("phantom", str(ctx.exception))


class LedgerErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_ledger_error_enum(self.config, self.ledger_src)
        for variant in (
            "EmptySymbol",
            "NonPositiveFillPrice",
            "NegativeCost",
            "InconsistentCashDelta",
            "NonPositiveMark",
            "Overflow",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_mark_guard_variant_is_caught(self) -> None:
        mutated = self.ledger_src.replace(
            "    NonPositiveMark { mark_minor: i64 },",
            "",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_ledger_error_enum(self.config, mutated)
        self.assertIn("NonPositiveMark", str(ctx.exception))

    def test_dropped_negative_cost_variant_is_caught(self) -> None:
        mutated = self.ledger_src.replace("    NegativeCost {", "    IgnoredVariant {", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_ledger_error_enum(self.config, mutated)
        self.assertIn("NegativeCost", str(ctx.exception))


class CostTrackingTest(_Fixture):
    def test_cost_tracking_evidence(self) -> None:
        evidence = check_cost_tracking(self.config, self.ledger_src)
        self.assertIn("FULL transaction-cost decomposition", evidence)
        self.assertIn("reconciles exactly", evidence)

    def test_dropped_total_fn_is_caught(self) -> None:
        mutated = self.ledger_src.replace(
            "pub fn transaction_cost_paid_minor", "pub fn renamed_total", 1
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_cost_tracking(self.config, mutated)
        self.assertIn("transaction_cost_paid_minor", str(ctx.exception))

    def test_dropped_slippage_accumulation_is_caught(self) -> None:
        # Dropping the slippage term everywhere it is read would let a charged cost
        # silently disappear from the ledger (the round-3 reconciliation finding).
        mutated = self.ledger_src.replace("i128::from(fill.slippage_minor)", "0i128")
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_cost_tracking(self.config, mutated)
        self.assertIn("slippage", str(ctx.exception))


class CashDeltaCheckTest(_Fixture):
    def test_cash_delta_evidence(self) -> None:
        evidence = check_cash_delta(self.config, self.ledger_src)
        self.assertIn("reconciliation stays airtight", evidence)

    def test_removed_cash_delta_comparison_is_caught(self) -> None:
        # Dropping the cash-delta consistency check lets a tampered fill through
        # (the round-4 finding). It must be caught.
        mutated = self.ledger_src.replace(
            "expected_cash_delta_minor != i128::from(fill.cash_delta_minor)", "false", 1
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_cash_delta(self.config, mutated)
        self.assertIn("cash_delta", str(ctx.exception))


class MarkSurfaceTest(_Fixture):
    def test_mark_surface_evidence(self) -> None:
        evidence = check_mark_surface(self.config, self.ledger_src)
        self.assertIn("symbol-keyed marking surface", evidence)

    def test_removed_book_keyed_marking_is_caught(self) -> None:
        mutated = self.ledger_src.replace(
            "ledger.unrealized_pnl_minor(symbol, snapshot)", "todo!()", 1
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_mark_surface(self.config, mutated)
        self.assertIn("symbol-keyed", str(ctx.exception))


class SymbolNormalizationTest(_Fixture):
    def test_normalization_evidence(self) -> None:
        evidence = check_symbol_normalization(self.config, self.ledger_src)
        self.assertIn("canonicalizes symbols", evidence)

    def test_missing_canonical_helper_is_caught(self) -> None:
        mutated = self.ledger_src.replace("fn canonical_symbol", "fn renamed_symbol", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_symbol_normalization(self.config, mutated)
        self.assertIn("canonical_symbol", str(ctx.exception))

    def test_dropped_uppercase_policy_is_caught(self) -> None:
        # Dropping the upper-case (keeping only trim) would let AAPL and aapl split.
        mutated = self.ledger_src.replace(
            "symbol.trim().to_uppercase()", "symbol.trim().to_string()", 1
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_symbol_normalization(self.config, mutated)
        self.assertIn("trim + upper-case", str(ctx.exception))

    def test_non_canonical_key_is_caught(self) -> None:
        # Keying on the raw symbol instead of the canonical form would split aliases.
        mutated = self.ledger_src.replace(
            "let symbol = canonical_symbol(&fill.symbol);",
            "let symbol = fill.symbol.clone();",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_symbol_normalization(self.config, mutated)
        self.assertIn("canonical", str(ctx.exception))


class AverageCostTest(_Fixture):
    def test_average_cost_evidence(self) -> None:
        evidence = check_average_cost(self.config, self.ledger_src)
        self.assertIn("cost_basis / quantity", evidence)

    def test_flipped_average_cost_formula_is_caught(self) -> None:
        # Deriving average cost from anything but cost_basis / quantity (the signed
        # basis is the source of truth) would misreport it. The contract must catch
        # the missing basis division.
        mutated = self.ledger_src.replace(
            "Some(self.cost_basis_minor / i128::from(self.quantity))",
            "Some(0)",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_average_cost(self.config, mutated)
        self.assertIn("average_cost_minor", str(ctx.exception))


class UnrealizedTest(_Fixture):
    def test_unrealized_evidence(self) -> None:
        evidence = check_unrealized(self.config, self.ledger_src)
        self.assertIn("marks the open position to market", evidence)

    def test_removed_mark_guard_is_caught(self) -> None:
        # Dropping the non-positive-mark guard lets corrupt live data drive a
        # fabricated mark-to-market value. It must be caught.
        mutated = self.ledger_src.replace(
            "return Err(LedgerError::NonPositiveMark { mark_minor });",
            "return Ok(0);",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_unrealized(self.config, mutated)
        self.assertIn("non-positive-mark", str(ctx.exception))

    def test_dropped_mark_to_market_product_is_caught(self) -> None:
        mutated = self.ledger_src.replace(
            "i128::from(mark_minor) * i128::from(self.quantity)",
            "i128::from(self.quantity)",
            1,
        )
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_unrealized(self.config, mutated)
        self.assertIn("mark-to-market", str(ctx.exception))


class ApplyFillAccountingTest(_Fixture):
    def test_accounting_evidence(self) -> None:
        evidence = check_apply_fill_accounting(self.config, self.ledger_src)
        self.assertIn("average-cost accounting", evidence)

    def test_removed_proportional_release_is_caught(self) -> None:
        # Scaling the released basis by anything but the closed magnitude would
        # leave residual basis on a full close (or over-release). It must be caught.
        mutated = self.ledger_src.replace("checked_mul(fill_abs)", "checked_mul(1)", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_apply_fill_accounting(self.config, mutated)
        self.assertIn("checked_mul(fill_abs)", str(ctx.exception))

    def test_removed_flip_handling_is_caught(self) -> None:
        mutated = self.ledger_src.replace("q_open", "q_rest")
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_apply_fill_accounting(self.config, mutated)
        self.assertIn("flip", str(ctx.exception))

    def test_removed_cost_removed_is_caught(self) -> None:
        mutated = self.ledger_src.replace("cost_removed", "released")
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_apply_fill_accounting(self.config, mutated)
        self.assertIn("cost_removed", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_integer_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.ledger_src)
        self.assertIn("integer minor units", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.ledger_src.replace("quantity: i64,", "quantity: f64,", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))

    def test_narrowed_money_field_is_caught(self) -> None:
        # The money accumulators must be i128 so the ledger math is exact.
        mutated = self.ledger_src.replace("cost_basis_minor: i128,", "cost_basis_minor: i64,", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("cost_basis_minor", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod virtual_ledger;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod virtual_ledger;", "pub mod renamed_ledger;", 1)
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("virtual_ledger", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("independent of the IB account", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.ledger_src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.ledger_src + "\n// positions reconciled against ib_insync under the hood\n"
        with self.assertRaises(SimLedgerCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable virtual-ledger path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("sim_ledger_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("sim_ledger_check.shutil.which", return_value=None):
            with self.assertRaises(SimLedgerCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_sixteen_items(self) -> None:
        # 15 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 16)

    def test_static_evidence_is_fifteen_items(self) -> None:
        self.assertEqual(len(assert_sim_ledger_static(load_config(), ROOT)), 15)


if __name__ == "__main__":
    unittest.main()
