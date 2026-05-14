#!/usr/bin/env python3
"""Contract evidence script for feature ERR-4.

Verifies that the market-data subscription-limit gate declared in
``architecture/runtime_services.json`` (block ``subscription_limit_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-market-data``.

ERR-4 traces SRS-MD-002 (SyRS SYS-70 + SYS-64; StRS A-13). The contract
guarantees: (a) ``SubscriptionLimitState`` declares WithinLimit /
ExceededLimit in ``atp-types``; (b) ``SubscriptionLimitEvent`` carries the
five required fields (state, strategy_id, symbol, current_lines,
configured_limit) and no broker/vendor/tick leakage; (c) the
``SubscriptionLineCounter`` and ``SubscriptionLimitEventSink`` ports live
in ``atp-market-data``; (d) inside
``MarketDataSubscriptionManager::request_subscription``, the body matches
on ``counter.try_acquire(...)``, the WithinLimit leaf is the only call
site of ``SubscriptionAccepted {``, and the ExceededLimit leaf produces
``OrderErrorCategory::SubscriptionLimitReached``, records a
``SubscriptionLimitEvent`` via ``events.record(``, and does NOT mutate
the subscription registry (no calls listed in the contract's
``forbidden_mutations`` array — keeps rejection a read-only operation).

Mirrors the PASS/FAIL output style of ``tools/freshness_check.py``.

Invoke:
    python3 tools/subscription_limit_check.py
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


class SubscriptionLimitCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SubscriptionLimitCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "subscription_limit_contract" not in config:
        fail("architecture metadata is missing subscription_limit_contract")
    return config["subscription_limit_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def market_data_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["market_data_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"market-data crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_subscription_limit_state_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["subscription_limit_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — subscription-limit gate "
        "(SRS-MD-002 / SyRS SYS-70)"
    )


def check_subscription_limit_event_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["subscription_limit_event"]
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
            f"{spec['struct']} leaks broker/vendor field(s): {', '.join(leaks)} "
            "(ERR-4 events must not carry broker/session/tick identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor/tick fields"
    )


def check_subscription_request_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["subscription_request"]
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
            f"{spec['struct']} leaks broker/vendor field(s): {', '.join(leaks)} "
            "(ERR-4 requests must not carry broker/session identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])})"
    )


def check_line_counter_port(config: dict, market_data_src: str) -> str:
    block = contract_block(config)
    spec = block["line_counter_port"]
    body = _trait_body(market_data_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-market-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the SRS-MD-002 / SyRS SYS-70 line-accounting port"
    )


def check_event_sink_port(config: dict, market_data_src: str) -> str:
    block = contract_block(config)
    spec = block["event_sink_port"]
    body = _trait_body(market_data_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-market-data declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the structured-event publication channel for ERR-4 (dashboard alert fan-out)"
    )


def check_subscription_limit_guard(config: dict, market_data_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(market_data_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    counter_call_token = guard["counter_call"] + "("
    if counter_call_token not in body:
        fail(
            f"{entry['method']} does not call `{counter_call_token}` — "
            "the subscription-limit probe is the only legitimate entry to "
            "the gate"
        )

    within_token = f"{guard['state_enum']}::{guard['within_variant']}"
    exceeded_token = f"{guard['state_enum']}::{guard['exceeded_variant']}"
    for token in (within_token, exceeded_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "ERR-4 requires both WithinLimit and ExceededLimit to be "
                "handled inside the subscription-limit match"
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
            f"`{accepted_token}` — the WithinLimit leaf is the only "
            "legitimate construction site for the acceptance envelope"
        )
    if accepted_token in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf produces "
            f"`{accepted_token}` — ERR-4 requires zero acceptance side "
            "effect when the line limit is reached"
        )

    rejection = block["rejection_category"]
    category_token = f"OrderErrorCategory::{rejection}"
    factory_token = "StructuredSubscriptionError::limit_reached("
    # The ExceededLimit arm must construct the rejection envelope through
    # the category-pinned factory (which references
    # `OrderErrorCategory::SubscriptionLimitReached` inside atp-types) or
    # by naming the category directly. Either is acceptable; both signal
    # that the SyRS SYS-64 wire string source of truth is being honoured.
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
            f"call `{event_call_token}` (SRS-MD-002 / SyRS SYS-70 "
            "structured-event publication)"
        )

    # Zero-registry-mutation invariant: the ExceededLimit leaf must not
    # call any of the forbidden mutators listed in the contract. The
    # rejected request must leave the line accounting exactly as it
    # found it.
    forbidden_mutations = guard.get("forbidden_mutations", [])
    for mutator in forbidden_mutations:
        token = f"{mutator}("
        if token in exceeded_arm:
            fail(
                f"{entry['method']} {exceeded_token} leaf calls "
                f"`{token}` — ERR-4 requires zero side effect on the "
                "subscription registry when the gate rejects"
            )

    return (
        f"atp-market-data::{entry['method']} gates "
        f"`{guard['accepted_struct']}` on a match {within_token} via "
        f"`{guard['counter_call']}`; the {exceeded_token} leaf emits "
        f"OrderErrorCategory::{rejection}, records a "
        f"{block['subscription_limit_event']['struct']} via "
        f"`{guard['event_call']}`, and mutates nothing in the subscription "
        "registry (ERR-4)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["market_data_crate"]["crate"]
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
            "err_4_subscription_limit_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_4_subscription_limit_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_4_subscription_limit_blocked: PASS "
        "(line-limit rejection + zero registry mutation verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("subscription_request", check_subscription_request_struct, "types"),
    ("subscription_limit_state", check_subscription_limit_state_enum, "types"),
    ("subscription_limit_event", check_subscription_limit_event_struct, "types"),
    ("line_counter_port", check_line_counter_port, "market_data"),
    ("event_sink_port", check_event_sink_port, "market_data"),
    ("subscription_limit_guard", check_subscription_limit_guard, "market_data"),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    market_data_src = market_data_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else market_data_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_subscription_limit_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    market_data_src = market_data_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else market_data_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-4 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except SubscriptionLimitCheckError as error:
        print(f"ERR-4 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-4 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
