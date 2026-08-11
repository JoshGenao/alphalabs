#!/usr/bin/env python3
"""Contract evidence script for ERR-7 / SRS-RESV-004 (SyRS SYS-49b / SYS-49c; StRS SN-1.25).

Verifies that the Hot-Swap demotion contract declared in
``architecture/runtime_services.json`` (block ``hot_swap_demotion_contract``) is real in the
Rust crates ``crates/atp-types`` and ``crates/atp-orchestrator``.

Types and envelopes:

  (a) ``HotSwapDemotionOutcome`` declares the binary FlatBeforeTimeout /
      TimedOutDemotionPending decision.
  (b) ``HotSwapDemotionRequest`` carries its three required fields and no broker / IB-order /
      vendor / container leakage; cancellation flows through a port, never a field on the
      envelope.
  (c) ``OperatorAlertChannel`` declares the dashboard/email/SMS triad, and
      ``OperatorAlertEvent`` + ``HotSwapDemotionEvent`` carry their required fields — including
      ``demotion_pending``, the outcome that separates "promotion is blocked" from "promotion
      STAYS blocked" — and reject the same forbidden allowlist.

Ports, and the capabilities they deliberately withhold:

  (d) the nine ports live in ``atp-orchestrator``: ``HotSwapLiquidationProbe`` (read-only
      timing authority), ``UnfilledOrderCanceller``, ``OperatorAlertSink``,
      ``HotSwapDemotionEventSink``, ``DemotionPendingLock``, ``SignalHalt``,
      ``DemotionBrokerageControl``, ``PaperTransition`` and ``LivePositionSource``. Two of them
      are checked for what they must NOT offer: ``DemotionPendingLock`` declares no clearing
      method (unblocking a demotion-pending swap is the operator's, via
      ``demotion_pending_store::resolve``), and ``DemotionBrokerageControl`` declares no
      ``disconnect`` (a routine changeover is not a kill switch).

The gate, SyRS SYS-49c:

  (e) inside ``StrategyOrchestrator::resolve_demotion`` the durable lockout is consulted —
      by BYTE POSITION — before the probe runs, and its verdict is actually branched on; the
      FlatBeforeTimeout arm is the only construction site of ``HotSwapDemotionResolved`` and
      dispatches no alert, cancels no order and engages no lockout; the
      TimedOutDemotionPending arm cancels the unfilled order, then pages the operator over all
      three channels, then engages the lockout (so the persisted record carries the outcomes an
      operator resolves against), records the event, produces
      ``OrderErrorCategory::HotSwapDemotionTimeout`` and calls no promotion path; and the
      probe-inconsistency branch blocks promotion and engages the lockout WITHOUT firing the
      destructive cancel.

The sequence and its terminal step, SyRS SYS-49b:

  (f) ``execute_demotion_sequence`` runs cease-signals → cancel-resting → submit-liquidations
      in that order, checked by byte position because the order is a safety property: a resting
      order that fills after the liquidation re-opens the position, and a strategy still
      emitting signals replaces resting orders as fast as they are cancelled.
  (g) ``complete_demotion_to_paper`` requires the ``&HotSwapDemotionResolved`` acceptance token
      the timeout arm cannot construct, and is the crate's ONLY ``transition_to_paper`` call
      site — so "transitions to paper only after live positions are flat" holds structurally
      rather than by convention.

The lockout store, SyRS SYS-49c (c)/(d):

  (h) ``DemotionPendingState`` is three-state, ``read_state``'s ``Err`` arm collapses to
      ``Unreadable`` (never ``Clear``), and ``blocks_promotion`` blocks on Pending AND
      Unreadable — an unreadable lockout is emphatically not an absent one.

Every guard above is mutation-proven: reordering the phases, ignoring the lock's verdict,
adding a clearing method to the lock port, adding ``disconnect`` to the brokerage port,
collapsing an unreadable store to ``Clear``, dropping the timeout arm's ``lock.engage``,
cancelling on the probe-inconsistency branch, and adding a second ``transition_to_paper`` call
site each make this script exit non-zero.

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
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


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


def module_source(module_path: str, root: Path = ROOT) -> str:
    """Read one of the contract's named modules (the sequence, the lockout store)."""

    source_path = root / module_path
    if not source_path.exists():
        fail(f"contract module missing: {module_path}")
    return source_path.read_text(encoding="utf-8")


