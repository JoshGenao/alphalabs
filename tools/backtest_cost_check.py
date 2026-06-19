#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-002 (configurable backtest cost models).

SRS-BT-002: "apply configurable commission, slippage, and spread-impact models to
backtests" (SyRS SYS-15a / SYS-15b / SYS-15c / SYS-15d; StRS SN-1.03). The
acceptance criterion: "Defaults match SyRS values; a backtest run can override
commission, slippage, and spread-impact models without changing strategy code."

This SDK-surface ships the *configurable transaction-cost model family* in
``crates/atp-simulation`` (module ``cost``) and verifies that the runnable
backtest engine (module ``backtest``) APPLIES it, per the structural contract in
``architecture/runtime_services.json`` (block ``backtest_cost_contract``):

  (a) ``CommissionModel`` / ``SlippageModel`` / ``SpreadImpactModel`` enumerate the
      configurable models, each with a SyRS default (IbTiered / NotionalBps /
      ObservedOrFallbackBps).
  (b) the SyRS default VALUES are encoded as named constants with exactly the
      published numbers (0.05% slippage, 0.10% fallback spread, the IB tiered
      $0.0035/share + $0.35 floor + 1% cap) — so the default is provably the SyRS
      baseline.
  (c) ``CostConfig`` bundles the three models, derives ``Default`` (the SyRS
      family), and exposes ``zero`` / ``validate`` / ``cost_breakdown``.
  (d) the engine applies the per-run ``BacktestRequest.cost_config``: it validates
      the config, computes a ``cost_breakdown`` per fill, SUBTRACTS the total from
      cash (a cost can never fabricate cash), records the cost decomposition on
      ``Fill``, and fails closed on a corrupt negative observed spread.
  (e) ALL cost math is integer minor units (no ``f64``; i128 intermediates +
      round-half-up + ``i64::try_from`` -> ``CostError::Overflow``), every cost is
      non-negative, and a negative configured parameter fails closed
      (``CostError::NegativeParameter``).
  (f) the cost module carries no vendor-SDK token and is the SAME family the
      internal simulation engine shares for paper fills (SRS-BT-003, now closed).

The operator override surface the AC names is now realized for the CLI: the
``bt002_cost_cli`` operator binary (``defaults`` + ``run``) prints the SyRS
constants, proves ``CostConfig::default() == syrs_defaults()``, and applies a
per-run CostConfig built from ``--commission`` / ``--slippage`` / ``--spread``
flags to the SAME fixture strategy (override without strategy changes, SYS-15d),
pinned by the ``srs_bt_002_cost_cli`` integration test (feature_list.json is now
``passes:true``). The PASS line still names the genuinely deferred owners (the
REST + dashboard half of the override surface, the IB monthly-volume tier ladder,
the real data + Python strategy runtime). The SRS-BT-003 sim-engine sharing is no
longer deferred — it is closed (passes:true; see sim_cost_contract).

Mirrors the PASS/FAIL output style of ``tools/backtest_check.py``.

Invoke:
    python3 tools/backtest_cost_check.py
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


class BacktestCostCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise BacktestCostCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "backtest_cost_contract" not in config:
        fail("architecture metadata is missing backtest_cost_contract")
    return config["backtest_cost_contract"]


def _module_source(config: dict, module_key: str, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block[module_key]}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cost_source(config: dict, root: Path = ROOT) -> str:
    return _module_source(config, "cost_module", root)


def backtest_source(config: dict, root: Path = ROOT) -> str:
    return _module_source(config, "backtest_module", root)


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / block["cost_cli"]["cargo_toml"]
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def _check_enum_variants(spec: dict, src: str) -> None:
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")


def check_commission_model(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["commission_model"]
    _check_enum_variants(spec, cost_src)
    if _compact(spec["default_token"]) not in _compact(cost_src):
        fail(
            f"{spec['enum']} default must be `{spec['default_variant']}` "
            f"(the published IB tiered schedule, SYS-15a) via `{spec['default_token']}`"
        )
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} models "
        f"({', '.join(spec['variants'])}); default is {spec['default_variant']} — the published IB "
        "tiered commission schedule (SYS-15a)"
    )


def check_slippage_model(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["slippage_model"]
    _check_enum_variants(spec, cost_src)
    if _compact(spec["default_token"]) not in _compact(cost_src):
        fail(
            f"{spec['enum']} default must be NotionalBps at DEFAULT_SLIPPAGE_BPS "
            f"(SYS-15b 0.05%) via `{spec['default_token']}`"
        )
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} models "
        f"({', '.join(spec['variants'])}); default is NotionalBps at DEFAULT_SLIPPAGE_BPS "
        "(0.05% of trade notional, SYS-15b)"
    )


