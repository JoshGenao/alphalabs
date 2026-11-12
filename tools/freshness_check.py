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
data-side condition, not a transport fault); (e) the paper half (SRS-MD-004 /
SYS-87): inside ``ExecutionEngine::dispatch_order``'s
``OrderRoute::InternalSimulation`` arm, a match on ``freshness.freshness(...)``
whose Fresh leaf is the only call site of ``simulation.submit_simulated(`` and
whose Stale leaf produces ``OrderErrorCategory::MarketDataStale`` and records
a ``StaleDataEvent``; the arm reads no brokerage connectivity, and the
``OrderRoute::LiveBrokerage`` arm does not re-check freshness (``route_order``
already does, so a second check would publish two events for one rejection).

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

from _rust_parser import _enum_body, _fn_block, _match_arm, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class FreshnessCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise FreshnessCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


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


def paper_gate_source(config: dict, root: Path = ROOT) -> str:
    block = freshness_block(config)
    if "paper_gate" not in block:
        fail("freshness_contract is missing paper_gate (SRS-MD-004 simulated-order half)")
    source_path = root / block["paper_gate"]["source"]
    if not source_path.exists():
        fail(f"paper gate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _normalize_calls(text: str) -> str:
    """Drop ``//`` comments and join rustfmt's method-chain line breaks.

    Comments go so that prose which NAMES a call cannot satisfy the check (the
    scanned bodies hold no string literal containing ``//``). Whitespace before
    a ``.`` goes because rustfmt breaks a long call as ``freshness\n.freshness(``,
    and a token check that fails on a reformat is a check people learn to edit
    rather than heed.
    """
    return re.sub(r"\s+\.", ".", re.sub(r"//[^\n]*", "", text))


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
        body = _normalize_calls(_fn_block(exec_src, entry["method"]))
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
        fail(f"{entry['method']} {stale_token} leaf must produce OrderErrorCategory::{rejection}")

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


def check_freshness_guard_in_dispatch_order(config: dict, paper_src: str) -> str:
    block = freshness_block(config)
    gate = block["paper_gate"]
    guard = block["guard"]
    try:
        body = _normalize_calls(_fn_block(paper_src, gate["method"]))
    except AssertionError as error:
        fail(str(error))
    try:
        paper_arm = _match_arm(body, gate["arm"])
        live_arm = _match_arm(body, "OrderRoute::LiveBrokerage")
    except AssertionError as error:
        fail(str(error))

    freshness_call_token = gate["freshness_call"] + "("
    simulation_call_token = gate["simulation_call"] + "("
    if freshness_call_token not in paper_arm:
        fail(
            f"{gate['method']} {gate['arm']} arm does not call `{freshness_call_token}` "
            "— paper submissions would bypass the SRS-MD-004 stale-data gate"
        )
    if freshness_call_token in live_arm:
        fail(
            f"{gate['method']} OrderRoute::LiveBrokerage arm calls `{freshness_call_token}` "
            "— route_order already gates live freshness; a second check publishes two "
            "events for one rejection"
        )

    fresh_token = f"{guard['state_enum']}::{guard['fresh_variant']}"
    stale_token = f"{guard['state_enum']}::{guard['stale_variant']}"
    try:
        fresh_arm = _match_arm(paper_arm, fresh_token)
        stale_arm = _match_arm(paper_arm, stale_token)
    except AssertionError as error:
        fail(str(error))

    if simulation_call_token not in fresh_arm:
        fail(
            f"{gate['method']} paper {fresh_token} leaf does not call "
            f"`{simulation_call_token}` — fresh paper orders would never be simulated"
        )
    if paper_arm.count(simulation_call_token) != fresh_arm.count(simulation_call_token):
        fail(
            f"{gate['method']} calls `{simulation_call_token}` outside the {fresh_token} "
            "leaf — a paper order could reach the simulator without the freshness gate"
        )

    rejection = block["rejection_category"]
    if f"OrderErrorCategory::{rejection}" not in stale_arm:
        fail(
            f"{gate['method']} paper {stale_token} leaf must produce "
            f"OrderErrorCategory::{rejection}"
        )
    event_call_token = gate["event_call"] + "("
    if event_call_token not in stale_arm:
        fail(
            f"{gate['method']} paper {stale_token} leaf is missing required call "
            f"`{event_call_token}` (SRS-MD-004 structured-event publication)"
        )
    if "connectivity." in paper_arm:
        fail(
            f"{gate['method']} {gate['arm']} arm reads brokerage connectivity — paper "
            "never touches IB, so an IB outage must not refuse a simulated order"
        )

    return (
        f"atp-execution::{gate['method']} gates `{gate['simulation_call']}` on "
        f"{fresh_token} inside {gate['arm']}; the {stale_token} leaf emits "
        f"OrderErrorCategory::{rejection} and records a "
        f"{block['stale_data_event']['struct']} via `{gate['event_call']}` "
        "(SRS-MD-004 / SYS-87 paper half)"
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
    paper = subprocess.run(
        [cargo, "test", "-p", crate, "--test", "srs_md_004_paper_stale_gate", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if paper.returncode != 0:
        fail(
            f"cargo test -p {crate} --test srs_md_004_paper_stale_gate failed:\n"
            f"{paper.stdout}\n{paper.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_3_stale_data_blocked + "
        "srs_md_004_paper_stale_gate: PASS (live and paper stale-gated rejection, "
        "zero broker and simulation side effect verified)"
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
    ("paper_freshness_guard", check_freshness_guard_in_dispatch_order, "paper"),
)


def _sources(config: dict, root: Path = ROOT) -> dict[str, str]:
    return {
        "types": types_source(config, root),
        "execution": execution_source(config, root),
        "paper": paper_gate_source(config, root),
    }


def run_checks() -> list[str]:
    config = load_config()
    sources = _sources(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        evidence.append(check(config, sources[scope]))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_freshness_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    sources = _sources(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        evidence.append(check(config, sources[scope]))
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
