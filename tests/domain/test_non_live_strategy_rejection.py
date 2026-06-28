"""ERR-1 / SRS-EXE-001 / SRS-ERR-001 — Paper-mode strategies are rejected
synchronously on the live execution path with no IB order side effect.

L7 domain (safety) test. The Rust integration test at
``crates/atp-execution/tests/err_1_no_ib_side_effect.rs`` builds a spy
brokerage adapter that counts ``submit_order`` invocations; this Python
test shells out to ``cargo test`` and asserts the spy was invoked zero
times on Paper submissions. The post-condition is the no-side-effect
guarantee the SRS calls for.
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
            "err_1_no_ib_side_effect",
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_paper_submission_is_rejected_with_no_broker_call() -> None:
    result = _run_cargo_test("err_1_paper_strategy_is_rejected_with_no_broker_call")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-1 Rust domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_paper_submissions_never_reach_broker_across_many_cases() -> None:
    # Pseudo-property: the Rust test sweeps multiple (strategy, symbol,
    # quantity) combinations and verifies the spy stayed at zero calls.
    result = _run_cargo_test("err_1_holds_for_many_paper_submissions")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"ERR-1 pseudo-property test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_live_submission_still_routes_through_broker() -> None:
    # Negative control: ERR-1's rejection must be selective. If a Live
    # submission also got blocked, the live path would silently break.
    result = _run_cargo_test("err_1_live_strategy_still_routes_through_the_broker")
    assert result.returncode == 0, (
        f"ERR-1 live-control test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
