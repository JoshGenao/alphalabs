"""SRS-EXE-001 / SyRS SYS-2a / SYS-2d / AC-15 — orders route to IB ONLY for
the single designated live strategy.

L7 domain (safety) test. The Rust integration test at
``crates/atp-execution/tests/srs_exe_001_live_designation.rs`` builds a spy
brokerage adapter that counts ``submit_order`` invocations and panic-on-touch
connectivity/freshness stubs; this Python test shells out to ``cargo test``
and asserts the live-designation authority holds end to end:

  * only the single designated strategy reaches the broker;
  * a non-designated strategy is rejected with NON_LIVE_STRATEGY_SUBMISSION
    before any broker / connectivity / freshness port is consulted;
  * with one live + 30 paper strategies, exactly one IB order side effect
    occurs (the SRS-EXE-001 acceptance scenario);
  * designation is exactly-one (SYS-2a) and demotable / re-designable.
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
            "srs_exe_001_live_designation",
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
        f"SRS-EXE-001 Rust domain test failed:\nSTDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def test_only_the_designated_strategy_routes_to_the_broker() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_001_only_the_designated_strategy_routes_to_the_broker")
    )


def test_non_designated_strategy_is_rejected_before_any_port() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_001_non_designated_strategy_is_rejected_before_any_port")
    )


def test_no_designation_rejects_every_strategy() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_001_no_designation_rejects_every_strategy")
    )


def test_one_live_among_thirty_paper_routes_only_the_live() -> None:
    # SRS-EXE-001 acceptance: 1 live + 30 paper -> exactly one IB order.
    _assert_single_pass(
        _run_cargo_test("srs_exe_001_one_live_among_thirty_paper_routes_only_the_live")
    )


def test_exactly_one_designation_is_demotable_and_re_designable() -> None:
    _assert_single_pass(
        _run_cargo_test("srs_exe_001_exactly_one_designation_is_demotable_and_re_designable")
    )
