#!/usr/bin/env python3
"""Contract evidence script for feature ERR-7.

Verifies that the Hot-Swap demotion liquidation-timeout gate declared in
``architecture/runtime_services.json`` (block ``hot_swap_demotion_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-orchestrator``.

ERR-7 traces SRS-RESV-004 (SyRS SYS-49b / SYS-49c; StRS SN-1.25). The
contract guarantees:

  (a) ``HotSwapDemotionOutcome`` declares the binary FlatBeforeTimeout /
      TimedOutDemotionPending decision in ``atp-types`` (the unbuilt 60 s
      async wait loop is the deferred runtime — the slice models only the
      outcome the loop produces).
  (b) ``HotSwapDemotionRequest`` carries the three required fields
      (demoting_strategy_id, candidate_strategy_id, timeout_seconds) and no
      broker / IB-order / vendor / container leakage; cancellation flows
      through a port, never a field on the envelope.
  (c) ``OperatorAlertChannel`` declares the SRS-RESV-004 dashboard/email/SMS
      triad, and ``OperatorAlertEvent`` + ``HotSwapDemotionEvent`` carry
      their required fields and reject the same forbidden allowlist.
  (d) the four ports — ``HotSwapLiquidationProbe`` (timing authority,
      read-only), ``UnfilledOrderCanceller``, ``OperatorAlertSink``, and
      ``HotSwapDemotionEventSink`` — live in ``atp-orchestrator``.
  (e) inside ``StrategyOrchestrator::resolve_demotion``, the body matches on
      ``liquidation.await_flat_or_timeout(...)``; the FlatBeforeTimeout arm
      is the only construction site of ``HotSwapDemotionResolved {`` and
      dispatches NO operator alert and cancels NO order; the
      TimedOutDemotionPending arm cancels the unfilled order, dispatches the
      operator alert over all three channels, records the demotion event,
      produces ``OrderErrorCategory::HotSwapDemotionTimeout``, and calls NO
      promotion path (``promote(`` / ``complete_swap(`` / ``go_live(`` …) —
      promotion is blocked on every timeout.

Mirrors the PASS/FAIL output style of ``tools/pacing_budget_check.py``.

Invoke:
    python3 tools/hot_swap_demotion_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from _rust_parser import _enum_body, _fn_block, _struct_body, _trait_body

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class HotSwapDemotionCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise HotSwapDemotionCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "hot_swap_demotion_contract" not in config:
        fail("architecture metadata is missing hot_swap_demotion_contract")
    return config["hot_swap_demotion_contract"]


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
# Precise block-arm extractor
# --------------------------------------------------------------------------- #
#
# `_rust_parser._variant_arm` extracts up to the next top-level comma, which
# over-runs a block-bodied arm that carries no trailing comma (the repo style
# for `=> { ... }` arms — see `StrategyOrchestrator::launch`). ERR-7 asserts
# *negatively* on BOTH arms (the flat arm must NOT dispatch/cancel; the
# timeout arm must NOT accept/promote), so each arm must be isolated exactly.
# This helper finds `VARIANT { .. } => {` and returns the balanced block.


def _arm_block(body: str, variant_token: str) -> str:
    pattern = re.compile(
        rf"{re.escape(variant_token)}\s*(?:\{{[^{{}}]*\}})?\s*=>\s*\{{",
        re.DOTALL,
    )
    match = pattern.search(body)
    if match is None:
        fail(f"resolve_demotion is missing a block arm for `{variant_token}`")
    start = match.end()
    depth = 1
    index = start
    while index < len(body) and depth:
        char = body[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    if depth:
        fail(f"could not parse the `{variant_token}` arm block")
    return body[start : index - 1]


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


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
            f"{', '.join(leaks)} (ERR-7 {kind} must not carry "
            "broker/session/IB-order/tick/vendor/container identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/IB-order/vendor fields"
    )


def check_demotion_request_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["demotion_request"], "demotion envelope")


def check_operator_alert_event_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["operator_alert_event"], "alert event")


def check_demotion_event_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["demotion_event"], "demotion event")


def _check_enum(types_src: str, spec: dict, note: str) -> str:
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"variants ({', '.join(spec['variants'])}) — {note}"
    )


def check_demotion_outcome_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["demotion_outcome"],
        "the binary SRS-RESV-004 liquidation-timeout decision (the 60 s wait "
        "loop that produces it is the deferred runtime)",
    )


def check_operator_alert_channel_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["operator_alert_channel"],
        "the SRS-RESV-004 dashboard/email/SMS notification triad",
    )


def check_side_effect_outcome_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["side_effect_outcome"],
        "the observable timeout-branch side-effect outcome (a failed IB "
        "cancel / missed operator alert is recorded as Failed, not silently "
        "indistinguishable from success)",
    )


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


def check_liquidation_probe_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["liquidation_probe_port"],
        "the read-only flat-vs-timeout timing authority (no mutators — the "
        "gate cannot promote through this port)",
    )


def check_unfilled_order_canceller_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["unfilled_order_canceller_port"],
        "the SRS-RESV-004 unfilled-liquidation-order cancel path (deferred IB adapter)",
    )


def check_operator_alert_sink_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["operator_alert_sink_port"],
        "the SRS-RESV-004 dashboard/email/SMS operator-alert dispatch channel",
    )


def check_demotion_event_sink_port(config: dict, orch_src: str) -> str:
    return _check_port(
        orch_src,
        contract_block(config)["demotion_event_sink_port"],
        "the structured demotion state-transition audit record (SRS-LOG-001 / SRS-UI-001)",
    )


def check_resolve_demotion_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(orch_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    probe_token = guard["probe_call"] + "("
    if probe_token not in body:
        fail(
            f"{entry['method']} does not call `{probe_token}` — the liquidation "
            "probe's outcome is the only legitimate entry to the gate"
        )

    enum = guard["outcome_enum"]
    flat_token = f"{enum}::{guard['flat_variant']}"
    timeout_token = f"{enum}::{guard['timeout_variant']}"
    for token in (flat_token, timeout_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — ERR-7 "
                "requires both the flat and the timeout outcomes to be handled"
            )

    flat_arm = _arm_block(body, flat_token)
    timeout_arm = _arm_block(body, timeout_token)

    accepted_token = guard["accepted_struct"] + " {"
    cancel_token = guard["cancel_call"] + "("
    alert_token = guard["alert_call"] + "("
    event_token = guard["event_call"] + "("

    # --- Flat arm: accept + audit; NO alert, NO cancel. -------------------- #
    if accepted_token not in flat_arm:
        fail(
            f"{entry['method']} {flat_token} arm does not construct "
            f"`{accepted_token}` — the flat outcome is the only acceptance site"
        )
    if event_token not in flat_arm:
        fail(
            f"{entry['method']} {flat_token} arm is missing `{event_token}` — "
            "the flat outcome must still record the audit transition"
        )
    for forbidden in (alert_token, cancel_token):
        if forbidden in flat_arm:
            fail(
                f"{entry['method']} {flat_token} arm calls `{forbidden}` — an "
                "in-time demotion raises no operator alert and cancels nothing"
            )

    # --- Timeout arm: cancel + alert(3 channels) + event + reject; --------- #
    # --- NO acceptance, NO promotion. -------------------------------------- #
    for required in (cancel_token, alert_token, event_token):
        if required not in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm is missing required "
                f"call `{required}` (SRS-RESV-004: cancel the unfilled order, "
                "notify the operator, record the demotion-pending transition)"
            )
    for channel in guard["alert_channels"]:
        if channel not in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm is missing alert channel "
                f"`{channel}` — SRS-RESV-004 fans the notification to dashboard, "
                "email, AND SMS"
            )
    category_token = f"OrderErrorCategory::{block['rejection_category']}"
    factory_token = guard["error_factory"] + "("
    if category_token not in timeout_arm and factory_token not in timeout_arm:
        fail(
            f"{entry['method']} {timeout_token} arm must produce {category_token} "
            f"(directly or via the `{guard['error_factory']}` factory — the SyRS "
            "SYS-64 wire string source of truth)"
        )
    if accepted_token in timeout_arm:
        fail(
            f"{entry['method']} {timeout_token} arm constructs `{accepted_token}` "
            "— a timed-out demotion is not an acceptance"
        )
    for promotion in guard["forbidden_promotions"]:
        token = promotion + "("
        if token in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm calls promotion path "
                f"`{token}` — ERR-7 requires promotion to be BLOCKED on timeout"
            )

    return (
        f"atp-orchestrator::{entry['method']} matches "
        f"`{guard['probe_call']}`; the {flat_token} arm is the sole "
        f"`{guard['accepted_struct']}` site (no alert, no cancel); the "
        f"{timeout_token} arm cancels via `{guard['cancel_call']}`, alerts all "
        "3 channels via `{alert}`, records via `{event}`, emits "
        "OrderErrorCategory::{cat}, and calls no promotion path (ERR-7)".format(
            alert=guard["alert_call"],
            event=guard["event_call"],
            cat=block["rejection_category"],
        )
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["orchestrator_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
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
    combined = lib.stdout + lib.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    integ = subprocess.run(
        [cargo, "test", "-p", crate, "--test", "err_7_hot_swap_demotion_timeout", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_7_hot_swap_demotion_timeout failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_7_hot_swap_demotion_timeout: PASS "
        "(timeout demotion-pending + blocked promotion + flat-arm selectivity verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("demotion_request", check_demotion_request_struct, "types"),
    ("demotion_outcome", check_demotion_outcome_enum, "types"),
    ("operator_alert_channel", check_operator_alert_channel_enum, "types"),
    ("operator_alert_event", check_operator_alert_event_struct, "types"),
    ("side_effect_outcome", check_side_effect_outcome_enum, "types"),
    ("demotion_event", check_demotion_event_struct, "types"),
    ("liquidation_probe_port", check_liquidation_probe_port, "orch"),
    ("unfilled_order_canceller_port", check_unfilled_order_canceller_port, "orch"),
    ("operator_alert_sink_port", check_operator_alert_sink_port, "orch"),
    ("demotion_event_sink_port", check_demotion_event_sink_port, "orch"),
    ("resolve_demotion_guard", check_resolve_demotion_guard, "orch"),
)


def _run_static(config: dict, types_src: str, orch_src: str) -> list[str]:
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    return evidence


def assert_hot_swap_demotion_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    return _run_static(config, types_source(config, root), orchestrator_source(config, root))


def run_checks() -> list[str]:
    config = load_config()
    evidence = _run_static(config, types_source(config), orchestrator_source(config))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-7 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except HotSwapDemotionCheckError as error:
        print(f"ERR-7 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-7 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
