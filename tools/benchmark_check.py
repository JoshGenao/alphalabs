#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-005 (compare strategy performance against a
user-selected benchmark defaulting to SPY).

SRS-BT-005 (SyRS SYS-17, SYS-36, SYS-37; StRS SN-1.04). The acceptance criterion: "If
no benchmark is selected, SPY is used; alpha and beta are computed against the selected
benchmark; dashboard and backtest reports identify the benchmark."

The benchmark selection / resolution-seam / comparison-report surface lives in
``crates/atp-simulation`` (module ``benchmark``), per the structural contract in
``architecture/runtime_services.json`` (block ``sim_benchmark_contract``). It WRAPS the
SRS-BT-004 ``metrics`` family (which already computes alpha/beta against a supplied
benchmark series) with the three pieces SRS-BT-005 names:

  (a) ``BenchmarkSelection`` resolves to SPY when the operator selects none
      (``selection.resolve()`` applies ``unwrap_or_default()``, the single SPY-default
      site) and validates a user-selected ticker, mapping a malformed symbol to
      ``BenchmarkError::UnknownBenchmark``.
  (b) ``BenchmarkSource::levels`` returns ``Result<ResolvedBenchmark, SourceFailure>`` --
      the NARROW error type, so a source can only report a typed operational failure
      (timeout / unavailable / not-found / stale), which ``compare`` maps to
      ``BenchmarkError::SourceUnavailable``. The returned ``ResolvedBenchmark``'s ``symbol``
      is bound to the data; ``compare`` validates it equals the selection AFTER the fetch,
      so a source cannot return one benchmark's levels while the report identifies another.
      The real stored-data resolver is the deferred SRS-DATA-007 owner and a fixture drives
      tests; timeout/cancellation ENFORCEMENT is the I/O adapter + SYS-36 dashboard owner,
      not this pure deterministic function.
  (c) ``BenchmarkComparison`` (and ``BenchmarkReport``) carry the benchmark identity plus
      alpha/beta and total/excess return as ``Option<f64>`` ratios, so a report
      identifies its benchmark; ``compare`` ties the three together.
  (d) ``compare`` is BOUND TO THE STRATEGY RUN WINDOW (``window: DateRange``, e.g.
      ``BacktestResult.range``): every equity mark must fall within the INCLUSIVE window
      (``window.contains``, ``EquityMarkOutsideWindow``, so a backtest whose first mark
      lands exactly on ``range.start`` is accepted) and the benchmark baseline must be the
      pre-trade prior close, strictly before the first mark (``BaselineNotBeforeRun``).
      Verifying the baseline is the IMMEDIATE prior close (not an arbitrarily stale earlier
      observation) needs the data layer's bar grid and is the deferred SRS-DATA-007
      resolver's responsibility. It then re-validates the resolved series at the trust
      boundary (symbol, length, timestamp alignment, strict positivity) BEFORE calling
      ``metrics::compute``, wrapping any ``MetricsError`` as ``BenchmarkError::Metrics``
      (defense in depth).
  (e) the numeric boundary is inherited: levels enter as integer minor units and the
      comparison emits dimensionless f64 ratios, each verified finite
      (``BenchmarkError::NonFiniteComparison``); the work is deterministic (no
      parallelism / RNG / clock).
  (f) ``benchmark`` adds no broker/adapter dependency and carries no vendor SDK token;
      ``lib.rs`` re-exports ``pub mod benchmark;``.

The PASS line is ``SRS-BT-005 SDK-SURFACE PASS`` -- it names the deferred owners (the
real benchmark level-series resolution via SRS-DATA-007, the dashboard/backtest report
rendering via SRS-UI / SRS-API, and the SRS-BT-009 persisted-comparison record) so the
partial-pass status (feature_list.json keeps ``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/metrics_check.py``.

Invoke:
    python3 tools/benchmark_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class BenchmarkCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise BenchmarkCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_benchmark_contract" not in config:
        fail("architecture metadata is missing sim_benchmark_contract")
    return config["sim_benchmark_contract"]


def benchmark_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / "src" / f"{block['benchmark_module']}.rs"
    )
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


