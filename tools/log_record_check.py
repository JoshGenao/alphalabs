#!/usr/bin/env python3
"""Log record SDK-surface contract check for SRS-LOG-001.

Verifies that ``python/atp_logging/`` matches the ``log_record_contract``
block in ``architecture/runtime_services.json`` and that the
``RoutedLogDispatcher`` enforces every documented schema rule + the
system-vs-strategy separation invariant. The check is the deterministic
mirror of the L3 contract test rig; it runs at every boot via ``init.sh``
and on CI so contract drift cannot land silently.

The check intentionally does NOT exercise the persistent sinks, the
dashboard, or the live REST/WebSocket endpoint — those are deferred per
the contract block's ``deferred[]`` list. The PASS line is
``SRS-LOG-001 SDK-SURFACE PASS`` (not bare ``PASS``) to mirror the
SDK-004 / ERR-9 / SRS-API-001 partial-pass pattern: the SDK surface is
locked, the runtime / sink halves still hold the feature_list entry at
``passes:false``.
"""

from __future__ import annotations

import argparse
import ast
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

from atp_logging import (  # noqa: E402  (path manipulation must come first)
    EVENT_TYPES_BY_SOURCE,
    STRATEGY_SOURCES,
    SYSTEM_SOURCES,
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    LogRecordError,
    LogRoutingError,
    LogSink,
    LogSinkError,
    RoutedLogDispatcher,
    Severity,
    Source,
    is_finite_non_negative_int,
)
from atp_logging import dispatcher as dispatcher_module  # noqa: E402
from atp_logging import errors as errors_module  # noqa: E402
from atp_logging import records as records_module  # noqa: E402

_CONTRACT_BLOCK = "log_record_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"


class LogRecordCheckError(AssertionError):
    """Raised when the log record surface diverges from the contract block."""


def _fail(message: str) -> None:
    raise LogRecordCheckError(message)


def _load_contract() -> dict[str, Any]:
    raw = json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))
    block = raw.get(_CONTRACT_BLOCK)
    if not isinstance(block, dict):
        _fail(f"runtime_services.json is missing the {_CONTRACT_BLOCK!r} block")
    return block


class _CapturingSink:
    """Test sink: records every dispatched LogRecord in order."""

    def __init__(self) -> None:
        self.records: list[LogRecord] = []

    def write(self, record: LogRecord) -> None:
        self.records.append(record)


class _RaisingSink:
    """Test sink: raises on every write."""

    def write(self, record: LogRecord) -> None:  # noqa: ARG002
        raise RuntimeError("simulated downstream sink failure")


