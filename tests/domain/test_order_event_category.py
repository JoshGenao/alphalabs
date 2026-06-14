"""SRS-SDK-004 / SyRS SYS-7 / SYS-85 / NFR-P4 — source-neutral order-event
callback category authority.

L7 domain (safety) test. The category authority decides which strategy-facing
callback (FILL / PARTIAL_FILL / CANCELLED / REJECTED / ACK / EXPIRED) a
dispatcher emits for an order-lifecycle transition. It lives in ``atp-types``
(shared, identical for live and paper) and is fail-closed against the
SRS-EXE-008 documented graph, so a dispatcher can never emit a callback for an
impossible transition (e.g. resurrecting a terminal order) and the live and
paper paths cannot drift — both money-safety / parity invariants. The Rust
integration test ``crates/atp-types/tests/srs_sdk_004_order_event.rs`` pins each
invariant end to end; this Python test shells out to ``cargo test`` and asserts:

  * the live and paper paths derive an identical category for an identical
    transition (SRS-SDK-001 / AC-14 parity, by construction);
  * a callback can be produced ONLY by a successful mutation of a TRACKED order
    via the order-bound OrderLedger::transition_with_event — a dispatcher cannot
    fabricate a fill for an order in another state, nor an unknown order;
  * a duplicate / stale / out-of-order broker event that maps to no legal
    modeled transition is fail-closed (the broker event-kind layer that dedups
    and disambiguates such events is owned by SRS-EXE-006 / SRS-SIM-002);
  * internal lifecycle states (PENDING_SUBMIT / CANCEL_PENDING) surface no
    callback even on a legal transition;
  * each destination state classifies correctly when reached by a real
    transition (OrderEventCategory is #[non_exhaustive] + for_state/for_transition
    are non-public — no dispatcher can construct or derive a category off-graph);
  * the four AC-named categories require fill economics and CANCELLED /
    REJECTED / EXPIRED require a reason (the Rust analog of the SDK guard);
  * the NFR-P4 latency budgets are the documented single-source-of-truth numbers.
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
            "srs_sdk_004_order_event",
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
        f"SRS-SDK-004 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_live_and_paper_derive_identical_category() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_live_and_paper_derive_identical_category"))


def test_callback_is_bound_to_a_real_transition() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_callback_is_bound_to_a_real_transition"))


def test_illegal_or_stale_event_is_fail_closed() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_illegal_or_stale_event_is_fail_closed"))


def test_internal_states_surface_no_callback() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_internal_states_surface_no_callback"))


def test_destination_states_classify() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_destination_states_classify"))


def test_ac_named_categories_require_fill_economics() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_ac_named_categories_require_fill_economics"))


def test_latency_budgets_are_the_nfr_p4_numbers() -> None:
    _assert_single_pass(_run_cargo_test("srs_sdk_004_latency_budgets_are_the_nfr_p4_numbers"))
