#!/usr/bin/env python3
"""Contract evidence script for feature ERR-3.

Verifies that the market-data freshness gate declared in
``architecture/runtime_services.json`` (block ``freshness_contract``) is
reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-execution``.

ERR-3 traces SRS-MD-004 (SyRS SYS-39a / SYS-64 / SYS-87 / NFR-P5). The
contract guarantees: (a) ``MarketDataFreshness`` declares Fresh / Stale in
``atp-types``; (b) ``StaleDataEvent`` carries the four required fields
(state, strategy_id, symbol, staleness_seconds) and no broker/vendor/tick
leakage; (c) the ``MarketDataFreshnessProbe`` and ``StaleDataEventSink``
ports live in ``atp-execution``; (d) inside the ``StrategyMode::Live`` arm
of ``ExecutionEngine::submit_live_order``, the Connected sub-arm nests a
match on ``freshness.freshness(...)`` whose Fresh leaf is the only call
site of ``broker.submit_order(`` and whose Stale leaf produces
``OrderErrorCategory::MarketDataStale``, records a ``StaleDataEvent``,
and does NOT call ``connectivity.request_reconnect`` (staleness is a
data-side condition, not a transport fault).

Mirrors the PASS/FAIL output style of ``tools/connectivity_check.py``.

Invoke:
    python3 tools/freshness_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from connectivity_check import _trait_body
from error_handling_check import _fn_block, _match_arm
from historical_data_check import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class FreshnessCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise FreshnessCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def freshness_block(config: dict) -> dict:
    if "freshness_contract" not in config:
        fail("architecture metadata is missing freshness_contract")
    return config["freshness_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = freshness_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def execution_source(config: dict, root: Path = ROOT) -> str:
    block = freshness_block(config)
    crate_path = root / block["execution_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_freshness_state_enum(config: dict, types_src: str) -> str:
    block = freshness_block(config)
    spec = block["freshness_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — market-data freshness "
        "gate (SRS-MD-004 / NFR-P5)"
    )


def check_stale_data_event_struct(config: dict, types_src: str) -> str:
    block = freshness_block(config)
    spec = block["stale_data_event"]
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
            "(ERR-3 events must not carry broker/session/tick identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor/tick fields"
    )


def check_freshness_probe_port(config: dict, exec_src: str) -> str:
    block = freshness_block(config)
    spec = block["freshness_probe_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the SRS-MD-004 freshness probe (subscription manager port)"
    )


def check_stale_data_event_sink_port(config: dict, exec_src: str) -> str:
    block = freshness_block(config)
    spec = block["stale_data_event_sink_port"]
    body = _trait_body(exec_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the structured-event publication channel for ERR-3"
    )


def check_freshness_guard_in_submit_live_order(config: dict, exec_src: str) -> str:
    block = freshness_block(config)
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

    # The Connected sub-arm must contain a nested match on freshness.
    try:
        connected_arm = _match_arm(live_arm, "ConnectivityState::Connected")
    except AssertionError as error:
        fail(str(error))

    freshness_call_token = guard["freshness_call"] + "("
    if freshness_call_token not in connected_arm:
        fail(
            f"{entry['method']} Connected sub-arm does not call "
            f"`{freshness_call_token}` — Live submissions would bypass the "
            "SRS-MD-004 staleness check"
        )

    fresh_token = f"{guard['state_enum']}::{guard['fresh_variant']}"
    stale_token = f"{guard['state_enum']}::{guard['stale_variant']}"
    for token in (fresh_token, stale_token):
        if token not in connected_arm:
            fail(
                f"{entry['method']} Connected sub-arm is missing the "
                f"`{token}` branch — ERR-3 requires both Fresh and Stale to "
                "be handled inside the freshness match"
            )

    try:
        fresh_arm = _match_arm(connected_arm, fresh_token)
    except AssertionError as error:
        fail(str(error))
    try:
        stale_arm = _match_arm(connected_arm, stale_token)
    except AssertionError as error:
        fail(str(error))

    broker_call_token = guard["broker_call"] + "("
    if broker_call_token not in fresh_arm:
        fail(
            f"{entry['method']} {fresh_token} leaf does not call "
            f"`{broker_call_token}` — Fresh submissions would never reach "
            "the broker"
        )
    if broker_call_token in stale_arm:
        fail(
            f"{entry['method']} {stale_token} leaf calls `{broker_call_token}` "
            "— ERR-3 requires zero broker side effect when market data is stale"
        )

    rejection = block["rejection_category"]
    if f"OrderErrorCategory::{rejection}" not in stale_arm:
        fail(
            f"{entry['method']} {stale_token} leaf must produce "
            f"OrderErrorCategory::{rejection}"
        )

    event_call_token = guard["event_call"] + "("
    if event_call_token not in stale_arm:
        fail(
            f"{entry['method']} {stale_token} leaf is missing required call "
            f"`{event_call_token}` (SRS-MD-004 structured-event publication)"
        )

    # Staleness is a data-side condition: the Stale leaf must NOT call the
    # connectivity reconnect port. That port is reserved for transport faults
    # (ERR-2's Unreachable / ScheduledRestartWindow branches).
    if "connectivity.request_reconnect(" in stale_arm:
        fail(
            f"{entry['method']} {stale_token} leaf must not call "
            "`connectivity.request_reconnect(` — staleness is a data-side "
            "condition, not a transport fault"
        )

    return (
        f"atp-execution::{entry['method']} gates `{guard['broker_call']}` on "
        f"a nested match {fresh_token} inside ConnectivityState::Connected; "
        f"the {stale_token} leaf emits OrderErrorCategory::{rejection}, "
        f"records a {block['stale_data_event']['struct']} via "
        f"`{guard['event_call']}`, and produces zero broker side effect "
        "(ERR-3)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = freshness_block(config)
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
            "err_3_stale_data_blocked",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_3_stale_data_blocked failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_3_stale_data_blocked: PASS "
        "(stale-gated rejection + zero broker side effect verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("freshness_state", check_freshness_state_enum, "types"),
    ("stale_data_event", check_stale_data_event_struct, "types"),
    ("freshness_probe_port", check_freshness_probe_port, "execution"),
    ("stale_data_event_sink_port", check_stale_data_event_sink_port, "execution"),
    ("freshness_guard", check_freshness_guard_in_submit_live_order, "execution"),
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


def assert_freshness_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    exec_src = execution_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-3 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except FreshnessCheckError as error:
        print(f"ERR-3 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-3 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
