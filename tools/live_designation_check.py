#!/usr/bin/env python3
"""Contract evidence script for feature SRS-EXE-001.

Verifies that the live-designation authority declared in
``architecture/runtime_services.json`` (block ``live_designation_contract``)
is present in the Rust crate ``crates/atp-execution``.

SRS-EXE-001 ("route orders to IB only for the designated live strategy")
traces SyRS SYS-1 / SYS-2a / SYS-2c / SYS-2d / AC-15, NFR-P1 / NFR-S2, and
StRS SN-1.01 / SN-1.06 / SN-1.11. The execution gate ``submit_live_order``
trusts a caller-passed ``StrategyMode``; this contract pins the *authority*
that establishes which strategy may route to IB. It guarantees:

  (a) ``LiveDesignationConfirmation`` is an explicit-confirmation token with
      a private field, a named ``from_operator`` constructor, and NO
      ``Default`` derive — so designation cannot be satisfied by an implicit
      value (SYS-2d / NFR-S2).
  (b) ``LiveDesignation`` exposes ``new`` / ``designate`` / ``demote`` /
      ``designated`` / ``authority_for`` and ``designate`` takes the
      confirmation token by value.
  (c) ``LiveRoutingDecision`` declares Authorized / NotDesignated and
      ``LiveDesignationError`` declares MissingConfirmation /
      ConfirmationMismatch / AlreadyDesignated / NotDesignated.
  (d) inside ``ExecutionEngine::route_order``, the body matches on
      ``designation.authority_for(...)``; the NotDesignated leaf produces
      ``OrderErrorCategory::NonLiveStrategySubmission`` and consults NONE of
      the broker / connectivity / freshness ports listed in the contract's
      ``forbidden_ports`` array (the rejection short-circuits before any
      side-effecting port); the Authorized leaf delegates to
      ``self.submit_live_order(`` with ``StrategyMode::Live`` derived from the
      authority, never trusted from the caller.

Mirrors the PASS/FAIL output style of ``tools/subscription_limit_check.py``.

Invoke:
    python3 tools/live_designation_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _match_arm, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class LiveDesignationCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise LiveDesignationCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "live_designation_contract" not in config:
        fail("architecture metadata is missing live_designation_contract")
    return config["live_designation_contract"]


def execution_source(config: dict, root: Path = ROOT) -> str:
    """Return lib.rs + designation.rs concatenated.

    The route_order gate lives in lib.rs; the LiveDesignation authority, the
    confirmation token, and the decision/error enums live in the designation
    module. The brace-matching helpers search the whole string, so the
    concatenation lets every collector resolve its construct.
    """
    block = contract_block(config)
    crate_path = root / block["execution_crate"]["path"]
    lib = crate_path / "src" / "lib.rs"
    designation = crate_path / "src" / "designation.rs"
    for source_path in (lib, designation):
        if not source_path.exists():
            fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return lib.read_text(encoding="utf-8") + "\n" + designation.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_confirmation_token(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    spec = block["confirmation_token"]
    struct = spec["struct"]
    try:
        body = _struct_body(exec_src, struct)
    except AssertionError as error:
        fail(str(error))
    if re.search(r"\bpub\s+\w+\s*:", body):
        fail(
            f"{struct} has a public field — the explicit-confirmation token "
            "must keep its fields private so designation cannot be satisfied "
            "by an implicit/forgeable value (SyRS SYS-2d / NFR-S2)"
        )
    constructor = spec["constructor"]
    if not re.search(rf"\bfn\s+{re.escape(constructor)}\s*\(", exec_src):
        fail(
            f"{struct} is missing the named constructor `{constructor}` — "
            "explicit confirmation must be constructed deliberately"
        )
    derive_match = re.search(
        rf"#\[derive\(([^)]*)\)\]\s*(?:#\[[^\]]*\]\s*)*pub struct {re.escape(struct)}\b",
        exec_src,
    )
    derives = derive_match.group(1) if derive_match else ""
    for forbidden in spec.get("forbidden_derives", []):
        if re.search(rf"\b{re.escape(forbidden)}\b", derives):
            fail(
                f"{struct} derives `{forbidden}` — an explicit-confirmation "
                "token must not be default-constructible (SyRS SYS-2d)"
            )
    return (
        f"atp-execution declares {struct} as an explicit-confirmation token "
        f"(private fields, `{constructor}` constructor, no "
        f"{'/'.join(spec.get('forbidden_derives', [])) or 'forbidden'} derive) "
        "— SYS-2d / NFR-S2"
    )


def check_registry(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    spec = block["registry"]
    struct = spec["struct"]
    try:
        body = _struct_body(exec_src, struct)
    except AssertionError as error:
        fail(str(error))
    field = spec.get("field")
    if field and not re.search(rf"\b{re.escape(field)}\s*:", body):
        fail(f"{struct} is missing the `{field}` field")
    derive_match = re.search(
        rf"#\[derive\(([^)]*)\)\]\s*(?:#\[[^\]]*\]\s*)*pub struct {re.escape(struct)}\b",
        exec_src,
    )
    derives = derive_match.group(1) if derive_match else ""
    for forbidden in spec.get("forbidden_derives", []):
        if re.search(rf"\b{re.escape(forbidden)}\b", derives):
            fail(
                f"{struct} derives `{forbidden}` — a cloned authority could be "
                "retained past a demote and keep authorizing a stale strategy "
                "(SyRS SYS-2a); the authority must not be Clone"
            )
    missing = [
        method
        for method in spec["methods"]
        if not re.search(rf"\bfn\s+{re.escape(method)}\s*\(", exec_src)
    ]
    if missing:
        fail(f"{struct} is missing methods: {', '.join(missing)}")
    confirmation_type = spec["confirmation_param_type"]
    designate_sig = re.search(r"\bpub\s+fn\s+designate\s*\(([^)]*)\)", exec_src)
    if designate_sig is None:
        fail(f"{struct}::designate signature could not be parsed")
    if confirmation_type not in designate_sig.group(1):
        fail(
            f"{struct}::designate does not take a `{confirmation_type}` — "
            "designation must require the explicit-confirmation token (SYS-2d)"
        )
    forbidden = spec.get("forbidden_derives", [])
    return (
        f"atp-execution declares {struct} with {len(spec['methods'])} methods "
        f"({', '.join(spec['methods'])}); designate requires a "
        f"{confirmation_type} (SYS-2a single-live + SYS-2d confirmation); "
        f"no {'/'.join(forbidden) or 'forbidden'} derive"
    )


def check_engine_ownership(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    spec = block["owner"]
    owner = spec["struct"]
    try:
        body = _struct_body(exec_src, owner)
    except AssertionError as error:
        fail(str(error))
    field = spec["field"]
    authority_type = spec["authority_type"]
    if not re.search(rf"\b{re.escape(field)}\s*:\s*{re.escape(authority_type)}\b", body):
        fail(
            f"{owner} does not own the authority as a `{field}: {authority_type}` "
            "field — the single live-designation authority must be engine-owned, "
            "not caller-supplied (SRS-EXE-001)"
        )
    # The routing boundary must NOT accept a caller-supplied authority instance.
    guard = block["guard"]
    no_caller_type = guard.get("no_caller_authority_type")
    entry_method = block["entry_point"]["method"]
    sig_match = re.search(
        rf"\bpub\s+fn\s+{re.escape(entry_method)}\s*(?:<[^>]*>)?\s*\(([^)]*)\)",
        exec_src,
    )
    if sig_match is None:
        fail(f"{entry_method} signature could not be parsed")
    if no_caller_type and re.search(rf"\b{re.escape(no_caller_type)}\b", sig_match.group(1)):
        fail(
            f"{entry_method} accepts a `{no_caller_type}` parameter — the routing "
            "boundary must consult the engine-owned authority, never a "
            "caller-supplied one (a strategy could otherwise designate itself)"
        )
    return (
        f"atp-execution::{owner} owns the authority as `{field}: {authority_type}` "
        f"and {entry_method} accepts no caller-supplied {authority_type} "
        "(SRS-EXE-001, SyRS SYS-2a)"
    )


def check_routing_decision(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    spec = block["routing_decision"]
    try:
        body = _enum_body(exec_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-execution declares {spec['enum']} with "
        f"{len(spec['variants'])} decisions ({', '.join(spec['variants'])})"
    )


def check_designation_error(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    spec = block["designation_error"]
    try:
        body = _enum_body(exec_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-execution declares {spec['enum']} with "
        f"{len(spec['variants'])} variants ({', '.join(spec['variants'])})"
    )


def check_route_order_guard(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(exec_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    authority_token = guard["authority_call"] + "("
    if authority_token not in body:
        fail(
            f"{entry['method']} does not call `{authority_token}` — the "
            "live-designation authority is the only legitimate source of the "
            "routing decision (SYS-2d)"
        )

    decision = guard["decision_enum"]
    authorized_token = f"{decision}::{guard['authorized_variant']}"
    not_designated_token = f"{decision}::{guard['not_designated_variant']}"
    for token in (authorized_token, not_designated_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "route_order must handle both authority decisions"
            )

    try:
        not_designated_arm = _match_arm(body, not_designated_token)
    except AssertionError as error:
        fail(str(error))
    try:
        authorized_arm = _match_arm(body, authorized_token)
    except AssertionError as error:
        fail(str(error))

    rejection = block["rejection_category"]
    category_token = f"OrderErrorCategory::{rejection}"
    if category_token not in not_designated_arm:
        fail(
            f"{entry['method']} {not_designated_token} leaf must produce "
            f"{category_token} — a non-designated submission is rejected with "
            "the SyRS SYS-64 NON_LIVE_STRATEGY_SUBMISSION category (ERR-1)"
        )

    # The NotDesignated leaf must short-circuit BEFORE any side-effecting port:
    # no broker / connectivity / freshness call may appear in it.
    for port in guard.get("forbidden_ports", []):
        token = f"{port}("
        if token in not_designated_arm:
            fail(
                f"{entry['method']} {not_designated_token} leaf calls `{token}` "
                "— a non-designated rejection must consult no broker, "
                "connectivity, or freshness port (SRS-EXE-001 / ERR-1)"
            )

    delegate_token = guard["delegate_call"] + "("
    if delegate_token not in authorized_arm:
        fail(
            f"{entry['method']} {authorized_token} leaf must delegate via "
            f"`{delegate_token}` — the designated strategy proceeds to the "
            "inner ERR-1/2/3 live gate"
        )
    live_mode_token = guard["live_mode_token"]
    if live_mode_token not in authorized_arm:
        fail(
            f"{entry['method']} {authorized_token} leaf must derive "
            f"`{live_mode_token}` from the authority — route_order must not "
            "trust a caller-supplied mode"
        )

    return (
        f"atp-execution::{entry['method']} resolves "
        f"`{guard['authority_call']}` and routes only on {authorized_token} "
        f"(delegating to `{guard['delegate_call']}` with {live_mode_token}); "
        f"the {not_designated_token} leaf emits {category_token} and consults "
        f"none of {len(guard.get('forbidden_ports', []))} forbidden ports "
        "(SRS-EXE-001)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["execution_crate"]["crate"]
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
        fail(f"cargo test -p {crate} --lib failed:\n{result.stdout}\n{result.stderr}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", "srs_exe_001_live_designation", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test srs_exe_001_live_designation failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + srs_exe_001_live_designation: PASS "
        "(authority invariants + only-the-designated-strategy-routes verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("confirmation_token", check_confirmation_token),
    ("registry", check_registry),
    ("engine_ownership", check_engine_ownership),
    ("routing_decision", check_routing_decision),
    ("designation_error", check_designation_error),
    ("route_order_guard", check_route_order_guard),
)


def run_checks() -> list[str]:
    config = load_config()
    exec_src = execution_source(config)
    evidence = [check(config, exec_src) for _, check in _STATIC_CHECKS]
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_live_designation_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    exec_src = execution_source(config, root)
    return [check(config, exec_src) for _, check in _STATIC_CHECKS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-001 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except LiveDesignationCheckError as error:
        print(f"SRS-EXE-001 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-EXE-001 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