def check_spread_impact_model(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["spread_impact_model"]
    _check_enum_variants(spec, cost_src)
    if _compact(spec["default_token"]) not in _compact(cost_src):
        fail(
            f"{spec['enum']} default must be ObservedOrFallbackBps at DEFAULT_SPREAD_FALLBACK_BPS "
            f"(SYS-15c) via `{spec['default_token']}`"
        )
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} models "
        f"({', '.join(spec['variants'])}); default is ObservedOrFallbackBps — half the observed "
        "spread per share, else DEFAULT_SPREAD_FALLBACK_BPS of notional (SYS-15c)"
    )


def check_syrs_default_constants(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["syrs_default_constants"]
    for name, value in spec.items():
        if not re.search(rf"\bconst\s+{re.escape(name)}\s*:\s*\w+\s*=\s*{value}\b", cost_src):
            fail(
                f"cost module constant `{name}` must equal the SyRS default value {value} "
                "(the default cost models must match the published SyRS values, SRS-BT-002 AC)"
            )
    return (
        "atp-simulation cost defaults match the SyRS values exactly: "
        f"DEFAULT_SLIPPAGE_BPS={spec['DEFAULT_SLIPPAGE_BPS']} (0.05%), "
        f"DEFAULT_SPREAD_FALLBACK_BPS={spec['DEFAULT_SPREAD_FALLBACK_BPS']} (0.10%), IB tiered "
        f"{spec['IB_TIERED_RATE_CENTIMINOR_PER_SHARE']} centi-minor/share "
        f"($0.0035), {spec['IB_TIERED_MIN_PER_ORDER_MINOR']}-minor floor ($0.35), "
        f"{spec['IB_TIERED_MAX_PCT_BPS']}-bps cap (1%) (SYS-15a/b/c)"
    )


def check_cost_config_struct(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["cost_config_struct"]
    body = _struct_body(cost_src, spec["struct"])
    missing = [
        f for f in spec["required_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    for method in (spec["zero_method"], spec["validate_method"], spec["breakdown_method"]):
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", cost_src):
            fail(f"{spec['struct']} is missing method `{method}`")
    # Default must be derived so the SyRS-default fields compose into the family.
    if not re.search(r"#\[derive\([^)]*\bDefault\b[^)]*\)\]\s*pub\s+struct\s+CostConfig", cost_src):
        fail("CostConfig must derive Default so CostConfig::default() is the SyRS cost family")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"model fields ({', '.join(spec['required_fields'])}), derives Default (the SyRS family), "
        f"and exposes {spec['zero_method']}() / {spec['validate_method']}() / "
        f"{spec['breakdown_method']}()"
    )


def check_cost_breakdown_struct(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["cost_breakdown_struct"]
    body = _struct_body(cost_src, spec["struct"])
    missing = [
        f for f in spec["required_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['total_method'])}\b", cost_src):
        fail(f"{spec['struct']} is missing the overflow-checked `{spec['total_method']}` total")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} cost "
        f"components ({', '.join(spec['required_fields'])}) and an overflow-checked "
        f"{spec['total_method']}()"
    )


def check_cost_error_enum(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["cost_error_enum"]
    _check_enum_variants(spec, cost_src)
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_validate_fail_closed(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["validate_fail_closed"]
    body = _compact(_fn_block(cost_src, spec["method"]))
    if _compact(spec["error_token"]) not in body:
        fail(
            f"CostConfig::{spec['method']} must reject a negative parameter with "
            f"`{spec['error_token']}` (a cost can never be negative)"
        )
    missing = [f for f in spec["guarded_fields"] if _compact(f) not in body]
    if missing:
        fail(
            f"CostConfig::{spec['method']} must guard the signed override parameters "
            f"({', '.join(missing)} not checked)"
        )
    return (
        f"atp-simulation CostConfig::{spec['method']} fails closed on a negative "
        f"{', '.join(spec['guarded_fields'])} ({spec['error_token']}) before any fill"
    )


def check_money_invariant(config: dict, cost_src: str) -> str:
    spec = contract_block(config)["money_invariant"]
    if spec["forbidden_float_token"] in cost_src:
        fail(
            f"cost module contains `{spec['forbidden_float_token']}` — all cost math MUST use "
            "integer minor units (SRS-BT money-correctness invariant)"
        )
    compact = _compact(cost_src)
    for token_key in ("wide_intermediate_token", "narrow_token"):
        if _compact(spec[token_key]) not in compact:
            fail(
                f"cost math must use `{spec[token_key]}` ({token_key}) so a cost product cannot "
                "silently overflow"
            )
    if not re.search(rf"\bfn\s+{re.escape(spec['round_helper'])}\b", cost_src):
        fail(f"cost module is missing the deterministic `{spec['round_helper']}` rounding helper")
    if spec["checked_op"] not in cost_src:
        fail(f"cost math must use overflow-checked `{spec['checked_op']}`")
    if not re.search(rf"\b{re.escape(spec['overflow_variant'])}\b", cost_src):
        fail(f"cost module must surface overflow via CostError::{spec['overflow_variant']}")
    return (
        f"atp-simulation cost math is integer-only: no {spec['forbidden_float_token']}, "
        f"{spec['wide_intermediate_token']} intermediates, deterministic {spec['round_helper']} "
        f"rounding, {spec['narrow_token']} narrowing + {spec['checked_op']} -> "
        f"CostError::{spec['overflow_variant']}"
    )


def check_engine_application(config: dict, backtest_src: str) -> str:
    spec = contract_block(config)["engine_application"]
    run = _compact(_fn_block(backtest_src, spec["run_method"]))
    for token_key in ("validate_token", "breakdown_token", "deduct_token", "negative_spread_guard"):
        if _compact(spec[token_key]) not in run:
            fail(
                f"BacktestEngine::{spec['run_method']} is missing `{spec[token_key]}` ({token_key}) "
                "— the engine must validate the cost config, compute a per-fill cost breakdown, "
                "SUBTRACT the total from cash (a cost can never fabricate cash), and fail closed on "
                "a negative observed spread"
            )
    missing = [t for t in spec["fill_population_tokens"] if _compact(t) not in run]
    if missing:
        fail(
            f"BacktestEngine::{spec['run_method']} must record the cost decomposition on Fill "
            f"({', '.join(missing)} not populated)"
        )
    return (
        f"atp-simulation BacktestEngine::{spec['run_method']} validates the cost config "
        f"(`{spec['validate_token']}`), computes a per-fill breakdown "
        f"(`{spec['breakdown_token']}`), subtracts the total from cash "
        f"(`{spec['deduct_token']}`), records the decomposition on Fill, and fails closed on a "
        f"negative observed spread (`{spec['negative_spread_guard']}`)"
    )


def check_wiring(config: dict, backtest_src: str) -> str:
    spec = contract_block(config)["wiring"]
    request_body = _struct_body(backtest_src, spec["request_struct"])
    if not re.search(
        rf"\bpub\s+{re.escape(spec['request_field'])}\s*:\s*{re.escape(spec['request_field_type'])}\b",
        request_body,
    ):
        fail(
            f"{spec['request_struct']} must carry a per-run "
            f"`{spec['request_field']}: {spec['request_field_type']}` (the SYS-15d override seam)"
        )
    bar_body = _struct_body(backtest_src, spec["bar_struct"])
    if not re.search(rf"\bpub\s+{re.escape(spec['bar_field'])}\s*:", bar_body):
        fail(
            f"{spec['bar_struct']} must carry an observed `{spec['bar_field']}` for the SYS-15c "
            "observed-spread path"
        )
    fill_body = _struct_body(backtest_src, spec["fill_struct"])
    missing = [
        f for f in spec["fill_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", fill_body)
    ]
    if missing:
        fail(f"{spec['fill_struct']} is missing cost-decomposition fields: {', '.join(missing)}")
    return (
        f"atp-simulation wires the per-run override onto {spec['request_struct']}."
        f"{spec['request_field']}, the observed spread onto {spec['bar_struct']}."
        f"{spec['bar_field']}, and the {len(spec['fill_fields'])} cost components onto "
        f"{spec['fill_struct']}"
    )


def check_shared_family(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["shared_family"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export the cost module (`{spec['lib_reexport_token']}`) "
            "so the backtest engine and the internal simulation engine share the SAME cost family "
            "(SRS-BT-003)"
        )
    return (
        "atp-simulation re-exports `pub mod cost` — the single shared transaction-cost model "
        "family the backtest engine applies (SRS-BT-002) and the internal simulation engine "
        "shares for paper fills (SRS-BT-003, closed)"
    )


def check_cost_cli(config: dict, cargo_text: str) -> str:
    block = contract_block(config)
    spec = block["cost_cli"]
    crate_path = ROOT / block["simulation_crate"]["path"]
    # (1) Cargo.toml registers the operator binary.
    if _compact(spec["cargo_bin_token"]) not in _compact(cargo_text):
        fail(
            "atp-simulation Cargo.toml must register the cost-override CLI bin "
            f"(`{spec['cargo_bin_token']}`)"
        )
    # (2) the bin source exists and wires both subcommands, the three per-model override flags, the
    #     defaults-match proof, and the per-run override seam (the config built from flags lands on
    #     the request, not in the strategy).
    bin_path = crate_path / spec["bin_path"]
    if not bin_path.exists():
        fail(f"cost-override CLI source missing: {bin_path.relative_to(ROOT)}")
    compact_bin = _compact(bin_path.read_text(encoding="utf-8"))
    for token_key in (
        "run_command_token",
        "defaults_command_token",
        "defaults_match_token",
        "override_seam_token",
    ):
        if _compact(spec[token_key]) not in compact_bin:
            fail(
                f"`{spec['bin']}` must wire `{spec[token_key]}` ({token_key}) so the operator CLI "
                "demonstrates the SyRS defaults AND a per-run override that lands on the request"
            )
    missing = [f for f in spec["override_flag_tokens"] if _compact(f) not in compact_bin]
    if missing:
        fail(
            f"`{spec['bin']}` must expose the per-model override flags ({', '.join(missing)} "
            "missing) so an operator can override commission/slippage/spread per run (SYS-15d)"
        )
    # (3) the integration test that drives the binary in fresh processes exists.
    test_path = crate_path / "tests" / f"{spec['cli_integration_test']}.rs"
    if not test_path.exists():
        fail(f"cost-override CLI integration test missing: {test_path.relative_to(ROOT)}")
    return (
        f"atp-simulation registers the {spec['bin']} operator binary (defaults + run): it prints the "
        "SyRS default constants and proves CostConfig::default()==syrs_defaults(), and builds a "
        "per-run CostConfig from --commission/--slippage/--spread applied to the SAME fixture "
        f"strategy (override seam `{spec['override_seam_token']}`); the {spec['cli_integration_test']} "
        "integration test drives it in fresh processes -- the CLI half of the SYS-15d override surface"
    )


def check_vendor_isolation(config: dict, cost_src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in cost_src]
    if leaked:
        fail(
            f"atp-simulation cost module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation cost module is free of all {len(tokens)} forbidden vendor SDK tokens "
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
                "cost models compile + pass (install the Rust toolchain)"
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
    cli_integration = block.get("rust_cli_integration_test")
    if cli_integration:
        cli = subprocess.run(
            [cargo, "test", "-p", crate, "--test", cli_integration, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if cli.returncode != 0:
            fail(
                f"cargo test -p {crate} --test {cli_integration} failed:\n{cli.stdout}\n{cli.stderr}"
            )
    suite = f"{integration} + {cli_integration}" if cli_integration else integration
    return (
        f"cargo test -p {crate} --lib + {suite}: PASS "
        "(default cost family matches SyRS values, observed-vs-fallback spread, per-run overrides "
        "without strategy changes, costs strictly reduce cash, fail-closed branches verified, and "
        "the bt002_cost_cli operator surface drives defaults + per-run override in fresh processes)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (collector, source-key) — "cost" reads cost.rs, "backtest" reads backtest.rs,
# "lib" reads lib.rs.
_STATIC_CHECKS = (
    ("commission_model", check_commission_model, "cost"),
    ("slippage_model", check_slippage_model, "cost"),
    ("spread_impact_model", check_spread_impact_model, "cost"),
    ("syrs_default_constants", check_syrs_default_constants, "cost"),
    ("cost_config_struct", check_cost_config_struct, "cost"),
    ("cost_breakdown_struct", check_cost_breakdown_struct, "cost"),
    ("cost_error_enum", check_cost_error_enum, "cost"),
    ("validate_fail_closed", check_validate_fail_closed, "cost"),
    ("money_invariant", check_money_invariant, "cost"),
    ("engine_application", check_engine_application, "backtest"),
    ("wiring", check_wiring, "backtest"),
    ("cost_cli", check_cost_cli, "cargo"),
    ("shared_family", check_shared_family, "lib"),
    ("vendor_isolation", check_vendor_isolation, "cost"),
)

_DEFERRED_OWNERS = (
    "SRS-API-001 / SRS-UI (REST + dashboard cost-override surface; the CLI half is realized via "
    "bt002_cost_cli)",
    "SRS-BT-002 (IB tiered monthly-volume tier ladder)",
    "SRS-BT-001-runtime (real data + Python strategy host)",
)


def _lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def assert_backtest_cost_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "cost": cost_source(config, root),
        "backtest": backtest_source(config, root),
        "lib": _lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_backtest_cost_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-002 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable cost models must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except BacktestCostCheckError as error:
        print(f"SRS-BT-002 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-002 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- CLI override surface realized via bt002_cost_cli; feature_list.json SRS-BT-002 "
        "passes:true. deferred to: " + ", ".join(_DEFERRED_OWNERS)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
