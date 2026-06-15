#!/usr/bin/env python3
"""Contract evidence script for feature SRS-EXE-002 (SDK-surface slice).

Verifies the source-neutral order-routing **dispatch authority** declared in
``architecture/runtime_services.json`` (block ``order_routing_contract``) is
present in the Rust crate ``crates/atp-execution`` and routes every non-live
strategy's order to the internal simulation engine -- never to IB.

SRS-EXE-002 ("route all non-live strategy orders to the internal simulation
engine") traces SyRS SYS-2b / SYS-2e, AC-10, and StRS SN-1.06 / SN-1.29 / C-11.
It is the normal-order-entry complement of the SRS-EXE-001 live-designation
authority (``designation.rs``): EXE-001 rejects non-designated submissions on
the *live* path; EXE-002 dispatches *every* order to one destination derived
from the same engine-owned ``LiveRoutingDecision`` authority. The check
guarantees:

  (a) ``OrderRoute`` declares LiveBrokerage / InternalSimulation -- the
      source-neutral routing destination, with no third variant.
  (b) ``SimulatedOrderReceipt`` is a DISTINCT type from ``OrderReceipt``: it
      carries ``sim_order_id`` and NOT ``broker_order_id`` -- a simulated order
      never mints a broker order id.
  (c) ``OrderRoutingReceipt`` declares Live / Simulated.
  (d) ``InternalSimulationSubmit`` is the port (declared at the execution layer
      like ``LiveBrokerageSubmit``) returning a ``SimulatedOrderReceipt`` -- so
      ``atp-execution`` never names a simulation type directly.
  (e) ``route_destination`` maps the engine-owned authority
      (``self.designation.authority_for``) to a destination: Authorized ->
      LiveBrokerage; NotDesignated -> InternalSimulation. The NotDesignated
      (non-live) arm MUST yield InternalSimulation and MUST NOT yield
      LiveBrokerage -- the AC-10 "paper orders never create IB orders"
      invariant.
  (f) ``dispatch_order`` branches on ``route_destination``: the LiveBrokerage
      arm delegates to ``route_order`` and touches no simulation port; the
      InternalSimulation arm submits to the simulation port and touches NONE of
      the broker / connectivity / freshness / route_order ports.
  (g) ``atp-execution`` does NOT depend on ``atp-simulation`` (SRS-ARCH-002
      one-way dependency direction; the simulation engine is reached only
      through the port).

Like ``tools/order_type_check.py`` this is a safety gate, so the cargo smoke
FAILS CLOSED when cargo is absent rather than reporting PASS on the static
regex alone -- the executable Rust proof (the order_routing lib tests + the
srs_exe_002_order_routing integration test, which route a paper order to a
panic-on-touch broker) is authoritative.

This is the routing-authority / SDK-surface half. It does NOT wire the real
simulation engine, run the Python strategy runtime, or exercise the operator
IB-paper-account workflow -- so it is NOT a full SRS-EXE-002 requirement pass.

Invoke:
    python3 tools/order_routing_check.py
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


class OrderRoutingCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OrderRoutingCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "order_routing_contract" not in config:
        fail("architecture metadata is missing order_routing_contract")
    return config["order_routing_contract"]


def execution_source(config: dict, root: Path = ROOT) -> str:
    """Return lib.rs + designation.rs + order_routing.rs concatenated.

    ``ExecutionEngine`` and ``route_order`` live in lib.rs; the
    ``LiveRoutingDecision`` authority lives in designation.rs; the
    ``OrderRoute`` decision, the ``InternalSimulationSubmit`` port, and the
    ``route_destination`` / ``dispatch_order`` methods live in order_routing.rs.
    The brace-matching helpers search the whole string, so the concatenation
    lets every collector resolve its construct.
    """
    block = contract_block(config)
    crate_path = root / block["execution_crate"]["path"]
    sources = (
        crate_path / "src" / "lib.rs",
        crate_path / "src" / "designation.rs",
        crate_path / "src" / "order_routing.rs",
    )
    for source_path in sources:
        if not source_path.exists():
            fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return "\n".join(p.read_text(encoding="utf-8") for p in sources)


def manifest_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    manifest = root / block["dependency_direction"]["crate_manifest"]
    if not manifest.exists():
        fail(f"crate manifest missing: {manifest.relative_to(root)}")
    return manifest.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def _enum_variant_names(enum_body: str) -> list[str]:
    """Names of the variants declared in an enum body.

    The body is the text between the enum braces (so it has no ``#[derive]`` or
    ``pub enum`` line). Each variant is the leading identifier of a non-comment
    line; doc-comment / attribute lines are skipped. Used to assert the enum
    declares EXACTLY the contracted variant set, so an unreviewed extra routing
    destination cannot be declared.
    """
    names: list[str] = []
    for line in enum_body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        match = re.match(r"([A-Za-z_]\w*)", stripped)
        if match:
            names.append(match.group(1))
    return names


def check_route_enum(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["route_enum"]
    try:
        body = _enum_body(exec_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    expected = list(spec["variants"])
    declared = _enum_variant_names(body)
    missing = [v for v in expected if v not in declared]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    # Reject EXTRA destinations. A subset check ("required variants present")
    # would let an unreviewed third route — e.g. one that submits straight to
    # the broker — be declared and escape the dispatch-arm inspection below.
    # Every routing destination must be enumerated in the contract.
    extra = [v for v in declared if v not in expected]
    if extra:
        fail(
            f"{spec['enum']} declares unexpected destination(s): {', '.join(extra)} — every "
            "routing destination must be enumerated in the order_routing_contract and inspected; "
            "a non-live order must never reach an unreviewed broker-bound route (AC-10)"
        )
    return (
        f"atp-execution declares the source-neutral {spec['enum']} with EXACTLY "
        f"{len(expected)} destinations ({', '.join(expected)}) — no unreviewed extra route"
    )


def check_simulated_receipt(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["simulated_receipt"]
    struct = spec["struct"]
    try:
        body = _struct_body(exec_src, struct)
    except AssertionError as error:
        fail(str(error))
    field = spec["field"]
    if not re.search(rf"\b{re.escape(field)}\s*:", body):
        fail(f"{struct} is missing the `{field}` field")
    forbidden = spec["forbidden_field"]
    if re.search(rf"\b{re.escape(forbidden)}\b", body):
        fail(
            f"{struct} carries `{forbidden}` — a simulated order must NOT mint a broker "
            "order id; SimulatedOrderReceipt must be a distinct type from OrderReceipt "
            "(AC-10: paper orders never create IB orders)"
        )
    return (
        f"atp-execution declares {struct} with `{field}` and NOT `{forbidden}` "
        "(a simulated order never mints a broker order id — distinct from OrderReceipt)"
    )


def check_routing_receipt(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["routing_receipt"]
    try:
        body = _enum_body(exec_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-execution declares {spec['enum']} with "
        f"{len(spec['variants'])} outcomes ({', '.join(spec['variants'])})"
    )


def check_simulation_port(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["simulation_port"]
    trait = spec["trait"]
    try:
        body = _trait_body(exec_src, trait)
    except AssertionError as error:
        fail(str(error))
    method = spec["method"]
    if not re.search(rf"\bfn\s+{re.escape(method)}\s*\(", body):
        fail(f"{trait} is missing the `{method}` method")
    receipt = spec["receipt_type"]
    if receipt not in body:
        fail(
            f"{trait}::{method} must return a `{receipt}` — the port yields a simulation "
            "acknowledgement, not a broker receipt"
        )
    return (
        f"atp-execution declares the {trait} port ({method} -> {receipt}); the simulation "
        "destination is reached only through it (atp-execution names no simulation type)"
    )


def check_route_destination(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["destination_decision"]
    try:
        body = _fn_block(exec_src, spec["method"])
    except AssertionError as error:
        fail(str(error))

    authority_token = spec["authority_call"] + "("
    if authority_token not in body:
        fail(
            f"{spec['method']} does not call `{authority_token}` — the routing destination "
            "must be derived from the engine-owned SRS-EXE-001 authority, not a new source "
            "of truth (so the live/paper split cannot drift)"
        )

    authority_enum = spec["authority_enum"]
    live_auth = f"{authority_enum}::{spec['live_authority_variant']}"
    sim_auth = f"{authority_enum}::{spec['simulation_authority_variant']}"
    for token in (live_auth, sim_auth):
        if token not in body:
            fail(f"{spec['method']} is missing the `{token}` branch")

    route_enum = spec["route_enum"]
    live_route = f"{route_enum}::{spec['live_route_variant']}"
    sim_route = f"{route_enum}::{spec['simulation_route_variant']}"

    try:
        live_arm = _match_arm(body, live_auth)
        sim_arm = _match_arm(body, sim_auth)
    except AssertionError as error:
        fail(str(error))

    if live_route not in live_arm:
        fail(f"{spec['method']} {live_auth} arm must map to {live_route}")
    # THE safety invariant: a non-live (NotDesignated) strategy must route to
    # the simulation engine and MUST NOT be routable to the live broker.
    if sim_route not in sim_arm:
        fail(
            f"{spec['method']} {sim_auth} arm must map to {sim_route} — every non-live "
            "strategy routes to the internal simulation engine (SYS-2b/2e)"
        )
    if live_route in sim_arm:
        fail(
            f"{spec['method']} {sim_auth} arm maps a non-live strategy to {live_route} — "
            "a non-live strategy must NEVER route to the broker (AC-10: paper orders never "
            "create IB orders)"
        )
    return (
        f"atp-execution::{spec['method']} maps the engine-owned {authority_enum} authority "
        f"to a destination ({spec['live_authority_variant']} -> {live_route}; "
        f"{spec['simulation_authority_variant']} -> {sim_route}); a non-live strategy can "
        "never route to the broker (AC-10)"
    )


def check_dispatch_guard(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["dispatch_guard"]
    try:
        body = _fn_block(exec_src, spec["method"])
    except AssertionError as error:
        fail(str(error))

    destination_token = spec["destination_call"] + "("
    if destination_token not in body:
        fail(
            f"{spec['method']} does not branch on `{destination_token}` — the dispatch must "
            "route on the single routing-destination decision"
        )

    route_enum = spec["route_enum"]
    live_token = f"{route_enum}::{spec['live_variant']}"
    sim_token = f"{route_enum}::{spec['simulation_variant']}"
    for token in (live_token, sim_token):
        if token not in body:
            fail(f"{spec['method']} is missing the `{token}` branch")

    # Reject EXTRA dispatch arms. Inspecting only the two configured arms would
    # let an unreviewed third arm (e.g. `OrderRoute::DirectIbBypass =>
    # broker.submit_order(...)`) route a non-live order to the broker without
    # ever being inspected. Every dispatch arm must be a configured, inspected
    # destination (the OrderRoute enum is locked to the same set by
    # check_route_enum, so this also pins the match exhaustive over exactly it).
    declared_arms = set(re.findall(rf"{re.escape(route_enum)}::(\w+)\s*=>", body))
    expected_arms = {spec["live_variant"], spec["simulation_variant"]}
    extra_arms = declared_arms - expected_arms
    if extra_arms:
        fail(
            f"{spec['method']} dispatches to unexpected route arm(s): {', '.join(sorted(extra_arms))} "
            "— every dispatch arm must be a configured, inspected destination; an extra arm could "
            "route a non-live order to the broker uninspected (AC-10)"
        )

    try:
        live_arm = _match_arm(body, live_token)
        sim_arm = _match_arm(body, sim_token)
    except AssertionError as error:
        fail(str(error))

    # LiveBrokerage arm: delegates to route_order, never touches the simulation port.
    delegate = spec["live_delegate_call"] + "("
    if delegate not in live_arm:
        fail(
            f"{spec['method']} {live_token} arm must delegate via `{delegate}` — the "
            "designated strategy proceeds through the SRS-EXE-001 authority gate and the "
            "ERR-1/2/3 connectivity/freshness safeguards"
        )
    for forbidden in spec["forbidden_in_live_arm"]:
        if (forbidden + "(") in live_arm:
            fail(
                f"{spec['method']} {live_token} arm calls `{forbidden}(` — the live dispatch "
                "must not touch the simulation port"
            )

    # InternalSimulation arm: submits to the sim port, touches NO IB port.
    submit = spec["simulation_submit_call"] + "("
    if submit not in sim_arm:
        fail(
            f"{spec['method']} {sim_token} arm must submit via `{submit}` — a non-live order "
            "routes to the internal simulation engine"
        )
    for forbidden in spec["forbidden_in_simulation_arm"]:
        if (forbidden + "(") in sim_arm:
            fail(
                f"{spec['method']} {sim_token} arm calls `{forbidden}(` — a non-live dispatch "
                "must reach NO IB-order-creating path (no route_order, no broker submit_order) "
                "so a paper order never creates an IB order (AC-10)"
            )

    return (
        f"atp-execution::{spec['method']} routes on `{spec['destination_call']}` over EXACTLY "
        f"{len(expected_arms)} arms (no unreviewed extra arm): the {live_token} arm delegates "
        f"to `{spec['live_delegate_call']}` (no simulation port) and the {sim_token} arm submits "
        f"via `{spec['simulation_submit_call']}` reaching none of "
        f"{len(spec['forbidden_in_simulation_arm'])} IB-order-creating paths (AC-10; read-only "
        "freshness/connectivity gates intentionally allowed for the SRS-MD-004 stale gate)"
    )


def check_dependency_direction(config: dict, manifest_src: str) -> str:
    spec = contract_block(config)["dependency_direction"]
    forbidden = spec["forbidden_dependency"]
    # Inspect the [dependencies] section only (a [dev-dependencies] use is a
    # test-time dependency and does not invert the runtime dependency direction).
    match = re.search(r"(?m)^\[dependencies\]\s*$", manifest_src)
    if match is None:
        fail("atp-execution Cargo.toml has no [dependencies] section")
    start = match.end()
    next_section = re.search(r"(?m)^\[", manifest_src[start:])
    section = (
        manifest_src[start : start + next_section.start()] if next_section else manifest_src[start:]
    )
    if re.search(rf"(?m)^\s*{re.escape(forbidden)}\b", section):
        fail(
            f"atp-execution depends on `{forbidden}` — the execution layer must not depend on "
            "the simulation engine (SRS-ARCH-002 one-way dependency direction); the simulation "
            "destination is reached only through the InternalSimulationSubmit port"
        )
    return (
        f"atp-execution does NOT depend on `{forbidden}` (SRS-ARCH-002 dependency direction "
        "preserved; simulation reached only through the port)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["execution_crate"]["crate"]
    integration = block["integration_test"]
    lib_filter = block["lib_test_filter"]
    cargo = shutil.which("cargo")
    if cargo is None:
        # FAIL CLOSED, do not skip-and-PASS. A static regex over the routing
        # arms can be fooled; the AUTHORITATIVE proof that a non-live order
        # never reaches IB is the executable Rust test (the order_routing lib
        # tests + the srs_exe_002 integration test route a paper order to a
        # panic-on-touch broker). For this safety gate, refusing to certify
        # without that proof is the correct behavior.
        fail(
            "cargo is not on PATH: the executable proof that a non-live order never reaches "
            "IB cannot run. This safety gate FAILS CLOSED rather than reporting PASS on the "
            "static regex alone. Install the Rust toolchain."
        )
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", lib_filter, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib {lib_filter} failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib {lib_filter} + --test {integration}: PASS "
        "(non-live orders route to the simulation engine and never reach a panic-on-touch broker)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


def _run_static(config: dict, exec_src: str, manifest_src: str) -> list[str]:
    return [
        check_route_enum(config, exec_src),
        check_simulated_receipt(config, exec_src),
        check_routing_receipt(config, exec_src),
        check_simulation_port(config, exec_src),
        check_route_destination(config, exec_src),
        check_dispatch_guard(config, exec_src),
        check_dependency_direction(config, manifest_src),
    ]


def run_checks() -> list[str]:
    config = load_config()
    exec_src = execution_source(config)
    manifest_src = manifest_source(config)
    evidence = _run_static(config, exec_src, manifest_src)
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_order_routing_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    exec_src = execution_source(config, root)
    manifest_src = manifest_source(config, root)
    return _run_static(config, exec_src, manifest_src)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-EXE-002 SDK-surface contract evidence")
    parser.parse_args(argv)

    try:
        config = load_config()
        evidence = run_checks()
    except OrderRoutingCheckError as error:
        print(f"SRS-EXE-002 FAIL: {error}", file=sys.stderr)
        return 1

    # Scope honestly: this is the routing-authority / SDK-surface half (the
    # source-neutral OrderRoute decision + the InternalSimulationSubmit port).
    # It does NOT wire the real simulation engine, run the Python strategy
    # runtime end to end, or exercise the operator IB-paper-account workflow —
    # all deferred. This is NOT a full SRS-EXE-002 requirement pass.
    print("SRS-EXE-002 SDK-SURFACE PASS (contract evidence only; not a full requirement pass)")
    for item in evidence:
        print(f"- {item}")
    print("Deferred end-to-end evidence (SRS-EXE-002 stays passes:false until these land):")
    for owner in contract_block(config).get("deferred", []):
        print(f"  * {owner}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
