#!/usr/bin/env python3
"""Contract evidence script for SRS-BT-008 (walk-forward analysis).

SRS-BT-008 (SyRS SYS-20; StRS SN-1.17). The acceptance criterion: "In-sample windows are
optimized, out-of-sample windows are evaluated, and outputs preserve the parameter set and
metrics per window."

The walk-forward surface lives in ``crates/atp-simulation`` (module ``walk_forward``), per
the structural contract in ``architecture/runtime_services.json`` (block
``sim_walk_forward_contract``). It is the named downstream consumer of the SRS-BT-007 sweep
core: it optimizes each in-sample window with ``SweepRunner::run`` and evaluates the winning
parameter set out-of-sample through the SAME sweep chain, so a walk-forward number is exactly
what a standalone sweep/backtest of that point would report. Each AC noun is a named artifact:

  (a) ``WalkForwardWindow`` / ``WalkForwardSchedule`` (the folds): every window pairs an
      in-sample ``DateRange`` with an out-of-sample ``DateRange``, and validation enforces the
      walk-forward NO-LOOKAHEAD invariant -- the out-of-sample window must lie strictly after
      the in-sample window (``LookaheadWindow``), else the out-of-sample measurement leaks data
      the optimizer already saw. The schedule fails closed on an empty schedule, an inverted
      sub-range, non-advancing folds, and overlapping out-of-sample windows; the ``rolling``
      generator uses overflow-checked arithmetic and funnels through ``new`` (one rule set).
  (b) ``WalkForwardRunner::run`` (in-sample optimized, out-of-sample evaluated): it drives every
      fold through a private ``SweepRunner`` -- the in-sample phase is a full grid search whose
      rank-1 point is the optimum (``NoOptimum`` if none is rankable), the out-of-sample phase
      rebuilds a single-point space from the winner and runs it through the SAME sweep, so there
      is no re-implementation of the ``BacktestEngine`` + ``benchmark::compare`` chain.
  (c) ``WalkForwardFold`` / ``WalkForwardReport`` (preserve parameter set + metrics per window):
      each fold carries the selected ``StrategyParameters`` plus both windows' metrics; the
      out-of-sample objective is honestly ``Option`` (undefined is never fabricated); any
      per-fold failure aborts the whole analysis naming the window; ``total_folds`` proves every
      scheduled fold is accounted for.
  (d) the work is deterministic (no parallelism / RNG / clock; SRS-BT-010); ``walk_forward``
      adds no broker/adapter dependency and carries no vendor SDK token; ``lib.rs`` re-exports
      ``pub mod walk_forward;``; ``bt008_walk_forward_cli`` is the operator surface.

The PASS line is ``SRS-BT-008 WALK-FORWARD PASS`` -- it names the deferred owners (the real
Python-strategy factory via the deferred strategy host, the REST/dashboard surface via
SRS-API-001 / SRS-UI, the real stored-data benchmark resolver via SRS-BT-005, and fold
persistence via the SRS-BT-009 consumer boundary).

Mirrors the PASS/FAIL output style of ``tools/backtest_sweep_check.py``.

Invoke:
    python3 tools/walk_forward_check.py
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


class WalkForwardCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise WalkForwardCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "sim_walk_forward_contract" not in config:
        fail("architecture metadata is missing sim_walk_forward_contract")
    return config["sim_walk_forward_contract"]


def walk_forward_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = (
        root / block["simulation_crate"]["path"] / "src" / f"{block['walk_forward_module']}.rs"
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


def check_window_types(config: dict, src: str) -> str:
    spec = contract_block(config)["window_types"]
    for key, label in (
        ("window_struct", "the in-sample/out-of-sample fold window"),
        ("schedule_struct", "the fold schedule"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"walk_forward must declare `pub struct {spec[key]}` ({label})")
    compact_src = _compact(src)
    for key, label in (
        ("validate_fn", "the window validator"),
        ("windows_fn", "the fold accessor"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"walk_forward must expose {label} (`{spec[key]}`)")
    if _compact(spec["date_range_import_token"]) not in compact_src:
        fail(
            "walk_forward windows must be built over the shipped backtest DateRange "
            f"(`{spec['date_range_import_token']}`) so a fold window is the same time axis the "
            "engine restricts replay to -- not a divergent window shape"
        )
    return (
        "atp-simulation declares WalkForwardWindow/WalkForwardSchedule over the shipped "
        "backtest DateRange (an in-sample range paired with the out-of-sample range that "
        "follows it), the folds the SRS-BT-008 analysis marches forward through time"
    )


def check_no_lookahead(config: dict, src: str) -> str:
    spec = contract_block(config)["no_lookahead"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    if not re.search(rf"\b{re.escape(spec['variant'])}\b", body):
        fail(
            f"{enum_name} must declare `{spec['variant']}` -- the walk-forward no-lookahead "
            "invariant (out-of-sample strictly after in-sample) must be a fail-closed error"
        )
    compact_src = _compact(src)
    if _compact(spec["guard_token"]) not in compact_src:
        fail(
            f"walk_forward must enforce the no-lookahead guard (`{spec['guard_token']}`): an "
            "out-of-sample window that overlaps or precedes the in-sample window would report "
            "data the optimizer already saw"
        )
    if _compact(spec["raised_token"]) not in compact_src:
        fail(
            f"the no-lookahead guard must be RAISED (`{spec['raised_token']}`), not merely "
            "declared -- a lookahead window must fail closed"
        )
    return (
        "atp-simulation enforces the walk-forward NO-LOOKAHEAD invariant: WalkForwardWindow "
        "validate() requires in_sample.end < out_of_sample.start and fails closed with "
        "LookaheadWindow, so an out-of-sample measurement can never include data the optimizer "
        "already saw (a fabricated out-of-sample edge)"
    )


def check_schedule_validation(config: dict, src: str) -> str:
    spec = contract_block(config)["schedule_validation"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    compact_src = _compact(src)
    for key, label in (
        ("empty_variant", "an empty schedule"),
        ("invalid_window_variant", "an inverted sub-range"),
        ("non_monotonic_variant", "non-advancing folds"),
        ("overlapping_variant", "overlapping out-of-sample windows"),
    ):
        variant = spec[key]
        if not re.search(rf"\b{re.escape(variant)}\b", body):
            fail(f"{enum_name} must fail closed on {label} (`{variant}`)")
        # Each guard must actually be RAISED (an Err construction), not just declared.
        if _compact(f"Err({enum_name}::{variant}") not in compact_src:
            fail(
                f"`{enum_name}::{variant}` must be raised by the schedule validation path "
                "(an Err construction), not merely declared"
            )
    return (
        "atp-simulation WalkForwardSchedule::new is fail-closed: an empty schedule, an inverted "
        "sub-range, non-advancing folds (in-sample start must strictly advance), and overlapping "
        "out-of-sample windows (walk-forward tiles forward without double-counting) are each "
        "rejected with their exact WalkForwardError"
    )


def check_rolling_generator(config: dict, src: str) -> str:
    spec = contract_block(config)["rolling_generator"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    compact_src = _compact(src)
    if _compact(spec["rolling_fn"]) not in compact_src:
        fail(f"walk_forward must expose the rolling generator (`{spec['rolling_fn']}`)")
    for key, label in (
        ("checked_add_token", "overflow-checked additive boundary arithmetic"),
        ("checked_mul_token", "overflow-checked multiplicative boundary arithmetic"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"the rolling generator must use {label} (`{spec[key]}`)")
    if not re.search(rf"\b{re.escape(spec['overflow_variant'])}\b", body):
        fail(
            f"{enum_name} must fail closed on rolling-schedule overflow "
            f"(`{spec['overflow_variant']}`)"
        )
    for zero in spec["zero_variants"]:
        if not re.search(rf"\b{re.escape(zero)}\b", body):
            fail(f"{enum_name} must fail closed on a zero rolling argument (`{zero}`)")
        if _compact(f"Err({enum_name}::{zero}") not in compact_src:
            fail(
                f"`{enum_name}::{zero}` must be raised by the rolling generator, not merely declared"
            )
    # The fold count must be CAPPED before any allocation: an unbounded operator-supplied count
    # must fail closed with a typed error, never panic on Vec::with_capacity or over-allocate.
    if _compact(spec["fold_cap_const"]) not in compact_src:
        fail(f"walk_forward must declare a fold cap (`{spec['fold_cap_const']}`)")
    if not re.search(rf"\b{re.escape(spec['fold_cap_variant'])}\b", body):
        fail(
            f"{enum_name} must fail closed on an oversized fold count "
            f"(`{spec['fold_cap_variant']}`)"
        )
    # Scope the ordering check to the rolling function body: the same cap is also raised in
    # new(), so an unscoped find() would locate new()'s cap (earlier in the file) and mask a
    # rolling cap that was removed or moved after the allocation.
    rolling_start = src.find(spec["rolling_fn"])
    rolling_src = src[rolling_start:] if rolling_start != -1 else ""
    cap_pos = rolling_src.find(spec["fold_cap_before_alloc_token"])
    alloc_pos = rolling_src.find(spec["alloc_token"])
    if cap_pos == -1:
        fail(
            f"the rolling generator's fold cap must be RAISED "
            f"(`{spec['fold_cap_before_alloc_token']}`), not merely declared"
        )
    if alloc_pos == -1 or cap_pos > alloc_pos:
        fail(
            "the fold-count cap must fire BEFORE the schedule is allocated "
            f"(`{spec['fold_cap_before_alloc_token']}` must precede `{spec['alloc_token']}` in "
            "rolling()) -- an unbounded operator-supplied count must never reach Vec::with_capacity"
        )
    if _compact(spec["funnels_through_new_token"]) not in compact_src:
        fail(
            f"the rolling generator must funnel through the validated constructor "
            f"(`{spec['funnels_through_new_token']}`) so one rule set validates every schedule "
            "(a step < out_of_sample_len that would overlap folds is rejected by new())"
        )
    return (
        "atp-simulation WalkForwardSchedule::rolling generates folds with overflow-checked "
        "boundary arithmetic (checked_add/checked_mul -> ScheduleOverflow), fails closed on a "
        "zero length/step/fold_count, caps the fold count (TooManyFolds against "
        "MAX_WALK_FORWARD_FOLDS) BEFORE any allocation so an unbounded count cannot panic or "
        "over-allocate, and funnels through new() so the forward-tiling / no-lookahead rules "
        "validate every generated schedule"
    )


def check_runner_reuse(config: dict, src: str) -> str:
    spec = contract_block(config)["runner_reuse"]
    compact_src = _compact(src)
    for key, label in (
        ("runner_struct", "the walk-forward orchestrator"),
        ("request_struct", "the walk-forward request"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"walk_forward must declare `pub struct {spec[key]}` ({label})")
    for key, label in (
        ("run_fn", "the run entry point"),
        ("sweep_field_token", "a private SweepRunner"),
        ("sweep_run_token", "the SweepRunner::run reuse"),
        ("test_seam_fn", "the cap test seam"),
    ):
        if _compact(spec[key]) not in compact_src:
            fail(f"walk_forward must expose {label} (`{spec[key]}`)")
    # The runner must REUSE SweepRunner, never re-implement the engine/compare chain.
    for token in spec["forbidden_reimpl_tokens"]:
        if _compact(token) in compact_src:
            fail(
                f"walk_forward must reuse SweepRunner, not re-implement the backtest/compare "
                f"chain: found `{token}` -- the out-of-sample number must be exactly what a "
                "standalone sweep of that point reports, evaluated through the SAME SweepRunner"
            )
    return (
        "atp-simulation WalkForwardRunner owns a private SweepRunner and evaluates every fold "
        "through it (self.sweep.run) -- the in-sample optimization AND the out-of-sample "
        "evaluation reuse the SAME shipped sweep chain, with no re-implementation of "
        "BacktestEngine::run or benchmark::compare in the walk_forward module"
    )


def check_in_sample_optimization(config: dict, src: str) -> str:
    spec = contract_block(config)["in_sample_optimization"]
    compact_src = _compact(src)
    if _compact(spec["rank_one_token"]) not in compact_src:
        fail(
            f"the in-sample window must be OPTIMIZED by taking the rank-1 sweep point "
            f"(`{spec['rank_one_token']}`) -- the best configuration by the selected objective"
        )
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    if not re.search(rf"\b{re.escape(spec['no_optimum_variant'])}\b", body):
        fail(
            f"{enum_name} must fail closed when an in-sample window has no rankable optimum "
            f"(`{spec['no_optimum_variant']}`) -- never silently select an unranked point"
        )
    if _compact(f"{enum_name}::{spec['no_optimum_variant']}") not in compact_src:
        fail(f"`{enum_name}::{spec['no_optimum_variant']}` must be raised, not merely declared")
    return (
        "atp-simulation optimizes each in-sample window by running the full SRS-BT-007 grid "
        "search and taking report.ranked.first() (the rank-1 optimum); an in-sample window with "
        "no rankable point fails closed with NoOptimum rather than selecting an unranked point"
    )


def check_out_of_sample_evaluation(config: dict, src: str) -> str:
    spec = contract_block(config)["out_of_sample_evaluation"]
    compact_src = _compact(src)
    if _compact(spec["singleton_fn"]) not in compact_src:
        fail(
            f"the out-of-sample evaluation must rebuild a single-point space from the winner "
            f"(`{spec['singleton_fn']}`) and run it through the SAME sweep -- not a parallel "
            "evaluation path"
        )
    if _compact(spec["option_objective_field"]) not in compact_src:
        fail(
            f"the out-of-sample objective must be honestly optional "
            f"(`{spec['option_objective_field']}`) -- None when the metric is undefined on the "
            "out-of-sample window, never a fabricated stand-in"
        )
    if _compact(spec["unranked_route_token"]) not in compact_src:
        fail(
            f"an undefined out-of-sample objective must route through the sweep's unranked bucket "
            f"(`{spec['unranked_route_token']}`), preserving the metrics with a None objective"
        )
    return (
        "atp-simulation evaluates the winning parameter set out-of-sample by rebuilding a "
        "single-point ParameterSpace and running it through the SAME SweepRunner; the "
        "out-of-sample objective is Option<f64> (Some when defined, None via the unranked bucket "
        "when undefined -- never a fabricated stand-in)"
    )


def check_preservation(config: dict, src: str) -> str:
    spec = contract_block(config)["preservation"]
    for key, label in (
        ("fold_struct", "the per-window fold result"),
        ("report_struct", "the walk-forward report"),
    ):
        if not re.search(rf"\bpub\s+struct\s+{re.escape(spec[key])}\b", src):
            fail(f"walk_forward must declare `pub struct {spec[key]}` ({label})")
    for key, label in (
        ("selected_params_field", "the optimized parameter set"),
        ("in_sample_metrics_field", "the in-sample metrics"),
        ("out_of_sample_metrics_field", "the out-of-sample metrics"),
    ):
        if not re.search(rf"\bpub\s+{re.escape(spec[key])}\b", src):
            fail(
                f"WalkForwardFold must preserve {label} per window (`pub {spec[key]}`) -- the "
                "SRS-BT-008 acceptance requires outputs to preserve the parameter set and metrics"
            )
    if not re.search(rf"\bpub\s+{re.escape(spec['accounting_field'])}\b", src):
        fail(
            f"WalkForwardReport must carry `{spec['accounting_field']}` so every scheduled fold "
            "is provably accounted for"
        )
    return (
        "atp-simulation WalkForwardFold preserves, per window, the selected parameter set and "
        "both the in-sample and out-of-sample metrics (the SRS-BT-008 acceptance), and "
        "WalkForwardReport.total_folds proves every scheduled fold is accounted for"
    )


def check_no_fabrication(config: dict, src: str) -> str:
    spec = contract_block(config)["no_fabrication"]
    for token in spec["forbidden_fallback_tokens"]:
        if token in src:
            fail(
                "the out-of-sample objective must not fabricate a stand-in for an undefined "
                f"metric (found `{token}`) -- an undefined objective is preserved as None"
            )
    return (
        "atp-simulation never fabricates an out-of-sample objective for an undefined metric: no "
        "unwrap_or(0.0) / unwrap_or_default() fallback -- undefined stays honestly None"
    )


def check_point_failure(config: dict, src: str) -> str:
    spec = contract_block(config)["point_failure"]
    enum_name = contract_block(config)["error_enum"]["enum"]
    body = _enum_body(src, enum_name)
    for key, label in (
        ("in_sample_variant", "an in-sample sweep failure"),
        ("out_of_sample_variant", "an out-of-sample sweep failure"),
    ):
        if not re.search(rf"\b{re.escape(spec[key])}\b", body):
            fail(f"{enum_name} must abort the whole analysis on {label} (`{spec[key]}`)")
    if _compact(spec["names_window_token"]) not in _compact(src):
        fail(
            f"a per-fold failure must NAME the offending window (`{spec['names_window_token']}`) "
            "so the operator can fix the schedule, and the analysis must abort rather than emit a "
            "partial report"
        )
    return (
        "atp-simulation aborts the WHOLE walk-forward on any per-fold sweep failure "
        "(InSampleSweepFailed / OutOfSampleEvalFailed) naming the offending window -- a partial "
        "report could mis-rank a configuration, so the analysis is all-or-error per fold"
    )


def check_error_enum(config: dict, src: str) -> str:
    spec = contract_block(config)["error_enum"]
    body = _enum_body(src, spec["enum"])
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} is missing fail-closed variants: {', '.join(missing)}")
    return (
        f"atp-simulation declares {spec['enum']} with {len(spec['variants'])} fail-closed "
        "variants covering schedule validation, the no-lookahead invariant, rolling overflow, "
        "no-optimum, and per-fold failure naming the window"
    )


def check_determinism(config: dict, src: str) -> str:
    spec = contract_block(config)["determinism"]
    leaked = [t for t in spec["forbidden_tokens"] if t in src]
    if leaked:
        fail(
            f"walk_forward must be deterministic (SRS-BT-010): found nondeterminism source(s) "
            f"{', '.join(leaked)} -- folds are evaluated sequentially with no parallelism, RNG, "
            "or wall-clock read"
        )
    return (
        "atp-simulation walk_forward is deterministic: no parallelism / RNG / clock token, so "
        "identical inputs produce an identical report (SRS-BT-010)"
    )


def check_module_reexport(config: dict, lib_src: str) -> str:
    spec = contract_block(config)["module_reexport"]
    if _compact(spec["lib_reexport_token"]) not in _compact(lib_src):
        fail(
            f"atp-simulation lib.rs must re-export `{spec['lib_reexport_token']}` so the "
            "walk-forward surface is part of the simulation engine"
        )
    return f"atp-simulation lib.rs re-exports `{spec['lib_reexport_token']}`"


def check_no_broker_dependency(config: dict, cargo_text: str) -> str:
    spec = contract_block(config)["no_broker_dependency"]
    leaked = [t for t in spec["forbidden_dep_tokens"] if t in cargo_text]
    if leaked:
        fail(
            f"atp-simulation Cargo.toml must NOT depend on the live/broker path: found "
            f"{', '.join(leaked)} -- a walk-forward analysis must be independent of the IB account"
        )
    return (
        f"atp-simulation Cargo.toml declares no dependency on the live/broker path "
        f"({', '.join(spec['forbidden_dep_tokens'])}) -- the walk-forward surface is "
        "broker-independent"
    )


def check_vendor_isolation(config: dict, src: str) -> str:
    tokens = contract_block(config)["vendor_forbidden_tokens"]
    leaked = [t for t in tokens if t in src]
    if leaked:
        fail(
            f"atp-simulation walk_forward module leaks vendor SDK token(s): {', '.join(leaked)} "
            "(the core engine must isolate vendors behind adapters per SRS-ARCH-003)"
        )
    return (
        f"atp-simulation walk_forward module is free of all {len(tokens)} forbidden vendor SDK "
        "tokens (SRS-ARCH-003 adapter isolation)"
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
            f"requires an explicit --direction (`{spec['explicit_direction_token']}`)"
        )
    if spec["no_lookahead_surface_token"] not in cli_src:
        fail(
            "the CLI must surface the no-lookahead rule to the operator "
            f"(`{spec['no_lookahead_surface_token']}`)"
        )
    if _compact(spec["kv_control_char_guard"]) not in _compact(cli_src):
        fail(
            f"the kv machine format must fail closed on control characters "
            f"(`{spec['kv_control_char_guard']}`) so a parameter value can never forge a proof "
            "line"
        )
    return (
        "atp-simulation registers the bt008_walk_forward_cli operator binary: --fold / --rolling "
        "schedule flags, an explicit --objective requires an explicit --direction (never "
        "guessed), the no-lookahead rule surfaced to the operator, and the kv machine grammar "
        "fails closed on control characters"
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
                "walk-forward path compiles + passes (install the Rust toolchain)"
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
        f"cargo test -p {crate} --test {integration} + {cli_test}: PASS (in-sample optimization "
        "matches a hand-derived rank-1 under both SYS-19 objectives, out-of-sample evaluation "
        "matches an independent backtest of the winner, no-lookahead enforced, per-fold failures "
        "abort naming the window, and the CLI round-trips the whole workflow across a process "
        "boundary)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #

# (name, collector, source-key) -- "walk_forward" reads walk_forward.rs, "lib" reads lib.rs,
# "cargo" reads the crate Cargo.toml.
_STATIC_CHECKS = (
    ("window_types", check_window_types, "walk_forward"),
    ("no_lookahead", check_no_lookahead, "walk_forward"),
    ("schedule_validation", check_schedule_validation, "walk_forward"),
    ("rolling_generator", check_rolling_generator, "walk_forward"),
    ("runner_reuse", check_runner_reuse, "walk_forward"),
    ("in_sample_optimization", check_in_sample_optimization, "walk_forward"),
    ("out_of_sample_evaluation", check_out_of_sample_evaluation, "walk_forward"),
    ("preservation", check_preservation, "walk_forward"),
    ("no_fabrication", check_no_fabrication, "walk_forward"),
    ("point_failure", check_point_failure, "walk_forward"),
    ("error_enum", check_error_enum, "walk_forward"),
    ("determinism", check_determinism, "walk_forward"),
    ("module_reexport", check_module_reexport, "lib"),
    ("no_broker_dependency", check_no_broker_dependency, "cargo"),
    ("vendor_isolation", check_vendor_isolation, "walk_forward"),
)

_DEFERRED_OWNERS = (
    "the real Python-strategy factory (the deferred strategy host / SRS-BT-001 runtime; fixture "
    "factories realize the SweepStrategyFactory seam solo)",
    "the REST / dashboard walk-forward surface (SRS-API-001 / SRS-UI)",
    "the real stored-data benchmark resolver behind BenchmarkSource (SRS-BT-005)",
    "persisting walk-forward folds into backtest history (the SRS-BT-009-consumer / orchestrator "
    "boundary; the runner deliberately stays pure)",
)


def assert_walk_forward_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable without cargo (used by the L3 contract test)."""
    sources = {
        "walk_forward": walk_forward_source(config, root),
        "lib": lib_source(config, root),
        "cargo": cargo_source(config, root),
    }
    evidence = [check(config, sources[source_key]) for _, check, source_key in _STATIC_CHECKS]
    evidence.append(check_cli_surface(config, sources["cargo"], cli_source(config, root)))
    return evidence


def run_checks(require_cargo: bool = False) -> list[str]:
    config = load_config()
    evidence = assert_walk_forward_static(config)
    evidence.append(check_cargo_test_smoke(config, require_cargo=require_cargo))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-BT-008 walk-forward contract evidence")
    parser.add_argument(
        "--require-cargo",
        action="store_true",
        help="Fail (not skip) if cargo is unavailable -- the runnable walk-forward path must compile.",
    )
    args = parser.parse_args(argv)

    try:
        evidence = run_checks(require_cargo=args.require_cargo)
    except WalkForwardCheckError as error:
        print(f"SRS-BT-008 WALK-FORWARD FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-BT-008 WALK-FORWARD PASS")
    for item in evidence:
        print(f"- {item}")
    print(
        "- deferred to: "
        + ", ".join(_DEFERRED_OWNERS)
        + "; feature_list.json keeps SRS-BT-008 passes:false until the close process flips it"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
