#!/usr/bin/env python3
"""Persistent log sink contract check for SRS-LOG-001's runtime half.

Verifies that ``python/atp_logging/persistence.py`` matches the
``log_persistence_contract`` block in
``architecture/runtime_services.json`` and that :class:`JsonlLogStore` plus
its read/query surface enforce every documented runtime invariant: the
system-vs-strategy separation guard, crash-durable append, torn-tail
tolerance vs corruption fail-closed, the ``GET /api/v1/logs`` query filters,
and the dependency-direction / vendor-isolation rules.

This is the deterministic mirror of the L1 (``tests/unit/test_log_persistence.py``)
and L7 (``tests/domain/test_log_persistence.py``) rigs; it runs at every
boot via ``init.sh`` and on CI so contract drift cannot land silently. The
PASS line is ``SRS-LOG-001 PERSISTENCE PASS`` — the persistent sinks are
built, but SRS-LOG-001 stays ``passes:false`` until the downstream dashboard
(SRS-UI-001) and live REST/WebSocket/CLI handlers (SRS-API-001) land.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from atp_logging import (  # noqa: E402
    LogClass,
    LogClassError,
    LogRecord,
    LogRecordError,
    LogSink,
    Severity,
    Source,
)
from atp_logging import persistence as persistence_module  # noqa: E402
from atp_logging.persistence import (  # noqa: E402
    JsonlLogStore,
    LogStoreCorruptionError,
    LogStoreError,
    build_separated_log_dispatcher,
    query,
    read_records,
)

_CONTRACT_BLOCK = "log_persistence_contract"
_RUNTIME_SERVICES = ROOT / "architecture" / "runtime_services.json"


class LogPersistenceCheckError(AssertionError):
    """Raised when the persistence surface diverges from the contract block."""


def _fail(message: str) -> None:
    raise LogPersistenceCheckError(message)


def _load_contract() -> dict[str, Any]:
    raw = json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))
    block = raw.get(_CONTRACT_BLOCK)
    if not isinstance(block, dict):
        _fail(f"runtime_services.json is missing the {_CONTRACT_BLOCK!r} block")
    return block


def _system_record(*, ts: int = 1_000, severity: Severity = Severity.INFO) -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=severity,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="kill switch",
        correlation_id="corr-1",
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def _strategy_record(*, ts: int = 2_000) -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="signal",
        message="strategy",
        correlation_id="corr-2",
        log_class=LogClass.STRATEGY,
        strategy_id="alpha",
    )


# ====================================================================== #
# Collectors
# ====================================================================== #


def check_module_path(block: dict[str, Any]) -> str:
    rel = block["module_path"]
    if not (ROOT / rel).exists():
        _fail(f"contracted module path missing on disk: {rel}")
    return f"contracted module path {rel} resolves on disk"


def check_required_exports(block: dict[str, Any]) -> str:
    expected = sorted(block["required_exports"])
    actual = sorted(persistence_module.__all__)
    if expected != actual:
        _fail(
            f"atp_logging.persistence.__all__ ({actual}) does not match contract "
            f"required_exports ({expected})"
        )
    error_exports = set(block["required_error_exports"])
    missing = error_exports - set(actual)
    if missing:
        _fail(f"persistence __all__ is missing required error exports: {sorted(missing)}")
    return f"atp_logging.persistence.__all__ exports the {len(expected)} contracted symbols"


def check_error_hierarchy(block: dict[str, Any]) -> str:
    del block
    for error_cls in (LogStoreError, LogStoreCorruptionError):
        if not issubclass(error_cls, LogRecordError):
            _fail(f"{error_cls.__name__} does not subclass LogRecordError")
    if not issubclass(LogStoreCorruptionError, LogStoreError):
        _fail("LogStoreCorruptionError must subclass LogStoreError")
    return "LogStoreError / LogStoreCorruptionError subclass LogRecordError (corruption ⊂ store)"


def check_store_implements_sink(block: dict[str, Any]) -> str:
    if not block.get("store_implements_sink"):
        _fail("contract store_implements_sink must be true")
    with tempfile.TemporaryDirectory() as tmp:
        store = JsonlLogStore(Path(tmp) / "system.jsonl", log_class=LogClass.SYSTEM)
        try:
            if not isinstance(store, LogSink):
                _fail("JsonlLogStore does not satisfy the LogSink protocol")
        finally:
            store.close()
    return "JsonlLogStore satisfies the LogSink protocol (registrable on a dispatcher)"


def check_separation_enforced_at_sink(block: dict[str, Any]) -> str:
    if not block.get("separation_enforced_at_sink"):
        _fail("contract separation_enforced_at_sink must be true")
    with tempfile.TemporaryDirectory() as tmp:
        with JsonlLogStore(Path(tmp) / "system.jsonl", log_class=LogClass.SYSTEM) as system_store:
            try:
                system_store.write(_strategy_record())
            except LogClassError:
                pass
            else:
                _fail("system store accepted a STRATEGY record (separation guard missing)")
        with JsonlLogStore(
            Path(tmp) / "strategy.jsonl", log_class=LogClass.STRATEGY
        ) as strategy_store:
            try:
                strategy_store.write(_system_record())
            except LogClassError:
                pass
            else:
                _fail("strategy store accepted a SYSTEM record (separation guard missing)")
        # The refused records left no trace.
        if read_records(Path(tmp) / "system.jsonl"):
            _fail("a refused STRATEGY record still landed in the system file")
    return "JsonlLogStore refuses a wrong-class record (system⊥strategy enforced at the sink)"


def check_separate_files(block: dict[str, Any]) -> str:
    sys_name = block["default_system_filename"]
    strat_name = block["default_strategy_filename"]
    if sys_name == strat_name:
        _fail("contract default system/strategy filenames must differ")
    with tempfile.TemporaryDirectory() as tmp:
        dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp)
        with system_store, strategy_store:
            dispatcher.dispatch(_system_record())
            dispatcher.dispatch(_strategy_record())
        sys_recs = read_records(Path(tmp) / sys_name)
        strat_recs = read_records(Path(tmp) / strat_name)
    if [r.log_class for r in sys_recs] != [LogClass.SYSTEM]:
        _fail("system file did not hold exactly the SYSTEM record")
    if [r.log_class for r in strat_recs] != [LogClass.STRATEGY]:
        _fail("strategy file did not hold exactly the STRATEGY record")
    return (
        f"build_separated_log_dispatcher persists SYSTEM→{sys_name}, STRATEGY→{strat_name} "
        "(physically separate files)"
    )


def check_build_rejects_same_filename(block: dict[str, Any]) -> str:
    del block
    # Identical strings, an aliasing './' prefix, a path separator, and a
    # parent-traversal name must ALL be refused so the two sinks stay
    # physically separate.
    cases = [
        ("x.jsonl", "x.jsonl"),  # identical
        ("system.jsonl", "./system.jsonl"),  # alias of the same file
        ("system.jsonl", "sub/strategy.jsonl"),  # separator escapes basename
        ("system.jsonl", "../strategy.jsonl"),  # parent traversal
    ]
    for sys_name, strat_name in cases:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                build_separated_log_dispatcher(
                    tmp, system_filename=sys_name, strategy_filename=strat_name
                )
            except LogStoreError:
                continue
            _fail(
                f"build_separated_log_dispatcher accepted aliasing/unsafe filenames "
                f"({sys_name!r}, {strat_name!r})"
            )
    return (
        "build_separated_log_dispatcher rejects identical, aliasing ('./'), and "
        "traversal/separator filenames (physical separation cannot be bypassed)"
    )


def check_durable_roundtrip(block: dict[str, Any]) -> str:
    del block
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.jsonl"
        record = _system_record(severity=Severity.CRITICAL)
        store = JsonlLogStore(path, log_class=LogClass.SYSTEM)
        store.write(record)
        del store  # simulate a crash: no orderly close
        recovered = read_records(path)
    if recovered != [record]:
        _fail("a durably-written record did not round-trip after an unclean restart")
    return "a fsync'd record round-trips byte-for-byte after an unclean restart"


def check_fsync_default(block: dict[str, Any]) -> str:
    if not block.get("fsync_default"):
        _fail("contract fsync_default must be true")
    sig = inspect.signature(JsonlLogStore.__init__)
    default = sig.parameters["fsync"].default
    if default is not True:
        _fail(f"JsonlLogStore fsync default is {default!r}, expected True")
    return "JsonlLogStore fsync defaults to True (durable by default)"


def check_torn_tail_tolerated(block: dict[str, Any]) -> str:
    if not block.get("torn_tail_tolerated"):
        _fail("contract torn_tail_tolerated must be true")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.jsonl"
        good = _system_record()
        with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
            store.write(good)
        with open(path, "ab") as fh:
            fh.write(b'{"timestamp_ns": 2, "sev')  # torn write, no newline
        recovered = read_records(path)
    if recovered != [good]:
        _fail("torn trailing fragment was not dropped (reader fabricated or lost a record)")
    return "a torn trailing fragment is dropped, never fabricated into a record"


def check_corruption_fails_closed(block: dict[str, Any]) -> str:
    if not block.get("corruption_fails_closed"):
        _fail("contract corruption_fails_closed must be true")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.jsonl"
        path.write_bytes(b'{"timestamp_ns": 1}\ngarbage-not-json\n')
        try:
            read_records(path)
        except LogStoreCorruptionError:
            return "a complete-but-unparseable line fails closed with LogStoreCorruptionError"
    _fail("corrupt complete line did not raise LogStoreCorruptionError")
    raise RuntimeError("unreachable")


def check_query_filters(block: dict[str, Any]) -> str:
    expected_fields = set(block["query_filter_fields"])
    sig = inspect.signature(query)
    actual_fields = {name for name in sig.parameters if name != "records"}
    if actual_fields != expected_fields:
        _fail(
            f"query() filter params {sorted(actual_fields)} do not match contract "
            f"query_filter_fields {sorted(expected_fields)}"
        )
    records = [
        _system_record(ts=100, severity=Severity.DEBUG),
        _system_record(ts=200, severity=Severity.WARN),
        _system_record(ts=300, severity=Severity.CRITICAL),
    ]
    if [r.timestamp_ns for r in query(records, min_severity=Severity.WARN)] != [200, 300]:
        _fail("query min_severity filter is wrong")
    if [r.timestamp_ns for r in query(records, start_ns=200, end_ns=300)] != [200, 300]:
        _fail("query time-window filter is wrong")
    if [r.timestamp_ns for r in query(records, newest_first=True, limit=1)] != [300]:
        _fail("query newest_first + limit is wrong")
    return f"query() exposes the {len(expected_fields)} contracted filters and applies them"


def check_rotation_bounded(block: dict[str, Any]) -> str:
    if not block.get("rotation_opt_in"):
        _fail("contract rotation_opt_in must be true")
    # Default: no rotation (unbounded append, no eviction).
    sig = inspect.signature(JsonlLogStore.__init__)
    if sig.parameters["max_bytes"].default is not None:
        _fail("JsonlLogStore.max_bytes default must be None (opt-in rotation)")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.jsonl"
        with JsonlLogStore(
            path, log_class=LogClass.SYSTEM, max_bytes=200, max_files=2, fsync=False
        ) as store:
            for i in range(12):
                store.write(_system_record(ts=i))
            recovered = store.read()
        # Bounded retention dropped the oldest; the newest survived in order.
        timestamps = [r.timestamp_ns for r in recovered]
        if timestamps != sorted(timestamps):
            _fail("rotated read is not in chronological insertion order")
        if not timestamps or timestamps[-1] != 11:
            _fail("rotation lost the most recent record")
        if len(recovered) >= 12:
            _fail("rotation did not bound retention")
        if path.with_name("system.jsonl.3").exists():
            _fail("rotation kept more than max_files segments")
    return "rotation is opt-in (default unbounded); when set it retains a bounded, ordered window"


def check_dependency_direction(block: dict[str, Any]) -> str:
    del block
    forbidden = ("atp_strategy", "atp_api", "atp_cli", "atp_ws", "atp_readiness", "atp_config")
    source = inspect.getsource(persistence_module)
    for name in forbidden:
        if f"import {name}" in source or f"from {name}" in source:
            _fail(f"persistence.py imports forbidden upstream module {name!r}")
    return f"persistence.py imports no upstream consumer package ({len(forbidden)} checked)"


def _iter_imports(node: ast.AST) -> Iterable[str]:
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                yield alias.name
        elif isinstance(child, ast.ImportFrom) and child.module is not None:
            yield child.module


def check_no_upstream_import_ast(block: dict[str, Any]) -> str:
    del block
    forbidden = {"atp_strategy", "atp_api", "atp_cli", "atp_ws", "atp_readiness", "atp_config"}
    path = ROOT / "python" / "atp_logging" / "persistence.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    leaked = [imp for imp in _iter_imports(tree) if imp.split(".")[0] in forbidden]
    if leaked:
        _fail(f"AST-level upstream import leak in persistence.py: {leaked}")
    return f"AST-level: persistence.py imports no upstream package ({len(forbidden)} checked)"


def check_vendor_token_isolation(block: dict[str, Any]) -> str:
    forbidden = block["vendor_forbidden_tokens"]
    text = (ROOT / "python" / "atp_logging" / "persistence.py").read_text(encoding="utf-8")
    leaked = [token for token in forbidden if token in text]
    if leaked:
        _fail(f"vendor tokens leaked into persistence.py: {leaked}")
    return f"no vendor SDK tokens ({len(forbidden)} checked) leaked into persistence.py"


def check_deferred_list(block: dict[str, Any]) -> str:
    deferred = block["deferred"]
    if not isinstance(deferred, list) or not deferred:
        _fail("contract.deferred must be a non-empty list")
    for entry in deferred:
        if not isinstance(entry.get("feature"), str) or not entry["feature"].strip():
            _fail(f"deferred entry missing non-empty 'feature': {entry}")
        if not isinstance(entry.get("what"), str) or not entry["what"].strip():
            _fail(f"deferred entry missing non-empty 'what': {entry}")
    named = {entry["feature"] for entry in deferred}
    required = {"SRS-UI-001", "SRS-API-001"}
    missing = required - named
    if missing:
        _fail(f"deferred list is missing required downstream features: {sorted(missing)}")
    return f"deferred list names the {sorted(required)} downstream halves keeping SRS-LOG-001 false"


def assert_log_persistence_static(_config: dict | None = None, root: Path = ROOT) -> list[str]:
    del root
    block = _load_contract()
    return [
        check_module_path(block),
        check_required_exports(block),
        check_error_hierarchy(block),
        check_store_implements_sink(block),
        check_separation_enforced_at_sink(block),
        check_separate_files(block),
        check_build_rejects_same_filename(block),
        check_durable_roundtrip(block),
        check_fsync_default(block),
        check_torn_tail_tolerated(block),
        check_corruption_fails_closed(block),
        check_query_filters(block),
        check_rotation_bounded(block),
        check_dependency_direction(block),
        check_no_upstream_import_ast(block),
        check_vendor_token_isolation(block),
        check_deferred_list(block),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=ROOT, help="repository root (default: parent of this script)"
    )
    args = parser.parse_args(argv)
    try:
        evidence = assert_log_persistence_static(root=args.root)
    except LogPersistenceCheckError as exc:
        print(f"SRS-LOG-001 PERSISTENCE FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "SRS-LOG-001 PERSISTENCE PASS — durable system/strategy persistent sinks "
        "(JsonlLogStore: separation enforced at the sink, fsync-durable append, torn-tail "
        "tolerant + corruption fails closed, opt-in bounded rotation, GET /api/v1/logs query "
        "surface). Dashboard pane (SRS-UI-001) + live REST/WebSocket/CLI handlers (SRS-API-001) "
        "still deferred, so SRS-LOG-001 stays passes:false."
    )
    for line in evidence:
        print(f"  * {line}")
    importlib.import_module("atp_logging.persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
