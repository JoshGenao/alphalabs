#!/usr/bin/env python3
"""Contract evidence script for SRS-SIM-001 (simulate paper orders locally, no broker routing).

SRS-SIM-001: "simulate paper strategy orders locally without routing to any
brokerage" (SyRS SYS-82 local paper order execution, SYS-3 order types, SYS-4
multi-leg composite; StRS SN-1.29 / SN-1.08 / SN-1.24). The acceptance criterion:
"Market, limit, stop, stop-limit, equity, option, and multi-leg orders are
processed by the simulation engine and create no IB API order calls."

The internal simulation engine's paper order-intake path lives in
``crates/atp-simulation`` (module ``paper_order``), per the structural contract in
``architecture/runtime_services.json`` (block ``sim_order_contract``):

  (a) ``PaperSimulationEngine::accept_order`` accepts a ``PaperOrderRequest`` (a
      single ``OrderLeg`` or a ``MultiLeg`` composite) for every ``OrderType``
      (Market / Limit / Stop / StopLimit) and ``AssetClass`` (Equity / Option).
  (b) it returns an ``OrderRouting`` whose ONLY variant is ``InternalSimulation``
      -- there is structurally no Broker/Ib variant to construct, so a paper order
      can never reach a brokerage. "Creates no IB API order calls" is a
      COMPILE-TIME guarantee, not a runtime check.
  (c) the ``atp-simulation`` crate has no dependency on the live/broker path
      (``atp-execution`` / ``atp-adapters``), reinforcing the invariant at the
      crate boundary.
  (d) a ``MultiLeg`` request routes as one composite transaction
      (``composite: true``, SYS-4) so its legs fill atomically.
  (e) intake fails closed before routing on an empty symbol, a non-positive
      quantity, a non-positive limit/stop price, and an empty multi-leg request
      (``OrderError`` variants; the leg guards live in ``validate_leg``).
  (f) every order price is an integer minor unit (``limit_price_minor`` /
      ``stop_price_minor``); the module contains no ``f64``.
  (g) ``lib.rs`` re-exports ``pub mod paper_order;`` and the module carries no
      vendor-SDK token (SRS-ARCH-003 adapter isolation).

The PASS line is ``SRS-SIM-001 SDK-SURFACE PASS`` -- it names the deferred owners
(the SYS-83 fill triggering + live-data fills, the full SYS-84 ledger, paper-state
persistence, the orchestrator routing of all non-live strategies, the Python
strategy runtime) so the partial-pass status (feature_list.json keeps
``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/sim_cost_check.py``.

Invoke:
    python3 tools/sim_order_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class SimOrderCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SimOrderCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_order_contract" not in config:
        fail("architecture metadata is missing sim_order_contract")
    return config["sim_order_contract"]


def order_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['order_module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


# The order-type vocabulary (AssetClass / Side / OrderType) was HOISTED to the
# shared leaf crate atp-types under SRS-EXE-003; this paper path CONSUMES that
# single shared definition by re-export (the future live intake will consume the
# same one -- deferred). paper_order now RE-EXPORTS it. atp-types names the side
# enum `OrderSide`; paper_order re-exports it as `Side`, so the contract's
# paper-facing name maps to the authority name here.
_AUTHORITY_ENUM_NAME = {"Side": "OrderSide"}


def _atp_types_order_source(root: Path = ROOT) -> str:
    """Read the atp-types order-type authority: lib.rs declares the crate-root
    AssetClass; order_type.rs declares OrderType / OrderSide (SRS-EXE-003)."""
    base = root / "crates" / "atp-types" / "src"
    sources = [base / "lib.rs", base / "order_type.rs"]
    for source_path in sources:
        if not source_path.exists():
            fail(f"atp-types order-type source missing: {source_path.relative_to(root)}")
    return "\n".join(source_path.read_text(encoding="utf-8") for source_path in sources)


def check_order_types(config: dict, order_src: str, atp_src: str | None = None) -> str:
    block = contract_block(config)
    if atp_src is None:
        atp_src = _atp_types_order_source()
    summaries = []
    for key in ("asset_class_enum", "side_enum", "order_type_enum"):
        spec = block[key]
        reexport_name = spec["enum"]
        authority_name = _AUTHORITY_ENUM_NAME.get(reexport_name, reexport_name)
        # (a) the vocabulary now lives in the shared atp-types authority...
        body = _enum_body(atp_src, authority_name)
        missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
        if missing:
            fail(f"atp_types::{authority_name} is missing variants: {', '.join(missing)}")
        # (b) ...and paper_order RE-EXPORTS it (does not redefine it), so the paper
        #     path consumes the single shared definition (live consumption deferred).
        if not re.search(
            rf"\bpub\s+use\s+atp_types::[^;]*\b{re.escape(authority_name)}\b", order_src
        ):
            fail(
                f"paper_order must `pub use` atp_types' {authority_name} (re-export the shared "
                f"SRS-EXE-003 order-type authority as {reexport_name}), not define its own copy"
            )
        summaries.append(f"{reexport_name} ({', '.join(spec['variants'])})")
    return (
        "atp-simulation paper_order re-exports the shared atp-types order-type authority "
        "(SRS-EXE-003): " + "; ".join(summaries)
    )


def check_order_leg_struct(config: dict, order_src: str) -> str:
    spec = contract_block(config)["order_leg_struct"]
    body = _struct_body(order_src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if missing:
        fail(f"{spec['struct']} is missing fields: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['struct']} with {len(spec['fields'])} fields "
        f"({', '.join(spec['fields'])})"
    )


def check_order_request_enum(config: dict, order_src: str) -> str:
    spec = contract_block(config)["order_request_enum"]
    body = _enum_body(order_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} ({', '.join(spec['variants'])}) -- single-leg and "
        "multi-leg composite paper orders"
    )


def check_routing_internal_only(config: dict, order_src: str) -> str:
    spec = contract_block(config)["routing"]
    body = _enum_body(order_src, spec["enum"])
    for variant in spec["allowed_variants"]:
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(f"{spec['enum']} must declare the `{variant}` variant")
    leaked = [
        t for t in spec["forbidden_variant_tokens"] if re.search(rf"\b{re.escape(t)}\b", body)
    ]
    if leaked:
        fail(
            f"{spec['enum']} must expose NO brokerage routing variant, found: {', '.join(leaked)} "
            "-- a paper order must never route to a broker (SRS-SIM-001 'no IB API order calls')"
        )
    fn_body = _compact(_fn_block(order_src, spec["intake_fn"]))
    if _compact(spec["internal_route_token"]) not in fn_body:
        fail(
            f"{spec['intake_fn']} must route accepted orders to `{spec['internal_route_token']}` "
            "(the only routing variant)"
        )
    if _compact(spec["composite_token"]) not in fn_body:
        fail(
            f"{spec['intake_fn']} must mark a multi-leg order as one composite transaction "
            f"(`{spec['composite_token']}`, SYS-4)"
        )
    return (
        f"atp-simulation {spec['enum']} has exactly the internal-only variant(s) "
        f"({', '.join(spec['allowed_variants'])}) and NO broker variant "
        f"({', '.join(spec['forbidden_variant_tokens'])}); {spec['intake_fn']} routes to "
        f"{spec['internal_route_token']} (composite for multi-leg) -- 'no IB API order calls' is a "
        "compile-time guarantee"
    )


def check_order_error_enum(config: dict, order_src: str) -> str:
    spec = contract_block(config)["order_error_enum"]
    body = _enum_body(order_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_fail_closed(config: dict, order_src: str) -> str:
    block = contract_block(config)
    spec = block["fail_closed"]
    fn_body = _compact(_fn_block(order_src, spec["fn"]))
    if _compact(spec["validate_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must validate each leg (`{spec['validate_token']}`) before routing -- a "
            "malformed order must never reach the fill path"
        )
    for token in spec["guard_tokens"]:
        if _compact(token) not in fn_body:
            fail(f"{spec['fn']} is missing fail-closed guard {token}")
    vspec = block["validate_fn"]
    compact_src = _compact(order_src)
    missing = [t for t in vspec["guard_tokens"] if _compact(t) not in compact_src]
    if missing:
        fail(f"{vspec['fn']} is missing leg fail-closed guards: {', '.join(missing)}")
    return (
        f"atp-simulation {spec['fn']} validates each leg (`{spec['validate_token']}`) and rejects "
        f"an empty multi-leg request before routing; {vspec['fn']} rejects an empty symbol, a "
        "non-positive quantity, and a non-positive limit/stop price"
    )


def check_money_invariant(config: dict, order_src: str, atp_src: str | None = None) -> str:
    spec = contract_block(config)["money_invariant"]
    if spec["forbidden_float_token"] in order_src:
        fail(
            f"paper_order module contains `{spec['forbidden_float_token']}` -- all order prices MUST "
            "be integer minor units (the money-correctness invariant shared with the fill path)"
        )
    # The order-type price fields were HOISTED to the shared atp-types authority
    # (SRS-EXE-003); verify the i64 minor-unit declaration where they now live.
    if atp_src is None:
        atp_src = _atp_types_order_source()
    for field in spec["minor_price_fields"]:
        if not re.search(rf"\b{re.escape(field)}\s*:\s*i64\b", atp_src):
            fail(
                f"order price field `{field}` must be declared as an integer minor unit (i64) so "
                "the downstream fill path is exact"
            )
    return (
        f"atp-simulation paper_order contains no {spec['forbidden_float_token']}; the shared "
        f"atp-types order-type authority types {', '.join(spec['minor_price_fields'])} as "
        "integer minor units (i64)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the paper "
            "order-intake path is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- the simulation engine must never reach a brokerage "
            "(SRS-SIM-001)"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- paper orders cannot reach a brokerage at "
        "the crate boundary"
    )


def check_vendor_isolation(config: dict, order_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in order_src]
    if leaked:
        fail(
            f"atp-simulation paper_order module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation paper_order module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "paper order-intake path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib + {integration}: PASS "
        "(every order type / asset class / single + multi-leg request routes to the internal "
        "simulation engine, the routing enum exposes no broker variant, and intake fails closed on "
        "bad input)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) — "order" reads paper_order.rs, "lib" reads
# lib.rs, "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("order_types", check_order_types, "order"),
    ("order_leg_struct", check_order_leg_struct, "order"),
    ("order_request_enum", check_order_request_enum, "order"),
    ("routing_internal_only", check_routing_internal_only, "order"),
    ("order_error_enum", check_order_error_enum, "order"),
    ("fail_closed", check_fail_closed, "order"),
    ("money_invariant", check_money_invariant, "order"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "order"),
)

_DEFERRED_OWNERS = (
    "SRS-SIM-002 (SYS-83 fill triggering + live-market-data fills)",
    "SRS-SIM-003 (full SYS-84 virtual ledger)",
    "SRS-SIM-004 (paper-state persistence)",
    "SRS-EXE-002 (orchestrator routing of all non-live strategies)",
    "SRS-SDK runtime (Python strategy host)",
)


def assert_sim_order_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "order": order_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_order_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-SIM-001 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable intake path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except SimOrderCheckError as error:
        print(f"SRS-SIM-001 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-SIM-001 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-SIM-001 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
