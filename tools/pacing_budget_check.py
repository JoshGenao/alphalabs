#!/usr/bin/env python3
"""Contract evidence script for feature ERR-6.

Verifies that the data-layer pacing-budget gate declared in
``architecture/runtime_services.json`` (block ``pacing_budget_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-data``.

ERR-6 traces SRS-DATA-002 + SRS-DATA-004 (SyRS SYS-31 / SYS-55 /
SYS-22b / SYS-23; StRS A-10 / SN-1.26 / SN-1.27). The contract
guarantees: (a) ``PacingBudgetState`` declares WithinBudget /
BudgetExceeded in ``atp-types``; (b) ``IngestionJobRequest`` carries
the two required fields (job_kind, window_seconds) and no
broker/vendor/tick leakage; (c) ``PacingBudgetEvent`` carries the five
required fields (state, job_kind, projected_requests,
permitted_requests, observed_at_seconds) and no broker/vendor/tick
leakage; (d) the ``PacingBudgetValidator`` and ``PacingBudgetEventSink``
ports live in ``atp-data``; (e) inside
``DataLayer::schedule_ingestion_job``, the body matches on
``validator.check_budget(...)``, the WithinBudget leaf is the only
call site of ``IngestionJobScheduled {``, and the BudgetExceeded leaf
reads ``validator.projected_requests(`` and
``validator.permitted_requests(``, produces
``OrderErrorCategory::IngestionPacingBudgetExceeded``, records a
``PacingBudgetEvent`` via ``events.record(``, and does NOT start the
affected job (no calls listed in the contract's
``forbidden_mutations`` array — keeps refusal a read-only operation).

Mirrors the PASS/FAIL output style of
``tools/ingestion_validation_check.py``.

Invoke:
    python3 tools/pacing_budget_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _match_arm, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class PacingBudgetCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise PacingBudgetCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "pacing_budget_contract" not in config:
        fail("architecture metadata is missing pacing_budget_contract")
    return config["pacing_budget_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def data_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["data_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"data crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_ingestion_job_request_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["ingestion_job_request"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks vendor / broker field(s): {', '.join(leaks)} "
            "(ERR-6 schedule envelopes must not carry broker/session/tick/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/broker fields"
    )


def check_pacing_budget_state_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["pacing_budget_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — pacing-budget gate "
        "(SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55)"
    )


def check_pacing_budget_event_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["pacing_budget_event"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks vendor / broker field(s): {', '.join(leaks)} "
            "(ERR-6 events must not carry broker/session/tick/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/broker/tick fields"
    )


def check_validator_port(config: dict, data_src: str) -> str:
    block = contract_block(config)
    spec = block["validator_port"]
    body = _trait_body(data_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the SRS-DATA-002 / SRS-DATA-004 / SyRS SYS-55 read-only pacing-budget probe"
    )


def check_event_sink_port(config: dict, data_src: str) -> str:
    block = contract_block(config)
    spec = block["event_sink_port"]
    body = _trait_body(data_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the structured-event publication channel for ERR-6 (dashboard alert + notification fan-out)"
    )


def check_pacing_budget_guard(config: dict, data_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(data_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    validator_call_token = guard["validator_call"] + "("
    if validator_call_token not in body:
        fail(
            f"{entry['method']} does not call `{validator_call_token}` — "
            "the pacing-budget classification is the only legitimate entry to the gate"
        )

    within_token = f"{guard['state_enum']}::{guard['within_variant']}"
    exceeded_token = f"{guard['state_enum']}::{guard['exceeded_variant']}"
    for token in (within_token, exceeded_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "ERR-6 requires both WithinBudget and BudgetExceeded to be "
                "handled inside the pacing-budget match"
            )

    try:
        within_arm = _match_arm(body, within_token)
    except AssertionError as error:
        fail(str(error))
    try:
        exceeded_arm = _match_arm(body, exceeded_token)
    except AssertionError as error:
        fail(str(error))

    accepted_token = guard["accepted_struct"] + " {"
    if accepted_token not in within_arm:
        fail(
            f"{entry['method']} {within_token} leaf does not produce "
            f"`{accepted_token}` — the WithinBudget leaf is the only "
            "legitimate construction site for the acceptance envelope"
        )
    if accepted_token in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf produces "
            f"`{accepted_token}` — ERR-6 requires zero acceptance side "
            "effect when the pacing budget is exceeded"
        )

    projected_call_token = guard["projected_call"] + "("
    if projected_call_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf is missing required "
            f"call `{projected_call_token}` — the rejection event must "
            "carry the projected request count (SRS-DATA-002 / SRS-DATA-004)"
        )
    permitted_call_token = guard["permitted_call"] + "("
    if permitted_call_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf is missing required "
            f"call `{permitted_call_token}` — the rejection event must "
            "carry the permitted request count for TOCTOU closure"
        )

    rejection = block["rejection_category"]
    category_token = f"OrderErrorCategory::{rejection}"
    factory_token = "StructuredPacingError::budget_exceeded("
    # The BudgetExceeded arm must construct the rejection envelope
    # through the category-pinned factory (which references
    # `OrderErrorCategory::IngestionPacingBudgetExceeded` inside
    # atp-types) or by naming the category directly. Either is
    # acceptable; both signal that the SyRS SYS-64 wire string source
    # of truth is being honoured.
    if category_token not in exceeded_arm and factory_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf must produce "
            f"{category_token} (directly or via the "
            f"`{factory_token.rstrip('(')}` factory — the SyRS SYS-64 "
            "wire string source of truth)"
        )

    event_call_token = guard["event_call"] + "("
    if event_call_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf is missing required "
            f"call `{event_call_token}` (SRS-DATA-002 / SRS-DATA-004 / "
            "SyRS SYS-55 structured-event publication)"
        )

    # Zero-job-start invariant: the BudgetExceeded leaf must not call
    # any of the forbidden mutators listed in the contract. The
    # refused job must leave the scheduler exactly as it found it.
    forbidden_mutations = guard.get("forbidden_mutations", [])
    for mutator in forbidden_mutations:
        token = f"{mutator}("
        if token in exceeded_arm:
            fail(
                f"{entry['method']} {exceeded_token} leaf calls "
                f"`{token}` — ERR-6 requires zero side effect on the "
                "scheduler when the gate refuses the job"
            )

    return (
        f"atp-data::{entry['method']} gates "
        f"`{guard['accepted_struct']}` on a match {within_token} via "
        f"`{guard['validator_call']}`; the {exceeded_token} leaf reads "
        f"`{guard['projected_call']}` + `{guard['permitted_call']}`, "
        f"emits OrderErrorCategory::{rejection}, records a "
        f"{block['pacing_budget_event']['struct']} via "
        f"`{guard['event_call']}`, and starts nothing on the scheduler "
        "(ERR-6)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["data_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}")
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "err_6_pacing_budget_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_6_pacing_budget_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_6_pacing_budget_blocked: PASS "
        "(pacing-budget refusal + zero job-start verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("ingestion_job_request", check_ingestion_job_request_struct, "types"),
    ("pacing_budget_state", check_pacing_budget_state_enum, "types"),
    ("pacing_budget_event", check_pacing_budget_event_struct, "types"),
    ("validator_port", check_validator_port, "data"),
    ("event_sink_port", check_event_sink_port, "data"),
    ("pacing_budget_guard", check_pacing_budget_guard, "data"),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    data_src = data_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else data_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_pacing_budget_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    data_src = data_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else data_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-6 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except PacingBudgetCheckError as error:
        print(f"ERR-6 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-6 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
