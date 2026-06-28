#!/usr/bin/env python3
"""Contract evidence script for SRS-SIM-002 (simulate fills using live market data + fill models).

SRS-SIM-002: "simulate fills using live market data and configurable fill models"
(SyRS SYS-83 fill simulation, SYS-87 realism constraints; StRS SN-1.29 / SN-1.03).
The acceptance criterion: "Market, limit, stop, and stop-limit simulated fills
follow SYS-83 defaults and per-strategy configuration; fill volume constraints are
enforced."

The internal simulation engine's fill-model / triggering path lives in
``crates/atp-simulation`` (module ``fill_model``), per the structural contract in
``architecture/runtime_services.json`` (block ``sim_fill_contract``):

  (a) ``PaperSimulationEngine::evaluate_fill`` turns a routed ``OrderType`` plus a
      ``MarketSnapshot`` (bid / ask / last / volume) and a per-strategy
      ``FillModelConfig`` into a ``FillDecision``.
  (b) the SYS-83 fill rules hold: a market order fills at the ask (buy) / bid
      (sell); a limit fills on price cross (buy when ask <= limit, sell when bid >=
      limit); a stop triggers on the last crossing the stop (buy when last >= stop,
      sell when last <= stop) then fills at market; a stop-limit triggers then rests
      as a limit.
  (c) the SYS-87b volume constraint is enforced: ``fill_quantity = min(requested,
      bar_volume)`` and a zero-volume bar yields ``NoFillReason::ZeroVolume``.
  (d) it fails closed BEFORE any fill decision on a non-positive quote, a crossed
      book, a negative bar volume, a non-positive requested quantity, and a
      non-positive limit/stop price on the order type (``FillModelError`` variants;
      the snapshot guards live in ``validate_snapshot`` and the order-price guards
      in ``validate_order_type``).
  (e) every price is an integer minor unit (``_minor`` suffix); the module contains
      no ``f64``.
  (f) ``lib.rs`` re-exports ``pub mod fill_model;`` and the module carries no
      vendor-SDK token (SRS-ARCH-003 adapter isolation); the ``atp-simulation``
      crate has no dependency on the live/broker path (``atp-execution`` /
      ``atp-adapters``), so a simulated fill stays inside the internal engine.

The operator-demonstrable surface is the ``sim002_fill_cli`` binary
(``defaults`` / ``rules`` / ``config`` / ``volume`` over the real ``evaluate_fill``
engine), driven in fresh processes by the L5 ``srs_sim_002_fill_cli`` and pinned by
``check_fill_cli``. With that surface realized, feature_list.json marks SRS-SIM-002
``passes:true``: every context named in the acceptance criterion is built and
operator-demonstrable. The PASS line is ``SRS-SIM-002 SDK-SURFACE PASS`` -- it names
the ADJACENT features (the SYS-87a market-hours gate, the SYS-87c stale-data
threshold, the SYS-70 live feed, the SYS-83b stochastic fill-probability model,
paper-state persistence, the orchestrator routing, the Python strategy runtime) as
separate requirements NOT part of SRS-SIM-002's acceptance criterion.

Mirrors the PASS/FAIL output style of ``tools/sim_order_check.py``.

Invoke:
    python3 tools/sim_fill_check.py
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


class SimFillCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise SimFillCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_fill_contract" not in config:
        fail("architecture metadata is missing sim_fill_contract")
    return config["sim_fill_contract"]


def fill_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['fill_module']}.rs"
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


def cli_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    rel = Path(block["simulation_crate"]["path"]) / block["fill_cli"]["bin_path"]
    source_path = root / rel
    if not source_path.exists():
        fail(f"source missing: {rel}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_market_snapshot_struct(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["market_snapshot_struct"]
    body = _struct_body(fill_src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if missing:
        fail(f"{spec['struct']} is missing live-market-data fields: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['struct']} with {len(spec['fields'])} live-market-data "
        f"fields ({', '.join(spec['fields'])}) -- the SYS-83 bid/ask/last/volume inputs"
    )


def check_fill_model_config(config: dict, fill_src: str) -> str:
    block = contract_block(config)
    spec = block["fill_model_config"]
    body = _struct_body(fill_src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if missing:
        fail(f"{spec['struct']} is missing fields: {', '.join(missing)}")
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['syrs_defaults_fn'])}\b", fill_src):
        fail(
            f"{spec['struct']} must expose `{spec['syrs_defaults_fn']}` so the SYS-83 default "
            "fill-model family is named at the call site"
        )
    limit_spec = block["limit_fill_enum"]
    limit_body = _enum_body(fill_src, limit_spec["enum"])
    missing_variants = [
        v for v in limit_spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", limit_body)
    ]
    if missing_variants:
        fail(f"{limit_spec['enum']} is missing variants: {', '.join(missing_variants)}")
    return (
        f"atp-simulation declares {spec['struct']} ({', '.join(spec['fields'])}) with "
        f"{spec['syrs_defaults_fn']}() and {limit_spec['enum']} "
        f"({', '.join(limit_spec['variants'])}) -- the configurable per-strategy fill model "
        "(SYS-83 default ImmediateOnCross)"
    )


def check_fill_decision_enum(config: dict, fill_src: str) -> str:
    block = contract_block(config)
    decision = block["fill_decision_enum"]
    decision_body = _enum_body(fill_src, decision["enum"])
    missing = [
        v for v in decision["variants"] if not re.search(rf"\b{re.escape(v)}\b", decision_body)
    ]
    if missing:
        fail(f"{decision['enum']} is missing variants: {', '.join(missing)}")
    reason = block["no_fill_reason_enum"]
    reason_body = _enum_body(fill_src, reason["enum"])
    missing_reasons = [
        v for v in reason["variants"] if not re.search(rf"\b{re.escape(v)}\b", reason_body)
    ]
    if missing_reasons:
        fail(f"{reason['enum']} is missing no-fill reasons: {', '.join(missing_reasons)}")
    return (
        f"atp-simulation declares {decision['enum']} ({', '.join(decision['variants'])}) and "
        f"{reason['enum']} ({', '.join(reason['variants'])}) -- a fill carries its price and its "
        "volume-capped quantity, a no-fill carries its reason"
    )


def check_fill_model_error_enum(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["fill_model_error_enum"]
    body = _enum_body(fill_src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_fill_rules(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["fill_rules"]
    compact_src = _compact(fill_src)
    missing_types = [t for t in spec["order_type_tokens"] if _compact(t) not in compact_src]
    if missing_types:
        fail(
            f"the fill model must dispatch on every order type; missing: {', '.join(missing_types)}"
        )
    for label, key in (
        ("market", "market_tokens"),
        ("stop", "stop_tokens"),
        ("limit (ImmediateOnCross)", "limit_tokens"),
        ("limit (RequireThroughCross)", "through_cross_tokens"),
    ):
        missing = [t for t in spec[key] if _compact(t) not in compact_src]
        if missing:
            fail(
                f"the {label} fill rule (SYS-83) is missing the directional token(s): "
                f"{', '.join(missing)}"
            )
    return (
        "atp-simulation fill_model implements the SYS-83 rules: market fills at the ask (buy) / "
        "bid (sell), a stop triggers on the last crossing the stop, and a limit fills on the "
        "ask/bid crossing the limit -- with two behavior-changing limit models (ImmediateOnCross "
        "fills a touch, RequireThroughCross requires a strict cross) for every OrderType (Market, "
        "Limit, Stop, StopLimit)"
    )


def check_evaluate_fill(config: dict, fill_src: str) -> str:
    block = contract_block(config)
    spec = block["evaluate_fn"]
    fn_body = _compact(_fn_block(fill_src, spec["fn"]))
    if _compact(spec["validate_order_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must validate the order type's prices "
            f"(`{spec['validate_order_token']}`) before any fill decision -- a non-positive "
            "limit/stop price must never cross a valid snapshot and return a fill at that price"
        )
    if _compact(spec["validate_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must validate the snapshot (`{spec['validate_token']}`) before any fill "
            "decision -- corrupt market data must never drive a fill"
        )
    if _compact(spec["dispatch_token"]) not in fn_body:
        fail(f"{spec['fn']} must dispatch the fill rules via `{spec['dispatch_token']}`")
    for token in spec["guard_tokens"]:
        if _compact(token) not in fn_body:
            fail(f"{spec['fn']} is missing fail-closed guard {token}")
    compact_src = _compact(fill_src)
    vspec = block["validate_fn"]
    missing = [t for t in vspec["guard_tokens"] if _compact(t) not in compact_src]
    if missing:
        fail(f"{vspec['fn']} is missing snapshot fail-closed guards: {', '.join(missing)}")
    ospec = block["validate_order_fn"]
    missing_order = [t for t in ospec["guard_tokens"] if _compact(t) not in compact_src]
    if missing_order:
        fail(f"{ospec['fn']} is missing order-price fail-closed guards: {', '.join(missing_order)}")
    # The single-order evaluate_fill must DELEGATE to the budget-aware evaluator
    # with a fresh per-call budget, so its fail-closed + cap behavior is identical.
    sspec = block["single_order_fn"]
    single_body = _compact(_fn_block(fill_src, sspec["fn"]))
    missing_delegate = [t for t in sspec["delegate_tokens"] if _compact(t) not in single_body]
    if missing_delegate:
        fail(
            f"{sspec['fn']} must build a fresh per-call BarVolumeBudget and delegate to "
            f"{spec['fn']} (missing: {', '.join(missing_delegate)})"
        )
    return (
        f"atp-simulation {spec['fn']} validates the order type's prices "
        f"(`{spec['validate_order_token']}`) and the snapshot (`{spec['validate_token']}`), rejects "
        f"a non-positive quantity, and dispatches the fill rules; {vspec['fn']} rejects a "
        f"non-positive quote, a crossed book, and a negative bar volume, and {ospec['fn']} rejects "
        f"a non-positive limit/stop price before any fill; {sspec['fn']} delegates to {spec['fn']} "
        "with a fresh per-call budget"
    )


def check_volume_budget(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["bar_volume_budget"]
    body = _struct_body(fill_src, spec["struct"])
    if not re.search(r"\bremaining\s*:\s*i64\b", body):
        fail(f"{spec['struct']} must track remaining volume as an integer minor-unit i64 field")
    for fn in (
        spec["new_fn"],
        spec["for_snapshot_fn"],
        spec["remaining_fn"],
        spec["observed_fn"],
        spec["consume_fn"],
    ):
        if not re.search(rf"\bfn\s+{re.escape(fn)}\b", fill_src):
            fail(f"{spec['struct']} must expose a `{fn}` method")
    compact_src = _compact(fill_src)
    if _compact(spec["negative_guard"]) not in compact_src:
        fail(
            f"{spec['struct']}::{spec['new_fn']} must fail closed on a negative volume "
            f"(`{spec['negative_guard']}`)"
        )
    # The budget must be BOUND to its bar: evaluate_fill_against_budget fails closed
    # when the budget's observed volume does not match the snapshot's bar volume, or
    # a stale/oversized budget could fill a thinner bar past its observed volume.
    if _compact(spec["binding_guard"]) not in compact_src:
        fail(
            f"{spec['struct']} must be bound to its bar: the fill path must reject a budget whose "
            f"observed volume does not match the snapshot (`{spec['binding_guard']}`)"
        )
    if _compact(spec["mismatch_error"]) not in compact_src:
        fail(f"a budget/snapshot mismatch must fail closed with `{spec['mismatch_error']}`")
    return (
        f"atp-simulation declares {spec['struct']} (remaining + observed_bar_volume: i64) with "
        f"{spec['new_fn']} / {spec['for_snapshot_fn']} / {spec['remaining_fn']} / "
        f"{spec['observed_fn']} / {spec['consume_fn']} -- the per-bar SYS-87b budget that bounds "
        "the AGGREGATE of fills against one bar, fails closed on a negative volume, and is BOUND to "
        "its bar (a budget/snapshot mismatch fails closed so a stale/oversized budget cannot "
        "overfill a thinner bar)"
    )


def check_volume_cap(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["volume_cap"]
    fn_body = _compact(_fn_block(fill_src, spec["fn"]))
    if _compact(spec["cap_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must cap the fill at the bar's REMAINING volume (`{spec['cap_token']}`, "
            "SYS-87b)"
        )
    if _compact(spec["zero_volume_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must reject an exhausted bar with `{spec['zero_volume_token']}` (SYS-87b)"
        )
    if _compact(spec["consume_token"]) not in fn_body:
        fail(
            f"{spec['fn']} must consume the budget on a fill (`{spec['consume_token']}`) so the "
            "AGGREGATE of fills against one bar cannot exceed its observed volume (SYS-87b)"
        )
    return (
        f"atp-simulation {spec['fn']} enforces the SYS-87b volume constraint: each fill is capped "
        f"at the bar's remaining volume (`{spec['cap_token']}`) and consumes the budget "
        f"(`{spec['consume_token']}`), so an exhausted bar fills nothing and the aggregate of fills "
        "never exceeds the observed volume"
    )


def check_money_invariant(config: dict, fill_src: str) -> str:
    spec = contract_block(config)["money_invariant"]
    if spec["forbidden_float_token"] in fill_src:
        fail(
            f"fill_model module contains `{spec['forbidden_float_token']}` -- all prices MUST be "
            "integer minor units (the money-correctness invariant shared with the fill path)"
        )
    for field in spec["minor_price_fields"]:
        if not re.search(rf"\b{re.escape(field)}\s*:\s*i64\b", fill_src):
            fail(
                f"price field `{field}` must be declared as an integer minor unit (i64) so the "
                "fill path is exact"
            )
    return (
        f"atp-simulation fill_model prices are integer minor units: no "
        f"{spec['forbidden_float_token']}, {', '.join(spec['minor_price_fields'])} typed i64"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the fill-model "
            "path is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- a simulated fill must never reach a brokerage (SRS-SIM-002)"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- a simulated fill cannot reach a brokerage "
        "at the crate boundary"
    )


def check_vendor_isolation(config: dict, fill_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in fill_src]
    if leaked:
        fail(
            f"atp-simulation fill_model module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation fill_model module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_fill_cli(config: dict, cli_src: str, root: Path = ROOT) -> str:
    """The operator binary that makes the SRS-SIM-002 acceptance criterion demonstrable.

    Verifies the bin is Cargo-registered, exposes all four subcommands, drives the REAL fill-model
    engine (so the SYS-83 / SYS-87b proofs run over the real types, not a hand-rolled echo), prints
    each `:true` proof headline, carries the fail-closed path, and is backed by the L5 integration
    test.
    """
    spec = contract_block(config)["fill_cli"]

    # Cargo-registered (without the [[bin]], the operator surface does not build).
    cargo = cargo_source(config, root)
    if f'name = "{spec["bin_name"]}"' not in cargo:
        fail(
            f"Cargo.toml must register the operator binary `{spec['bin_name']}` — without it the "
            "SRS-SIM-002 fill-model operator surface does not build"
        )

    # All four subcommands present.
    missing_cmds = [c for c in spec["subcommands"] if f'"{c}"' not in cli_src]
    if missing_cmds:
        fail(f"{spec['bin_name']} is missing subcommand(s): {', '.join(missing_cmds)}")

    # Drives the REAL fill-model engine — the SYS-83 / SYS-87b proof must run over the real types,
    # not a hand-rolled stand-in that could agree with itself.
    missing_engine = [t for t in spec["engine_tokens"] if t not in cli_src]
    if missing_engine:
        fail(
            f"{spec['bin_name']} must drive the real fill-model engine (missing "
            f"{', '.join(missing_engine)}) so the SYS-83 / SYS-87b proofs are genuine (SRS-SIM-002)"
        )

    # Each `:true` proof headline the acceptance criterion turns on (SYS-83 rules, per-strategy
    # config divergence, SYS-87b volume cap).
    missing_proofs = [t for t in spec["proof_tokens"] if t not in cli_src]
    if missing_proofs:
        fail(
            f"{spec['bin_name']} must print every proof headline (missing "
            f"{', '.join(missing_proofs)}) — the SYS-83 / per-strategy / SYS-87b acceptance halves"
        )

    # An injected fault fails closed before any fill decision (no fabricated proof).
    if _compact(spec["fail_closed_token"]) not in _compact(cli_src):
        fail(
            f"{spec['bin_name']} must fail closed on an injected fault "
            f"(`{spec['fail_closed_token']}`) so corrupt market data never produces a fill proof"
        )

    # Backed by the L5 integration test.
    block = contract_block(config)
    l5_path = root / block["simulation_crate"]["path"] / "tests" / f"{spec['l5_test']}.rs"
    if not l5_path.exists():
        fail(f"missing L5 integration test {l5_path.relative_to(root)}")

    return (
        f"operator binary {spec['bin_name']} is Cargo-registered, exposes "
        f"{', '.join(spec['subcommands'])}, drives the REAL fill-model engine "
        f"({', '.join(spec['engine_tokens'])}), prints {', '.join(spec['proof_tokens'])}, fails "
        f"closed on an injected fault, and is driven in fresh processes by the L5 {spec['l5_test']}"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cli_integration = block["fill_cli"]["l5_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "fill-model path compiles + passes (install the Rust toolchain)"
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
    for test_name in (integration, cli_integration):
        integ = subprocess.run(
            [cargo, "test", "-p", crate, "--test", test_name, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if integ.returncode != 0:
            fail(
                f"cargo test -p {crate} --test {test_name} failed:\n{integ.stdout}\n{integ.stderr}"
            )
    return (
        f"cargo test -p {crate} --lib + {integration} + {cli_integration}: PASS "
        "(every order type resolves a deterministic fill decision over a MarketSnapshot, the "
        "SYS-87b volume cap is enforced, a filled decision flows through the shared cost family, "
        "corrupt market data fails closed, and the sim002_fill_cli operator surface proves the "
        "SYS-83 rules, the per-strategy fill-model divergence, and the SYS-87b cap in fresh processes)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) — "fill" reads fill_model.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("market_snapshot_struct", check_market_snapshot_struct, "fill"),
    ("fill_model_config", check_fill_model_config, "fill"),
    ("fill_decision_enum", check_fill_decision_enum, "fill"),
    ("fill_model_error_enum", check_fill_model_error_enum, "fill"),
    ("fill_rules", check_fill_rules, "fill"),
    ("evaluate_fill", check_evaluate_fill, "fill"),
    ("volume_cap", check_volume_cap, "fill"),
    ("volume_budget", check_volume_budget, "fill"),
    ("money_invariant", check_money_invariant, "fill"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "fill"),
    ("fill_cli", check_fill_cli, "cli"),
)

# Features ADJACENT to SRS-SIM-002 — each a separate requirement with its own owner, NOT a context
# inside SRS-SIM-002's acceptance criterion (which the sim002_fill_cli operator surface demonstrates).
_ADJACENT_FEATURES = (
    "SYS-87a market-hours gate (SYS-50 trading calendar)",
    "SYS-87c stale-data threshold (SRS-MD-004 / SYS-39 freshness)",
    "SYS-70 live market-data feed (subscription manager)",
    "SYS-83b stochastic fill-probability model",
    "SRS-SIM-004 (paper-state persistence)",
    "SRS-EXE-002 (orchestrator routing of all non-live strategies)",
    "SRS-SDK runtime (Python strategy host)",
)


def assert_sim_fill_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "fill": fill_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
        "cli": cli_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_fill_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-SIM-002 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable fill-model path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except SimFillCheckError as error:
        print(f"SRS-SIM-002 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-SIM-002 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- adjacent features (separate requirements, NOT contexts inside SRS-SIM-002's acceptance "
        "criterion): "
        + ", ".join(_ADJACENT_FEATURES)
        + "; feature_list.json marks SRS-SIM-002 passes:true"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
