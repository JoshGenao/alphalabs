"""Contract tests for SRS-DATA-010 (SSD storage eviction policy).

SRS-DATA-010 / SyRS SYS-69 / StRS C-5, BG-6 — at the default 80% high-water mark eviction prioritises
old inactive data; data for the currently running live strategy is never evicted; data accessed within
the recency window (default 24h) by a running backtest/factor job is not evicted. This slice ships the
``eviction`` policy module + the ``access_journal`` recency producer in ``crates/atp-data``, the
``data010_eviction_cli`` operator CLI, and additive instrumentation of the backtest / factor read paths.

Mirrors ``tests/test_data009_cold_read_contract.py``: shells out to ``tools/data010_eviction_check.py``
for the aggregate PASS, then exercises each per-check function in-process with negative spot-checks that
mutate the Rust source in memory (a planner that skips the pin filter, an enforce that reaches into the
SSD primary, a float in the target math, an injected clock read, a journal that drops its Corrupt
variant, an existing read fn deleted), so a regression that would silently break an acceptance clause is
caught structurally.
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

from data010_eviction_check import (  # noqa: E402
    EvictionCheckError,
    assert_eviction_static,
    bar_source,
    check_access_journal,
    check_cli,
    check_determinism,
    check_enforce_never_touches_hot,
    check_instrumentation,
    check_numeric_boundary,
    check_planner,
    check_policy_type,
    cli_source,
    eviction_source,
    factor_source,
    journal_source,
    lib_source,
    load_config,
)


class EvictionScriptTest(unittest.TestCase):
    def test_srs_data_010_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/data010_eviction_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-010 STORAGE-EVICTION PASS", result.stdout)
        for needle in (
            "exposes both the eviction and access_journal modules",
            "defaults 80% / 24h",
            "pinned candidates (live/recent/retention",
            "structurally un-evictable",
            "writes fail open",
            "instrumented ADDITIVELY",
            "refuses a destructive enforce",
            "integer arithmetic",
            "read no wall-clock",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.evict_src = eviction_source(self.config)
        self.journal_src = journal_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cli_src = cli_source(self.config)
        self.bar_src = bar_source(self.config)
        self.factor_src = factor_source(self.config)


class PositiveContractTest(_Fixture):
    def test_all_static_checks_pass_on_the_real_source(self) -> None:
        evidence = assert_eviction_static(self.config)
        self.assertTrue(all(isinstance(line, str) and line for line in evidence))


class NegativeContractTest(_Fixture):
    """Each mutation must be CAUGHT by the corresponding check (fail-closed structural guard)."""

    def test_policy_missing_high_water_default_is_caught(self) -> None:
        mutated = self.evict_src.replace(
            "pub const DEFAULT_HIGH_WATER_PERCENT: u32 = 80;",
            "pub const DEFAULT_HIGH_WATER_PERCENT: u32 = 999;",
        )
        with self.assertRaises(EvictionCheckError):
            check_policy_type(self.config, mutated)

    def test_policy_dropping_invalid_recency_variant_is_caught(self) -> None:
        mutated = self.evict_src.replace("InvalidRecencyWindow", "SomethingElse")
        with self.assertRaises(EvictionCheckError):
            check_policy_type(self.config, mutated)

    def test_planner_that_skips_the_pin_filter_is_caught(self) -> None:
        # Simulate a planner that pushes EVERY candidate to evictable (dropping the `None =>` guard),
        # which would let a live/recent/retention record be evicted.
        mutated = self.evict_src.replace(
            "None => evictable.push(candidate),",
            "_ => evictable.push(candidate),",
        )
        with self.assertRaises(EvictionCheckError):
            check_planner(self.config, mutated)

    def test_planner_that_drops_cold_before_hot_ordering_is_caught(self) -> None:
        mutated = self.evict_src.replace(
            "a.tier\n            .cmp(&b.tier)", "0i32\n            .cmp(&0i32)"
        )
        with self.assertRaises(EvictionCheckError):
            check_planner(self.config, mutated)

    def test_enforce_reaching_into_the_ssd_primary_is_caught(self) -> None:
        # Inject an ssd_dir reference into the physical eviction path — the guard that keeps enforce
        # from ever touching hot data must catch it.
        mutated = self.evict_src.replace(
            "let dir = self.reader.cold_cache_dir();",
            "let dir = self.reader.cold_cache_dir();\n        let _leak = self.reader.tier().config().ssd_dir();",
            1,
        )
        with self.assertRaises(EvictionCheckError):
            check_enforce_never_touches_hot(self.config, mutated)

    def test_journal_dropping_the_corrupt_variant_is_caught(self) -> None:
        mutated = self.journal_src.replace("Corrupt {", "Tolerated {")
        with self.assertRaises(EvictionCheckError):
            check_access_journal(self.config, mutated)

    def test_journal_that_propagates_a_write_error_is_caught(self) -> None:
        # A record impl that does NOT discard the append result (fails to fail-open) loses the token.
        mutated = self.journal_src.replace(
            "let _ = self.append(job, symbol, access_ts);", "self.append(job, symbol, access_ts);"
        )
        with self.assertRaises(EvictionCheckError):
            check_access_journal(self.config, mutated)

    def test_float_in_the_target_math_is_caught(self) -> None:
        mutated = self.evict_src.replace(
            "capacity_records.saturating_mul(self.high_water_percent as u64) / 100",
            "(capacity_records as f64 * self.high_water_percent as f64 / 100.0) as u64",
        )
        with self.assertRaises(EvictionCheckError):
            check_numeric_boundary(self.config, mutated)

    def test_injected_wall_clock_read_is_caught(self) -> None:
        mutated = self.evict_src.replace(
            "let target = policy.target_records(capacity_records);",
            "let _leak = std::time::SystemTime::now();\n    let target = policy.target_records(capacity_records);",
            1,
        )
        with self.assertRaises(EvictionCheckError):
            check_determinism(self.config, mutated, self.journal_src)

    def test_deleting_the_existing_factor_read_fn_is_caught(self) -> None:
        # The instrumentation must be ADDITIVE: removing the existing read fn must be caught.
        mutated = self.factor_src.replace(
            "pub fn assemble_factor_inputs(", "pub fn assemble_factor_inputs_REMOVED("
        )
        with self.assertRaises(EvictionCheckError):
            check_instrumentation(self.config, self.bar_src, mutated)

    def test_missing_scheduled_recording_entry_point_is_caught(self) -> None:
        # The canonical scheduled factor path must have a recording-capable entry point.
        mutated = self.factor_src.replace(
            "pub fn run_scheduled_factor_job_over_store_recorded",
            "pub fn some_other_helper",
        )
        with self.assertRaises(EvictionCheckError):
            check_instrumentation(self.config, self.bar_src, mutated)

    def test_journal_dropping_ensure_usable_is_caught(self) -> None:
        # Rename the health preflight away entirely (a substring-preserving rename would not remove it).
        mutated = self.journal_src.replace("pub fn ensure_usable", "pub fn probe_health")
        with self.assertRaises(EvictionCheckError):
            check_access_journal(self.config, mutated)

    def test_cli_dropping_the_journal_health_preflight_is_caught(self) -> None:
        # --use-journal must fail closed on an unusable journal (call ensure_usable).
        mutated = self.cli_src.replace(".ensure_usable()", ".dir()")
        with self.assertRaises(EvictionCheckError):
            check_cli(self.config, mutated)

    def test_cli_dropping_the_fail_closed_gate_is_caught(self) -> None:
        mutated = self.cli_src.replace(
            "if !has_source && !parsed.assume_unprotected {",
            "if false {",
        )
        with self.assertRaises(EvictionCheckError):
            check_cli(self.config, mutated)

    def test_cli_dropping_the_nonzero_breach_exit_is_caught(self) -> None:
        mutated = self.cli_src.replace("if !outcome.reached_target {", "if false {")
        with self.assertRaises(EvictionCheckError):
            check_cli(self.config, mutated)


if __name__ == "__main__":
    unittest.main()