def check_selection(config: dict, src: str) -> str:
    spec = contract_block(config)["selection"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"benchmark must declare `pub struct {spec['struct']}`")
    compact_src = _compact(src)
    for key, label in (
        ("resolve_fn", "a resolve() that applies the SPY default"),
        ("unselected_fn", "an unselected() constructor (no operator choice)"),
        ("from_symbol_fn", "a from_symbol() that validates a user-selected ticker"),
        ("is_default_fn", "an is_default() so a report can state the default was used"),
    ):
        if not re.search(rf"\b{re.escape(spec[key])}\b", src):
            fail(f"{spec['struct']} must expose {label} (`{spec[key]}`)")
    if _compact(spec["spy_default_token"]) not in compact_src:
        fail(
            f"{spec['struct']}::resolve must apply the SPY default exactly once "
            f"(`{spec['spy_default_token']}`) so an unselected benchmark becomes SPY (SYS-17)"
        )
    if _compact(spec["unknown_error_token"]) not in compact_src:
        fail(
            f"from_symbol must reject a malformed selected symbol with "
            f"`{spec['unknown_error_token']}`"
        )
    return (
        "atp-simulation BenchmarkSelection resolves an unselected benchmark to SPY "
        "(unwrap_or_default, the single SYS-17 default site) and validates a user-selected ticker "
        "(UnknownBenchmark on a malformed symbol), with is_default() so a report identifies the "
        "default"
    )


def check_source_trait(config: dict, src: str) -> str:
    spec = contract_block(config)["source_trait"]
    body = _compact(_trait_body(src, spec["trait"]))
    if _compact(spec["levels_fn"]) not in body:
        fail(f"{spec['trait']} must declare {spec['levels_fn']}() (`{spec['levels_fn']}`)")
    if _compact(spec["returns_token"]) not in body:
        fail(
            f"{spec['trait']}::levels must return a {spec['returns_token']} so the resolved symbol "
            "is bound to the returned data (not a separate pre-fetch declaration)"
        )
    return (
        f"atp-simulation declares the {spec['trait']} resolution port (levels -> "
        f"{spec['returns_token']}) -- the deferred stored-data resolver seam (owner SRS-DATA-007), "
        "with a fixture impl for tests"
    )


def check_resolved_identity(config: dict, src: str) -> str:
    spec = contract_block(config)["resolved_identity"]
    body = _compact(_struct_body(src, spec["struct"]))
    if _compact(spec["symbol_field"]) not in body:
        fail(f"{spec['struct']} must carry the resolved symbol (`{spec['symbol_field']}`)")
    if _compact(spec["levels_field"]) not in body:
        fail(f"{spec['struct']} must carry the resolved levels (`{spec['levels_field']}`)")
    compact_src = _compact(src)
    if _compact(spec["post_fetch_guard"]) not in compact_src:
        fail(
            f"compare must validate the RETURNED benchmark symbol after the fetch "
            f"(`{spec['post_fetch_guard']}`) so a source cannot return one benchmark's levels while "
            "the report identifies another"
        )
    if _compact(spec["mismatch_error"]) not in compact_src:
        fail(f"a substituted benchmark series must fail closed with `{spec['mismatch_error']}`")
    return (
        f"atp-simulation binds benchmark identity to the returned data: {spec['struct']} carries the "
        "resolved symbol + levels, and compare validates that symbol equals the selection AFTER the "
        "fetch (SourceSymbolMismatch), so a buggy cache/resolver cannot report SPY while comparing "
        "against another benchmark's levels"
    )


