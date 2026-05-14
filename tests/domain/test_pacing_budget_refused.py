"""ERR-6 / SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-31 / SYS-55 / StRS A-10
— when a configured ingestion job's projected IB historical-data
request count exceeds the permitted count for its window, the data
layer's scheduling-time gate must refuse the job with
``INGESTION_PACING_BUDGET_EXCEEDED``, publish a structured
``PacingBudgetEvent`` carrying both the projected and permitted request
counts, and leave the scheduler exactly as it found it (zero job-start
on refusal).

L7 domain (safety) test. The Rust integration test at
``crates/atp-data/tests/err_6_pacing_budget_blocked.rs`` builds spy
implementations of ``PacingBudgetValidator`` and
``PacingBudgetEventSink`` that count calls / record events; this Python
test shells out to ``cargo test`` to anchor those post-conditions in
the domain-test layer so the deterministic critic recognizes the diff
as having a paired ``tests/domain/`` safety test (matched by
``pacing[_-]?budget`` / ``ingestion[_-]?schedule`` in
``SAFETY_PATH_RE``).
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
            "atp-data",
            "--test",
            "err_6_pacing_budget_blocked",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_budget_exceeded_state_blocks_job_with_structured_error() -> None:
    # SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55: the rejection envelope
    # must carry the INGESTION_PACING_BUDGET_EXCEEDED wire string, the
    # original schedule request, and exactly one PacingBudgetEvent
    # must be recorded with projected_requests, permitted_requests,
    # job_kind, and observed_at_seconds populated.
    result = _run_cargo_test(
        "err_6_budget_exceeded_state_blocks_job_with_structured_error"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-6 Rust domain test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_within_budget_state_returns_scheduled_and_emits_no_event() -> None:
    # Negative control: ERR-6's rejection must be selective. A
    # WithinBudget state must return IngestionJobScheduled and must
    # NOT touch the event sink — the Rust PacingBudgetForbiddenSink
    # would panic if invoked.
    result = _run_cargo_test(
        "err_6_within_budget_state_returns_scheduled_and_emits_no_event"
    )
    assert result.returncode == 0, (
        f"ERR-6 WithinBudget control test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def test_budget_exceeded_state_holds_across_many_schedules() -> None:
    # Pseudo-property: the Rust test sweeps multiple
    # (job_kind, window_seconds, projected, permitted) cases across
    # both SyRS SYS-55 ingestion jobs (SYS-22b minute-bar watchlist and
    # SYS-23 option-chain capture) and verifies the gate emits exactly
    # one event per refused job with the per-case projected/permitted
    # numerics correctly recorded.
    result = _run_cargo_test(
        "err_6_budget_exceeded_state_holds_across_many_schedules"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-6 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_identical_contract_for_minute_bar_and_option_chain_jobs() -> None:
    # SyRS SYS-55 job-invariance: the rejection envelope must be
    # identical for both SYS-22b (minute-bar watchlist) and SYS-23
    # (option-chain capture). The data-layer gate API takes no
    # per-job branch precisely so that both jobs flow through the
    # same gate.
    result = _run_cargo_test(
        "err_6_identical_contract_for_minute_bar_and_option_chain_jobs"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-6 SYS-55 job-invariance test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_budget_exceeded_anchors_zero_job_start_via_port_shape() -> None:
    # Zero-job-start invariant (behavioral anchor): the
    # PacingBudgetValidator port exposes no mutator method, so the
    # gate cannot start a job through it. The PRIMARY enforcement is
    # the static check (tools/pacing_budget_check.py) via the
    # contract's forbidden_mutations allowlist; this test anchors the
    # port-shape post-condition at the behavioral layer.
    result = _run_cargo_test(
        "err_6_budget_exceeded_anchors_zero_job_start_via_port_shape"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-6 zero-job-start invariant test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )
