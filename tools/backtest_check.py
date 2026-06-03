#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-001 (runnable backtest engine).

SRS-BT-001: "backtest Python strategies against stored data and user-uploaded
Parquet data over configurable date ranges" (SyRS SYS-14 / SYS-43a; StRS
SN-1.02 / SN-1.13 / C-4). The acceptance criterion: "A backtest can be launched
with system data or uploaded Parquet data; start and end dates are selectable
through API and dashboard."

This SDK-surface ships a *genuinely runnable* deterministic engine in
``crates/atp-simulation`` (module ``backtest``) and verifies its structural
contract declared in ``architecture/runtime_services.json`` (block
``backtest_contract``):

  (a) ``BacktestDataSource`` enumerates SystemData + UploadedData (a backtest is
      launchable from the system catalog OR uploaded data); ``DateRange`` is the
      configurable window and ``validate`` fails closed on an inverted range.
  (b) ``BacktestBar`` / ``BacktestRequest`` / ``Fill`` / ``EquityPoint`` /
      ``BacktestResult`` carry their required fields and no broker/vendor leakage.
  (c) the engine seam is two ports — ``BarSource`` (the deferred Parquet /
      system-catalog reader) and ``BacktestStrategy`` (the deferred Python
      strategy host) — and ``BacktestEngine::run`` validates the range, restricts
      replay to the configurable window, replays deterministically, drives the
      strategy, and fails closed (EmptySymbol / InvalidDateRange / EmptyData).
  (d) ALL money math is integer minor units: the source contains no ``f64``, the
      minor-unit fields are ``i64``, and the notional is computed through the
      overflow-safe ``checked_notional`` (i128 intermediate + checked_add/sub).
  (e) the core simulation crate carries no vendor-SDK token.

The PASS line is ``SRS-BT-001 SDK-SURFACE PASS`` — it names the deferred runtime
owners (real Parquet reader, Python strategy host, REST/dashboard launch surface,
cost models, metrics, persistence) so the partial-pass status (feature_list.json
keeps ``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/subscription_fanout_check.py``.

Invoke:
    python3 tools/backtest_check.py
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class BacktestCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise BacktestCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    import json

    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "backtest_contract" not in config:
        fail("architecture metadata is missing backtest_contract")
    return config["backtest_contract"]


def backtest_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['module']}.rs"
    if not source_path.exists():
        fail(f"backtest module source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def _check_struct_fields(spec: dict, src: str, label: str) -> None:
    body = _struct_body(src, spec["struct"])
    missing = [
        f for f in spec["required_fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    for forbidden in spec.get("forbidden_fields", []):
        if re.search(rf"\bpub\s+{re.escape(forbidden)}\s*:", body):
            fail(
                f"{spec['struct']} leaks broker/vendor field `{forbidden}` "
                f"({label} must not carry broker/vendor identifiers)"
            )


def check_data_source_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["data_source_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} launch sources "
        f"({', '.join(spec['variants'])}) — a backtest can be launched from system data OR "
        "uploaded data (SRS-BT-001 AC)"
    )


def check_date_range(config: dict, src: str) -> str:
    spec = contract_block(config)["date_range"]
    body = _struct_body(src, spec["struct"])
    missing = [f for f in spec["fields"] if not re.search(rf"\bpub\s+{re.escape(f)}\s*:", body)]
    if missing:
        fail(f"{spec['struct']} is missing fields: {', '.join(missing)}")
    validate = _compact(_fn_block(src, spec["validate_method"]))
    if _compact(spec["inverted_check_token"]) not in validate:
        fail(
            f"{spec['struct']}::{spec['validate_method']} must fail closed on an inverted "
            f"window via `{spec['inverted_check_token']}`"
        )
    if _compact(f"BacktestError::{spec['invalid_variant']}") not in validate:
        fail(
            f"{spec['struct']}::{spec['validate_method']} must return "
            f"BacktestError::{spec['invalid_variant']} on an inverted window"
        )
    return (
        f"atp-simulation declares {spec['struct']} (configurable {', '.join(spec['fields'])}) "
        f"whose {spec['validate_method']}() fails closed on start > end "
        f"(BacktestError::{spec['invalid_variant']}) — SRS-BT-001 configurable date range"
    )


def check_bar_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["bar_struct"]
    _check_struct_fields(spec, src, "SRS-BT-001 bars")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"fields ({', '.join(spec['required_fields'])}; integer close_minor) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor fields"
    )


def check_request_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["request_struct"]
    _check_struct_fields(spec, src, "SRS-BT-001 request")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"launch fields ({', '.join(spec['required_fields'])})"
    )


def check_fill_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["fill_struct"]
    _check_struct_fields(spec, src, "SRS-BT-001 trade log")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"trade-log fields ({', '.join(spec['required_fields'])})"
    )


