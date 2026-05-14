#!/usr/bin/env python3
"""Contract evidence script for feature SRS-ORCH-001.

Verifies that the strategy-orchestrator lifecycle gate declared in
``architecture/runtime_services.json`` (block ``orchestrator_lifecycle_contract``)
is reachable from the Rust crates ``crates/atp-types`` and
``crates/atp-orchestrator``.

SRS-ORCH-001 traces SyRS SYS-10 / SYS-13 / AC-12 / NFR-P9 / NFR-R5 /
NFR-S5 and StRS SN-1.10 / SN-2.03 / SN-2.05. The contract guarantees:

* ``ContainerLifecycleAction`` declares Create / Start / Stop / Restart /
  Destroy in ``atp-types`` (the SYS-10 lifecycle vocabulary).
* ``ContainerHealthState`` declares Healthy / Unresponsive in
  ``atp-types`` (the SYS-13 auto-restart trigger).
* ``LaunchReadiness`` declares ReadyWithinDeadline / DeadlineExceeded in
  ``atp-types`` (the NFR-P9 30-second budget classifier).
* ``StrategyLaunchRequest`` carries (strategy_id, mode, deployment_hash,
  deadline_millis) and rejects broker / ib_session_id / docker_image /
  container_id / vendor / host_path bleed.
* ``StrategyLaunchOutcome`` carries (strategy_id, ready_within_deadline,
  elapsed_millis, deadline_millis) and rejects the same bleed set.
* ``ContainerHealthEvent`` carries (state, strategy_id, action_taken,
  observed_at_seconds) and rejects the same bleed set (the dashboard
  fan-out must stay free of container-runtime shape).
* ``STRATEGY_STARTUP_DEADLINE_MS`` is declared as a ``pub const u64``
  equal to ``30_000`` (NFR-P9 single source of truth).
* The ``StrategyContainerRuntime`` and ``HealthCheckEventSink`` ports
  live in ``atp-orchestrator``.
* Inside ``StrategyOrchestrator::launch``, the body matches on
  ``runtime.start(...)``, the ReadyWithinDeadline leaf is the only
  call site of ``StrategyLaunchOutcome {``, and the DeadlineExceeded
  leaf produces ``OrderErrorCategory::StrategyStartupDeadlineExceeded``,
  calls ``runtime.destroy(`` to release the over-deadline container's
  resource profile (SRS-ORCH-002 / SyRS SYS-57), THEN records a
  ``ContainerHealthEvent`` via ``sink.record(`` with
  ``action_taken = ContainerLifecycleAction::Destroy``, and does NOT
  mutate the orchestrator registry behind the runtime port (no calls
  listed in the contract's ``forbidden_mutations`` array).
* Inside ``StrategyOrchestrator::observe_health``, the body matches on
  ``runtime.health(...)``, the Healthy leaf is read-only (no
  ``runtime.restart`` / ``runtime.destroy`` / ``runtime.stop`` /
  ``sink.record`` calls), and the Unresponsive leaf calls BOTH
  ``runtime.restart(`` AND ``sink.record(`` (the SYS-13 atomic
  auto-restart + dashboard fan-out binding).

Mirrors the PASS/FAIL output style of ``tools/pacing_budget_check.py``.

Invoke:
    python3 tools/orchestrator_lifecycle_check.py
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Re-uses the shared rust-source parsers introduced for the per-ERR
# contracts (SESSION 13..18). The historical TODO to hoist them into a
# dedicated module remains tracked at SESSION 18. SRS-ORCH-001's
# `LaunchReadiness` is a struct-variant enum, so the existing
# `_match_arm` (which only handles unit-variant arms) is supplemented
# with a local `_variant_arm` finder that accepts `Variant { ... } =>`.
from connectivity_check import _trait_body
from error_handling_check import _fn_block
from historical_data_check import _enum_body, _struct_body


def _variant_arm(body: str, variant_token: str) -> str:
    """Return the body of the match arm whose pattern starts with
    ``variant_token`` (e.g. ``LaunchReadiness::ReadyWithinDeadline``).
    Handles both unit variants (``Variant =>``) and struct variants
    (``Variant { field, .. } =>``). The arm body is everything up to
    the next top-level ``,`` or the closing ``}`` of the match.
    """
    pattern = re.compile(
        rf"{re.escape(variant_token)}\s*(?:\{{[^}}]*\}})?\s*=>\s*",
        re.DOTALL,
    )
    arm_match = pattern.search(body)
    if arm_match is None:
        fail(f"function body is missing match arm for `{variant_token}`")
    start = arm_match.end()
    depth = 0
    index = start
    in_string = False
    string_char = ""
    while index < len(body):
        char = body[index]
        if in_string:
            if char == "\\" and index + 1 < len(body):
                index += 2
                continue
            if char == string_char:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            string_char = char
        elif char in ("{", "("):
            depth += 1
        elif char in ("}", ")"):
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        index += 1
    return body[start:index]

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "architecture" / "runtime_services.json"


class OrchestratorLifecycleCheckError(AssertionError):
    pass


def fail(message: str) -> None:
    raise OrchestratorLifecycleCheckError(message)


def load_config(root: Path = ROOT) -> dict:
    return json.loads(
        (root / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )


def contract_block(config: dict) -> dict:
    if "orchestrator_lifecycle_contract" not in config:
        fail("architecture metadata is missing orchestrator_lifecycle_contract")
    return config["orchestrator_lifecycle_contract"]


def types_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["types_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"types crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


def orchestrator_source(config: dict, root: Path = ROOT) -> str:
    block = contract_block(config)
    crate_path = root / block["orchestrator_crate"]["path"]
    source_path = crate_path / "src" / "lib.rs"
    if not source_path.exists():
        fail(f"orchestrator crate source missing: {source_path.relative_to(root)}")
    return source_path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-check evidence collectors
# --------------------------------------------------------------------------- #


def check_lifecycle_action_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["lifecycle_action"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"actions ({', '.join(spec['variants'])}) — the SyRS SYS-10 "
        "strategy container lifecycle vocabulary"
    )


def check_container_health_state_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["container_health_state"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"states ({', '.join(spec['variants'])}) — the SyRS SYS-13 "
        "auto-restart trigger classifier"
    )


def check_launch_readiness_enum(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["launch_readiness"]
    try:
        body = _enum_body(types_src, spec["enum"])
    except AssertionError as error:
        fail(str(error))
    missing = [v for v in spec["variants"] if not re.search(rf"\b{re.escape(v)}\b", body)]
    if missing:
        fail(f"{spec['enum']} enum is missing variants: {', '.join(missing)}")
    return (
        f"atp-types declares {spec['enum']} with {len(spec['variants'])} "
        f"variants ({', '.join(spec['variants'])}) — the NFR-P9 30-second "
        "launch-budget classifier"
    )


def check_strategy_launch_request_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["strategy_launch_request"]
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
            f"{spec['struct']} leaks vendor / container-runtime field(s): {', '.join(leaks)} "
            "(SRS-ORCH-001 launch envelopes must not carry "
            "broker/session/docker/container/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/container-runtime fields"
    )


def check_strategy_launch_outcome_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["strategy_launch_outcome"]
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
            f"{spec['struct']} leaks vendor / container-runtime field(s): {', '.join(leaks)} "
            "(SRS-ORCH-001 launch outcomes must not carry "
            "broker/session/docker/container/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/container-runtime fields"
    )


def check_container_health_event_struct(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["container_health_event"]
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
            f"{spec['struct']} leaks vendor / container-runtime field(s): {', '.join(leaks)} "
            "(SRS-ORCH-001 events must not carry "
            "broker/session/docker/container/vendor identifiers)"
        )
    return (
        f"atp-types declares {spec['struct']} with the "
        f"{len(spec['required_fields'])} required fields "
        f"({', '.join(spec['required_fields'])}) and rejects "
        f"{len(spec['forbidden_fields'])} forbidden vendor/container-runtime fields"
    )


def check_startup_deadline_constant(config: dict, types_src: str) -> str:
    block = contract_block(config)
    spec = block["startup_deadline_constant"]
    name = spec["name"]
    expected_value = spec["value"]
    # The constant is declared as `pub const NAME: u64 = 30_000;` —
    # accept either underscored or plain digit form.
    pattern = re.compile(
        rf"\bpub\s+const\s+{re.escape(name)}\s*:\s*u64\s*=\s*([0-9_]+)\s*;",
    )
    match = pattern.search(types_src)
    if match is None:
        fail(
            f"{name} is not declared as `pub const {name}: u64` in atp-types — "
            "NFR-P9 requires a single source of truth for the 30-second budget"
        )
    literal = match.group(1).replace("_", "")
    if int(literal) != expected_value:
        fail(
            f"{name} has value {literal} but NFR-P9 requires {expected_value}"
        )
    return (
        f"atp-types declares {name} = {expected_value} (ms) — the NFR-P9 "
        "single source of truth for strategy container startup time"
    )


def check_container_runtime_port(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    spec = block["container_runtime_port"]
    body = _trait_body(orch_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-orchestrator declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} methods ({', '.join(spec['methods'])}) — "
        "the AC-12 orchestrator-only container lifecycle port"
    )


def check_health_event_sink_port(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    spec = block["health_event_sink_port"]
    body = _trait_body(orch_src, spec["trait"])
    missing = [m for m in spec["methods"] if not re.search(rf"\bfn\s+{re.escape(m)}\b", body)]
    if missing:
        fail(f"{spec['trait']} trait is missing methods: {', '.join(missing)}")
    return (
        f"atp-orchestrator declares port trait {spec['trait']} with "
        f"{len(spec['methods'])} method ({', '.join(spec['methods'])}) — "
        "the SyRS SYS-13 dashboard + audit-log + notification fan-out channel"
    )


def check_launch_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    entry = block["launch_entry_point"]
    guard = block["launch_guard"]
    try:
        body = _fn_block(orch_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    runtime_call_token = guard["runtime_call"] + "("
    if runtime_call_token not in body:
        fail(
            f"{entry['method']} does not call `{runtime_call_token}` — "
            "the launch-readiness classification is the only legitimate entry to the gate"
        )

    ready_token = f"{guard['state_enum']}::{guard['ready_variant']}"
    exceeded_token = f"{guard['state_enum']}::{guard['exceeded_variant']}"
    for token in (ready_token, exceeded_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "SRS-ORCH-001 requires both ReadyWithinDeadline and "
                "DeadlineExceeded to be handled inside the launch-readiness match"
            )

    ready_arm = _variant_arm(body, ready_token)
    exceeded_arm = _variant_arm(body, exceeded_token)

    accepted_token = guard["accepted_struct"] + " {"
    if accepted_token not in ready_arm:
        fail(
            f"{entry['method']} {ready_token} leaf does not produce "
            f"`{accepted_token}` — the ReadyWithinDeadline leaf is the only "
            "legitimate construction site for the launch outcome"
        )
    if accepted_token in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf produces "
            f"`{accepted_token}` — SRS-ORCH-001 requires zero acceptance "
            "side effect when the launch deadline is breached"
        )

    rejection = block["rejection_category"]
    category_token = f"OrderErrorCategory::{rejection}"
    factory_token = "StructuredOrchestratorError::startup_deadline_exceeded("
    # The DeadlineExceeded arm must construct the rejection envelope
    # through the category-pinned factory (which references
    # `OrderErrorCategory::StrategyStartupDeadlineExceeded` inside
    # atp-types) or by naming the category directly. Either is
    # acceptable; both signal that the SyRS SYS-64 wire string source
    # of truth is being honoured.
    if category_token not in exceeded_arm and factory_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf must produce "
            f"{category_token} (directly or via the "
            f"`{factory_token.rstrip('(')}` factory — the SyRS SYS-64 "
            "wire string source of truth)"
        )

    sink_call_token = guard["sink_call"] + "("
    if sink_call_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf is missing required "
            f"call `{sink_call_token}` (SyRS SYS-13 dashboard fan-out)"
        )

    # SRS-ORCH-002 + SyRS SYS-57 + NFR-R5: the over-deadline container
    # must release its resource profile before the gate returns the
    # rejection. Without `runtime.destroy(` here the half-launched
    # container is orphaned, the host memory safety margin slowly
    # drains, AND the `action_taken = Destroy` event payload (asserted
    # below) becomes a lie to the dashboard / audit log about what the
    # orchestrator actually did.
    destroy_call_token = guard["runtime_destroy_call"] + "("
    if destroy_call_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf is missing required "
            f"call `{destroy_call_token}` — SRS-ORCH-002 / SyRS SYS-57 "
            "requires the over-deadline container to release its resource "
            "profile so the host memory budget is not consumed by an "
            "orphaned half-launched container"
        )

    # The event payload's action_taken must match the action the gate
    # actually performs on the over-deadline branch. Drift between
    # payload-claim and behaviour is a public-contract lie.
    action_taken_token = guard["exceeded_action_taken"]
    if action_taken_token not in exceeded_arm:
        fail(
            f"{entry['method']} {exceeded_token} leaf does not stamp "
            f"`action_taken: {action_taken_token}` on the emitted event — "
            "the audit log must record the actual lifecycle action the "
            "orchestrator performed (SRS-ORCH-001 / SyRS SYS-13)"
        )

    # The order of the calls also matters: the destroy must precede the
    # event so a sink failure cannot mask the resource release. This is
    # a positional check on the arm body.
    destroy_pos = exceeded_arm.find(destroy_call_token)
    sink_pos = exceeded_arm.find(sink_call_token)
    if destroy_pos > sink_pos:
        fail(
            f"{entry['method']} {exceeded_token} leaf records the event "
            "BEFORE destroying the container — SRS-ORCH-001 requires the "
            "destroy to precede the dashboard fan-out so a sink failure "
            "cannot mask the resource release"
        )

    # Zero-mutation invariant on refusal: the DeadlineExceeded leaf must
    # not call any of the forbidden mutators listed in the contract.
    # The refused launch must leave the orchestrator's registry exactly
    # as it found it.
    forbidden_mutations = guard.get("forbidden_mutations", [])
    for mutator in forbidden_mutations:
        token = f"{mutator}("
        if token in exceeded_arm:
            fail(
                f"{entry['method']} {exceeded_token} leaf calls "
                f"`{token}` — SRS-ORCH-001 requires zero side effect on "
                "the orchestrator registry when the gate refuses the launch"
            )

    deadline_const = guard.get("deadline_constant")
    if deadline_const and deadline_const not in orch_src:
        fail(
            f"atp-orchestrator does not reference {deadline_const} — "
            "NFR-P9's single source of truth must be reachable from the "
            "orchestrator crate (e.g. via `startup_deadline_millis`)"
        )

    return (
        f"atp-orchestrator::{entry['method']} gates "
        f"`{guard['accepted_struct']}` on a match {ready_token} via "
        f"`{guard['runtime_call']}`; the {exceeded_token} leaf calls "
        f"`{guard['runtime_destroy_call']}` to release the over-deadline "
        f"container, records a {block['container_health_event']['struct']} "
        f"with action_taken={guard['exceeded_action_taken']} via "
        f"`{guard['sink_call']}`, emits OrderErrorCategory::{rejection}, "
        "and mutates nothing on the orchestrator registry (SRS-ORCH-001)"
    )


def check_health_observe_guard(config: dict, orch_src: str) -> str:
    block = contract_block(config)
    entry = block["health_observe_entry_point"]
    guard = block["health_observe_guard"]
    try:
        body = _fn_block(orch_src, entry["method"])
    except AssertionError as error:
        fail(str(error))

    health_call_token = guard["runtime_health_call"] + "("
    if health_call_token not in body:
        fail(
            f"{entry['method']} does not call `{health_call_token}` — "
            "the health classification is the only legitimate entry to the gate"
        )

    healthy_token = f"{guard['state_enum']}::{guard['healthy_variant']}"
    unresponsive_token = f"{guard['state_enum']}::{guard['unresponsive_variant']}"
    for token in (healthy_token, unresponsive_token):
        if token not in body:
            fail(
                f"{entry['method']} is missing the `{token}` branch — "
                "SyRS SYS-13 requires both Healthy and Unresponsive to be "
                "handled inside the health match"
            )

    healthy_arm = _variant_arm(body, healthy_token)
    unresponsive_arm = _variant_arm(body, unresponsive_token)

    restart_call_token = guard["runtime_restart_call"] + "("
    if restart_call_token not in unresponsive_arm:
        fail(
            f"{entry['method']} {unresponsive_token} leaf is missing required "
            f"call `{restart_call_token}` — SyRS SYS-13 mandates auto-restart "
            "of unresponsive containers"
        )

    sink_call_token = guard["sink_call"] + "("
    if sink_call_token not in unresponsive_arm:
        fail(
            f"{entry['method']} {unresponsive_token} leaf is missing required "
            f"call `{sink_call_token}` — SyRS SYS-13 binds the restart action "
            "to the dashboard fan-out in one transaction"
        )

    # The Healthy arm must be read-only — no restart / destroy / stop /
    # event. SYS-13's selectivity invariant: a healthy probe never
    # triggers a side effect, otherwise the dashboard distorts and the
    # operator stops trusting the auto-restart counter.
    forbidden_on_healthy = guard.get("forbidden_mutations_on_healthy", [])
    for mutator in forbidden_on_healthy:
        token = f"{mutator}("
        if token in healthy_arm:
            fail(
                f"{entry['method']} {healthy_token} leaf calls `{token}` — "
                "SYS-13 selectivity: a healthy observation must be read-only"
            )

    return (
        f"atp-orchestrator::{entry['method']} matches {healthy_token} "
        f"read-only, and on {unresponsive_token} calls "
        f"`{guard['runtime_restart_call']}` AND records a "
        f"{block['container_health_event']['struct']} via "
        f"`{guard['sink_call']}` — SyRS SYS-13 auto-restart + dashboard fan-out"
    )


def check_cargo_test_smoke(config: dict) -> str:
    block = contract_block(config)
    crate = block["orchestrator_crate"]["crate"]
    cargo = shutil.which("cargo")
    if cargo is None:
        return f"cargo test -p {crate}: skipped (cargo not on PATH)"
    result = subprocess.run(
        [cargo, "test", "-p", crate, "--lib", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"cargo test -p {crate} failed:\n{result.stdout}\n{result.stderr}")
    combined = result.stdout + result.stderr
    if "test result: ok" not in combined and "0 failed" not in combined:
        fail(f"cargo test output did not include `test result: ok`:\n{combined}")
    integ = subprocess.run(
        [
            cargo,
            "test",
            "-p",
            crate,
            "--test",
            "orch_1_lifecycle_contract",
            "--quiet",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if integ.returncode != 0:
        fail(
            f"cargo test -p {crate} --test orch_1_lifecycle_contract failed:\n"
            f"{integ.stdout}\n{integ.stderr}"
        )
    return (
        f"cargo test -p {crate} --lib + orch_1_lifecycle_contract: PASS "
        "(lifecycle gate + auto-restart + zero registry mutation verified)"
    )


# --------------------------------------------------------------------------- #
# Coverage and entry point
# --------------------------------------------------------------------------- #


_STATIC_CHECKS = (
    ("lifecycle_action", check_lifecycle_action_enum, "types"),
    ("container_health_state", check_container_health_state_enum, "types"),
    ("launch_readiness", check_launch_readiness_enum, "types"),
    ("strategy_launch_request", check_strategy_launch_request_struct, "types"),
    ("strategy_launch_outcome", check_strategy_launch_outcome_struct, "types"),
    ("container_health_event", check_container_health_event_struct, "types"),
    ("startup_deadline_constant", check_startup_deadline_constant, "types"),
    ("container_runtime_port", check_container_runtime_port, "orch"),
    ("health_event_sink_port", check_health_event_sink_port, "orch"),
    ("launch_guard", check_launch_guard, "orch"),
    ("health_observe_guard", check_health_observe_guard, "orch"),
)


def run_checks() -> list[str]:
    config = load_config()
    types_src = types_source(config)
    orch_src = orchestrator_source(config)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    evidence.append(check_cargo_test_smoke(config))
    return evidence


def assert_orchestrator_lifecycle_static(
    config: dict, root: Path = ROOT
) -> list[str]:
    """Static checks usable from ``tools/architecture_check.py`` (no cargo)."""
    types_src = types_source(config, root)
    orch_src = orchestrator_source(config, root)
    evidence: list[str] = []
    for _, check, scope in _STATIC_CHECKS:
        source = types_src if scope == "types" else orch_src
        evidence.append(check(config, source))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRS-ORCH-001 contract evidence")
    parser.parse_args(argv)

    try:
        evidence = run_checks()
    except OrchestratorLifecycleCheckError as error:
        print(f"SRS-ORCH-001 FAIL: {error}", file=sys.stderr)
        return 1

    print("SRS-ORCH-001 PASS")
    for item in evidence:
        print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
