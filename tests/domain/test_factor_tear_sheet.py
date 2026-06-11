"""SRS-BT-006 / SyRS SYS-18 -- a completed factor-analysis run produces trustworthy
factor returns, information coefficient, and turnover analysis: each per-period IC sits in
its mathematical [-1, 1] domain, each quantile turnover sits in [0, 1], the long-short
spread is exactly the top-minus-bottom quantile difference, an undefined statistic is None
(never a fabricated zero), the computation is deterministic, and the surface is independent
of the IB account.

L7 domain (safety) test. The acceptance criterion's safety core is that the factor
statistics an operator ranks and allocates research/capital on are *trustworthy*: an IC
outside [-1, 1] or a turnover outside [0, 1] is a corrupt statistic that would mis-rank a
factor; a fabricated zero in place of an honest "undefined" would silently overstate a
factor's quality on a degenerate cross-section; a non-deterministic tear-sheet would make a
factor's apparent quality depend on run order; and a leaked broker/vendor dependency would
let the offline factor analysis be reconciled against the live IB account. This test proves
the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration test
     ``crates/atp-factor-pipeline/tests/srs_bt_006_factor_analysis.rs`` and asserts that the
     end-to-end tear-sheet is coherent (IC == 1 on a perfectly-ranking factor, the
     quantile spread is the top-minus-bottom difference, turnover tracks membership churn),
     that repeated runs are identical (determinism), and that a degenerate or non-finite
     panel fails closed.

  2. Structural -- it asserts, via ``tools/factor_analysis_check.py``, that the
     ``atp-factor-pipeline`` crate declares no live/broker/simulation dependency, that the
     ``factor_analysis`` module leaks no vendor SDK token, that it uses no nondeterminism
     source, that it clamps the IC to its [-1, 1] domain, that it gates the spread/turnover on
     a strict extreme-separation predicate (so a constant or cutoff-tied factor cannot
     fabricate alpha), that it fails closed at the trust boundary, and that it verifies every
     aggregate AND every per-quantile mean is finite.

Each structural guard is checked for non-vacuity: an injected dependency, a leaked vendor
token, an injected nondeterminism source, a removed IC domain clamp, a removed separation
predicate / spread gate, a removed trust-boundary guard, and a removed finite check (aggregate
and per-quantile) are each shown to be caught.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from factor_analysis_check import (  # noqa: E402
    FactorAnalysisCheckError,
    cargo_source,
    check_determinism,
    check_factor_returns,
    check_nan_guard,
    check_no_broker_dependency,
    check_separation,
    check_spearman,
    check_trust_boundary,
    check_turnover,
    check_vendor_isolation,
    load_config,
    module_source,
)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-factor-pipeline",
            "--test",
            "srs_bt_006_factor_analysis",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_one_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output for {label}:\n{combined}"


def test_tear_sheet_is_coherent_end_to_end() -> None:
    # The safety core: IC in [-1, 1] (== 1 on a perfectly-ranking factor), the quantile
    # spread is the top-minus-bottom difference, turnover in [0, 1] (== 0 on stable
    # membership), and an undefined risk-adjusted IC is None, never a fabricated value.
    _assert_one_passed(
        _run_cargo_test("end_to_end_tear_sheet_is_coherent"),
        "SRS-BT-006 coherent tear-sheet",
    )


def test_turnover_tracks_membership_churn() -> None:
    # Turnover is the quantile membership churn: a full top/bottom swap yields turnover 1.0.
    _assert_one_passed(
        _run_cargo_test("turnover_tracks_membership_churn_across_periods"),
        "SRS-BT-006 turnover churn",
    )


def test_computation_is_deterministic() -> None:
    # Identical inputs must produce identical tear-sheets, or a factor's apparent quality
    # would depend on run order (SRS-BT-010).
    _assert_one_passed(
        _run_cargo_test("computation_is_deterministic_across_runs"),
        "SRS-BT-006 determinism",
    )


def test_degenerate_panel_fails_closed() -> None:
    # A period with fewer securities than quantiles cannot fill every bucket and is rejected
    # rather than producing an undefined quantile mean.
    _assert_one_passed(
        _run_cargo_test("degenerate_panel_fails_closed"),
        "SRS-BT-006 degenerate fail-closed",
    )


def test_non_finite_input_fails_closed() -> None:
    # A non-finite factor value or return is rejected at the trust boundary, never folded
    # into a poison statistic.
    _assert_one_passed(
        _run_cargo_test("non_finite_input_fails_closed"),
        "SRS-BT-006 non-finite fail-closed",
    )


def test_constant_factor_withholds_spread_and_turnover() -> None:
    # Safety: a factor with no ranking signal (all equal) must NOT produce a non-zero spread
    # or turnover driven by the SecurityKey tiebreak -- that would present false alpha as
    # factor performance. The spread/turnover are withheld (None).
    _assert_one_passed(
        _run_cargo_test("constant_factor_withholds_spread_and_turnover"),
        "SRS-BT-006 constant-factor withholds spread",
    )


def test_extreme_returns_in_a_quantile_fail_closed() -> None:
    # Safety: a quantile mean that overflows to infinity on finite inputs (a non-edge bucket
    # that never reaches the spread guard) must fail closed, never leak a poison value into a
    # successful tear sheet.
    _assert_one_passed(
        _run_cargo_test("extreme_returns_in_a_quantile_fail_closed"),
        "SRS-BT-006 quantile-overflow fail-closed",
    )


def test_turnover_counts_removals_on_a_shrinking_universe() -> None:
    # Safety: turnover drives transaction-cost estimates. A shrinking universe (the current
    # quantile a strict subset of the prior) must NOT report zero churn -- the removed names
    # are real turnover, and hiding them would understate cost and mis-rank the factor.
    _assert_one_passed(
        _run_cargo_test("turnover_counts_removals_when_the_universe_shrinks"),
        "SRS-BT-006 shrinking-universe turnover",
    )


def test_undefined_period_withholds_its_spread_but_mean_remains() -> None:
    # Safety: a period whose factor gives no signal has its spread withheld (None) -- never a
    # fabricated SecurityKey-driven number -- while mean_spread, an average over the DEFINED
    # periods, remains. (A compounded cumulative spread is deliberately not reported; the panel
    # cannot validate non-overlapping forward windows, so compounding is deferred.)
    _assert_one_passed(
        _run_cargo_test("undefined_period_withholds_its_spread_but_mean_spans_the_defined_ones"),
        "SRS-BT-006 undefined-period spread withholding",
    )


def test_factor_crate_has_no_broker_or_simulation_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker/simulation dependency, so the offline
    # factor analysis is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected dependency is caught.
    mutated = cargo_source(config) + '\natp-simulation = { path = "../atp-simulation" }\n'
    with pytest.raises(FactorAnalysisCheckError):
        check_no_broker_dependency(config, mutated)


def test_factor_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real module must carry no vendor SDK token.
    check_vendor_isolation(config, module_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = module_source(config) + "\n// factor values mirrored to ib_insync under the hood\n"
    with pytest.raises(FactorAnalysisCheckError):
        check_vendor_isolation(config, mutated)


def test_computation_uses_no_nondeterminism_source() -> None:
    config = load_config()
    # The real module uses no parallelism/RNG/clock, so the tear-sheet is order-independent.
    check_determinism(config, module_source(config))
    # ...and the guard must not be vacuous: an injected parallel iterator is caught.
    mutated = module_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(FactorAnalysisCheckError):
        check_determinism(config, mutated)


def test_information_coefficient_is_clamped_to_its_domain() -> None:
    config = load_config()
    # The real module clamps the per-period IC to [-1, 1], so floating-point overflow cannot
    # leak an out-of-domain correlation that would mis-rank a factor.
    check_spearman(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the clamp is caught.
    mutated = module_source(config).replace("correlation.clamp(-1.0, 1.0)", "correlation", 1)
    with pytest.raises(FactorAnalysisCheckError):
        check_spearman(config, mutated)


def test_computation_fails_closed_at_the_trust_boundary() -> None:
    config = load_config()
    # The real module rejects an empty/degenerate/duplicate/non-monotonic/non-finite panel
    # before any statistic is computed.
    check_trust_boundary(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the duplicate-security guard (which
    # would double-count a name in a quantile and the turnover set) is caught.
    mutated = module_source(config).replace(
        "FactorAnalysisError::DuplicateSecurity", "FactorAnalysisError::EmptyPanel"
    )
    with pytest.raises(FactorAnalysisCheckError):
        check_trust_boundary(config, mutated)


def test_module_verifies_aggregates_are_finite() -> None:
    config = load_config()
    # The real module verifies each aggregate AND each quantile mean is finite before
    # returning it, so a NaN/inf never leaks into a factor ranking or tear-sheet.
    check_nan_guard(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the finite check is caught.
    mutated = module_source(config).replace("is_finite()", "is_nan()")
    with pytest.raises(FactorAnalysisCheckError):
        check_nan_guard(config, mutated)
    # ...nor is the per-quantile-mean guard vacuous: dropping it is caught.
    mutated = module_source(config).replace('finite("quantile_mean"', 'skip("quantile_mean"', 1)
    with pytest.raises(FactorAnalysisCheckError):
        check_nan_guard(config, mutated)


def test_spread_and_turnover_gated_on_per_bucket_cleanliness() -> None:
    config = load_config()
    # Safety: the real module gates the spread on BOTH extremes being factor-clean and each
    # turnover series on ITS OWN bucket, so a constant/cutoff-tied factor cannot attribute a
    # SecurityKey-driven spread (false alpha) and an unrelated tie does not suppress an
    # unambiguous turnover.
    check_separation(config, module_source(config))
    # ...and the guards must not be vacuous: removing the per-cutoff cleanliness fn is caught.
    mutated = module_source(config).replace("let cutoff_clean", "let always_clean")
    with pytest.raises(FactorAnalysisCheckError):
        check_separation(config, mutated)
    # ...nor the spread gate: dropping the `separates` arm of the spread match is caught.
    mutated = module_source(config).replace(
        "(true, Some(bottom), Some(top))", "(_, Some(bottom), Some(top))", 1
    )
    with pytest.raises(FactorAnalysisCheckError):
        check_separation(config, mutated)
    # ...nor the per-series turnover gate: jointly gating top turnover on the bottom bucket is
    # caught (it would suppress an unambiguous top turnover on a bottom-only tie).
    mutated = module_source(config).replace(
        "top_clean && previous_top_clean", "bottom_clean && previous_bottom_clean", 1
    )
    with pytest.raises(FactorAnalysisCheckError):
        check_separation(config, mutated)


def test_constant_factor_withholds_the_quantile_return_ladder() -> None:
    # Safety (Codex round-5): a constant/tied factor must NOT expose a quantile-return ladder
    # driven only by SecurityKey ordering -- every bucket mean is withheld (None).
    _assert_one_passed(
        _run_cargo_test("constant_factor_withholds_spread_and_turnover"),
        "SRS-BT-006 identity-driven ladder withheld",
    )


def test_bottom_only_tie_keeps_top_turnover_defined() -> None:
    # Safety (Codex round-6): a tie at one extreme must not suppress the other extreme's
    # unambiguous turnover -- the two series are gated on their own bucket's cleanliness.
    _assert_one_passed(
        _run_cargo_test("bottom_only_tie_keeps_top_turnover_defined"),
        "SRS-BT-006 per-series turnover gating",
    )


def test_inner_cutoff_tie_withholds_spread_with_three_quantiles() -> None:
    # Safety (Codex round-3): a tie straddling an inner quantile cutoff (3+ quantiles) decides
    # the bottom/top composition by SecurityKey, so the spread must be withheld even though the
    # extreme buckets look separated.
    _assert_one_passed(
        _run_cargo_test("inner_cutoff_tie_withholds_spread_with_three_quantiles"),
        "SRS-BT-006 inner-cutoff tie",
    )


def test_turnover_is_weight_based_so_reweighting_counts() -> None:
    config = load_config()
    # Safety (Codex round-3): the real module measures turnover as half the L1 distance between
    # the equal-weight portfolios, so a shrinking universe (which reweights the retained names)
    # is not understated by a set-membership ratio.
    check_turnover(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the retained-name reweighting term is caught.
    mutated = module_source(config).replace(
        "(current_weight - previous_weight).abs()", "0.0_f64", 1
    )
    with pytest.raises(FactorAnalysisCheckError):
        check_turnover(config, mutated)


def test_spread_is_withheld_off_option_so_undefined_is_not_fabricated() -> None:
    config = load_config()
    # Safety: the per-period spread is Option<f64> so an unranked period is None, never a
    # fabricated SecurityKey-driven number. (The compounded cumulative spread is deliberately
    # deferred -- the panel cannot validate non-overlapping forward windows.)
    check_factor_returns(config, module_source(config))
    # ...and the guard must not be vacuous: demoting the spread off Option is caught.
    mutated = module_source(config).replace(
        "pub spread_per_period: Vec<(u64, Option<f64>)>,",
        "pub spread_per_period: Vec<(u64, f64)>,",
        1,
    )
    with pytest.raises(FactorAnalysisCheckError):
        check_factor_returns(config, mutated)


def test_mean_spread_is_not_overclaimed_as_horizon_agnostic() -> None:
    # Safety (Codex round-7): mean_spread is NOT horizon-agnostic -- a spread scales with the
    # forward-return horizon, so averaging across mixed horizons mixes magnitudes. The evidence
    # must state the regular-panel precondition, never call mean_spread horizon-agnostic; only the
    # rank-based IC family is genuinely horizon-agnostic. Locks the round-7 doc-scrub.
    config = load_config()
    evidence = check_factor_returns(config, module_source(config))
    assert "horizon-agnostic" not in evidence, (
        "mean_spread evidence must not claim horizon-agnostic"
    )
    assert "regular panel" in evidence, (
        "mean_spread evidence must state its regular-panel precondition"
    )
    src = module_source(config)
    # mean_spread's precondition is documented...
    assert "CONSISTENT forward-return horizon" in src
    # ...and every remaining "horizon-agnostic" mention in the module is about the rank-based IC
    # family (which genuinely is), never the magnitude-scaled spread.
    for line in src.splitlines():
        if "horizon-agnostic" in line:
            assert "InformationCoefficient" in line or "rank" in line.lower(), (
                f"only the IC family may be called horizon-agnostic: {line.strip()!r}"
            )
