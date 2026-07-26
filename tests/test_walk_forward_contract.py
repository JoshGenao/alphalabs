"""Contract tests for SRS-BT-008 (walk-forward analysis).

SRS-BT-008 / SyRS SYS-20 / StRS SN-1.17 -- in-sample windows are optimized, out-of-sample
windows are evaluated, and outputs preserve the parameter set and metrics per window. This
slice ships the deterministic schedule + no-lookahead + runner + fold-report surface in
``crates/atp-simulation`` (module ``walk_forward``), reusing the shipped SRS-BT-007
``SweepRunner`` for BOTH the in-sample optimization and the out-of-sample evaluation; the
deferred halves (the real Python-strategy factory via the deferred strategy host, the
REST/dashboard surface via SRS-API-001 / SRS-UI, the real stored-data benchmark resolver via
SRS-BT-005, and fold persistence via the SRS-BT-009 consumer boundary) are named by the check
script's PASS output.

Mirrors ``tests/test_backtest_sweep_contract.py``: shells out to
``tools/walk_forward_check.py``, then exercises each per-check function in-process, including
negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in memory and assert
the contract actually catches the regression (a dropped no-lookahead guard, a bypassed
SweepRunner reuse, a fabricated out-of-sample objective, a neutered rolling cardinality
arithmetic, a dropped fold accounting field, an injected nondeterminism source, a dropped lib
re-export, an injected broker dependency, a leaked vendor token, and a direction-guessing
CLI).
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

from walk_forward_check import (  # noqa: E402
    WalkForwardCheckError,
    assert_walk_forward_static,
    cargo_source,
    check_cargo_test_smoke,
    check_cli_surface,
    check_determinism,
    check_error_enum,
    check_in_sample_optimization,
    check_module_reexport,
    check_no_broker_dependency,
    check_no_fabrication,
    check_no_lookahead,
    check_out_of_sample_evaluation,
    check_point_failure,
    check_preservation,
    check_rolling_generator,
    check_runner_reuse,
    check_schedule_validation,
    check_vendor_isolation,
    check_window_types,
    cli_source,
    lib_source,
    load_config,
    run_checks,
    walk_forward_source,
)


class WalkForwardScriptTest(unittest.TestCase):
    def test_srs_bt_008_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/walk_forward_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-008 WALK-FORWARD PASS", result.stdout)
        for needle in (
            "declares WalkForwardWindow/WalkForwardSchedule over the shipped backtest DateRange",
            "enforces the walk-forward NO-LOOKAHEAD invariant",
            "in_sample.end < out_of_sample.start and fails closed with LookaheadWindow",
            "WalkForwardSchedule::new is fail-closed",
            "overflow-checked boundary arithmetic",
            "owns a private SweepRunner and evaluates every fold through it",
            "no re-implementation of BacktestEngine::run or benchmark::compare",
            "taking report.ranked.first() (the rank-1 optimum)",
            "fails closed with NoOptimum",
            "the out-of-sample objective is Option<f64>",
            "never a fabricated stand-in",
            "preserves, per window, the selected parameter set and both the in-sample and "
            "out-of-sample metrics",
            "total_folds proves every scheduled fold is accounted for",
            "InSampleSweepFailed / OutOfSampleEvalFailed) naming the offending window",
            "declares WalkForwardError with 14 fail-closed variants",
            "walk_forward is deterministic",
            "lib.rs re-exports `pub mod walk_forward;`",
            "Cargo.toml declares no dependency on the live/broker path "
            "(atp-adapters, atp-execution)",
            "walk_forward module is free of all 5 forbidden vendor SDK tokens",
            "registers the bt008_walk_forward_cli operator binary",
            "an explicit --objective requires an explicit --direction",
            "deferred to: the real Python-strategy factory",
            "REST / dashboard walk-forward surface (SRS-API-001 / SRS-UI)",
            "SRS-BT-005",
            "SRS-BT-009",
            "feature_list.json keeps SRS-BT-008 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = walk_forward_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.cli_src = cli_source(self.config)


class MissingBlockTest(_Fixture):
    def test_missing_contract_block_fails(self) -> None:
        broken = copy.deepcopy(self.config)
        del broken["sim_walk_forward_contract"]
        with self.assertRaises(WalkForwardCheckError):
            check_window_types(broken, self.src)


class WindowTypesTest(_Fixture):
    def test_window_evidence(self) -> None:
        evidence = check_window_types(self.config, self.src)
        self.assertIn("WalkForwardWindow/WalkForwardSchedule", evidence)

    def test_renamed_window_struct_caught(self) -> None:
        mutated = self.src.replace("pub struct WalkForwardWindow", "pub struct Fold")
        with self.assertRaises(WalkForwardCheckError):
            check_window_types(self.config, mutated)

    def test_divergent_date_range_import_caught(self) -> None:
        mutated = self.src.replace(
            "use crate::backtest::{BacktestRequest, BarSource, DateRange}", ""
        )
        with self.assertRaises(WalkForwardCheckError):
            check_window_types(self.config, mutated)


class NoLookaheadTest(_Fixture):
    def test_no_lookahead_evidence(self) -> None:
        evidence = check_no_lookahead(self.config, self.src)
        self.assertIn("NO-LOOKAHEAD", evidence)

    def test_dropped_lookahead_guard_caught(self) -> None:
        # Removing the guard (allowing out-of-sample to overlap in-sample) is the
        # lookahead-bias safety regression -- it must be caught.
        mutated = self.src.replace(
            "if self.in_sample.end >= self.out_of_sample.start {", "if false {"
        )
        with self.assertRaises(WalkForwardCheckError):
            check_no_lookahead(self.config, mutated)

    def test_declared_but_never_raised_lookahead_caught(self) -> None:
        mutated = self.src.replace("return Err(WalkForwardError::LookaheadWindow", "// tolerated (")
        with self.assertRaises(WalkForwardCheckError):
            check_no_lookahead(self.config, mutated)


class ScheduleValidationTest(_Fixture):
    def test_schedule_validation_evidence(self) -> None:
        evidence = check_schedule_validation(self.config, self.src)
        self.assertIn("fail-closed", evidence)

    def test_dropped_overlap_guard_caught(self) -> None:
        mutated = self.src.replace("OverlappingFolds", "MergedFolds")
        with self.assertRaises(WalkForwardCheckError):
            check_schedule_validation(self.config, mutated)

    def test_declared_but_never_raised_empty_caught(self) -> None:
        mutated = self.src.replace("return Err(WalkForwardError::EmptySchedule);", "// tolerated")
        with self.assertRaises(WalkForwardCheckError):
            check_schedule_validation(self.config, mutated)


class RollingGeneratorTest(_Fixture):
    def test_rolling_evidence(self) -> None:
        evidence = check_rolling_generator(self.config, self.src)
        self.assertIn("overflow-checked", evidence)

    def test_unchecked_arithmetic_caught(self) -> None:
        mutated = self.src.replace("checked_add", "wrapping_add_stub")
        with self.assertRaises(WalkForwardCheckError):
            check_rolling_generator(self.config, mutated)

    def test_dropped_funnel_through_new_caught(self) -> None:
        # A rolling generator that returns windows WITHOUT re-validating through new()
        # could emit an overlapping/lookahead schedule -- caught.
        mutated = self.src.replace("Self::new(windows)", "Ok(Self { windows })")
        with self.assertRaises(WalkForwardCheckError):
            check_rolling_generator(self.config, mutated)

    def test_dropped_fold_cap_caught(self) -> None:
        # Removing the pre-allocation fold cap (the OOM/panic-from-unbounded-count regression)
        # is caught.
        mutated = self.src.replace("return Err(WalkForwardError::TooManyFolds", "// no cap (")
        with self.assertRaises(WalkForwardCheckError):
            check_rolling_generator(self.config, mutated)

    def test_fold_cap_after_allocation_caught(self) -> None:
        # Moving the cap check AFTER Vec::with_capacity (allocate first, cap later -- the
        # unbounded-allocation regression) is caught by the ordering assertion.
        cap = (
            "        if fold_count > MAX_WALK_FORWARD_FOLDS {\n"
            "            return Err(WalkForwardError::TooManyFolds {\n"
            "                count: fold_count,\n"
            "                limit: MAX_WALK_FORWARD_FOLDS,\n"
            "            });\n"
            "        }\n"
        )
        self.assertIn(cap, self.src)
        mutated = self.src.replace(cap, "").replace(
            "let mut windows = Vec::with_capacity(fold_count);",
            "let mut windows = Vec::with_capacity(fold_count);\n" + cap,
        )
        with self.assertRaises(WalkForwardCheckError):
            check_rolling_generator(self.config, mutated)


class RunnerReuseTest(_Fixture):
    def test_runner_evidence(self) -> None:
        evidence = check_runner_reuse(self.config, self.src)
        self.assertIn("private SweepRunner", evidence)

    def test_reimplemented_engine_caught(self) -> None:
        # Re-implementing the backtest/compare chain instead of reusing SweepRunner is
        # caught by the forbidden-token guard.
        mutated = self.src + "\nfn _leak() { let _ = benchmark::compare; }\n"
        with self.assertRaises(WalkForwardCheckError):
            check_runner_reuse(self.config, mutated)

    def test_dropped_sweep_field_caught(self) -> None:
        mutated = self.src.replace("sweep: SweepRunner", "engine: InlineReplayLoop")
        with self.assertRaises(WalkForwardCheckError):
            check_runner_reuse(self.config, mutated)


class InSampleOptimizationTest(_Fixture):
    def test_in_sample_evidence(self) -> None:
        evidence = check_in_sample_optimization(self.config, self.src)
        self.assertIn("rank-1 optimum", evidence)

    def test_dropped_rank_one_caught(self) -> None:
        mutated = self.src.replace("in_report.ranked.first()", "in_report.unranked.first()")
        with self.assertRaises(WalkForwardCheckError):
            check_in_sample_optimization(self.config, mutated)

    def test_dropped_no_optimum_caught(self) -> None:
        mutated = self.src.replace("NoOptimum", "SilentDefault")
        with self.assertRaises(WalkForwardCheckError):
            check_in_sample_optimization(self.config, mutated)


class OutOfSampleEvaluationTest(_Fixture):
    def test_out_of_sample_evidence(self) -> None:
        evidence = check_out_of_sample_evaluation(self.config, self.src)
        self.assertIn("single-point ParameterSpace", evidence)

    def test_dropped_option_objective_caught(self) -> None:
        # Making the out-of-sample objective non-optional would force a fabricated value
        # when the metric is undefined on the out-of-sample window.
        mutated = self.src.replace(
            "pub out_of_sample_objective: Option<f64>,", "pub out_of_sample_objective: f64,"
        )
        with self.assertRaises(WalkForwardCheckError):
            check_out_of_sample_evaluation(self.config, mutated)


class NoFabricationTest(_Fixture):
    def test_no_fabrication_evidence(self) -> None:
        evidence = check_no_fabrication(self.config, self.src)
        self.assertIn("honestly None", evidence)

    def test_fabricated_fallback_caught(self) -> None:
        mutated = self.src.replace(
            "(None, point.metrics.clone(), point.comparison.clone())",
            "(Some(point.metrics.win_rate.unwrap_or(0.0)), point.metrics.clone(), "
            "point.comparison.clone())",
        )
        with self.assertRaises(WalkForwardCheckError):
            check_no_fabrication(self.config, mutated)


class PointFailureTest(_Fixture):
    def test_point_failure_evidence(self) -> None:
        evidence = check_point_failure(self.config, self.src)
        self.assertIn("naming the offending window", evidence)

    def test_anonymous_failure_caught(self) -> None:
        mutated = self.src.replace("window: *window", "reason: reason")
        with self.assertRaises(WalkForwardCheckError):
            check_point_failure(self.config, mutated)


class PreservationTest(_Fixture):
    def test_preservation_evidence(self) -> None:
        evidence = check_preservation(self.config, self.src)
        self.assertIn("preserves", evidence)

    def test_dropped_accounting_field_caught(self) -> None:
        mutated = self.src.replace("pub total_folds", "total_folds_private")
        with self.assertRaises(WalkForwardCheckError):
            check_preservation(self.config, mutated)

    def test_dropped_selected_params_caught(self) -> None:
        mutated = self.src.replace("pub selected_parameters:", "pub dropped_parameters:")
        with self.assertRaises(WalkForwardCheckError):
            check_preservation(self.config, mutated)


class ErrorEnumTest(_Fixture):
    def test_error_enum_evidence(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        self.assertIn("14 fail-closed variants", evidence)

    def test_dropped_variant_caught(self) -> None:
        mutated = self.src.replace("SingletonRebuild", "ToleratedRebuild")
        with self.assertRaises(WalkForwardCheckError):
            check_error_enum(self.config, mutated)


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("deterministic", evidence)

    def test_injected_parallelism_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(WalkForwardCheckError):
            check_determinism(self.config, mutated)

    def test_injected_clock_caught(self) -> None:
        mutated = self.src + "\nfn _now() { let _ = std::time::Instant::now(); }\n"
        with self.assertRaises(WalkForwardCheckError):
            check_determinism(self.config, mutated)


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod walk_forward;", evidence)

    def test_dropped_reexport_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod walk_forward;", "mod walk_forward_disabled;")
        with self.assertRaises(WalkForwardCheckError):
            check_module_reexport(self.config, mutated)


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("broker-independent", evidence)

    def test_injected_broker_dep_caught(self) -> None:
        mutated = self.cargo_src + '\natp-adapters = { path = "../atp-adapters" }\n'
        with self.assertRaises(WalkForwardCheckError):
            check_no_broker_dependency(self.config, mutated)


class VendorIsolationTest(_Fixture):
    def test_vendor_evidence(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("forbidden vendor SDK tokens", evidence)

    def test_leaked_vendor_token_caught(self) -> None:
        mutated = self.src + "\n// evaluated via ibapi under the hood\n"
        with self.assertRaises(WalkForwardCheckError):
            check_vendor_isolation(self.config, mutated)


class CliSurfaceTest(_Fixture):
    def test_cli_evidence(self) -> None:
        evidence = check_cli_surface(self.config, self.cargo_src, self.cli_src)
        self.assertIn("bt008_walk_forward_cli", evidence)

    def test_unregistered_binary_caught(self) -> None:
        mutated = self.cargo_src.replace('name = "bt008_walk_forward_cli"', 'name = "bt008_hidden"')
        with self.assertRaises(WalkForwardCheckError):
            check_cli_surface(self.config, mutated, self.cli_src)

    def test_direction_guessing_cli_caught(self) -> None:
        mutated = self.cli_src.replace("--objective requires --direction", "direction defaulted")
        with self.assertRaises(WalkForwardCheckError):
            check_cli_surface(self.config, self.cargo_src, mutated)

    def test_forgeable_kv_emission_caught(self) -> None:
        mutated = self.cli_src.replace("fn kv_field", "fn raw_field")
        with self.assertRaises(WalkForwardCheckError):
            check_cli_surface(self.config, self.cargo_src, mutated)


class StaticSuiteTest(_Fixture):
    def test_static_suite_collects_all_evidence(self) -> None:
        evidence = assert_walk_forward_static(self.config)
        # 15 static checks + the CLI surface check.
        self.assertEqual(len(evidence), 16)

    def test_run_checks_appends_cargo_smoke(self) -> None:
        with mock.patch("walk_forward_check.shutil.which", return_value=None):
            evidence = run_checks()
        self.assertEqual(len(evidence), 17)
        self.assertIn("skipped (cargo not on PATH)", evidence[-1])


class CargoSmokeGateTest(_Fixture):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("walk_forward_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(self.config)
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_when_required(self) -> None:
        with mock.patch("walk_forward_check.shutil.which", return_value=None):
            with self.assertRaises(WalkForwardCheckError):
                check_cargo_test_smoke(self.config, require_cargo=True)


if __name__ == "__main__":
    unittest.main()