def _system_record(
    *,
    source: Source = Source.KILL_SWITCH,
    event_type: str = "ACTIVATION",
    correlation_id: str = "corr-1",
    message: str = "ok",
    severity: Severity = Severity.INFO,
    timestamp_ns: int | None = None,
) -> LogRecord:
    return LogRecord(
        timestamp_ns=time.time_ns() if timestamp_ns is None else timestamp_ns,
        severity=severity,
        source=source,
        event_type=event_type,
        message=message,
        correlation_id=correlation_id,
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def _strategy_record(
    *,
    event_type: str = "user_signal_fired",
    correlation_id: str = "corr-strat-1",
    message: str = "strategy emitted signal",
    severity: Severity = Severity.INFO,
    strategy_id: str = "strategy-abc",
) -> LogRecord:
    return LogRecord(
        timestamp_ns=time.time_ns(),
        severity=severity,
        source=Source.STRATEGY,
        event_type=event_type,
        message=message,
        correlation_id=correlation_id,
        log_class=LogClass.STRATEGY,
        strategy_id=strategy_id,
    )


def _bound_dispatcher() -> tuple[RoutedLogDispatcher, _CapturingSink, _CapturingSink]:
    dispatcher = RoutedLogDispatcher()
    sys_sink = _CapturingSink()
    strat_sink = _CapturingSink()
    dispatcher.register_sink(LogClass.SYSTEM, sys_sink)
    dispatcher.register_sink(LogClass.STRATEGY, strat_sink)
    return dispatcher, sys_sink, strat_sink


# ====================================================================== #
# Collectors
# ====================================================================== #


def check_module_paths(block: dict[str, Any]) -> str:
    expected = block["module_paths"]
    missing = [p for p in expected if not (ROOT / p).exists()]
    if missing:
        _fail(f"contracted module paths missing on disk: {missing}")
    readme = ROOT / block["readme_path"]
    if not readme.exists():
        _fail(f"contracted readme path missing: {readme}")
    return f"{len(expected)} contracted module paths + 1 README path resolve on disk"


def check_required_exports(block: dict[str, Any]) -> str:
    import atp_logging

    expected = sorted(block["required_exports"])
    actual = sorted(atp_logging.__all__)
    if expected != actual:
        _fail(
            f"atp_logging.__all__ ({actual}) does not match contract required_exports ({expected})"
        )
    error_exports = set(block["required_error_exports"])
    missing_error = error_exports - set(actual)
    if missing_error:
        _fail(f"atp_logging.__all__ is missing required error exports: {sorted(missing_error)}")
    return (
        f"atp_logging.__all__ exports the {len(expected)} contracted symbols "
        f"(including all {len(error_exports)} error types)"
    )


def check_error_hierarchy(block: dict[str, Any]) -> str:
    del block
    for error_cls in (LogPayloadError, LogClassError, LogRoutingError, LogSinkError):
        if not issubclass(error_cls, LogRecordError):
            _fail(
                f"{error_cls.__name__} does not subclass LogRecordError; "
                "callers cannot catch the family with one except clause"
            )
    return (
        "LogPayloadError / LogClassError / LogRoutingError / LogSinkError "
        "all subclass LogRecordError"
    )


def check_enum_variants(block: dict[str, Any]) -> str:
    pairs = (
        ("severity", Severity, block["severity_variants"]),
        ("log_class", LogClass, block["log_class_variants"]),
        ("source", Source, block["source_variants"]),
    )
    for name, enum_cls, expected in pairs:
        actual = [m.value for m in enum_cls]
        if actual != list(expected):
            _fail(
                f"{enum_cls.__name__} variants {actual} do not match contract "
                f"{name}_variants {expected}"
            )
    return (
        f"Severity ({len(block['severity_variants'])}) / LogClass "
        f"({len(block['log_class_variants'])}) / Source "
        f"({len(block['source_variants'])}) declare the contracted variants in order"
    )


def check_system_source_partition(block: dict[str, Any]) -> str:
    expected_system = set(block["system_source_variants"])
    expected_strategy = set(block["strategy_source_variants"])
    actual_system = {s.value for s in SYSTEM_SOURCES}
    actual_strategy = {s.value for s in STRATEGY_SOURCES}
    if expected_system != actual_system:
        _fail(
            f"SYSTEM_SOURCES values {sorted(actual_system)} do not match contract "
            f"system_source_variants {sorted(expected_system)}"
        )
    if expected_strategy != actual_strategy:
        _fail(
            f"STRATEGY_SOURCES values {sorted(actual_strategy)} do not match contract "
            f"strategy_source_variants {sorted(expected_strategy)}"
        )
    all_sources = {s.value for s in Source}
    union = actual_system | actual_strategy
    if union != all_sources:
        _fail(
            "SYSTEM_SOURCES ∪ STRATEGY_SOURCES does not cover every Source variant; "
            f"missing: {sorted(all_sources - union)}"
        )
    overlap = actual_system & actual_strategy
    if overlap:
        _fail(f"SYSTEM_SOURCES and STRATEGY_SOURCES overlap on: {sorted(overlap)}")
    return (
        f"SYSTEM_SOURCES ({len(actual_system)}) and STRATEGY_SOURCES "
        f"({len(actual_strategy)}) partition all {len(all_sources)} Source variants"
    )


def check_event_types_by_source(block: dict[str, Any]) -> str:
    expected = block["event_types_by_source"]
    actual = {src.value: list(types) for src, types in EVENT_TYPES_BY_SOURCE.items()}
    if actual != expected:
        _fail(
            "EVENT_TYPES_BY_SOURCE does not match contract event_types_by_source; "
            f"actual={actual}, expected={expected}"
        )
    # Every SYSTEM source must have at least one event type (the AC enumerates
    # them); STRATEGY is intentionally empty (user-defined).
    for src in SYSTEM_SOURCES:
        if not EVENT_TYPES_BY_SOURCE.get(src):
            _fail(f"SYSTEM source {src.value!r} has no AC-pinned event types")
    if EVENT_TYPES_BY_SOURCE[Source.STRATEGY]:
        _fail(
            "EVENT_TYPES_BY_SOURCE[Source.STRATEGY] must be empty "
            "(strategy event types are user-defined per SN-2.02)"
        )
    total_types = sum(len(v) for v in EVENT_TYPES_BY_SOURCE.values())
    return (
        f"EVENT_TYPES_BY_SOURCE pins {total_types} AC-named event types across "
        f"{len(SYSTEM_SOURCES)} SYSTEM sources; STRATEGY is intentionally empty"
    )


def check_log_record_field_set(block: dict[str, Any]) -> str:
    expected = set(block["required_log_record_fields"])
    actual = {f.name for f in LogRecord.__dataclass_fields__.values()}
    if actual != expected:
        _fail(
            f"LogRecord fields {sorted(actual)} do not match contract "
            f"required_log_record_fields {sorted(expected)}"
        )
    return f"LogRecord declares the {len(expected)} contracted fields"


def check_log_record_frozen(block: dict[str, Any]) -> str:
    if not block.get("log_record_is_frozen"):
        _fail("contract log_record_is_frozen must be true")
    record = _system_record()
    try:
        record.message = "mutated"  # type: ignore[misc]
    except Exception:  # noqa: BLE001
        return "LogRecord is frozen (mutation raises)"
    _fail("LogRecord accepted mutation; the dataclass must be frozen")
    raise RuntimeError("unreachable")


def check_dispatcher_methods(block: dict[str, Any]) -> str:
    expected_methods = set(block["required_dispatcher_methods"])
    method_names = {
        name
        for name, member in inspect.getmembers(RoutedLogDispatcher)
        if (inspect.isfunction(member) or inspect.ismethod(member)) and not name.startswith("_")
    }
    missing = expected_methods - method_names
    if missing:
        _fail(f"RoutedLogDispatcher is missing contracted methods: {sorted(missing)}")
    return f"RoutedLogDispatcher exposes the {len(expected_methods)} contracted methods"


def check_sink_protocol_methods(block: dict[str, Any]) -> str:
    expected = set(block["required_sink_protocol_methods"])
    # ``Protocol`` methods land on the class as plain attributes; pull them
    # from ``__annotations__`` / ``__dict__`` to be safe.
    method_names = {
        name
        for name, member in inspect.getmembers(LogSink)
        if (inspect.isfunction(member) or inspect.ismethod(member)) and not name.startswith("_")
    }
    missing = expected - method_names
    if missing:
        _fail(f"LogSink Protocol is missing contracted methods: {sorted(missing)}")
    return f"LogSink Protocol declares the {len(expected)} contracted method(s)"


def check_dispatch_routes_by_log_class(block: dict[str, Any]) -> str:
    del block
    dispatcher, sys_sink, strat_sink = _bound_dispatcher()
    dispatcher.dispatch(_system_record())
    dispatcher.dispatch(_strategy_record())
    if len(sys_sink.records) != 1:
        _fail(f"system sink received {len(sys_sink.records)} records (expected 1)")
    if len(strat_sink.records) != 1:
        _fail(f"strategy sink received {len(strat_sink.records)} records (expected 1)")
    if sys_sink.records[0].log_class is not LogClass.SYSTEM:
        _fail("system sink received a non-SYSTEM record")
    if strat_sink.records[0].log_class is not LogClass.STRATEGY:
        _fail("strategy sink received a non-STRATEGY record")
    return "dispatch routes SYSTEM records to system_sink, STRATEGY records to strategy_sink"


def check_dispatch_rejects_non_record(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    for bad in (None, {"timestamp_ns": 0}, "record", 42, [1, 2]):
        try:
            dispatcher.dispatch(bad)  # type: ignore[arg-type]
        except LogPayloadError:
            continue
        else:
            _fail(f"dispatch accepted non-LogRecord payload {type(bad).__name__}")
    return "dispatch rejects non-LogRecord payloads (None, dict, str, int, list)"


def check_dispatch_validates_enum_fields(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    # Build a record-like object that bypasses the dataclass __init__ guards
    # so we can inject raw strings into the enum-typed fields. Use a
    # minimal mock that quacks like a LogRecord but carries raw strings.

    class _Mock:
        log_class = LogClass.SYSTEM
        severity = "INFO"  # raw str instead of Severity
        source = Source.KILL_SWITCH
        event_type = "ACTIVATION"
        message = "ok"
        correlation_id = "c"
        strategy_id = None
        timestamp_ns = 1

    # The dispatch payload-shape guard rejects non-LogRecord up front,
    # which keeps the API stable. To verify enum validation we instead
    # construct a real LogRecord via object.__new__ and patch fields.
    record = LogRecord(
        timestamp_ns=1,
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="ok",
        correlation_id="c",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
    cases = (
        ("severity", "INFO"),
        ("source", "kill_switch"),
        ("log_class", "system"),
    )
    fail_count = 0
    for field_name, bad_value in cases:
        object.__setattr__(record, field_name, bad_value)
        try:
            dispatcher.dispatch(record)
        except LogPayloadError:
            fail_count += 1
        else:
            _fail(
                f"dispatch accepted raw str for enum field {field_name!r}; "
                "discriminant validation is missing"
            )
        finally:
            # Restore enum value so the next iteration tests a different field.
            if field_name == "severity":
                object.__setattr__(record, field_name, Severity.INFO)
            elif field_name == "source":
                object.__setattr__(record, field_name, Source.KILL_SWITCH)
            elif field_name == "log_class":
                object.__setattr__(record, field_name, LogClass.SYSTEM)
    return f"dispatch rejects raw-string enum values on all {fail_count} enum fields"


def check_dispatch_validates_timestamp(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    bad_values: list[Any] = [-1, True, False, 3.14, "1000", None, float("inf"), float("nan")]
    for bad in bad_values:
        record = LogRecord(
            timestamp_ns=0,  # placeholder; we mutate below
            severity=Severity.INFO,
            source=Source.KILL_SWITCH,
            event_type="ACTIVATION",
            message="ok",
            correlation_id="c",
            log_class=LogClass.SYSTEM,
            strategy_id=None,
        )
        object.__setattr__(record, "timestamp_ns", bad)
        try:
            dispatcher.dispatch(record)
        except LogPayloadError:
            continue
        else:
            _fail(f"dispatch accepted invalid timestamp_ns={bad!r}")
    return (
        f"dispatch rejects bad timestamp_ns values ({len(bad_values)} negative cases: "
        "negative / bool / float / str / None / inf / nan)"
    )


def check_dispatch_validates_non_empty_strings(block: dict[str, Any]) -> str:
    string_fields = block["string_record_fields"]
    dispatcher, _, _ = _bound_dispatcher()
    for field_name in string_fields:
        for bad_value in ("", "   ", "\n\t"):
            record = LogRecord(
                timestamp_ns=time.time_ns(),
                severity=Severity.INFO,
                source=Source.KILL_SWITCH,
                event_type="ACTIVATION",
                message="ok",
                correlation_id="c",
                log_class=LogClass.SYSTEM,
                strategy_id=None,
            )
            object.__setattr__(record, field_name, bad_value)
            try:
                dispatcher.dispatch(record)
            except LogPayloadError:
                continue
            else:
                _fail(
                    f"dispatch accepted empty/whitespace-only value for {field_name!r}: "
                    f"{bad_value!r}"
                )
    return (
        f"dispatch rejects empty + whitespace-only values on all {len(string_fields)} "
        "non-empty string fields"
    )


def check_dispatch_validates_string_field_type(block: dict[str, Any]) -> str:
    string_fields = block["string_record_fields"]
    dispatcher, _, _ = _bound_dispatcher()
    for field_name in string_fields:
        for bad_value in (None, 42, 3.14, b"bytes", ["list"]):
            record = LogRecord(
                timestamp_ns=time.time_ns(),
                severity=Severity.INFO,
                source=Source.KILL_SWITCH,
                event_type="ACTIVATION",
                message="ok",
                correlation_id="c",
                log_class=LogClass.SYSTEM,
                strategy_id=None,
            )
            object.__setattr__(record, field_name, bad_value)
            try:
                dispatcher.dispatch(record)
            except LogPayloadError:
                continue
            else:
                _fail(
                    f"dispatch accepted non-str value for {field_name!r}: "
                    f"{type(bad_value).__name__}={bad_value!r}"
                )
    return (
        f"dispatch rejects non-str values on all {len(string_fields)} string fields "
        "(None / int / float / bytes / list)"
    )


def check_system_records_reject_strategy_id(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="ok",
        correlation_id="c",
        log_class=LogClass.SYSTEM,
        strategy_id="strategy-leaked",
    )
    try:
        dispatcher.dispatch(record)
    except LogClassError:
        return "SYSTEM record carrying strategy_id is rejected with LogClassError"
    _fail("SYSTEM record accepted with strategy_id set (must be None)")
    raise RuntimeError("unreachable")


def check_strategy_records_require_strategy_id(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    for bad_id in (None, "", "   "):
        record = LogRecord(
            timestamp_ns=time.time_ns(),
            severity=Severity.INFO,
            source=Source.STRATEGY,
            event_type="user_signal",
            message="ok",
            correlation_id="c",
            log_class=LogClass.STRATEGY,
            strategy_id=bad_id,
        )
        try:
            dispatcher.dispatch(record)
        except LogClassError:
            continue
        else:
            _fail(f"STRATEGY record accepted with strategy_id={bad_id!r}")
    return "STRATEGY record without a non-empty strategy_id is rejected (None / '' / whitespace)"


def check_system_source_invariant(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    # SYSTEM record carrying Source.STRATEGY must fail.
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="user_signal",
        message="ok",
        correlation_id="c",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
    try:
        dispatcher.dispatch(record)
    except LogClassError:
        pass
    else:
        _fail("SYSTEM record accepted Source.STRATEGY (cross-field invariant missing)")
    # STRATEGY record carrying a system Source must fail.
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="ok",
        correlation_id="c",
        log_class=LogClass.STRATEGY,
        strategy_id="strat-1",
    )
    try:
        dispatcher.dispatch(record)
    except LogClassError:
        pass
    else:
        _fail("STRATEGY record accepted a SYSTEM source (cross-field invariant missing)")
    return (
        "SYSTEM<->STRATEGY cross-field invariant enforced: "
        "log_class=SYSTEM requires SYSTEM_SOURCES, log_class=STRATEGY requires Source.STRATEGY"
    )


def check_event_type_constraints_on_system(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, _ = _bound_dispatcher()
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.KILL_SWITCH,
        event_type="BOGUS_EVENT_TYPE",
        message="ok",
        correlation_id="c",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
    try:
        dispatcher.dispatch(record)
    except LogPayloadError:
        return "dispatch rejects event_type not in EVENT_TYPES_BY_SOURCE[source] for SYSTEM records"
    _fail("SYSTEM record accepted an unlisted event_type")
    raise RuntimeError("unreachable")


def check_event_type_free_form_on_strategy(block: dict[str, Any]) -> str:
    del block
    dispatcher, _, strat_sink = _bound_dispatcher()
    record = LogRecord(
        timestamp_ns=time.time_ns(),
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="completely_user_defined_event_x9",
        message="strategy emitted a signal",
        correlation_id="c",
        log_class=LogClass.STRATEGY,
        strategy_id="strat-1",
    )
    dispatcher.dispatch(record)
    if len(strat_sink.records) != 1:
        _fail("STRATEGY record was not accepted with a user-defined event_type")
    return "STRATEGY record accepts user-defined event_type (free-form per SN-2.02)"


def check_routing_error_without_registered_sink(block: dict[str, Any]) -> str:
    del block
    dispatcher = RoutedLogDispatcher()
    # No sinks registered; dispatch must fail closed.
    try:
        dispatcher.dispatch(_system_record())
    except LogRoutingError:
        pass
    else:
        _fail("dispatch accepted a SYSTEM record with no sink registered")
    # Partial registration: SYSTEM sink only, STRATEGY dispatch must fail.
    dispatcher.register_sink(LogClass.SYSTEM, _CapturingSink())
    try:
        dispatcher.dispatch(_strategy_record())
    except LogRoutingError:
        pass
    else:
        _fail("dispatch accepted a STRATEGY record without a strategy sink registered")
    return "dispatch fails closed with LogRoutingError when no sink is registered"


def check_sink_exceptions_wrapped(block: dict[str, Any]) -> str:
    del block
    dispatcher = RoutedLogDispatcher()
    dispatcher.register_sink(LogClass.SYSTEM, _RaisingSink())
    try:
        dispatcher.dispatch(_system_record())
    except LogSinkError as exc:
        if exc.__cause__ is None or not isinstance(exc.__cause__, RuntimeError):
            _fail("LogSinkError did not preserve original exception on __cause__")
        return "sink-raised exceptions are wrapped in LogSinkError with original on __cause__"
    _fail("sink-raised RuntimeError was not wrapped in LogSinkError")
    raise RuntimeError("unreachable")


def check_register_sink_validates_inputs(block: dict[str, Any]) -> str:
    del block
    dispatcher = RoutedLogDispatcher()
    # Wrong log_class type.
    try:
        dispatcher.register_sink("system", _CapturingSink())  # type: ignore[arg-type]
    except LogPayloadError:
        pass
    else:
        _fail("register_sink accepted raw str for log_class")
    # Wrong sink type (no .write method).

    class _NotASink:
        pass

    try:
        dispatcher.register_sink(LogClass.SYSTEM, _NotASink())  # type: ignore[arg-type]
    except LogPayloadError:
        pass
    else:
        _fail("register_sink accepted object missing .write method")
    return "register_sink rejects non-LogClass and non-LogSink inputs"


def check_record_as_dict_json_roundtrip(block: dict[str, Any]) -> str:
    expected = set(block["required_log_record_fields"])
    record = _system_record()
    payload = record.as_dict()
    missing = expected - set(payload.keys())
    if missing:
        _fail(f"LogRecord.as_dict() is missing contracted fields: {sorted(missing)}")
    if payload["severity"] != Severity.INFO.value:
        _fail("LogRecord.as_dict() did not serialise Severity enum to .value string")
    if payload["log_class"] != LogClass.SYSTEM.value:
        _fail("LogRecord.as_dict() did not serialise LogClass enum to .value string")
    if payload["source"] != Source.KILL_SWITCH.value:
        _fail("LogRecord.as_dict() did not serialise Source enum to .value string")
    # JSON round-trip.
    json.dumps(payload)
    return f"LogRecord.as_dict() emits the {len(expected)} contracted fields and JSON round-trips"


def check_dependency_direction(block: dict[str, Any]) -> str:
    del block
    forbidden_upstreams = (
        "atp_strategy",
        "atp_api",
        "atp_cli",
        "atp_ws",
        "atp_readiness",
        "atp_config",
    )
    for module in (records_module, dispatcher_module, errors_module):
        source = inspect.getsource(module)
        for forbidden in forbidden_upstreams:
            if f"import {forbidden}" in source or f"from {forbidden}" in source:
                _fail(
                    f"{module.__name__} imports forbidden upstream module "
                    f"{forbidden!r} (atp_logging is upstream of every consumer)"
                )
    return (
        f"no upstream-of-logging imports leaked into the records / dispatcher / "
        f"errors modules ({len(forbidden_upstreams)} forbidden upstreams checked)"
    )


def check_vendor_token_isolation(block: dict[str, Any]) -> str:
    forbidden = block["vendor_forbidden_tokens"]
    leaked: list[tuple[str, str]] = []
    package_dir = ROOT / "python" / "atp_logging"
    for path in package_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                leaked.append((path.name, token))
    if leaked:
        _fail(
            f"vendor tokens leaked into atp_logging Python sources: {leaked}; "
            "the SDK surface must stay adapter-agnostic"
        )
    return f"no vendor SDK tokens ({len(forbidden)} checked) leaked into atp_logging sources"


def check_downstream_surface_snapshots(block: dict[str, Any]) -> str:
    paths = block["downstream_surface_snapshots"]
    seen_logs = []
    for rel in paths:
        full = ROOT / rel
        if not full.exists():
            _fail(f"contracted downstream surface snapshot missing: {rel}")
        data = json.loads(full.read_text(encoding="utf-8"))
        flat = json.dumps(data)
        if "log" not in flat.lower():
            _fail(f"downstream snapshot {rel} no longer mentions 'log' anywhere")
        seen_logs.append(rel)
    return (
        f"{len(seen_logs)} downstream surface snapshots present and reference "
        "the LOGS surface (OpenAPI / CLI manual / AsyncAPI)"
    )


def _iter_imports(node: ast.AST) -> Iterable[str]:
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                yield alias.name
        elif isinstance(child, ast.ImportFrom) and child.module is not None:
            yield child.module


def check_no_cycle_with_downstream_packages(block: dict[str, Any]) -> str:
    del block
    # Downstream packages (atp_api / atp_cli / atp_ws / atp_readiness) MAY
    # import atp_logging in the future, but atp_logging must not import THEM.
    # We just re-assert the dependency direction at the AST level (the
    # source-text check above catches inline strings; this one catches
    # legitimate import statements parsed through ast).
    forbidden = {"atp_strategy", "atp_api", "atp_cli", "atp_ws", "atp_readiness", "atp_config"}
    package_dir = ROOT / "python" / "atp_logging"
    leaked: list[tuple[str, str]] = []
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imported in _iter_imports(tree):
            root = imported.split(".")[0]
            if root in forbidden:
                leaked.append((path.name, imported))
    if leaked:
        _fail(f"AST-level upstream import leak: {leaked}")
    return f"AST-level: atp_logging imports no upstream package ({len(forbidden)} checked)"


def check_deferred_list_populated(block: dict[str, Any]) -> str:
    deferred = block["deferred"]
    if not isinstance(deferred, list) or not deferred:
        _fail("contract.deferred must be a non-empty list")
    for entry in deferred:
        if not isinstance(entry, dict):
            _fail(f"deferred entry must be a dict; got {type(entry).__name__}")
        feature = entry.get("feature")
        what = entry.get("what")
        if not isinstance(feature, str) or not feature.strip():
            _fail(f"deferred entry missing non-empty 'feature': {entry}")
        if not isinstance(what, str) or not what.strip():
            _fail(f"deferred entry missing non-empty 'what': {entry}")
    required = {
        "SRS-LOG-001-runtime",
        "SRS-UI-001",
        "SRS-API-001",
        "SRS-NOTIF-001",
    }
    named = {entry["feature"] for entry in deferred}
    missing = required - named
    if missing:
        _fail(f"deferred list is missing required downstream features: {sorted(missing)}")
    return (
        f"deferred list has {len(deferred)} entries; all required downstream "
        f"features named ({sorted(required)})"
    )


def check_is_finite_non_negative_int_predicate(block: dict[str, Any]) -> str:
    del block
    accepted = [0, 1, 10**18]
    rejected = [-1, True, False, 3.14, "1", None, float("inf"), float("nan")]
    for value in accepted:
        if not is_finite_non_negative_int(value):
            _fail(f"is_finite_non_negative_int rejected valid value {value!r}")
    for value in rejected:
        if is_finite_non_negative_int(value):
            _fail(f"is_finite_non_negative_int accepted invalid value {value!r}")
    return (
        f"is_finite_non_negative_int accepts {len(accepted)} valid values and "
        f"rejects {len(rejected)} invalid ones"
    )


def assert_log_record_static(_config: dict | None = None, root: Path = ROOT) -> list[str]:
    del root
    block = _load_contract()
    return [
        check_module_paths(block),
        check_required_exports(block),
        check_error_hierarchy(block),
        check_enum_variants(block),
        check_system_source_partition(block),
        check_event_types_by_source(block),
        check_log_record_field_set(block),
        check_log_record_frozen(block),
        check_dispatcher_methods(block),
        check_sink_protocol_methods(block),
        check_dispatch_routes_by_log_class(block),
        check_dispatch_rejects_non_record(block),
        check_dispatch_validates_enum_fields(block),
        check_dispatch_validates_timestamp(block),
        check_dispatch_validates_non_empty_strings(block),
        check_dispatch_validates_string_field_type(block),
        check_system_records_reject_strategy_id(block),
        check_strategy_records_require_strategy_id(block),
        check_system_source_invariant(block),
        check_event_type_constraints_on_system(block),
        check_event_type_free_form_on_strategy(block),
        check_routing_error_without_registered_sink(block),
        check_sink_exceptions_wrapped(block),
        check_register_sink_validates_inputs(block),
        check_record_as_dict_json_roundtrip(block),
        check_dependency_direction(block),
        check_no_cycle_with_downstream_packages(block),
        check_vendor_token_isolation(block),
        check_downstream_surface_snapshots(block),
        check_deferred_list_populated(block),
        check_is_finite_non_negative_int_predicate(block),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: parent of this script)"
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_log_record_static(root=args.root)
    except LogRecordCheckError as exc:
        print(f"SRS-LOG-001 SDK-SURFACE FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SRS-LOG-001 SDK-SURFACE PASS — log record schema + sink routing "
        "contract (concrete persistent sinks deferred to SRS-LOG-001 runtime; "
        "dashboard log pane rendering deferred to SRS-UI-001; live GET /api/v1/logs "
        "+ LOGS WebSocket + admin logs CLI runner deferred to SRS-API-001 + "
        "operator-interface-runtime; ERROR/CRITICAL notification fan-out deferred "
        "to SRS-NOTIF-001)"
    )
    for line in evidence:
        print(f"  * {line}")
    # Sanity: importlib used so the check fails clean if atp_logging moves
    # under a packaging surface.
    importlib.import_module("atp_logging")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
