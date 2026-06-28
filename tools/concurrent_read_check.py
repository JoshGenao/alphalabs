#!/usr/bin/env python3
"""Contract evidence script for SRS-DATA-017 (support concurrent reads during ingestion writes).

SRS-DATA-017 (SyRS SYS-63; StRS SN-1.26 / SN-1.28). The acceptance criterion: "Strategy containers,
backtests, factor jobs, and notebooks read previously ingested data while ingestion jobs write new
data WITHOUT corruption or blocking completed data." Verification mode: Load test.

This is a runtime CONCURRENCY property of the SRS-DATA-016 storage substrate (the canonical
``MarketDataStore`` in ``crates/atp-data``, module ``store``), not a new code surface. The substrate
already provides snapshot-isolated reads -- this check pins the four invariants that make
concurrent-read-during-write safe, so a later edit that breaks any of them fails the gate:

  (a) the READER path is LOCK-FREE -- the ``data007_query_cli`` query binary takes no
      ``StoreLock::acquire`` anywhere, and the ``data016_ingest_cli`` ``inspect`` reader takes none
      either (it only ``load_from_path``s), so a read never blocks on, nor is blocked by, an
      ingestion write;
  (b) the WRITER is SERIALIZED -- ``data016_ingest_cli`` ``cmd_ingest`` holds the single-writer
      ``StoreLock`` across the whole load-modify-save (acquire -> ``load_from_path`` ->
      ``save_to_path``), so two ingestion jobs cannot last-publish-wins over each other;
  (c) the WRITE is ATOMICALLY PUBLISHED -- ``save_to_path`` fsyncs a scratch file then ``fs::rename``s
      it onto the live store (and fsyncs the parent dir), so a concurrent reader observes the whole
      old file or the whole new file, never a torn read;
  (d) the READ FAILS CLOSED -- ``load_from_path`` routes a present file through the checksum-first
      ``restore`` (the integrity checksum is verified BEFORE any record is decoded:
      ``checksum(body) != stored_checksum -> StoreError::ChecksumMismatch``), so a partial / corrupt
      read is an error, never a silently-partial store;
  and the single-writer lock itself is an O_EXCL ``create_new(true)`` guard refused with
  ``StoreError::Locked``.

Together: snapshot isolation via copy-on-write atomic file replacement + a single-writer lock +
lock-free readers ("readers never block writers, writers never block readers, no torn reads"). The
Load test (the AC's verification mode) is the Rust integration test ``srs_data_017_concurrent_reads``
(a lock-held writer thread + several lock-free reader threads over real files) plus the gated
cross-process Python load test.

The PASS line is ``SRS-DATA-017 CONCURRENT-READ PASS``. Mirrors the PASS/FAIL output style of
``tools/unified_query_check.py``.

Invoke:
    python3 tools/concurrent_read_check.py [--require-cargo]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConcurrentReadCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ConcurrentReadCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "concurrent_read_runtime_contract" not in config:
        fail("architecture metadata is missing concurrent_read_runtime_contract")
    return config["concurrent_read_runtime_contract"]


def _crate_path(config: dict, root: Path) -> Path:
    return root / contract_block(config)["data_crate"]["path"]


def store_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_path(config, root) / "src" / f"{block['store_module']}.rs"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def read_cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_path(config, root) / "src" / "bin" / f"{block['read_cli_bin']}.rs"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def write_cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_path(config, root) / "src" / "bin" / f"{block['write_cli_bin']}.rs"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    path = _crate_path(config, root) / "Cargo.toml"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)}")
    return path.read_text(encoding="utf-8")


def load_test_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    path = _crate_path(config, root) / "tests" / f"{block['rust_integration_test']}.rs"
    if not path.exists():
        fail(f"source missing: {path.relative_to(root)} (the SRS-DATA-017 Load test)")
    return path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide or split a token."""
    return re.sub(r"\s+", "", text)


