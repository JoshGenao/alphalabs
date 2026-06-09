#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-004 (compute required backtest and paper/live
performance metrics).

SRS-BT-004 (SyRS SYS-16, SYS-86; StRS SN-1.04 / SN-1.05 / SN-1.29). The acceptance
criterion: "Sharpe ratio, Sortino ratio, alpha, beta, maximum drawdown, annualized
return, annualized volatility, and win rate are produced for completed backtests,
paper strategies, and live dashboard reporting."

The shared performance-metric family lives in ``crates/atp-simulation`` (module
``metrics``), per the structural contract in ``architecture/runtime_services.json``
(block ``sim_metrics_contract``):

  (a) ``PerformanceMetrics`` carries the eight SYS-16 metrics, each ``Option<f64>``
      (a metric undefined on the input is None, never a fabricated 0.0), plus the
      ``benchmark_symbol`` identity.
  (b) money enters in integer minor units (EquityPoint/BenchmarkPoint/Fill are i64)
      but the metrics are dimensionless f64 ratios; the f64 work is deterministic
      (fixed left-to-right folds, no parallelism / RNG / clock) so identical inputs
      yield identical metrics (SRS-BT-010), and a non-strictly-increasing timestamp
      is rejected (MetricsError::NonMonotonicTimestamps).
  (c) ``Benchmark`` defaults to SPY (DEFAULT_BENCHMARK_SYMBOL); alpha/beta are
      computed against a caller-supplied benchmark level series whose first point is
      the benchmark's pre-trade baseline and whose remaining points align 1:1 with the
      equity curve, fail-closed on a length/timestamp mismatch or non-positive level.
  (d) the win rate counts COMPLETE flat-to-flat round trips (fragmentation- and
      order-invariant) on canonicalized symbols, a win on strictly positive NET
      round-trip P&L, fail-closed on a non-positive price, a negative cost, a backwards
      timestamp, or an ambiguous same-symbol/same-timestamp pair.
  (g) the pre-trade baseline is a REQUIRED compute input (starting_equity_minor), so
      the first period's P&L (incl. entry costs) is always captured.
  (e) ``MetricsConfig`` carries the annualization factor (default 252) and the
      per-period risk-free rate; ``new`` fails closed on a zero factor or an
      impossible risk-free rate. Every computed metric is verified finite
      (MetricsError::NonFiniteComputation), so a pathological input fails closed
      rather than leaking NaN/inf.
  (f) ``metrics`` adds no broker/adapter dependency and carries no vendor SDK token;
      ``lib.rs`` re-exports ``pub mod metrics;``.

The PASS line is ``SRS-BT-004 SDK-SURFACE PASS`` -- it names the deferred owners (the
live dashboard reporting path, the paper/live runtime metric accumulators that feed
this family, and the SRS-BT-005 benchmark-resolution surface) so the partial-pass
status (feature_list.json keeps ``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/sim_persistence_check.py``.

Invoke:
    python3 tools/metrics_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _struct_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class MetricsCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise MetricsCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_metrics_contract" not in config:
        fail("architecture metadata is missing sim_metrics_contract")
    return config["sim_metrics_contract"]


def metrics_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['metrics_module']}.rs"
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


def check_metrics_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["metrics_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    metric_type = _compact(spec["metric_type"])
    missing = [
        f for f in spec["metric_fields"] if _compact(f"{f}:{spec['metric_type']}") not in body
    ]
    if missing:
        fail(
            f"{spec['struct']} must declare every SYS-16 metric as {spec['metric_type']} so an "
            f"undefined metric is None, not a fabricated zero: missing {', '.join(missing)}"
        )
    if metric_type not in body:
        fail(f"{spec['struct']} must report metrics as {spec['metric_type']}")
    if _compact(spec["benchmark_field"]) not in body:
        fail(f"{spec['struct']} must carry the benchmark identity (`{spec['benchmark_field']}`)")
    return (
        f"atp-simulation declares {spec['struct']} with all eight SYS-16 metrics as "
        f"{spec['metric_type']} (Sharpe, Sortino, alpha, beta, max drawdown, annualized return, "
        "annualized volatility, win rate) plus the benchmark_symbol identity"
    )


def check_benchmark(config: dict, src: str) -> str:
    spec = contract_block(config)["benchmark"]
    if not re.search(rf"\bpub\s+struct\s+{re.escape(spec['struct'])}\b", src):
        fail(f"metrics must declare `pub struct {spec['struct']}`")
    for key, label in (
        ("default_const_token", "the SPY default constant"),
        ("spy_fn_token", "the spy() constructor"),
        ("default_impl_token", "the Default-to-SPY impl"),
        ("symbol_validation_token", "the canonical-symbol guard"),
    ):
        if _compact(spec[key]) not in _compact(src):
            fail(
                f"metrics Benchmark must default to SPY and validate a selected symbol: missing "
                f"{label} (`{spec[key]}`)"
            )
    return (
        "atp-simulation Benchmark defaults to SPY (DEFAULT_BENCHMARK_SYMBOL + Default impl) and "
        "rejects a non-canonical selected symbol (SRS-BT-005 / SYS-17 default seeded here)"
    )


def check_config_struct(config: dict, src: str) -> str:
    spec = contract_block(config)["config_struct"]
    body = _compact(_struct_body(src, spec["struct"]))
    if _compact(f"{spec['u32_field']}: u32") not in body:
        fail(f"{spec['struct']} must declare `{spec['u32_field']}: u32` (the annualization factor)")
    if _compact(f"{spec['f64_field']}: f64") not in body:
        fail(f"{spec['struct']} must declare `{spec['f64_field']}: f64` (the risk-free rate)")
    if _compact(spec["default_const_token"]) not in _compact(src):
        fail(f"the default annualization factor must be 252 (`{spec['default_const_token']}`)")
    return (
        f"atp-simulation declares {spec['struct']} with {spec['u32_field']}: u32 and "
        f"{spec['f64_field']}: f64, defaulting the annualization factor to 252"
    )


def check_config_validation(config: dict, src: str) -> str:
    spec = contract_block(config)["config_validation"]
    # Scan the whole compact source rather than _fn_block(src, "new"): two `new`
    # constructors exist (Benchmark::new and MetricsConfig::new), so a body lookup by
    # name is ambiguous; these guard tokens are unique to MetricsConfig::new anyway.
    compact_src = _compact(src)
    for key, label in (
        ("ppy_guard", "a zero annualization factor"),
        ("rf_guard", "an impossible (<= -100%/period) risk-free rate"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"MetricsConfig::{spec['fn']} must fail closed on {label} (`{spec[key]}`)")
    for key in ("ppy_error_token", "rf_error_token"):
        if _compact(spec[key]) not in compact_src:
            fail(f"MetricsConfig::{spec['fn']} must reject with `{spec[key]}`")
    return (
        f"atp-simulation MetricsConfig::{spec['fn']} fails closed on a zero annualization factor "
        f"({spec['ppy_error_token']}) and a non-finite / impossible risk-free rate "
        f"({spec['rf_error_token']})"
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


def check_compute_fn(config: dict, src: str) -> str:
    spec = contract_block(config)["compute_fn"]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['fn'])}\b", src):
        fail(f"metrics must expose `pub fn {spec['fn']}` (the metric-family entry point)")
    signature = src[src.index(f"pub fn {spec['fn']}") :].split("{", 1)[0]
    missing = [p for p in spec["param_tokens"] if p not in signature]
    if missing:
        fail(
            f"`{spec['fn']}` must take {', '.join(spec['param_tokens'])}: missing "
            f"{', '.join(missing)}"
        )
    return (
        f"atp-simulation exposes `pub fn {spec['fn']}` over the equity curve, trade log, benchmark, "
        "benchmark levels, and config -- the single entry point for backtest, paper, and live"
    )


def check_baseline(config: dict, src: str) -> str:
    spec = contract_block(config)["baseline"]
    compact_src = _compact(src)
    if _compact(spec["compute_param_token"]) not in compact_src:
        fail(
            f"compute must take the pre-trade baseline as a REQUIRED input "
            f"(`{spec['compute_param_token']}`) so the first period can never be silently omitted"
        )
    if _compact(spec["period_returns_signature_token"]) not in compact_src:
        fail(
            "period_returns must take the baseline so the first return is "
            "(equity_curve[0] - starting) / starting"
        )
    if _compact(spec["drawdown_peak_token"]) not in compact_src:
        fail(
            f"max drawdown's running peak must start at the baseline "
            f"(`{spec['drawdown_peak_token']}`) so an initial drop below starting equity is captured"
        )
    if _compact(spec["annualized_return_token"]) not in compact_src:
        fail(f"annualized return must run from the baseline (`{spec['annualized_return_token']}`)")
    return (
        "atp-simulation takes the pre-trade baseline as a REQUIRED compute input "
        "(starting_equity_minor): period returns run forward from it, the drawdown peak starts at "
        "it, and annualized return runs from it, so the first period's P&L (including entry costs) "
        "is always captured and never silently omitted"
    )


def check_metric_fns(config: dict, src: str) -> str:
    fns = contract_block(config)["metric_fns"]
    missing = [fn for fn in fns if not re.search(rf"\bfn\s+{re.escape(fn)}\b", src)]
    if missing:
        fail(f"metrics is missing metric computation fn(s): {', '.join(missing)}")
    return (
        f"atp-simulation computes each metric in a dedicated fn ({', '.join(fns)}) so every SYS-16 "
        "metric has a verifiable, separately-tested computation"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"metrics must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- the family must use fixed left-to-right folds with no "
            "parallelism, RNG, or wall-clock read"
        )
    if _compact(spec["monotonic_guard_token"]) not in _compact(src):
        fail(
            f"metrics must reject a non-strictly-increasing timestamp "
            f"(`{spec['monotonic_guard_token']}`) so the period returns are not order-dependent"
        )
    return (
        "atp-simulation metric folds are deterministic: no parallelism / RNG / clock token, and a "
        "non-monotonic timestamp is rejected (NonMonotonicTimestamps), so identical inputs yield "
        "identical metrics (SRS-BT-010)"
    )


def check_fail_closed(config: dict, src: str) -> str:
    spec = contract_block(config)["fail_closed"]
    compact_src = _compact(src)
    for key, label in (
        ("non_positive_equity_token", "reject a non-positive equity mark (divide-by-zero return)"),
        ("non_positive_benchmark_token", "reject a non-positive benchmark level"),
        ("length_mismatch_token", "reject a benchmark series of the wrong length"),
        ("timestamp_mismatch_token", "reject a benchmark mark misaligned by timestamp"),
        ("non_positive_price_token", "reject a non-positive fill price in the win-rate path"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"metrics must {label} (`{spec[key]}`)")
    return (
        "atp-simulation fails closed on a non-positive equity mark, a misaligned or non-positive "
        "benchmark series, and a non-positive fill price; a degenerate input never silently "
        "produces a corrupt metric"
    )


def check_undefined_semantics(config: dict, src: str) -> str:
    spec = contract_block(config)["undefined_semantics"]
    compact_src = _compact(src)
    if _compact(spec["option_token"]) not in compact_src:
        fail(f"metrics must be {spec['option_token']} so an undefined metric is None")
    if _compact(spec["win_rate_none_guard"]) not in compact_src:
        fail(
            f"win rate must be None when no trade closed (`{spec['win_rate_none_guard']}`), not a "
            "fabricated 0.0"
        )
    if _compact(spec["none_token"]) not in compact_src:
        fail(f"a degenerate input must return None (`{spec['none_token']}`)")
    return (
        "atp-simulation reports an undefined metric as None (Option<f64>; win rate None on no closed "
        "trade) rather than a fabricated zero"
    )


def check_nan_guard(config: dict, src: str) -> str:
    spec = contract_block(config)["nan_guard"]
    compact_src = _compact(src)
    if not re.search(rf"\b{re.escape(spec['finite_fn_token'])}\b", src):
        fail(f"metrics must guard a non-finite result with `{spec['finite_fn_token']}`")
    if _compact(spec["is_finite_token"]) not in compact_src:
        fail(f"metrics must verify each result is finite (`{spec['is_finite_token']}`)")
    if _compact(spec["error_token"]) not in compact_src:
        fail(f"a non-finite metric must fail closed with `{spec['error_token']}`")
    return (
        "atp-simulation verifies every computed metric is finite (fn finite + is_finite) and fails "
        "closed (NonFiniteComputation) rather than leaking NaN/inf into a ranking or dashboard"
    )


def check_win_rate(config: dict, src: str) -> str:
    spec = contract_block(config)["win_rate"]
    if not re.search(rf"\bfn\s+{re.escape(spec['fn'])}\b", src):
        fail(f"metrics must compute the win rate in `fn {spec['fn']}`")
    compact_src = _compact(src)
    for key, label in (
        ("round_trip_settle_token", "settle a round trip when the position returns to flat"),
        (
            "cash_flow_token",
            "sum realized cash flow over the round trip (order/fragmentation invariant)",
        ),
        ("win_token", "count a win only on strictly positive NET round-trip P&L"),
        ("flip_token", "split a flip through zero into a close and a reopen"),
        (
            "order_guard_token",
            "reject a backwards-timestamp (reordered) trade log so the win rate is "
            "order-independent",
        ),
        (
            "canonical_symbol_token",
            "canonicalize the fill symbol with the ledger policy (SYS-86 parity)",
        ),
        ("negative_cost_token", "reject a negative transaction-cost component"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"the win rate must {label} (`{spec[key]}`)")
    return (
        "atp-simulation win rate counts COMPLETE flat-to-flat round trips on canonicalized symbols "
        "(fragmentation- and order-invariant: a round trip closed in one fill or in volume-capped "
        "partial fills is the same trade, SYS-86), a win on strictly positive NET round-trip P&L "
        "(NegativeFillCost rejected), rejects a backwards timestamp (NonMonotonicTradeLog), and "
        "applies multiple same-timestamp fills in trade-log (execution) order rather than rejecting "
        "legitimate same-bar fills"
    )


def check_run_coherence(config: dict, src: str) -> str:
    spec = contract_block(config)["run_coherence"]
    compact_src = _compact(src)
    for key, label in (
        ("window_token", "derive the run window from the equity curve's first/last mark"),
        ("guard_token", "reject a fill outside that window"),
        ("error_token", "fail closed on an out-of-run trade log"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"compute must validate the trade log and equity curve describe the SAME run: "
                f"missing the guard to {label} (`{spec[key]}`)"
            )
    return (
        "atp-simulation compute requires every trade-log fill to fall within the equity curve's run "
        "window [first mark ts, last mark ts] (TradeLogOutsideRun), so a stale or mismatched "
        "(asynchronously-snapshotted) trade log cannot be combined with a different run's equity "
        "curve; the full atomic run-snapshot identity is the deferred accumulator's responsibility"
    )


def check_dispersion_tolerance(config: dict, src: str) -> str:
    spec = contract_block(config)["dispersion_tolerance"]
    if not re.search(rf"\b{re.escape(spec['fn_token'])}\b", src):
        fail(
            f"metrics must guard a near-zero dispersion denominator with `{spec['fn_token']}` so a "
            "floating-point-noise dispersion does not produce a spurious enormous ratio"
        )
    compact_src = _compact(src)
    for key, label in (
        ("const_token", "a documented dispersion tolerance constant"),
        ("sharpe_usage_token", "a tolerant denominator check in Sharpe"),
        ("sortino_usage_token", "a tolerant denominator check in Sortino"),
        ("beta_usage_token", "a tolerant denominator check in beta"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"metrics must apply {label} (`{spec[key]}`)")
    return (
        "atp-simulation guards the Sharpe/Sortino/beta denominators with a scale-aware tolerance "
        "(negligible_dispersion / DISPERSION_EPSILON) instead of an exact == 0.0 check, so a "
        "floating-point-noise-level dispersion yields None rather than an enormous, ranking-corrupting "
        "ratio"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    if _compact(spec["benchmark_level_token"]) not in compact_src:
        fail(
            f"benchmark levels must enter as integer minor units (`{spec['benchmark_level_token']}`)"
        )
    if _compact(spec["equity_consume_token"]) not in compact_src:
        fail(f"metrics must consume the integer equity curve (`{spec['equity_consume_token']}`)")
    if _compact(spec["metric_output_token"]) not in compact_src:
        fail(f"metrics must output dimensionless f64 ratios (`{spec['metric_output_token']}`)")
    return (
        "atp-simulation keeps money in integer minor units on input (equity_minor / level_minor: "
        "i64) and outputs the metrics as dimensionless f64 ratios -- the f64 is the metric domain, "
        "not a money-correctness leak"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the metric "
            "family is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- the metric family must be independent of the IB account"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the metric family is broker-independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation metrics module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation metrics module is free of all {len(tokens)} forbidden vendor SDK tokens "
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
                "metric path compiles + passes (install the Rust toolchain)"
            )
        return f"cargo test -p {crate} --test {integration}: skipped (cargo not on PATH)"
    lib = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "metrics", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if lib.returncode != 0:
        fail(f"cargo test -p {crate} --lib metrics failed:\n{lib.stdout}\n{lib.stderr}")
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
        f"cargo test -p {crate} --lib metrics + {integration}: PASS "
        "(computes the eight metrics from a real BacktestResult, is deterministic across repeated "
        "runs, defaults the benchmark to SPY, reports undefined metrics as None, and fails closed on "
        "a degenerate curve / misaligned benchmark)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "metrics" reads metrics.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("metrics_struct", check_metrics_struct, "metrics"),
    ("benchmark", check_benchmark, "metrics"),
    ("config_struct", check_config_struct, "metrics"),
    ("config_validation", check_config_validation, "metrics"),
    ("error_enum", check_error_enum, "metrics"),
    ("compute_fn", check_compute_fn, "metrics"),
    ("baseline", check_baseline, "metrics"),
    ("metric_fns", check_metric_fns, "metrics"),
    ("determinism", check_determinism, "metrics"),
    ("fail_closed", check_fail_closed, "metrics"),
    ("undefined_semantics", check_undefined_semantics, "metrics"),
    ("nan_guard", check_nan_guard, "metrics"),
    ("dispersion_tolerance", check_dispersion_tolerance, "metrics"),
    ("win_rate", check_win_rate, "metrics"),
    ("run_coherence", check_run_coherence, "metrics"),
    ("numeric_boundary", check_numeric_boundary, "metrics"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "metrics"),
)

_DEFERRED_OWNERS = (
    "live dashboard performance reporting (SRS-UI / SRS-API; SYS-36 <= 5s refresh)",
    "paper/live runtime metric accumulators (the SRS-SIM-004 snapshot metrics slot)",
    "SRS-BT-005 benchmark resolution + report identification (SYS-17, SYS-37)",
    "SRS-BT-006 factor analysis / tear-sheet (atp-factor-pipeline)",
)


def assert_sim_metrics_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "metrics": metrics_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_sim_metrics_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-004 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable metric path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except MetricsCheckError as error:
        print(f"SRS-BT-004 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-004 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-004 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
