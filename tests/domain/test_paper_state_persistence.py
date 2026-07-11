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
    check_disk_persistence,
    check_fail_closed,
    check_integrity,
    check_metrics_persistence,
    check_no_broker_dependency,
    check_restore_deadline,
    check_user_state_persistence,
    check_vendor_isolation,
    load_config,
    persistence_source,
)


def _run_cargo_test(
    test_name: str, test_file: str = "srs_sim_004_paper_state"
) -> subprocess.CompletedProcess[str]:
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
            test_file,
            test_name,
            "--",
            "--exact",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_lib_test(test_path: str) -> subprocess.CompletedProcess[str]:
    """Run a single library (in-module) unit test by its fully-qualified path."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust lib test")
    return subprocess.run(
        [cargo, "test", "-p", "atp-simulation", "--lib", test_path, "--", "--exact"],
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


# --------------------------------------------------------------------------- #
# SRS-SIM-004 disk persistence, restore-deadline, metrics/user-state, and the CLI
# --------------------------------------------------------------------------- #


def test_state_survives_a_disk_round_trip() -> None:
    # The safety core of "persist ... and restored": all three captured sub-states
    # (ledger, metrics, user-state) survive an atomic on-disk save then load exactly.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_disk_round_trip_reproduces_full_state"),
        "SRS-SIM-004 disk round-trip fidelity",
    )


def test_metrics_and_user_state_round_trip() -> None:
    # SYS-89 persists accumulated metrics and the user-state dictionary; both must
    # round-trip exactly, not be dropped into a reserved slot.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_metrics_and_user_state_round_trip_exactly"),
        "SRS-SIM-004 metrics + user-state round-trip",
    )


def test_restore_deadline_is_measured_and_enforced() -> None:
    # SYS-89 "restored within 30 seconds": recover_from_path measures the restore
    # phase and enforces the deadline; a file load is well within budget...
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_recover_from_path_measures_and_meets_the_deadline"),
        "SRS-SIM-004 restore within the 30s deadline",
    )
    # ...and an overrun fails closed rather than silently resuming.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_restore_deadline_fails_closed_when_overrun"),
        "SRS-SIM-004 over-deadline restore fails closed",
    )


def test_disk_recovery_fails_closed() -> None:
    # A missing store, a missing snapshot file, or a corrupt on-disk blob must fail
    # closed on recovery, never substituting or partially restoring state.
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_load_from_missing_dir_and_file_fail_closed"),
        "SRS-SIM-004 missing-store recovery fails closed",
    )
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_corrupt_file_on_disk_fails_closed"),
        "SRS-SIM-004 corrupt-store recovery fails closed",
    )
    _assert_one_passed(
        _run_cargo_test("srs_sim_004_non_object_user_state_fails_closed"),
        "SRS-SIM-004 non-dictionary user-state fails closed",
    )


def test_cli_survives_cross_process_restart_and_faults_fail_closed() -> None:
    # The operator fault-injection surface: a persisted snapshot survives a fresh
    # process (the process-level analog of a container restart), and every fault
    # injection makes the restore fail closed.
    _assert_one_passed(
        _run_cargo_test(
            "srs_sim_004_persist_then_restore_survives_a_fresh_process",
            test_file="srs_sim_004_persist_cli",
        ),
        "SRS-SIM-004 cross-process persist/restore survival",
    )
    _assert_one_passed(
        _run_cargo_test(
            "srs_sim_004_every_inject_fault_fails_closed",
            test_file="srs_sim_004_persist_cli",
        ),
        "SRS-SIM-004 every fault injection fails closed",
    )


def test_disk_store_is_atomic_and_fails_closed_on_a_missing_directory() -> None:
    config = load_config()
    # The real store uses the scratch->fsync->rename->dir-fsync recipe and fails
    # closed on a missing store directory.
    check_disk_persistence(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the parent-dir fsync (so the
    # rename may not survive a crash) is caught.
    mutated = persistence_source(config).replace("dir_handle", "renamed_handle")
    with pytest.raises(SimPersistenceCheckError):
        check_disk_persistence(config, mutated)


def test_restore_deadline_guard_is_enforced() -> None:
    config = load_config()
    check_restore_deadline(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the deadline comparison would let
    # a too-slow restore silently resume.
    mutated = persistence_source(config).replace(
        "if restore_elapsed > Duration::from_secs(self.restore_deadline_secs)", "if false", 1
    )
    with pytest.raises(SimPersistenceCheckError):
        check_restore_deadline(config, mutated)


def test_metrics_restore_revalidates_invariants() -> None:
    config = load_config()
    check_metrics_persistence(config, persistence_source(config))
    # ...and the guard must not be vacuous: restoring metrics without re-validating
    # (dropping the from_components call) would trust an incoherent accumulator.
    mutated = persistence_source(config).replace(
        "PaperMetricsAccumulator::from_components(", "trust_blindly(", 1
    )
    with pytest.raises(SimPersistenceCheckError):
        check_metrics_persistence(config, mutated)


def test_metrics_cash_reconciliation_fails_closed() -> None:
    # The adversarial-review safety fix: a persisted PaperMetricsAccumulator whose
    # running cash disagrees with its own trade log (starting cash + each fill's
    # re-derived -(qty*price)-costs delta) must fail closed, so a checksum-valid
    # snapshot from a buggy/foreign writer cannot restore fabricated net-liq equity.
    # Proven at from_components level AND end-to-end through a checksum-VALID blob.
    _assert_one_passed(
        _run_lib_test(
            "paper_metrics::tests::from_components_rejects_cash_that_does_not_reconcile_with_the_trade_log"
        ),
        "SRS-SIM-004 metrics cash-reconcile (from_components)",
    )
    _assert_one_passed(
        _run_lib_test(
            "paper_state::tests::deserialize_rejects_metrics_cash_that_does_not_reconcile"
        ),
        "SRS-SIM-004 metrics cash-reconcile (checksum-valid end-to-end)",
    )


def test_v1_snapshot_migrates_forward_not_stranded() -> None:
    # The adversarial-review schema-evolution safety fix: a legacy v1 (ledger-only)
    # snapshot must still restore after the v2 upgrade -- an upgrade that stranded
    # persisted state would defeat SYS-89 recovery just as badly as losing it.
    _assert_one_passed(
        _run_lib_test("paper_state::tests::deserialize_migrates_a_v1_ledger_only_snapshot"),
        "SRS-SIM-004 v1 snapshot migration",
    )
    # ...and the guard must not be vacuous: dropping the v1 read branch is caught.
    from sim_persistence_check import check_schema_version  # noqa: E402

    config = load_config()
    check_schema_version(config, persistence_source(config))
    mutated = persistence_source(config).replace(
        "if schema_version == SCHEMA_VERSION_V1", "if false", 1
    )
    with pytest.raises(SimPersistenceCheckError):
        check_schema_version(config, mutated)


def test_invalid_user_state_write_is_rejected_and_preserves_last_good_store() -> None:
    # The adversarial-review poison-pill safety fix: persisting a non-dictionary
    # user-state is rejected at the save_to_path write boundary, so a bad caller can
    # never atomically overwrite the last valid checkpoint with a file recovery refuses.
    _assert_one_passed(
        _run_cargo_test(
            "srs_sim_004_save_rejects_non_object_user_state_and_preserves_last_good_store"
        ),
        "SRS-SIM-004 write-boundary user-state poison-pill guard",
    )


def test_adversarial_state_fails_closed_not_aborting() -> None:
    # The adversarial-review DoS-resistance fix: a hostile-but-checksum-valid snapshot
    # must always return a typed PersistenceError, never abort the process, or a foreign
    # writer could crash restart recovery. A huge untrusted record count must not
    # pre-allocate (OOM/panic), and a deeply-nested user-state dict must not overflow the
    # recursive JSON validator (on both the write and restore paths).
    _assert_one_passed(
        _run_lib_test(
            "paper_state::tests::deserialize_rejects_a_huge_untrusted_record_count_without_aborting"
        ),
        "SRS-SIM-004 huge-count DoS resistance",
    )
    _assert_one_passed(
        _run_lib_test("paper_state::tests::deep_user_state_json_fails_closed_without_overflowing"),
        "SRS-SIM-004 deep-JSON DoS resistance",
    )


def test_user_state_restore_requires_a_json_object() -> None:
    config = load_config()
    check_user_state_persistence(config, persistence_source(config))
    # ...and the guard must not be vacuous: dropping the JSON-object validation would
    # restore a non-dictionary user-state value.
    mutated = persistence_source(config).replace("if !is_json_object(&json)", "if false", 1)
    with pytest.raises(SimPersistenceCheckError):
        check_user_state_persistence(config, mutated)
