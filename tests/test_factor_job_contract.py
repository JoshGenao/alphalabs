"""Contract tests for SRS-FAC-001 (compute scheduled factors across the full US equity
universe).

SRS-FAC-001 / SyRS SYS-32, SYS-33, SYS-51, NFR-P7 / StRS SN-2.06, BG-3 -- a scheduled factor
job processes 8,000+ securities using market and fundamental data, resolves its schedule
through the trading calendar strategy scheduling uses, and completes before the configured
deadline. This slice ships the deterministic scheduled-factor-job surface in
``crates/atp-factor-pipeline`` (module ``factor_job``) -- the upstream PRODUCER of the
SRS-BT-006 factor panel -- AND the store-backed READ path
(``store_inputs::run_scheduled_factor_job_over_store``) that sources both market and fundamental
inputs from the unified historical store (SRS-DATA-007) and feeds the scored core, demonstrated over an
8,000+ fixture universe within the calendar-resolved deadline read from a DETERMINISTIC clock.
SRS-FAC-001's acceptance is a PERFORMANCE TEST (NFR-P7): the live wall-clock harness over real securities
is a deferred close blocker. The store-backed run DERIVES its data as-of from the calendar's
``session_as_of_ts(schedule.session)`` (not a caller timestamp), so a caller cannot pair a session with a
future as-of -- only the concrete real-calendar ``SessionOrdinal`` -> epoch mapping (test calendars stand
in) is deferred, so ``feature_list.json`` keeps ``passes:false`` (the store-backed read is a foundational
primitive). The other deferred owners (the real Databento/Sharadar network adapters, the SYS-57
workload-priority admission, and the SRS-UI / SRS-API operator surface) are
other features.

Mirrors ``tests/test_factor_analysis_contract.py``: shells out to
``tools/factor_job_check.py``, then exercises each per-check function in-process, including
negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in memory and assert the
contract actually catches the regression (a dropped struct field, a renamed entry point, a
dropped error variant, a removed calendar/universe/deadline guard, a fabricated-instead-of-skip
mutation, a removed regularity guard, an injected nondeterminism source, a money-typed factor
score, a dropped lib re-export, an injected broker dependency, and a leaked vendor token).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from factor_job_check import (  # noqa: E402
    FactorJobCheckError,
    assert_factor_job_static,
    cargo_source,
    check_assemble_fn,
    check_calendar_resolution,
    check_clock,
    check_coverage_gate,
    check_deadline_gate,
    check_determinism,
    check_deterministic_output,
    check_equity_gate,
    check_factor_model,
    check_forward_window,
    check_full_universe_floor,
    check_full_universe_gate,
    check_inputs,
    check_module_reexport,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_outcome_and_error_enums,
    check_regularity,
    check_run_fn,
    check_score_outputs,
    check_skip_not_fabricate,
    check_trading_calendar_port,
    check_vendor_isolation,
    lib_source,
    load_config,
    module_source,
)


class FactorJobScriptTest(unittest.TestCase):
    def test_srs_fac_001_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/factor_job_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-FAC-001 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "full-universe floor FULL_UNIVERSE_MIN = 8000",
            "declares the TradingCalendar port",
            "the SyRS SYS-51 reuse boundary",
            "declares the session-aware Instant (session + minute) and the injected Clock port (now)",
            "user-defined FactorModel trait whose compute takes BOTH",
            "MarketFactorInput (trailing_return, realized_volatility)",
            "FactorJobSchedule (session + start/deadline minutes before",
            "FactorScore (security, factor_value, rank)",
            "every unscored security",
            "RealizedFactorSession + RealizedObservation",
            "exposes `pub fn run_factor_job",
            "exposes `pub fn assemble_regular_panel",
            "FactorJobError with 19 fail-closed variants",
            "validates min_scored_ratio is in [0, 1] (InvalidCoverageRatio)",
            "the hard platform floor means a config of 0.0 cannot collapse coverage",
            "supervised HARD-deadline termination/cancellation of a hung or pathologically-slow",
            "the in-process gate is OBSERVATIONAL",
            "rejects any non-equity security (NonEquitySecurity)",
            "checks each period's declared forward_window_end is exactly the declared horizon",
            "for LABEL CONSISTENCY",
            "below ceil(max(min_scored_ratio, MIN_SCORED_COVERAGE_RATIO) * universe)",
            "resolves the schedule through the calendar's session_open",
            "a lead before the day start fails closed (ScheduleBeforeDayStart)",
            "below the HARD 8,000 full-universe floor",
            "compared against the constant FULL_UNIVERSE_MIN -- not a caller config",
            "gates its NFR-P7 deadline against the ABSOLUTE session-aware instant read from the injected Clock",
            "rejects an early start (started < start_instant)",
            "a late start (started > deadline_instant -- session-aware, so a later-day run is caught)",
            "sorts BOTH the ranked scores and the skipped list by SecurityKey",
            "fails closed with NoUsableCoverage",
            "records every unscored security as a SkippedSecurity",
            "resolves the rebalance interval through the calendar",
            "factor job is deterministic",
            "keeps factor scores and forward returns as dimensionless f64",
            "lib.rs re-exports `pub mod factor_job;`",
            "Cargo.toml declares no dependency on the live/broker/simulation path",
            "factor_job module is free of all 5 forbidden vendor SDK tokens",
            "SRS-DATA-007 store READ itself is DONE",
            "feature_list.json keeps SRS-FAC-001 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class StaticCoverageTest(unittest.TestCase):
    def test_all_static_collectors_pass(self) -> None:
        config = load_config()
        evidence = assert_factor_job_static(config)
        self.assertEqual(len(evidence), 25)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = module_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class FullUniverseFloorTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("full-universe floor", check_full_universe_floor(self.config, self.src))

    def test_dropped_floor_is_caught(self) -> None:
        mutated = self.src.replace("pub const FULL_UNIVERSE_MIN: usize = 8_000", "", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_full_universe_floor(self.config, mutated)
        self.assertIn("full-universe floor", str(ctx.exception))


class TradingCalendarPortTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("TradingCalendar port", check_trading_calendar_port(self.config, self.src))

    def test_missing_method_is_caught(self) -> None:
        # Dropping next_session would leave the rebalance interval unresolvable through the
        # calendar (SYS-51).
        mutated = self.src.replace("fn next_session(", "fn next_trading(", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_trading_calendar_port(self.config, mutated)
        self.assertIn("next_session", str(ctx.exception))


class ClockTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("injected Clock port", check_clock(self.config, self.src))

    def test_missing_clock_method_is_caught(self) -> None:
        mutated = self.src.replace("fn now(", "fn tick(")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_clock(self.config, mutated)
        self.assertIn("`fn now`", str(ctx.exception))


class FactorModelTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("compute takes BOTH", check_factor_model(self.config, self.src))

    def test_single_source_factor_is_caught(self) -> None:
        # A factor that no longer takes the fundamental input would violate SYS-32 (both sources).
        mutated = self.src.replace(
            "fundamental: &FundamentalFactorInput,\n    ) -> Option<f64>;",
            ") -> Option<f64>;",
            1,
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_factor_model(self.config, mutated)
        self.assertIn("both market data and fundamental data", str(ctx.exception))


class InputsTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("MarketFactorInput", check_inputs(self.config, self.src))

    def test_non_optional_source_is_caught(self) -> None:
        # Making the fundamental input non-Option would make a missing source unrepresentable, so
        # the job could not record a skip and would be forced to fabricate.
        mutated = self.src.replace(
            "pub fundamental: Option<FundamentalFactorInput>,",
            "pub fundamental: FundamentalFactorInput,",
            1,
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_inputs(self.config, mutated)
        self.assertIn("fundamental", str(ctx.exception))


class ScoreOutputsTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("FactorScore", check_score_outputs(self.config, self.src))

    def test_dropped_skip_variant_is_caught(self) -> None:
        mutated = self.src.replace("FactorAbstained,", "", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_score_outputs(self.config, mutated)
        self.assertIn("FactorSkipReason", str(ctx.exception))


class RunFnTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("run_factor_job", check_run_fn(self.config, self.src))

    def test_renamed_entry_point_is_caught(self) -> None:
        mutated = self.src.replace("pub fn run_factor_job", "pub fn run_job", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_run_fn(self.config, mutated)
        self.assertIn("run_factor_job", str(ctx.exception))


class AssembleFnTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("assemble_regular_panel", check_assemble_fn(self.config, self.src))


class EnumsTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("DeadlineExceeded", check_outcome_and_error_enums(self.config, self.src))

    def test_dropped_error_variant_is_caught(self) -> None:
        mutated = self.src.replace("OverlappingForwardWindows {", "OverlapDropped {", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_outcome_and_error_enums(self.config, mutated)
        self.assertIn("OverlappingForwardWindows", str(ctx.exception))


class CalendarResolutionTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("resolves the schedule", check_calendar_resolution(self.config, self.src))

    def test_removed_session_guard_is_caught(self) -> None:
        # The NotASession guard appears in both run_factor_job and assemble_regular_panel; removing
        # it everywhere is what the check must catch.
        mutated = self.src.replace("Err(FactorJobError::NotASession", "Ok(FactorJobError::Skip")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_calendar_resolution(self.config, mutated)
        self.assertIn("NotASession", str(ctx.exception))

    def test_dropped_session_open_resolution_is_caught(self) -> None:
        # Resolving only is_session (not session_open) would accept a calendar with no resolvable
        # open and never resolve the before-open offsets (Codex finding #3).
        mutated = self.src.replace(
            ".session_open(schedule.session)", ".is_session(schedule.session)"
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_calendar_resolution(self.config, mutated)
        self.assertIn("session_open", str(ctx.exception))

    def test_removed_before_day_start_guard_is_caught(self) -> None:
        mutated = self.src.replace("FactorJobError::ScheduleBeforeDayStart {", "Wrong {")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_calendar_resolution(self.config, mutated)
        self.assertIn("ScheduleBeforeDayStart", str(ctx.exception))


class FullUniverseGateTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("full-universe floor", check_full_universe_gate(self.config, self.src))

    def test_removed_floor_guard_is_caught(self) -> None:
        mutated = self.src.replace("Err(FactorJobError::UniverseBelowMinimum", "Ok(thing", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_full_universe_gate(self.config, mutated)
        self.assertIn("UniverseBelowMinimum", str(ctx.exception))

    def test_caller_configurable_floor_is_caught(self) -> None:
        # Comparing against a caller config instead of the hard constant would let a caller weaken
        # full-universe coverage (Codex finding #1).
        mutated = self.src.replace(
            "universe.len() < FULL_UNIVERSE_MIN", "universe.len() < config.min_universe"
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_full_universe_gate(self.config, mutated)
        self.assertIn("FULL_UNIVERSE_MIN", str(ctx.exception))


class DeadlineGateTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("session-aware instant", check_deadline_gate(self.config, self.src))

    def test_removed_clock_read_is_caught(self) -> None:
        mutated = self.src.replace("clock.now()", "(default_instant())")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deadline_gate(self.config, mutated)
        self.assertIn("clock.now()", str(ctx.exception))

    def test_removed_early_start_check_is_caught(self) -> None:
        # Without the early-start check a run fired before its schedule would proceed (Codex F2).
        mutated = self.src.replace("started < start_instant", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deadline_gate(self.config, mutated)
        self.assertIn("started < start_instant", str(ctx.exception))

    def test_removed_late_start_check_is_caught(self) -> None:
        # Without the session-aware late-start check a later-day run would pass (Codex F1).
        mutated = self.src.replace("started >= deadline_instant", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deadline_gate(self.config, mutated)
        self.assertIn("started >= deadline_instant", str(ctx.exception))

    def test_removed_completion_check_is_caught(self) -> None:
        # Without the completion check, ranking/finalization overrun is excluded (Codex finding).
        mutated = self.src.replace("completed >= deadline_instant", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deadline_gate(self.config, mutated)
        self.assertIn("completed >= deadline_instant", str(ctx.exception))

    def test_removed_monotonic_clock_check_is_caught(self) -> None:
        # Without the monotonic check a backward wall clock could read a late completion as on-time
        # (Codex R6).
        mutated = self.src.replace("completed < started", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deadline_gate(self.config, mutated)
        self.assertIn("completed < started", str(ctx.exception))


class DeterministicOutputTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("sorts BOTH", check_deterministic_output(self.config, self.src))

    def test_unsorted_skipped_is_caught(self) -> None:
        # Leaving skipped in input order makes the output order-dependent (Codex finding #3).
        mutated = self.src.replace(
            "skipped.sort_by(|a, b| a.security.cmp(&b.security))", "let _ = &skipped"
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deterministic_output(self.config, mutated)
        self.assertIn("skipped.sort_by", str(ctx.exception))

    def test_non_canonical_scan_is_caught(self) -> None:
        # Scoring in caller-input order (not canonical key order) lets a stateful model be
        # order-dependent (Codex R7).
        mutated = self.src.replace(
            "scan.sort_by(|a, b| a.security.cmp(&b.security))", "let _ = &scan"
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_deterministic_output(self.config, mutated)
        self.assertIn("canonical", str(ctx.exception).lower())


class CoverageGateTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("NoUsableCoverage", check_coverage_gate(self.config, self.src))

    def test_removed_coverage_guard_is_caught(self) -> None:
        mutated = self.src.replace("Err(FactorJobError::NoUsableCoverage", "Ok(thing", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_coverage_gate(self.config, mutated)
        self.assertIn("NoUsableCoverage", str(ctx.exception))

    def test_non_ratio_floor_is_caught(self) -> None:
        # A fixed floor instead of a ratio of the universe lets a one-scored "success" slip (Codex).
        mutated = self.src.replace("config.min_scored_ratio", "0.0_f64")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_coverage_gate(self.config, mutated)
        self.assertIn("config.min_scored_ratio", str(ctx.exception))

    def test_missing_minimum_floor_is_caught(self) -> None:
        # Without the `.max(1)`, a degenerate ratio could let a zero-scored run pass.
        mutated = self.src.replace(".ceil() as usize).max(1)", ".ceil() as usize)")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_coverage_gate(self.config, mutated)
        self.assertIn("at least 1", str(ctx.exception))

    def test_missing_ratio_range_validation_is_caught(self) -> None:
        # Without the [0,1] range check a negative ratio would weaken the gate (Codex R6).
        mutated = self.src.replace("(0.0..=1.0).contains(&config.min_scored_ratio)", "true")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_coverage_gate(self.config, mutated)
        self.assertIn("[0, 1]", str(ctx.exception))

    def test_missing_platform_floor_is_caught(self) -> None:
        # Without the hard platform minimum, a config ratio of 0.0 collapses the floor (Codex R8).
        mutated = self.src.replace(
            "config.min_scored_ratio.max(MIN_SCORED_COVERAGE_RATIO)", "config.min_scored_ratio"
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_coverage_gate(self.config, mutated)
        self.assertIn("platform minimum", str(ctx.exception))


class EquityGateTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("non-equity security", check_equity_gate(self.config, self.src))

    def test_removed_equity_check_is_caught(self) -> None:
        mutated = self.src.replace("asset_class() != AssetClass::Equity", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_equity_gate(self.config, mutated)
        self.assertIn("EQUITY", str(ctx.exception))


class ForwardWindowTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("LABEL CONSISTENCY", check_forward_window(self.config, self.src))

    def test_unverified_horizon_is_caught(self) -> None:
        mutated = self.src.replace("actual_gap != Some(forward_horizon_sessions)", "false")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_forward_window(self.config, mutated)
        self.assertIn("forward window", str(ctx.exception))


class SkipNotFabricateTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn(
            "records every unscored security as a SkippedSecurity",
            check_skip_not_fabricate(self.config, self.src),
        )

    def test_removed_abstain_skip_is_caught(self) -> None:
        mutated = self.src.replace("FactorSkipReason::FactorAbstained", "fabricate(0.0)")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_skip_not_fabricate(self.config, mutated)
        self.assertIn("FactorAbstained", str(ctx.exception))


class RegularityTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn(
            "rebalance interval through the calendar", check_regularity(self.config, self.src)
        )

    def test_removed_interval_guard_is_caught(self) -> None:
        mutated = self.src.replace("Err(FactorJobError::IrregularRebalanceInterval", "Ok(stuff", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_regularity(self.config, mutated)
        self.assertIn("IrregularRebalanceInterval", str(ctx.exception))

    def test_removed_overlap_guard_is_caught(self) -> None:
        mutated = self.src.replace("Err(FactorJobError::OverlappingForwardWindows", "Ok(stuff", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_regularity(self.config, mutated)
        self.assertIn("OverlappingForwardWindows", str(ctx.exception))

    def test_removed_panel_validate_is_caught(self) -> None:
        mutated = self.src.replace("panel.validate()?;", "", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_regularity(self.config, mutated)
        self.assertIn("FactorPanel trust boundary", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("deterministic", check_determinism(self.config, self.src))

    def test_injected_nondeterminism_is_caught(self) -> None:
        mutated = self.src.replace("let mut scored", "let _ = Instant::now(); let mut scored", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("Instant::now", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("dimensionless f64", check_numeric_boundary(self.config, self.src))

    def test_money_typed_factor_is_caught(self) -> None:
        # Typing the factor score as integer minor units would conflate the factor domain with
        # money (it is a dimensionless score, not a price).
        mutated = self.src.replace("pub factor_value: f64,", "pub factor_value: i64,")
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("factor_value: f64", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("re-exports", check_module_reexport(self.config, self.lib_src))

    def test_dropped_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod factor_job;", "mod factor_job;", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("factor_job", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("no dependency", check_no_broker_dependency(self.config, self.cargo_src))

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src.replace(
            "[dependencies]", '[dependencies]\natp-execution = { path = "../atp-execution" }', 1
        )
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_evidence(self) -> None:
        self.assertIn("forbidden vendor SDK tokens", check_vendor_isolation(self.config, self.src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src.replace("let mut scored", "let databento = 1; let mut scored", 1)
        with self.assertRaises(FactorJobCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("databento", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
