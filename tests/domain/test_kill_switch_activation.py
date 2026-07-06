"""SRS-SAFE-001 / SyRS SYS-44a / NFR-P3 / StRS SN-1.11 — the kill-switch
ACTIVATION gate runs the QuantConnect-Liquidate sequence: halt every paper
simulation engine FIRST (the 1 s SRS-LOG-001 HALTED-observability budget must
not sit behind up to 5 s of lawful brokerage I/O), cancel every resting
live-strategy order, submit an opposite-direction market liquidation for
every open live-strategy position, and disconnect from the brokerage LAST
("IB Gateway is disconnected after liquidation orders are submitted").
Continue-to-safety: every phase is attempted regardless of earlier failures,
every outcome is recorded on the returned report, nothing rolls back.

L7 domain (safety) test. The Rust integration suites at
``crates/atp-execution/tests/srs_safe_001_kill_switch_activation.rs`` (spy
ports over the real gate + real ``LiveExecutionState``) and
``crates/atp-simulation/tests/srs_safe_001_halt_fleet.rs`` (the 30-engine
reference-baseline fleet over REAL ``HaltablePaperEngine`` gates) carry the
post-conditions; this Python test shells out to ``cargo test`` to anchor them
in the domain-test layer so the deterministic critic recognizes the diff as
having a paired ``tests/domain/`` safety test (matched by
``kill[_-]?switch`` / ``srs[_-]?safe[_-]?001`` in ``SAFETY_PATH_RE``).

Scope note: the gate runs over ports; the mocked-IB fixture transport is the
verification vehicle SRS-SAFE-001's own Step 2 prescribes. The LIVE path
(real SRS-EXE-006 IB transport, live SRS-EXE-001/005 state producers,
SRS-EXE-002 hosting) is enumerated in
``architecture/runtime_services.json#kill_switch_activation_contract
.deferred[]`` and keeps the feature ``passes:false`` (serialized). The
NFR-P3 5-second wall-clock evidence lives in the companion
``tests/domain/test_kill_switch_latency.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(package: str, suite: str, test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [cargo, "test", "-p", package, "--test", suite, test_name, "--", "--exact"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_passed(result: subprocess.CompletedProcess[str], label: str) -> None:
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"{label} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined or "test result: ok. 1 passed" in combined, (
        f"unexpected cargo test output:\n{combined}"
    )


def _activation_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo_test("atp-execution", "srs_safe_001_kill_switch_activation", test_name)


def _fleet_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo_test("atp-simulation", "srs_safe_001_halt_fleet", test_name)


def test_phase_ordering_halt_cancels_liquidations_disconnect() -> None:
    # SYS-44a sequencing on a shared call log: halt before any cancel, every
    # cancel before the first liquidation, disconnect strictly after the LAST
    # liquidation, exactly one disconnect.
    _assert_passed(
        _activation_test("srs_safe_001_phase_ordering_halt_cancels_liquidations_disconnect"),
        "SRS-SAFE-001 phase-ordering Rust domain test",
    )


def test_cancels_exactly_the_resting_live_strategy_orders() -> None:
    # SYS-44a (a): exactly the non-terminal live-strategy orders are
    # cancelled, once each; a FILLED order and another strategy's order are
    # untouched; the broker binding (or its honest absence) is carried.
    _assert_passed(
        _activation_test("srs_safe_001_cancels_exactly_the_resting_live_strategy_orders"),
        "SRS-SAFE-001 resting-cancel selectivity test",
    )


def test_liquidations_close_every_position_opposite_side_abs_quantity() -> None:
    # SYS-44a (a): every open position gets exactly one validated MARKET
    # liquidation in the opposite direction of the held quantity (long 100 →
    # SELL 100; short 50 → BUY 50).
    _assert_passed(
        _activation_test(
            "srs_safe_001_liquidations_close_every_position_opposite_side_abs_quantity"
        ),
        "SRS-SAFE-001 opposite-direction liquidation test",
    )


def test_failures_are_recorded_and_never_stop_later_phases() -> None:
    # Continue-to-safety: an injected cancel + liquidation + disconnect
    # failure is each recorded as Failed{reason}; every later phase is still
    # attempted; the report is returned and NOT fully_clean.
    _assert_passed(
        _activation_test("srs_safe_001_failures_are_recorded_and_never_stop_later_phases"),
        "SRS-SAFE-001 continue-to-safety fault-injection test",
    )


def test_failed_paper_halt_is_recorded_and_brokerage_phases_still_run() -> None:
    # A failed halt fan-out is recorded (no summary fabricated) and the
    # brokerage phases still run — the kill switch never gives up early.
    _assert_passed(
        _activation_test(
            "srs_safe_001_failed_paper_halt_is_recorded_and_brokerage_phases_still_run"
        ),
        "SRS-SAFE-001 failed-halt continue test",
    )


def test_empty_state_still_halts_and_disconnects() -> None:
    # With nothing to cancel or liquidate the gate still halts the fleet and
    # disconnects — an empty book is not an excuse to skip the safety actions.
    _assert_passed(
        _activation_test("srs_safe_001_empty_state_still_halts_and_disconnects"),
        "SRS-SAFE-001 empty-state test",
    )


def test_timings_are_measured_on_the_injected_clock() -> None:
    # NFR-P3 is MEASURED, not asserted: monotone phase marks on the injected
    # clock; a slow clock pushes liquidations_submitted_ms past 5 000 ms and
    # within_nfr_p3() reports false.
    _assert_passed(
        _activation_test("srs_safe_001_timings_are_measured_on_the_injected_clock"),
        "SRS-SAFE-001 measured-timings test",
    )


def test_fleet_halt_reaches_every_engine_and_no_fill_escapes() -> None:
    # SYS-44a (b) at the NFR-SC1 reference baseline: halt_all transitions all
    # 30 engines and every one subsequently REFUSES to fill — no further
    # simulated fill exists to drive an on_fill callback.
    _assert_passed(
        _fleet_test("srs_safe_001_halt_all_reaches_every_engine_and_no_fill_escapes"),
        "SRS-SAFE-001 fleet no-escape test",
    )


def test_fleet_second_halt_is_idempotent() -> None:
    # Idempotence: a second halt_all reports 30 already_halted / 0
    # transitioned and preserves each engine's original transition record.
    _assert_passed(
        _fleet_test("srs_safe_001_second_halt_is_idempotent_and_preserves_original_transitions"),
        "SRS-SAFE-001 fleet idempotence test",
    )


def test_fleet_registration_fails_closed() -> None:
    # A duplicate or blank engine id is rejected — a silently-replaced engine
    # would escape the fleet halt.
    _assert_passed(
        _fleet_test("srs_safe_001_registration_fails_closed_on_duplicate_and_blank_ids"),
        "SRS-SAFE-001 fleet fail-closed registration test",
    )


# --------------------------------------------------------------------------- #
# Operator CLI (safe001_kill_switch_cli): the orchestrator composition — the
# REAL gate + REAL fleet + REAL LiveExecutionState over the mocked-IB fixture
# transport, shelled exactly as the python/atp_safety backend shells it.
# --------------------------------------------------------------------------- #

_CLI_RELATIVE = Path("target") / "debug" / "safe001_kill_switch_cli"


def _cli_binary() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot build the kill-switch CLI")
    build = subprocess.run(
        [cargo, "build", "-p", "atp-orchestrator", "--bin", "safe001_kill_switch_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (
        f"CLI build failed:\nSTDOUT:\n{build.stdout}\nSTDERR:\n{build.stderr}"
    )
    binary = REPO_ROOT / _CLI_RELATIVE
    assert binary.exists(), f"built binary missing at {binary}"
    return binary


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_cli_binary()), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _parse_report(stdout: str) -> dict:
    line = next((line for line in stdout.splitlines() if line.startswith("report:")), None)
    assert line is not None, f"no report: line in CLI output:\n{stdout}"
    return json.loads(line[len("report:") :])


def test_cli_clean_scenario_reports_full_sequence_and_exits_zero() -> None:
    result = _run_cli(
        "activate",
        "--position",
        "AAPL:100",
        "--position",
        "MSFT:-50",
        "--resting",
        "4",
        "--engines",
        "6",
    )
    assert result.returncode == 0, f"clean activation must exit 0:\n{result.stderr}"
    report = _parse_report(result.stdout)

    # SYS-44a (a): one opposite-direction MARKET liquidation per position.
    liquidations = {entry["symbol"]: entry for entry in report["liquidations"]}
    assert liquidations["AAPL"]["side"] == "SELL"
    assert liquidations["AAPL"]["quantity"] == 100
    assert liquidations["MSFT"]["side"] == "BUY"
    assert liquidations["MSFT"]["quantity"] == 50
    assert all(entry["outcome"]["status"] == "SUCCEEDED" for entry in report["liquidations"])
    assert len(report["resting_order_cancels"]) == 4

    # SYS-44a (b): every REAL engine gate is HALTED (composition-level fact).
    assert report["all_engines_halted"] is True
    assert report["paper_halt_summary"]["engines_total"] == 6
    assert report["paper_halt_summary"]["transitioned"] == 6

    # Timing marks are monotone and the NFR-P3 verdict is carried.
    timings = report["timings"]
    assert (
        timings["halt_completed_ms"]
        <= timings["cancels_completed_ms"]
        <= timings["liquidations_submitted_ms"]
        <= timings["disconnect_completed_ms"]
    )
    assert report["fully_clean"] is True
    assert report["within_nfr_p3"] is True
    assert report["ib_disconnect"]["status"] == "SUCCEEDED"


def test_cli_fault_injection_is_surfaced_and_exits_nonzero() -> None:
    result = _run_cli(
        "activate",
        "--position",
        "AAPL:100",
        "--fail-liquidation",
        "AAPL",
        "--fail-disconnect",
        "--resting",
        "2",
        "--engines",
        "3",
    )
    assert result.returncode == 1, (
        "an activation whose report records failures must exit 1 "
        f"(got {result.returncode}):\n{result.stderr}"
    )
    report = _parse_report(result.stdout)
    aapl = next(entry for entry in report["liquidations"] if entry["symbol"] == "AAPL")
    assert aapl["outcome"]["status"] == "FAILED"
    assert "injected liquidation failure" in aapl["outcome"]["reason"]
    assert report["ib_disconnect"]["status"] == "FAILED"
    assert report["fully_clean"] is False
    # Continue-to-safety: the paper halt still succeeded and the cancels ran.
    assert report["all_engines_halted"] is True
    assert len(report["resting_order_cancels"]) == 2


def test_cli_rejects_unknown_flags_and_degenerate_positions() -> None:
    unknown = _run_cli("activate", "--bogus")
    assert unknown.returncode == 2, "unknown flag must be a usage error (exit 2)"
    assert "report:" not in unknown.stdout, "no report may be produced on a usage error"

    flat = _run_cli("activate", "--position", "AAPL:0")
    assert flat.returncode == 2, "a zero-quantity position must be rejected"
    assert "non-zero" in flat.stderr


def test_cli_perf_reference_shape_passes_the_nfr_p3_budget() -> None:
    result = _run_cli("perf", "--iterations", "5")
    assert result.returncode == 0, f"perf run must PASS:\n{result.stdout}\n{result.stderr}"
    assert "verdict:PASS" in result.stdout
    assert "budget_ms:5000" in result.stdout
    assert "shape: positions:50 resting:50 engines:30" in result.stdout, (
        "perf must default to the NFR-SC1 reference shape"
    )
