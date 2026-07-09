"""SRS-EXE-009 / SyRS SYS-90, NFR-R3, NFR-R4 — the durable order-intent **outbox**
commits a live order intent BEFORE submission and, on restart, reconciles the
durable record against the broker's reported state without ever re-firing an order
the broker already has.

L7 domain (safety) test. The acceptance criterion's safety core is that a crash in
the *submit window* — between durably committing an order intent and receiving the
broker's acknowledgement — never causes a duplicate live order (real money). The
outbox therefore (1) durably commits the intent before submission; (2) on restart
treats an acknowledged broker ID as bound to its correlation ID (SRS-EXE-008) and
never resubmits it; (3) adopts an unacknowledged intent the broker already holds and
resubmits one only when the broker view is complete enough to prove it never landed
— failing closed (never resubmitting) on a partial view or an ID conflict; and
(4) retains each entry until its terminal state (FILLED / CANCELLED / REJECTED /
EXPIRED) is observed. A corrupt / missing snapshot fails closed with no partial
state.

The durable substrate lives in ``atp-execution`` (``outbox``); the order lifecycle
it persists is ``atp-types`` (``OrderState`` / ``OrderLifecycle``, SRS-EXE-008),
whose module — and ``live_state`` (SRS-EXE-005) — already defer "the SRS-EXE-009
durable outbox (write-ahead intent commit + acknowledged-broker-ID reconciliation
for the submit-crash window)" to this feature. Each safety invariant is pinned by
one Rust integration test; this Python test shells out to ``cargo test`` and asserts
the safety-relevant subset passes.

Scope (honest bound): the deterministic + fault-injection tests prove the outbox
durability and the reconciliation *decision logic* against a mocked broker. The
concrete ``BrokerOpenOrderSource`` that queries IB's open + recently-completed
orders (SRS-EXE-006 adapter), the wiring of ``commit_intent`` into the live
``submit_live_order`` path (SRS-EXE-001 runtime), the event-driven lifecycle
transitions (SRS-EXE-008), and the end-to-end real-IB restart reconciliation proof
(NFR-R3) are deferred — so SRS-EXE-009 stays ``passes:false``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]

_TARGET = "srs_exe_009_outbox_reconcile"
_CLI_TARGET = "srs_exe_009_outbox_reconcile_cli"


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return cargo


def _run(package: str, test_target: str, test_name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _cargo(),
            "test",
            "-p",
            package,
            "--test",
            test_target,
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


def test_write_ahead_intent_is_durable_before_submission() -> None:
    # AC bullet 1: the intent survives a persist -> reload as an unbound PENDING_SUBMIT.
    _assert_one_passed(
        _run(
            "atp-execution", _TARGET, "srs_exe_009_write_ahead_intent_is_durable_before_submission"
        ),
        "SRS-EXE-009 write-ahead durability",
    )


def test_bound_intent_not_resubmitted_after_restart() -> None:
    # AC bullet 3 (the safety spine): an acknowledged (bound) intent is never
    # resubmitted, even when the broker's open-only view no longer shows it.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_bound_intent_not_resubmitted_after_restart"),
        "SRS-EXE-009 bound intent never resubmitted",
    )


def test_unacked_intent_adopted_when_broker_has_it() -> None:
    # AC bullet 2: an unacknowledged intent the broker already holds is adopted, not
    # resubmitted.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_unacked_intent_adopted_when_broker_has_it"),
        "SRS-EXE-009 adopt unacknowledged intent",
    )


def test_resubmit_only_under_full_coverage() -> None:
    # The crash-window decision: resubmit ONLY under a complete broker view; an
    # open-only view is ambiguous and fails closed (unresolved, never resubmitted).
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_resubmit_only_under_full_coverage"),
        "SRS-EXE-009 resubmit only under full coverage",
    )


def test_id_conflict_never_resubmits() -> None:
    # A broker-id mismatch on a bound intent is surfaced as unresolved and never
    # resubmitted (auto-resubmitting could double a live order).
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_id_conflict_never_resubmits"),
        "SRS-EXE-009 id-conflict never resubmits",
    )


_SEAM = "srs_exe_009_durable_submit"


def test_durable_submit_persists_intent_before_broker() -> None:
    # AC bullet 1 (enforced): the durable-submit seam persists the outbox to disk
    # BEFORE the broker is contacted (the broker stub asserts the snapshot exists).
    _assert_one_passed(
        _run("atp-execution", _SEAM, "srs_exe_009_durable_submit_persists_intent_before_broker"),
        "SRS-EXE-009 durable submit persists before broker",
    )


def test_durable_submit_rejects_non_designated_before_any_record() -> None:
    # The single-live-strategy invariant on the durable path: a non-designated
    # strategy is rejected BEFORE any outbox mutation or broker contact (no durable
    # intent, no IB contact).
    _assert_one_passed(
        _run(
            "atp-execution",
            _SEAM,
            "srs_exe_009_durable_submit_rejects_non_designated_before_any_record",
        ),
        "SRS-EXE-009 durable submit rejects non-designated before any record",
    )


def test_durable_submit_gate_rejection_is_never_resubmittable() -> None:
    # A designated strategy blocked by the inner ERR-2 gate: the intent is durably
    # marked REJECTED (reload from disk proves it), so a restart never resubmits it,
    # and the broker is never reached.
    _assert_one_passed(
        _run(
            "atp-execution",
            _SEAM,
            "srs_exe_009_durable_submit_gate_rejection_is_never_resubmittable",
        ),
        "SRS-EXE-009 durable submit gate rejection never resubmittable",
    )


def test_durable_submit_invalid_order_cannot_poison_recovery() -> None:
    # An invalid submission is rejected BEFORE any durable write, so a persisted
    # invalid order can never brick the fail-closed restore for unrelated valid orders.
    _assert_one_passed(
        _run(
            "atp-execution",
            _SEAM,
            "srs_exe_009_durable_submit_invalid_order_cannot_poison_recovery",
        ),
        "SRS-EXE-009 durable submit invalid order cannot poison recovery",
    )


def test_durable_submit_ack_persist_failure_is_distinct_and_carries_receipt() -> None:
    # A durability fault AFTER the broker accepted the order surfaces AckNotDurable
    # (a live order exists — carry the receipt), distinct from the safe pre-broker
    # WriteAheadPersistence, so a caller never blind-retries a live order.
    _assert_one_passed(
        _run(
            "atp-execution",
            _SEAM,
            "srs_exe_009_durable_submit_ack_persist_failure_is_distinct_and_carries_receipt",
        ),
        "SRS-EXE-009 durable submit ack-persist failure is distinct + carries receipt",
    )


def test_durable_submit_failed_write_ahead_does_not_poison_outbox() -> None:
    # A failed write-ahead durable write fails closed: broker never reached, the
    # in-memory outbox is not poisoned, and nothing durable is written.
    _assert_one_passed(
        _run(
            "atp-execution",
            _SEAM,
            "srs_exe_009_durable_submit_failed_write_ahead_does_not_poison_outbox",
        ),
        "SRS-EXE-009 durable submit failed write-ahead does not poison outbox",
    )


def test_duplicate_broker_rows_never_adopted() -> None:
    # The duplicate-live-order hazard: two broker rows for one correlation key are
    # surfaced as unresolved, never collapsed into a single adopt/skip/resubmit.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_duplicate_broker_rows_never_adopted"),
        "SRS-EXE-009 duplicate broker rows never adopted",
    )


def test_retained_until_terminal() -> None:
    # AC bullet 4: entries are retained until a terminal state is observed, then
    # released — across a restart.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_retained_until_terminal"),
        "SRS-EXE-009 retention until terminal",
    )


def test_no_duplicate_commit_after_restart() -> None:
    # The idempotency spine: after a restart the reloaded outbox rejects a
    # re-committed correlation id as a duplicate.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_no_duplicate_commit_after_restart"),
        "SRS-EXE-009 no duplicate commit after restart",
    )


def test_corrupt_snapshot_fails_closed() -> None:
    # A corrupt-magic / tampered / truncated snapshot is rejected whole (no partial
    # restore) — the fault-injection integrity core.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_corrupt_snapshot_fails_closed"),
        "SRS-EXE-009 corrupt-snapshot fail-closed",
    )


def test_missing_snapshot_fails_closed() -> None:
    # A missing store directory OR a directory with no snapshot file fails closed —
    # recovery never silently restores an empty outbox.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_009_missing_snapshot_fails_closed"),
        "SRS-EXE-009 missing-snapshot fail-closed",
    )


def test_cli_bound_intent_never_resubmitted() -> None:
    # The operator/fault-injection CLI proves the bound-intent safety property e2e.
    _assert_one_passed(
        _run("atp-execution", _CLI_TARGET, "restart_skip_bound_never_resubmits"),
        "SRS-EXE-009 CLI bound-intent never resubmitted",
    )


def test_cli_partial_coverage_never_resubmits() -> None:
    # The CLI fault-injection proves an open-only broker view never triggers a resubmit.
    _assert_one_passed(
        _run("atp-execution", _CLI_TARGET, "injected_partial_coverage_never_resubmits"),
        "SRS-EXE-009 CLI partial-coverage fail-closed",
    )


def test_cli_malformed_invocations_fail_closed() -> None:
    # The CLI rejects every malformed invocation (unknown subcommand/flag/fault,
    # valueless flag, inapplicable fault) with a non-zero exit and no proof token.
    _assert_one_passed(
        _run("atp-execution", _CLI_TARGET, "malformed_invocations_fail_closed"),
        "SRS-EXE-009 CLI malformed-input fail-closed",
    )
