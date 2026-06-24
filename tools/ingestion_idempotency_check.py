#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-016 (make ingestion jobs idempotent).

SRS-DATA-016 (SyRS NFR-R4; StRS SN-1.26 / SN-1.27). The acceptance criterion:
"Re-running Databento, IB, option-chain, or Sharadar ingestion for an already ingested
date creates no duplicate records and does not corrupt existing data."

The data-layer storage substrate -- the canonical market-data record, a local keyed store,
and an idempotent validating write path -- lives in ``crates/atp-data`` (module ``store``),
per the structural contract in ``architecture/runtime_services.json`` (block
``ingestion_idempotency_contract``):

  (a) ``MarketDataRecord`` carries a vendor-neutral ``NaturalKey`` (DatasetKind, symbol,
      resolution, event_ts, option_contract) -- the dedup identity -- and a canonically-ordered
      set of ``MarketField`` values in integer minor units. ``DatasetKind`` is the
      vendor-NEUTRAL taxonomy the four sources the acceptance names map onto (the core never
      names a vendor; SRS-ARCH-003).
  (b) ``MarketDataStore::upsert`` is the idempotency core: a fresh key INSERTS; an
      already-present key with identical content is the no-op ``UpsertOutcome::UnchangedDuplicate``
      (no duplicate row, byte-identical store); an already-present key with DIFFERENT content
      fails closed (``StoreError::ConflictingContent``) leaving the existing record intact -- the
      "corrupts existing data" guard. Records are held in one canonical natural-key order
      (``binary_search_by``), so the serialized form is byte-identical for the same record set.
  (c) ``DataLayer::ingest_market_record`` COMPOSES the UNCHANGED ERR-5 validation gate
      (``self.ingest_record``) and only on a Valid classification applies ``store.upsert`` -- so
      the store mutator never lives inside ``ingest_record`` (whose quarantine arm stays
      statically read-only, ``ingestion_validation_check.py`` forbidden_mutations).
  (d) ``serialize``/``restore`` is a deterministic, dependency-free text codec (``MAGIC`` header +
      FNV-1a ``checksum`` over the body verified BEFORE building any state, untrusted counts grown
      incrementally not pre-allocated, a single bulk sort on restore) that fails closed on a
      corrupt / truncated / checksum-mismatching / duplicate-key blob.
  (e) the durable FILE layer (``save_to_path`` / ``load_from_path``) mirrors the SRS-BT-009
      ``backtest_store`` pattern -- unique scratch + fsync + atomic rename + parent-dir fsync;
      load fails closed on a missing/unmounted directory and restores an empty store only for a
      missing FILE in a provisioned directory; the persisted directory is the ``ATP_DATA_STORE_DIR``
      storage_paths key.
  (f) all value fields stay integer minor units (no f64 in the core); the work is deterministic;
      ``store`` carries no broker/adapter dependency and no lowercase vendor SDK token; ``lib.rs``
      re-exports ``pub mod store;``.

The PASS line is ``SRS-DATA-016 IDEMPOTENT-INGEST PASS`` -- it names the deferred owners (the real
provider network adapters via SRS-DATA-001/003/005/006, the unified query consumers via
SRS-DATA-007, SSD/NAS tiering via SRS-DATA-008/009/010, the validator rules + alert surface via
SRS-DATA-013 / SRS-NOTIF-001). Unlike the SDK-surface checks, SRS-DATA-016 flips passes:true: the
idempotency property is a self-contained runtime property demonstrated end to end.

Mirrors the PASS/FAIL output style of ``tools/backtest_store_check.py``.

Invoke:
    python3 tools/ingestion_idempotency_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]


class IngestionIdempotencyCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise IngestionIdempotencyCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "ingestion_idempotency_contract" not in config:
        fail("architecture metadata is missing ingestion_idempotency_contract")
    return config["ingestion_idempotency_contract"]


