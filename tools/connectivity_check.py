#!/usr/bin/env python3
"""Contract evidence script for feature ERR-2.

Verifies that the IB-Gateway connectivity gate declared in
``architecture/runtime_services.json`` (block ``connectivity_contract``) is
reachable from the Rust crates ``crates/atp-types`` and ``crates/atp-execution``.

ERR-2 traces SRS-SAFE-003 + SRS-MD-005 (SyRS SYS-45 / SYS-46 / NFR-R2). The
contract guarantees: (a) ``ConnectivityState`` declares Connected /
Unreachable / ScheduledRestartWindow in ``atp-types``; (b)
``ConnectivityEvent`` carries the four required fields (state, strategy_id,
symbol, scheduled_restart) and no broker/vendor leakage; (c) the
``BrokerageConnectivity`` and ``ConnectivityEventSink`` ports live in
``atp-execution``; (d) inside the ``StrategyMode::Live`` arm of
``ExecutionEngine::submit_live_order``, the broker is only called when
``ConnectivityState::Connected``, and the Unreachable /
ScheduledRestartWindow branch produces
``OrderErrorCategory::ConnectivityBlocked``, records a
``ConnectivityEvent``, and requests a reconnect — all without invoking
the brokerage port.

Mirrors the PASS/FAIL output style of ``tools/error_handling_check.py``.

Invoke:
    python3 tools/connectivity_check.py
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


class ConnectivityCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise ConnectivityCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def connectivity_block(config: dict) -> dict:
    if "connectivity_contract" not in config:
        fail("architecture metadata is missing connectivity_contract")
    return config["connectivity_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = connectivity_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def execution_source(config: dict, root: Path = ROOT) -> str:
    block = connectivity_block(config)
    crate_path = root / block["execution_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_connectivity_state_enum(config: dict, types_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — connectivity safety gate "
        "(SRS-SAFE-003 / SRS-MD-005)"
    )


def check_connectivity_event_struct(config: dict, types_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_event"]
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
            "(ERR-2 events must not carry broker/session identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor fields"
    )


def check_brokerage_connectivity_port(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    spec = block["brokerage_connectivity_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the SRS-SAFE-003 connectivity probe + reconnect request"
    )


def check_connectivity_event_sink_port(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    spec = block["connectivity_event_sink_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the structured-event publication channel for ERR-2"
    )


def check_connectivity_guard_in_submit_live_order(config: dict, exec_src: str) -> str:
    block = connectivity_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(exec_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    try:
        live_arm = _match_arm(body, "StrategyMode::Live")
    except AssertionError as error:
        fail(str(error))

    # Inside the Live arm we expect a nested match on connectivity.state().
    connected_token = f"{guard['state_enum']}::{guard['connected_variant']}"
    if connected_token not in live_arm:
        fail(
            f"{entry['method']} Live arm is missing the connectivity-gate "
            f"`{connected_token}` branch — Live submissions would bypass the "
            "SRS-SAFE-003 connectivity check"
        )

    # The Connected sub-arm must be the only call site of broker.submit_order(.
    call_token = guard["broker_call"] + "("
    try:
        connected_arm = _match_arm(live_arm, connected_token)
    except AssertionError as error:
        fail(str(error))
    if call_token not in connected_arm:
        fail(
            f"{entry['method']} {connected_token} sub-arm does not call "
            f"`{call_token}` — Connected submissions would never reach the broker"
        )

    # The blocked-states branch must carry CONNECTIVITY_BLOCKED + events.record(
    # + connectivity.request_reconnect( AND must NOT call broker.submit_order(.
    blocked_block = live_arm
    for variant in guard["blocked_variants"]:
        if f"{guard['state_enum']}::{variant}" not in blocked_block:
            fail(
                f"{entry['method']} Live arm is missing the "
                f"{guard['state_enum']}::{variant} branch — "
                "ERR-2 requires both Unreachable and ScheduledRestartWindow "
                "to be blocked"
            )

    blocked_only = live_arm.replace(connected_arm, "")
    rejection = block["rejection_category"]
    if f"OrderErrorCategory::{rejection}" not in blocked_only:
        fail(
            f"{entry['method']} blocked-state branch must produce "
            f"OrderErrorCategory::{rejection}"
        )
    for token in (
        guard["event_call"] + "(",
        guard["reconnect_call"] + "(",
    ):
        if token not in blocked_only:
            fail(
                f"{entry['method']} blocked-state branch is missing required "
                f"call `{token}` (SRS-SAFE-003 logging + reconnect attempt)"
            )
    if call_token in blocked_only:
        fail(
            f"{entry['method']} blocked-state branch calls `{call_token}` — "
            "ERR-2 requires zero broker side effect when IB is unreachable"
        )

    return (
        f"atp-execution::{entry['method']} gates `{guard['broker_call']}` on "
        f"{connected_token}; Unreachable / ScheduledRestartWindow branches "
        f"emit OrderErrorCategory::{rejection}, record a "
        f"{block['connectivity_event']['struct']}, and call "
        f"`{guard['reconnect_call']}` with zero broker side effect (ERR-2)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = connectivity_block(config)
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
            "err_2_connectivity_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_2_connectivity_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_2_connectivity_blocked: PASS "
        "(connectivity-gated rejection + zero broker side effect verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("connectivity_state", check_connectivity_state_enum, "types"),
    ("connectivity_event", check_connectivity_event_struct, "types"),
    ("brokerage_connectivity_port", check_brokerage_connectivity_port, "execution"),
    ("connectivity_event_sink_port", check_connectivity_event_sink_port, "execution"),
    ("connectivity_guard", check_connectivity_guard_in_submit_live_order, "execution"),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    exec_src = execution_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_connectivity_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    exec_src = execution_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-2 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except ConnectivityCheckError as error:
        print(f"ERR-2 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-2 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