def check_equity_point_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["equity_point_struct"]
    _check_struct_fields(spec, src, "SRS-BT-001 equity curve")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"equity-curve fields ({', '.join(spec['required_fields'])})"
    )


def check_result_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["result_struct"]
    _check_struct_fields(spec, src, "SRS-BT-001 result")
    return (
        f"atp-simulation declares {spec['struct']} with the {len(spec['required_fields'])} "
        f"result fields ({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/vendor fields"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed "
        f"variants ({', '.join(spec['variants'])})"
    )


def check_bar_source_port(config: dict, src: str) -> str:
    spec = contract_block(config)["bar_source_port"]
    body = _trait_body(src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    if not re.search(rf"\b{re.escape(spec['bounded_read_param'])}\s*:\s*usize\b", body):
        fail(
            f"{spec['trait']}::bars must take a `{spec['bounded_read_param']}: usize` bound so a "
            "source caps its own read (cannot materialize an unbounded response)"
        )
    return (
        f"atp-simulation declares port trait {spec['trait']} with {len(spec['methods'])} "
        f"method ({', '.join(spec['methods'])}); bars takes a {spec['bounded_read_param']} read "
        "bound — the deferred Parquet / system-catalog reader seam"
    )


def check_strategy_port(config: dict, src: str) -> str:
    spec = contract_block(config)["strategy_port"]
    body = _trait_body(src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    if _compact(spec["fallible_return"]) not in _compact(body):
        fail(
            f"{spec['trait']}::on_bar must be fallible (`{spec['fallible_return']}`) so the "
            "deferred Python host can surface a raise / timeout / marshaling failure rather "
            "than the engine silently treating it as a 0 delta"
        )
    return (
        f"atp-simulation declares fallible port trait {spec['trait']} with {len(spec['methods'])} "
        f"method ({', '.join(spec['methods'])}) returning {spec['fallible_return']} — the "
        "deferred Python strategy execution boundary"
    )


def check_engine(config: dict, src: str) -> str:
    spec = contract_block(config)["engine"]
    # The inherent impl carries new + run.
    for method in spec["methods"]:
        if not re.search(rf"\bpub\s+fn\s+{re.escape(method)}\b", src):
            fail(f"{spec['struct']} is missing method `{method}`")
    if not re.search(rf"\bconst\s+{re.escape(spec['row_limit_const'])}\b", src):
        fail(
            f"backtest module is missing the `{spec['row_limit_const']}` replay-size cap "
            "(the fail-closed memory guard for the in-memory engine)"
        )
    run = _compact(_fn_block(src, spec["run_method"]))
    for token_key in (
        "range_validate_token",
        "empty_symbol_token",
        "provenance_check_token",
        "provenance_error_token",
        "source_cap_pass_token",
        "row_limit_token",
        "symbol_guard_token",
        "duplicate_guard_token",
        "price_guard_token",
        "range_restrict_token",
        "determinism_sort_token",
        "strategy_drive_token",
        "strategy_fallible_token",
        "empty_data_token",
    ):
        if _compact(spec[token_key]) not in run:
            fail(
                f"{spec['struct']}::{spec['run_method']} is missing `{spec[token_key]}` "
                f"({token_key}) — the engine must validate the range, bound the replay size, "
                "guard the source trust boundary (reject a foreign symbol, duplicate timestamps, "
                "and a non-positive price), restrict replay to the configurable window, replay "
                "deterministically, propagate a strategy failure (no silent 0 delta), and fail "
                "closed on an empty window"
            )
    return (
        f"atp-simulation: {spec['struct']}::{spec['run_method']} validates the range "
        f"(`{spec['range_validate_token']}`), validates data-source provenance "
        f"(`{spec['provenance_check_token']}` -> `{spec['provenance_error_token']}`), bounds the "
        f"replay size (`{spec['row_limit_token']}` / {spec['row_limit_const']}), guards the source trust "
        f"boundary (`{spec['symbol_guard_token']}` + `{spec['duplicate_guard_token']}` + "
        f"`{spec['price_guard_token']}`), restricts replay to the window "
        f"(`{spec['range_restrict_token']}`), replays deterministically "
        f"(`{spec['determinism_sort_token']}`), propagates a strategy failure "
        f"(`{spec['strategy_fallible_token']}`), and fails closed on an empty window"
    )


def check_money_invariant(config: dict, src: str) -> str:
    spec = contract_block(config)["money_invariant"]
    if spec["forbidden_float_token"] in src:
        fail(
            f"backtest module contains `{spec['forbidden_float_token']}` — all backtest money "
            "math MUST use integer minor units (SRS-BT money-correctness invariant)"
        )
    for field in spec["minor_unit_fields"]:
        if not re.search(rf"\b{re.escape(field)}\s*:\s*i64\b", src):
            fail(f"money field `{field}` must be typed i64 minor units")
    if not re.search(rf"\bfn\s+{re.escape(spec['notional_helper'])}\b", src):
        fail(f"backtest module is missing the overflow-safe `{spec['notional_helper']}` helper")
    compact = _compact(src)
    if _compact(spec["wide_intermediate_token"]) not in compact:
        fail(
            f"{spec['notional_helper']} must use a wide intermediate "
            f"(`{spec['wide_intermediate_token']}`) so the notional product cannot silently overflow"
        )
    for op in spec["checked_ops"]:
        if op not in src:
            fail(f"backtest money math must use overflow-checked `{op}`")
    if not re.search(rf"\b{re.escape(spec['overflow_variant'])}\b", src):
        fail(f"backtest module must surface overflow via BacktestError::{spec['overflow_variant']}")
    return (
        f"atp-simulation backtest money math is integer-only: no {spec['forbidden_float_token']}, "
        f"{len(spec['minor_unit_fields'])} i64 minor-unit fields, overflow-safe "
        f"{spec['notional_helper']} ({spec['wide_intermediate_token']} + "
        f"{', '.join(spec['checked_ops'])} -> BacktestError::{spec['overflow_variant']})"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation backtest module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation backtest module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        # This is a *runnable* Rust engine. Where the engine must actually
        # compile and pass (init.sh local gate, via --require-cargo), a missing
        # cargo is a failure, not a skip. The Python-only CI job omits the flag
        # because the dedicated Rust CI job (cargo test --workspace) is the
        # authoritative compile gate there.
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable "
                f"{crate} backtest engine compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
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
        "(end-to-end run over system + uploaded data, date sub-selection, fail-closed "
        "branches, deterministic replay verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

_STATIC_CHECKS = (
    ("data_source_enum", check_data_source_enum),
    ("date_range", check_date_range),
    ("bar_struct", check_bar_struct),
    ("request_struct", check_request_struct),
    ("fill_struct", check_fill_struct),
    ("equity_point_struct", check_equity_point_struct),
    ("result_struct", check_result_struct),
    ("error_enum", check_error_enum),
    ("bar_source_port", check_bar_source_port),
    ("strategy_port", check_strategy_port),
    ("engine", check_engine),
    ("money_invariant", check_money_invariant),
    ("vendor_isolation", check_vendor_isolation),
)

_DEFERRED_OWNERS = (
    "SRS-BT-001-runtime (Parquet reader + Python strategy host)",
    "SRS-API-001 / SRS-UI (REST + dashboard launch)",
    "SRS-BT-002/003 (cost models)",
    "SRS-BT-004 (metrics)",
    "SRS-BT-009 (persistence)",
    "SRS-BT-010 (determinism guarantee)",
)


def assert_backtest_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    src = backtest_source(config, root)
    return [check(config, src) for _, check in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_backtest_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-001 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable — the runnable engine must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except BacktestCheckError as error:
        print(f"SRS-BT-001 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-001 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-001 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
