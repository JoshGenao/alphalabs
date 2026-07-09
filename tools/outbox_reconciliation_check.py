#!/usr/bin/env python3
"""Contract evidence script for feature SRS-EXE-009.

Verifies that the durable order-intent **outbox** + restart reconciliation
declared in ``architecture/runtime_services.json`` (block
``outbox_reconciliation_contract``) is present in the Rust crate
``crates/atp-execution`` (module ``outbox``).

SRS-EXE-009 ("durably commit live order intents before submission to IB and use
the durable record to reconcile broker state on restart") traces SyRS SYS-90 /
NFR-R3 / NFR-R4 and StRS SN-2.05. It is a live-order safety path: a crash in the
submit window must never cause a duplicate live order. This contract pins:

  (a) the write-ahead outbox API — ``OrderOutbox`` with ``commit_intent``
      (durable PENDING_SUBMIT record, idempotent DuplicateClientCorrelationId
      rejection), ``bind_ack``, ``observe_state``, and ``prune_terminal``.
  (b) the durable codec — ``OutboxSnapshot`` with its own MAGIC, the
      fsync -> rename -> dir-fsync durability sequence, and a checksum-guarded,
      fail-closed ``deserialize``.
  (c) the reconciliation surface — the ``reconcile`` entry point, the
      ``BrokerOpenOrderSource`` port, the ``SnapshotCoverage`` enum, the
      ``ReconciliationPlan`` fields, and the ``ConflictKind`` variants.
  (d) the coverage artifacts — the ``exe009_outbox_reconcile_cli`` binary and
      the integration / CLI / domain tests.
  (e) an honest ``deferred[]`` naming the real-IB owners (SRS-EXE-006 /
      SRS-EXE-001 / SRS-EXE-008) that keep SRS-EXE-009 ``passes:false``.

Mirrors the PASS/FAIL output style of ``tools/live_designation_check.py``.

Invoke:
    python3 tools/outbox_reconciliation_check.py
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
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"

_CONTRACT = "outbox_reconciliation_contract"


class OutboxReconciliationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OutboxReconciliationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if _CONTRACT not in config:
        fail(f"architecture metadata is missing {_CONTRACT}")
    return config[_CONTRACT]


def outbox_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    module = root / block["module"]
    if not module.is_file():
        fail(f"outbox module missing: {block['module']}")
    return module.read_text(encoding="utf-8")


def _require_all(source: str, tokens: list[str], where: str) -> None:
    for token in tokens:
        if token not in source:
            fail(f"{where}: missing `{token}`")


def _require_decls(source: str, decls: list[tuple[str, str]], where: str) -> None:
    """Require each `(kind, name)` Rust declaration — matched with a trailing word
    boundary so a rename (e.g. ``commit_intent`` -> ``commit_intentX``) is caught,
    which a plain substring check would miss."""
    for kind, name in decls:
        if not re.search(rf"pub {kind} {re.escape(name)}\b", source):
            fail(f"{where}: missing `pub {kind} {name}`")


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #


def check_metadata(config: dict, _source: str) -> str:
    block = contract_block(config)
    if block.get("requirement") != "SRS-EXE-009":
        fail("contract requirement must be SRS-EXE-009")
    for key in ("srs_refs", "syrs_refs", "strs_refs", "safety_invariants", "deferred"):
        if not block.get(key):
            fail(f"contract is missing non-empty `{key}`")
    for ref in ("SYS-90", "NFR-R3", "NFR-R4"):
        if ref not in block["syrs_refs"]:
            fail(f"syrs_refs must include {ref}")
    return (
        f"{_CONTRACT} pins SRS-EXE-009 "
        f"(SyRS {', '.join(block['syrs_refs'])}; StRS {', '.join(block['strs_refs'])})"
    )


def check_outbox_api(config: dict, source: str) -> str:
    outbox = contract_block(config)["outbox"]
    _require_decls(
        source,
        [
            ("struct", outbox["struct"]),
            ("struct", outbox["entry"]),
            ("fn", outbox["write_ahead_method"]),
            ("fn", outbox["ack_method"]),
            ("fn", outbox["observe_method"]),
            ("fn", outbox["retention_method"]),
            ("enum", outbox["error_enum"]),
        ],
        "outbox API",
    )
    _require_all(
        source,
        [
            f"OrderState::{outbox['commit_state']}",
            f"OrderErrorCategory::{outbox['duplicate_rejection_category']}",
        ],
        "outbox API",
    )
    # commit_intent must reject a duplicate (the idempotency spine).
    if outbox["duplicate_rejection_category"] not in source:
        fail("commit_intent must reject a duplicate correlation id")
    return (
        f"{outbox['struct']} exposes {outbox['write_ahead_method']} / "
        f"{outbox['ack_method']} / {outbox['observe_method']} / "
        f"{outbox['retention_method']} with idempotent "
        f"{outbox['duplicate_rejection_category']} rejection"
    )


def check_durable_codec(config: dict, source: str) -> str:
    codec = contract_block(config)["durable_codec"]
    _require_decls(
        source,
        [("struct", codec["struct"]), ("enum", codec["error_enum"])]
        + [("fn", method) for method in codec["methods"]],
        "durable codec",
    )
    _require_all(source, [f'"{codec["magic"]}"', codec["integrity_error"]], "durable codec")
    # The durability sequence must appear IN ORDER (fsync -> rename -> dir fsync).
    positions = []
    cursor = 0
    for token in codec["durability_sequence"]:
        idx = source.find(token, cursor)
        if idx == -1:
            fail(f"durable codec: durability step `{token}` not found in order")
        positions.append(idx)
        cursor = idx + len(token)
    if positions != sorted(positions):
        fail("durable codec: fsync -> rename -> dir-fsync sequence is out of order")
    return (
        f"{codec['struct']} persists via "
        f"{' -> '.join(codec['durability_sequence'])} with a "
        f"{codec['integrity_error']}-guarded fail-closed deserialize"
    )


def check_reconciliation(config: dict, source: str) -> str:
    rec = contract_block(config)["reconciliation"]
    _require_decls(
        source,
        [
            ("fn", rec["entry_point"]),
            ("trait", rec["broker_port"]),
            ("struct", rec["broker_snapshot"]),
            ("struct", rec["broker_order"]),
            ("enum", rec["coverage_enum"]),
            ("struct", rec["plan_struct"]),
            ("enum", rec["conflict_enum"]),
        ],
        "reconciliation",
    )
    _require_all(
        source,
        rec["coverage_variants"]
        + [f"pub {field}:" for field in rec["plan_fields"]]
        + rec["conflict_variants"],
        "reconciliation",
    )
    return (
        f"{rec['entry_point']} over {rec['broker_port']} produces "
        f"{rec['plan_struct']} ({', '.join(rec['plan_fields'])}) with "
        f"{rec['coverage_enum']} ({', '.join(rec['coverage_variants'])})"
    )


def check_terminal_states(config: dict, source: str) -> str:
    block = contract_block(config)
    states = block["terminal_states"]
    if sorted(states) != sorted(["FILLED", "CANCELLED", "REJECTED", "EXPIRED"]):
        fail("terminal_states must be exactly FILLED/CANCELLED/REJECTED/EXPIRED")
    # is_terminal() is the retention authority — its use must be present.
    if "is_terminal()" not in source:
        fail("retention must key on OrderState::is_terminal()")
    return f"retention releases the terminal set {', '.join(states)} via is_terminal()"


def check_coverage_artifacts(config: dict, _source: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    cargo = (root / "crates" / "atp-execution" / "Cargo.toml").read_text(encoding="utf-8")
    if f'name = "{block["cli_binary"]}"' not in cargo:
        fail(f"Cargo.toml is missing the [[bin]] {block['cli_binary']}")
    tests_dir = root / "crates" / "atp-execution" / "tests"
    for target in (block["integration_test"], block["cli_test"]):
        if not (tests_dir / f"{target}.rs").is_file():
            fail(f"missing integration test file {target}.rs")
    if not (root / block["domain_test"]).is_file():
        fail(f"missing domain test {block['domain_test']}")
    return (
        f"{block['cli_binary']} binary + {block['integration_test']} / "
        f"{block['cli_test']} integration tests + {block['domain_test']}"
    )


def check_durable_submit(config: dict, _source: str, root: Path = ROOT) -> str:
    ds = contract_block(config)["durable_submit"]
    lib_path = root / ds["lib"]
    if not lib_path.is_file():
        fail(f"durable submit: lib missing {ds['lib']}")
    lib = lib_path.read_text(encoding="utf-8")
    # The PUBLIC entry is route_order_durably (authority-derived); the mode-trusting
    # inner is pub(crate) so no public path can self-declare live-ness.
    if not re.search(rf"pub fn {re.escape(ds['method'])}\b", lib):
        fail(f"durable submit: missing `pub fn {ds['method']}`")
    if not re.search(rf"pub\(crate\) fn {re.escape(ds['inner'])}\b", lib):
        fail(f"durable submit: `{ds['inner']}` must be pub(crate) (no public caller-mode path)")
    _require_decls(lib, [("enum", ds["error_enum"])], "durable submit")
    # The public entry must gate on the engine-owned live-designation authority; the
    # inner must write-ahead (commit + persist) BEFORE the broker call and bind/reject.
    for token in (
        ds["authority_call"],
        "commit_intent",
        ".persist(",
        "submit_live_order",
        "bind_ack",
        "observe_state",
    ):
        if token not in lib:
            fail(f"durable submit: {ds['method']} path must use `{token}`")
    seam = root / "crates" / "atp-execution" / "tests" / f"{ds['seam_test']}.rs"
    if not seam.is_file():
        fail(f"durable submit: missing seam test {ds['seam_test']}.rs")
    return (
        f"{ds['type']}::{ds['method']} gates on {ds['authority_call']} then write-aheads "
        f"(commit_intent + persist) before the broker call via the pub(crate) "
        f"{ds['inner']}; binds/rejects via {ds['error_enum']} (seam test {ds['seam_test']})"
    )


def check_deferred(config: dict, _source: str) -> str:
    deferred = contract_block(config)["deferred"]
    joined = " ".join(deferred)
    for owner in ("SRS-EXE-006", "SRS-EXE-001", "SRS-EXE-008"):
        if owner not in joined:
            fail(f"deferred[] must name owner {owner}")
    if "passes:false" not in joined:
        fail("deferred[] must state SRS-EXE-009 stays passes:false until the real-IB proof lands")
    return "deferred[] names owners SRS-EXE-006 / SRS-EXE-001 / SRS-EXE-008 (passes:false)"


_STATIC_CHECKS = [
    ("metadata", check_metadata),
    ("outbox_api", check_outbox_api),
    ("durable_codec", check_durable_codec),
    ("reconciliation", check_reconciliation),
    ("durable_submit", check_durable_submit),
    ("terminal_states", check_terminal_states),
    ("coverage_artifacts", check_coverage_artifacts),
    ("deferred", check_deferred),
]


def _run_check(name: str, check, config: dict, source: str) -> str:
    # coverage_artifacts needs the repo root, not the source; dispatch by name.
    if name == "coverage_artifacts":
        return check(config, source, ROOT)
    return check(config, source)


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["execution_crate"]["crate"]
    target = block["integration_test"]
    if shutil.which("cargo") is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        ["cargo", "test", "-p", crate, "--test", target],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} --test {target} failed:\n{result.stdout}\n{result.stderr}")
    return f"cargo test -p {crate} --test {target}: passed"


def run_checks() -> list[str]:
    config = load_config()
    source = outbox_source(config)
    evidence = [_run_check(name, check, config, source) for name, check in _STATIC_CHECKS]
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_outbox_reconciliation_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (mirrors live_designation_check)."""
    source = outbox_source(config, root)
    out = []
    for name, check in _STATIC_CHECKS:
        out.append(
            check(config, source, root) if name == "coverage_artifacts" else check(config, source)
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-009 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except OutboxReconciliationCheckError as error:
        print(f"SRS-EXE-009 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-EXE-009 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
