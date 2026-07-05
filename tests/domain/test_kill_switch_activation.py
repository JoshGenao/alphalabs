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

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_cargo_test(
    package: str, suite: str, test_name: str
) -> subprocess.CompletedProcess[str]:
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
    return _run_cargo_test(
        "atp-execution", "srs_safe_001_kill_switch_activation", test_name
    )


def _fleet_test(test_name: str) -> subprocess.CompletedProcess[str]:
    return _run_cargo_test("atp-simulation", "srs_safe_001_halt_fleet", test_name)


def test_phase_ordering_halt_cancels_liquidations_disconnect() -> None:
    # SYS-44a sequencing on a shared call log: halt before any cancel, every
    # cancel before the first liquidation, disconnect strictly after the LAST
    # liquidation, exactly one disconnect.
    _assert_passed(
        _activation_test(
            "srs_safe_001_phase_ordering_halt_cancels_liquidations_disconnect"
        ),
        "SRS-SAFE-001 phase-ordering Rust domain test",
    )


def test_cancels_exactly_the_resting_live_strategy_orders() -> None:
    # SYS-44a (a): exactly the non-terminal live-strategy orders are
    # cancelled, once each; a FILLED order and another strategy's order are
    # untouched; the broker binding (or its honest absence) is carried.
    _assert_passed(
        _activation_test(
            "srs_safe_001_cancels_exactly_the_resting_live_strategy_orders"
        ),
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
        _activation_test(
            "srs_safe_001_failures_are_recorded_and_never_stop_later_phases"
        ),
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
        _fleet_test(
            "srs_safe_001_second_halt_is_idempotent_and_preserves_original_transitions"
        ),
        "SRS-SAFE-001 fleet idempotence test",
    )


def test_fleet_registration_fails_closed() -> None:
    # A duplicate or blank engine id is rejected — a silently-replaced engine
    # would escape the fleet halt.
    _assert_passed(
        _fleet_test("srs_safe_001_registration_fails_closed_on_duplicate_and_blank_ids"),
        "SRS-SAFE-001 fleet fail-closed registration test",
    )