def _fn_body(src: str, fn_name: str) -> str:
    """Return the brace-matched body of ``fn <fn_name>`` (with or without ``pub``)."""
    match = re.search(rf"\b(?:pub\s+)?fn\s+{re.escape(fn_name)}\b[^{{]*\{{", src)
    if not match:
        fail(f"Rust source is missing function `{fn_name}`")
    depth = 1
    index = match.end()
    while index < len(src) and depth:
        char = src[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse function body for `{fn_name}`")
    return src[match.end() : index - 1]


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_read_cli_lock_free(config: dict, read_cli_src: str) -> str:
    spec = contract_block(config)["lock_free_read"]
    token = spec["read_cli_no_lock_token"]
    if _compact(token) in _compact(read_cli_src):
        fail(
            f"the query reader ({contract_block(config)['read_cli_bin']}) must be LOCK-FREE: it must "
            f"NOT acquire the single-writer lock (`{token}` found) -- a read takes no lock so it "
            "never blocks on, nor is blocked by, an ingestion write"
        )
    return (
        f"{contract_block(config)['read_cli_bin']} is a lock-free reader (no `{token}`) -- it loads "
        "the atomically-published snapshot and never blocks on an active ingestion write"
    )


def check_inspect_lock_free(config: dict, write_cli_src: str) -> str:
    spec = contract_block(config)["lock_free_read"]
    body = _fn_body(write_cli_src, spec["inspect_fn"])
    if _compact(spec["read_cli_no_lock_token"]) in _compact(body):
        fail(
            f"the `{spec['inspect_fn']}` reader must be LOCK-FREE: it must NOT acquire "
            f"`{spec['read_cli_no_lock_token']}` -- inspecting persisted data is a read, not a write"
        )
    if _compact(spec["inspect_load_token"]) not in _compact(body):
        fail(
            f"`{spec['inspect_fn']}` must read via `{spec['inspect_load_token']}` (the read-only "
            "snapshot load)"
        )
    return (
        f"{contract_block(config)['write_cli_bin']} `{spec['inspect_fn']}` is a lock-free reader "
        f"(reads via `{spec['inspect_load_token']}`, takes no single-writer lock)"
    )


def check_writer_serialized(config: dict, write_cli_src: str) -> str:
    spec = contract_block(config)["writer_serialization"]
    body = _compact(_fn_body(write_cli_src, spec["ingest_fn"]))
    for key, label in (
        ("acquire_token", "acquire the single-writer StoreLock"),
        ("load_token", "load the existing catalog"),
        ("save_token", "persist the modified catalog"),
    ):
        if _compact(spec[key]) not in body:
            fail(
                f"the ingestion writer (`{spec['ingest_fn']}`) must {label} (`{spec[key]}`) -- it must "
                "hold the single-writer lock across the WHOLE load-modify-save so two jobs cannot "
                "last-publish-wins over each other"
            )
    # Lock LIFETIME, not just token presence: the lock must be acquired BEFORE the load-modify-save
    # (acquire -> load -> save) and still held AT the save. Token presence alone would let a regression
    # acquire then `drop(_lock)` before the load/save and still pass, silently reopening
    # last-publish-wins data loss between concurrent ingestion jobs.
    acquire_at = body.find(_compact(spec["acquire_token"]))
    load_at = body.find(_compact(spec["load_token"]))
    save_at = body.find(_compact(spec["save_token"]))
    if not acquire_at < load_at < save_at:
        fail(
            f"the ingestion writer (`{spec['ingest_fn']}`) must acquire the StoreLock BEFORE the "
            "load-modify-save (acquire -> load_from_path -> save_to_path), not after -- otherwise two "
            "jobs can interleave their load/modify and the later save erases the earlier job's records"
        )
    drop_at = body.find(_compact(f"drop({spec['lock_binding']})"))
    if drop_at != -1 and drop_at < save_at:
        fail(
            f"the ingestion writer must HOLD the StoreLock until after `{spec['save_token']}` -- a "
            f"`drop({spec['lock_binding']})` before the save releases the lock mid load-modify-save and "
            "reopens last-publish-wins data loss between concurrent ingestion jobs"
        )
    return (
        f"{contract_block(config)['write_cli_bin']} `{spec['ingest_fn']}` acquires the single-writer "
        "StoreLock BEFORE and holds it ACROSS the whole load-modify-save (acquire -> load_from_path -> "
        "save_to_path, with no premature drop) -- writers are serialized, so a reader never observes a "
        "half-applied ingestion and two concurrent jobs cannot last-publish-wins over each other"
    )


def check_atomic_publish(config: dict, store_src: str) -> str:
    spec = contract_block(config)["atomic_publish"]
    body = _compact(_fn_body(store_src, spec["save_fn"]))
    if _compact(spec["rename_token"]) not in body:
        fail(
            f"`{spec['save_fn']}` must publish atomically via `{spec['rename_token']}` -- an in-place "
            "write would let a concurrent reader observe a torn / half-written store"
        )
    if _compact(spec["fsync_token"]) not in body:
        fail(
            f"`{spec['save_fn']}` must fsync (`{spec['fsync_token']}`) before/after the atomic rename "
            "so the published bytes are durable"
        )
    return (
        f"MarketDataStore::{spec['save_fn']} publishes atomically (scratch -> fsync -> "
        f"`{spec['rename_token']}` -> parent fsync) -- a concurrent reader sees the whole old or whole "
        "new file, never a torn read"
    )


def check_fail_closed_read(config: dict, store_src: str) -> str:
    spec = contract_block(config)["fail_closed_read"]
    load_body = _compact(_fn_body(store_src, spec["load_fn"]))
    if _compact(spec["load_calls_restore_token"]) not in load_body:
        fail(
            f"`{spec['load_fn']}` must route a present file through the fail-closed codec "
            f"(`{spec['load_calls_restore_token']}`) rather than trusting raw bytes"
        )
    restore_body = _compact(_fn_body(store_src, spec["restore_fn"]))
    if _compact(spec["checksum_guard_token"]) not in restore_body:
        fail(
            f"`{spec['restore_fn']}` must verify the integrity checksum BEFORE decoding "
            f"(`{spec['checksum_guard_token']}`) -- a torn / corrupt read must fail closed, not yield "
            "a partial store"
        )
    if _compact(spec["mismatch_err_token"]) not in restore_body:
        fail(
            f"`{spec['restore_fn']}` must reject a checksum mismatch with `{spec['mismatch_err_token']}`"
        )
    return (
        f"MarketDataStore::{spec['load_fn']} reads fail-closed: a present file is decoded through the "
        f"checksum-first `{spec['restore_fn']}` (verifies the integrity checksum before any record is "
        f"decoded -> `{spec['mismatch_err_token']}` on a torn read) -- never a silently-partial store"
    )


def check_single_writer_lock(config: dict, store_src: str) -> str:
    spec = contract_block(config)["store_lock"]
    body = _compact(_fn_body(store_src, spec["acquire_fn"]))
    if _compact(spec["o_excl_token"]) not in body:
        fail(
            f"the StoreLock `{spec['acquire_fn']}` must be an exclusive (O_EXCL) create "
            f"(`{spec['o_excl_token']}`) so a second writer is refused, not silently admitted"
        )
    if _compact(spec["locked_err_token"]) not in body:
        fail(
            f"the StoreLock `{spec['acquire_fn']}` must refuse a held lock with "
            f"`{spec['locked_err_token']}` (never a last-publish-wins overwrite)"
        )
    return (
        f"StoreLock::{spec['acquire_fn']} is an O_EXCL `{spec['o_excl_token']}` single-writer guard -- "
        f"a second concurrent writer is refused (`{spec['locked_err_token']}`), never admitted to a "
        "last-publish-wins overwrite"
    )


def check_load_test_present(config: dict, load_test_src: str) -> str:
    block = contract_block(config)
    tokens = block["load_test"]["test_file_tokens"]
    compact = _compact(load_test_src)
    missing = [t for t in tokens if _compact(t) not in compact]
    if missing:
        fail(
            f"the Load test {block['rust_integration_test']}.rs must exercise concurrent readers + a "
            f"lock-held writer over real files and assert no corruption: missing {', '.join(missing)}"
        )
    return (
        f"the Load test {block['rust_integration_test']}.rs drives concurrent lock-free readers + a "
        "lock-held writer over real files and asserts every read is uncorrupted (ChecksumMismatch is "
        "the failure mode it rules out)"
    )


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-data Cargo.toml must NOT depend on the broker/execution path: found "
            f"{', '.join(leaked)} -- the storage concurrency property is broker-independent"
        )
    return (
        f"atp-data Cargo.toml declares no dependency on the broker/execution path "
        f"({', '.join(spec['forbidden_dep_tokens'])})"
    )


