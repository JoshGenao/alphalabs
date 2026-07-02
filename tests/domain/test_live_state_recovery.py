"""SRS-EXE-005 / SyRS SYS-90, NFR-R3 — live strategy state persists and recovers
across an execution-engine restart *without duplicate submissions*, re-executing
the warm-up, and failing closed on a corrupt snapshot.

L7 domain (safety) test. The acceptance criterion's safety core is that the live
execution state (the order lifecycle ledger and its correlation IDs, broker IDs,
fills, positions, equity, and the user state dictionary) survives a restart and
is *restored exactly*, so that a strategy which deterministically re-derives a
client correlation ID after a restart has its re-submission rejected as a
duplicate — a restart must never re-fire an order the broker already has. It must
also re-run the SRS-SDK-005 warm-up on restart (so live trading never resumes on
un-warmed indicators), recover within the NFR-R3 60 s deadline (excluding
warm-up), and reject a corrupt / tampered / truncated snapshot whole (never a
partial state that would mis-state the ledger).

The durable substrate lives in ``atp-execution`` (``live_state``); the order
lifecycle ledger it persists is ``atp-types`` (``OrderLedger``, SRS-EXE-008),
whose own module already defers "durable persistence of the ledger across a
process restart" to this feature. Each safety invariant is pinned by one Rust
integration test; this Python test shells out to ``cargo test`` and asserts the
safety-relevant subset passes.

Scope (honest bound): the deterministic tests prove the persistence + restore
*mechanism*. The full end-to-end 60 s container-restart wall-clock proof (a live
strategy container restarted with warm-up reconstructing real indicator buffers)
is a fault-injection/integration exercise the operator runs, so SRS-EXE-005 stays
``passes:false``. Producers of broker IDs / fills / positions / equity (SRS-EXE-006
IB adapter, a live account sync) and the SRS-EXE-009 durable outbox are separate
features; this module persists and restores whatever is captured.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]


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


_TARGET = "srs_exe_005_live_state_recovery"


def test_full_state_round_trips_across_restart() -> None:
    # The captured live state restores exactly (orders, broker IDs, fills,
    # positions, equity, user dictionary) and re-serializes byte-identically.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_round_trip_reproduces_full_live_state"),
        "SRS-EXE-005 round-trip fidelity",
    )


def test_no_duplicate_submission_after_restart() -> None:
    # The safety spine: after a restart the restored ledger rejects a re-submitted
    # correlation ID as a duplicate, so no order is re-fired to the broker.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_no_duplicate_submission_after_restart"),
        "SRS-EXE-005 no duplicate submission after restart",
    )


def test_end_to_end_disk_restart_no_duplicate() -> None:
    # Fault-injection shape: persist to disk -> (process dies) -> recover_from_path
    # -> the restored ledger still rejects the duplicate.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_end_to_end_disk_restart_no_duplicate"),
        "SRS-EXE-005 end-to-end disk restart no duplicate",
    )


def test_warmup_reexecuted_on_restart() -> None:
    # Warm-up (SRS-SDK-005) is re-executed on restart for every strategy with
    # recovered state, so indicators are rebuilt before live trading resumes.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_warmup_reexecuted_on_restart"),
        "SRS-EXE-005 warm-up re-execution on restart",
    )


def test_warmup_failure_aborts_recovery() -> None:
    # Negative control: a failed warm-up fails the recovery closed (no resume on
    # un-warmed indicators).
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_warmup_failure_aborts_recovery"),
        "SRS-EXE-005 warm-up failure aborts recovery",
    )


def test_restore_deadline_exceeded_fails_closed() -> None:
    # The NFR-R3 recovery deadline (60 s, excluding warm-up) is enforced.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_restore_deadline_exceeded_fails_closed"),
        "SRS-EXE-005 restore-deadline enforcement",
    )


def test_corrupt_snapshot_fails_closed() -> None:
    # A truncated / corrupt-magic snapshot is rejected whole (no partial restore).
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_corrupt_snapshot_fails_closed"),
        "SRS-EXE-005 corrupt-snapshot fail-closed",
    )


def test_tampered_value_fails_closed() -> None:
    # The fault-injection integrity core: a structurally-valid byte change fails
    # closed via the checksum, never restoring fabricated state.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_tampered_value_fails_closed"),
        "SRS-EXE-005 integrity (tamper) fail-closed",
    )


def test_recovery_fails_closed_on_missing_snapshot() -> None:
    # A missing store directory OR an existing directory with no snapshot file must
    # fail closed — recovery never silently restores empty state (which would drop
    # the ledger and could allow duplicate submissions after a lost file).
    _assert_one_passed(
        _run(
            "atp-execution",
            _TARGET,
            "srs_exe_005_recovery_fails_closed_on_a_missing_snapshot",
        ),
        "SRS-EXE-005 missing-snapshot fail-closed",
    )


def test_warmup_runs_for_a_registered_strategy_without_orders() -> None:
    # A live strategy with recovered positions but no active order is still warmed
    # up on restart, so it never resumes on cold indicators.
    _assert_one_passed(
        _run(
            "atp-execution",
            _TARGET,
            "srs_exe_005_warmup_reexecutes_for_a_registered_strategy_without_orders",
        ),
        "SRS-EXE-005 warm-up for a registered order-less strategy",
    )


def test_restored_ledger_rejects_corrupt_persisted_set() -> None:
    # The ledger-restore layer (atp-types) fails closed on a duplicate key, a
    # key/strategy mismatch, or a dangling cancel-replace link.
    _assert_one_passed(
        _run("atp-types", "srs_exe_005_ledger_restore", "restore_from_rejects_a_duplicate_key"),
        "SRS-EXE-005 ledger-restore duplicate-key fail-closed",
    )
    _assert_one_passed(
        _run(
            "atp-types",
            "srs_exe_005_ledger_restore",
            "restore_from_rejects_a_dangling_replaces_link",
        ),
        "SRS-EXE-005 ledger-restore dangling-replaces fail-closed",
    )


def test_duplicate_fill_fails_closed() -> None:
    # A duplicate fill identity would double-count an execution on restart.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_duplicate_fill_fails_closed"),
        "SRS-EXE-005 duplicate-fill fail-closed",
    )


def test_user_state_must_be_a_json_object() -> None:
    # The persisted user state dictionary is validated as a JSON object.
    _assert_one_passed(
        _run("atp-execution", _TARGET, "srs_exe_005_user_state_must_be_a_json_object"),
        "SRS-EXE-005 user-state JSON-object validation",
    )
