#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-007 (grid search / parameter sweeps).

SRS-BT-007 (SyRS SYS-19; StRS SN-1.16). The acceptance criterion: "A parameter space
definition produces ranked backtest results by the selected objective function."

The sweep surface lives in ``crates/atp-simulation`` (module ``sweep``), per the
structural contract in ``architecture/runtime_services.json`` (block
``sim_parameter_sweep_contract``). Each AC noun is a named artifact:

  (a) ``ParameterSpace`` (the parameter space definition): validated ``ParameterAxis``
      dimensions -- fail-closed on zero axes, empty/duplicate axis names, empty value
      lists, empty value tokens, and duplicate values within an axis -- enumerated as a
      deterministic Cartesian product of canonical ``StrategyParameters`` points (the
      SRS-BT-009 parameter-set identity). Cardinality is bounded: ``checked_mul`` in
      ``u128`` against ``MAX_SWEEP_POINTS``, enforced BEFORE materialization.
  (b) ``ObjectiveFunction`` (the selected objective function): an allowlist-parsed
      ``ObjectiveMetric`` over the full eight-metric SRS-BT-004 / SYS-16 family plus an
      explicit ``Direction`` (max|min; no per-metric default direction), with the two
      SYS-19 named conveniences (maximize Sharpe, minimize max drawdown).
  (c) ``SweepStrategyFactory``: the fail-closed bridge from one parameter point to a
      configured strategy (missing / unknown / unparseable parameters abort -- never a
      silent default run misattributed to the labeled point). The real implementor is
      the deferred Python strategy host; SRS-BT-008 walk-forward reuses the seam.
  (d) ``SweepRunner::run`` + ``SweepReport`` (the ranked backtest results): every point
      evaluated sequentially through the SAME shipped ``BacktestEngine`` +
      ``benchmark::compare`` chain, ranked best-first via ``f64::total_cmp`` with
      canonical-parameter tie-breaks; an undefined objective routes the point to the
      ``unranked`` bucket (``ObjectiveUndefined`` -- never fabricated, never ranked
      last, never dropped: ``total_points`` proves the accounting); any per-point
      failure aborts the whole sweep naming the point (``PointFailed``).
  (e) the work is deterministic (no parallelism / RNG / clock; SRS-BT-010); ``sweep``
      adds no broker/adapter dependency and carries no vendor SDK token; ``lib.rs``
      re-exports ``pub mod sweep;``; ``bt007_sweep_cli`` is the operator surface.

The PASS line is ``SRS-BT-007 SDK-SURFACE PASS`` -- it names the deferred owners (the
real Python-strategy factory via the deferred strategy host, the REST/dashboard sweep
surface via SRS-API-001 / SRS-UI, the real stored-data benchmark resolver via
SRS-BT-005, the SRS-BT-008 walk-forward consumer, and sweep-point persistence via the
SRS-BT-009 consumer boundary).

Mirrors the PASS/FAIL output style of ``tools/backtest_store_check.py``.

Invoke:
    python3 tools/backtest_sweep_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class BacktestSweepCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise BacktestSweepCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_parameter_sweep_contract" not in config:
        fail("architecture metadata is missing sim_parameter_sweep_contract")
    return config["sim_parameter_sweep_contract"]


