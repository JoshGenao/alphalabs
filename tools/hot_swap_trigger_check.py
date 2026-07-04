#!/usr/bin/env python3
"""Contract evidence script for feature SRS-RESV-003.

Verifies that the Hot-Swap trigger DECISION + CONFIGURATION + LOGGING layer
declared in ``architecture/runtime_services.json`` (block
``hot_swap_trigger_contract``) is reachable from the Rust crates
``crates/atp-types`` and ``crates/atp-orchestrator``.

SRS-RESV-003 traces SyRS SYS-49a (StRS SN-1.25 / SN-1.30). The contract
guarantees:

  (a) ``HotSwapTriggerKind`` declares the four-trigger taxonomy (manual +
      three automatic) and maps each variant 1:1 to its UPPER_SNAKE wire
      string via ``as_str``.
  (b) The automatic-trigger config enums (``DrawdownDemotionTrigger``,
      ``RankingPromotionTrigger``) default to ``Disabled`` via ``#[default]``,
      and ``HotSwapTriggerConfig`` derives ``Default`` over them — so a default
      config disables every automatic trigger (SYS-49a "automatic triggers
      shall default to disabled"), statically.
  (c) ``LiveStrategyState``, ``HotSwapTriggerProposal``, and
      ``HotSwapTriggerEvent`` carry their required fields and no broker /
      IB-order / vendor / container leakage (source-neutral).
  (d) the three injected ports — ``LiveStrategyProbe``,
      ``ReservoirRankingSource``, ``HotSwapTriggerLog`` — live in
      ``atp-orchestrator``.
  (e) the fire helper ``fire_trigger`` records EVERY fired trigger through
      ``log.record`` (SYS-49a "all swap triggers shall be logged"), and BOTH
      entry points (``evaluate_automatic_triggers`` /
      ``request_manual_promotion``) route their fires through it — so a
      proposal cannot exist without a paired log attempt. The automatic
      evaluation consults the three triggers in the fixed priority order
      drawdown-demotion → top-ranked → highest-momentum.
  (f) ``HotSwapTriggerProposal::to_demotion_request`` bridges a fired trigger
      to the existing ``HotSwapDemotionRequest`` the SRS-RESV-004 gate
      consumes — the clean, non-duplicating boundary (this layer decides +
      logs; SRS-RESV-004 executes).

Static-only (no cargo): the Rust behavioral post-conditions are anchored by
``crates/atp-orchestrator/tests/resv_3_hot_swap_triggers.rs`` and the
``tests/domain/test_hot_swap_trigger_config.py`` shell. This script is reached
through ``tools/architecture_check.py`` (so it runs in both ``ci.yml`` and
``tools/run_ci_locally.sh`` via the aggregated ``architecture`` step).

Invoke:
    python3 tools/hot_swap_trigger_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class HotSwapTriggerCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise HotSwapTriggerCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "hot_swap_trigger_contract" not in config:
        fail("architecture metadata is missing hot_swap_trigger_contract")
    return config["hot_swap_trigger_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["types_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def orchestrator_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["orchestrator_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"orchestrator crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Private-fn brace matcher
# --------------------------------------------------------------------------- #
#
# `_rust_parser._fn_block` requires `pub fn`. The fire helper `fire_trigger` is
# deliberately private (an internal single-place logging guarantee, not public
# API), so the guard needs a matcher that also finds a non-`pub` fn body.


def _any_fn_block(source: str, fn_name: str) -> str:
    match = re.search(rf"\bfn\s+{re.escape(fn_name)}\b[^\{{]*\{{", source)
    if not match:
        fail(f"Rust source is missing function `{fn_name}`")
    start = match.end()
    depth = 1
    index = start
    while index < len(source) and depth:
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse function body for `{fn_name}`")
    return source[start : index - 1]


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_trigger_kind_enum(config: dict, types_src: str) -> str:
    spec = contract_block(config)["trigger_kind"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    for variant, wire in spec["wire_strings"].items():
        if not re.search(rf'Self::{re.escape(variant)}\s*=>\s*"{re.escape(wire)}"', types_src):
            fail(f'{spec["enum"]}::{variant} does not map 1:1 to wire string "{wire}" via as_str')
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} trigger "
        f"variants, each mapped 1:1 to its UPPER_SNAKE wire string via as_str"
    )


def check_default_disabled_enums(config: dict, types_src: str) -> str:
    specs = contract_block(config)["default_disabled_enums"]
    for spec in specs:
        try:
            body = _enum_body(types_src, spec["enum"])
        except AssertionError as error:
            fail(str(error))
        missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
        if missing:
            fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
        default_variant = spec["default_variant"]
        if not re.search(rf"#\[default\]\s*{re.escape(default_variant)}\b", body):
            fail(
                f"{spec['enum']} does not annotate `{default_variant}` with "
                "`#[default]` — the SYS-49a 'automatic triggers default to "
                "disabled' invariant must be statically encoded"
            )
    names = ", ".join(spec["enum"] for spec in specs)
    return (
        f"atp-types declares {len(specs)} automatic-trigger enum(s) ({names}) that "
        "each default to Disabled via #[default] — automatic triggers default off"
    )


def check_trigger_config(config: dict, types_src: str) -> str:
    spec = contract_block(config)["trigger_config"]
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    if spec.get("derives_default"):
        if not re.search(
            rf"#\[derive\([^)]*\bDefault\b[^)]*\)\]\s*pub struct {re.escape(spec['struct'])}\b",
            types_src,
        ):
            fail(
                f"{spec['struct']} does not derive Default — the default config "
                "must disable every automatic trigger"
            )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} automatic-trigger fields and derives "
        "Default (default = all automatic triggers disabled)"
    )


def _check_struct(types_src: str, spec: dict, kind: str) -> str:
    try:
        body = _struct_body(types_src, spec["struct"])
    except AssertionError as error:
        fail(str(error))
    missing = [
        field
        for field in spec["required_fields"]
        if not re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if missing:
        fail(f"{spec['struct']} is missing required fields: {', '.join(missing)}")
    leaks = [
        field
        for field in spec["forbidden_fields"]
        if re.search(rf"\bpub\s+{re.escape(field)}\s*:", body)
    ]
    if leaks:
        fail(
            f"{spec['struct']} leaks broker / IB-order / vendor field(s): "
            f"{', '.join(leaks)} (SRS-RESV-003 {kind} must stay source-neutral)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/IB-order/vendor fields"
    )


def check_live_strategy_state_struct(config: dict, types_src: str) -> str:
    return _check_struct(
        types_src, contract_block(config)["live_strategy_state"], "live-strategy input"
    )


def check_trigger_proposal_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["trigger_proposal"], "trigger proposal")


def check_trigger_event_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["trigger_event"], "trigger log event")


def _check_port(orch_src: str, spec: dict, note: str) -> str:
    try:
        body = _trait_body(orch_src, spec["trait"])
    except AssertionError as error:
        fail(str(error))
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-orchestrator declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method(s) ({', '.join(spec['methods'])}) — {note}"
    )


def check_live_strategy_probe_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["live_strategy_probe_port"],
        "the read-only current-live-strategy probe (identity + drawdown; no mutators)",
    )


def check_reservoir_ranking_source_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["reservoir_ranking_source_port"],
        "the injected SRS-RESV-002 ranking snapshot source",
    )


def check_trigger_log_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["trigger_log_port"],
        "the best-effort swap-trigger log sink (durable SYS-61 store deferred to SRS-LOG-001)",
    )


def check_evaluation_guard(config: dict, orch_src: str) -> str:
    guard = contract_block(config)["guard"]

    # (e) every fired trigger is logged: the fire helper records through
    #     `log.record`, and both entry points route their fires through it.
    fire_helper = guard["fire_helper"]
    fire_body = _any_fn_block(orch_src, fire_helper)
    log_call = guard["log_call"] + "("
    if log_call not in fire_body:
        fail(
            f"{fire_helper} does not call `{log_call}` — every fired trigger must "
            "be logged (SYS-49a 'all swap triggers shall be logged')"
        )

    fire_call = fire_helper + "("
    for method_key in ("evaluate_method", "manual_method"):
        method = guard[method_key]
        try:
            body = _fn_block(orch_src, method)
        except AssertionError as error:
            fail(str(error))
        if fire_call not in body:
            fail(
                f"{method} does not route its fires through `{fire_call}` — a "
                "trigger must not be produced without a paired log attempt"
            )

    # Priority order: the automatic evaluation consults the three triggers in
    # the fixed order drawdown-demotion → top-ranked → highest-momentum.
    eval_body = _fn_block(orch_src, guard["evaluate_method"])
    indices = []
    for field in guard["priority_ordered_fields"]:
        position = eval_body.find(field)
        if position < 0:
            fail(f"{guard['evaluate_method']} does not reference `{field}`")
        indices.append(position)
    if indices != sorted(indices):
        fail(
            f"{guard['evaluate_method']} does not evaluate the automatic triggers "
            "in the required priority order (drawdown-demotion first as the risk "
            "control, then top-ranked, then highest-momentum)"
        )
    return (
        f"atp-orchestrator::{guard['evaluate_method']} + {guard['manual_method']} "
        f"route every fire through {fire_helper}, which records via {guard['log_call']} "
        "(all triggers logged); automatic triggers are evaluated drawdown-first"
    )


def check_demotion_request_bridge(config: dict, types_src: str) -> str:
    guard = contract_block(config)["guard"]
    bridge = guard["demotion_request_bridge"]
    struct = guard["demotion_request_struct"]
    if not re.search(
        rf"\bfn\s+{re.escape(bridge)}\s*\([^)]*\)\s*->\s*{re.escape(struct)}\b",
        types_src,
    ):
        fail(
            f"HotSwapTriggerProposal::{bridge} does not return {struct} — the "
            "clean handoff to the SRS-RESV-004 gate is missing"
        )
    return (
        f"atp-types bridges a fired trigger to the SRS-RESV-004 gate via "
        f"HotSwapTriggerProposal::{bridge} -> {struct} (decide+log here; execute there)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("trigger_kind", check_trigger_kind_enum, "types"),
    ("default_disabled_enums", check_default_disabled_enums, "types"),
    ("trigger_config", check_trigger_config, "types"),
    ("live_strategy_state", check_live_strategy_state_struct, "types"),
    ("trigger_proposal", check_trigger_proposal_struct, "types"),
    ("trigger_event", check_trigger_event_struct, "types"),
    ("live_strategy_probe_port", check_live_strategy_probe_port, "orch"),
    ("reservoir_ranking_source_port", check_reservoir_ranking_source_port, "orch"),
    ("trigger_log_port", check_trigger_log_port, "orch"),
    ("evaluation_guard", check_evaluation_guard, "orch"),
    ("demotion_request_bridge", check_demotion_request_bridge, "types"),
)


def _run_static(config: dict, types_src: str, orch_src: str) -> list[str]:
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    return evidence


def assert_hot_swap_trigger_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    return _run_static(config, types_source(config, root), orchestrator_source(config, root))


def run_checks() -> list[str]:
    config = load_config()
    return assert_hot_swap_trigger_static(config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-RESV-003 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except HotSwapTriggerCheckError as error:
        print(f"SRS-RESV-003 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-RESV-003 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
