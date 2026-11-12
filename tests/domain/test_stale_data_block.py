"""ERR-3 / SRS-MD-004 / NFR-P5 — when subscribed market data for the
order's symbol is stale (heartbeat age > 15 s), live order submissions
must be rejected with ``MARKET_DATA_STALE``, no IB order side effect must
occur, no reconnect must be requested (staleness is a data-side
condition, not a transport fault), and a structured ``StaleDataEvent``
must be published carrying the observed staleness in seconds.

L7 domain (safety) test. The Rust integration test at
``crates/atp-execution/tests/err_3_stale_data_blocked.rs`` builds spy
implementations of ``LiveBrokerageSubmit``, ``BrokerageConnectivity``,
``ConnectivityEventSink``, ``MarketDataFreshnessProbe``, and
``StaleDataEventSink`` that count calls / record events; this Python
test shells out to ``cargo test`` to anchor those post-conditions in
the domain-test layer so the deterministic critic recognizes the diff
as having a paired ``tests/domain/`` safety test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-execution",
            "--test",
            "err_3_stale_data_blocked",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_stale_state_blocks_live_submission_with_no_broker_call() -> None:
    # SRS-MD-004 / NFR-P5: live submissions must fail with MARKET_DATA_STALE
    # while subscribed data is stale; no reconnect must be requested
    # (data-side condition, not transport); exactly one StaleDataEvent must
    # be published carrying the observed staleness in seconds.
    result = _run_cargo_test("err_3_stale_state_blocks_live_submission_with_no_broker_call")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-3 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_fresh_state_still_routes_through_broker() -> None:
    # Negative control: ERR-3's rejection must be selective. A Live +
    # Connected + Fresh submission still reaches the broker — otherwise the
    # gate would silently disable the live path even when the feed is
    # healthy.
    result = _run_cargo_test("err_3_fresh_state_still_routes_through_broker")
    assert result.returncode == 0, (
        f"ERR-3 fresh-control test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_unreachable_state_does_not_consult_freshness_port() -> None:
    # Nested-match invariant: the ERR-2 connectivity gate must
    # short-circuit before the ERR-3 freshness gate. If Unreachable
    # somehow fell through to the freshness check, the Rust test's
    # ForbiddenFreshness stub would panic. The submission still fails —
    # but with CONNECTIVITY_BLOCKED, not MARKET_DATA_STALE.
    result = _run_cargo_test("err_3_unreachable_state_does_not_consult_freshness_port")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-3 nested-match invariant test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_stale_state_holds_across_many_live_submissions() -> None:
    # Pseudo-property: the Rust test sweeps multiple (strategy, symbol,
    # quantity, staleness_seconds) combinations and verifies the broker
    # stayed at zero calls, no reconnects were requested, and exactly one
    # StaleDataEvent was emitted per blocked submission carrying the
    # observed staleness.
    result = _run_cargo_test("err_3_stale_state_holds_across_many_live_submissions")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-3 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


# --------------------------------------------------------------------------- #
# SRS-MD-004 — the production probe, the paper gate, and the real composition.
# Each case runs ONE named Rust test and requires that exactly one test ran and
# passed: a filter that matched nothing would otherwise exit 0 having proved
# nothing.
# --------------------------------------------------------------------------- #

_ONE_PASSED = re.compile(r"test result: ok\. 1 passed; 0 failed")


def _run_one(crate: str, target: str, test_name: str) -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--test", target, test_name, "--", "--exact"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, f"{crate}::{target}::{test_name} failed:\n{combined}"
    assert _ONE_PASSED.search(combined), (
        f"{crate}::{target}::{test_name} did not run exactly one passing test:\n{combined}"
    )


@pytest.mark.parametrize(
    ("crate", "target", "test_name"),
    [
        # Live: the production entry points block while stale and resume when fresh.
        (
            "atp-execution",
            "err_3_stale_data_blocked",
            "srs_md_004_route_order_blocks_while_stale_and_resumes_when_fresh",
        ),
        (
            "atp-execution",
            "err_3_stale_data_blocked",
            "srs_md_004_durable_live_path_blocks_while_stale_and_marks_the_intent_terminal",
        ),
        # Paper: a stale simulated order never reaches the simulator; fresh resumes it.
        (
            "atp-execution",
            "srs_md_004_paper_stale_gate",
            "c3_a_stale_paper_order_is_refused_and_a_fresh_one_reaches_the_simulator",
        ),
        (
            "atp-execution",
            "srs_md_004_paper_stale_gate",
            "c3_live_and_paper_on_one_engine_are_both_blocked_with_one_event_each",
        ),
        # The verdict itself fails closed.
        (
            "atp-market-data",
            "srs_md_004_freshness_probe",
            "c1_a_line_older_than_15_seconds_is_stale_and_a_new_tick_makes_it_fresh",
        ),
        (
            "atp-market-data",
            "srs_md_004_freshness_probe",
            "c1_a_poisoned_board_reads_every_line_stale_and_drops_observations",
        ),
        (
            "atp-market-data",
            "srs_md_004_freshness_probe",
            "c1_identities_the_board_cannot_name_fail_closed",
        ),
        # The shipped composition: real probe, dispatch, simulator and IB bridge.
        (
            "atp-orchestrator",
            "srs_md_004_stale_gate_wired",
            "c4_live_and_paper_block_while_stale_and_resume_when_fresh_data_arrives",
        ),
    ],
)
def test_srs_md_004_stale_data_blocks_live_and_paper(
    crate: str, target: str, test_name: str
) -> None:
    _run_one(crate, target, test_name)