def sweep_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["simulation_crate"]["path"] / "src" / f"{block['sweep_module']}.rs"
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
    source_path = (
        root / block["simulation_crate"]["path"] / "src" / "bin" / f"{block['cli']['bin']}.rs"
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


def check_space_types(config: dict, src: str) -> str:
    spec = contract_block(config)["space_types"]
    for key, label in (
        ("axis_struct", "the validated axis type"),
        ("space_struct", "the parameter space definition"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"sweep must declare `pub struct {spec[key]}` ({label})")
    compact_src = _compact(src)
    for key, label in (
        ("points_fn", "the Cartesian-product enumerator"),
        ("point_count_fn", "the cardinality accessor"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"ParameterSpace must expose {label} (`{spec[key]}`)")
    if _compact(spec["point_identity_import_token"]) not in compact_src:
        fail(
            "sweep points must BE the SRS-BT-009 StrategyParameters identity "
            f"(`{spec['point_identity_import_token']}`) so a sweep point is queryable in "
            "backtest history by exactly the axis that produced it -- not a divergent "
            "point shape"
        )
    return (
        "atp-simulation declares ParameterAxis/ParameterSpace (the AC's 'parameter space "
        "definition') whose points() enumerates a deterministic Cartesian product of canonical "
        "StrategyParameters -- the SRS-BT-009 parameter-set identity, so every sweep point is "
        "queryable in backtest history"
    )


def check_space_validation(config: dict, src: str) -> str:
    spec = contract_block(config)["space_validation"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    for key, label in (
        ("empty_space_variant", "zero axes"),
        ("empty_axis_name_variant", "an empty axis name"),
        ("duplicate_axis_variant", "a duplicate axis name"),
        ("empty_axis_values_variant", "an axis with no values"),
        ("empty_axis_value_variant", "an empty value token"),
        ("duplicate_axis_value_variant", "a duplicate value within an axis"),
    ):
        if not re.search(rf"\b{re.escape(spec[key])}\b", body):
            fail(
                f"{enum_name} must fail closed on {label} (`{spec[key]}`) -- a degenerate space "
                "definition must never silently enumerate"
            )
    compact_src = _compact(src)
    # Each guard must actually be RAISED somewhere (an `Err(SweepError::...)`
    # construction), not just declared in the enum / matched in Display.
    for key in spec:
        if _compact(f"Err(SweepError::{spec[key]}") not in compact_src:
            fail(
                f"`SweepError::{spec[key]}` must be raised by the space validation path "
                "(an Err construction), not merely declared"
            )
    return (
        "atp-simulation ParameterAxis/ParameterSpace validation is fail-closed variant by "
        "variant: zero axes, empty/duplicate axis names, empty value lists, empty value tokens, "
        "and duplicate values within an axis are each rejected with their exact SweepError "
        "(identical enumerated points would make ranking ambiguous)"
    )


def check_point_cap(config: dict, src: str) -> str:
    spec = contract_block(config)["point_cap"]
    compact_src = _compact(src)
    for key, label in (
        ("cap_const", "a named cardinality cap"),
        ("checked_mul_token", "overflow-checked cardinality arithmetic"),
        ("cap_error_variant", "a fail-closed too-many-points error"),
        ("cap_before_materialize_token", "the cap enforced before materialization"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"sweep must provide {label} (`{spec[key]}`)")
    # The cap check must precede the materialization loop in points(): the return Err
    # must appear BEFORE the Vec push in the source.
    cap_pos = src.find(spec["cap_before_materialize_token"])
    push_pos = src.find("points.push(")
    if cap_pos == -1 or push_pos == -1 or cap_pos > push_pos:
        fail(
            "the TooManyPoints cap must fire BEFORE any point is materialized "
            "(the cap check must precede the enumeration loop in points())"
        )
    return (
        "atp-simulation bounds every sweep: point_count() computes the axis-size product with "
        "checked_mul in u128 and points() fails closed with TooManyPoints against the cap "
        "(MAX_SWEEP_POINTS default) BEFORE materializing a single point or running a single "
        "backtest"
    )


def check_objective(config: dict, src: str) -> str:
    spec = contract_block(config)["objective"]
    for key, label in (
        ("metric_enum", "the objective metric selector"),
        ("direction_enum", "the objective direction"),
        ("function_struct", "the selected objective function"),
    ):
        token = spec[key]
        if not re.search(rf"\bpub\s+(?:enum|struct)\s+{re.escape(token)}\b", src):
            fail(f"sweep must declare `{token}` ({label})")
    compact_src = _compact(src)
    if _compact(spec["metric_allowlist_fn"]) not in compact_src:
        fail(f"ObjectiveMetric must parse via an allowlist (`{spec['metric_allowlist_fn']}`)")
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    for key, label in (
        ("unknown_metric_variant", "an unknown metric token"),
        ("unknown_direction_variant", "an unknown direction token"),
    ):
        if not re.search(rf"\b{re.escape(spec[key])}\b", body):
            fail(f"{enum_name} must fail closed on {label} (`{spec[key]}`)")
    for convenience in spec["syrs_named_conveniences"]:
        if _compact(convenience) not in compact_src:
            fail(f"the SYS-19 named objectives must be first-class conveniences (`{convenience}`)")
    missing = [t for t in spec["metric_tokens"] if f'"{t}"' not in src]
    if missing:
        fail(
            "the objective selector must cover the full eight-metric SYS-16 family: missing "
            f"token(s) {', '.join(missing)}"
        )
    return (
        "atp-simulation declares ObjectiveFunction (the AC's 'selected objective function'): an "
        "allowlist-parsed ObjectiveMetric over all eight SYS-16 metrics plus an explicit "
        "max|min Direction, with the two SYS-19 named conveniences (maximize_sharpe, "
        "minimize_max_drawdown); unknown tokens fail closed"
    )


def check_factory_seam(config: dict, src: str) -> str:
    spec = contract_block(config)["factory_seam"]
    if not re.search(rf"\bpub\s+trait\s+{re.escape(spec['trait'])}\b", src):
        fail(f"sweep must declare the strategy bridge `pub trait {spec['trait']}`")
    if _compact(spec["build_fn"]) not in _compact(src):
        fail(f"{spec['trait']} must expose `{spec['build_fn']}`")
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    for variant in spec["fail_closed_variants"]:
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(
                f"{enum_name} must give the factory a fail-closed vocabulary (`{variant}`) -- a "
                "point the strategy cannot interpret must never silently run with defaults"
            )
    return (
        "atp-simulation declares SweepStrategyFactory -- the fail-closed bridge from one "
        "StrategyParameters point to a configured BacktestStrategy (missing/unknown/unparseable "
        "parameters abort, never a silent default run) -- the seam the deferred Python strategy "
        "host implements and SRS-BT-008 walk-forward reuses"
    )


def check_runner_reuse(config: dict, src: str) -> str:
    spec = contract_block(config)["runner"]
    compact_src = _compact(src)
    for key, label in (
        ("runner_struct", "the sweep orchestrator"),
        ("run_fn", "the run entry point"),
        ("request_struct", "the sweep request"),
        ("evaluation_struct", "the grouped evaluation dependencies"),
        ("test_seam_fn", "the cap test seam"),
    ):
        token = spec[key]
        pattern = (
            rf"\bpub\s+struct\s+{re.escape(token)}\b"
            if key in ("runner_struct", "request_struct", "evaluation_struct")
            else None
        )
        if pattern is not None:
            if not re.search(pattern, src):
                fail(f"sweep must declare `{token}` ({label})")
        elif _compact(token) not in compact_src:
            fail(f"sweep must expose {label} (`{token}`)")
    for key, label in (
        ("engine_reuse_token", "the shipped BacktestEngine"),
        ("compare_reuse_token", "the shipped benchmark::compare producer"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(
                f"SweepRunner must reuse {label} (`{spec[key]}`) -- a sweep result must be "
                "exactly what a standalone run of that point would report, never a parallel "
                "re-implementation"
            )
    return (
        "atp-simulation SweepRunner::run evaluates every point through the SAME shipped "
        "BacktestEngine::run + benchmark::compare chain a single backtest uses (no parallel "
        "re-implementation), sequentially in deterministic enumeration order"
    )


def check_objective_ranking(config: dict, src: str) -> str:
    spec = contract_block(config)["ranking"]
    for key, label in (
        ("report_struct", "the ranked results report"),
        ("ranked_struct", "the ranked row"),
        ("unranked_struct", "the unranked row"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"sweep must declare `pub struct {spec[key]}` ({label})")
    compact_src = _compact(src)
    if _compact(spec["total_order_token"]) not in compact_src:
        fail(
            f"ranking must sort via `{spec['total_order_token']}` -- a total order over f64, so "
            "the sort can never be undermined by a partial-order comparison"
        )
    if _compact(spec["direction_match_token"]) not in compact_src:
        fail(
            f"ranking must be direction-driven (`{spec['direction_match_token']}`) so "
            "Maximize/Minimize genuinely invert the order"
        )
    if _compact(spec["tie_break_token"]) not in compact_src:
        fail(
            f"equal objective values must tie-break on the canonical parameter entries "
            f"(`{spec['tie_break_token']}`) so the ranking is deterministic (SRS-BT-010)"
        )
    if not re.search(rf"\bpub\s+{re.escape(spec['accounting_field'])}\b", src):
        fail(
            f"SweepReport must carry `{spec['accounting_field']}` so every enumerated point is "
            "provably accounted for (ranked + unranked)"
        )
    return (
        "atp-simulation SweepReport is the AC's 'ranked backtest results': best-first via "
        "f64::total_cmp (direction-driven), ties broken by canonical parameter entries, 1-based "
        "positional ranks, and total_points proving ranked + unranked accounts for every point"
    )


def check_none_fail_closed(config: dict, src: str) -> str:
    spec = contract_block(config)["none_fail_closed"]
    compact_src = _compact(src)
    if not re.search(rf"\b{re.escape(spec['unranked_reason_variant'])}\b", src):
        fail(
            f"sweep must declare the `{spec['unranked_reason_variant']}` unranked reason -- an "
            "undefined objective is routed, never fabricated"
        )
    if _compact(spec["unranked_route_token"]) not in compact_src:
        fail(
            f"a None objective must route the point to the unranked bucket "
            f"(`{spec['unranked_route_token']}`) -- never a fabricated stand-in value, never "
            "ranked last, never dropped"
        )
    if "unwrap_or(0.0)" in src or "unwrap_or_default()" in src:
        fail(
            "the objective extraction must not fabricate a stand-in for an undefined metric "
            "(found an unwrap_or fallback) -- undefined routes to unranked"
        )
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    if not re.search(rf"\b{re.escape(spec['non_finite_variant'])}\b", body):
        fail(
            f"{enum_name} must fail closed on a non-finite extracted objective "
            f"(`{spec['non_finite_variant']}`) -- defense-in-depth over compare()'s guarantee"
        )
    return (
        "atp-simulation routes an undefined (None) objective to the unranked bucket with "
        "ObjectiveUndefined -- never a fabricated 0, never ranked last, never dropped -- and "
        "fails closed on a non-finite extracted objective (NonFiniteObjective)"
    )


def check_point_failure(config: dict, src: str) -> str:
    spec = contract_block(config)["point_failure"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    if not re.search(rf"\b{re.escape(spec['variant'])}\b", body):
        fail(f"{enum_name} must declare `{spec['variant']}` (whole-sweep abort on a bad point)")
    if _compact(spec["names_point_token"]) not in _compact(src):
        fail(
            f"a per-point failure must NAME the offending point (`{spec['names_point_token']}`) "
            "so the operator can fix the space, and the sweep must abort rather than emit a "
            "partial ranking"
        )
    return (
        "atp-simulation aborts the WHOLE sweep on any per-point failure (factory rejection, "
        "engine error, benchmark error) with PointFailed naming the offending point -- a partial "
        "ranking could silently mis-rank, and SRS-BT-008 needs all-or-error reproducibility"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed "
        "variants covering space validation, the cardinality cap, objective parsing, factory "
        "rejection, point failure, and non-finite defense"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"sweep must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- points are evaluated sequentially with no parallelism, "
            "RNG, or wall-clock read"
        )
    return (
        "atp-simulation sweep is deterministic: no parallelism / RNG / clock token, so identical "
        "inputs produce an identical ranked report (SRS-BT-010)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the sweep "
            "surface is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- a parameter sweep must be independent of the IB account"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the sweep surface is broker-independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation sweep module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation sweep module is free of all {len(tokens)} forbidden vendor SDK tokens "
        "(SRS-ARCH-003 adapter isolation)"
    )


def check_cli_surface(config: dict, cargo_text: str, cli_src: str) -> str:
    spec = contract_block(config)["cli"]
    if spec["cargo_bin_token"] not in cargo_text:
        fail(
            f"atp-simulation Cargo.toml must register the operator binary "
            f"(`{spec['cargo_bin_token']}`)"
        )
    if spec["explicit_direction_token"] not in cli_src:
        fail(
            "the CLI must refuse to guess an objective direction: an explicit --objective "
            f"requires an explicit --direction (`{spec['explicit_direction_token']}`) -- a "
            "guessed direction could silently invert a ranking"
        )
    if _compact(spec["kv_control_char_guard"]) not in _compact(cli_src):
        fail(
            f"the kv machine format must fail closed on control characters "
            f"(`{spec['kv_control_char_guard']}`) so a parameter value can never forge a proof "
            "line"
        )
    return (
        "atp-simulation registers the bt007_sweep_cli operator binary: repeatable --axis flags "
        "define the space, an explicit --objective requires an explicit --direction (never "
        "guessed), and the kv machine grammar fails closed on control characters"
    )


def check_cargo_test_smoke(config: dict, require_cargo: bool = False) -> str:
    block = contract_block(config)
    crate = block["simulation_crate"]["crate"]
    integration = block["rust_integration_test"]
    cli_test = block["rust_cli_test"]
    cargo = shutil.which("cargo")
    if cargo is None:
        if require_cargo:
            fail(
                f"cargo not on PATH but --require-cargo set: cannot verify the runnable {crate} "
                "sweep path compiles + passes (install the Rust toolchain)"
            )
        return (
            f"cargo test -p {crate} --test {integration} + {cli_test}: skipped (cargo not on PATH)"
        )
    for test in (integration, cli_test):
        run = subprocess.run(
            [cargo, "test", "-p", crate, "--test", test, "--quiet"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if run.returncode != 0:
            fail(f"cargo test -p {crate} --test {test} failed:\n{run.stdout}\n{run.stderr}")
    return (
        f"cargo test -p {crate} --test {integration} + {cli_test}: PASS (deterministic Cartesian "
        "enumeration, hand-verified ranking under both SYS-19 named objectives, undefined "
        "objectives unranked not fabricated, cap fires before any run, per-point failures abort "
        "naming the point, and the CLI round-trips the whole workflow across a process boundary)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "sweep" reads sweep.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("space_types", check_space_types, "sweep"),
    ("space_validation", check_space_validation, "sweep"),
    ("point_cap", check_point_cap, "sweep"),
    ("objective", check_objective, "sweep"),
    ("factory_seam", check_factory_seam, "sweep"),
    ("runner_reuse", check_runner_reuse, "sweep"),
    ("objective_ranking", check_objective_ranking, "sweep"),
    ("none_fail_closed", check_none_fail_closed, "sweep"),
    ("point_failure", check_point_failure, "sweep"),
    ("error_enum", check_error_enum, "sweep"),
    ("determinism", check_determinism, "sweep"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "sweep"),
)

_DEFERRED_OWNERS = (
    "the real Python-strategy factory implementing SweepStrategyFactory (the deferred strategy "
    "host / SRS-BT-001 runtime; fixture factories realize the seam solo)",
    "the REST / dashboard sweep surface (SRS-API-001 / SRS-UI)",
    "the real stored-data benchmark resolver behind BenchmarkSource (SRS-BT-005)",
    "walk-forward analysis consuming SweepRunner::run per in-sample window (SRS-BT-008)",
    "persisting sweep points into backtest history (the SRS-BT-009-consumer / orchestrator "
    "boundary; the runner deliberately stays pure)",
)


def assert_backtest_sweep_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "sweep": sweep_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    evidence = [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]
    evidence.append(check_cli_surface(config, sources["cargo"], cli_source(config, root)))
    return evidence


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_backtest_sweep_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-007 SDK-surface contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable sweep path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except BacktestSweepCheckError as error:
        print(f"SRS-BT-007 SDK-SURFACE FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-007 SDK-SURFACE PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-007 passes:false until the close process flips it"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
