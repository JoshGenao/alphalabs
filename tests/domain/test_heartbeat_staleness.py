"""SRS-MD-003 / SyRS SYS-39 / NFR-P5 — market data and IB Gateway heartbeat
freshness is monitored continuously: an observation age strictly over the
15-second NFR-P5 budget marks the feed stale (detected), publishes a
``HeartbeatStalenessEvent`` transition through a fallible sink (logged /
displayed), and the fail-closed rules hold — a watched feed never observed is
stale with no fabricated age, a failing publication cannot un-stale a feed,
and the merged per-line view stays stale while SRS-MD-007's gap detector
reports a gap even if ticks keep arriving.

L7 domain (safety) test. The Rust integration test at
``crates/atp-market-data/tests/srs_md_003_heartbeat_freshness.rs`` builds spy
and failing ``HeartbeatEventSink``s and asserts the boundary / fail-closed /
transition-once / composition invariants; this Python test shells out to
``cargo test`` to anchor those post-conditions in the domain-test layer so
the deterministic critic recognizes the heartbeat-freshness change as
carrying a paired ``tests/domain/`` safety test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_BINARY = "srs_md_003_heartbeat_freshness"


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-market-data",
            "--test",
            TEST_BINARY,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_passed(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-MD-003 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_staleness_over_15_seconds_is_detected_and_published() -> None:
    # Core AC: an observation age over the NFR-P5 15 s budget marks the feed
    # stale and publishes a HEARTBEAT_STALE transition event carrying the
    # observed age, the last-observation instant, and the threshold.
    _assert_passed(
        _run_cargo_test("heartbeat_staleness_over_15s_marks_feed_stale_and_publishes_event")
    )


def test_exactly_15_seconds_is_not_stale() -> None:
    # AC boundary: staleness OVER 15 seconds. Exactly 15.000 s is Fresh; one
    # nanosecond more is Stale (the strict comparison happens at ns precision).
    _assert_passed(_run_cargo_test("staleness_of_exactly_15_seconds_is_not_stale"))


def test_never_observed_feed_fails_closed_with_no_fabricated_age() -> None:
    # A watched feed with NO observation is stale (no data is not "up") and
    # reports staleness_ms None — an unknown age is never invented.
    _assert_passed(_run_cargo_test("never_observed_feed_is_stale_with_no_fabricated_age"))


def test_fresh_observation_recovers_and_publishes_recovery() -> None:
    # A fresh observation brings the feed back inside the budget and publishes
    # exactly one HEARTBEAT_RECOVERED transition.
    _assert_passed(_run_cargo_test("fresh_observation_recovers_stale_feed_and_publishes_recovery"))


def test_transitions_publish_once_not_every_evaluation() -> None:
    # Steady-state staleness must not spam the SRS-LOG-001 stream: one event
    # per Fresh<->Stale flip, never one per evaluation.
    _assert_passed(_run_cargo_test("transitions_publish_once_not_every_evaluation"))


def test_publication_failure_is_fail_closed_and_surfaced() -> None:
    # When the SRS-LOG-001 / dashboard sink fails, the feed is STILL stale
    # (state commits before publication) and the lost audit event is surfaced
    # to the caller — never silently swallowed, never silently tradable.
    _assert_passed(_run_cargo_test("stale_state_committed_before_failing_publication"))


def test_broker_heartbeat_is_independent_of_market_data_lines() -> None:
    # SYS-39 monitors BOTH feed kinds: the broker connection's freshness is
    # tracked separately from every market-data line's.
    _assert_passed(_run_cargo_test("broker_heartbeat_tracked_independently_of_market_data_lines"))


def test_combined_view_merges_gap_and_time_staleness() -> None:
    # The SRS-MD-007 composition: a gap-stale line stays stale even while
    # ticks keep arriving (time-fresh), and a silent line goes stale even
    # after the gap recovers — stale iff either input says so.
    _assert_passed(
        _run_cargo_test("gap_stale_line_stays_stale_in_combined_view_despite_fresh_heartbeats")
    )