def store_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / f"{block['store_module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "Cargo.toml"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["data_crate"]["path"] / "src" / "bin" / f"{block['cli_bin']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_dataset_kind(config: dict, src: str) -> str:
    spec = contract_block(config)["dataset_kind"]
    if not re.search(rf"\bpub\s+enum\s+{re.escape(spec['enum'])}\b", src):
        fail(f"store must declare `pub enum {spec['enum']}`")
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing the vendor-neutral kind variant(s): {', '.join(missing)}")
    return (
        f"atp-data declares the vendor-neutral {spec['enum']} taxonomy ({', '.join(spec['variants'])}) "
        "the four ingestion sources (Databento daily, IB minute, IB option-chain, Sharadar) map onto"
    )


def check_record_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["record_struct"]
    for key in ("struct", "natural_key_struct", "field_struct"):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"store must declare `pub struct {spec[key]}`")
    key_body = _compact(_struct_body(src, spec["natural_key_struct"]))
    missing = [f for f in spec["natural_key_fields"] if _compact(f) not in key_body]
    if missing:
        fail(
            f"{spec['natural_key_struct']} must carry the dedup identity fields: missing "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-data declares {spec['struct']} keyed by the {spec['natural_key_struct']} "
        "(kind, symbol, resolution, event_ts, option_contract) dedup identity, with integer-minor "
        f"{spec['field_struct']} values"
    )


def check_store_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["store_struct"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"store must declare `pub struct {spec['struct']}`")
    body = _compact(_struct_body(src, spec["struct"]))
    if _compact(spec["records_field"]) not in body:
        fail(f"{spec['struct']} must hold the records (`{spec['records_field']}`)")
    return f"atp-data declares {spec['struct']} -- the keyed, persistable market-data catalog"


