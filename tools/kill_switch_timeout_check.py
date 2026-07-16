#!/usr/bin/env python3
"""Contract evidence script for feature ERR-8.

Verifies that the kill-switch liquidation-timeout gate declared in
``architecture/runtime_services.json`` (block ``kill_switch_timeout_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-execution``.

ERR-8 traces SRS-SAFE-002 (SyRS SYS-44b; StRS SN-1.11). The contract
guarantees:

  (a) ``KillSwitchLiquidationOutcome`` declares the binary FilledBeforeTimeout
      / TimedOutUnfilled decision in ``atp-types`` (the unbuilt 30 s async wait
      loop is the deferred runtime — the slice models only the outcome the loop
      produces).
  (b) ``KillSwitchTimeoutRequest`` carries the live strategy, the domain
      ``UnfilledLiquidationOrder`` (so "log the unfilled order details" is
      satisfiable without leaking vendor IB-order identifiers), and the
      timeout; neither it nor the alert/audit events leak broker / IB-order /
      vendor / container fields. The cancel + disconnect flow through a port,
      never a field on the envelope.
  (c) ``OperatorAlertChannel`` declares (at least) the SYS-44b email/SMS pair,
      and ``KillSwitchAlertEvent`` / ``KillSwitchTimeoutEvent`` carry their
      required fields and reject the forbidden allowlist.
  (d) the four ports — ``KillSwitchLiquidationProbe`` (timing authority,
      read-only), ``KillSwitchOperatorAlertSink``, ``IbLiquidationCleanup``
      (cancel + disconnect, the two IB-adapter operations), and
      ``KillSwitchTimeoutEventSink`` — live in ``atp-execution``.
  (e) inside ``ExecutionEngine::resolve_kill_switch_timeout``, the body matches
      on ``liquidation.await_filled_or_timeout(...)``; the FilledBeforeTimeout
      arm is the only construction site of ``KillSwitchLiquidationResolved {``
      and dispatches NO alert, cancels NOTHING, and disconnects NOTHING; the
      TimedOutUnfilled arm pages the operator over email + SMS, cancels the
      unfilled order, disconnects from IB, records the audit event, and
      produces ``OrderErrorCategory::KillSwitchLiquidationTimeout`` (via the
      category-pinned factory) — the SYS-44b sequence.
  (f) the fill-confirmation failure taxonomy is TYPED
      (``KillSwitchProbeError``: ConnectivityBlocked / OrderStateUnavailable /
      ProbeTimeout), and an INCONSISTENT probe report (a ``TimedOutUnfilled``
      before the request's deadline, or with a mismatched ``timeout_seconds``)
      is rejected via ``StructuredKillSwitchTimeoutError::probe_inconsistent``
      (distinct discriminator, ``not_attempted()`` cleanup) instead of firing
      the destructive cancel + disconnect early.

Mirrors the PASS/FAIL output style of ``tools/hot_swap_demotion_check.py``.

Invoke:
    python3 tools/kill_switch_timeout_check.py
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


class KillSwitchTimeoutCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise KillSwitchTimeoutCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads((root / "architecture" / "runtime_services.json").read_text(encoding="utf-8"))


def contract_block(config: dict) -> dict:
    if "kill_switch_timeout_contract" not in config:
        fail("architecture metadata is missing kill_switch_timeout_contract")
    return config["kill_switch_timeout_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["types_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def execution_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    source_path = root / block["execution_crate"]["path"] / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"execution crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Precise block-arm extractor
# --------------------------------------------------------------------------- #
#
# `_rust_parser._variant_arm` extracts up to the next top-level comma, which
# over-runs a block-bodied arm that carries no trailing comma (the repo style
# for `=> { ... }` arms). ERR-8 asserts *negatively* on the filled arm (it must
# NOT page/cancel/disconnect) and *positively* on the timeout arm, so each arm
# must be isolated exactly. This helper finds `VARIANT { .. } => {` and returns
# the balanced block.


def _arm_block(body: str, variant_token: str) -> str:
    pattern = re.compile(
        rf"{re.escape(variant_token)}\s*(?:\{{[^{{}}]*\}})?\s*=>\s*\{{",
        re.DOTALL,
    )
    match = pattern.search(body)
    if match is None:
        fail(f"resolve_kill_switch_timeout is missing a block arm for `{variant_token}`")
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
            f"{', '.join(leaks)} (ERR-8 {kind} must not carry "
            "broker/session/IB-order/tick/vendor/container identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden broker/IB-order/vendor fields"
    )


def check_timeout_request_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["timeout_request"], "request envelope")


def check_unfilled_order_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["unfilled_order"], "unfilled-order log")


def check_alert_event_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["alert_event"], "alert event")


def check_timeout_event_struct(config: dict, types_src: str) -> str:
    return _check_struct(types_src, contract_block(config)["timeout_event"], "timeout event")


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
        f"required variant(s) ({', '.join(spec['variants'])}) — {note}"
    )


def check_liquidation_outcome_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["liquidation_outcome"],
        "the binary SRS-SAFE-002 liquidation-timeout decision (the 30 s wait "
        "loop that produces it is the deferred runtime)",
    )


def check_operator_alert_channel_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["operator_alert_channel"],
        "the SYS-44b email/SMS operator-page pair",
    )


def check_side_effect_outcome_enum(config: dict, types_src: str) -> str:
    return _check_enum(
        types_src,
        contract_block(config)["side_effect_outcome"],
        "the observable timeout-branch side-effect outcome (a failed cancel / "
        "missed page / failed disconnect is recorded as Failed, not silently "
        "indistinguishable from success)",
    )


def _check_port(exec_src: str, spec: dict, note: str) -> str:
    try:
        body = _trait_body(exec_src, spec["trait"])
    except AssertionError as error:
        fail(str(error))
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-execution declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method(s) ({', '.join(spec['methods'])}) — {note}"
    )


def check_liquidation_probe_port(config: dict, exec_src: str) -> str:
    return _check_port(
        exec_src,
        contract_block(config)["liquidation_probe_port"],
        "the read-only filled-vs-timeout timing authority (no mutators)",
    )


def check_probe_error_enum(config: dict, exec_src: str) -> str:
    spec = contract_block(config)["probe_error"]
    try:
        body = _enum_body(exec_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-execution declares the typed {spec['enum']} taxonomy with "
        f"{len(spec['variants'])} variant(s) ({', '.join(spec['variants'])}) — "
        "every degraded fill-confirmation path fails the gate closed with a "
        "distinct kind discriminator (no automated cancel/disconnect)"
    )


def check_probe_inconsistent_factory(config: dict, types_src: str) -> str:
    guard = contract_block(config)["guard"]
    factory = guard["probe_inconsistent_factory"].rsplit("::", 1)[-1]
    if not re.search(rf"\bpub\s+fn\s+{re.escape(factory)}\b", types_src):
        fail(
            f"atp-types is missing `pub fn {factory}` — the premature/mismatched "
            "TimedOutUnfilled rejection needs its category-pinned factory"
        )
    try:
        body = _fn_block(types_src, factory)
    except AssertionError as error:
        fail(str(error))
    if '"KillSwitchLiquidationProbeInconsistent"' not in body:
        fail(
            f"`{factory}` does not pin the distinct "
            '`"KillSwitchLiquidationProbeInconsistent"` error_type discriminator'
        )
    if "not_attempted()" not in body:
        fail(
            f"`{factory}` must construct KillSwitchCleanupOutcome::not_attempted() "
            "— an inconsistent probe report takes NO automated order/session action"
        )
    return (
        f"atp-types declares StructuredKillSwitchTimeoutError::{factory} with the "
        "distinct KillSwitchLiquidationProbeInconsistent discriminator and a "
        "not_attempted() cleanup record (nothing destructive ran)"
    )


def check_operator_alert_sink_port(config: dict, exec_src: str) -> str:
    return _check_port(
        exec_src,
        contract_block(config)["operator_alert_sink_port"],
        "the SYS-44b email/SMS operator-page dispatch channel",
    )


def check_ib_cleanup_port(config: dict, exec_src: str) -> str:
    return _check_port(
        exec_src,
        contract_block(config)["ib_cleanup_port"],
        "the SYS-44b IB-adapter cancel + disconnect pair (deferred SRS-EXE-006)",
    )


def check_timeout_event_sink_port(config: dict, exec_src: str) -> str:
    return _check_port(
        exec_src,
        contract_block(config)["timeout_event_sink_port"],
        "the structured kill-switch-timeout audit record (SRS-LOG-001 / SRS-UI-001)",
    )


def check_resolve_kill_switch_timeout_guard(config: dict, exec_src: str) -> str:
    block = contract_block(config)
    entry = block["entry_point"]
    guard = block["guard"]
    try:
        body = _fn_block(exec_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    probe_token = guard["probe_call"] + "("
    if probe_token not in body:
        fail(
            f"{entry['method']} does not call `{probe_token}` — the liquidation "
            "probe's outcome is the only legitimate entry to the gate"
        )

    # The probe is fallible: a probe failure must fail closed via the distinct
    # probe-unavailable refusal, never be misclassified as a confirmed timeout.
    probe_error_token = guard["probe_error_factory"] + "("
    if probe_error_token not in body:
        fail(
            f"{entry['method']} does not call `{probe_error_token}` — a probe "
            "failure must fail closed with the distinct probe-unavailable "
            "refusal (no automated cancel/disconnect on an unconfirmable state)"
        )

    # Outcome-consistency hardening: a TimedOutUnfilled reported BEFORE the
    # request's deadline (or with a mismatched timeout_seconds) must be
    # rejected via the probe-inconsistency factory, never trusted into the
    # destructive cancel + disconnect.
    probe_inconsistent_token = guard["probe_inconsistent_factory"] + "("
    if probe_inconsistent_token not in body:
        fail(
            f"{entry['method']} does not call `{probe_inconsistent_token}` — a "
            "premature/mismatched TimedOutUnfilled report must be rejected "
            "without firing the destructive cleanup early"
        )

    enum = guard["outcome_enum"]
    filled_token = f"{enum}::{guard['filled_variant']}"
    timeout_token = f"{enum}::{guard['timeout_variant']}"
    for token in (filled_token, timeout_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — ERR-8 "
                "requires both the filled and the timeout outcomes to be handled"
            )

    filled_arm = _arm_block(body, filled_token)
    timeout_arm = _arm_block(body, timeout_token)

    accepted_token = guard["accepted_struct"] + " {"
    alert_token = guard["alert_call"] + "("
    cancel_token = guard["cancel_call"] + "("
    disconnect_token = guard["disconnect_call"] + "("
    event_token = guard["event_call"] + "("

    # --- Filled arm: accept + audit; NO page, NO cancel, NO disconnect. ----- #
    if accepted_token not in filled_arm:
        fail(
            f"{entry['method']} {filled_token} arm does not construct "
            f"`{accepted_token}` — the filled outcome is the only acceptance site"
        )
    if event_token not in filled_arm:
        fail(
            f"{entry['method']} {filled_token} arm is missing `{event_token}` — "
            "the filled outcome must still record the audit transition"
        )
    for forbidden in guard["filled_forbidden_calls"]:
        token = forbidden + "("
        if token in filled_arm:
            fail(
                f"{entry['method']} {filled_token} arm calls `{token}` — a "
                "liquidation that filled in time raises no page and cancels / "
                "disconnects nothing (the SYS-44b error path must not engage)"
            )

    # --- Timeout arm: page(email+SMS) + cancel + disconnect + event + ------- #
    # --- reject; NO acceptance. -------------------------------------------- #
    for required in (alert_token, cancel_token, disconnect_token, event_token):
        if required not in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm is missing required "
                f"call `{required}` (SYS-44b: log the unfilled order, notify by "
                "email and SMS, cancel the unfilled order, disconnect from IB)"
            )
    for channel in guard["alert_channels"]:
        if channel not in timeout_arm:
            fail(
                f"{entry['method']} {timeout_token} arm is missing alert channel "
                f"`{channel}` — SYS-44b notifies the operator by email AND SMS"
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
            "— a timed-out liquidation is not an acceptance"
        )

    return (
        f"atp-execution::{entry['method']} matches "
        f"`{guard['probe_call']}`; the {filled_token} arm is the sole "
        f"`{guard['accepted_struct']}` site (no page, no cancel, no disconnect); "
        f"the {timeout_token} arm pages via `{guard['alert_call']}` over email + "
        f"SMS, cancels via `{guard['cancel_call']}`, disconnects via "
        f"`{guard['disconnect_call']}`, records via `{guard['event_call']}`, and "
        f"emits OrderErrorCategory::{block['rejection_category']} (ERR-8)"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["execution_crate"]["crate"]
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
        [cargo, "test", "-p", crate, "--test", "err_8_kill_switch_liquidation_timeout", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test err_8_kill_switch_liquidation_timeout failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + err_8_kill_switch_liquidation_timeout: PASS "
        "(timeout page+cancel+disconnect + filled-arm selectivity verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("timeout_request", check_timeout_request_struct, "types"),
    ("unfilled_order", check_unfilled_order_struct, "types"),
    ("liquidation_outcome", check_liquidation_outcome_enum, "types"),
    ("operator_alert_channel", check_operator_alert_channel_enum, "types"),
    ("alert_event", check_alert_event_struct, "types"),
    ("side_effect_outcome", check_side_effect_outcome_enum, "types"),
    ("timeout_event", check_timeout_event_struct, "types"),
    ("liquidation_probe_port", check_liquidation_probe_port, "exec"),
    ("probe_error", check_probe_error_enum, "exec"),
    ("probe_inconsistent_factory", check_probe_inconsistent_factory, "types"),
    ("operator_alert_sink_port", check_operator_alert_sink_port, "exec"),
    ("ib_cleanup_port", check_ib_cleanup_port, "exec"),
    ("timeout_event_sink_port", check_timeout_event_sink_port, "exec"),
    ("resolve_kill_switch_timeout_guard", check_resolve_kill_switch_timeout_guard, "exec"),
)


def _run_static(config: dict, types_src: str, exec_src: str) -> list[str]:
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else exec_src
        evidence.append(check(config, source))
    return evidence


def assert_kill_switch_timeout_static(config: dict, root: Path = ROOT) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    return _run_static(config, types_source(config, root), execution_source(config, root))


def run_checks() -> list[str]:
    config = load_config()
    evidence = _run_static(config, types_source(config), execution_source(config))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ERR-8 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except KillSwitchTimeoutCheckError as error:
        print(f"ERR-8 FAIL: {error}", file=sys.stderr)
        return 1

    print("ERR-8 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
