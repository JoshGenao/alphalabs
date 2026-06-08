#!/usr/bin/env python3
"""Contract evidence script for SRS-SIM-004 (persist paper strategy simulation state).

SRS-SIM-004: "persist paper strategy simulation state" (SyRS SYS-89; StRS
SN-1.29 / SN-2.05). The acceptance criterion: "Virtual positions, pending
simulated orders, accumulated metrics, and user state are persisted every 60
seconds by default and restored within 30 seconds of container restart, excluding
warm-up."

The paper-state persistence path lives in ``crates/atp-simulation`` (module
``paper_state``), per the structural contract in
``architecture/runtime_services.json`` (block ``sim_persistence_contract``):

  (a) ``PaperStateSnapshot`` is a versioned envelope (``schema_version`` i64,
      ``config``, ``book``) that captures the FULL SRS-SIM-003 ``VirtualLedgerBook``
      without any new dependency (no serde; money stays integer minor units).
  (b) ``PersistenceConfig`` carries the SYS-89 cadence: ``interval_secs`` (u64,
      default 60), ``restore_deadline_secs`` (u64, default 30), and
      ``persist_on_shutdown`` (bool, default true); ``new`` fails closed on a
      zero-second interval or restore deadline.
  (c) ``serialize`` is DETERMINISTIC: it sorts strategies by id and positions by
      canonical symbol before emitting, so the same state always serializes to
      byte-identical output and an unchanged 60s checkpoint never churns.
  (d) the hand-rolled, dependency-free codec length-prefixes strings, so an OCC
      option symbol containing spaces round-trips, and ``restore`` is the
      round-trip back to a ``VirtualLedgerBook``
      (``restore(serialize(capture(book))) == book``).
  (e) ``deserialize`` fails closed and atomically: it validates the magic header,
      the schema version, the config, and every position field invariant (the
      quantity/basis biconditional, sign agreement, non-negative cost components,
      canonical symbols, no duplicate records, no trailing data) and builds the
      whole book in a local, so a corrupt or tampered blob yields no
      partially-restored state (``PersistenceError`` variants).
  (f) the SYS-89 sub-states that have no runtime type yet (pending orders,
      metrics, user-state) are reserved, forward-compatible slots; a non-zero
      reserved slot is rejected (``UnsupportedSection``) rather than dropped.
  (g) every money figure is an integer minor unit; the module contains no ``f64``;
      ``lib.rs`` re-exports ``pub mod paper_state;`` and the module carries no
      vendor-SDK token (SRS-ARCH-003); the ``atp-simulation`` crate has no
      dependency on the live/broker path (``atp-execution`` / ``atp-adapters``),
      so persisted paper state is independent of the IB account.

The PASS line is ``SRS-SIM-004 SDK-SURFACE PASS`` -- it names the deferred owners
(the live 60s timer / 30s-restore container wiring via SRS-EXE-002 / SYS-89, the
pending-order store, the SYS-85 / SRS-BT-004 metric family, and the Python
runtime) so the partial-pass status (feature_list.json keeps ``passes:false``) is
loud.

Mirrors the PASS/FAIL output style of ``tools/sim_ledger_check.py``.

Invoke:
    python3 tools/sim_persistence_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class SimPersistenceCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SimPersistenceCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_persistence_contract" not in config:
        fail("architecture metadata is missing sim_persistence_contract")
    return config["sim_persistence_contract"]


def persistence_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / "src" / f"{block['persistence_module']}.rs"
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_snapshot_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["snapshot_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    expected = {
        "schema_version": "schema_version:i64",
        "config": "config:PersistenceConfig",
        "book": "book:VirtualLedgerBook",
    }
    missing = [f for f in spec["fields"] if _compact(expected[f]) not in body]
    if missing:
        fail(
            f"{spec['struct']} must be a versioned envelope holding "
            f"{', '.join(expected[f] for f in missing)} -- the schema version, the persistence "
            "config, and the captured virtual ledger book"
        )
    return (
        f"atp-simulation declares {spec['struct']} as a versioned envelope "
        "(schema_version: i64, config: PersistenceConfig, book: VirtualLedgerBook) capturing the "
        "full SRS-SIM-003 ledger"
    )


def check_config_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["config_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["u64_fields"] if _compact(f"{f}:u64") not in body]
    if missing:
        fail(
            f"{spec['struct']} must declare the SYS-89 cadence as u64 seconds: missing "
            f"{', '.join(f'{f}: u64' for f in missing)}"
        )
    if _compact(f"{spec['bool_field']}:bool") not in body:
        fail(f"{spec['struct']} must declare `{spec['bool_field']}: bool`")
    return (
        f"atp-simulation declares {spec['struct']} with the SYS-89 cadence "
        f"({', '.join(f'{f}: u64' for f in spec['u64_fields'])}, {spec['bool_field']}: bool)"
    )


def check_config_defaults(config: dict, src: str) -> str:
    spec = contract_block(config)["config_defaults"]
    compact_src = _compact(src)
    if _compact(f"{spec['interval_const']}:u64={spec['interval_value']}") not in compact_src:
        fail(
            f"the SYS-89 default interval must be {spec['interval_value']}s "
            f"(`{spec['interval_const']}: u64 = {spec['interval_value']}`)"
        )
    if _compact(f"{spec['deadline_const']}:u64={spec['deadline_value']}") not in compact_src:
        fail(
            f"the SYS-89 default restore deadline must be {spec['deadline_value']}s "
            f"(`{spec['deadline_const']}: u64 = {spec['deadline_value']}`)"
        )
    if _compact(spec["shutdown_default_token"]) not in compact_src:
        fail(
            f"the default config must persist on shutdown (`{spec['shutdown_default_token']}`, "
            "SYS-89 'and on container shutdown')"
        )
    return (
        f"atp-simulation defaults to the SYS-89 baseline: {spec['interval_const']} = "
        f"{spec['interval_value']}s, {spec['deadline_const']} = {spec['deadline_value']}s, persist "
        "on shutdown"
    )


def check_config_validation(config: dict, src: str) -> str:
    spec = contract_block(config)["config_validation"]
    compact_src = _compact(src)
    for key, label in (
        ("interval_guard", "a zero-second interval"),
        ("deadline_guard", "a zero-second restore deadline"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"PersistenceConfig::{spec['fn']} must fail closed on {label} (`{spec[key]}`) -- a "
                "zero-second cadence is unmeetable"
            )
    if _compact(spec["error_token"]) not in compact_src:
        fail(
            f"PersistenceConfig::{spec['fn']} must reject a non-positive cadence with "
            f"`{spec['error_token']}`"
        )
    # SYS-89 hard ceiling: the restore deadline cannot exceed 30s, so the config
    # cannot encode an SLA slower than the requirement allows.
    if _compact(spec["ceiling_guard"]) not in compact_src:
        fail(
            f"PersistenceConfig::{spec['fn']} must reject a restore deadline above the SYS-89 "
            f"ceiling (`{spec['ceiling_guard']}`) -- the config must not be able to encode a slower "
            "SLA than the 30s the requirement mandates"
        )
    if _compact(spec["ceiling_error_token"]) not in compact_src:
        fail(
            f"PersistenceConfig::{spec['fn']} must reject an over-ceiling restore deadline with "
            f"`{spec['ceiling_error_token']}`"
        )
    return (
        f"atp-simulation PersistenceConfig::{spec['fn']} fails closed on a zero-second interval or "
        f"restore deadline ({spec['error_token']}) AND on a restore deadline above the SYS-89 30s "
        f"ceiling ({spec['ceiling_error_token']}), so it cannot encode an out-of-SLA cadence"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_schema_version(config: dict, src: str) -> str:
    spec = contract_block(config)["schema_version"]
    compact_src = _compact(src)
    for key, label in (
        ("const_token", "a SCHEMA_VERSION constant"),
        ("magic_const_token", "a MAGIC header constant"),
        ("version_guard_token", "the schema-version guard"),
        ("version_error_token", "the UnknownSchemaVersion error"),
        ("magic_guard_token", "the magic-header guard"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"paper_state must version its snapshot and reject foreign/old blobs: missing "
                f"{label} (`{spec[key]}`)"
            )
    return (
        "atp-simulation versions the snapshot (SCHEMA_VERSION + MAGIC header) and rejects a foreign "
        "blob or unknown version (UnknownSchemaVersion) rather than mis-reading it"
    )


def check_codec(config: dict, src: str) -> str:
    spec = contract_block(config)["codec"]
    for key in ("capture_fn", "serialize_fn", "deserialize_fn", "restore_fn", "into_book_fn"):
        fn = spec[key]
        if not re.search(rf"\bpub\s+fn\s+{re.escape(fn)}\b", src):
            fail(f"paper_state must expose `pub fn {fn}` (the capture/serialize/restore surface)")
    if _compact(spec["restore_body_token"]) not in _compact(src):
        fail(
            f"`restore` must be the round-trip deserialize-then-into_book "
            f"(`{spec['restore_body_token']}`)"
        )
    return (
        "atp-simulation exposes capture / serialize / deserialize / into_book and a restore() "
        "round-trip (restore(serialize(capture(book))) == book)"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    serialize_body = _compact(_fn_block(src, spec.get("serialize_fn", "serialize")))
    compact_src = _compact(src)
    for key, label in (
        ("strategy_sort_token", "sort strategies by id"),
        ("position_sort_token", "sort positions by canonical symbol"),
    ):
        if _compact(spec[key]) not in serialize_body:
            fail(
                f"serialize must {label} (`{spec[key]}`) so the snapshot is deterministic -- HashMap "
                "order is unspecified, so an unsorted serialize would churn every checkpoint"
            )
    for key, label in (
        ("ledger_iter_token", "iterate the book's ledgers"),
        ("position_iter_token", "iterate a ledger's positions"),
        (
            "length_prefix_token",
            "length-prefix strings (so an OCC option symbol with spaces survives)",
        ),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"serialize must {label} (`{spec[key]}`)")
    return (
        "atp-simulation serialize sorts strategies by id and positions by canonical symbol before "
        "emitting (byte-identical output for the same state) and length-prefixes strings so an OCC "
        "option symbol containing spaces round-trips"
    )


def check_fail_closed(config: dict, src: str) -> str:
    spec = contract_block(config)["fail_closed"]
    compact_src = _compact(src)
    # Every token is a unique CODE expression (not a natural-language string that
    # could leak into a doc comment), compared whitespace-insensitively.
    for key, label in (
        ("negative_cost_token", "reject a negative cost component"),
        ("biconditional_token", "enforce the quantity/basis flat-state biconditional"),
        ("sign_token", "enforce quantity/basis sign agreement"),
        ("canonical_token", "reject a non-canonical symbol"),
        ("duplicate_token", "reject a duplicate strategy/symbol"),
        ("trailing_token", "reject trailing data after the snapshot"),
        ("expect_end_token", "require the cursor to be exhausted"),
        (
            "shutdown_required_token",
            "reject a snapshot that disables mandatory shutdown persistence",
        ),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"deserialize must {label} (`{spec[key]}`)")
    return (
        "atp-simulation deserialize is fail-closed and atomic: it rejects a negative cost component, "
        "a quantity/basis flat-state or sign mismatch, a non-canonical symbol, a duplicate record, "
        "trailing data, and a snapshot that disables the SYS-89-mandatory shutdown persistence, "
        "building the book in a local so a corrupt blob yields no partial state"
    )


def check_reserved_slots(config: dict, src: str) -> str:
    spec = contract_block(config)["reserved_slots"]
    missing = [s for s in spec["slots"] if s not in src]
    if missing:
        fail(
            "the snapshot must reserve forward-compatible slots for the not-yet-built SYS-89 "
            f"sub-states; missing: {', '.join(missing)}"
        )
    if _compact(spec["unsupported_error_token"]) not in _compact(src):
        fail(
            f"deserialize must reject data in a reserved slot (`{spec['unsupported_error_token']}`) "
            "rather than silently dropping it"
        )
    return (
        f"atp-simulation reserves forward-compatible slots ({', '.join(spec['slots'])}) for the "
        "not-yet-built SYS-89 sub-states and fails closed (UnsupportedSection) on a non-empty slot"
    )


def check_integrity(config: dict, src: str) -> str:
    spec = contract_block(config)["integrity"]
    compact_src = _compact(src)
    if not re.search(rf"\b{re.escape(spec['checksum_fn'])}\b", src):
        fail(
            f"paper_state must define an integrity `{spec['checksum_fn']}` so a corrupt snapshot is "
            "detected (the SYS-89 fault-injection criterion)"
        )
    if _compact(spec["write_token"]) not in compact_src:
        fail(f"serialize must write the integrity checksum over the body (`{spec['write_token']}`)")
    if _compact(spec["verify_token"]) not in compact_src:
        fail(
            f"deserialize must verify the integrity checksum before building state "
            f"(`{spec['verify_token']}`) so a structurally-valid byte change fails closed"
        )
    if _compact(spec["error_token"]) not in compact_src:
        fail(f"a checksum mismatch must fail closed with `{spec['error_token']}`")
    return (
        "atp-simulation frames the snapshot with a dependency-free, integer integrity checksum over "
        "the body, verified BEFORE any state is built, so a structurally-valid tampered/corrupted "
        "byte (a flipped digit, a sign-consistent quantity/basis change, truncation, appended bytes) "
        f"fails closed with {spec['error_token']} under fault injection"
    )


def check_money_invariant(config: dict, src: str) -> str:
    token = contract_block(config)["money_invariant"]["forbidden_float_token"]
    if token in src:
        fail(
            f"paper_state module contains `{token}` -- persisted money figures MUST be integer minor "
            "units (the money-correctness invariant)"
        )
    return f"atp-simulation paper_state money is integer minor units: no {token}"


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so persistence is "
            "part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- persisted paper state must be independent of the IB account "
            "(SRS-SIM-004 / SYS-89)"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- persisted paper state is independent of "
        "the IB account at the crate boundary"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation paper_state module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation paper_state module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "paper-state persistence path compiles + passes (install the Rust toolchain)"
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
        "(capture/serialize/restore round-trips a real book exactly, serialization is deterministic "
        "and independent of insertion order, an OCC option symbol with spaces survives, a flat "
        "closed position keeps its realized P&L, and a corrupt/tampered snapshot fails closed with "
        "no partial state)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) — "persistence" reads paper_state.rs, "lib" reads
# lib.rs, "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("snapshot_struct", check_snapshot_struct, "persistence"),
    ("config_struct", check_config_struct, "persistence"),
    ("config_defaults", check_config_defaults, "persistence"),
    ("config_validation", check_config_validation, "persistence"),
    ("error_enum", check_error_enum, "persistence"),
    ("schema_version", check_schema_version, "persistence"),
    ("codec", check_codec, "persistence"),
    ("determinism", check_determinism, "persistence"),
    ("fail_closed", check_fail_closed, "persistence"),
    ("reserved_slots", check_reserved_slots, "persistence"),
    ("integrity", check_integrity, "persistence"),
    ("money_invariant", check_money_invariant, "persistence"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "persistence"),
)

_DEFERRED_OWNERS = (
    "live 60s persistence timer + 30s restore on container restart (SRS-EXE-002 / SYS-89 lifecycle)",
    "pending-order store (SRS-SIM-001 / SRS-SIM-002 paper-order path)",
    "SYS-85 accumulated paper performance metrics (SRS-BT-004 metric family)",
    "user-state dictionary (SRS-SDK Python strategy runtime)",
    "SYS-88 corporate-action adjustment of persisted positions (SRS-DATA-021)",
)


def assert_sim_persistence_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "persistence": persistence_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_persistence_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-SIM-004 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable persistence path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except SimPersistenceCheckError as error:
        print(f"SRS-SIM-004 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-SIM-004 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-SIM-004 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