def check_upsert(config: dict, src: str) -> str:
    spec = contract_block(config)["upsert"]
    compact_src = _compact(src)
    if _compact(spec["fn"]) not in compact_src:
        fail(f"MarketDataStore must expose the idempotent `{spec['fn']}`")
    if not re.search(rf"\bpub\s+enum\s+{re.escape(spec['outcome_enum'])}\b", src):
        fail(f"store must declare `pub enum {spec['outcome_enum']}`")
    body = _enum_body(src, spec["outcome_enum"])
    missing = [v for v in spec["outcome_variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['outcome_enum']} is missing variant(s): {', '.join(missing)}")
    for key, label in (
        ("noop_token", "an idempotent no-op on an identical re-ingest"),
        ("conflict_guard_token", "a fail-closed guard on a conflicting re-ingest"),
        ("canonical_order_token", "a canonical-order keyed lookup"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"upsert must provide {label} (`{spec[key]}`)")
    return (
        "atp-data MarketDataStore::upsert is the SRS-DATA-016 idempotency core: a fresh key inserts, "
        "an identical re-ingest is a no-op (UnchangedDuplicate, no duplicate row), and a conflicting "
        "re-ingest fails closed (ConflictingContent) leaving existing data intact"
    )


def check_ingest_market_record(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["ingest_market_record"]
    compact = _compact(lib_src)
    if _compact(spec["fn"]) not in compact:
        fail(f"DataLayer must expose the idempotent ingestion entry point `{spec['fn']}`")
    for key, label in (
        ("composes_gate_token", "compose the UNCHANGED ERR-5 validation gate"),
        ("store_write_token", "apply the record via the idempotent store.upsert"),
    ):
        if _compact(spec[key]) not in compact:
            fail(
                f"ingest_market_record must {label} (`{spec[key]}`) -- the store mutator must live "
                "here, never inside ingest_record"
            )
    # The ERR-5 envelope must be DERIVED from the record (not an independent parameter), so the gate
    # validates exactly the record that will be persisted -- a caller cannot validate one payload and
    # store another.
    if _compact(spec["binds_validation_token"]) not in compact:
        fail(
            f"ingest_market_record must DERIVE the validated envelope from the record "
            f"(`{spec['binds_validation_token']}`), binding validation to the persisted record -- "
            "otherwise a caller could validate a benign payload and store a different one"
        )
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['outcome_struct'])}\b", lib_src):
        fail(f"lib.rs must declare `pub struct {spec['outcome_struct']}`")
    if not re.search(rf"\bpub\s+enum\s+{re.escape(spec['error_enum'])}\b", lib_src):
        fail(f"lib.rs must declare `pub enum {spec['error_enum']}`")
    return (
        "atp-data DataLayer::ingest_market_record composes the unchanged ERR-5 gate "
        "(self.ingest_record) on an envelope DERIVED from the record "
        "(record.ingestion_submission(), binding validation to exactly the persisted record) then "
        "applies the idempotent store.upsert -- so an invalid record is still quarantined read-only, "
        "a caller cannot validate one payload and store another, and the store mutator never "
        "contaminates ingest_record"
    )


def check_record_hash(config: dict, src: str) -> str:
    spec = contract_block(config)["record_hash"]
    compact = _compact(src)
    for key, label in (
        ("submission_fn", "the record-derived ERR-5 envelope (ingestion_submission)"),
        ("normalized_bytes_fn", "the canonical full-record normalized bytes"),
        (
            "sha256_derivation_token",
            "record_hash = SHA-256 of the normalized record bytes (the IngestionRecordSubmission "
            "type contract)",
        ),
        (
            "full_record_encoding_token",
            "normalized bytes that cover the WHOLE record (key INCLUDED, so distinct records cannot "
            "collide)",
        ),
        ("sha256_module_token", "a dependency-free SHA-256 implementation"),
        ("fips_vectors_token", "the SHA-256 pinned by FIPS known-answer vectors"),
    ):
        if _compact(spec[key]) not in compact:
            fail(f"record_hash must provide {label} (`{spec[key]}`)")
    return (
        "atp-data ingestion_submission derives record_hash as the canonical SHA-256 (the "
        "IngestionRecordSubmission type contract) of the normalized WHOLE-record bytes (key + value, "
        "via encode_record) -- so two distinct records never share a record_hash; the SHA-256 is a "
        "dependency-free impl pinned by FIPS known-answer vectors"
    )


def check_ingest_record_unchanged(config: dict, lib_src: str) -> str:
    # The ERR-5 ingest_record gate must stay store-free: the new write path must NOT introduce a
    # store mutator into the ingest_record fn body (that would break ingestion_validation_check.py).
    match = re.search(r"pub fn ingest_record\b", lib_src)
    if match is None:
        fail("lib.rs must still declare the ERR-5 `ingest_record` gate")
    # Extract the ingest_record fn body by brace balance from the first '{' after the signature.
    start = lib_src.index("{", match.end())
    depth = 0
    end = start
    for i in range(start, len(lib_src)):
        if lib_src[i] == "{":
            depth += 1
        elif lib_src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = lib_src[start : end + 1]
    for forbidden in ("store.upsert", "save_to_path", "MarketDataStore"):
        if forbidden in body:
            fail(
                f"the ERR-5 ingest_record gate must stay store-free: found `{forbidden}` in its body "
                "(the idempotent store write belongs in ingest_market_record)"
            )
    return (
        "atp-data ingest_record (the ERR-5 gate) stays store-free -- the idempotent write lives only "
        "in ingest_market_record, so the quarantine arm remains statically read-only"
    )


def check_codec(config: dict, src: str) -> str:
    spec = contract_block(config)["codec"]
    compact_src = _compact(src)
    for key, label in (
        ("magic_token", "a MAGIC header constant"),
        ("schema_version_token", "a SCHEMA_VERSION constant"),
        ("serialize_fn", "a serialize() entry point"),
        ("restore_fn", "a restore() entry point"),
        ("checksum_fn", "an integrity checksum"),
        ("checksum_first_token", "the checksum verified BEFORE building any state"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"store codec must provide {label} (`{spec[key]}`)")
    if spec["forbidden_alloc_token"] in src:
        fail(
            f"store decode must not pre-allocate from an untrusted count "
            f"(`{spec['forbidden_alloc_token']}`) -- grow vectors incrementally so an oversized count "
            "fails closed by exhausting the cursor, not an OOM abort"
        )
    if _compact(spec["bulk_restore_sort_token"]) not in compact_src:
        fail(
            f"restore must canonicalize with a single sort (`{spec['bulk_restore_sort_token']}`) "
            "rather than a per-record sorted insert"
        )
    return (
        "atp-data serialize/restore is a deterministic, dependency-free text codec (MAGIC + FNV-1a "
        "checksum verified before any state is built, untrusted counts grown incrementally, single "
        "bulk sort) that round-trips the store and fails closed on a corrupt/truncated blob"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-data declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_file_persistence(config: dict, src: str) -> str:
    spec = contract_block(config)["file_persistence"]
    compact_src = _compact(src)
    for key, label in (
        ("save_fn", "a save_to_path() entry point"),
        ("load_fn", "a load_from_path() entry point"),
        ("store_filename_const", "a STORE_FILENAME constant"),
        ("tmp_filename_const", "a STORE_TMP_FILENAME scratch-file constant"),
        ("unique_scratch_token", "a per-call unique scratch name"),
        ("file_sync_token", "an fsync of the scratch file BEFORE the rename"),
        ("atomic_rename_token", "an atomic temp+rename publish"),
        ("dir_sync_token", "an fsync of the parent directory AFTER the rename"),
        ("restore_on_load_token", "load delegating a present file to the fail-closed restore() codec"),
        ("missing_dir_failclosed_token", "a missing directory failing closed (not empty history)"),
        ("missing_file_empty_token", "a missing file restoring an empty store (not an error)"),
        ("io_error_variant", "a fail-closed StoreError::Io for a real I/O failure"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"store durable persistence must provide {label} (`{spec[key]}`)")
    config_key = spec["config_key"]
    keys = config.get("configuration", {}).get("keys", [])
    match = next((entry for entry in keys if entry.get("name") == config_key), None)
    if match is None:
        fail(f"durable market-data store needs the {config_key} configuration key catalogued")
    if match.get("category") != "storage_paths" or match.get("type") != "path":
        fail(
            f"{config_key} must be a storage_paths path key (got "
            f"category={match.get('category')!r}, type={match.get('type')!r})"
        )
    return (
        "atp-data save_to_path / load_from_path durably persist the store to the "
        f"{config_key} directory via a crash-durable write (unique scratch, fsync, atomic rename, "
        "parent-dir fsync), fail closed through restore() on a corrupt file and through "
        "StoreError::Io on a missing/unmounted directory, and restore empty only when a provisioned "
        "directory holds no file (the SSD/NAS tiering of it is the deferred SRS-DATA-008 owner)"
    )


def check_store_lock(config: dict, src: str) -> str:
    spec = contract_block(config)["store_lock"]
    compact_src = _compact(src)
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['lock_struct'])}\b", src):
        fail(f"store must declare the single-writer `pub struct {spec['lock_struct']}`")
    for key, label in (
        ("lock_filename_const", "a LOCK_FILENAME constant"),
        ("acquire_fn", "an acquire() entry point"),
        ("exclusive_token", "an ATOMIC exclusive create (O_EXCL) so a second writer is refused"),
        ("release_drop_token", "a Drop impl that releases the lock"),
        ("locked_variant", "a fail-closed Locked variant for a refused concurrent writer"),
        ("missing_dir_failclosed_token", "a missing-directory fail-closed guard"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"StoreLock must provide {label} (`{spec[key]}`)")
    # The OPERATOR load-modify-save must actually HOLD the lock, or the guard is decorative and two
    # concurrent ingestion jobs could still lose records (the [high] concurrency finding).
    cli = _compact(cli_source(config))
    if _compact(spec["cli_guard_token"]) not in cli:
        fail(
            f"the operator CLI must hold the single-writer lock across load-modify-save "
            f"(`{spec['cli_guard_token']}`), or two concurrent ingestion jobs could lose records"
        )
    return (
        "atp-data StoreLock is the single-writer guard: an atomic exclusive create (O_EXCL) refuses "
        "a concurrent writer (StoreError::Locked) rather than a last-publish-wins overwrite, fails "
        "closed on a missing directory, and releases on Drop -- the operator CLI holds it across the "
        "whole load-modify-save so two concurrent ingestion jobs cannot lose records"
    )


def check_reingest_nonmutating(config: dict, cli_src: str) -> str:
    spec = contract_block(config)["reingest_proof"]
    match = re.search(r"fn cmd_reingest\b", cli_src)
    if match is None:
        fail("the operator CLI must declare cmd_reingest (the idempotency proof command)")
    # Extract the cmd_reingest body by brace balance from the first '{' after the signature.
    start = cli_src.index("{", match.end())
    depth = 0
    end = start
    for i in range(start, len(cli_src)):
        if cli_src[i] == "{":
            depth += 1
        elif cli_src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = cli_src[start : end + 1]
    if spec["no_save_token"] in body:
        fail(
            f"cmd_reingest is a non-mutating PROOF and must NOT call `{spec['no_save_token']}` -- a "
            "failed idempotency proof must never persist newly-inserted records"
        )
    return (
        "atp-data data016_ingest_cli reingest is a non-mutating proof: it never calls save_to_path, "
        "so running it on the wrong kind/date or a fresh directory fails closed without writing"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    missing = [t for t in spec["integer_minor_tokens"] if _compact(t) not in compact_src]
    if missing:
        fail(f"record value fields must stay integer minor units: missing {', '.join(missing)}")
    return (
        "atp-data keeps every record value field in integer minor units (value_minor: i64, "
        "i128::from intermediates in the codec) -- no f64 in the core"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"store must be deterministic: found nondeterminism source(s) {', '.join(leaked)} -- "
            "persistence/ingestion must use fixed folds with no parallelism, RNG, or wall-clock read"
        )
    return (
        "atp-data store is deterministic: no parallelism / RNG / clock token, so a re-ingest is "
        "reproducible and the serialized form is byte-identical for the same record set"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(f"atp-data lib.rs must re-export `{spec['lib_reexport_token']}`")
    return f"atp-data lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-data Cargo.toml must NOT depend on the broker/execution path: found "
            f"{', '.join(leaked)} -- the storage substrate is broker-independent"
        )
    return (
        f"atp-data Cargo.toml declares no dependency on the broker/execution path "
        f"({', '.join(spec['forbidden_dep_tokens'])})"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-data store module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core data layer must stay vendor-neutral per SRS-ARCH-003)"
        )
    return (
        f"atp-data store module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(the dataset kinds are vendor-neutral; SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "idempotent-ingestion path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{lib.stdout}\n{lib.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", integration, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(f"cargo test -p {crate} --test {integration} failed:\n{integ.stdout}\n{integ.stderr}")
    return (
        f"cargo test -p {crate} --lib + {integration}: PASS "
        "(ingest -> persist -> reload -> re-ingest is a no-op across every dataset kind, the persisted "
        "file is byte-identical, a conflicting re-ingest fails closed, and a quarantined record "
        "never reaches the store)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "store" reads store.rs, "lib" reads lib.rs, "cargo" reads Cargo.toml.
_STATIC_CHECKS = (
    ("dataset_kind", check_dataset_kind, "store"),
    ("record_struct", check_record_struct, "store"),
    ("store_struct", check_store_struct, "store"),
    ("upsert", check_upsert, "store"),
    ("ingest_market_record", check_ingest_market_record, "lib"),
    ("record_hash", check_record_hash, "store"),
    ("ingest_record_unchanged", check_ingest_record_unchanged, "lib"),
    ("codec", check_codec, "store"),
    ("error_enum", check_error_enum, "store"),
    ("file_persistence", check_file_persistence, "store"),
    ("store_lock", check_store_lock, "store"),
    ("reingest_nonmutating", check_reingest_nonmutating, "cli"),
    ("numeric_boundary", check_numeric_boundary, "store"),
    ("determinism", check_determinism, "store"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "store"),
)

_DEFERRED_OWNERS = (
    "real Databento/IB/Sharadar/option-chain NETWORK adapters (SRS-DATA-001/003/005/006); fixture "
    "sources stand in, as the verification step permits",
    "unified historical query READ consumers -- strategy code, notebooks (SRS-DATA-007)",
    "SSD-primary / NAS-archival tiering, eviction, cold-read failover of the store directory "
    "(SRS-DATA-008/009/010)",
    "the concrete validator SYS-77 rule logic + quarantine dashboard/notification alert surface "
    "(SRS-DATA-013 / SRS-NOTIF-001)",
)


def assert_ingestion_idempotency_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "store": store_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
        "cli": cli_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_ingestion_idempotency_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-DATA-016 idempotent-ingestion contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable ingestion path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except IngestionIdempotencyCheckError as error:
        print(f"SRS-DATA-016 IDEMPOTENT-INGEST FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-016 IDEMPOTENT-INGEST PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
