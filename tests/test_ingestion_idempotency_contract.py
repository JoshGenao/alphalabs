"""Contract tests for SRS-DATA-016 (make ingestion jobs idempotent).

SRS-DATA-016 / SyRS NFR-R4 / StRS SN-1.26, SN-1.27 -- re-running an ingestion for an already
ingested date creates no duplicate records and does not corrupt existing data. This slice ships
the data-layer storage substrate (canonical record + keyed store + idempotent validating write
path) in ``crates/atp-data`` (module ``store``), composing the unchanged ERR-5 validation gate.

Mirrors ``tests/test_backtest_store_contract.py``: shells out to
``tools/ingestion_idempotency_check.py``, then exercises each per-check function in-process,
including negative spot-checks that mutate the Rust source / lib.rs / Cargo.toml in memory and
assert the contract actually catches the regression (a dropped idempotent no-op, a dropped
conflict guard, a store mutator leaked into the ERR-5 gate, a dropped checksum-first ordering, a
dropped durable fsync/rename, a money-into-float field, an injected nondeterminism source, an
injected broker dependency, a leaked vendor token).
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

from ingestion_idempotency_check import (  # noqa: E402
    IngestionIdempotencyCheckError,
    assert_ingestion_idempotency_static,
    cargo_source,
    check_cargo_test_smoke,
    check_codec,
    check_dataset_kind,
    check_determinism,
    check_error_enum,
    check_file_persistence,
    check_ingest_market_record,
    check_ingest_record_unchanged,
    check_no_broker_dependency,
    check_numeric_boundary,
    check_record_hash,
    check_record_struct,
    check_reingest_nonmutating,
    check_store_lock,
    check_upsert,
    check_vendor_isolation,
    cli_source,
    lib_source,
    load_config,
    run_checks,
    store_source,
)


class IdempotencyScriptTest(unittest.TestCase):
    def test_srs_data_016_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/ingestion_idempotency_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SRS-DATA-016 IDEMPOTENT-INGEST PASS", result.stdout)
        for needle in (
            "declares the vendor-neutral DatasetKind taxonomy",
            "keyed by the NaturalKey (kind, symbol, resolution, event_ts, option_contract)",
            "MarketDataStore::upsert is the SRS-DATA-016 idempotency core",
            "fails closed (ConflictingContent) leaving existing data intact",
            "ingest_market_record composes the unchanged ERR-5 gate",
            "envelope DERIVED from the record",
            "a caller cannot validate one payload and store another",
            "derives record_hash as the canonical SHA-256",
            "two distinct records never share a record_hash",
            "ingest_record (the ERR-5 gate) stays store-free",
            "serialize/restore is a deterministic, dependency-free text codec",
            "declares StoreError with 8 fail-closed variants",
            "save_to_path / load_from_path durably persist the store to the ATP_DATA_STORE_DIR",
            "StoreLock is the single-writer guard",
            "two concurrent ingestion jobs cannot lose records",
            "reingest is a non-mutating proof: it never calls save_to_path",
            "no f64 in the core",
            "lib.rs re-exports `pub mod store;`",
            "Cargo.toml declares no dependency on the broker/execution path",
            "store module is free of all 5 forbidden vendor SDK tokens",
        ):
            self.assertIn(needle, result.stdout, f"missing evidence needle: {needle!r}")


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config()
        self.src = store_source(self.config)
        self.lib_src = lib_source(self.config)
        self.cargo_src = cargo_source(self.config)
        self.cli_src = cli_source(self.config)


class DatasetKindTest(_Fixture):
    def test_dataset_kind_evidence(self) -> None:
        self.assertIn(
            "vendor-neutral DatasetKind taxonomy", check_dataset_kind(self.config, self.src)
        )

    def test_dropped_kind_variant_is_caught(self) -> None:
        mutated = self.src.replace("    OptionChainSnapshot,\n", "", 1)
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_dataset_kind(self.config, mutated)
        self.assertIn("OptionChainSnapshot", str(ctx.exception))


class RecordStructTest(_Fixture):
    def test_record_evidence(self) -> None:
        self.assertIn("dedup identity", check_record_struct(self.config, self.src))

    def test_dropped_natural_key_field_is_caught(self) -> None:
        mutated = self.src.replace("pub event_ts: i64,", "pub stamped_at: i64,", 1)
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_record_struct(self.config, mutated)
        self.assertIn("event_ts", str(ctx.exception))


class UpsertTest(_Fixture):
    def test_upsert_evidence(self) -> None:
        self.assertIn("idempotency core", check_upsert(self.config, self.src))

    def test_dropped_noop_is_caught(self) -> None:
        # Turning the identical-content no-op into an insert would create duplicate rows.
        mutated = self.src.replace("UpsertOutcome::UnchangedDuplicate", "UpsertOutcome::Inserted")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_upsert(self.config, mutated)
        self.assertIn("no-op", str(ctx.exception).lower())

    def test_dropped_conflict_guard_is_caught(self) -> None:
        # Dropping the conflicting-content guard would let a re-ingest silently overwrite (corrupt).
        mutated = self.src.replace("StoreError::ConflictingContent", "StoreError::CorruptRecord")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_upsert(self.config, mutated)
        self.assertIn("conflicting", str(ctx.exception).lower())


class IngestMarketRecordTest(_Fixture):
    def test_composition_evidence(self) -> None:
        self.assertIn(
            "composes the unchanged ERR-5 gate",
            check_ingest_market_record(self.config, self.lib_src),
        )

    def test_dropped_gate_composition_is_caught(self) -> None:
        # Bypassing the ERR-5 validation gate would let an invalid record reach the store.
        mutated = self.lib_src.replace("self.ingest_record(", "self.skip_validation(")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_ingest_market_record(self.config, mutated)
        self.assertIn("ERR-5", str(ctx.exception))

    def test_dropped_store_write_is_caught(self) -> None:
        mutated = self.lib_src.replace("store.upsert(", "no_write(")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_ingest_market_record(self.config, mutated)
        self.assertIn("upsert", str(ctx.exception).lower())

    def test_dropped_validation_binding_is_caught(self) -> None:
        # Reverting to an independently-supplied submission (instead of deriving it from the record)
        # would let a caller validate a benign payload and persist a different one -- the [high]
        # Codex finding. The contract must catch the loss of the record-derived envelope.
        mutated = self.lib_src.replace("record.ingestion_submission()", "forged_submission()")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_ingest_market_record(self.config, mutated)
        self.assertIn("DERIVE", str(ctx.exception))


class RecordHashTest(_Fixture):
    def test_record_hash_evidence(self) -> None:
        self.assertIn("canonical SHA-256", check_record_hash(self.config, self.src))

    def test_non_sha256_record_hash_is_caught(self) -> None:
        # Reverting record_hash to a non-SHA-256 (e.g. the FNV-1a checksum) violates the
        # IngestionRecordSubmission type contract -- caught.
        mutated = self.src.replace(
            "sha256::hex(&self.normalized_bytes())",
            'format!("{:016x}", checksum(&self.normalized_bytes()))',
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_record_hash(self.config, mutated)
        self.assertIn("SHA-256", str(ctx.exception))

    def test_value_only_hash_is_caught(self) -> None:
        # Hashing only the value fields (not the full record) would collide across distinct keys.
        # Dropping the full-record encoding from normalized_bytes must be caught.
        mutated = self.src.replace(
            "encode_record(&mut encoded, self)", "encode_fields_only(&mut encoded, self)"
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_record_hash(self.config, mutated)
        self.assertIn("WHOLE record", str(ctx.exception))


class IngestRecordUnchangedTest(_Fixture):
    def test_gate_stays_store_free_evidence(self) -> None:
        self.assertIn("stays store-free", check_ingest_record_unchanged(self.config, self.lib_src))

    def test_store_mutator_leaked_into_gate_is_caught(self) -> None:
        # Injecting a store write into the ERR-5 ingest_record body must be caught (it would break
        # ingestion_validation_check.py's read-only quarantine-arm contract).
        mutated = self.lib_src.replace(
            "match validator.validate(record) {",
            "let _ = store.upsert(record);\n        match validator.validate(record) {",
            1,
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_ingest_record_unchanged(self.config, mutated)
        self.assertIn("store-free", str(ctx.exception).lower())


class CodecTest(_Fixture):
    def test_codec_evidence(self) -> None:
        self.assertIn(
            "deterministic, dependency-free text codec", check_codec(self.config, self.src)
        )

    def test_dropped_checksum_first_is_caught(self) -> None:
        mutated = self.src.replace("if checksum(body) != stored_checksum", "if false")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("checksum", str(ctx.exception).lower())

    def test_reintroduced_unbounded_alloc_is_caught(self) -> None:
        mutated = self.src.replace(
            "let mut fields = Vec::new();",
            "let mut fields = Vec::with_capacity(field_count);",
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_codec(self.config, mutated)
        self.assertIn("untrusted count", str(ctx.exception).lower())


class ErrorEnumTest(_Fixture):
    def test_variants_present(self) -> None:
        evidence = check_error_enum(self.config, self.src)
        for variant in ("ConflictingContent", "DuplicateKey", "Locked", "ChecksumMismatch"):
            self.assertIn(variant, evidence)

    def test_dropped_variant_is_caught(self) -> None:
        mutated = self.src.replace("    ConflictingContent { key: String },", "", 1)
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_error_enum(self.config, mutated)
        self.assertIn("ConflictingContent", str(ctx.exception))


class FilePersistenceTest(_Fixture):
    def test_file_persistence_evidence(self) -> None:
        self.assertIn("durably persist the store", check_file_persistence(self.config, self.src))

    def test_dropped_file_fsync_is_caught(self) -> None:
        mutated = self.src.replace("scratch.sync_all()", "Ok(())")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_file_persistence(self.config, mutated)
        self.assertIn("fsync", str(ctx.exception))

    def test_dropped_missing_dir_failclosed_is_caught(self) -> None:
        mutated = self.src.replace("!dir.is_dir()", "false")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_file_persistence(self.config, mutated)
        self.assertIn("missing directory", str(ctx.exception))

    def test_load_not_delegating_to_restore_is_caught(self) -> None:
        mutated = self.src.replace("Self::restore(&contents)", "Ok(Self::new())")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_file_persistence(self.config, mutated)
        self.assertIn("restore()", str(ctx.exception))


class StoreLockTest(_Fixture):
    def test_store_lock_evidence(self) -> None:
        self.assertIn("single-writer guard", check_store_lock(self.config, self.src))

    def test_dropped_exclusive_create_is_caught(self) -> None:
        # Reverting the atomic O_EXCL create (so two writers both "acquire") would let concurrent
        # ingestion jobs lose records -- the [high] concurrency finding. Must be caught.
        mutated = self.src.replace("create_new(true)", "create(true)")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_store_lock(self.config, mutated)
        self.assertIn("exclusive", str(ctx.exception).lower())

    def test_dropped_release_drop_is_caught(self) -> None:
        mutated = self.src.replace("impl Drop for StoreLock", "impl Debug for StoreLock")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_store_lock(self.config, mutated)
        self.assertIn("Drop", str(ctx.exception))

    def test_cli_not_holding_the_lock_is_caught(self) -> None:
        # If the operator load-modify-save does not actually hold the lock, the guard is decorative.
        # Patch the contract to point cli_guard at a token the CLI does not contain.
        import copy

        mutated_config = copy.deepcopy(self.config)
        mutated_config["ingestion_idempotency_contract"]["store_lock"]["cli_guard_token"] = (
            "NeverPresentInCli::acquire"
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_store_lock(mutated_config, self.src)
        self.assertIn("load-modify-save", str(ctx.exception))


class ReingestProofTest(_Fixture):
    def test_reingest_nonmutating_evidence(self) -> None:
        self.assertIn("non-mutating proof", check_reingest_nonmutating(self.config, self.cli_src))

    def test_reingest_that_saves_is_caught(self) -> None:
        # Injecting a save into cmd_reingest turns a failed idempotency proof into a state-changing
        # ingest (the [high] finding). The contract must catch a save in the reingest body.
        mutated = self.cli_src.replace(
            "    let store_len_after = store.len();",
            "    store.save_to_path(&dir).ok();\n    let store_len_after = store.len();",
            1,
        )
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_reingest_nonmutating(self.config, mutated)
        self.assertIn("save_to_path", str(ctx.exception))


class NumericBoundaryTest(_Fixture):
    def test_numeric_evidence(self) -> None:
        self.assertIn("integer minor units", check_numeric_boundary(self.config, self.src))

    def test_money_into_float_is_caught(self) -> None:
        mutated = self.src.replace("i128::from(field.value_minor)", "field.value_minor as f64")
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_numeric_boundary(self.config, mutated)
        self.assertIn("integer minor units", str(ctx.exception))


class DeterminismTest(_Fixture):
    def test_determinism_evidence(self) -> None:
        self.assertIn("deterministic", check_determinism(self.config, self.src))

    def test_injected_parallelism_is_caught(self) -> None:
        mutated = self.src + "\nfn _leak() { let _ = vec![0].par_iter(); }\n"
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_determinism(self.config, mutated)
        self.assertIn("nondeterminism", str(ctx.exception))


class NoBrokerDependencyTest(_Fixture):
    def test_injected_broker_dependency_is_caught(self) -> None:
        mutated = self.cargo_src + '\natp-execution = { path = "../atp-execution" }\n'
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_no_broker_dependency(self.config, mutated)
        self.assertIn("atp-execution", str(ctx.exception))


class VendorIsolationTest(_Fixture):
    def test_no_vendor_tokens(self) -> None:
        self.assertIn("free of all", check_vendor_isolation(self.config, self.src))

    def test_leaked_vendor_token_is_caught(self) -> None:
        mutated = self.src + "\n// records mirrored to ib_insync under the hood\n"
        with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
            check_vendor_isolation(self.config, mutated)
        self.assertIn("ib_insync", str(ctx.exception))


class CargoSmokeTest(unittest.TestCase):
    def test_missing_cargo_skips_by_default(self) -> None:
        with mock.patch("ingestion_idempotency_check.shutil.which", return_value=None):
            evidence = check_cargo_test_smoke(load_config())
        self.assertIn("skipped", evidence)

    def test_missing_cargo_fails_closed_when_required(self) -> None:
        with mock.patch("ingestion_idempotency_check.shutil.which", return_value=None):
            with self.assertRaises(IngestionIdempotencyCheckError) as ctx:
                check_cargo_test_smoke(load_config(), require_cargo=True)
        self.assertIn("--require-cargo", str(ctx.exception))


class AggregateEvidenceTest(unittest.TestCase):
    def test_static_evidence_is_seventeen_items(self) -> None:
        self.assertEqual(len(assert_ingestion_idempotency_static(load_config(), ROOT)), 17)

    def test_run_checks_emits_eighteen_items(self) -> None:
        # 17 static + 1 cargo smoke (or skipped marker if cargo absent).
        self.assertEqual(len(run_checks()), 18)


if __name__ == "__main__":
    unittest.main()
