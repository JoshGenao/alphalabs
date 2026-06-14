"""SRS-EXE-008 / SyRS SYS-3 / SYS-7 / SYS-64 / SYS-90 — order lifecycle state
machine + client-correlation-id idempotency.

L7 domain (safety) test. The order lifecycle machine governs order-submission
idempotency — preventing a duplicate submission for the same client
correlation id from creating a second order (no double execution) — and the
single documented transition graph that keeps a terminal order from being
resurrected. Both are money-safety invariants, so the machine lives in
``atp-types`` (shared, identical for live and paper submissions) and is pinned
end to end by the Rust integration test
``crates/atp-types/tests/srs_exe_008_order_lifecycle.rs``. This Python test
shells out to ``cargo test`` and asserts each invariant holds:

  * a duplicate submission for the same correlation id is rejected idempotently
    with the SRS-ERR-001 ``DUPLICATE_CLIENT_CORRELATION_ID`` envelope, and the
    first order is never disturbed nor a second order created;
  * the four terminal states (FILLED / CANCELLED / REJECTED / EXPIRED) have no
    outgoing transition;
  * every edge not in the documented graph is refused without mutating state;
  * cancel-replace is cancel-then-new, retaining the original correlation id on
    the replacement for audit;
  * the client-assigned correlation id is the stable idempotency key — the same
    id always maps to one order, and an unknown id is never auto-created.
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
            "atp-types",
            "--test",
            "srs_exe_008_order_lifecycle",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_single_pass(result: subprocess.CompletedProcess[str]) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-EXE-008 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_duplicate_submission_is_rejected_idempotently() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_008_duplicate_submission_is_rejected_idempotently")
    )


def test_correlation_ids_are_namespaced_per_strategy() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_correlation_ids_are_namespaced_per_strategy"))


def test_terminal_states_have_no_outgoing_transitions() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_terminal_states_have_no_outgoing_transitions"))


def test_illegal_transitions_are_refused() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_illegal_transitions_are_refused"))


def test_cancel_replace_is_cancel_then_new_retaining_original_id() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_008_cancel_replace_is_cancel_then_new_retaining_original_id")
    )


def test_correlation_id_is_the_stable_idempotency_key() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_correlation_id_is_the_stable_idempotency_key"))


def test_partial_fill_racing_cancel_can_still_be_cancelled() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_008_partial_fill_racing_cancel_can_still_be_cancelled")
    )


def test_pending_submit_handles_pre_ack_races() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_pending_submit_handles_pre_ack_races"))


def test_cancel_replace_blocks_doubled_exposure() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_cancel_replace_blocks_doubled_exposure"))


def test_an_original_is_replaced_at_most_once() -> None:
    _assert_single_pass(_run_cargo_test("srs_exe_008_an_original_is_replaced_at_most_once"))
