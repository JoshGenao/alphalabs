"""SRS-FAC-001 / SyRS SYS-32, SYS-33, SYS-51, NFR-P7 -- a scheduled full-universe factor job
produces factor rankings an operator can trust: it screens the FULL universe (an 8,000 floor it
will not silently under-claim), never fabricates a score for a security it cannot compute on
BOTH market and fundamental sources, resolves its schedule through the SAME trading calendar
strategy scheduling uses (so a run cannot fire on a non-session or mis-time its deadline), fails
CLOSED when it cannot finish before the configured deadline (rather than presenting a stale,
over-deadline ranking as fresh), produces a REGULAR panel for the downstream tear-sheet, and
stays independent of the live IB account.

L7 domain (safety) test. The acceptance criterion's safety core is that the ranked
``FactorScoreSet`` an operator screens, ranks, and allocates research/capital against is
*trustworthy*: a universe that silently falls short of full coverage would mis-rank a factor on a
biased sample; a fabricated score for a security missing a data source would inject noise into the
ranking; a run that fires on a non-trading day or overruns its deadline undetected would present a
stale ranking as current; an irregular produced panel would let the tear-sheet's interval/horizon
aggregates mix incomparable magnitudes; and a leaked broker dependency would let the offline factor
job be reconciled against the live IB account. This test proves the invariant from two angles:

  1. Behavioral -- it shells out to the Rust integration tests
     ``crates/atp-factor-pipeline/tests/srs_fac_001_factor_job.rs`` and
     ``srs_fac_001_store_backed_job.rs`` and asserts the end-to-end job ranks the full universe
     through the calendar within the deadline, skips a security missing either source (never
     fabricating), fails closed below the full-universe floor and on a non-session day, reports a
     deadline overrun fail-closed, and produces a regular panel that feeds ``compute_tear_sheet`` --
     and that the SRS-DATA-007 store-backed execution path
     (``run_scheduled_factor_job_over_store``) runs the full universe over store-resident market +
     fundamental data, skips a security with no store data without fabricating, and fails closed on
     a malformed store record.

  2. Structural -- it asserts, via ``tools/factor_job_check.py``, that the ``factor_job`` module
     resolves its schedule through the calendar, enforces the full-universe floor, gates on the
     deadline budget, records every unscored security as an auditable skip, builds a regular panel,
     uses no nondeterminism source, declares no broker/simulation dependency, and leaks no vendor
     SDK token -- each guard shown non-vacuous by a mutation the check must catch.
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

from factor_job_check import (  # noqa: E402
    FactorJobCheckError,
    cargo_source,
    check_calendar_resolution,
    check_coverage_gate,
    check_deadline_gate,
    check_determinism,
    check_deterministic_output,
    check_equity_gate,
    check_forward_window,
    check_full_universe_floor,
    check_full_universe_gate,
    check_no_broker_dependency,
    check_regularity,
    check_skip_not_fabricate,
    check_vendor_isolation,
    load_config,
    module_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_fac_001_factor_job"
) -> subprocess.CompletedProcess[str]:
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
            test_file,
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


# --------------------------------------------------------------------------- #
# Behavioral (end-to-end Rust integration)
# --------------------------------------------------------------------------- #


def test_ranks_full_universe_through_calendar_within_deadline() -> None:
    # The job screens and ranks 8,000 securities, resolving its schedule through the calendar and
    # completing within the deadline window -- the full SRS-FAC-001 happy path.
    _assert_one_passed(
        _run_cargo_test("ranks_full_universe_through_calendar_within_deadline"),
        "SRS-FAC-001 full-universe ranking",
    )


def test_run_is_deterministic_and_order_independent() -> None:
    # A factor's ranking must not depend on run order or input order, or its apparent quality
    # would be an artifact of nondeterminism (SRS-BT-010).
    _assert_one_passed(
        _run_cargo_test("run_is_deterministic_and_order_independent_across_seeds"),
        "SRS-FAC-001 determinism",
    )


def test_skips_securities_missing_either_source_without_fabricating() -> None:
    # Safety: a security missing market OR fundamental data, or for which the factor abstains, is
    # an auditable skip -- never a fabricated score injected into the ranking.
    _assert_one_passed(
        _run_cargo_test("skips_securities_missing_either_source"),
        "SRS-FAC-001 skip-not-fabricate",
    )


def test_fails_closed_below_full_universe_floor() -> None:
    # Safety: a universe below the 8,000 floor cannot attest full-universe coverage and is
    # rejected -- the job must not rank a factor on a silently-biased partial sample.
    _assert_one_passed(
        _run_cargo_test("fails_closed_below_full_universe_floor"),
        "SRS-FAC-001 universe-floor fail-closed",
    )


def test_fails_closed_on_non_session_schedule() -> None:
    # Safety: a run scheduled for a non-trading day fails closed (SYS-51) -- a factor job cannot
    # fire off the trading calendar strategy scheduling resolves against.
    _assert_one_passed(
        _run_cargo_test("fails_closed_on_non_session_schedule"),
        "SRS-FAC-001 non-session fail-closed",
    )


def test_reports_deadline_exceeded_on_late_finalization() -> None:
    # Safety (NFR-P7): a run whose scoring + ranking + finalization crossed the deadline instant
    # fails closed with no ranked set -- finalization is inside the deadline (Codex finding #2).
    _assert_one_passed(
        _run_cargo_test("reports_deadline_exceeded_when_run_overruns"),
        "SRS-FAC-001 late-finalization fail-closed",
    )


def test_rejects_late_start() -> None:
    # Safety (NFR-P7): a run invoked after the resolved deadline instant fails closed before doing
    # work -- it cannot complete on time (Codex finding #1).
    _assert_one_passed(
        _run_cargo_test("rejects_late_start"),
        "SRS-FAC-001 late-start fail-closed",
    )


def test_rejects_run_fired_on_a_later_session() -> None:
    # Safety (Codex finding): a run for the scheduled session invoked on a LATER session is past the
    # deadline -- the session-aware instant catches it where a bare minute-of-day would not.
    _assert_one_passed(
        _run_cargo_test("rejects_run_fired_on_a_later_session"),
        "SRS-FAC-001 later-session fail-closed",
    )


def test_rejects_early_start() -> None:
    # Safety (Codex finding): a run invoked before its scheduled start fails closed (the
    # orchestrator fired it ahead of schedule).
    _assert_one_passed(
        _run_cargo_test("rejects_early_start"),
        "SRS-FAC-001 early-start fail-closed",
    )


def test_output_order_independent_with_skips() -> None:
    # Safety (Codex finding): a universe with skips yields the same outcome under reversed input,
    # because both the scores AND the skipped list are sorted.
    _assert_one_passed(
        _run_cargo_test("ranking_is_order_independent_with_skips"),
        "SRS-FAC-001 skip-order determinism",
    )


def test_deadline_is_absolute_not_size_based() -> None:
    # Safety: the deadline is the ABSOLUTE resolved instant read from the injected clock, so the
    # same universe yields opposite outcomes under a fast vs a slow clock.
    _assert_one_passed(
        _run_cargo_test("deadline_outcome_depends_on_clock_not_universe_size"),
        "SRS-FAC-001 absolute deadline",
    )


def test_fails_closed_on_no_usable_coverage() -> None:
    # Safety (Codex finding): an all-skipped universe scores nothing, so the run fails closed
    # rather than emitting an empty ranking as a success.
    _assert_one_passed(
        _run_cargo_test("fails_closed_on_no_usable_coverage"),
        "SRS-FAC-001 coverage fail-closed",
    )


def test_fails_closed_on_thin_coverage() -> None:
    # Safety (Codex finding): a run scoring far fewer than the configured fraction of the universe
    # fails closed -- a near-empty ranking is not a successful full-universe computation.
    _assert_one_passed(
        _run_cargo_test("fails_closed_on_thin_coverage"),
        "SRS-FAC-001 thin-coverage fail-closed",
    )


def test_coverage_floor_cannot_collapse() -> None:
    # Safety (Codex R8): a config ratio of 0.0 cannot collapse the coverage floor to one security --
    # the hard platform minimum still requires half the universe scored.
    _assert_one_passed(
        _run_cargo_test("coverage_floor_cannot_collapse_below_platform_minimum"),
        "SRS-FAC-001 hard coverage floor",
    )


def test_scope_honest_deferral_of_preemptive_cancellation() -> None:
    # Scope honesty (Codex R9): the contract must DECLARE that preemptive termination of a hung
    # model is the deferred supervised runtime -- the in-process gate is observational, never
    # claiming to preempt a hung synchronous model.
    import subprocess

    result = subprocess.run(
        [sys.executable, "tools/factor_job_check.py"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "supervised HARD-deadline termination/cancellation of a hung" in result.stdout
    assert "the in-process gate is OBSERVATIONAL" in result.stdout


def test_rejects_mislabeled_forward_window() -> None:
    # Safety (Codex finding): a period whose forward window does not span its declared horizon is
    # rejected -- the horizon is verified through the calendar, not caller-asserted.
    _assert_one_passed(
        _run_cargo_test("rejects_mislabeled_forward_window"),
        "SRS-FAC-001 forward-window verified",
    )


def test_rejects_invalid_coverage_ratio() -> None:
    # Safety (Codex finding): a coverage ratio outside [0,1] fails closed -- an invalid policy is
    # not silently treated as permissive.
    _assert_one_passed(
        _run_cargo_test("rejects_invalid_coverage_ratio"),
        "SRS-FAC-001 invalid-ratio fail-closed",
    )


def test_fails_closed_on_backward_clock() -> None:
    # Safety (Codex finding): a regressing clock between the start and completion reads fails closed
    # -- a backward wall clock cannot make a late completion read as on-time.
    _assert_one_passed(
        _run_cargo_test("fails_closed_on_backward_clock"),
        "SRS-FAC-001 backward-clock fail-closed",
    )


def test_produces_regular_panel_for_tear_sheet() -> None:
    # The producer bridge to SRS-BT-006: the job assembles a REGULAR panel (constant
    # calendar-resolved interval + non-overlapping horizon) that feeds compute_tear_sheet, so the
    # tear-sheet's interval/horizon-dependent means are computed on comparable magnitudes.
    _assert_one_passed(
        _run_cargo_test("produces_regular_panel_for_tear_sheet"),
        "SRS-FAC-001 regular-panel producer",
    )


def test_rejects_irregular_panel() -> None:
    # Safety: a panel whose rebalance interval is not constant (resolved through the calendar) is
    # rejected, so a downstream mean does not silently mix incomparable interval magnitudes.
    _assert_one_passed(
        _run_cargo_test("rejects_irregular_panel_across_weekend_gap"),
        "SRS-FAC-001 irregular-panel fail-closed",
    )


# --------------------------------------------------------------------------- #
# Behavioral (store-backed execution path -- SRS-DATA-007 factor-job consumer)
# --------------------------------------------------------------------------- #


def test_store_backed_job_runs_full_universe_over_store_within_deadline() -> None:
    # The SRS-DATA-007 factor-job consumer: run_scheduled_factor_job_over_store sources BOTH market
    # and fundamental inputs from the unified store (by symbol / date range / resolution, no provider
    # named), assembles the cross-section, and runs the full 8,000+ universe within the deadline.
    _assert_one_passed(
        _run_cargo_test(
            "runs_full_universe_factor_job_over_the_store_within_deadline",
            test_file="srs_fac_001_store_backed_job",
        ),
        "SRS-FAC-001 store-backed full-universe run",
    )


def test_store_backed_job_skips_missing_store_data_without_fabricating() -> None:
    # Safety: a security with no store data is an auditable skip (MissingMarketData) -- the
    # store-backed path never fabricates a factor input for a security the store cannot supply.
    _assert_one_passed(
        _run_cargo_test(
            "securities_missing_a_store_source_are_skipped_not_fabricated",
            test_file="srs_fac_001_store_backed_job",
        ),
        "SRS-FAC-001 store-backed skip-not-fabricate",
    )


def test_store_backed_assembly_fails_closed_on_malformed_record() -> None:
    # Safety: a fundamental record present but missing a required field makes assembly fail closed
    # (StoreFactorJobError::Input), so the job runs on no fabricated data rather than a guessed ratio.
    _assert_one_passed(
        _run_cargo_test(
            "store_assembly_failure_propagates_as_a_fail_closed_job_error",
            test_file="srs_fac_001_store_backed_job",
        ),
        "SRS-FAC-001 store-backed assembly fail-closed",
    )


# --------------------------------------------------------------------------- #
# Structural (contract guards, each shown non-vacuous)
# --------------------------------------------------------------------------- #


def test_enforces_full_universe_floor() -> None:
    config = load_config()
    check_full_universe_floor(config, module_source(config))
    check_full_universe_gate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the floor rejection is caught.
    mutated = module_source(config).replace("Err(FactorJobError::UniverseBelowMinimum", "Ok(skip")
    with pytest.raises(FactorJobCheckError):
        check_full_universe_gate(config, mutated)
    # ...nor may the floor be caller-configurable: comparing against a config minimum (which a
    # caller could lower below 8,000) instead of the hard constant is caught.
    mutated = module_source(config).replace(
        "universe.len() < FULL_UNIVERSE_MIN", "universe.len() < config.min_universe"
    )
    with pytest.raises(FactorJobCheckError):
        check_full_universe_gate(config, mutated)


def test_resolves_schedule_through_the_calendar() -> None:
    config = load_config()
    check_calendar_resolution(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the non-session rejection is caught.
    mutated = module_source(config).replace("Err(FactorJobError::NotASession", "Ok(skip")
    with pytest.raises(FactorJobCheckError):
        check_calendar_resolution(config, mutated)
    # ...nor may it skip resolving the before-open offsets through session_open: resolving only
    # is_session (a calendar with no resolvable open would be accepted) is caught.
    mutated = module_source(config).replace(
        ".session_open(schedule.session)", ".is_session(schedule.session)"
    )
    with pytest.raises(FactorJobCheckError):
        check_calendar_resolution(config, mutated)


def test_gates_on_the_absolute_deadline() -> None:
    config = load_config()
    check_deadline_gate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the session-aware late-START check is caught (a
    # run invoked after the deadline -- even on a later session -- would otherwise pass; Codex #1).
    mutated = module_source(config).replace("started >= deadline_instant", "false")
    with pytest.raises(FactorJobCheckError):
        check_deadline_gate(config, mutated)
    # ...nor the EARLY-start check: a run fired before its schedule must be caught (Codex #2).
    mutated = module_source(config).replace("started < start_instant", "false")
    with pytest.raises(FactorJobCheckError):
        check_deadline_gate(config, mutated)
    # ...nor the COMPLETION check: a finalization overrun must be caught (Codex #2).
    mutated = module_source(config).replace("completed >= deadline_instant", "false")
    with pytest.raises(FactorJobCheckError):
        check_deadline_gate(config, mutated)


def test_output_is_order_independent() -> None:
    config = load_config()
    check_deterministic_output(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the skipped sort is caught (Codex #3 -- a
    # reversed input with skips would otherwise change the outcome).
    mutated = module_source(config).replace(
        "skipped.sort_by(|a, b| a.security.cmp(&b.security))", "let _ = &skipped"
    )
    with pytest.raises(FactorJobCheckError):
        check_deterministic_output(config, mutated)
    # ...nor may it score in caller-input order: dropping the canonical scan sort is caught (Codex
    # R7 -- a stateful model would otherwise be order-dependent).
    mutated = module_source(config).replace(
        "scan.sort_by(|a, b| a.security.cmp(&b.security))", "let _ = &scan"
    )
    with pytest.raises(FactorJobCheckError):
        check_deterministic_output(config, mutated)


def test_stateful_model_output_is_order_independent() -> None:
    # Safety (Codex R7): a model whose score depends on call order still yields an order-independent
    # output, because the job scores in canonical key order.
    _assert_one_passed(
        _run_cargo_test("stateful_model_output_is_order_independent"),
        "SRS-FAC-001 stateful-model determinism",
    )


def test_fails_closed_below_coverage_floor() -> None:
    config = load_config()
    check_coverage_gate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the coverage rejection is caught (Codex finding
    # #3 -- an all-skipped run must not report success).
    mutated = module_source(config).replace("Err(FactorJobError::NoUsableCoverage", "Ok(skip")
    with pytest.raises(FactorJobCheckError):
        check_coverage_gate(config, mutated)
    # ...nor may the floor be a fixed number instead of a ratio of the universe (Codex F1 -- a
    # one-scored success could otherwise slip through).
    mutated = module_source(config).replace("config.min_scored_ratio", "0.0_f64")
    with pytest.raises(FactorJobCheckError):
        check_coverage_gate(config, mutated)
    # ...nor may an out-of-range ratio be accepted: dropping the [0,1] validation is caught (R6).
    mutated = module_source(config).replace(
        "(0.0..=1.0).contains(&config.min_scored_ratio)", "true"
    )
    with pytest.raises(FactorJobCheckError):
        check_coverage_gate(config, mutated)
    # ...nor may a config of 0.0 collapse the floor: dropping the hard platform minimum is caught
    # (Codex R8).
    mutated = module_source(config).replace(
        "config.min_scored_ratio.max(MIN_SCORED_COVERAGE_RATIO)", "config.min_scored_ratio"
    )
    with pytest.raises(FactorJobCheckError):
        check_coverage_gate(config, mutated)


def test_clock_must_be_monotonic() -> None:
    config = load_config()
    check_deadline_gate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the backward-clock check is caught (R6 -- a
    # regressing wall clock could otherwise read a late completion as on-time).
    mutated = module_source(config).replace("completed < started", "false")
    with pytest.raises(FactorJobCheckError):
        check_deadline_gate(config, mutated)


def test_rejects_non_equity_securities() -> None:
    config = load_config()
    check_equity_gate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the asset-class check is caught (Codex F2).
    mutated = module_source(config).replace("asset_class() != AssetClass::Equity", "false")
    with pytest.raises(FactorJobCheckError):
        check_equity_gate(config, mutated)


def test_verifies_forward_window_provenance() -> None:
    config = load_config()
    check_forward_window(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the horizon verification is caught (Codex F3 --
    # a mislabeled forward window would otherwise be certified as a regular panel).
    mutated = module_source(config).replace(
        "actual_gap != Some(forward_horizon_sessions)", "false"
    )
    with pytest.raises(FactorJobCheckError):
        check_forward_window(config, mutated)


def test_records_unscored_securities_as_skips() -> None:
    config = load_config()
    check_skip_not_fabricate(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the abstain skip is caught.
    mutated = module_source(config).replace("FactorSkipReason::FactorAbstained", "fabricate(0.0)")
    with pytest.raises(FactorJobCheckError):
        check_skip_not_fabricate(config, mutated)


def test_produces_a_regular_panel() -> None:
    config = load_config()
    check_regularity(config, module_source(config))
    # ...and the guard must not be vacuous: dropping the irregular-interval rejection is caught.
    mutated = module_source(config).replace(
        "Err(FactorJobError::IrregularRebalanceInterval", "Ok(skip"
    )
    with pytest.raises(FactorJobCheckError):
        check_regularity(config, mutated)


def test_uses_no_nondeterminism_source() -> None:
    config = load_config()
    check_determinism(config, module_source(config))
    # ...and the guard must not be vacuous: an injected parallel iterator is caught.
    mutated = module_source(config) + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
    with pytest.raises(FactorJobCheckError):
        check_determinism(config, mutated)


def test_job_has_no_broker_or_simulation_dependency() -> None:
    config = load_config()
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected dependency is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(FactorJobCheckError):
        check_no_broker_dependency(config, mutated)


def test_module_leaks_no_vendor_token() -> None:
    config = load_config()
    check_vendor_isolation(config, module_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = module_source(config) + "\n// scores mirrored to ib_insync under the hood\n"
    with pytest.raises(FactorJobCheckError):
        check_vendor_isolation(config, mutated)


def test_gated_core_and_preflight_are_crate_private_not_a_public_bypass() -> None:
    # Safety: run_factor_job_gated accepts caller-forged session/started/deadline, bypassing
    # preflight_schedule's calendar resolution + start/deadline checks + InvalidCoverageRatio guard. It
    # (and the preflight + StartGate it threads) must be crate-PRIVATE, so only the in-crate store
    # wrapper -- which preflights first -- can reach it; external callers use the always-preflighting
    # run_factor_job / run_scheduled_factor_job_over_store. A regression to `pub` would re-open the
    # schedule/coverage-gate bypass.
    src = module_source(load_config())
    for name in ("run_factor_job_gated", "preflight_schedule"):
        assert f"pub(crate) fn {name}" in src, f"{name} must be crate-private (pub(crate) fn)"
        assert f"pub fn {name}" not in src, f"{name} must NOT be a public bypass (pub fn)"
    assert "pub(crate) enum StartGate" in src
    assert "pub enum StartGate" not in src
    # The always-preflighting public entry point stays public.
    assert "pub fn run_factor_job" in src
