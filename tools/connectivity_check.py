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


def _code_only(source: str) -> str:
    """Strip line comments so a guard tests CODE, not the prose about it.

    Without this, a doc comment that says "never `to_socket_addrs`" trips the
    check that forbids `to_socket_addrs` — the guard fires on the sentence
    explaining why the guard exists. A check that flags its own documentation is
    one people learn to route around.
    """
    return "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("//", "*"))
    )


def _fn_body_any(source: str, fn_name: str) -> str:
    """Return a function body, tolerating ``const`` / ``async`` modifiers.

    ``_rust_parser._fn_block`` matches ``pub fn`` exactly, and the restart-window
    classifiers are ``pub const fn`` (they are pure and evaluated at compile time
    in the crate's own tests). Widening the SHARED parser would change what ~20
    other check tools match, so the tolerance lives here instead.
    """
    match = re.search(rf"\bpub\s+(?:const\s+|async\s+)*fn\s+{re.escape(fn_name)}\b[^{{]*{{", source)
    if not match:
        fail(f"Rust source is missing function `{fn_name}`")
    start = match.end()
    depth = 1
    index = start
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse function body for `{fn_name}`")
    return source[start : index - 1]


def restart_window_block(config: dict) -> dict:
    block = connectivity_block(config)
    if "restart_window" not in block:
        fail("connectivity_contract is missing the SRS-MD-005 restart_window block")
    return block["restart_window"]


