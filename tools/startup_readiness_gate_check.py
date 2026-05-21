#!/usr/bin/env python3
"""Startup readiness gate SDK-surface contract check for ERR-9.

Verifies that ``python/atp_readiness/`` matches the
``startup_readiness_gate_contract`` block in
``architecture/runtime_services.json`` and that the gate state machine
enforces every allowed / forbidden transition + every audit-trail field
documented there. The check is the deterministic mirror of the L3 contract
test rig; it runs at every boot via ``init.sh`` and on CI so contract drift
cannot land silently.

The check intentionally does NOT exercise the runtime readiness probes
(SRS-MD-006) or the log / dashboard / API sinks — those are deferred per
the contract block's ``deferred[]`` list. The PASS line is
``ERR-9 SDK-SURFACE PASS`` (not bare ``PASS``) to mirror the SDK-004 partial-
pass pattern: the SDK surface is locked, the runtime / sink halves still
hold the feature_list entry at ``passes:false``.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from atp_config import (  # noqa: E402  (path manipulation must come first)
    PLACEHOLDER_VALUE,
    REQUIRED_KEYS,
    Severity,
)
from atp_readiness import (  # noqa: E402
    GateState,
    GateTransitionError,
    OperatorOverride,
    OverrideAuditError,
    PreTradeHoldError,
    ReadinessGate,
    ReadinessGateError,
)
from atp_readiness import errors as readiness_errors  # noqa: E402
from atp_readiness import gate as gate_module  # noqa: E402
from atp_readiness import override as override_module  # noqa: E402

_CONTRACT_BLOCK = "startup_readiness_gate_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"


class StartupReadinessGateCheckError(AssertionError):
    """Raised when the gate diverges from the contract block."""


def _fail(message: str) -> None:
    raise StartupReadinessGateCheckError(message)


def _load_contract() -> dict[str, Any]:
    raw = json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))
    block = raw.get(_CONTRACT_BLOCK)
    if not isinstance(block, dict):
        _fail(f"runtime_services.json is missing the {_CONTRACT_BLOCK!r} block")
    return block


def _default_env() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


def _missing_key_env() -> dict[str, str]:
    env = _default_env()
    env.pop("DATABENTO_API_KEY", None)
    return env


def check_required_exports(block: dict[str, Any]) -> str:
    import atp_readiness

    expected = sorted(block["required_exports"])
    actual = sorted(atp_readiness.__all__)
    if expected != actual:
        _fail(
            f"atp_readiness.__all__ ({actual}) does not match contract "
            f"required_exports ({expected})"
        )
    error_exports = set(block["required_error_exports"])
    missing_error = error_exports - set(actual)
    if missing_error:
        _fail(f"atp_readiness.__all__ is missing required error exports: {sorted(missing_error)}")
    return (
        f"atp_readiness.__all__ exports the {len(expected)} contracted symbols "
        f"(including all {len(error_exports)} error types)"
    )


def check_gate_state_variants(block: dict[str, Any]) -> str:
    expected = list(block["gate_state_variants"])
    actual = [state.value for state in GateState]
    if expected != actual:
        _fail(f"GateState variants {actual} do not match contract gate_state_variants {expected}")
    return f"GateState declares the {len(expected)} contracted variants in order"


def _transition_set(rows: Iterable[Iterable[str]]) -> set[tuple[str, str]]:
    return {(row[0], row[1]) for row in rows}


def check_transition_completeness(block: dict[str, Any]) -> str:
    variants = block["gate_state_variants"]
    all_pairs = {(a, b) for a in variants for b in variants}
    allowed = _transition_set(block["allowed_transitions"])
    forbidden = _transition_set(block["forbidden_transitions"])
    if allowed & forbidden:
        _fail(
            f"allowed_transitions and forbidden_transitions overlap: {sorted(allowed & forbidden)}"
        )
    covered = allowed | forbidden
    if covered != all_pairs:
        _fail(
            "allowed_transitions ∪ forbidden_transitions does not cover all "
            f"{len(all_pairs)} state pairs; missing: "
            f"{sorted(all_pairs - covered)}"
        )
    return (
        f"{len(allowed)} allowed + {len(forbidden)} forbidden transitions "
        f"partition the full {len(all_pairs)}-pair state square"
    )


def check_allowed_transitions_enforced(block: dict[str, Any]) -> str:
    allowed = _transition_set(block["allowed_transitions"])
    # We verify each allowed transition by driving the gate's
    # ``_assert_transition`` helper directly. The behavioural exercises below
    # cover the reachable subset; this loop covers the unreachable-via-
    # default-path entries too so the contract metadata can't drift.
    sentinel_gate = ReadinessGate()
    for from_state_value, to_state_value in sorted(allowed):
        from_state = GateState(from_state_value)
        to_state = GateState(to_state_value)
        try:
            sentinel_gate._assert_transition(from_state, to_state)  # type: ignore[attr-defined]
        except GateTransitionError as exc:
            _fail(f"allowed transition {from_state_value} -> {to_state_value} raised {exc}")
    return f"all {len(allowed)} contracted allowed transitions pass _assert_transition"


def check_forbidden_transitions_rejected(block: dict[str, Any]) -> str:
    forbidden = _transition_set(block["forbidden_transitions"])
    sentinel_gate = ReadinessGate()
    for from_state_value, to_state_value in sorted(forbidden):
        from_state = GateState(from_state_value)
        to_state = GateState(to_state_value)
        try:
            sentinel_gate._assert_transition(from_state, to_state)  # type: ignore[attr-defined]
        except GateTransitionError:
            continue
        else:
            _fail(
                f"forbidden transition {from_state_value} -> {to_state_value} "
                "did not raise GateTransitionError"
            )
    return f"all {len(forbidden)} contracted forbidden transitions raise GateTransitionError"


def check_gate_methods_present(block: dict[str, Any]) -> str:
    expected_methods = set(block["required_gate_methods"])
    expected_properties = set(block["required_gate_properties"])
    method_names = {
        name
        for name, member in inspect.getmembers(ReadinessGate)
        if (inspect.isfunction(member) or inspect.ismethod(member)) and not name.startswith("_")
    }
    property_names = {
        name for name, member in inspect.getmembers(ReadinessGate) if isinstance(member, property)
    }
    missing_methods = expected_methods - method_names
    missing_properties = expected_properties - property_names
    if missing_methods:
        _fail(f"ReadinessGate is missing contracted methods: {sorted(missing_methods)}")
    if missing_properties:
        _fail(f"ReadinessGate is missing contracted properties: {sorted(missing_properties)}")
    return (
        f"ReadinessGate exposes the {len(expected_methods)} contracted methods "
        f"and {len(expected_properties)} contracted properties"
    )


def check_operator_override_audit_fields(block: dict[str, Any]) -> str:
    expected = set(block["operator_override_required_fields"])
    fields = {f.name for f in OperatorOverride.__dataclass_fields__.values()}
    missing = expected - fields
    extra = fields - expected
    if missing:
        _fail(f"OperatorOverride is missing contracted audit fields: {sorted(missing)}")
    if extra:
        _fail(f"OperatorOverride carries extra fields not in the contract: {sorted(extra)}")
    return f"OperatorOverride carries the {len(expected)} contracted audit fields"


def check_override_audit_rejects_empty_strings(block: dict[str, Any]) -> str:
    string_fields = block["operator_override_string_fields"]
    seeded = ReadinessGate.from_env(_missing_key_env())
    base_kwargs = dict(
        actor="alice@example.com",
        reason="ok",
        audit_trail_id="audit-1",
        timestamp_ns=time.time_ns(),
    )
    for field_name in string_fields:
        kwargs = dict(base_kwargs)
        kwargs[field_name] = ""
        try:
            seeded.operator_override(OperatorOverride(**kwargs))
        except OverrideAuditError:
            continue
        else:
            _fail(f"OperatorOverride.{field_name} accepted empty string without OverrideAuditError")
    # whitespace-only must also be rejected (operator audit log requires a real value)
    for field_name in string_fields:
        kwargs = dict(base_kwargs)
        kwargs[field_name] = "   "
        try:
            seeded.operator_override(OperatorOverride(**kwargs))
        except OverrideAuditError:
            continue
        else:
            _fail(
                f"OperatorOverride.{field_name} accepted whitespace-only string "
                "without OverrideAuditError"
            )
    return (
        f"empty + whitespace-only values rejected on all {len(string_fields)} string audit fields"
    )


def check_override_audit_rejects_bad_timestamps(block: dict[str, Any]) -> str:
    int_fields = block["operator_override_int_fields"]
    seeded = ReadinessGate.from_env(_missing_key_env())
    base_kwargs = dict(
        actor="alice@example.com",
        reason="ok",
        audit_trail_id="audit-1",
        timestamp_ns=time.time_ns(),
    )
    bad_values: list[Any] = [-1, True, False, 3.14, "1000", None]
    for field_name in int_fields:
        for bad_value in bad_values:
            kwargs = dict(base_kwargs)
            kwargs[field_name] = bad_value  # type: ignore[assignment]
            try:
                seeded.operator_override(OperatorOverride(**kwargs))
            except OverrideAuditError:
                continue
            else:
                _fail(
                    f"OperatorOverride.{field_name} accepted invalid value "
                    f"{bad_value!r} without OverrideAuditError"
                )
    return (
        f"timestamp_ns rejects negative / bool / float / str / None ("
        f"{len(bad_values)} negative cases)"
    )


def check_override_rejects_wrong_type(block: dict[str, Any]) -> str:
    del block  # unused
    seeded = ReadinessGate.from_env(_missing_key_env())
    for bad in [
        None,
        {"actor": "a", "reason": "r", "audit_trail_id": "i", "timestamp_ns": 0},
        "operator",
        42,
    ]:
        try:
            seeded.operator_override(bad)  # type: ignore[arg-type]
        except OverrideAuditError:
            continue
        else:
            _fail(
                f"operator_override accepted non-OperatorOverride payload "
                f"{type(bad).__name__!r} without raising OverrideAuditError"
            )
    return "operator_override rejects non-OperatorOverride payloads (None, dict, str, int)"


def check_pre_trade_blocks_assert_ready(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate.from_env(_missing_key_env())
    if gate.state is not GateState.PRE_TRADE_BLOCKED:
        _fail(
            "ReadinessGate.from_env on a missing-key env did not produce "
            f"PRE_TRADE_BLOCKED (got {gate.state.value})"
        )
    try:
        gate.assert_ready_or_hold()
    except PreTradeHoldError as hold:
        if not hold.report.errors:
            _fail("PreTradeHoldError.report carries no errors")
        return (
            f"assert_ready_or_hold raises PreTradeHoldError with "
            f"{len(hold.report.errors)} structured error(s)"
        )
    _fail("assert_ready_or_hold did not raise on a PRE_TRADE_BLOCKED gate")
    raise RuntimeError("unreachable")


def check_ready_passes_assert_ready(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate.from_env(_default_env())
    if gate.state is not GateState.READY:
        _fail(
            "ReadinessGate.from_env on the catalogue defaults did not produce "
            f"READY (got {gate.state.value})"
        )
    gate.assert_ready_or_hold()
    if gate.report.errors:
        _fail("READY-state report unexpectedly carries error-severity failures")
    return (
        f"READY state passes assert_ready_or_hold; {len(gate.report.warnings)} warning(s) preserved"
    )


def check_initializing_blocks_assert_ready(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate()  # uninitialised by design
    if gate.state is not GateState.INITIALIZING:
        _fail(f"new ReadinessGate did not start in INITIALIZING (got {gate.state.value})")
    try:
        gate.assert_ready_or_hold()
    except GateTransitionError:
        return "INITIALIZING gate refuses assert_ready_or_hold (must seed first)"
    _fail("assert_ready_or_hold accepted an unseeded gate")
    raise RuntimeError("unreachable")


def check_reevaluate_requires_seeded_gate(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate()
    try:
        gate.reevaluate(_default_env())
    except GateTransitionError:
        return "reevaluate refuses to run on an INITIALIZING gate"
    _fail("reevaluate accepted an unseeded gate")
    raise RuntimeError("unreachable")


def check_reevaluate_clears_pre_trade(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate.from_env(_missing_key_env())
    if gate.state is not GateState.PRE_TRADE_BLOCKED:
        _fail("expected PRE_TRADE_BLOCKED before reevaluate")
    gate.reevaluate(_default_env())
    if gate.state is not GateState.READY:
        _fail(f"reevaluate with clean env did not transition to READY (got {gate.state.value})")
    return "reevaluate with clean env transitions PRE_TRADE_BLOCKED -> READY"


def check_override_only_releases_pre_trade(block: dict[str, Any]) -> str:
    del block
    # Operator override must NOT be accepted from READY or INITIALIZING.
    base_override = OperatorOverride(
        actor="alice@example.com",
        reason="paper-only warm-up",
        audit_trail_id="audit-7",
        timestamp_ns=time.time_ns(),
    )

    initialising = ReadinessGate()
    try:
        initialising.operator_override(base_override)
    except GateTransitionError:
        pass
    else:
        _fail("operator_override accepted on INITIALIZING gate")

    ready = ReadinessGate.from_env(_default_env())
    try:
        ready.operator_override(base_override)
    except GateTransitionError:
        pass
    else:
        _fail("operator_override accepted on READY gate")

    blocked = ReadinessGate.from_env(_missing_key_env())
    blocked.operator_override(base_override)
    if blocked.state is not GateState.OVERRIDDEN:
        _fail(f"PRE_TRADE_BLOCKED + override did not reach OVERRIDDEN (got {blocked.state.value})")
    if len(blocked.overrides) != 1:
        _fail("override audit list did not record exactly one override")
    return (
        "operator_override only releases the hold from PRE_TRADE_BLOCKED; "
        "rejected from INITIALIZING and READY"
    )


def check_dashboard_payload_shape(block: dict[str, Any]) -> str:
    expected = set(block["required_dashboard_payload_fields"])
    gate = ReadinessGate.from_env(_missing_key_env())
    payload = gate.as_dashboard_payload()
    missing = expected - set(payload.keys())
    if missing:
        _fail(f"dashboard payload is missing contracted fields: {sorted(missing)}")
    if not isinstance(payload["errors"], list):
        _fail("dashboard payload 'errors' must be a list")
    if not isinstance(payload["warnings"], list):
        _fail("dashboard payload 'warnings' must be a list")
    if payload["ok"] is True:
        _fail("dashboard payload 'ok' must be False on a PRE_TRADE_BLOCKED gate")
    if payload["state"] != GateState.PRE_TRADE_BLOCKED.value:
        _fail("dashboard payload 'state' does not match gate state")
    # JSON round-trip: the payload must serialise without bespoke encoders.
    json.dumps(payload)
    return (
        f"as_dashboard_payload exposes the {len(expected)} contracted fields and JSON round-trips"
    )


def check_log_record_shape(block: dict[str, Any]) -> str:
    expected = set(block["required_log_record_fields"])
    gate = ReadinessGate.from_env(_missing_key_env())
    records = gate.as_log_records()
    if not records:
        _fail("as_log_records returned no records for a PRE_TRADE_BLOCKED gate")
    for record in records:
        missing = expected - set(record.keys())
        if missing:
            _fail(f"log record is missing contracted fields: {sorted(missing)}")
        # JSON round-trip
        json.dumps(record)
    return (
        f"as_log_records yields {len(records)} record(s) carrying the "
        f"{len(expected)} contracted fields"
    )


def check_structured_failure_carries_srs_trace(block: dict[str, Any]) -> str:
    del block
    gate = ReadinessGate.from_env(_missing_key_env())
    failures = gate.report.errors
    if not failures:
        _fail("PRE_TRADE_BLOCKED gate has no errors")
    for failure in failures:
        if not failure.srs_trace:
            _fail(f"failure {failure.key} carries no SRS trace")
        if failure.severity is not Severity.ERROR:
            _fail(f"failure {failure.key} is not error-severity")
    return (
        f"{len(failures)} PRE_TRADE_BLOCKED failure(s) carry a non-empty "
        "SRS trace and error severity"
    )


def check_override_audit_trail_preserved_through_reevaluate(block: dict[str, Any]) -> str:
    del block
    blocked = ReadinessGate.from_env(_missing_key_env())
    blocked.operator_override(
        OperatorOverride(
            actor="alice@example.com",
            reason="Reservoir warm-up; IB creds intentionally unset",
            audit_trail_id="audit-100",
            timestamp_ns=time.time_ns(),
        )
    )
    # reevaluate to a clean env: state should drop back to READY but the
    # audit trail entries must remain.
    blocked.reevaluate(_default_env())
    if blocked.state is not GateState.READY:
        _fail(
            "OVERRIDDEN -> reevaluate(clean) did not transition to READY "
            f"(got {blocked.state.value})"
        )
    if len(blocked.overrides) != 1:
        _fail("override audit list was cleared by reevaluate")
    return "operator override audit trail is preserved across an OVERRIDDEN -> READY reevaluation"


def check_module_paths(block: dict[str, Any]) -> str:
    expected = block["module_paths"]
    missing = [p for p in expected if not (ROOT / p).exists()]
    if missing:
        _fail(f"contracted module paths missing on disk: {missing}")
    readme = ROOT / block["readme_path"]
    if not readme.exists():
        _fail(f"contracted readme path missing: {readme}")
    return f"{len(expected)} contracted module paths + 1 README path resolve on disk"


def check_no_runtime_probe_leakage(block: dict[str, Any]) -> str:
    del block
    # ERR-9 SDK-surface deliberately excludes the SRS-MD-006 runtime probes
    # (IB connectivity, ingestion freshness, NAS reachability, etc.). The
    # gate module must not import or implement them — if it does, the SDK
    # surface has accidentally absorbed deferred scope.
    src = (ROOT / "python" / "atp_readiness" / "gate.py").read_text(encoding="utf-8")
    forbidden_tokens = (
        "ib_gateway",
        "ib_insync",
        "IBConnection",
        "ingestion_freshness",
        "nas_reach",
        "ssd_probe",
        "service_health",
        "interactive_brokers",
        "databento",
        "sharadar",
    )
    leaked = [tok for tok in forbidden_tokens if tok in src]
    if leaked:
        _fail(
            f"python/atp_readiness/gate.py references deferred SRS-MD-006 / vendor "
            f"tokens (must stay SDK-surface): {leaked}"
        )
    return f"gate.py contains no deferred-scope tokens ({len(forbidden_tokens)} checked)"


def check_dependency_direction(block: dict[str, Any]) -> str:
    del block
    # The SDK package must not import from atp_strategy / atp_api / atp_cli —
    # those are downstream consumers, not dependencies. atp_config IS allowed
    # (the gate consumes its ReadinessReport).
    for module in (gate_module, override_module, readiness_errors):
        source = inspect.getsource(module)
        for forbidden in ("atp_strategy", "atp_api", "atp_cli"):
            if f"import {forbidden}" in source or f"from {forbidden}" in source:
                _fail(f"{module.__name__} imports forbidden upstream module {forbidden!r}")
    return "no upstream-of-readiness imports leaked into the gate / override / errors modules"


def check_readiness_gate_error_hierarchy(block: dict[str, Any]) -> str:
    del block
    # All three concrete errors must extend ReadinessGateError so downstream
    # callers can catch the family with a single base class.
    for error_cls in (PreTradeHoldError, GateTransitionError, OverrideAuditError):
        if not issubclass(error_cls, ReadinessGateError):
            _fail(
                f"{error_cls.__name__} does not subclass ReadinessGateError; "
                "callers cannot catch the family with one except clause"
            )
    return (
        "PreTradeHoldError / GateTransitionError / OverrideAuditError all "
        "subclass ReadinessGateError"
    )


def check_env_type_guard(block: dict[str, Any]) -> str:
    del block
    # A non-Mapping env (None, str, list) must be rejected with TypeError so
    # the boot script gets a structured error rather than an unhelpful
    # AttributeError from atp_config.load_and_validate.
    for bad in (None, "not a dict", ["ATP_ENV=development"], 42):
        try:
            ReadinessGate.from_env(bad)  # type: ignore[arg-type]
        except TypeError:
            continue
        except Exception as exc:  # noqa: BLE001  (any other type is also a failure)
            _fail(
                "ReadinessGate.from_env on a non-Mapping env raised "
                f"{type(exc).__name__} instead of TypeError"
            )
        else:
            _fail(
                "ReadinessGate.from_env accepted a non-Mapping env "
                f"({type(bad).__name__}) without raising TypeError"
            )
    return "ReadinessGate.from_env rejects non-Mapping envs with TypeError (4 negative cases)"


def assert_startup_readiness_gate_static(
    _config: dict | None = None, root: Path = ROOT
) -> list[str]:
    del root
    block = _load_contract()
    return [
        check_env_type_guard(block),
        check_module_paths(block),
        check_required_exports(block),
        check_readiness_gate_error_hierarchy(block),
        check_gate_state_variants(block),
        check_transition_completeness(block),
        check_allowed_transitions_enforced(block),
        check_forbidden_transitions_rejected(block),
        check_gate_methods_present(block),
        check_operator_override_audit_fields(block),
        check_override_rejects_wrong_type(block),
        check_override_audit_rejects_empty_strings(block),
        check_override_audit_rejects_bad_timestamps(block),
        check_pre_trade_blocks_assert_ready(block),
        check_ready_passes_assert_ready(block),
        check_initializing_blocks_assert_ready(block),
        check_reevaluate_requires_seeded_gate(block),
        check_reevaluate_clears_pre_trade(block),
        check_override_only_releases_pre_trade(block),
        check_dashboard_payload_shape(block),
        check_log_record_shape(block),
        check_structured_failure_carries_srs_trace(block),
        check_override_audit_trail_preserved_through_reevaluate(block),
        check_no_runtime_probe_leakage(block),
        check_dependency_direction(block),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: parent of this script)"
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_startup_readiness_gate_static(root=args.root)
    except StartupReadinessGateCheckError as exc:
        print(f"ERR-9 SDK-SURFACE FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "ERR-9 SDK-SURFACE PASS — pre-trade readiness gate + structured payload "
        "contract (log sink wiring deferred to SRS-LOG-001; dashboard rendering "
        "deferred to SRS-UI-001; live REST/WebSocket endpoint deferred to "
        "SRS-API-001; runtime readiness probes — IB connectivity, ingestion "
        "freshness, NAS reachability — deferred to SRS-MD-006)"
    )
    for line in evidence:
        print(f"  * {line}")
    # Sanity: PLACEHOLDER_VALUE referenced so the import is exercised in this check.
    if not PLACEHOLDER_VALUE:
        return 1
    # Sanity: import importlib so the check fails clean if atp_readiness moves
    # under a packaging surface (e.g., is reorganised into a namespace).
    importlib.import_module("atp_readiness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
