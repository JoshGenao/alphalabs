#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-003 (shared simulation/backtest cost family).

SRS-BT-003: "use the same transaction-cost model family for internal simulation
and backtesting unless configured otherwise" (SyRS SYS-15e / SYS-83d; StRS
SN-1.03 / SN-1.29). The acceptance criterion: "A paper strategy and backtest
using identical cost configuration compute fills and commissions from the same
model family."

The configurable transaction-cost model *family* already lives in
``crates/atp-simulation`` (module ``cost``, SRS-BT-002) and the runnable backtest
engine (module ``backtest``) is its first consumer. This SDK-surface ships the
internal simulation engine's paper-fill path (module ``sim``) that consumes the
**same** family, per the structural contract in
``architecture/runtime_services.json`` (block ``sim_cost_contract``):

  (a) ``PaperSimulationEngine`` holds a ``cost_config: CostConfig`` that derives
      ``Default`` = ``CostConfig::default()`` — the IDENTICAL SyRS baseline the
      backtest engine defaults to (SYS-15e) — and exposes ``with_cost_config`` for
      a per-strategy override "unless explicitly configured otherwise".
  (b) ``simulate_fill`` computes the per-fill cost by calling the IDENTICAL shared
      entry point ``self.cost_config.cost_breakdown(...)`` the backtest engine
      calls, and ALWAYS SUBTRACTS the total (``checked_sub(total_cost_minor)``),
      so a cost can never fabricate cash.
  (c) ``PaperFill`` records the same commission/slippage/spread-impact
      decomposition the backtest ``Fill`` records, plus the signed
      ``cash_delta_minor``; the minimal ``PaperLedger`` accumulates cash,
      position, and commission paid.
  (d) the fill fails closed before any cost on an empty symbol, a non-positive
      price, a negative observed spread, and a misconfigured negative parameter
      (``SimError`` variants, incl. ``Cost(CostError)``).
  (e) ALL cost math is integer minor units (no ``f64``; ``i128::from``
      intermediates + ``i64::try_from`` -> ``SimError::Overflow``).
  (f) ``lib.rs`` re-exports both ``pub mod cost;`` and ``pub mod sim;`` and the
      sim module carries no vendor-SDK token (SRS-ARCH-003 adapter isolation).