def check_vendor_isolation(config: dict, store_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in store_src]
    if leaked:
        fail(
            f"atp-data store path leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the storage substrate is vendor-neutral per SRS-ARCH-003)"
        )
    return (
        f"atp-data store path is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(the storage substrate is vendor-neutral; SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot run the {crate} concurrent-read "
                "Load test (install the Rust toolchain)"
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
        "(a lock-held writer thread doing many load-modify-save ingests runs concurrently with "
        "lock-free reader threads; every read is Ok and still holds the full previously-ingested seed "
        "set, the observed count is monotonic non-decreasing, and no reader is ever refused the lock)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "store" reads store.rs, "read_cli"/"write_cli" read the operator
# binaries, "cargo" reads Cargo.toml, "load_test" reads the Rust Load test.
_STATIC_CHECKS = (
    ("read_cli_lock_free", check_read_cli_lock_free, "read_cli"),
    ("inspect_lock_free", check_inspect_lock_free, "write_cli"),
    ("writer_serialized", check_writer_serialized, "write_cli"),
    ("atomic_publish", check_atomic_publish, "store"),
    ("fail_closed_read", check_fail_closed_read, "store"),
    ("single_writer_lock", check_single_writer_lock, "store"),
    ("load_test_present", check_load_test_present, "load_test"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "store"),
)

_DEFERRED_OWNERS = (
    "in-process Python / backtest / factor / notebook query bindings over this store -- the Rust "
    "data007_query_cli + data016_ingest_cli inspect surfaces are the operator-demonstrable lock-free "
    "readers (SRS-UI / SRS-API / the SRS-SDK strategy host)",
    "real Databento / IB / Sharadar / option-chain NETWORK adapters that materialize records "
    "(SRS-DATA-001 / 003 / 005 / 006; fixture sources stand in)",
    "SSD-primary / NAS-archival tiering, eviction, and cold-read failover of the store directory "
    "(SRS-DATA-008 / 009 / 010)",
    "richer lock liveness (pid-liveness / lease expiry) and cross-host write coordination -- the "
    "single-user, local-one-host baseline serializes writers on one filesystem",
)


def assert_concurrent_read_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "store": store_source(config, root),
        "read_cli": read_cli_source(config, root),
        "write_cli": write_cli_source(config, root),
        "cargo": cargo_source(config, root),
        "load_test": load_test_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_concurrent_read_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SRS-DATA-017 concurrent-read-during-write contract evidence"
    )
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the concurrent-read Load test must run.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except ConcurrentReadCheckError as error:
        print(f"SRS-DATA-017 CONCURRENT-READ FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-DATA-017 CONCURRENT-READ PASS")
    for item in evidence:
        print(f"- {item}")
    print("- deferred to: " + "; ".join(_DEFERRED_OWNERS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
