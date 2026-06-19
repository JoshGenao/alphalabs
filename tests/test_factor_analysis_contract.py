"""Contract tests for SRS-BT-006 (produce factor analysis and tear-sheet outputs).

SRS-BT-006 / SyRS SYS-18 / StRS SN-1.05 -- factor returns, information coefficient, and
turnover analysis for completed factor-analysis runs. This slice ships the deterministic
factor-analysis surface in ``crates/atp-factor-pipeline`` (module ``factor_analysis``) and the
operator RENDERING surface that reads a completed run (the ``factor_tear_sheet_cli`` binary,
pinned by the ``srs_bt_006_tear_sheet_cli`` integration test), so ``feature_list.json`` is now
``passes:true``. The still-deferred halves (the scheduled full-universe factor job via
SRS-FAC-001, the real factor/return data wiring via SRS-DATA-007, the REST/dashboard rendering
half via SRS-UI / SRS-API, and the cross-crate SRS-BT-004 metrics bundle) are owned by those
features, not SRS-BT-006.

Mirrors ``tests/test_benchmark_contract.py``: shells out to
``tools/factor_analysis_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in memory
and assert the contract actually catches the regression (a dropped struct field, a non-Option
IC summary, a per-period IC demoted off Option, a renamed compute fn, a dropped error
variant, a dropped IC domain clamp, a removed trust-boundary guard, an injected
nondeterminism source, a dropped NaN guard, a money-into-int factor input, a dropped lib
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

from factor_analysis_check import (  # noqa: E402
    FactorAnalysisCheckError,
    assert_factor_analysis_static,
    cargo_source,
    check_cargo_test_smoke,
    check_compute_fn,
    check_determinism,
    check_error_enum,
    check_factor_returns,
    check_information_coefficient,
    check_module_reexport,
    check_nan_guard,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_observation,
    check_panel,
    check_period,
    check_separation,
    check_spearman,
    check_tear_sheet,
    check_tear_sheet_cli,
    check_trust_boundary,
    check_turnover,
    check_vendor_isolation,
    lib_source,
    load_config,
    module_source,
    run_checks,
)


class FactorAnalysisScriptTest(unittest.TestCase):
    def test_srs_bt_006_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/factor_analysis_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-BT-006 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "declares FactorObservation",
            "declares FactorPeriod",
            "declares FactorPanel (periods + quantiles) with a fail-closed",
            "declares InformationCoefficient",
            "declares FactorReturns",
            "the compounded cumulative spread is deferred",
            "declares TurnoverAnalysis",
            "measured as half the L1 distance between the equal-weight target portfolios",
            "declares FactorTearSheet bundling the IC, factor-return, and turnover",
            "exposes `pub fn compute_tear_sheet",
            "declares FactorAnalysisError with 8 fail-closed variants",
            "computes the IC as Spearman = Pearson of average tie ranks",
            "gates the spread and turnover on per-bucket factor-cleanliness",
            "each turnover series is gated only on ITS OWN bucket",
            "a constant/tied factor exposes no identity-driven ladder",
            "turnover as Option<f64> (None when not factor-driven)",
            "FactorPanel::validate fails closed at the trust boundary",
            "factor analysis is deterministic",
            "verifies every computed aggregate AND every quantile mean is finite",
            "keeps factor scores and forward returns as dimensionless f64",
            "lib.rs re-exports `pub mod factor_analysis;`",
            "Cargo.toml declares no dependency on the live/broker/simulation path",
            "factor_analysis module is free of all 5 forbidden vendor SDK tokens",
            "registers the factor_tear_sheet_cli operator binary (defaults + run)",
            "the CLI half of the SYS-18 operator rendering surface",
            "feature_list.json has SRS-BT-006 passes:true",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = module_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class ObservationTest(_Fixture):
    def test_observation_evidence(self) -> None:
        self.assertIn("FactorObservation", check_observation(self.config, self.src))

    def test_dropped_field_is_caught(self) -> None:
        mutated = self.src.replace("pub forward_return: f64,", "pub fwd: f64,", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_observation(self.config, mutated)
        self.assertIn("forward_return", str(ctx.exception))


class PeriodTest(_Fixture):
    def test_period_evidence(self) -> None:
        self.assertIn("FactorPeriod", check_period(self.config, self.src))


class PanelTest(_Fixture):
    def test_panel_evidence(self) -> None:
        self.assertIn("fail-closed validate", check_panel(self.config, self.src))

    def test_missing_validate_is_caught(self) -> None:
        mutated = self.src.replace("pub fn validate", "fn validate", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_panel(self.config, mutated)
        self.assertIn("validate", str(ctx.exception))


class InformationCoefficientTest(_Fixture):
    def test_ic_evidence(self) -> None:
        evidence = check_information_coefficient(self.config, self.src)
        self.assertIn("Spearman IC", evidence)

    def test_non_option_summary_is_caught(self) -> None:
        # Typing the risk-adjusted IC as bare f64 would force a fabricated value on
        # degenerate input (no defined IC).
        mutated = self.src.replace("pub risk_adjusted: Option<f64>,", "pub risk_adjusted: f64,", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_information_coefficient(self.config, mutated)
        self.assertIn("risk_adjusted", str(ctx.exception))

    def test_per_period_demoted_off_option_is_caught(self) -> None:
        # Dropping Option from the per-period IC would force a fabricated zero for a
        # zero-dispersion period instead of an honest None.
        mutated = self.src.replace(
            "pub per_period: Vec<(u64, Option<f64>)>,",
            "pub per_period: Vec<(u64, f64)>,",
            1,
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_information_coefficient(self.config, mutated)
        self.assertIn("per_period", str(ctx.exception))


class FactorReturnsTest(_Fixture):
    def test_factor_returns_evidence(self) -> None:
        self.assertIn("long-short spread", check_factor_returns(self.config, self.src))

    def test_dropped_spread_field_is_caught(self) -> None:
        mutated = self.src.replace("pub spread_per_period:", "pub spread_dropped:", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_factor_returns(self.config, mutated)
        self.assertIn("spread_per_period", str(ctx.exception))

    def test_spread_demoted_off_option_is_caught(self) -> None:
        # Typing the spread as bare f64 would force a fabricated SecurityKey-driven value for a
        # period whose factor does not separate the extremes (Codex finding #2).
        mutated = self.src.replace(
            "pub spread_per_period: Vec<(u64, Option<f64>)>,",
            "pub spread_per_period: Vec<(u64, f64)>,",
            1,
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_factor_returns(self.config, mutated)
        self.assertIn("Option<f64>", str(ctx.exception))

    def test_per_quantile_mean_demoted_off_option_is_caught(self) -> None:
        # Typing the quantile means as bare f64 would publish an identity-driven ladder for a
        # constant/tied factor (Codex round-5 finding).
        mutated = self.src.replace(
            "pub per_quantile_mean: Vec<Vec<Option<f64>>>,",
            "pub per_quantile_mean: Vec<Vec<f64>>,",
            1,
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_factor_returns(self.config, mutated)
        self.assertIn("per_quantile_mean", str(ctx.exception))


class TurnoverTest(_Fixture):
    def test_turnover_evidence(self) -> None:
        self.assertIn(
            "half the L1 distance between the equal-weight target portfolios",
            check_turnover(self.config, self.src),
        )

    def test_turnover_demoted_off_option_is_caught(self) -> None:
        mutated = self.src.replace(
            "pub top_turnover: Vec<(u64, Option<f64>)>,",
            "pub top_turnover: Vec<(u64, f64)>,",
            1,
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_turnover(self.config, mutated)
        self.assertIn("Option<f64>", str(ctx.exception))

    def test_set_based_turnover_is_caught(self) -> None:
        # Dropping the retained-name weight-change term reverts to a set-membership ratio, which
        # understates turnover when the universe size changes (Codex round-3 finding).
        mutated = self.src.replace("(current_weight - previous_weight).abs()", "0.0_f64", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_turnover(self.config, mutated)
        self.assertIn("current_weight - previous_weight", str(ctx.exception))


class SeparationTest(_Fixture):
    def test_separation_evidence(self) -> None:
        self.assertIn("per-bucket factor-cleanliness", check_separation(self.config, self.src))

    def test_dropped_per_cutoff_check_is_caught(self) -> None:
        # Removing the per-cutoff cleanliness fn collapses the factor-separation gate, so a
        # constant or cutoff-tied factor could fabricate a spread/ladder again.
        mutated = self.src.replace("let cutoff_clean", "let always_clean")
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_separation(self.config, mutated)
        self.assertIn("cutoff_clean", str(ctx.exception))

    def test_dropped_spread_gate_is_caught(self) -> None:
        # Dropping the `separates` arm of the spread match (taking the spread regardless)
        # reintroduces the false-alpha bug.
        mutated = self.src.replace(
            "(true, Some(bottom), Some(top))", "(_, Some(bottom), Some(top))", 1
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_separation(self.config, mutated)
        self.assertIn("(true, Some(bottom), Some(top))", str(ctx.exception))

    def test_jointly_gated_top_turnover_is_caught(self) -> None:
        # Gating the top turnover on the BOTTOM bucket's cleanliness (instead of its own) would
        # suppress an unambiguous top turnover on a bottom-only tie (Codex round-6 finding).
        mutated = self.src.replace(
            "top_clean && previous_top_clean", "bottom_clean && previous_bottom_clean", 1
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_separation(self.config, mutated)
        self.assertIn("top_clean && previous_top_clean", str(ctx.exception))


class TearSheetTest(_Fixture):
    def test_tear_sheet_evidence(self) -> None:
        self.assertIn("bundling the IC", check_tear_sheet(self.config, self.src))


class ComputeFnTest(_Fixture):
    def test_compute_evidence(self) -> None:
        self.assertIn("single SRS-BT-006 entry point", check_compute_fn(self.config, self.src))

    def test_renamed_compute_is_caught(self) -> None:
        mutated = self.src.replace("pub fn compute_tear_sheet(", "pub fn renamed(", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_compute_fn(self.config, mutated)
        self.assertIn("compute_tear_sheet", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in (
            "EmptyPanel",
            "DuplicateSecurity",
            "InsufficientSecurities",
            "NonFiniteComputation",
        ):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        # Rename every occurrence so the variant is absent from the enum body (the
        # construction site in validate() precedes the enum declaration in file order).
        mutated = self.src.replace("InsufficientSecurities", "InsufficientDropped")
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("InsufficientSecurities", str(ctx.exception))


class SpearmanTest(_Fixture):
    def test_spearman_evidence(self) -> None:
        self.assertIn("average tie ranks", check_spearman(self.config, self.src))

    def test_dropped_domain_clamp_is_caught(self) -> None:
        # Removing the [-1, 1] clamp would let FP overflow leak an out-of-domain IC.
        mutated = self.src.replace("correlation.clamp(-1.0, 1.0)", "correlation", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_spearman(self.config, mutated)
        self.assertIn("[-1, 1]", str(ctx.exception))

    def test_renamed_rank_fn_is_caught(self) -> None:
        mutated = self.src.replace("fn average_ranks", "fn ranks_renamed")
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_spearman(self.config, mutated)
        self.assertIn("average_ranks", str(ctx.exception))


class TrustBoundaryTest(_Fixture):
    def test_trust_boundary_evidence(self) -> None:
        self.assertIn(
            "fails closed at the trust boundary", check_trust_boundary(self.config, self.src)
        )

    def test_dropped_duplicate_guard_is_caught(self) -> None:
        mutated = self.src.replace(
            "FactorAnalysisError::DuplicateSecurity", "FactorAnalysisError::EmptyPanel"
        )
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_trust_boundary(self.config, mutated)
        self.assertIn("DuplicateSecurity", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        self.assertIn("deterministic", check_determinism(self.config, self.src))

    def test_injected_parallelism_is_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("nondeterminism", str(ctx.exception))


class NanGuardTest(_Fixture):
    def test_nan_guard_evidence(self) -> None:
        self.assertIn("finite", check_nan_guard(self.config, self.src))

    def test_removed_finite_check_is_caught(self) -> None:
        mutated = self.src.replace("is_finite()", "is_nan()")
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_nan_guard(self.config, mutated)
        self.assertIn("finite", str(ctx.exception))

    def test_removed_quantile_mean_guard_is_caught(self) -> None:
        # Dropping the per-bucket finiteness guard lets a middle-quantile mean overflow to
        # inf and slip into a successful tear sheet (Codex finding #1).
        mutated = self.src.replace('finite("quantile_mean"', 'skip("quantile_mean"', 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_nan_guard(self.config, mutated)
        self.assertIn("quantile_mean", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_numeric_boundary_evidence(self) -> None:
        self.assertIn("dimensionless f64", check_numeric_boundary(self.config, self.src))

    def test_money_into_int_factor_input_is_caught(self) -> None:
        # Demote every factor_value site to i64 (the struct field and the constructor param)
        # so the dimensionless-f64 token is genuinely absent.
        mutated = self.src.replace("factor_value: f64", "factor_value: i64")
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("factor value", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        self.assertIn("pub mod factor_analysis;", check_module_reexport(self.config, self.lib_src))

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod factor_analysis;", "pub mod renamed;", 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("factor_analysis", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        self.assertIn("independent", check_no_broker_dependency(self.config, self.cargo_src))

    def test_injected_simulation_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-simulation = { path = "../atp-simulation" }\n'
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-simulation", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// factor values streamed through ib_insync under the hood\n"
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class TearSheetCliTest(_Fixture):
    def test_cli_surface_evidence(self) -> None:
        evidence = check_tear_sheet_cli(self.config, self.cargo_src)
        self.assertIn("factor_tear_sheet_cli operator binary", evidence)
        self.assertIn("CLI half of the SYS-18 operator rendering surface", evidence)

    def test_unregistered_cli_bin_is_caught(self) -> None:
        mutated = self.cargo_src.replace('name = "factor_tear_sheet_cli"', 'name = "x"', 1)
        with self.assertRaises(FactorAnalysisCheckError) as ctx:
            check_tear_sheet_cli(self.config, mutated)
        self.assertIn("factor_tear_sheet_cli", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable factor-analysis path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("factor_analysis_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("factor_analysis_check.shutil.which", return_value=None):
            with self.assertRaises(FactorAnalysisCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_twenty_items(self) -> None:
        # 19 static (incl. the tear_sheet_cli operator surface) + 1 cargo smoke (or skipped marker
        # if cargo absent).
        self.assertEqual(len(run_checks()), 20)

    def test_static_evidence_is_nineteen_items(self) -> None:
        self.assertEqual(len(assert_factor_analysis_static(load_config(), ROOT)), 19)


if __name__ == "__main__":
    unittest.main()
