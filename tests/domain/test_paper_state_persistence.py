"""SRS-SIM-004 / SyRS SYS-89 — persisted paper state restores faithfully, stays
deterministic and broker-independent, and fails closed on corruption.

L7 domain (safety) test. The acceptance criterion's safety core is that a paper
strategy's simulation state (its virtual position ledger) is persisted and
*restored exactly* across a restart, *deterministically* (so a periodic checkpoint
of unchanged state never churns or diverges), *independently of the IB account*,
and *fail-closed* (a corrupt or tampered snapshot is rejected whole, never restored
partially). A leak in any of these is a trading-safety bug: a restart that silently
loses, duplicates, or fabricates a virtual position, or that restores a partial
ledger, would mis-state every downstream paper metric and orchestrator decision.
This test proves the invariant from three angles:

  1. Behavioral — it shells out to the Rust integration test
     ``crates/atp-simulation/tests/srs_sim_004_paper_state.rs`` and asserts that a
     captured snapshot restores the ledger byte-for-byte, that serialization is
     deterministic regardless of insertion order, that per-strategy isolation and
     the cash-reconciliation invariant survive the round trip, that a flat
     fully-closed position keeps its realized P&L, that an OCC option symbol with
     spaces survives, and that a corrupt/tampered/truncated snapshot fails closed
     with no partially-restored state.

  2. Structural (broker independence) — it asserts, via
     ``tools/sim_persistence_check.py``, that the ``atp-simulation`` crate declares
     no dependency on the live/broker path (``atp-execution`` / ``atp-adapters``)
     and that the ``paper_state`` module leaks no vendor SDK token, so persisted
     paper state cannot be reconciled against or routed to the IB account.

  3. Structural (determinism + fail-closed) — it asserts the serializer sorts its
     keys (so the snapshot is deterministic) and the deserializer enforces the
     ledger field invariants (so a corrupt snapshot is rejected).

Each structural guard is checked for non-vacuity: an injected broker dependency, a
leaked vendor token, a removed key sort, and a removed field invariant are each
shown to be caught.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_persistence_check import (  # noqa: E402
    SimPersistenceCheckError,
    cargo_source,
    check_determinism,
    check_fail_closed,
    check_integrity,
    check_no_broker_dependency,
    check_vendor_isolation,
    load_config,
    persistence_source,
)


def _run_cargo_test(test_name: str) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-simulation",
            "--test",
            "srs_sim_004_paper_state",
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


def test_round_trip_reproduces_the_book_exactly() -> None:
    # The safety core: a captured snapshot restores the full ledger byte-for-byte.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_round_trip_reproduces_the_book_exactly"),
        "SRS-SIM-004 round-trip fidelity",
    )


def test_serialization_is_deterministic() -> None:
    # A periodic checkpoint of unchanged state must serialize identically
    # regardless of HashMap order, or it would churn and diverge across restarts.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_serialization_is_deterministic_across_insertion_order"),
        "SRS-SIM-004 deterministic serialization",
    )


def test_isolated_strategies_survive_persistence() -> None:
    # Per-strategy isolation (SYS-84) must hold through persistence.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_isolated_strategies_survive_persistence"),
        "SRS-SIM-004 per-strategy isolation through persistence",
    )


def test_reconciliation_survives_persistence() -> None:
    # Money-correctness: realized P&L minus total cost still reconciles with the
    # simulator's cash after restore -- no charged cost disappears.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_reconciliation_survives_persistence"),
        "SRS-SIM-004 cash reconciliation through persistence",
    )


def test_flat_closed_position_keeps_realized_pnl() -> None:
    # A fully-closed position is flat but still carries realized P&L; persistence
    # must not drop it.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_flat_closed_position_keeps_realized_pnl"),
        "SRS-SIM-004 flat-position history",
    )


def test_occ_option_symbol_survives() -> None:
    # A canonical OCC option symbol contains spaces; length-prefixing must keep it
    # intact through the round trip.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_occ_option_symbol_survives"),
        "SRS-SIM-004 OCC option symbol survival",
    )


def test_corrupt_snapshot_fails_closed() -> None:
    # Negative control: a tampered/truncated snapshot is rejected whole, never
    # restored partially.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_corrupt_snapshot_fails_closed_with_no_partial_state"),
        "SRS-SIM-004 fail-closed restore",
    )


def test_structurally_valid_tampered_value_fails_closed() -> None:
    # The fault-injection safety core: a value altered to another structurally-valid
    # value (sign-consistent quantity/basis) must fail closed via the integrity
    # checksum, not restore as fabricated P&L.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_tampered_value_fails_closed"),
        "SRS-SIM-004 integrity (tamper) fail-closed",
    )


def test_simulation_crate_has_no_broker_dependency() -> None:
    config = load_config()
    # The real Cargo.toml must declare no live/broker-path dependency, so persisted
    # paper state is independent of the IB account at the crate boundary.
    check_no_broker_dependency(config, cargo_source(config))
    # ...and the guard must not be vacuous: an injected broker dep is caught.
    mutated = cargo_source(config) + '\natp-execution = { path = "../atp-execution" }\n'
    with pytest.raises(SimPersistenceCheckError):
        check_no_broker_dependency(config, mutated)


def test_persistence_module_leaks_no_vendor_token() -> None:
    config = load_config()
    # The real persistence module must carry no vendor SDK token.
    check_vendor_isolation(config, persistence_source(config))
    # ...and the guard must not be vacuous: a leaked token is caught.
    mutated = persistence_source(config) + "\n// snapshot mirrored to ib_insync under the hood\n"
    with pytest.raises(SimPersistenceCheckError):
        check_vendor_isolation(config, mutated)


def test_serializer_sorts_keys_for_determinism() -> None:
    config = load_config()
    # The real serializer sorts strategies and positions before emitting.
    check_determinism(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the strategy sort (which would
    # let HashMap order leak into the snapshot and churn checkpoints) is caught.
    mutated = persistence_source(config).replace("strategies.sort_by", "strategies.iter", 1)
    with pytest.raises(SimPersistenceCheckError):
        check_determinism(config, mutated)


def test_deserializer_enforces_field_invariants() -> None:
    config = load_config()
    # The real deserializer enforces the quantity/basis biconditional and the other
    # ledger field invariants.
    check_fail_closed(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the biconditional (which would
    # let a flat position carry a phantom basis) is caught.
    mutated = persistence_source(config).replace(
        "(quantity == 0) != (cost_basis_minor == 0)", "false", 1
    )
    with pytest.raises(SimPersistenceCheckError):
        check_fail_closed(config, mutated)


def test_deserializer_verifies_an_integrity_checksum() -> None:
    config = load_config()
    # The real deserializer verifies a checksum over the body before building state,
    # so a structurally-valid byte change fails closed under fault injection.
    check_integrity(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the verification is caught.
    mutated = persistence_source(config).replace("checksum(body) != stored_checksum", "false", 1)
    with pytest.raises(SimPersistenceCheckError):
        check_integrity(config, mutated)
