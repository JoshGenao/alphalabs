"""SRS-NOTIF-001 / SRS-SAFE-003 — a blocked live submission must page the
operator over email AND SMS, must not page for planned maintenance, and must not
be silenceable by a forged flag or drowned by a retry storm.

L7 domain (safety) test. ``crates/atp-orchestrator/src/connectivity_notification.rs``
binds ``atp-execution``'s ERR-2 connectivity gate to the SRS-NOTIF-001 dispatcher —
the path that tells a human the platform has lost its broker. Its Rust unit tests
drive the real sink over real dispatcher and store objects; this test shells out
to ``cargo test`` so the safety post-conditions are anchored in the domain layer.

The file path matches ``SAFETY_PATH_RE`` (``connectivity``), so the deterministic
critic requires this pairing — but it would be warranted regardless: a missed
connectivity alert means the operator does not learn the platform stopped
trading.

NOT proven here, and not claimable from this path:

* That connectivity loss is DETECTED WHEN IT HAPPENS. The trigger is a blocked
  live submission, not the disconnect — so a Gateway that drops while no order is
  being routed goes unnoticed until the next blocked order, which may be seconds
  later or never. The stamped detection instant is therefore the OBSERVATION
  instant, and the stored dispatch latency measures observation-to-dispatch, not
  loss-to-dispatch. Reading it as NFR-P6 compliance for the loss would credit
  this path with something it has not shown.
  Closing that needs a connectivity-loss producer watching the gateway
  continuously, which needs the live-IB inbound surface that does not exist yet
  (owners: SRS-MD-003 heartbeat monitor, SRS-EXE-001 execution runtime).
* That a REAL IB Gateway outage drives this path at all, or that the relay
  delivered to a real mailbox and handset. Those are the operator run that flips
  SRS-NOTIF-001.
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
        pytest.skip(reason="cargo not on PATH; cannot run Rust unit test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-orchestrator",
            "--lib",
            f"connectivity_notification::tests::{test_name}",
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
        f"{label} Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


def test_an_unreachable_gateway_pages_over_both_required_channels() -> None:
    # SYS-46: the whole point of the feature. A blocked live submission reaches
    # the operator on email AND SMS, inside the NFR-P6 60s dispatch budget.
    _assert_one_passed(
        _run_cargo_test("an_unreachable_gateway_dispatches_over_both_required_channels"),
        "SRS-NOTIF-001 connectivity page",
    )


def test_planned_maintenance_is_suppressed_but_still_recorded() -> None:
    # SYS-75: a scheduled restart is not a fault. Nothing is sent, but the stored
    # event records both channels as Suppressed -- proof the dispatcher CHOSE
    # silence, which a dropped alert could not produce.
    _assert_one_passed(
        _run_cargo_test("a_scheduled_restart_window_is_suppressed_not_sent"),
        "SRS-NOTIF-001 restart-window suppression",
    )


def test_a_forged_maintenance_flag_cannot_silence_a_real_outage() -> None:
    # The ConnectivityEvent carries both a state and a scheduled_restart bool.
    # Trusting the bool alone would let whoever builds the event silence a
    # genuine outage. Suppression requires both to agree; disagreement pages.
    _assert_one_passed(
        _run_cargo_test("a_scheduled_restart_flag_alone_cannot_silence_a_genuine_outage"),
        "SRS-NOTIF-001 forged-flag guard",
    )


def test_a_healthy_gateway_never_fabricates_an_outage() -> None:
    # No-fabrication: a Connected observation must not write a false outage into
    # the operator's audit trail.
    _assert_one_passed(
        _run_cargo_test("a_healthy_state_never_fabricates_an_outage_alert"),
        "SRS-NOTIF-001 no fabricated outage",
    )


def test_a_maintenance_window_cannot_silence_a_real_outage() -> None:
    # The false-all-clear case, and the sharpest one in this file. A scheduled
    # restart is suppressed -- it SENDS NOTHING -- so if it also arms the shared
    # cool-down, a genuine Unreachable arriving inside that window is coalesced
    # and the operator is never paged. A restart window is exactly when a real
    # failure is most likely and least distinguishable from the planned
    # disconnect, which makes it the worst possible moment to go quiet.
    # Outage and maintenance now hold independent windows.
    _assert_one_passed(
        _run_cargo_test("a_maintenance_window_cannot_silence_a_real_outage_that_follows_it"),
        "SRS-NOTIF-001 maintenance must not mask an outage",
    )


def test_an_outage_does_not_consume_the_maintenance_budget() -> None:
    # The converse direction of the same independence property.
    _assert_one_passed(
        _run_cargo_test("an_outage_does_not_consume_the_maintenance_windows_budget"),
        "SRS-NOTIF-001 window independence",
    )


def test_a_retry_storm_pages_once_and_admits_what_it_folded() -> None:
    # The sink fires once per BLOCKED ORDER, not once per outage. Without
    # coalescing, a retry loop pages hundreds of times, burns the SMS budget, and
    # buries the first useful alert. Coalescing is never silent -- the count
    # rides in the next alert, so a storm cannot read as one isolated block.
    _assert_one_passed(
        _run_cargo_test("a_retry_storm_pages_once_and_reports_the_coalesced_count"),
        "SRS-NOTIF-001 alert-storm control",
    )


def test_the_cooldown_is_armed_by_the_attempt_not_by_success() -> None:
    # A provider outage is exactly when every send fails. Arming the cool-down on
    # success would leave a broken provider un-rate-limited.
    _assert_one_passed(
        _run_cargo_test("the_cooldown_is_armed_by_the_attempt_not_by_success"),
        "SRS-NOTIF-001 cool-down arming",
    )


def test_recording_an_alert_does_not_delay_the_reconnect() -> None:
    # `record` runs inside ExecutionEngine::submit_live_order, which calls
    # connectivity.request_reconnect() immediately AFTER it. Dispatching inline
    # would put up to two per-channel send deadlines between detecting the outage
    # and asking to reconnect -- so reporting the problem would extend it, and the
    # strategy's order path would stall for the same span. The network work is
    # moved off the caller's thread; callers that need the outcome flush().
    #
    # There is deliberately NO inline fallback when the worker cannot be spawned:
    # spawn failure means resource exhaustion, so doing the I/O inline would put
    # ~40s in front of the reconnect at the worst possible moment. Recovery wins
    # over the page, and the miss is recorded rather than silent.
    _assert_one_passed(
        _run_cargo_test("record_returns_immediately_even_when_the_channels_are_slow"),
        "SRS-NOTIF-001 non-blocking record",
    )


def test_a_failed_alert_worker_does_not_silence_the_next_outage() -> None:
    # The cool-down must be armed only once the worker is actually running. Armed
    # before that, a transient resource spike suppresses the page for the whole
    # 5-minute window on a dispatch that never began -- the operator hears nothing
    # about a live outage and no durable event is written either.
    _assert_one_passed(
        _run_cargo_test("a_failed_worker_spawn_does_not_arm_the_cooldown"),
        "SRS-NOTIF-001 failed-spawn must not suppress",
    )


def test_a_failing_transport_never_panics_the_execution_path() -> None:
    # This sink runs INSIDE the execution engine's order-rejection path. A panic
    # here would turn a transport hiccup into an unusable engine.
    _assert_one_passed(
        _run_cargo_test("a_failing_transport_is_recorded_and_never_panics_the_execution_path"),
        "SRS-NOTIF-001 non-panicking sink",
    )


def test_the_delivery_status_is_durably_stored() -> None:
    # The AC's second half: delivery status is stored as a notification event.
    _assert_one_passed(
        _run_cargo_test("the_stored_event_is_appended_to_the_durable_audit_store"),
        "SRS-NOTIF-001 durable storage",
    )
