#!/usr/bin/env python3
"""Contract evidence script for feature ERR-5.

Verifies that the data-layer ingestion-validation gate declared in
``architecture/runtime_services.json`` (block ``ingestion_validation_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-data``.

ERR-5 traces SRS-DATA-013 (SyRS SYS-77; StRS SN-1.26 / SN-1.27). The
contract guarantees: (a) ``RecordValidationOutcome`` declares Valid /
Quarantined in ``atp-types``; (b) ``QuarantineReason`` enumerates the
six SyRS SYS-77 rule categories (a..f); (c) ``IngestionValidationEvent``
carries the five required fields (state, reason, source, record_hash,
observed_at_seconds) and no broker/vendor/tick leakage;
(d) ``IngestionRecordSubmission`` carries the two required fields
(source, record_hash) and no broker/vendor/tick leakage; (e) the
``RecordValidator`` and ``IngestionValidationEventSink`` ports live in
``atp-data``; (f) inside ``DataLayer::ingest_record``, the body matches
on ``validator.validate(...)``, the Valid leaf is the only call site of
``IngestionAccepted {``, and the Quarantined leaf produces
``OrderErrorCategory::IngestionRecordValidationFailed``, records an
``IngestionValidationEvent`` via ``events.record(``, and does NOT write
the record to primary storage (no calls listed in the contract's
``forbidden_mutations`` array — keeps rejection a read-only operation).

Mirrors the PASS/FAIL output style of ``tools/subscription_limit_check.py``.

Invoke:
    python3 tools/ingestion_validation_check.py
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


class IngestionValidationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise IngestionValidationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "ingestion_validation_contract" not in config:
        fail("architecture metadata is missing ingestion_validation_contract")
    return config["ingestion_validation_contract"]


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
# Local helper — payload-tolerant match-arm extractor
# --------------------------------------------------------------------------- #


def _quarantined_arm(body: str, variant_token: str) -> str:
    """Return the body of the ``<variant_token>(<binding>) =>`` arm.

    ``_match_arm`` requires the pattern to appear literally; the ERR-5
    Quarantined arm carries a binding (e.g. ``Quarantined(reason)``)
    that we don't want the static check to hard-code by name. This
    helper finds the variant followed by any ``(...)`` payload, then
    extracts the arm body using the same brace/paren-balanced scan as
    ``_match_arm``.
    """
    arm_match = re.search(
        rf"{re.escape(variant_token)}\s*\([^)]*\)\s*=>\s*", body
    )
    if not arm_match:
        fail(f"ingest_record is missing match arm for `{variant_token}(...)`")
    start = arm_match.end()
    depth = 0
    index = start
    in_string = False
    string_char = ""
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\" and index + 1 < len(body):
                index += 2
                continue
            if char == string_char:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            string_char = char
        elif char == "{" or char == "(":
            depth += 1
        elif char == "}" or char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_ingestion_record_submission_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["ingestion_record_submission"]
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
            "(ERR-5 ingestion envelopes must not carry broker/session/tick/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/broker fields"
    )


def check_record_validation_outcome_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["record_validation_outcome"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — ingestion-validation gate "
        "(SRS-DATA-013 / SyRS SYS-77)"
    )


def check_quarantine_reason_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["quarantine_reason"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"variants ({', '.join(spec['variants'])}) — SyRS SYS-77 rule "
        "categories (a..f) for the count-and-nature dashboard alert"
    )


def check_ingestion_validation_event_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["ingestion_validation_event"]
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
            "(ERR-5 events must not carry broker/session/tick/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/broker/tick fields"
    )


def check_record_validator_port(config: dict, data_src: str) -> str:
    block = contract_block(config)
    spec = block["validator_port"]
    body = _trait_body(data_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the SRS-DATA-013 / SyRS SYS-77 read-only validation probe"
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
        "the structured-event publication channel for ERR-5 (dashboard alert + quarantine fan-out)"
    )


def check_ingestion_validation_guard(config: dict, data_src: str) -> str:
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
            "the record-validation probe is the only legitimate entry to the gate"
        )

    valid_token = f"{guard['state_enum']}::{guard['valid_variant']}"
    quarantined_token = f"{guard['state_enum']}::{guard['quarantined_variant']}"
    for token in (valid_token, quarantined_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "ERR-5 requires both Valid and Quarantined to be handled "
                "inside the record-validation match"
            )

    try:
        valid_arm = _match_arm(body, valid_token)
    except AssertionError as error:
        fail(str(error))
    quarantined_arm = _quarantined_arm(body, quarantined_token)

    accepted_token = guard["accepted_struct"] + " {"
    if accepted_token not in valid_arm:
        fail(
            f"{entry['method']} {valid_token} leaf does not produce "
            f"`{accepted_token}` — the Valid leaf is the only legitimate "
            "construction site for the acceptance envelope"
        )
    if accepted_token in quarantined_arm:
        fail(
            f"{entry['method']} {quarantined_token} leaf produces "
            f"`{accepted_token}` — ERR-5 requires zero acceptance side "
            "effect when the record is quarantined"
        )

    rejection = block["rejection_category"]
    category_token = f"OrderErrorCategory::{rejection}"
    factory_token = "StructuredIngestionError::quarantined("
    # The Quarantined arm must construct the rejection envelope through
    # the category-pinned factory (which references
    # `OrderErrorCategory::IngestionRecordValidationFailed` inside
    # atp-types) or by naming the category directly. Either is
    # acceptable; both signal that the SyRS SYS-64 wire string source
    # of truth is being honoured.
    if category_token not in quarantined_arm and factory_token not in quarantined_arm:
        fail(
            f"{entry['method']} {quarantined_token} leaf must produce "
            f"{category_token} (directly or via the "
            f"`{factory_token.rstrip('(')}` factory — the SyRS SYS-64 "
            "wire string source of truth)"
        )

    event_call_token = guard["event_call"] + "("
    if event_call_token not in quarantined_arm:
        fail(
            f"{entry['method']} {quarantined_token} leaf is missing required "
            f"call `{event_call_token}` (SRS-DATA-013 / SyRS SYS-77 "
            "structured-event publication)"
        )

    # Zero-primary-write invariant: the Quarantined leaf must not call
    # any of the forbidden mutators listed in the contract. The
    # rejected record must leave the primary storage tier exactly as
    # it found it.
    forbidden_mutations = guard.get("forbidden_mutations", [])
    for mutator in forbidden_mutations:
        token = f"{mutator}("
        if token in quarantined_arm:
            fail(
                f"{entry['method']} {quarantined_token} leaf calls "
                f"`{token}` — ERR-5 requires zero write to primary "
                "storage when the gate quarantines"
            )

    return (
        f"atp-data::{entry['method']} gates "
        f"`{guard['accepted_struct']}` on a match {valid_token} via "
        f"`{guard['validator_call']}`; the {quarantined_token} leaf emits "
        f"OrderErrorCategory::{rejection}, records an "
        f"{block['ingestion_validation_event']['struct']} via "
        f"`{guard['event_call']}`, and writes nothing to the primary "
        "storage tier (ERR-5)"
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
            "err_5_record_validation_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_5_record_validation_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_5_record_validation_blocked: PASS "
        "(record-validation rejection + zero primary-write verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("ingestion_record_submission", check_ingestion_record_submission_struct, "types"),
    ("record_validation_outcome", check_record_validation_outcome_enum, "types"),
    ("quarantine_reason", check_quarantine_reason_enum, "types"),
    ("ingestion_validation_event", check_ingestion_validation_event_struct, "types"),
    ("record_validator_port", check_record_validator_port, "data"),
    ("event_sink_port", check_event_sink_port, "data"),
    ("ingestion_validation_guard", check_ingestion_validation_guard, "data"),
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


def assert_ingestion_validation_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    data_src = data_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else data_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-5 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except IngestionValidationCheckError as error:
        print(f"ERR-5 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-5 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
