"""Contract tests for SRS-BT-007 (grid search / multidimensional parameter sweeps).

SRS-BT-007 / SyRS SYS-19 / StRS SN-1.16 -- a parameter space definition produces ranked
backtest results by the selected objective function. This slice ships the deterministic
space + objective + factory-seam + runner + ranked-report surface in
``crates/atp-simulation`` (module ``sweep``), reusing the shipped ``BacktestEngine`` +
``benchmark::compare`` chain and the SRS-BT-009 ``StrategyParameters`` point identity;
the deferred halves (the real Python-strategy factory via the deferred strategy host,
the REST/dashboard sweep surface via SRS-API-001 / SRS-UI, the real stored-data
benchmark resolver via SRS-BT-005, and the SRS-BT-008 walk-forward consumer) are named
by the check script's PASS output.

Mirrors ``tests/test_backtest_store_contract.py``: shells out to
``tools/backtest_sweep_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in
memory and assert the contract actually catches the regression (a renamed space type, a
dropped validation variant, a neutered cardinality cap, a shrunken objective allowlist,
a dropped factory error vocabulary, a bypassed engine/compare reuse, a partial-order
sort, a fabricated undefined-objective fallback, an anonymous point failure, an injected
nondeterminism source, a dropped lib re-export, an injected broker dependency, a leaked
vendor token, and a direction-guessing CLI).
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

from backtest_sweep_check import (  # noqa: E402
    BacktestSweepCheckError,
    assert_backtest_sweep_static,
    cargo_source,
    check_cargo_test_smoke,
    check_cli_surface,
    check_determinism,
    check_error_enum,
    check_factory_seam,
    check_module_reexport,
    check_no_broker_dependency,
    check_none_fail_closed,
    check_objective,
    check_objective_ranking,
    check_point_cap,
    check_point_failure,
    check_runner_reuse,
    check_space_types,
    check_space_validation,
    check_vendor_isolation,
    cli_source,
    lib_source,
    load_config,
    run_checks,
    sweep_source,
)


class BacktestSweepScriptTest(unittest.TestCase):
    def test_srs_bt_007_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/backtest_sweep_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-007 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "declares ParameterAxis/ParameterSpace (the AC's 'parameter space definition')",
            "deterministic Cartesian product of canonical StrategyParameters",
            "fail-closed variant by variant",
            "checked_mul in u128",
            "BEFORE materializing a single point or running a single backtest",
            "declares ObjectiveFunction (the AC's 'selected objective function')",
            "all eight SYS-16 metrics",
            "maximize_sharpe, minimize_max_drawdown",
            "declares SweepStrategyFactory",
            "never a silent default run",
            "SAME shipped BacktestEngine::run + benchmark::compare chain",
            "SweepReport is the AC's 'ranked backtest results'",
            "f64::total_cmp (direction-driven), ties broken by canonical parameter entries",
            "total_points proving ranked + unranked accounts for every point",
            "ObjectiveUndefined -- never a fabricated 0, never ranked last, never dropped",
            "PointFailed naming the offending point",
            "declares SweepError with 15 fail-closed variants",
            "sweep is deterministic",
            "lib.rs re-exports `pub mod sweep;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "sweep module is free of all 5 forbidden vendor SDK tokens",
            "registers the bt007_sweep_cli operator binary",
            "an explicit --objective requires an explicit --direction",
            "deferred to: the real Python-strategy factory",
            "REST / dashboard sweep surface (SRS-API-001 / SRS-UI)",
            "SRS-BT-005",
            "SRS-BT-008",
            "feature_list.json keeps SRS-BT-007 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = sweep_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.cli_src = cli_source(self.config)


class MissingBlockTest(_Fixture):
    def test_missing_contract_block_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        del broken["sim_parameter_sweep_contract"]
        with self.assertRaises(BacktestSweepCheckError):
            check_space_types(broken, self.src)


class SpaceTypesTest(_Fixture):
    def test_space_evidence(self) -> None:
        evidence = check_space_types(self.config, self.src)
        self.assertIn("parameter space definition", evidence)
        self.assertIn("StrategyParameters", evidence)

    def test_renamed_space_struct_caught(self) -> None:
        mutated = self.src.replace("pub struct ParameterSpace", "pub struct ParamGrid")
        with self.assertRaises(BacktestSweepCheckError):
            check_space_types(self.config, mutated)

    def test_divergent_point_identity_caught(self) -> None:
        # Points must BE the SRS-BT-009 StrategyParameters identity, not a divergent shape.
        mutated = self.src.replace("use crate::backtest_store::StrategyParameters", "")
        with self.assertRaises(BacktestSweepCheckError):
            check_space_types(self.config, mutated)


class SpaceValidationTest(_Fixture):
    def test_validation_evidence(self) -> None:
        evidence = check_space_validation(self.config, self.src)
        self.assertIn("fail-closed variant by variant", evidence)

    def test_dropped_duplicate_axis_guard_caught(self) -> None:
        mutated = self.src.replace("DuplicateAxis", "MergedAxis")
        with self.assertRaises(BacktestSweepCheckError):
            check_space_validation(self.config, mutated)

    def test_declared_but_never_raised_guard_caught(self) -> None:
        # A variant that exists in the enum but is never raised is a vacuous guard.
        mutated = self.src.replace("return Err(SweepError::EmptySpace);", "// tolerated")
        with self.assertRaises(BacktestSweepCheckError):
            check_space_validation(self.config, mutated)


class PointCapTest(_Fixture):
    def test_cap_evidence(self) -> None:
        evidence = check_point_cap(self.config, self.src)
        self.assertIn("BEFORE materializing", evidence)

    def test_unchecked_arithmetic_caught(self) -> None:
        mutated = self.src.replace("checked_mul", "saturating_mul")
        with self.assertRaises(BacktestSweepCheckError):
            check_point_cap(self.config, mutated)

    def test_cap_after_materialization_caught(self) -> None:
        # Moving the cap check after the enumeration loop (materialize first, cap later
        # -- the unbounded-memory regression) is caught by the ordering assertion.
        early_cap = (
            "        if count > max_points as u128 {\n"
            "            return Err(SweepError::TooManyPoints {\n"
            "                count,\n"
            "                limit: max_points,\n"
            "            });\n"
            "        }\n"
        )
        self.assertIn(early_cap, self.src)
        mutated = self.src.replace(early_cap, "").replace(
            "                    return Ok(points);",
            "                    if count > max_points as u128 {\n"
            "                        return Err(SweepError::TooManyPoints {\n"
            "                            count,\n"
            "                            limit: max_points,\n"
            "                        });\n"
            "                    }\n"
            "                    return Ok(points);",
        )
        with self.assertRaises(BacktestSweepCheckError):
            check_point_cap(self.config, mutated)


class ObjectiveTest(_Fixture):
    def test_objective_evidence(self) -> None:
        evidence = check_objective(self.config, self.src)
        self.assertIn("eight SYS-16 metrics", evidence)

    def test_shrunken_metric_family_caught(self) -> None:
        mutated = self.src.replace('"sortino_ratio"', '"legacy_ratio"')
        with self.assertRaises(BacktestSweepCheckError):
            check_objective(self.config, mutated)

    def test_dropped_syrs_convenience_caught(self) -> None:
        mutated = self.src.replace("pub fn minimize_max_drawdown", "fn hidden_drawdown")
        with self.assertRaises(BacktestSweepCheckError):
            check_objective(self.config, mutated)


class FactorySeamTest(_Fixture):
    def test_factory_evidence(self) -> None:
        evidence = check_factory_seam(self.config, self.src)
        self.assertIn("fail-closed bridge", evidence)

    def test_dropped_missing_parameter_vocabulary_caught(self) -> None:
        # Removing the MissingParameter variant would let a factory silently default.
        mutated = self.src.replace("MissingParameter", "OptionalParameter")
        with self.assertRaises(BacktestSweepCheckError):
            check_factory_seam(self.config, mutated)


class RunnerReuseTest(_Fixture):
    def test_runner_evidence(self) -> None:
        evidence = check_runner_reuse(self.config, self.src)
        self.assertIn("SAME shipped", evidence)

    def test_bypassed_engine_caught(self) -> None:
        mutated = self.src.replace("BacktestEngine", "InlineReplayLoop")
        with self.assertRaises(BacktestSweepCheckError):
            check_runner_reuse(self.config, mutated)


class ObjectiveRankingTest(_Fixture):
    def test_ranking_evidence(self) -> None:
        evidence = check_objective_ranking(self.config, self.src)
        self.assertIn("total_cmp", evidence)

    def test_partial_order_sort_caught(self) -> None:
        mutated = self.src.replace("total_cmp", "partial_cmp_stub")
        with self.assertRaises(BacktestSweepCheckError):
            check_objective_ranking(self.config, mutated)

    def test_direction_ignored_caught(self) -> None:
        # A ranking that ignores the direction (always ascending) is caught.
        mutated = self.src.replace("Direction::Maximize => b.1.total_cmp(&a.1)", "_ => unreachable")
        with self.assertRaises(BacktestSweepCheckError):
            check_objective_ranking(self.config, mutated)

    def test_dropped_accounting_field_caught(self) -> None:
        mutated = self.src.replace("pub total_points", "total_points_private")
        with self.assertRaises(BacktestSweepCheckError):
            check_objective_ranking(self.config, mutated)


class NoneFailClosedTest(_Fixture):
    def test_none_evidence(self) -> None:
        evidence = check_none_fail_closed(self.config, self.src)
        self.assertIn("never a fabricated 0", evidence)

    def test_dropped_unranked_route_caught(self) -> None:
        mutated = self.src.replace("None => unranked.push(UnrankedPoint", "None => continue //(")
        with self.assertRaises(BacktestSweepCheckError):
            check_none_fail_closed(self.config, mutated)

    def test_fabricated_fallback_caught(self) -> None:
        mutated = self.src.replace(
            "request.objective.metric.value(&report.metrics)",
            "Some(request.objective.metric.value(&report.metrics).unwrap_or(0.0))",
        )
        with self.assertRaises(BacktestSweepCheckError):
            check_none_fail_closed(self.config, mutated)


class PointFailureTest(_Fixture):
    def test_point_failure_evidence(self) -> None:
        evidence = check_point_failure(self.config, self.src)
        self.assertIn("naming the offending point", evidence)

    def test_anonymous_failure_caught(self) -> None:
        mutated = self.src.replace("parameters: parameters.clone()", "reason: reason.clone()")
        with self.assertRaises(BacktestSweepCheckError):
            check_point_failure(self.config, mutated)


class ErrorEnumTest(_Fixture):
    def test_error_enum_evidence(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        self.assertIn("15 fail-closed variants", evidence)

    def test_dropped_variant_caught(self) -> None:
        mutated = self.src.replace("NonFiniteObjective", "ToleratedObjective")
        with self.assertRaises(BacktestSweepCheckError):
            check_error_enum(self.config, mutated)


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("deterministic", evidence)

    def test_injected_parallelism_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(BacktestSweepCheckError):
            check_determinism(self.config, mutated)

    def test_injected_clock_caught(self) -> None:
        mutated = self.src + "\nfn _now() { let _ = std::time::Instant::now(); }\n"
        with self.assertRaises(BacktestSweepCheckError):
            check_determinism(self.config, mutated)


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod sweep;", evidence)

    def test_dropped_reexport_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod sweep;", "mod sweep_disabled;")
        with self.assertRaises(BacktestSweepCheckError):
            check_module_reexport(self.config, mutated)


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("broker-independent", evidence)

    def test_injected_broker_dep_caught(self) -> None:
        mutated = self.cargo_src + '\natp-adapters = { path = "../atp-adapters" }\n'
        with self.assertRaises(BacktestSweepCheckError):
            check_no_broker_dependency(self.config, mutated)


class VendorIsolationTest(_Fixture):
    def test_vendor_evidence(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("forbidden vendor SDK tokens", evidence)

    def test_leaked_vendor_token_caught(self) -> None:
        mutated = self.src + "\n// ranked via ibapi under the hood\n"
        with self.assertRaises(BacktestSweepCheckError):
            check_vendor_isolation(self.config, mutated)


class CliSurfaceTest(_Fixture):
    def test_cli_evidence(self) -> None:
        evidence = check_cli_surface(self.config, self.cargo_src, self.cli_src)
        self.assertIn("bt007_sweep_cli", evidence)

    def test_unregistered_binary_caught(self) -> None:
        mutated = self.cargo_src.replace('name = "bt007_sweep_cli"', 'name = "bt007_hidden"')
        with self.assertRaises(BacktestSweepCheckError):
            check_cli_surface(self.config, mutated, self.cli_src)

    def test_direction_guessing_cli_caught(self) -> None:
        # A CLI that guesses the direction for an explicit --objective could silently
        # invert a ranking; removing the refusal is caught.
        mutated = self.cli_src.replace("--objective requires --direction", "direction defaulted")
        with self.assertRaises(BacktestSweepCheckError):
            check_cli_surface(self.config, self.cargo_src, mutated)

    def test_forgeable_kv_emission_caught(self) -> None:
        mutated = self.cli_src.replace("fn kv_field", "fn raw_field")
        with self.assertRaises(BacktestSweepCheckError):
            check_cli_surface(self.config, self.cargo_src, mutated)


class StaticSuiteTest(_Fixture):
    def test_static_suite_collects_all_evidence(self) -> None:
        evidence = assert_backtest_sweep_static(self.config)
        # 14 static checks + the CLI surface check.
        self.assertEqual(len(evidence), 15)

    def test_run_checks_appends_cargo_smoke(self) -> None:
        with mock.patch("backtest_sweep_check.shutil.which", return_value=None):
            evidence = run_checks()
        self.assertEqual(len(evidence), 16)
        self.assertIn("skipped (cargo not on PATH)", evidence[-1])


class CargoSmokeGateTest(_Fixture):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("backtest_sweep_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(self.config)
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_when_required(self) -> None:
        with mock.patch("backtest_sweep_check.shutil.which", return_value=None):
            with self.assertRaises(BacktestSweepCheckError):
                check_cargo_test_smoke(self.config, require_cargo=True)


if __name__ == "__main__":
    unittest.main()