The PASS line is ``SRS-BT-003 SDK-SURFACE PASS`` — it names the deferred owners
(the SYS-83 fill models + live-data fills, the full SYS-84 ledger, paper-state
persistence, the operator override surface, the Python strategy runtime) so the
partial-pass status (feature_list.json keeps ``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/backtest_cost_check.py``.

Invoke:
    python3 tools/sim_cost_check.py
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


class SimCostCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SimCostCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_cost_contract" not in config:
        fail("architecture metadata is missing sim_cost_contract")
    return config["sim_cost_contract"]


def _module_source(config: dict, module_key: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block[module_key]}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def sim_source(config: dict, root: Path = ROOT) -> str:
    return _module_source(config, "sim_module", root)


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_engine_struct(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["engine_struct"]
    body = _struct_body(sim_src, spec["struct"])
    if not re.search(
        rf"\b{re.escape(spec['cost_field'])}\s*:\s*{re.escape(spec['cost_field_type'])}\b", body
    ):
        fail(
            f"{spec['struct']} must hold a `{spec['cost_field']}: {spec['cost_field_type']}` so "
            "the simulation engine carries the shared cost family"
        )
    if _compact(spec["default_derive_token"]) not in _compact(sim_src):
        fail(
            f"{spec['struct']} must derive Default (`{spec['default_derive_token']}`) so its "
            "default cost family is provably CostConfig::default() (the same SyRS baseline the "
            "backtest engine defaults to, SYS-15e)"
        )
    for method in spec["methods"]:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", sim_src):
            fail(f"{spec['struct']} is missing method `{method}`")
    return (
        f"atp-simulation declares {spec['struct']} carrying the shared "
        f"{spec['cost_field']}: {spec['cost_field_type']}, deriving Default (= CostConfig::default(), "
        f"SYS-15e), with {len(spec['methods'])} methods ({', '.join(spec['methods'])})"
    )


def check_shared_entry_point(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["shared_entry_point"]
    if _compact(spec["use_cost_token"]) not in _compact(sim_src):
        fail(
            f"sim module must import the shared cost family (`{spec['use_cost_token']}`) — the "
            "internal simulation engine and the backtest engine share cost::CostConfig (SRS-BT-003)"
        )
    body = _compact(_fn_block(sim_src, spec["fn"]))
    if _compact(spec["breakdown_token"]) not in body:
        fail(
            f"{spec['fn']} must compute costs via the IDENTICAL shared entry point "
            f"`{spec['breakdown_token']}` the backtest engine calls (SRS-BT-003 / SYS-15e)"
        )
    if _compact(spec["subtract_cost_token"]) not in body:
        fail(
            f"{spec['fn']} must SUBTRACT the total cost (`{spec['subtract_cost_token']}`) so a "
            "simulated cost can never fabricate cash"
        )
    return (
        f"atp-simulation {spec['fn']} consumes the shared cost family — imports `use crate::cost::`, "
        f"computes the per-fill breakdown via the identical `cost_breakdown(...)` entry point the "
        f"backtest engine calls, and subtracts the total (`{spec['subtract_cost_token']}`)"
    )


def check_paper_fill_struct(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["paper_fill_struct"]
    body = _struct_body(sim_src, spec["struct"])
    missing = [
        f for f in spec["cost_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing cost-decomposition fields: {', '.join(missing)}")
    if not re.search(rf"\bpub\s+{re.escape(spec['cash_field'])}\s*:", body):
        fail(f"{spec['struct']} is missing the signed `{spec['cash_field']}`")
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['total_method'])}\b", sim_src):
        fail(f"{spec['struct']} is missing the overflow-checked `{spec['total_method']}` total")
    return (
        f"atp-simulation declares {spec['struct']} with the same {len(spec['cost_fields'])} cost "
        f"components ({', '.join(spec['cost_fields'])}) the backtest Fill carries, a signed "
        f"{spec['cash_field']}, and an overflow-checked {spec['total_method']}()"
    )


def check_paper_ledger_struct(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["paper_ledger_struct"]
    body = _struct_body(sim_src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if missing:
        fail(f"{spec['struct']} is missing fields: {', '.join(missing)}")
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['apply_method'])}\b", sim_src):
        fail(f"{spec['struct']} is missing the `{spec['apply_method']}` accumulator")
    return (
        f"atp-simulation declares the minimal virtual ledger {spec['struct']} "
        f"({', '.join(spec['fields'])}) with {spec['apply_method']}() — the SYS-84 seam that "
        "accumulates the shared family's commissions"
    )


def check_sim_error_enum(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["sim_error_enum"]
    body = _enum_body(sim_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_fail_closed(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["fail_closed"]
    body = _compact(_fn_block(sim_src, spec["fn"]))
    if _compact(spec["validate_token"]) not in body:
        fail(
            f"{spec['fn']} must validate the cost config (`{spec['validate_token']}`) before any "
            "fill — a negative parameter would otherwise fabricate cash"
        )
    missing = [t for t in spec["guard_tokens"] if _compact(t) not in body]
    if missing:
        fail(
            f"{spec['fn']} must fail closed before computing any cost: missing guards "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-simulation {spec['fn']} fails closed before any cost — validates the config "
        f"(`{spec['validate_token']}`) and rejects an empty symbol, a non-positive price, and a "
        "negative observed spread"
    )


def check_money_invariant(config: dict, sim_src: str) -> str:
    spec = contract_block(config)["money_invariant"]
    if spec["forbidden_float_token"] in sim_src:
        fail(
            f"sim module contains `{spec['forbidden_float_token']}` — all fill money math MUST use "
            "integer minor units (SRS-BT money-correctness invariant)"
        )
    compact = _compact(sim_src)
    for token_key in ("wide_intermediate_token", "narrow_token"):
        if _compact(spec[token_key]) not in compact:
            fail(
                f"sim money math must use `{spec[token_key]}` ({token_key}) so a fill notional "
                "cannot silently overflow"
            )
    if spec["checked_op"] not in sim_src:
        fail(f"sim money math must use overflow-checked `{spec['checked_op']}`")
    if not re.search(rf"\b{re.escape(spec['overflow_variant'])}\b", sim_src):
        fail(f"sim module must surface overflow via SimError::{spec['overflow_variant']}")
    return (
        f"atp-simulation sim money math is integer-only: no {spec['forbidden_float_token']}, "
        f"{spec['wide_intermediate_token']} intermediates, {spec['narrow_token']} narrowing + "
        f"{spec['checked_op']} -> SimError::{spec['overflow_variant']}"
    )


def check_shared_family(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["shared_family"]
    compact = _compact(lib_src)
    for token_key in ("lib_cost_reexport_token", "lib_sim_reexport_token"):
        if _compact(spec[token_key]) not in compact:
            fail(
                f"atp-simulation lib.rs must re-export `{spec[token_key]}` so the backtest engine "
                "and the internal simulation engine share the SAME cost family (SRS-BT-003)"
            )
    return (
        "atp-simulation lib.rs re-exports both `pub mod cost;` and `pub mod sim;` — the backtest "
        "engine and the internal simulation engine apply the SAME cost::CostConfig family through "
        "the identical cost_breakdown entry point (SRS-BT-003 / SYS-15e)"
    )


def check_vendor_isolation(config: dict, sim_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in sim_src]
    if leaked:
        fail(
            f"atp-simulation sim module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation sim module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
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
                "shared-cost simulation fill compiles + passes (install the Rust toolchain)"
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
        "(sim default cost family equals the backtest default, identical config -> identical fills "
        "and commissions in both engines, observed-spread path matches, a cost never fabricates "
        "cash, fail-closed branches verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) — "sim" reads sim.rs, "lib" reads lib.rs.
_STATIC_CHECKS = (
    ("engine_struct", check_engine_struct, "sim"),
    ("shared_entry_point", check_shared_entry_point, "sim"),
    ("paper_fill_struct", check_paper_fill_struct, "sim"),
    ("paper_ledger_struct", check_paper_ledger_struct, "sim"),
    ("sim_error_enum", check_sim_error_enum, "sim"),
    ("fail_closed", check_fail_closed, "sim"),
    ("money_invariant", check_money_invariant, "sim"),
    ("shared_family", check_shared_family, "lib"),
    ("vendor_isolation", check_vendor_isolation, "sim"),
)

_DEFERRED_OWNERS = (
    "SRS-SIM-002 (SYS-83 fill models + live-market-data fills)",
    "SRS-SIM-003 (full SYS-84 virtual ledger)",
    "SRS-SIM-004 (paper-state persistence)",
    "SRS-API-001 / SRS-UI (operator per-strategy cost-override surface)",
    "SRS-BT-001-runtime (real data + Python strategy host)",
)


def assert_sim_cost_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "sim": sim_source(config, root),
        "lib": lib_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_cost_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-003 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable sim fill must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except SimCostCheckError as error:
        print(f"SRS-BT-003 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-003 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-003 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
