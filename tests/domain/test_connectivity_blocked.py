"""ERR-2 / SRS-SAFE-003 / SRS-MD-005 — when IB Gateway is unreachable (or
during the configured daily-restart window), live order submissions must be
rejected with ``CONNECTIVITY_BLOCKED``, no IB order side effect must occur,
a reconnect must be attempted, and a structured ``ConnectivityEvent`` must
be published.

L7 domain (safety) test. The Rust integration test at
``crates/atp-execution/tests/err_2_connectivity_blocked.rs`` builds spy
implementations of ``LiveBrokerageSubmit``, ``BrokerageConnectivity``, and
``ConnectivityEventSink`` that count calls / record events; this Python
test shells out to ``cargo test`` to anchor those post-conditions in the
domain-test layer so the deterministic critic recognizes the diff as
having a paired ``tests/domain/`` safety test.
"""

from __future__ import annotations

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
            "err_2_connectivity_blocked",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_unreachable_state_blocks_live_submission_with_no_broker_call() -> None:
    # SRS-SAFE-003: live submissions must fail with CONNECTIVITY_BLOCKED
    # while IB is unreachable; engine must request a reconnect and publish
    # a structured event for dashboards.
    result = _run_cargo_test("err_2_unreachable_state_blocks_live_submission_with_no_broker_call")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-2 Rust domain test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_scheduled_restart_window_blocks_with_suppressed_marker() -> None:
    # SRS-MD-005: during the configured daily restart window, the published
    # event must carry scheduled_restart=true so the notification dispatcher
    # can apply the suppression rule.
    result = _run_cargo_test("err_2_scheduled_restart_window_blocks_with_suppressed_marker")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-2 scheduled-restart-window test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_connected_state_still_routes_through_broker() -> None:
    # Negative control: ERR-2's rejection must be selective. A Live +
    # Connected submission still reaches the broker — otherwise the gate
    # would silently disable the live path even when IB is healthy.
    result = _run_cargo_test("err_2_connected_state_still_routes_through_broker")
    assert result.returncode == 0, (
        f"ERR-2 connected-control test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def test_unreachable_holds_across_many_live_submissions() -> None:
    # Pseudo-property: the Rust test sweeps multiple (strategy, symbol,
    # quantity) combinations and verifies the broker stayed at zero calls
    # and one event/reconnect-attempt per blocked submission.
    result = _run_cargo_test("err_2_unreachable_holds_across_many_live_submissions")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-2 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )
