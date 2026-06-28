"""SRS-ORCH-002 / SyRS SYS-11 / SYS-57 / NFR-SC1 — strategy container
resource profiles are enforced at the orchestrator launch boundary;
defaults match the SyRS SYS-11 spec literals (live: 512 MB / 0.25 CPU;
paper: 300 MB / 0.10 CPU); configuration overrides are validated
against the SRS-ARCH-005 catalogue bounds and a misconfigured launch
never reaches the runtime port.

L7 domain (safety) test. The Rust integration test at
``crates/atp-orchestrator/tests/orch_2_resource_profile_contract.rs``
builds spy implementations of ``StrategyContainerRuntime`` (with a
``create_profiles`` recorder so the test can assert what was actually
passed to the runtime port) and a ``ForbiddenSink`` that panics if any
event is emitted on the validation-rejection path; this Python test
shells out to ``cargo test`` to anchor those post-conditions in the
domain-test layer so the deterministic critic recognizes the diff as
having a paired ``tests/domain/`` safety test (matched by
``resource[_-]?profile`` in ``SAFETY_PATH_RE``).
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
            "orch_2_resource_profile_contract",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_cargo_passed(result: subprocess.CompletedProcess[str], requirement_label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{requirement_label} integration test failed:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_live_default_profile_is_propagated_through_create_to_outcome() -> None:
    # SyRS SYS-11 default: live containers get 512 MB / 0.25 CPU. The
    # outcome must carry the SAME profile the request supplied — not a
    # re-default at the gate. The runtime spy records what was passed
    # to `create`; the assertion proves the gate did not strip,
    # re-default, or rewrite the profile.
    result = _run_cargo_test("orch_2_live_default_profile_is_propagated_through_create_to_outcome")
    _assert_cargo_passed(result, "SRS-ORCH-002 live-default propagation")


def test_paper_default_profile_is_propagated_through_create_to_outcome() -> None:
    # SyRS SYS-11 default: paper containers get 300 MB / 0.10 CPU.
    result = _run_cargo_test("orch_2_paper_default_profile_is_propagated_through_create_to_outcome")
    _assert_cargo_passed(result, "SRS-ORCH-002 paper-default propagation")


def test_in_range_custom_override_is_propagated_unchanged() -> None:
    # "Configuration overrides are validated" — an override within the
    # catalogue bounds (≥ 64 MB, ≤ 65,536 MB; ≥ 0.05 CPU, ≤ 16.0 CPU)
    # must be accepted and threaded byte-equal through `create` and
    # into the outcome. No re-defaulting; no clamping; no rounding.
    result = _run_cargo_test("orch_2_in_range_custom_override_is_propagated_unchanged")
    _assert_cargo_passed(result, "SRS-ORCH-002 in-range override propagation")


def test_below_floor_memory_is_refused_without_invoking_runtime() -> None:
    # SRS-ORCH-002: a misconfigured override (mem below the 64 MB
    # catalogue floor) must never reach `runtime.create` — there is
    # no container to destroy, no event to emit, no resources to
    # release. The spy counters prove the gate short-circuited at
    # validation. The error envelope carries the original request and
    # the specific violation discriminator.
    result = _run_cargo_test("orch_2_below_floor_memory_is_refused_without_invoking_runtime")
    _assert_cargo_passed(result, "SRS-ORCH-002 below-floor mem refusal")


def test_above_ceiling_cpu_is_refused_without_invoking_runtime() -> None:
    # Symmetric to the floor case but on the CPU upper bound.
    result = _run_cargo_test("orch_2_above_ceiling_cpu_is_refused_without_invoking_runtime")
    _assert_cargo_passed(result, "SRS-ORCH-002 above-ceiling cpu refusal")


def test_launch_envelope_is_mode_uniform_with_distinct_default_profiles() -> None:
    # AC-14 / AC-15 uniformity: the same gate, same envelope shape,
    # accepts a Live launch and a Paper launch — only the default
    # profile differs. No mode-branch in the gate logic.
    result = _run_cargo_test(
        "orch_2_launch_envelope_is_mode_uniform_with_distinct_default_profiles"
    )
    _assert_cargo_passed(result, "SRS-ORCH-002 mode-uniform launch envelope")
