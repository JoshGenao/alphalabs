"""Contract tests for SRS-SIM-004 (persist paper strategy simulation state).

SRS-SIM-004 / SyRS SYS-89 / StRS SN-1.29 / SN-2.05 — persist paper strategy
simulation state. Acceptance: virtual positions, pending simulated orders,
accumulated metrics, and user state are persisted every 60 seconds by default and
restored within 30 seconds of container restart, excluding warm-up. This slice
ships the deterministic snapshot/restore of the SRS-SIM-003 virtual ledger plus the
cadence config in ``crates/atp-simulation`` (module ``paper_state``); the deferred
halves (the live 60s timer / 30s-restore container wiring via SRS-EXE-002 / SYS-89,
the pending-order store, the SYS-85 / SRS-BT-004 metric family, the Python runtime)
keep ``feature_list.json`` at ``passes:false``.

Mirrors ``tests/test_virtual_ledger_contract.py``: shells out to
``tools/sim_persistence_check.py``, then exercises each per-check function
in-process, including negative spot-checks that mutate the Rust source / Cargo.toml
in memory and assert the contract actually catches the regression (a dropped
envelope field, a narrowed cadence field, a changed default, a removed validation
guard, a dropped error variant, a removed schema gate, a renamed codec fn, a
removed sort, a removed fail-closed invariant, a dropped reserved slot, an injected
float, a dropped lib re-export, an injected broker dependency, a leaked vendor
token).
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = ROOT / "tools"

if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from sim_persistence_check import (  # noqa: E402
    SimPersistenceCheckError,
    assert_sim_persistence_static,
    cargo_source,
    check_cargo_test_smoke,
    check_codec,
    check_config_defaults,
    check_config_struct,
    check_config_validation,
    check_determinism,
    check_error_enum,
    check_fail_closed,
    check_integrity,
    check_module_reexport,
    check_money_invariant,
    check_no_broker_dependency,
    check_reserved_slots,
    check_schema_version,
    check_snapshot_struct,
    check_vendor_isolation,
    lib_source,
    load_config,
    persistence_source,
    run_checks,
)


class SimPersistenceScriptTest(unittest.TestCase):
    def test_srs_sim_004_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/sim_persistence_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-SIM-004 SDK-SURFACE PASS", result.stdout)
        for needle in (
            "PaperStateSnapshot as a versioned envelope (schema_version: i64, config: "
            "PersistenceConfig, book: VirtualLedgerBook)",
            "PersistenceConfig with the SYS-89 cadence (interval_secs: u64, "
            "restore_deadline_secs: u64, persist_on_shutdown: bool)",
            "DEFAULT_INTERVAL_SECS = 60s, DEFAULT_RESTORE_DEADLINE_SECS = 30s, persist on shutdown",
            "fails closed on a zero-second interval or restore deadline "
            "(PersistenceError::NonPositiveConfig) AND on a restore deadline above the SYS-89 30s "
            "ceiling (PersistenceError::RestoreDeadlineTooLong)",
            "PersistenceError with 9 fail-closed variants (CorruptSnapshot, UnknownSchemaVersion, "
            "InconsistentField, DuplicateRecord, UnsupportedSection, NonPositiveConfig, "
            "RestoreDeadlineTooLong, ChecksumMismatch, ShutdownPersistenceRequired)",
            "rejects a foreign blob or unknown version (UnknownSchemaVersion)",
            "restore() round-trip (restore(serialize(capture(book))) == book)",
            "sorts strategies by id and positions by canonical symbol",
            "length-prefixes strings so an OCC option symbol containing spaces round-trips",
            "integrity checksum over the body, verified BEFORE any state is built",
            "fails closed with PersistenceError::ChecksumMismatch under fault injection",
            "fail-closed and atomic",
            "reserves forward-compatible slots (pending simulated orders, accumulated paper "
            "metrics, user-state dictionary)",
            "fails closed (UnsupportedSection) on a non-empty slot",
            "paper_state money is integer minor units: no f64",
            "lib.rs re-exports `pub mod paper_state;`",
            "Cargo.toml declares no dependency on the live/broker path (atp-adapters, atp-execution)",
            "paper_state module is free of all 5 forbidden vendor SDK tokens",
            "feature_list.json keeps SRS-SIM-004 passes:false",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = persistence_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)


class SnapshotStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_snapshot_struct(self.config, self.src)
        self.assertIn(
            "schema_version: i64, config: PersistenceConfig, book: VirtualLedgerBook", evidence
        )

    def test_dropped_book_field_is_caught(self) -> None:
        mutated = self.src.replace("book: VirtualLedgerBook,", "ledger: VirtualLedgerBook,", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_snapshot_struct(self.config, mutated)
        self.assertIn("book", str(ctx.exception))


class ConfigStructTest(_Fixture):
    def test_fields_present(self) -> None:
        evidence = check_config_struct(self.config, self.src)
        self.assertIn("interval_secs: u64", evidence)

    def test_narrowed_interval_is_caught(self) -> None:
        # A narrower-than-u64 interval could overflow at large cadences; catch it.
        mutated = self.src.replace("    interval_secs: u64,", "    interval_secs: u32,", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_config_struct(self.config, mutated)
        self.assertIn("interval_secs", str(ctx.exception))


class ConfigDefaultsTest(_Fixture):
    def test_defaults_evidence(self) -> None:
        evidence = check_config_defaults(self.config, self.src)
        self.assertIn("DEFAULT_INTERVAL_SECS = 60s", evidence)

    def test_changed_default_interval_is_caught(self) -> None:
        # The SYS-89 default is 60s; a different default must be caught.
        mutated = self.src.replace(
            "pub const DEFAULT_INTERVAL_SECS: u64 = 60;",
            "pub const DEFAULT_INTERVAL_SECS: u64 = 120;",
            1,
        )
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_config_defaults(self.config, mutated)
        self.assertIn("60", str(ctx.exception))

    def test_dropped_shutdown_default_is_caught(self) -> None:
        # persist_on_shutdown: true appears in both the Default impl and new() (it is
        # mandatory), so drop every occurrence to prove the guard is non-vacuous.
        mutated = self.src.replace("persist_on_shutdown: true,", "persist_on_shutdown: false,")
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_config_defaults(self.config, mutated)
        self.assertIn("shutdown", str(ctx.exception))


class ConfigValidationTest(_Fixture):
    def test_validation_evidence(self) -> None:
        evidence = check_config_validation(self.config, self.src)
        self.assertIn("fails closed", evidence)

    def test_removed_interval_guard_is_caught(self) -> None:
        mutated = self.src.replace("if interval_secs == 0", "if false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_config_validation(self.config, mutated)
        self.assertIn("interval", str(ctx.exception))

    def test_removed_restore_deadline_ceiling_is_caught(self) -> None:
        # Dropping the SYS-89 30s ceiling guard would let the config encode a slower
        # SLA than the requirement allows.
        mutated = self.src.replace(
            "if restore_deadline_secs > DEFAULT_RESTORE_DEADLINE_SECS", "if false", 1
        )
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_config_validation(self.config, mutated)
        self.assertIn("ceiling", str(ctx.exception))


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in ("CorruptSnapshot", "UnsupportedSection", "NonPositiveConfig"):
            self.assertIn(variant, evidence)

    def test_dropped_unsupported_variant_is_caught(self) -> None:
        mutated = self.src.replace("    UnsupportedSection { context: &'static str },", "", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("UnsupportedSection", str(ctx.exception))


class SchemaVersionTest(_Fixture):
    def test_schema_evidence(self) -> None:
        evidence = check_schema_version(self.config, self.src)
        self.assertIn("rejects a foreign blob", evidence)

    def test_removed_version_guard_is_caught(self) -> None:
        # Dropping the schema-version gate would let a future/old layout be
        # mis-read instead of rejected.
        mutated = self.src.replace("if schema_version != SCHEMA_VERSION", "if false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_schema_version(self.config, mutated)
        self.assertIn("schema-version guard", str(ctx.exception))

    def test_removed_magic_guard_is_caught(self) -> None:
        mutated = self.src.replace("if magic != MAGIC", "if false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_schema_version(self.config, mutated)
        self.assertIn("magic-header guard", str(ctx.exception))


class CodecTest(_Fixture):
    def test_codec_evidence(self) -> None:
        evidence = check_codec(self.config, self.src)
        self.assertIn("round-trip", evidence)

    def test_renamed_restore_fn_is_caught(self) -> None:
        mutated = self.src.replace("pub fn restore(", "pub fn renamed_restore(", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("restore", str(ctx.exception))

    def test_broken_restore_body_is_caught(self) -> None:
        # A restore that does not deserialize-then-into_book is not the documented
        # round-trip.
        mutated = self.src.replace(
            "PaperStateSnapshot::deserialize(serialized).map(PaperStateSnapshot::into_book)",
            "Ok(VirtualLedgerBook::new())",
            1,
        )
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("round-trip", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        evidence = check_determinism(self.config, self.src)
        self.assertIn("sorts strategies by id", evidence)

    def test_removed_strategy_sort_is_caught(self) -> None:
        # Without sorting, serialize would emit in HashMap order and churn every
        # checkpoint -- the determinism invariant must catch a dropped sort.
        mutated = self.src.replace("strategies.sort_by", "strategies.iter", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("sort strategies", str(ctx.exception))

    def test_removed_length_prefix_is_caught(self) -> None:
        mutated = self.src.replace("value.len().to_string()", "String::new()", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("length-prefix", str(ctx.exception))


class FailClosedTest(_Fixture):
    def test_fail_closed_evidence(self) -> None:
        evidence = check_fail_closed(self.config, self.src)
        self.assertIn("fail-closed and atomic", evidence)

    def test_removed_biconditional_is_caught(self) -> None:
        mutated = self.src.replace("(quantity == 0) != (cost_basis_minor == 0)", "false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("biconditional", str(ctx.exception))

    def test_removed_canonical_guard_is_caught(self) -> None:
        mutated = self.src.replace("symbol != symbol.to_uppercase()", "false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("non-canonical", str(ctx.exception))

    def test_removed_trailing_guard_is_caught(self) -> None:
        # Dropping the expect_end() call would let trailing/extra bytes through.
        mutated = self.src.replace("cursor.expect_end()?;", "// no end-of-blob check", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("trailing data", str(ctx.exception))

    def test_removed_shutdown_guard_is_caught(self) -> None:
        # Dropping the shutdown-persistence guard would let a snapshot disable a
        # SYS-89-mandatory behavior.
        mutated = self.src.replace("if !persist_on_shutdown", "if false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_fail_closed(self.config, mutated)
        self.assertIn("shutdown", str(ctx.exception))


class ReservedSlotsTest(_Fixture):
    def test_reserved_slots_evidence(self) -> None:
        evidence = check_reserved_slots(self.config, self.src)
        self.assertIn("forward-compatible slots", evidence)

    def test_dropped_slot_is_caught(self) -> None:
        # Removing the pending-orders slot (both the serialize comment and the
        # deserialize guard) drops a forward-compatible sub-state.
        mutated = self.src.replace("pending simulated orders", "removed slot")
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_reserved_slots(self.config, mutated)
        self.assertIn("pending simulated orders", str(ctx.exception))

    def test_removed_unsupported_guard_is_caught(self) -> None:
        mutated = self.src.replace(
            "PersistenceError::UnsupportedSection { context }",
            "PersistenceError::CorruptSnapshot { context }",
        )
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_reserved_slots(self.config, mutated)
        self.assertIn("reserved slot", str(ctx.exception))


class IntegrityTest(_Fixture):
    def test_integrity_evidence(self) -> None:
        evidence = check_integrity(self.config, self.src)
        self.assertIn("integrity checksum", evidence)
        self.assertIn("ChecksumMismatch", evidence)

    def test_missing_checksum_fn_is_caught(self) -> None:
        mutated = self.src.replace("fn checksum", "fn renamed_sum", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_integrity(self.config, mutated)
        self.assertIn("checksum", str(ctx.exception))

    def test_removed_verify_is_caught(self) -> None:
        # Dropping the verification (the Codex finding) would silently accept a
        # structurally-valid tampered snapshot.
        mutated = self.src.replace("checksum(body) != stored_checksum", "false", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_integrity(self.config, mutated)
        self.assertIn("verify", str(ctx.exception))


class MoneyInvariantTest(_Fixture):
    def test_money_evidence(self) -> None:
        evidence = check_money_invariant(self.config, self.src)
        self.assertIn("integer minor units", evidence)

    def test_injected_float_is_caught(self) -> None:
        mutated = self.src + "\nfn _leak() -> f64 { 0.0 }\n"
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_money_invariant(self.config, mutated)
        self.assertIn("f64", str(ctx.exception))


class ModuleReexportTest(_Fixture):
    def test_reexport_evidence(self) -> None:
        evidence = check_module_reexport(self.config, self.lib_src)
        self.assertIn("pub mod paper_state;", evidence)

    def test_missing_reexport_is_caught(self) -> None:
        mutated = self.lib_src.replace("pub mod paper_state;", "pub mod renamed_state;", 1)
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_module_reexport(self.config, mutated)
        self.assertIn("paper_state", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_no_broker_dep_evidence(self) -> None:
        evidence = check_no_broker_dependency(self.config, self.cargo_src)
        self.assertIn("independent of the IB account", evidence)

    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        evidence = check_vendor_isolation(self.config, self.src)
        self.assertIn("free of all", evidence)

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// snapshot replicated through ib_insync under the hood\n"
        with self.assertRaises(SimPersistenceCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    """The runnable persistence path must compile where it matters."""

    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("sim_persistence_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("sim_persistence_check.shutil.which", return_value=None):
            with self.assertRaises(SimPersistenceCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_run_checks_emits_sixteen_items(self) -> None:
        # 15 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 16)

    def test_static_evidence_is_fifteen_items(self) -> None:
        self.assertEqual(len(assert_sim_persistence_static(load_config(), ROOT)), 15)


if __name__ == "__main__":
    unittest.main()