def _crate_lib_source(config: dict, crate_key: str, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block[crate_key]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"{crate_key} source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def market_data_source(config: dict, root: Path = ROOT) -> str:
    return _crate_lib_source(config, "market_data_crate", root)


def producer_source(config: dict, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block["producer"]["module"]
    if not source_path.exists():
        fail(f"restart-window producer missing: {block['producer']['module']}")
    return source_path.read_text(encoding="utf-8")


def reachability_source(config: dict, root: Path = ROOT) -> str:
    block = restart_window_block(config)
    source_path = root / block["reachability_port"]["module"]
    if not source_path.exists():
        fail(f"gateway-reachability seam missing: {block['reachability_port']['module']}")
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
        fail(f"{entry['method']} blocked-state branch must produce OrderErrorCategory::{rejection}")
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


# --------------------------------------------------------------------------- #
# SRS-MD-005 — the scheduled restart window (SyRS SYS-75)
# --------------------------------------------------------------------------- #


def check_restart_phase_enum(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["restart_phase"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} phases "
        f"({', '.join(spec['variants'])}) — the SyRS SYS-75 restart window (SRS-MD-005)"
    )


def check_market_data_admission_enum(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["market_data_admission"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} outcomes "
        f"({', '.join(spec['variants'])}) — a market-data refusal states WHETHER and WHY, "
        "so planned maintenance is never rendered as an outage or the reverse"
    )


def check_restart_window_defaults(config: dict, types_src: str) -> str:
    spec = restart_window_block(config)["window_type"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    if re.search(r"\bpub\s+\w+\s*:", body):
        fail(
            f"{spec['struct']} exposes a public field — the window is a safety input and "
            "must be constructible only through its validating constructor"
        )
    for method in spec["methods"]:
        if f"fn {method}(" not in types_src:
            fail(f"{spec['struct']} is missing the `{method}` method")
    defaults = spec["defaults"]
    for const_key, value_key in (
        ("suspend_lead_seconds_const", "suspend_lead_seconds"),
        ("window_seconds_const", "window_seconds"),
    ):
        name = defaults[const_key]
        expected = defaults[value_key]
        match = re.search(rf"{re.escape(name)}\s*:\s*i64\s*=\s*(\d+)\s*;", types_src)
        if match is None:
            fail(f"atp-types is missing the `{name}` default constant")
        if int(match.group(1)) != expected:
            fail(
                f"{name} is {match.group(1)} but the SyRS SYS-75 catalogue default is "
                f"{expected} — a documented default the code does not implement"
            )
    return (
        f"atp-types declares {spec['struct']} with private fields, "
        f"{len(spec['methods'])} classification methods, and the SyRS SYS-75 defaults "
        f"({defaults['suspend_lead_seconds']}s lead / {defaults['window_seconds']}s window)"
    )


def check_restart_escalation_arm(config: dict, types_src: str) -> str:
    """The sharpest clause: the window must END.

    A `RestartPhase::Elapsed` that still returned the maintenance state would
    suppress a genuine outage forever, and nothing else in the tree would
    notice — the dispatcher would keep honouring a marker that never cleared.
    """
    block = restart_window_block(config)
    escalation = block["escalation"]
    body = _fn_body_any(types_src, "connectivity_state")
    phase = block["restart_phase"]["enum"]
    elapsed = f"{phase}::{escalation['phase']}"
    if elapsed not in body:
        fail(
            f"connectivity_state does not handle {elapsed} — without it the restart "
            "window never closes and a real outage stays suppressed"
        )
    unreachable = f"ConnectivityState::{escalation['unreachable_state']}"
    if unreachable not in body:
        fail(
            f"connectivity_state never produces {unreachable} — the SyRS SYS-75 "
            "escalation to standard connectivity-loss handling is unreachable"
        )
    if re.search(r"^\s*_\s*=>", body, re.MULTILINE):
        fail(
            "connectivity_state uses a catch-all match arm — a phase added later would "
            "inherit whichever answer happened to be there instead of failing to compile"
        )
    return (
        f"atp-types::connectivity_state maps {elapsed} onto {unreachable} with no "
        "catch-all arm — the SyRS SYS-75 escalation is exhaustive by construction"
    )


def check_market_data_admission_sites(config: dict, market_data_src: str) -> str:
    """Enumerate the admission functions FROM THE SOURCE, not from a checklist.

    A list of "these two functions consult the guard" passes happily while a
    third admission path added later admits during the window. So the discovery
    runs over the source: every public function whose body registers or admits a
    subscription must consult the gate, and a declared function that has
    vanished fails too (a broken scan reports a clean tree, which is the failure
    mode that looks most like working).
    """
    block = restart_window_block(config)
    spec = block["admission_sites"]
    guard_call = spec["guard_call"]
    port = block["gate_port"]

    try:
        trait_body = _trait_body(market_data_src, port["trait"])
    except AssertionError as error:
        fail(str(error))
    for method in port["methods"]:
        if f"fn {method}(" not in trait_body:
            fail(f"{port['trait']} is missing the `{method}` method")

    declared = list(spec["functions"])
    discovered = sorted(
        set(re.findall(r"pub fn (\w+)\s*<[^>]*W: RestartWindowGate", market_data_src))
        | set(re.findall(r"pub fn (\w+)[\s\S]{0,400}?window: &W", market_data_src))
    )
    if not discovered:
        fail(
            "no subscription admission function takes a RestartWindowGate — either the "
            "guard was removed or this scan no longer matches the source, and a scan "
            "that finds nothing must fail rather than report a clean tree"
        )
    missing = [name for name in declared if name not in discovered]
    if missing:
        fail(
            f"declared admission site(s) {', '.join(missing)} no longer take the "
            "restart-window port — the contract and the source disagree"
        )
    undeclared = [name for name in discovered if name not in declared]
    if undeclared:
        fail(
            f"undeclared admission site(s) {', '.join(undeclared)} take the "
            "restart-window port; add them to admission_sites.functions so the "
            "contract enumerates every arm"
        )
    for name in discovered:
        try:
            body = _fn_block(market_data_src, name)
        except AssertionError as error:
            fail(str(error))
        if guard_call not in body:
            fail(
                f"atp-market-data::{name} accepts the restart-window port but never "
                f"calls `{guard_call}` — SyRS SYS-75(a) requires market-data requests "
                "to be suspended at EVERY admission point, not just the outermost"
            )
    return (
        f"atp-market-data gates {len(discovered)} subscription admission site(s) "
        f"({', '.join(discovered)}) on `{guard_call}` through the {port['trait']} port "
        "— SyRS SYS-75(a) market-data suspension"
    )


def check_restart_window_producer(config: dict, producer_src: str) -> str:
    block = restart_window_block(config)
    spec = block["producer"]
    if f"pub struct {spec['struct']}" not in producer_src:
        fail(f"{spec['module']} does not declare `{spec['struct']}`")
    for trait in spec["implements"]:
        if not re.search(rf"impl<[^>]*>\s+(?:\w+::)*{re.escape(trait)}\s+for", producer_src):
            fail(
                f"{spec['struct']} does not implement `{trait}` — the order gate and the "
                "market-data gate must read ONE window, or they drift"
            )
    return (
        f"atp-orchestrator::{spec['struct']} implements "
        f"{' + '.join(spec['implements'])} — one producer behind both suspensions "
        "(SRS-MD-005)"
    )


def check_reachability_seam_is_unpinned(config: dict, reachability_src: str) -> str:
    """The probe must NOT live in the digest-pinned transport module.

    `tools/ib_adapter_check.py` SHA-256s `interactive_brokers.rs` against the
    operator's live paper-account evidence, so a probe added there would flip a
    closed-green feature red and need a fresh live run to recover.
    """
    block = restart_window_block(config)
    spec = block["reachability_port"]
    if "interactive_brokers" in spec["module"]:
        fail(
            "the reachability seam is declared inside the digest-pinned transport "
            "module; it must be its own file (see connection_control.rs)"
        )
    try:
        trait_body = _trait_body(reachability_src, spec["trait"])
    except AssertionError as error:
        fail(str(error))
    for method in spec["methods"]:
        if f"fn {method}(" not in trait_body:
            fail(f"{spec['trait']} is missing the `{method}` method")
    code = _code_only(reachability_src)
    if "to_socket_addrs" in code:
        fail(
            "the reachability probe resolves a hostname — a blocking getaddrinfo sits "
            "OUTSIDE connect_timeout's deadline, so the bound would be a suggestion"
        )
    if "connect_timeout" not in code:
        fail(
            "the reachability probe has no explicit connect deadline — a bare "
            "TcpStream::connect hangs on the OS default"
        )
    return (
        f"atp-adapters::{spec['trait']} lives outside the digest-pinned transport module, "
        "probes with an explicit connect deadline, and never resolves a hostname"
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
    # SRS-MD-005 — the producer of ConnectivityState::ScheduledRestartWindow.
    ("restart_phase", check_restart_phase_enum, "types"),
    ("market_data_admission", check_market_data_admission_enum, "types"),
    ("restart_window_defaults", check_restart_window_defaults, "types"),
    ("restart_escalation", check_restart_escalation_arm, "types"),
    ("market_data_admission_sites", check_market_data_admission_sites, "market_data"),
    ("restart_window_producer", check_restart_window_producer, "producer"),
    ("reachability_seam", check_reachability_seam_is_unpinned, "reachability"),
)


def _sources(config: dict, root: Path = ROOT) -> dict[str, str]:
    return {
        "types": types_source(config, root),
        "execution": execution_source(config, root),
        "market_data": market_data_source(config, root),
        "producer": producer_source(config, root),
        "reachability": reachability_source(config, root),
    }


def run_checks() -> list[str]:
    config = load_config()
    sources = _sources(config)
    evidence = [check(config, sources[scope]) for _, check, scope in _STATIC_CHECKS]
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_connectivity_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    sources = _sources(config, root)
    return [check(config, sources[scope]) for _, check, scope in _STATIC_CHECKS]


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