def check_comparison_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["comparison_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    if _compact(spec["identity_field"]) not in body:
        fail(f"{spec['struct']} must carry the benchmark identity (`{spec['identity_field']}`)")
    if _compact(spec["default_flag_field"]) not in body:
        fail(
            f"{spec['struct']} must record whether the default was used (`{spec['default_flag_field']}`)"
        )
    missing = [f for f in spec["ratio_fields"] if _compact(f"{f}:{spec['ratio_type']}") not in body]
    if missing:
        fail(
            f"{spec['struct']} must report each comparison ratio as {spec['ratio_type']} so an "
            f"undefined value is None, not a fabricated zero: missing {', '.join(missing)}"
        )
    return (
        f"atp-simulation declares {spec['struct']} carrying the benchmark identity "
        "(benchmark_symbol + is_default_benchmark) plus alpha, beta, and total/excess return as "
        f"{spec['ratio_type']} -- the data a report renders to identify and contrast against its "
        "benchmark"
    )


def check_report_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["report_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    for key, label in (
        ("metrics_field", "the metric family"),
        ("comparison_field", "the comparison"),
    ):
        if _compact(spec[key]) not in body:
            fail(f"{spec['struct']} must bundle {label} (`{spec[key]}`)")
    return (
        f"atp-simulation declares {spec['struct']} bundling the full PerformanceMetrics family with "
        "the BenchmarkComparison the report identifies"
    )


def check_compare_fn(config: dict, src: str) -> str:
    spec = contract_block(config)["compare_fn"]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['fn'])}\b", src):
        fail(f"benchmark must expose `pub fn {spec['fn']}` (the comparison entry point)")
    signature = src[src.index(f"pub fn {spec['fn']}") :].split("{", 1)[0]
    missing = [p for p in spec["param_tokens"] if p not in signature]
    if missing:
        fail(
            f"`{spec['fn']}` must take {', '.join(spec['param_tokens'])}: missing "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-simulation exposes `pub fn {spec['fn']}` over the run (baseline + equity curve + trade "
        "log), the benchmark selection, the resolution source, and the metric config -- the single "
        "SRS-BT-005 comparison entry point"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed variants "
        f"({', '.join(spec['variants'])})"
    )


def check_trust_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["trust_boundary"]
    compact_src = _compact(src)
    for key, label in (
        ("symbol_guard", "reject a source serving a different benchmark than the selection"),
        ("length_guard", "reject a resolved series of the wrong length"),
        ("timestamp_guard", "reject a misaligned resolved level"),
        ("positivity_guard", "reject a non-positive resolved level"),
        ("window_guard", "reject an equity mark outside the run window"),
        ("defense_in_depth_token", "re-validate through metrics::compute (wrapping its error)"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"compare must {label} (`{spec[key]}`)")
    return (
        "atp-simulation compare re-validates the resolved series at the trust boundary -- symbol "
        "match, length, per-timestamp alignment, strict positivity, and run-window coherence -- and "
        "fails closed with a source-attributed BenchmarkError BEFORE calling metrics::compute, which "
        "re-validates as defense-in-depth (MetricsError wrapped as BenchmarkError::Metrics)"
    )


def check_run_window_binding(config: dict, src: str) -> str:
    spec = contract_block(config)["run_window_binding"]
    compact_src = _compact(src)
    for key, label in (
        ("window_param_token", "take the run's evaluation window (not a free baseline ts)"),
        ("coherence_guard_token", "reject an equity mark outside the window"),
        ("inclusive_contains_token", "use INCLUSIVE window containment (window.contains)"),
        ("baseline_guard_token", "require the baseline strictly before the first mark"),
        ("baseline_before_run_token", "fail closed on a baseline not before the run"),
        ("invalid_window_token", "reject an inverted window"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"compare must bind the comparison to the strategy run window: missing the guard to "
                f"{label} (`{spec[key]}`)"
            )
    return (
        "atp-simulation compare is bound to the strategy run's INCLUSIVE evaluation window (window: "
        "DateRange, e.g. BacktestResult.range): every equity mark must fall within the window "
        "(window.contains, EquityMarkOutsideWindow, so a backtest whose first mark lands exactly on "
        "range.start is accepted) and the benchmark baseline must be the pre-trade prior close, "
        "strictly before the first mark (BaselineNotBeforeRun); verifying it is the IMMEDIATE prior "
        "close needs the deferred SRS-DATA-007 bar grid"
    )


def check_source_failure(config: dict, src: str) -> str:
    spec = contract_block(config)["source_failure"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(
            f"{spec['enum']} must enumerate the operational read failures a real resolver surfaces: "
            f"missing {', '.join(missing)}"
        )
    if _compact(spec["error_variant_token"]) not in _compact(src):
        fail(
            f"BenchmarkError must carry a typed source-failure variant "
            f"(`{spec['error_variant_token']}`) so an operational read failure is not misclassified "
            "as a malformed series"
        )
    compact_src = _compact(src)
    if _compact(spec["narrow_return_token"]) not in compact_src:
        fail(
            f"BenchmarkSource::levels must fail with the NARROW {spec['enum']} "
            f"(`{spec['narrow_return_token']}`) so a source cannot return a consumer-only "
            "BenchmarkError variant (compiler-enforced adapter-boundary contract)"
        )
    if _compact(spec["compare_map_token"]) not in compact_src:
        fail(
            f"compare must own the mapping from {spec['enum']} to BenchmarkError::SourceUnavailable "
            f"(`{spec['compare_map_token']}`)"
        )
    return (
        f"atp-simulation declares {spec['enum']} ({', '.join(spec['variants'])}) as the NARROW "
        "BenchmarkSource::levels error type (so a source cannot return a consumer-only variant), "
        "which compare maps to BenchmarkError::SourceUnavailable -- the deferred SRS-DATA-007 "
        "resolver surfaces a timeout / unavailable / not-found / stale read distinctly (stale-data "
        "blocking) rather than hiding it behind a malformed series"
    )


def check_spy_default(config: dict, src: str) -> str:
    spec = contract_block(config)["spy_default"]
    compact_src = _compact(src)
    if _compact(spec["resolve_token"]) not in compact_src:
        fail(
            f"the SPY default must be applied exactly once in resolve (`{spec['resolve_token']}`) so "
            "an unselected benchmark becomes SPY (SYS-17)"
        )
    if _compact(spec["selection_call_token"]) not in compact_src:
        fail(
            f"compare must resolve the selection (`{spec['selection_call_token']}`) so the SPY "
            "default flows into the comparison"
        )
    return (
        "atp-simulation applies the SPY default in BenchmarkSelection::resolve (unwrap_or_default) "
        "and compare resolves the selection, so a run that names no benchmark is compared against and "
        "identifies SPY (SYS-17)"
    )


def check_metrics_reuse(config: dict, src: str) -> str:
    spec = contract_block(config)["metrics_reuse"]
    compact_src = _compact(src)
    if _compact(spec["import_token"]) not in compact_src:
        fail(
            f"benchmark must consume the SRS-BT-004 metric family (`{spec['import_token']}`) rather "
            "than re-implement alpha/beta"
        )
    missing = [m for m in spec["import_members"] if not re.search(rf"\b{re.escape(m)}\b", src)]
    if missing:
        fail(f"benchmark must reuse metrics members: missing {', '.join(missing)}")
    if _compact(spec["compute_call_token"]) not in compact_src:
        fail(f"compare must delegate to metrics::compute (`{spec['compute_call_token']}`)")
    return (
        "atp-simulation benchmark reuses the SRS-BT-004 metric family (imports compute / Benchmark / "
        "BenchmarkPoint / MetricsConfig / PerformanceMetrics and delegates alpha/beta to "
        "metrics::compute) rather than duplicating the metric math"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"benchmark must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- the comparison must use fixed left-to-right folds with no "
            "parallelism, RNG, or wall-clock read"
        )
    return (
        "atp-simulation benchmark comparison is deterministic: no parallelism / RNG / clock token, so "
        "identical inputs yield identical comparisons (SRS-BT-010)"
    )


def check_nan_guard(config: dict, src: str) -> str:
    spec = contract_block(config)["nan_guard"]
    compact_src = _compact(src)
    if not re.search(rf"\b{re.escape(spec['finite_fn_token'])}\b", src):
        fail(f"benchmark must guard a non-finite ratio with `{spec['finite_fn_token']}`")
    if _compact(spec["is_finite_token"]) not in compact_src:
        fail(f"benchmark must verify each comparison ratio is finite (`{spec['is_finite_token']}`)")
    if _compact(spec["error_token"]) not in compact_src:
        fail(f"a non-finite comparison ratio must fail closed with `{spec['error_token']}`")
    return (
        "atp-simulation verifies every comparison ratio is finite (fn finite_opt + is_finite) and "
        "fails closed (NonFiniteComparison) rather than leaking NaN/inf into a ranking or dashboard"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    if _compact(spec["level_input_token"]) not in compact_src:
        fail(f"benchmark levels must enter as integer minor units (`{spec['level_input_token']}`)")
    if _compact(spec["ratio_output_token"]) not in compact_src:
        fail(
            f"the comparison must output dimensionless f64 ratios (`{spec['ratio_output_token']}`)"
        )
    return (
        "atp-simulation keeps levels in integer minor units on input (level_minor: i64) and outputs "
        "the comparison as dimensionless f64 ratios -- the f64 is the metric domain, not a money leak"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the benchmark "
            "surface is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- the benchmark surface must be independent of the IB account"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the benchmark surface is broker-independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation benchmark module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation benchmark module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    module = block["benchmark_module"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "benchmark path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", module, "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib {module} failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib {module} + {integration}: PASS "
        "(resolves an unselected benchmark to SPY, computes alpha/beta against the resolved series, "
        "is deterministic, and fails closed on a misaligned / wrong-symbol / non-positive source)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "benchmark" reads benchmark.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("selection", check_selection, "benchmark"),
    ("source_trait", check_source_trait, "benchmark"),
    ("resolved_identity", check_resolved_identity, "benchmark"),
    ("comparison_struct", check_comparison_struct, "benchmark"),
    ("report_struct", check_report_struct, "benchmark"),
    ("compare_fn", check_compare_fn, "benchmark"),
    ("error_enum", check_error_enum, "benchmark"),
    ("trust_boundary", check_trust_boundary, "benchmark"),
    ("run_window_binding", check_run_window_binding, "benchmark"),
    ("source_failure", check_source_failure, "benchmark"),
    ("spy_default", check_spy_default, "benchmark"),
    ("metrics_reuse", check_metrics_reuse, "benchmark"),
    ("determinism", check_determinism, "benchmark"),
    ("nan_guard", check_nan_guard, "benchmark"),
    ("numeric_boundary", check_numeric_boundary, "benchmark"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "benchmark"),
)

_DEFERRED_OWNERS = (
    "benchmark level-series resolution from stored data (SRS-DATA-007 behind BenchmarkSource)",
    "immediate-prior-close baseline verification (needs the SRS-DATA-007 bar grid)",
    "benchmark-read timeout/cancellation enforcement (SRS-DATA-007 adapter + SYS-36 dashboard)",
    "dashboard / backtest report benchmark identification (SRS-UI / SRS-API; SYS-36 <= 5s, SYS-37)",
    "persisting the benchmark comparison into the queryable backtest record (SRS-BT-009)",
)


def assert_sim_benchmark_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "benchmark": benchmark_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_benchmark_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-005 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable benchmark path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except BenchmarkCheckError as error:
        print(f"SRS-BT-005 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-005 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-005 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