def orchestrator_crate_sources(config: dict, root: Path = ROOT) -> dict[str, str]:
    """Every ``.rs`` file in the orchestrator crate, keyed by repo-relative path.

    Whole-crate collectors need this: a guarantee like "``transition_to_paper`` has exactly one
    call site" is only worth stating if it is checked across every file that could add a
    second one. Checking the one module that declares it would confirm what the author already
    knew and miss the call site a later change adds elsewhere.
    """

    block = contract_block(config)
    crate_root = root / block["orchestrator_crate"]["path"] / "src"
    if not crate_root.is_dir():
        fail(f"orchestrator crate source directory missing: {crate_root}")
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(crate_root.rglob("*.rs"))
    }


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


def _calls(text: str) -> str:
    """Collapse whitespace around ``.`` so a dotted call is one searchable token.

    rustfmt breaks ``paper.transition_to_paper(x)`` across lines the moment the receiver chain
    grows, and a check searching for the literal ``paper.transition_to_paper(`` then silently
    stops matching — a guard that quietly finds nothing is worse than no guard, because its
    PASS line still claims the invariant is enforced. Positions are compared only within one
    normalised string, so the ordering checks stay valid.
    """

    return re.sub(r"\s*\.\s*", ".", text)


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
        body = _calls(_fn_block(orch_src, entry["method"]))
    except AssertionError as error:
        fail(str(error))

    probe_token = guard["probe_call"] + "("
    if probe_token not in body:
        fail(
            f"{entry['method']} does not call `{probe_token}` — the liquidation "
            "probe's outcome is the only legitimate entry to the gate"
        )

    # --- SyRS SYS-49c (d): the lockout is consulted BEFORE anything happens. --- #
    # Checked by byte position, not mere presence. A consult that ran after the probe would
    # still refuse the swap, but only after the demotion sequence had already cancelled orders
    # and submitted liquidations for a swap that was never permitted to start.
    lock_state_token = guard["lock_state_call"] + "("
    lock_engage_token = guard["lock_engage_call"] + "("
    at_lock = body.find(lock_state_token)
    if at_lock < 0:
        fail(
            f"{entry['method']} does not call `{lock_state_token}` — SyRS SYS-49c (d) blocks "
            "promotion until an operator resolves, which the gate cannot honour without "
            "reading the durable lockout"
        )
    if guard["lock_blocks_predicate"] not in body:
        fail(
            f"{entry['method']} never consults `{guard['lock_blocks_predicate']}()` — reading "
            "the lockout state and ignoring its verdict is not a block"
        )
    if at_lock > body.find(probe_token):
        fail(
            f"{entry['method']} consults `{lock_state_token}` AFTER `{probe_token}` — a swap "
            "held in demotion-pending must be refused before any demotion is attempted"
        )
    pending_factory = guard["pending_error_factory"] + "("
    if pending_factory not in body:
        fail(
            f"{entry['method']} does not produce `{pending_factory}` — a held lockout needs "
            "its own rejection, distinct from a fresh liquidation timeout"
        )
    inconsistency_factory = guard["probe_inconsistency_error_factory"] + "("
    if inconsistency_factory not in body:
        fail(
            f"{entry['method']} does not produce `{inconsistency_factory}` — a timeout the "
            "probe's own numbers contradict must block WITHOUT the premature destructive "
            "cancel, which needs a distinct rejection"
        )
    # The probe-inconsistency branch must block without cancelling. It sits between the
    # normalisation and the outcome match, so isolate it by taking the text from the
    # inconsistency factory backwards to the preceding `if let`.
    inconsistency_start = body.find("let probe_inconsistency")
    inconsistency_end = body.find(inconsistency_factory)
    if inconsistency_start < 0 or inconsistency_end < 0:
        fail(f"{entry['method']} has no isolatable probe-inconsistency branch")
    inconsistency_branch = body[inconsistency_start:inconsistency_end]
    if (guard["cancel_call"] + "(") in inconsistency_branch:
        fail(
            f"{entry['method']} cancels the unfilled liquidation order on the "
            "probe-inconsistency branch — a destructive broker action must not be taken on a "
            "report the gate is simultaneously declaring untrustworthy"
        )
    if lock_engage_token not in inconsistency_branch:
        fail(
            f"{entry['method']} does not engage the lockout on the probe-inconsistency "
            "branch — an unbelievable probe cannot vouch for flat positions, so promotion "
            "must stay blocked past this call"
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

    flat_arm = _calls(_arm_block(body, flat_token))
    timeout_arm = _calls(_arm_block(body, timeout_token))

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
    for required in (cancel_token, alert_token, event_token, lock_engage_token):
        if required not in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm is missing required "
                f"call `{required}` (SRS-RESV-004: cancel the unfilled order, "
                "notify the operator, engage the durable demotion-pending lockout, "
                "record the transition)"
            )
    # Ordering within the arm: the destructive broker action precedes the page (SAFE-002's
    # rule), and the lockout is engaged AFTER both so the persisted record carries their
    # outcomes — whether a live order may still be resting, and whether anyone was paged, are
    # exactly the facts an operator resolves against.
    if timeout_arm.find(cancel_token) > timeout_arm.find(alert_token):
        fail(
            f"{entry['method']} {timeout_token} arm dispatches the operator alert before the "
            "unfilled-order cancel — the destructive broker action goes first"
        )
    # Two-phase, and the order is the whole point. The lockout is engaged BEFORE anything
    # destructive, so a crash between the side effects and the write cannot lose the block; it
    # is amended AFTER, so the persisted record carries the outcomes an operator resolves
    # against. `(found by /codex:adversarial-review, SRS-RESV-004 r4 [high])`
    lock_amend_token = guard["lock_amend_call"] + "("
    if lock_amend_token not in timeout_arm:
        fail(
            f"{entry['method']} {timeout_token} arm never calls `{lock_amend_token}` — a "
            "lockout engaged before the side effects must be updated with their outcomes"
        )
    if timeout_arm.find(lock_engage_token) > timeout_arm.find(cancel_token):
        fail(
            f"{entry['method']} {timeout_token} arm engages the lockout AFTER the "
            "unfilled-order cancel — a crash in between would leave no durable block, and the "
            "next attempt would read an empty store as 'nothing is pending'"
        )
    if timeout_arm.find(lock_amend_token) < timeout_arm.find(alert_token):
        fail(
            f"{entry['method']} {timeout_token} arm amends the lockout before the operator "
            "alert — the persisted record must carry the side-effect outcomes it is resolved "
            "against"
        )
    # The flat arm must engage NOTHING: an in-time demotion leaves nothing pending, and a
    # lockout written there would block every subsequent swap.
    if lock_engage_token in flat_arm:
        fail(
            f"{entry['method']} {flat_token} arm calls `{lock_engage_token}` — a demotion "
            "that reached flat has nothing pending to lock out"
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


# --------------------------------------------------------------------------- #
# SRS-RESV-004 collectors — the whole class, not one instance
# --------------------------------------------------------------------------- #


def _check_forbidden_methods(orch_src: str, spec: dict) -> None:
    """A port must not offer a capability the gate is not allowed to have.

    ``DemotionPendingLock`` must carry no way to CLEAR a lockout (that is the operator's), and
    ``DemotionBrokerageControl`` must carry no ``disconnect`` (a routine changeover is not a
    kill switch). Every capability on a port is one the holder can exercise, so the check is on
    the port's shape rather than on the gate remembering not to call it.
    """

    forbidden = spec.get("forbidden_methods") or []
    if not forbidden:
        return
    body = _trait_body(orch_src, spec["trait"])
    present = [m for m in forbidden if re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if present:
        fail(
            f"{spec['trait']} declares forbidden method(s) {', '.join(present)} — "
            f"{spec.get('note', 'this capability must not be reachable through this port')}"
        )


def check_demotion_pending_lock_port(config: dict, orch_src: str) -> str:
    spec = contract_block(config)["demotion_pending_lock_port"]
    evidence = _check_port(
        orch_src,
        spec,
        "the SyRS SYS-49c (d) durable lockout — consulted before any flat result is "
        "accepted, engaged before a refusal returns",
    )
    _check_forbidden_methods(orch_src, spec)
    return (
        f"{evidence}, and declares NO clearing method "
        f"({', '.join(spec['forbidden_methods'])}) — only an operator resolves a lockout"
    )


def _sequence_module_source(config: dict) -> str:
    """The SYS-49b sequence module — where the four sequence ports are declared."""

    return module_source(contract_block(config)["sequence_guard"]["module"])


def check_signal_halt_port(config: dict) -> str:
    return _check_port(
        _sequence_module_source(config),
        contract_block(config)["signal_halt_port"],
        "SyRS SYS-49b (1)",
    )


def check_demotion_brokerage_port(config: dict) -> str:
    spec = contract_block(config)["demotion_brokerage_port"]
    source = _sequence_module_source(config)
    evidence = _check_port(source, spec, "SyRS SYS-49b (2) and (3)")
    _check_forbidden_methods(source, spec)
    return f"{evidence}, and NO disconnect — a demotion must never disconnect IB"


def check_paper_transition_port(config: dict) -> str:
    return _check_port(
        _sequence_module_source(config),
        contract_block(config)["paper_transition_port"],
        "the SyRS SYS-49b flat-start handoff to the internal simulation engine",
    )


def check_live_position_source_port(config: dict) -> str:
    return _check_port(
        _sequence_module_source(config),
        contract_block(config)["live_position_source_port"],
        "the fallible flat-confirmation input (an unreadable view is never an empty one)",
    )


def check_demotion_sequence_order(config: dict) -> str:
    """SyRS SYS-49b runs 1 → 2 → 3, and the order is a safety property.

    Enforced by BYTE POSITION inside ``execute_demotion_sequence``, so a refactor that reorders
    the phases fails here rather than in a live changeover. A resting order that fills after
    the liquidation re-opens the position it just closed; a strategy still emitting signals
    replaces resting orders as fast as they are cancelled.
    """

    spec = contract_block(config)["sequence_guard"]
    source = module_source(spec["module"])
    try:
        body = _calls(_fn_block(source, spec["entry_point"]))
    except AssertionError as error:
        fail(str(error))

    # SyRS SYS-2a, checked BEFORE the phase order: the positions this sequence liquidates are
    # ACCOUNT-level, so `request.demoting_strategy_id` decides whose book gets flattened. It must
    # be proven against the live registry before the first port call, or a stale/malformed swap
    # request liquidates the live account under an identity that does not own it.
    # `(found by /codex:adversarial-review, SRS-RESV-004 r1 [high])`
    authorization = spec["authorization_call"] + "("
    at_authorization = body.find(authorization)
    if at_authorization < 0:
        fail(
            f"{spec['entry_point']} does not call `{authorization}` — a demotion must prove the "
            "request names the CURRENT LIVE strategy before it liquidates an account-level book"
        )

    positions: list[tuple[str, int]] = []
    for call in spec["ordered_calls"]:
        token = call + "("
        index = body.find(token)
        if index < 0:
            fail(
                f"{spec['entry_point']} does not call `{token}` — SyRS SYS-49b requires all "
                "three demotion phases"
            )
        positions.append((call, index))
        if index < at_authorization:
            fail(
                f"{spec['entry_point']} calls `{call}` BEFORE `{authorization}` — every port "
                "call must sit behind the live-identity proof, or a refused demotion has "
                "already touched the account"
            )
    for (earlier, at_earlier), (later, at_later) in zip(positions, positions[1:], strict=False):
        if at_earlier >= at_later:
            fail(
                f"{spec['entry_point']} calls `{later}` before `{earlier}` — SyRS SYS-49b's "
                "phase order is load-bearing, not cosmetic"
            )
    return (
        f"atp-orchestrator::{spec['entry_point']} proves live identity via "
        f"`{spec['authorization_call']}` before ANY port call, then runs the SYS-49b phases in "
        "order: " + " < ".join(call for call, _ in positions)
    )


def check_paper_transition_is_flat_only(config: dict, orch_src: str) -> str:
    """'Transitions to paper only after live positions are flat', enforced structurally.

    Two halves, both needed. ``complete_demotion_to_paper`` must take the acceptance token the
    gate constructs ONLY on its flat arm — so no timed-out demotion can produce the argument.
    And that function must be the transition's ONLY call site in the whole crate, or a second
    caller could move a container to paper without the token at all.
    """

    spec = contract_block(config)["completion_guard"]
    source = module_source(spec["module"])
    try:
        body = _calls(_fn_block(source, spec["entry_point"]))
    except AssertionError as error:
        fail(str(error))

    token = spec["acceptance_token"]
    signature_start = source.find(f"pub fn {spec['entry_point']}")
    signature = source[signature_start : signature_start + 600]
    if f"&{token}" not in signature:
        fail(
            f"{spec['entry_point']} does not take a `&{token}` — the flat-only guarantee "
            "rests on requiring the acceptance token the timeout arm cannot construct"
        )
    transition_token = spec["transition_call"] + "("
    if transition_token not in body:
        fail(f"{spec['entry_point']} does not call `{transition_token}`")

    # Whole-crate sweep for a second call site.
    #
    # Enumerate EVERY call, then require that the single survivor is the one inside
    # complete_demotion_to_paper. An earlier version exempted the whole declaring FILE, which
    # let a second call site added to that same file pass unnoticed — the exemption has to be
    # scoped to the function that holds the guarantee, not to the file that happens to contain
    # it.
    method = "." + spec["transition_call"].split(".", 1)[-1] + "("
    call_sites: list[str] = []
    for path, text in orchestrator_crate_sources(config).items():
        for line_number, line in enumerate(_calls(text).split("\n"), start=1):
            if method not in line:
                continue
            stripped = line.strip()
            # A trait declaration or a port implementation is not a call site.
            if stripped.startswith(("///", "//", "*", "#")):
                continue
            if re.match(r"(pub\s+)?fn\s", stripped):
                continue
            call_sites.append(f"{path}:{line_number}: {stripped}")
    if len(call_sites) != 1:
        fail(
            f"the paper transition is called from {len(call_sites)} site(s); only "
            f"{spec['entry_point']} may call it, or the flat-only guarantee is bypassable:\n  "
            + "\n  ".join(call_sites or ["<none>"])
        )
    # ...and that one call must be the one inside the guarded function.
    if transition_token not in body:
        fail(
            f"the crate's only `{method}` call site is not inside {spec['entry_point']} — the "
            "acceptance token cannot be gating a transition it does not perform"
        )

    # The gate must remain the token's ONLY construction site anywhere in the crate —
    # otherwise a caller could mint an acceptance for a demotion that never reached flat and
    # hand it straight to the function above.
    #
    # Counted on real code only: the struct's own declaration and the Rustdoc that quotes the
    # token are not construction sites, and a naive substring count over the file finds four.
    construction_sites: list[str] = []
    for path, text in orchestrator_crate_sources(config).items():
        for line_number, line in enumerate(text.split("\n"), start=1):
            stripped = line.strip()
            if f"{token} {{" not in stripped:
                continue
            if stripped.startswith(("///", "//", "*", "#")) or "struct " in stripped:
                continue
            construction_sites.append(f"{path}:{line_number}: {stripped}")
    if len(construction_sites) != 1:
        fail(
            f"`{token} {{` is constructed at {len(construction_sites)} site(s); the gate's "
            "flat arm must be the ONLY one:\n  " + "\n  ".join(construction_sites or ["<none>"])
        )
    return (
        f"atp-orchestrator::{spec['entry_point']} requires the `&{token}` acceptance token "
        f"(constructed only by the gate's flat arm) and is the crate's ONLY "
        f"`{spec['transition_call']}` call site"
    )


def check_lockout_store(config: dict) -> str:
    """The lockout is three-state, and the fail-closed collapse lives in exactly one place."""

    spec = contract_block(config)["lockout_store"]
    source = module_source(spec["module"])
    try:
        body = _enum_body(source, spec["state_enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["state_variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(
            f"{spec['state_enum']} is missing variant(s) {', '.join(missing)} — unreadable, "
            "absent and held are three different facts and each needs its own state"
        )
    if spec["magic"] not in source:
        fail(f"{spec['module']} does not declare the magic marker {spec['magic']!r}")
    for entry_point in (spec["fail_closed_reader"], spec["resolve_entry_point"]):
        if not re.search(rf"pub fn {re.escape(entry_point)}\b", source):
            fail(f"{spec['module']} does not export `{entry_point}`")

    # The collapse itself: the reader's Err arm must produce the BLOCKING state. This is the
    # exact line a regression would flip, and flipping it would render a corrupt lockout as a
    # confident "nothing is pending".
    #
    # (Deliberately not attempted: a crate-wide sweep for "somebody else collapsed an error
    # into Clear". Matching a state and constructing one are textually identical in Rust, so
    # such a check would flag every legitimate `match` arm — and a guard that must be
    # suppressed everywhere is one nobody keeps. The behavioural coverage is
    # `resv_4_an_unreadable_lockout_blocks_exactly_like_a_held_one` and the gate's own
    # `resv_4_an_unreadable_lockout_blocks_promotion_exactly_like_a_held_one`.)
    reader_body = _fn_block(source, spec["fail_closed_reader"])
    err_arm_index = reader_body.find("Err(")
    if err_arm_index < 0:
        fail(
            f"{spec['fail_closed_reader']} has no `Err(` arm — it cannot be the fail-closed "
            "collapse if it never handles an unreadable store"
        )
    err_arm = reader_body[err_arm_index:]
    if f"{spec['state_enum']}::Unreadable" not in err_arm:
        fail(
            f"{spec['fail_closed_reader']}'s `Err(` arm does not produce "
            f"`{spec['state_enum']}::Unreadable` — an unreadable lockout must not collapse "
            "into a non-blocking state"
        )
    if f"{spec['state_enum']}::Clear" in err_arm:
        fail(
            f"{spec['fail_closed_reader']}'s `Err(` arm mentions "
            f"`{spec['state_enum']}::Clear` — an unreadable lockout is never an absent one"
        )

    # ...and the predicate the promotion path branches on must block on BOTH non-clear states.
    predicate_body = _fn_block(source, "blocks_promotion")
    for blocking in ("Pending", "Unreadable"):
        if blocking not in predicate_body:
            fail(
                f"blocks_promotion does not mention `{blocking}` — an unreadable lockout must "
                "block promotion exactly like a held one"
            )
    return (
        f"{spec['module']} declares {spec['state_enum']} with "
        f"{len(spec['state_variants'])} states ({', '.join(spec['state_variants'])}); "
        f"`{spec['fail_closed_reader']}` collapses an unreadable store to Unreadable (never "
        f"Clear), `blocks_promotion` blocks on Pending AND Unreadable, and "
        f"`{spec['resolve_entry_point']}` is the only clearing path"
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
    ("demotion_pending_lock_port", check_demotion_pending_lock_port, "orch"),
    ("signal_halt_port", check_signal_halt_port, "config"),
    ("demotion_brokerage_port", check_demotion_brokerage_port, "config"),
    ("paper_transition_port", check_paper_transition_port, "config"),
    ("live_position_source_port", check_live_position_source_port, "config"),
    ("resolve_demotion_guard", check_resolve_demotion_guard, "orch"),
    ("paper_transition_is_flat_only", check_paper_transition_is_flat_only, "orch"),
    # Whole-module collectors: they read the contract's named modules themselves.
    ("demotion_sequence_order", check_demotion_sequence_order, "config"),
    ("lockout_store", check_lockout_store, "config"),
)


def _run_static(config: dict, types_src: str, orch_src: str) -> list[str]:
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        if scope == "config":
            evidence.append(check(config))
            continue
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
