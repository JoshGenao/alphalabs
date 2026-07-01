"""SRS-MD-007 / SyRS SYS-39 / SYS-39a / SYS-70 / NFR-P5 — the market-data
subscription manager detects tick-sequence gaps and reflects the gap in the
consolidated line's staleness: a forward sequence skip is logged as a
``SequenceGapEvent`` (symbol / expected / observed / timestamp) and marks the
affected line stale; the line recovers only on a fresh monotonic tick OR an
operator-acknowledged resync; the reported ``MarketDataFreshness::Stale`` is
exactly the value the SRS-MD-004 execution gate rejects ``MARKET_DATA_STALE``
on for live AND paper submissions.

L7 domain (safety) test. The Rust integration test at
``crates/atp-market-data/tests/srs_md_007_sequence_gap.rs`` builds a spy
``SequenceGapEventSink`` and asserts the gap / recovery / fail-closed /
per-security-isolation invariants; this Python test shells out to
``cargo test`` to anchor those post-conditions in the domain-test layer so the
deterministic critic recognizes the stale-data change as carrying a paired
``tests/domain/`` safety test.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_BINARY = "srs_md_007_sequence_gap"


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
        f"SRS-MD-007 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_sequence_gap_marks_line_stale_and_logs_event() -> None:
    # Core AC: a forward sequence skip is a gap — logged with symbol / expected
    # / observed / timestamp — and the affected line enters the stale state.
    _assert_passed(_run_cargo_test("sequence_gap_marks_line_stale_and_logs_event"))


def test_monotonic_fresh_tick_recovers_the_line() -> None:
    # Recovery condition #1: a fresh tick with a monotonic sequence clears the
    # stale state (and publishes no additional gap event).
    _assert_passed(_run_cargo_test("monotonic_fresh_tick_recovers_the_line"))


def test_operator_resync_recovers_the_line() -> None:
    # Recovery condition #2: an operator-acknowledged resync returns the line to
    # Fresh and re-baselines so a legitimate post-reconnect jump is not a gap.
    _assert_passed(_run_cargo_test("operator_resync_recovers_the_line"))


def test_stale_freshness_is_the_value_the_md_004_gate_blocks_on() -> None:
    # The MD-007 -> MD-004 seam: the detector reports MarketDataFreshness::Stale
    # on a gap and Fresh after recovery, in the shared atp-types vocabulary the
    # execution gate rejects MARKET_DATA_STALE on. Fail-closed default (an
    # unsubscribed line is Stale) is asserted in the same Rust test.
    _assert_passed(_run_cargo_test("stale_freshness_is_the_value_the_md_004_gate_blocks_on"))


def test_uncanonicalizable_ticks_fail_closed() -> None:
    # An empty-symbol or option tick cannot name a security — it is rejected and
    # can neither advance a sequence nor open a gap.
    _assert_passed(_run_cargo_test("uncanonicalizable_ticks_fail_closed"))


def test_gaps_are_isolated_per_security() -> None:
    # A gap on one security's line never marks another security's line stale.
    _assert_passed(_run_cargo_test("gaps_are_isolated_per_security"))


def test_gap_publication_failure_is_fail_closed_and_surfaced() -> None:
    # When the SRS-LOG-001 / dashboard sink fails to publish the gap event, the
    # line is still marked stale (fail closed) and the failure is surfaced to
    # the caller so the runtime can alert on the lost audit evidence.
    _assert_passed(_run_cargo_test("gap_publication_failure_is_fail_closed_and_surfaced"))


def test_repeated_gaps_preserve_the_original_stale_onset_time() -> None:
    # stale_since_ns records when the line FIRST went stale; a later gap on an
    # already-stale line must not reset the staleness age.
    _assert_passed(_run_cargo_test("repeated_gaps_preserve_the_original_stale_onset_time"))
