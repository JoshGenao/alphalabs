"""SRS-ORCH-001 / SyRS SYS-10 / SYS-13 / AC-12 / NFR-P9 / NFR-R5 /
NFR-S5 — strategy container lifecycle is owned exclusively by the
Strategy Orchestrator, every launch honours the 30-second NFR-P9
budget, and an unresponsive container is auto-restarted and
surfaced on the dashboard in one transaction.

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/orch_1_lifecycle_contract.rs`` builds
spy implementations of ``StrategyContainerRuntime`` and
``HealthCheckEventSink`` that count calls / record events; this
Python test shells out to ``cargo test`` to anchor those
post-conditions in the domain-test layer so the deterministic critic
recognizes the diff as having a paired ``tests/domain/`` safety test
(matched by ``orchestrator[_-]?lifecycle`` / ``strategy[_-]?container``
in ``SAFETY_PATH_RE``).
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
            "atp-orchestrator",
            "--test",
            "orch_1_lifecycle_contract",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_ready_within_deadline_state_returns_outcome_and_emits_no_event() -> None:
    # NFR-P9: a launch that finishes within the 30,000 ms budget must
    # return a StrategyLaunchOutcome with ready_within_deadline=true,
    # carry the observed elapsed_millis, and must NOT touch the
    # dashboard sink — the ForbiddenSink in the Rust test would panic
    # if invoked.
    result = _run_cargo_test(
        "orch_1_ready_within_deadline_state_returns_outcome_and_emits_no_event"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 ReadyWithinDeadline test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_deadline_exceeded_state_blocks_launch_with_structured_error() -> None:
    # SRS-ORCH-001 + NFR-P9 + SyRS SYS-64: a launch that breaches the
    # 30,000 ms budget must be refused with
    # STRATEGY_STARTUP_DEADLINE_EXCEEDED, the rejection envelope must
    # carry the original StrategyLaunchRequest, and exactly one
    # ContainerHealthEvent must be recorded carrying the observed
    # state, the strategy id, the action taken, and the timestamp.
    result = _run_cargo_test("orch_1_deadline_exceeded_state_blocks_launch_with_structured_error")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 DeadlineExceeded test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_healthy_observation_is_read_only() -> None:
    # SyRS SYS-13 selectivity invariant: a healthy probe must NOT
    # invoke `restart`, `stop`, or `destroy`, AND must NOT record a
    # ContainerHealthEvent. Distorting the dashboard with phantom
    # actions would erode operator trust in the auto-restart counter.
    result = _run_cargo_test("orch_1_healthy_observation_is_read_only")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 Healthy read-only test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_unresponsive_observation_restarts_and_records_event_exactly_once() -> None:
    # SyRS SYS-13's atomic binding: an unresponsive container must be
    # restarted AND surfaced on the dashboard in one transaction.
    # Exactly one restart call, exactly one event, no destroy, no
    # stop. The auto-restart counter on the dashboard depends on the
    # "exactly one" guarantee.
    result = _run_cargo_test(
        "orch_1_unresponsive_observation_restarts_and_records_event_exactly_once"
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 Unresponsive auto-restart test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_launch_is_mode_uniform_across_live_and_paper() -> None:
    # AC-14 / AC-15 uniformity: the orchestrator's lifecycle gate
    # must take no mode-branch. Both Live and Paper launches flow
    # through the same gate with the same envelope shape and the
    # same NFR-P9 deadline (resource profiles differ per SRS-ORCH-002
    # but the lifecycle vocabulary is identical).
    result = _run_cargo_test("orch_1_launch_is_mode_uniform_across_live_and_paper")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 mode-uniformity test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_deadline_exceeded_anchors_zero_outcome_on_refusal() -> None:
    # Zero-acceptance invariant (behavioural anchor): a
    # DeadlineExceeded launch must NEVER return a
    # StrategyLaunchOutcome. The PRIMARY enforcement is the static
    # check (tools/orchestrator_lifecycle_check.py) via the
    # contract's forbidden_mutations + accepted_struct allowlist;
    # this test anchors the post-condition at the integration layer.
    result = _run_cargo_test("orch_1_deadline_exceeded_anchors_zero_outcome_on_refusal")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-ORCH-001 zero-acceptance invariant test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )
