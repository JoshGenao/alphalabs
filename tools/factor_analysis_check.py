#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-006 (produce factor analysis and tear-sheet
outputs).

SRS-BT-006 (SyRS SYS-18; StRS SN-1.05). The acceptance criterion: "Factor returns,
information coefficient, and turnover analysis are available for completed factor-analysis
runs."

The deterministic, dependency-free factor-analysis surface lives in
``crates/atp-factor-pipeline`` (module ``factor_analysis``), per the structural contract in
``architecture/runtime_services.json`` (block ``factor_analysis_contract``). One entry
point, ``compute_tear_sheet``, consumes a ``FactorPanel`` -- a per-rebalance-period panel of
``(SecurityKey, factor value, forward return)`` observations plus a quantile count -- and
returns one ``FactorTearSheet`` bundling the three deliverables SYS-18 names:

  (a) ``InformationCoefficient`` -- the per-period Spearman rank correlation between factor
      value and forward return (``average_ranks`` average tie ranks, ``pearson`` of the
      ranks, clamped to the ``[-1, 1]`` domain) plus the mean / std / risk-adjusted IC. A
      period whose factor values or returns have zero rank dispersion carries ``None`` (the
      ``per_period: Vec<(u64, Option<f64>)>`` series), never a fabricated zero.
  (b) ``FactorReturns`` -- the per-period quantile-sorted mean returns
      (``per_quantile_mean``) plus the top-minus-bottom long-short ``spread_per_period``
      series with its ``mean_spread`` and compounded ``cumulative_spread``.
  (c) ``TurnoverAnalysis`` -- the per-period top/bottom-quantile membership churn
      (``top_turnover`` / ``bottom_turnover``) with ``mean_top`` / ``mean_bottom``.

The surface is fail-closed at the trust boundary (``FactorPanel::validate`` rejects an empty
panel, an invalid quantile count, a period with fewer securities than quantiles, a duplicate
security, non-monotonic periods, and a non-finite input), the work is deterministic (fixed
left-to-right folds, cross-sections sorted by the total order ``(factor_value, SecurityKey)``,
no parallelism / RNG / clock -- the SRS-BT-010 criterion), factor values and returns are
dimensionless ``f64`` (the factor domain, not a money leak), and every computed aggregate is
verified finite (``FactorAnalysisError::NonFiniteComputation``). ``atp-factor-pipeline`` adds
no broker/adapter/simulation dependency and carries no vendor SDK token; ``lib.rs``
re-exports ``pub mod factor_analysis;``.

The PASS line is ``SRS-BT-006 SDK-SURFACE PASS`` -- it names the deferred owners (the
scheduled full-universe factor job via SRS-FAC-001, the real factor/return data wiring via
SRS-DATA-007, the operator tear-sheet rendering via SRS-UI / SRS-API, and the cross-crate
SRS-BT-004 metrics bundle) so the partial-pass status (feature_list.json keeps
``passes:false``) is loud.

Mirrors the PASS/FAIL output style of ``tools/benchmark_check.py``.

Invoke:
    python3 tools/factor_analysis_check.py
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


class FactorAnalysisCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise FactorAnalysisCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "factor_analysis_contract" not in config:
        fail("architecture metadata is missing factor_analysis_contract")
    return config["factor_analysis_contract"]


def module_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["factor_pipeline_crate"]["path"] / "src" / f"{block['module']}.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def lib_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["factor_pipeline_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def cargo_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["factor_pipeline_crate"]["path"] / block["no_broker_dependency"]["cargo_toml"]
    )
    if not source_path.exists():
        fail(f"source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def _compact(text: str) -> str:
    """Strip all whitespace so rustfmt line-wrapping cannot hide a token."""
    return re.sub(r"\s+", "", text)


def _check_struct_fields(src: str, spec: dict, label: str) -> None:
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f"{f}:") not in body]
    if missing:
        fail(f"{spec['struct']} ({label}) is missing fields: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_observation(config: dict, src: str) -> str:
    spec = contract_block(config)["observation"]
    _check_struct_fields(src, spec, "factor observation")
    return (
        "atp-factor-pipeline declares FactorObservation (security: SecurityKey, factor_value, "
        "forward_return) -- one security's factor score and realized forward return for a period"
    )


def check_period(config: dict, src: str) -> str:
    spec = contract_block(config)["period"]
    _check_struct_fields(src, spec, "factor period")
    return "atp-factor-pipeline declares FactorPeriod (ts + the cross-section of observations)"


def check_panel(config: dict, src: str) -> str:
    spec = contract_block(config)["panel"]
    _check_struct_fields(src, spec, "factor panel")
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['validate_fn'])}\b", src):
        fail(f"FactorPanel must expose a fail-closed `pub fn {spec['validate_fn']}`")
    return (
        "atp-factor-pipeline declares FactorPanel (periods + quantiles) with a fail-closed "
        "validate() trust boundary"
    )


def check_information_coefficient(config: dict, src: str) -> str:
    spec = contract_block(config)["information_coefficient"]
    body = _compact(_struct_body(src, spec["struct"]))
    missing = [f for f in spec["fields"] if _compact(f"{f}:") not in body]
    if missing:
        fail(f"{spec['struct']} is missing fields: {', '.join(missing)}")
    option_missing = [
        f for f in spec["option_ratio_fields"] if _compact(f"{f}:{spec['ratio_type']}") not in body
    ]
    if option_missing:
        fail(
            f"{spec['struct']} must report each IC summary as {spec['ratio_type']} so an undefined "
            f"statistic is None, not a fabricated zero: missing {', '.join(option_missing)}"
        )
    if _compact(spec["per_period_option_token"]) not in body:
        fail(
            f"{spec['struct']}.per_period must carry the per-period IC as `Option<f64>` "
            f"(`{spec['per_period_option_token']}`) so a zero-dispersion period is None, not zero"
        )
    return (
        "atp-factor-pipeline declares InformationCoefficient: the per-period Spearman IC as "
        "Vec<(u64, Option<f64>)> (None on zero rank dispersion) plus mean / std / risk-adjusted IC "
        "as Option<f64> (undefined is None, never a fabricated zero)"
    )


def check_factor_returns(config: dict, src: str) -> str:
    spec = contract_block(config)["factor_returns"]
    _check_struct_fields(src, spec, "factor returns")
    if _compact(spec["spread_option_token"]) not in _compact(_struct_body(src, spec["struct"])):
        fail(
            f"FactorReturns.spread_per_period must carry the spread as `Option<f64>` "
            f"(`{spec['spread_option_token']}`) so a period whose factor does not separate the "
            "extremes is None, not a fabricated SecurityKey-driven spread"
        )
    return (
        "atp-factor-pipeline declares FactorReturns: per-period quantile mean returns plus the "
        "top-minus-bottom long-short spread as Option<f64> (None when the factor does not separate "
        "the extremes) and its arithmetic mean_spread -- only horizon-agnostic statistics; the "
        "compounded cumulative spread is deferred (the panel cannot validate non-overlapping "
        "forward windows)"
    )


def check_turnover(config: dict, src: str) -> str:
    spec = contract_block(config)["turnover"]
    _check_struct_fields(src, spec, "turnover analysis")
    if _compact(spec["turnover_option_token"]) not in _compact(_struct_body(src, spec["struct"])):
        fail(
            f"TurnoverAnalysis.top_turnover must carry the turnover as `Option<f64>` "
            f"(`{spec['turnover_option_token']}`) so churn that is not factor-driven is None"
        )
    if _compact(spec["weight_turnover_token"]) not in _compact(src):
        fail(
            f"target turnover must be WEIGHT-BASED (`{spec['weight_turnover_token']}`: half the "
            "L1 distance between the equal-weight target quantile portfolios) so a shrinking universe "
            "-- which reweights the retained names -- is not understated by a set-membership ratio"
        )
    return (
        "atp-factor-pipeline declares TurnoverAnalysis: per-period top/bottom-quantile TARGET "
        "turnover as Option<f64> (None when not factor-driven), measured as half the L1 distance "
        "between the equal-weight target portfolios (the factor-signal turnover; realized "
        "return-driven drift turnover is the deferred backtest engine's), with means over the "
        "defined values"
    )


def check_separation(config: dict, src: str) -> str:
    spec = contract_block(config)["separation"]
    compact_src = _compact(src)
    if _compact(spec["predicate_token"]) not in compact_src:
        fail(
            f"factor_analysis must compute the extreme-separation predicate "
            f"(`{spec['predicate_token']}`) so a constant or cutoff-tied factor cannot attribute a "
            "SecurityKey-driven spread to the factor"
        )
    if _compact(spec["spread_gate_token"]) not in compact_src:
        fail(
            f"the spread must be gated on the separation predicate (`{spec['spread_gate_token']}`) "
            "so it is withheld (None) when the factor does not separate the extremes"
        )
    if _compact(spec["inner_cutoff_token"]) not in compact_src:
        fail(
            f"the separation predicate must check BOTH bounding cutoffs (`{spec['inner_cutoff_token']}`), "
            "not just extreme-to-extreme -- for 3+ quantiles a tie can straddle an inner cutoff and "
            "decide the bottom/top composition by SecurityKey while the extremes look separated"
        )
    return (
        "atp-factor-pipeline gates the spread and turnover on a strict extreme-separation predicate "
        "(separates_extremes: BOTH the q0|q1 and q(Q-2)|q(Q-1) bounding cutoffs untied), so a "
        "constant or cutoff-tied factor (incl. an inner-cutoff tie at 3+ quantiles) reports None "
        "rather than a fabricated SecurityKey-driven spread/turnover"
    )


def check_tear_sheet(config: dict, src: str) -> str:
    spec = contract_block(config)["tear_sheet"]
    _check_struct_fields(src, spec, "tear sheet")
    return (
        "atp-factor-pipeline declares FactorTearSheet bundling the IC, factor-return, and turnover "
        "analyses for one completed run (the SRS-BT-006 deliverable)"
    )


def check_compute_fn(config: dict, src: str) -> str:
    spec = contract_block(config)["compute_fn"]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(spec['fn'])}\b", src):
        fail(f"factor_analysis must expose `pub fn {spec['fn']}` (the tear-sheet entry point)")
    signature = _compact(src[src.index(f"pub fn {spec['fn']}") :].split("{", 1)[0])
    missing = [p for p in spec["param_tokens"] if _compact(p) not in signature]
    if missing:
        fail(f"`{spec['fn']}` must take {', '.join(spec['param_tokens'])}: missing {missing}")
    if _compact(spec["return_token"]) not in signature:
        fail(f"`{spec['fn']}` must return `{spec['return_token']}`")
    return (
        "atp-factor-pipeline exposes `pub fn compute_tear_sheet(panel: &FactorPanel) -> "
        "Result<FactorTearSheet, FactorAnalysisError>` -- the single SRS-BT-006 entry point"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-factor-pipeline declares {spec['enum']} with {len(spec['variants'])} fail-closed "
        f"variants ({', '.join(spec['variants'])})"
    )


def check_spearman(config: dict, src: str) -> str:
    spec = contract_block(config)["spearman"]
    compact_src = _compact(src)
    for key, label in (
        ("rank_fn", "average tie ranks"),
        ("pearson_fn", "Pearson correlation of the ranks"),
    ):
        if not re.search(rf"\bfn\s+{re.escape(spec[key])}\b", src):
            fail(f"factor_analysis must compute {label} via `fn {spec[key]}`")
    if _compact(spec["clamp_token"]) not in compact_src:
        fail(
            f"the per-period IC must be clamped to its [-1, 1] domain (`{spec['clamp_token']}`) so "
            "floating-point overflow cannot leak an out-of-domain correlation"
        )
    return (
        "atp-factor-pipeline computes the IC as Spearman = Pearson of average tie ranks "
        "(average_ranks + pearson), clamped to its [-1, 1] domain"
    )


def check_trust_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["trust_boundary"]
    compact_src = _compact(src)
    for key, label in (
        ("empty_guard", "reject an empty panel"),
        ("quantile_guard", "reject a quantile count below 2"),
        ("insufficient_guard", "reject a period with fewer securities than quantiles"),
        ("duplicate_guard", "reject a security appearing twice in one period"),
        ("monotonic_guard", "reject non-strictly-increasing period timestamps"),
        ("finite_input_guard", "reject a non-finite factor value or return"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"FactorPanel::validate must {label} (`{spec[key]}`)")
    return (
        "atp-factor-pipeline FactorPanel::validate fails closed at the trust boundary: empty panel, "
        "invalid quantile count, insufficient securities, duplicate security, non-monotonic periods, "
        "and non-finite inputs are each rejected before any statistic is computed"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"factor_analysis must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- it must use fixed left-to-right folds with no parallelism, RNG, "
            "or wall-clock read"
        )
    return (
        "atp-factor-pipeline factor analysis is deterministic: no parallelism / RNG / clock token, "
        "cross-sections sorted by the total order (factor_value, SecurityKey), so identical inputs "
        "yield bit-identical tear-sheets (SRS-BT-010)"
    )


def check_nan_guard(config: dict, src: str) -> str:
    spec = contract_block(config)["nan_guard"]
    compact_src = _compact(src)
    if not re.search(rf"\b{re.escape(spec['finite_fn_token'])}\b", src):
        fail(f"factor_analysis must guard a non-finite statistic with `{spec['finite_fn_token']}`")
    if _compact(spec["is_finite_token"]) not in compact_src:
        fail(f"factor_analysis must verify each aggregate is finite (`{spec['is_finite_token']}`)")
    if _compact(spec["error_token"]) not in compact_src:
        fail(f"a non-finite statistic must fail closed with `{spec['error_token']}`")
    if _compact(spec["quantile_mean_guard_token"]) not in compact_src:
        fail(
            f"every quantile mean must pass through finite() before leaving the function "
            f"(`{spec['quantile_mean_guard_token']}`) -- a middle-bucket mean can overflow to inf on "
            "finite inputs and never reaches the spread guard"
        )
    return (
        "atp-factor-pipeline verifies every computed aggregate AND every quantile mean is finite "
        '(fn finite + is_finite, incl. finite("quantile_mean")) and fails closed '
        "(NonFiniteComputation) rather than leaking NaN/inf into a ranking or tear-sheet"
    )


def check_numeric_boundary(config: dict, src: str) -> str:
    spec = contract_block(config)["numeric_boundary"]
    compact_src = _compact(src)
    for key, label in (
        ("factor_input_token", "factor value"),
        ("return_input_token", "forward return"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"the {label} must be a dimensionless f64 (`{spec[key]}`)")
    return (
        "atp-factor-pipeline keeps factor scores and forward returns as dimensionless f64 "
        "(factor_value: f64, forward_return: f64) -- the f64 is the factor domain, not a money leak"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-factor-pipeline lib.rs must re-export `{spec['lib_reexport_token']}` so the "
            "factor-analysis surface is part of the factor pipeline runtime"
        )
    return f"atp-factor-pipeline lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-factor-pipeline Cargo.toml must NOT depend on the live/broker/simulation path: "
            f"found {', '.join(leaked)} -- the factor-analysis surface is self-contained over the "
            "data layer"
        )
    return (
        f"atp-factor-pipeline Cargo.toml declares no dependency on the live/broker/simulation path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the factor-analysis surface is independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-factor-pipeline factor_analysis module leaks vendor SDK token(s): "
            f"{', '.join(leaked)} (the core engine must isolate vendors behind adapters per "
            "SRS-ARCH-003)"
        )
    return (
        f"atp-factor-pipeline factor_analysis module is free of all {len(tokens)} forbidden vendor "
        "SDK tokens (SRS-ARCH-003 adapter isolation)"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["factor_pipeline_crate"]["crate"]
    integration = block["rust_integration_test"]
    module = block["module"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "factor-analysis path compiles + passes (install the Rust toolchain)"
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
        "(computes the per-period Spearman IC, quantile factor-return spread, and quantile turnover; "
        "is deterministic; and fails closed on an empty / degenerate / non-finite panel)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "module" reads factor_analysis.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("observation", check_observation, "module"),
    ("period", check_period, "module"),
    ("panel", check_panel, "module"),
    ("information_coefficient", check_information_coefficient, "module"),
    ("factor_returns", check_factor_returns, "module"),
    ("turnover", check_turnover, "module"),
    ("tear_sheet", check_tear_sheet, "module"),
    ("compute_fn", check_compute_fn, "module"),
    ("error_enum", check_error_enum, "module"),
    ("spearman", check_spearman, "module"),
    ("separation", check_separation, "module"),
    ("trust_boundary", check_trust_boundary, "module"),
    ("determinism", check_determinism, "module"),
    ("nan_guard", check_nan_guard, "module"),
    ("numeric_boundary", check_numeric_boundary, "module"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "module"),
)

_DEFERRED_OWNERS = (
    "scheduled full-universe factor job producing the panel (SRS-FAC-001)",
    "real factor-value / forward-return data wiring (SRS-DATA-007 unified historical interface)",
    "operator factor tear-sheet rendering (SRS-UI / SRS-API)",
    "cross-crate bundle with the SRS-BT-004 PerformanceMetrics family into one report",
    "a validated compounded cumulative spread (needs each period's forward-return horizon to "
    "compound only non-overlapping windows; the panel has a start timestamp only)",
    "realized return-driven drift turnover (needs holding-period returns + a rebalancing "
    "convention -- the deferred backtest engine; the turnover here is factor-signal/target turnover)",
)


def assert_factor_analysis_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "module": module_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    return [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_factor_analysis_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-006 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable factor path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except FactorAnalysisCheckError as error:
        print(f"SRS-BT-006 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-006 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-006 passes:false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
