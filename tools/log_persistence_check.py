#!/usr/bin/env python3
"""Persistent log sink contract check for SRS-LOG-001's runtime half.

Verifies that ``python/atp_logging/persistence.py`` matches the
``log_persistence_contract`` block in
``architecture/runtime_services.json`` and that :class:`JsonlLogStore` plus
its read/query surface enforce every documented runtime invariant: the
system-vs-strategy separation guard, crash-durable append, torn-tail
tolerance vs corruption fail-closed, the ``GET /api/v1/logs`` query filters,
and the dependency-direction / vendor-isolation rules.

It also checks the OPERATOR SURFACE the same block's ``operator_surface`` entry
declares — the ``atp_logs_service`` query handler / publisher / wiring and the
``atp_dashboard`` log pane — including the invariants that keep an audit surface
honest: an unwired runtime still defers to ``SRS-LOG-001``, a corrupt store fails
closed rather than reading as empty, and the dashboard's producer-coverage map
covers every AC-named system source.

This is the deterministic mirror of the L1 (``tests/unit/test_log_persistence.py``,
``tests/unit/test_logs_query_handler.py``) and L7
(``tests/domain/test_log_persistence.py``,
``tests/domain/test_log_operator_surface.py``) rigs; it runs at every boot via
``init.sh`` and on CI so contract drift cannot land silently. The PASS line is
``SRS-LOG-001 PERSISTENCE PASS`` — the sinks AND their operator surfaces are
built, but SRS-LOG-001 stays ``passes:false`` until the five unproduced SYS-61
system sources gain producers and the dashboard browser-automation evidence is
recorded.
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
    LogStoreClassMismatchError,
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


def check_separation_is_enforced_on_read_too(block: dict[str, Any]) -> str:
    """The store must fail closed when its FILE holds a foreign record.

    ``write`` guards the way in, but a trail restored from backup, recovered
    onto the wrong path, or hand-edited can still hold one — and the store is
    the object that claims a class, so it owes the guarantee on the way out as
    well. Left unchecked, the same contamination has two different wrong
    outcomes depending on how the caller filters: returned as though it belonged
    here, or silently filtered away so the broken separation leaves no trace.
    """

    if not block.get("separation_enforced_on_read"):
        _fail("contract separation_enforced_on_read must be true")

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "system.jsonl"
        with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
            store.write(_system_record())
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(_strategy_record().as_dict()) + "\n")
        with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
            for label, filters in (
                ("unfiltered", {}),
                ("filtered", {"source": Source.KILL_SWITCH}),
            ):
                try:
                    store.read(**filters)
                except LogStoreClassMismatchError:
                    continue
                _fail(
                    f"a contaminated system store returned from a {label} read instead of "
                    "failing closed (SRS-LOG-001 separation broken with no trace)"
                )
        # The lock-free reader gives the same guarantee to a caller that knows
        # its class but holds no store instance (the kill-switch status cell,
        # the availability CLI). Both filter hard, and a filter must not be what
        # decides whether broken separation is visible.
        try:
            read_records(path, expect_class=LogClass.SYSTEM, source=Source.KILL_SWITCH)
        except LogStoreClassMismatchError:
            pass
        else:
            _fail(
                "read_records(expect_class=...) let a filter launder a wrong-class record "
                "out of the result instead of failing closed"
            )
        for module_name, symbol in block["class_asserting_readers"].items():
            source = (ROOT / module_name).read_text(encoding="utf-8")
            if "expect_class" not in source:
                _fail(
                    f"{module_name} reads a known-class trail ({symbol}) without asserting the "
                    "class, so a contaminated store is silently filtered clean"
                )
    return (
        "JsonlLogStore.read and read_records(expect_class=...) fail closed on a contaminated "
        "trail — on an unfiltered read AND on a filter that would have excluded the foreign "
        "record, so no filter can launder it; the known-class callers pass expect_class"
    )


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
    # The dashboard-pane (SRS-UI-001) and REST/CLI/WS (SRS-API-001) halves are
    # BUILT — see the operator_surface block — so what must be named here is
    # what actually still keeps the feature false: the missing producers, the
    # core-runtime forwarding path, and the browser-automation evidence.
    required = {
        "SRS-LOG-001-core-forwarding",
        "SRS-LOG-001-producer-coverage",
        "SRS-LOG-001-dashboard-e2e",
    }
    missing = required - named
    if missing:
        _fail(f"deferred list is missing required downstream features: {sorted(missing)}")
    built = {"SRS-UI-001", "SRS-API-001"} & named
    if built:
        _fail(
            f"deferred list still names {sorted(built)}, but those halves are built "
            "(see log_persistence_contract.operator_surface) — an overstated deferred "
            "list hides what is really blocking the flip"
        )
    return f"deferred list names the {sorted(required)} halves keeping SRS-LOG-001 false"


def check_the_cli_documents_every_severity_it_accepts(block: dict[str, Any]) -> str:
    """The manual must not under-document the values the handler takes.

    ``admin logs`` is now a SERVED command, and its generated manual entry says
    the arguments above are the ones the live command honours. A ``--severity``
    summary that lists four of the five levels makes that sentence false for the
    fifth — and the missing one was CRITICAL, the level a kill-switch activation
    lands at, on the one CLI surface an operator uses to find critical audit
    events. Checked against the enum rather than a copy of the list.
    """

    del block

    commands = importlib.import_module("atp_cli").COMMANDS
    command = next(c for c in commands if c.group.value == "admin" and c.name == "logs")
    severity_arg = next(a for a in command.arguments if a.name == "--severity")
    missing = [s.value for s in Severity if s.value not in severity_arg.summary]
    if missing:
        _fail(
            f"`admin logs --severity` does not document the accepted level(s) {missing}; "
            "the handler accepts every Severity member"
        )

    manual = json.loads((ROOT / "python" / "atp_cli" / "manual.json").read_text(encoding="utf-8"))
    entry = next(
        argument
        for group in manual["groups"]
        for entry in group["commands"]
        if entry["invocation"] == "admin logs"
        for argument in entry["arguments"]
        if argument["name"] == "--severity"
    )
    frozen_missing = [s.value for s in Severity if s.value not in entry["summary"]]
    if frozen_missing:
        _fail(f"the FROZEN manual under-documents --severity levels {frozen_missing}")
    return (
        "`admin logs --severity` documents every Severity level it accepts, in the declaration "
        "AND in the frozen manual (CRITICAL included — it is where kill-switch activations land)"
    )


def check_a_resumed_read_does_not_reopen_the_history(block: dict[str, Any]) -> str:
    """A poller that remembers a position must not re-walk the trail to find it.

    The publisher polls once a second and rotation is opt-in, so a scan that
    starts at the head of the trail costs more every day the system runs — until
    the ticker's real cadence drifts past its interval while ``health()`` still
    reports ``ok``. Asserted by counting the SEGMENTS a resumed read opens, not
    by reading the docstring: the docstring was already right when this
    regressed.
    """

    if not block["operator_surface"].get("publisher_resumes_at_the_anchor_slot"):
        _fail("operator_surface.publisher_resumes_at_the_anchor_slot must be true")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        path = directory / "system.jsonl"
        # Small max_bytes so the trail really is several segments.
        store = JsonlLogStore(path, log_class=LogClass.SYSTEM, max_bytes=400, max_files=4)
        try:
            for index in range(40):
                store.write(_system_record(ts=1_700_000_000_000_000_000 + index))
            entries = list(
                persistence_module.iter_records_with_positions(
                    path, max_files=4, expect_class=LogClass.SYSTEM
                )
            )
            anchor_position, anchor_record = entries[-2]
            if persistence_module.record_at(path, anchor_position, max_files=4) != anchor_record:
                _fail("record_at did not recover the record at a position the reader just yielded")

            opened: list[Path] = []
            real_open = Path.open

            def counting_open(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                opened.append(self)
                return real_open(self, *args, **kwargs)

            Path.open = counting_open  # type: ignore[method-assign]
            try:
                resumed = list(
                    persistence_module.iter_records_with_positions(
                        path,
                        max_files=4,
                        expect_class=LogClass.SYSTEM,
                        resume_after=anchor_position,
                    )
                )
            finally:
                Path.open = real_open  # type: ignore[method-assign]
        finally:
            store.close()

    if [record for _position, record in resumed] != [entries[-1][1]]:
        _fail("a resumed read did not return exactly the records after the resume position")
    segments_in_trail = len({position.inode for position, _record in entries})
    if len(opened) >= segments_in_trail:
        _fail(
            f"a resumed read opened {len(opened)} segment(s) of a {segments_in_trail}-segment "
            "trail — it is still walking the history to find its place"
        )
    return (
        f"a resumed read opened {len(opened)} of {segments_in_trail} segments and returned only "
        "the records after the anchor (record_at verifies the slot by content first)"
    )


def check_reads_are_memory_bounded(block: dict[str, Any]) -> str:
    """The operator read path caps MEMORY, not just the number of rows returned.

    The trail is append-only and rotation is opt-in, so slicing a page off a
    fully-materialised list would let the log's size decide whether the runtime
    survives a query.
    """

    import inspect as _inspect

    streaming = getattr(persistence_module, block["streaming_read_function"])
    bounded = getattr(persistence_module, block["bounded_read_function"])
    if not _inspect.isgeneratorfunction(streaming):
        _fail(f"{block['streaming_read_function']} must be a generator (streaming read)")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _dispatcher, system_store, _strategy = build_separated_log_dispatcher(directory)
        total = 50
        for index in range(total):
            system_store.write(
                LogRecord(
                    timestamp_ns=1_700_000_000_000_000_000 + index,
                    severity=Severity.INFO,
                    source=Source.KILL_SWITCH,
                    event_type="ACTIVATION",
                    message=f"event {index:03d}",
                    correlation_id=f"c-{index:03d}",
                    log_class=LogClass.SYSTEM,
                )
            )
        system_store.close()
        path = directory / block["default_system_filename"]
        page, matched = bounded(path, limit=5, log_class=LogClass.SYSTEM)
        if len(page) != 5:
            _fail(f"bounded read returned {len(page)} records for limit=5")
        if matched != total:
            _fail(f"bounded read reported {matched} matches; the trail holds {total}")
        newest = [record.message for _position, record in page]
        if newest != [f"event {i:03d}" for i in range(total - 1, total - 6, -1)]:
            _fail(f"bounded read did not return the NEWEST records newest-first: {newest}")
        # The handler must use it — a handler that re-materialised the trail
        # would defeat the bound no matter how good the reader is.
        source = inspect.getsource(importlib.import_module("atp_logs_service.handlers"))
        if block["bounded_read_function"] not in source:
            _fail("LogsQueryHandler does not use the bounded reader")
    return (
        f"reads are memory-bounded: {block['streaming_read_function']} streams and "
        f"{block['bounded_read_function']} keeps only the page (exact total preserved)"
    )


def check_resume_cursor_is_physical_not_value_based(block: dict[str, Any]) -> str:
    """A cursor that resumes across reads must key on POSITION, not content.

    Two audit records may legitimately be byte-identical (a retried operation
    writing the same message with the same correlation id in the same
    nanosecond). A value-equality cursor reads the second as "already sent" and
    drops it — a silent loss on the one channel that must not lose events.
    """

    positional = getattr(persistence_module, block["positional_read_function"])
    cursor_type = getattr(persistence_module, block["cursor_type"])

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _dispatcher, system_store, _strategy = build_separated_log_dispatcher(directory)
        duplicate = _system_record()
        system_store.write(duplicate)
        system_store.write(duplicate)  # byte-identical second line
        system_store.close()

        entries = list(positional(directory / block["default_system_filename"]))
        if len(entries) != 2:
            _fail(f"expected 2 persisted lines, streamed {len(entries)}")
        positions = [position for position, _record in entries]
        records = [record for _position, record in entries]
        if not all(isinstance(position, cursor_type) for position in positions):
            _fail(
                f"{block['positional_read_function']} must yield {block['cursor_type']} positions"
            )
        if records[0] != records[1]:
            _fail("the fixture failed to persist two identical records")
        if positions[0] == positions[1]:
            _fail(
                "two byte-identical records share a cursor position — a resume cursor "
                "keyed on this would silently drop the second"
            )
        # The publisher must actually key on the position type.
        source = inspect.getsource(importlib.import_module("atp_logs_service.publisher"))
        if block["cursor_type"] not in source:
            _fail(f"the LOGS publisher does not use {block['cursor_type']} as its cursor")
    return (
        f"resume cursor is physical: identical records get distinct "
        f"{block['cursor_type']} positions and the LOGS publisher keys on them"
    )


def check_operator_surface_modules(block: dict[str, Any]) -> str:
    """Every module/class the operator_surface block declares actually exists."""

    surface = block.get("operator_surface")
    if not isinstance(surface, dict):
        _fail("log_persistence_contract is missing the 'operator_surface' block")
    for key in (
        "module_path",
        "publisher_module_path",
        "wiring_module_path",
        "dashboard_provider_module",
    ):
        path = ROOT / surface[key]
        if not path.exists():
            _fail(f"operator_surface.{key} does not exist: {surface[key]}")
    service = importlib.import_module("atp_logs_service")
    pane = importlib.import_module("atp_dashboard.logs")
    for module, key in ((service, "handler_class"), (service, "publisher_class")):
        if not hasattr(module, surface[key]):
            _fail(f"atp_logs_service is missing {surface[key]!r}")
    if not hasattr(service, surface["wiring_function"]):
        _fail(f"atp_logs_service is missing {surface['wiring_function']!r}")
    if not hasattr(pane, surface["dashboard_provider_class"]):
        _fail(f"atp_dashboard.logs is missing {surface['dashboard_provider_class']!r}")
    return (
        f"operator_surface: {surface['package']} ships "
        f"{surface['handler_class']} / {surface['publisher_class']} / "
        f"{surface['wiring_function']}, and atp_dashboard.logs ships "
        f"{surface['dashboard_provider_class']}"
    )


def check_operator_surface_matches_declared_sdk(block: dict[str, Any]) -> str:
    """The handler serves the SDK-declared operation ids, params, and fields.

    A handler bound to an identifier the contract does not declare would never
    be reached; one that accepts a parameter the SDK does not declare (or drops
    one it does) is the contract-vs-handler drift this check exists to catch.
    """

    surface = block["operator_surface"]
    service = importlib.import_module("atp_logs_service")
    routes = importlib.import_module("atp_api").ROUTES
    commands = importlib.import_module("atp_cli").COMMANDS
    channels = {channel.name.value for channel in importlib.import_module("atp_ws").EVENT_CHANNELS}

    route = next((r for r in routes if r.path == "/api/v1/logs"), None)
    if route is None:
        _fail("atp_api declares no GET /api/v1/logs route for the handler to serve")
    if f"{route.method.value} {route.path}" != surface["rest_operation"]:
        _fail(f"operator_surface.rest_operation does not match the declared route: {route.path}")
    if service.LOGS_REST_OPERATION != surface["rest_operation"]:
        _fail("LogsQueryHandler is registered for an identifier the contract does not declare")
    if sorted(route.request_fields) != sorted(surface["rest_request_fields"]):
        _fail(
            "operator_surface.rest_request_fields drifted from the declared route: "
            f"{sorted(route.request_fields)} != {sorted(surface['rest_request_fields'])}"
        )
    # The handler must accept exactly the declared params (plus the runtime's
    # generic confirm token) and refuse anything else.
    accepted = set(service.REST_PARAMS) - {"confirm"}
    if accepted != set(route.request_fields):
        _fail(f"handler REST_PARAMS {sorted(accepted)} != declared {sorted(route.request_fields)}")

    command = next(
        (c for c in commands if c.group.value == "admin" and c.name == "logs"),
        None,
    )
    if command is None:
        _fail("atp_cli declares no `admin logs` command for the handler to serve")
    if command.invocation != surface["cli_operation"] != service.LOGS_CLI_OPERATION:
        _fail("operator_surface.cli_operation does not match the declared CLI invocation")
    declared_options = {
        arg.name.lstrip("-").replace("-", "_") for arg in command.arguments if arg.name != "--json"
    }
    if set(service.CLI_PARAMS) != declared_options:
        _fail(
            f"handler CLI_PARAMS {sorted(service.CLI_PARAMS)} != declared {sorted(declared_options)}"
        )

    if surface["ws_channel"] not in channels:
        _fail(f"atp_ws declares no {surface['ws_channel']!r} channel")
    if set(surface["event_fields"]) != set(service.EVENT_FIELDS):
        _fail("operator_surface.event_fields drifted from atp_logs_service.EVENT_FIELDS")
    # The LIVE response shape must equal the declaration in BOTH directions: a
    # superset is undeclared drift a strict client cannot parse, a subset is a
    # promise the handler does not keep. Serve a real request and compare.
    declared_top = set(route.response_fields)
    registry = importlib.import_module("atp_runtime.registry")
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _dispatcher, system_store, _strategy = build_separated_log_dispatcher(directory)
        system_store.write(_system_record())
        system_store.close()
        handler = service.LogsQueryHandler(
            system_store_path=directory / block["default_system_filename"],
            strategy_store_path=directory / block["default_strategy_filename"],
        )
        body = handler.handle(
            registry.Request(
                surface=registry.Surface.REST,
                operation=registry.OperationKey(registry.Surface.REST, surface["rest_operation"]),
                method="GET",
                path="/api/v1/logs",
                query={},
            )
        ).body
        events = body.get("events")
        if not isinstance(events, list) or not events:
            _fail("the handler served no events for a store that holds one record")
        emitted_top = set(body)
        emitted_item = set(events[0])
    # Compare the two LEVELS separately: the per-event fields live under
    # ``events[]``, and documenting them beside the array would send a generated
    # client looking in the wrong place.
    if emitted_top != declared_top:
        _fail(
            "the live GET /api/v1/logs top-level response and its atp_api declaration "
            f"disagree: only-live={sorted(emitted_top - declared_top)}, "
            f"only-declared={sorted(declared_top - emitted_top)}"
        )
    declared_item = {name for name, _type in dict(route.response_item_fields).get("events", ())}
    if emitted_item != declared_item:
        _fail(
            "the live GET /api/v1/logs event shape and its declared events[] item "
            f"disagree: only-live={sorted(emitted_item - declared_item)}, "
            f"only-declared={sorted(declared_item - emitted_item)}"
        )
    return (
        f"operator surface matches the SDK: {surface['rest_operation']}, "
        f"{surface['cli_operation']}, {surface['ws_channel']} channel, "
        f"{len(surface['rest_request_fields'])} request fields, "
        f"{len(surface['event_fields'])} event fields"
    )


def check_unwired_runtime_still_defers(block: dict[str, Any]) -> str:
    """A composition that never wired logs must NOT look served."""

    del block
    runtime_module = importlib.import_module("atp_runtime.runtime")
    runtime = runtime_module.OperatorInterfaceRuntime()
    status, body = runtime.dispatch_rest("GET", "/api/v1/logs", b"")
    if status != 501:
        _fail(f"a bare runtime answered GET /api/v1/logs with {status}, not a deferred 501")
    owner = body.get("error", {}).get("detail", {}).get("owner")
    if owner != "SRS-LOG-001":
        _fail(f"the deferred LOGS handler names owner {owner!r}, not SRS-LOG-001")
    if runtime.is_publisher_registered("LOGS"):
        _fail("a bare runtime claims the LOGS publisher without anything publishing")
    return "an unwired runtime defers GET /api/v1/logs to SRS-LOG-001 and claims no LOGS publisher"


def check_query_surface_fails_closed_on_corruption(block: dict[str, Any]) -> str:
    """The read surfaces refuse to render an unreadable trail as an empty one."""

    surface = block["operator_surface"]
    service = importlib.import_module("atp_logs_service")
    pane_module = importlib.import_module("atp_dashboard.logs")
    errors = importlib.import_module("atp_runtime.errors")
    registry = importlib.import_module("atp_runtime.registry")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _dispatcher, system_store, _strategy_store = (
            persistence_module.build_separated_log_dispatcher(directory)
        )
        system_store.write(_sample_system_record())
        with (directory / block["default_system_filename"]).open("a", encoding="utf-8") as handle:
            handle.write("{not a record}\n")

        handler = service.LogsQueryHandler(
            system_store_path=directory / block["default_system_filename"],
            strategy_store_path=directory / block["default_strategy_filename"],
        )
        request = registry.Request(
            surface=registry.Surface.REST,
            operation=registry.OperationKey(registry.Surface.REST, surface["rest_operation"]),
            method="GET",
            path="/api/v1/logs",
            query={},
        )
        try:
            handler.handle(request)
        except errors.InterfaceError as error:
            if error.status != 500:
                _fail(f"a corrupt store produced HTTP {error.status}, not a 500")
        else:
            _fail("the query handler served a corrupt store instead of failing closed")

        cell = pane_module.LogPaneProvider(
            system_store_path=directory / block["default_system_filename"],
            strategy_store_path=directory / block["default_strategy_filename"],
        ).logs_snapshot()
        system_cell = cell["classes"]["system"]
        if system_cell["records"] is not None:
            _fail("the dashboard pane rendered an unreadable trail as a record list")
        if cell["ok"] is not False:
            _fail("the dashboard pane reported ok=True while a trail was unreadable")
    return "a corrupt store fails closed on the query handler (500) and the pane (records=None)"


def check_follow_is_not_advertised_anywhere(block: dict[str, Any]) -> str:
    """The contract, the CLI surface, and the frozen manual must agree on ``--follow``.

    The runtime cannot stream a command result to stdout, so the capability is
    not advertised at all. A contract that still promised a structured rejection
    would send a consumer looking for a machine-readable error where the shipped
    surface gives an argparse usage failure — drift in the opposite direction
    from the usual one, and just as misleading.
    """

    surface = block["operator_surface"]
    if surface.get("follow_declared") is not False:
        _fail("operator_surface.follow_declared must be false — the CLI declares no --follow")

    commands = importlib.import_module("atp_cli").COMMANDS
    command = next(c for c in commands if c.group.value == "admin" and c.name == "logs")
    if any(arg.name == "--follow" for arg in command.arguments):
        _fail("`admin logs` declares a --follow option the runtime cannot serve")
    if "LOGS WebSocket channel" not in command.summary:
        _fail("`admin logs` does not point the operator at the streaming surface")

    manual = json.loads((ROOT / "python" / "atp_cli" / "manual.json").read_text(encoding="utf-8"))
    if "--follow" in json.dumps(manual):
        _fail("the frozen CLI manual still advertises --follow")

    # EVERY contract block, not just this feature's: the same claim was written
    # into operator_workflow_surface_contract too, and a per-block check missed
    # it. Any block promising a structured rejection is promising a failure
    # shape the shipped surface does not produce.
    contracts = json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))
    stale = [
        name
        for name, body in contracts.items()
        if "--follow" in json.dumps(body) and "refused with a structured" in json.dumps(body)
    ]
    if stale:
        _fail(
            f"contract block(s) {sorted(stale)} still promise a structured --follow rejection; "
            "the CLI declares no such option and argparse refuses it before dispatch"
        )

    service = importlib.import_module("atp_logs_service")
    if "follow" in service.CLI_PARAMS:
        _fail("the handler still accepts a `follow` option the surface does not declare")
    return (
        "--follow is advertised nowhere (contract, atp_cli.commands, frozen manual, handler "
        "params) and the command summary names the LOGS WebSocket channel instead"
    )


def check_no_block_still_defers_a_built_surface(block: dict[str, Any]) -> str:
    """No contract block may describe a SHIPPED log surface as deferred work.

    The same claim was written into three blocks over this feature's life
    (log_persistence_contract, log_record_contract,
    operator_workflow_surface_contract), and fixing one at a time left the others
    telling downstream agents that shipped code is still somebody else's to
    build. This scans every block for the phrasings that mean "not built yet"
    applied to a surface the operator_surface entry says IS built.
    """

    surface = block["operator_surface"]
    contracts = json.loads(_RUNTIME_SERVICES.read_text(encoding="utf-8"))
    # Phrases that assert one of these surfaces is still owed by someone else.
    stale_claims = (
        "dashboard log pane rendering owned by SRS-UI-001",
        "CLI runner owned by SRS-API-001",
        "Dashboard log pane rendering for both the system and strategy log classes (per",
        "Live GET /api/v1/logs REST handler body + LOGS WebSocket channel publisher + admin logs "
        "CLI runner. The route",
    )
    offenders: list[tuple[str, str]] = []
    for name, body in contracts.items():
        if not isinstance(body, dict):
            continue
        text = json.dumps(body)
        for claim in stale_claims:
            if claim in text:
                offenders.append((name, claim[:60]))
    if offenders:
        _fail(
            "contract block(s) still describe a BUILT log surface as deferred: "
            f"{offenders} — see log_persistence_contract.operator_surface "
            f"({surface['package']} / {surface['dashboard_provider_module']})"
        )
    return (
        "no contract block describes the shipped log surfaces (REST / CLI / LOGS "
        "publisher / dashboard pane) as somebody else's deferred work"
    )


def check_missing_store_fails_closed(block: dict[str, Any]) -> str:
    """A CONFIGURED trail that is not there must not answer like an empty one.

    Reading a missing file succeeds trivially and yields zero records — the same
    shape as a healthy quiet system. A deleted store, a mispointed directory, or
    a writer that never started would otherwise show a green pane and a 200 over
    missing audit history.
    """

    service = importlib.import_module("atp_logs_service")
    pane_module = importlib.import_module("atp_dashboard.logs")
    errors = importlib.import_module("atp_runtime.errors")
    registry = importlib.import_module("atp_runtime.registry")

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)  # deliberately empty: no store files at all
        system = directory / block["default_system_filename"]
        strategy = directory / block["default_strategy_filename"]

        handler = service.LogsQueryHandler(system_store_path=system, strategy_store_path=strategy)
        try:
            handler.handle(
                registry.Request(
                    surface=registry.Surface.REST,
                    operation=registry.OperationKey(
                        registry.Surface.REST, block["operator_surface"]["rest_operation"]
                    ),
                    method="GET",
                    path="/api/v1/logs",
                    query={},
                )
            )
        except errors.InterfaceError as error:
            if error.type != "LOGS_STORE_MISSING":
                _fail(f"a missing store raised {error.type!r}, not LOGS_STORE_MISSING")
        else:
            _fail("the query handler answered 200 for a store that does not exist")

        snapshot = pane_module.LogPaneProvider(
            system_store_path=system, strategy_store_path=strategy
        ).logs_snapshot()
        if snapshot["ok"] is not False:
            _fail("the dashboard pane reported ok=True with no store present")
        for log_class, cell in snapshot["classes"].items():
            if cell["records"] is not None:
                _fail(f"the {log_class} cell rendered a missing trail as a record list")
            if cell["store_present"] is not False:
                _fail(f"the {log_class} cell claims store_present for a file that is absent")
    return (
        "a missing store fails closed on both surfaces (LOGS_STORE_MISSING / "
        "records=None), never as an empty trail"
    )


def check_producer_coverage_is_stated(block: dict[str, Any]) -> str:
    """The pane states a coverage verdict for EVERY AC-named system source."""

    del block
    pane_module = importlib.import_module("atp_dashboard.logs")
    records = importlib.import_module("atp_logging.records")
    covered = set(pane_module.SOURCE_COVERAGE)
    if covered != set(records.SYSTEM_SOURCES):
        _fail(
            "SOURCE_COVERAGE does not cover every SYS-61 system source: missing "
            f"{sorted(s.value for s in set(records.SYSTEM_SOURCES) - covered)}"
        )
    # Per EVENT TYPE, not just per source. "partial" tells an operator that
    # something under a source is missing without saying what, which is how
    # market_data's SEQUENCE_GAP — declared, unproduced, and unproducible from
    # IB's API — sat unnamed behind a note that accounted for the other three.
    unproduced_total = 0
    for source, coverage in pane_module.SOURCE_COVERAGE.items():
        declared = set(records.EVENT_TYPES_BY_SOURCE[source])
        cell = coverage.as_dict(source)
        if not set(coverage.produced) <= declared:
            _fail(
                f"{source.value} claims to produce event types it does not declare: "
                f"{sorted(set(coverage.produced) - declared)}"
            )
        if cell["state"] not in {"produced", "partial", "deferred"}:
            _fail(f"{source.value} has an unknown coverage state {cell['state']!r}")
        missing = cell["unproduced_event_types"]
        if set(cell["produced_event_types"]) | set(missing) != declared:
            _fail(f"{source.value} does not account for every declared event type")
        if missing and not coverage.owners:
            _fail(f"{source.value} has unproduced event types {missing} but names no owner")
        if not missing and cell["state"] != "produced":
            _fail(f"{source.value} produces every declared event type but is not 'produced'")
        unproduced_total += len(missing)
    deferred = sorted(
        source.value
        for source, coverage in pane_module.SOURCE_COVERAGE.items()
        if coverage.state_for(source) == "deferred"
    )
    return (
        f"producer coverage stated for all {len(covered)} system sources AND every declared "
        f"event type ({unproduced_total} unproduced, each naming an owner); {len(deferred)} "
        f"sources have no producer at all ({deferred})"
    )


def _sample_system_record() -> Any:
    records = importlib.import_module("atp_logging.records")
    return records.LogRecord(
        timestamp_ns=1_700_000_000_000_000_000,
        severity=records.Severity.CRITICAL,
        source=records.Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message="check-fixture activation",
        correlation_id="check-1",
        log_class=records.LogClass.SYSTEM,
    )


def check_public_docs_do_not_call_a_live_surface_contract_only(block: dict[str, Any]) -> str:
    """The three FROZEN public documents must not advertise LOGS as a placeholder.

    ``openapi.json`` / ``manual.json`` / ``asyncapi.json`` are the generated
    contracts an integrator and an operator actually read. Every unimplemented
    entry carries a "Contract only — concrete behaviour lands with the downstream
    feature" sentence, which is exactly right until a handler lands and exactly
    backwards afterwards: it tells a client not to build against an endpoint that
    returns audit records, and tells an operator the log trail is unavailable when
    the command works. The declarations (``Route.served_by``,
    ``Command.served_by``, ``EventChannel.served_by``) drive the generators, so
    this pins the GENERATED artefacts too — a regeneration that dropped the
    declaration would otherwise quietly restore the placeholder.

    Scoped deliberately to the LOGS surfaces. Other features' entries are their
    own contracts to keep honest; see the session note.
    """

    del block

    surfaces = (
        ("openapi.json", ROOT / "python" / "atp_api" / "openapi.json", "/api/v1/logs"),
        ("manual.json", ROOT / "python" / "atp_cli" / "manual.json", "admin logs"),
        ("asyncapi.json", ROOT / "python" / "atp_ws" / "asyncapi.json", "LOGS"),
    )

    routes = importlib.import_module("atp_api").ROUTES
    commands = importlib.import_module("atp_cli").COMMANDS
    channels = importlib.import_module("atp_ws").EVENT_CHANNELS

    route = next(r for r in routes if r.path == "/api/v1/logs")
    command = next(c for c in commands if c.group.value == "admin" and c.name == "logs")
    channel = next(c for c in channels if c.name.value == "LOGS")
    for label, declared in (
        ("GET /api/v1/logs", route.served_by),
        ("admin logs", command.served_by),
        ("LOGS channel", channel.served_by),
    ):
        if declared != "SRS-LOG-001":
            _fail(f"{label} does not declare served_by=SRS-LOG-001 ({declared!r}) — it is live")

    # The generated descriptions, read the way a consumer reads them.
    openapi = json.loads(surfaces[0][1].read_text(encoding="utf-8"))
    description = openapi["paths"]["/api/v1/logs"]["get"]["description"]
    if "Contract only" in description:
        _fail("openapi.json still calls GET /api/v1/logs contract-only")
    if "Implemented by SRS-LOG-001" not in description:
        _fail("openapi.json does not name SRS-LOG-001 as the implementer of GET /api/v1/logs")

    manual = json.loads(surfaces[1][1].read_text(encoding="utf-8"))
    entries = [
        entry
        for group in manual["groups"]
        for entry in group["commands"]
        if entry["invocation"] == "admin logs"
    ]
    if len(entries) != 1:
        _fail(f"expected exactly one `admin logs` manual entry, found {len(entries)}")
    if "Contract only" in entries[0]["description"]:
        _fail("manual.json still calls `admin logs` contract-only")
    if "Implemented by SRS-LOG-001" not in entries[0]["description"]:
        _fail("manual.json does not name SRS-LOG-001 as the implementer of `admin logs`")

    asyncapi = json.loads(surfaces[2][1].read_text(encoding="utf-8"))
    logs_channel = asyncapi["channels"]["/ws/v1/logs"]
    if "Contract only" in logs_channel["description"]:
        _fail("asyncapi.json still calls the LOGS channel contract-only")
    if "Published by SRS-LOG-001" not in logs_channel["description"]:
        _fail("asyncapi.json does not name SRS-LOG-001 as the publisher of the LOGS channel")

    return (
        "public contracts match the live surface: openapi.json, manual.json, and "
        "asyncapi.json each name SRS-LOG-001 as the implementer/publisher of the LOGS "
        "surface instead of calling it contract-only (driven by served_by, so a "
        "regeneration cannot silently restore the placeholder)"
    )


def assert_log_persistence_static(_config: dict | None = None, root: Path = ROOT) -> list[str]:
    del root
    block = _load_contract()
    return [
        check_module_path(block),
        check_required_exports(block),
        check_error_hierarchy(block),
        check_store_implements_sink(block),
        check_separation_enforced_at_sink(block),
        check_separation_is_enforced_on_read_too(block),
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
        check_reads_are_memory_bounded(block),
        check_a_resumed_read_does_not_reopen_the_history(block),
        check_the_cli_documents_every_severity_it_accepts(block),
        check_resume_cursor_is_physical_not_value_based(block),
        check_operator_surface_modules(block),
        check_operator_surface_matches_declared_sdk(block),
        check_unwired_runtime_still_defers(block),
        check_query_surface_fails_closed_on_corruption(block),
        check_missing_store_fails_closed(block),
        check_follow_is_not_advertised_anywhere(block),
        check_no_block_still_defers_a_built_surface(block),
        check_producer_coverage_is_stated(block),
        check_public_docs_do_not_call_a_live_surface_contract_only(block),
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
        "tolerant + corruption fails closed, opt-in bounded rotation) PLUS the operator "
        "surfaces over them (GET /api/v1/logs + admin logs + LOGS publisher via "
        "atp_logs_service.wire_logs; dashboard pane via atp_dashboard.logs). SRS-LOG-001 stays "
        "passes:false: 5 of the 8 SYS-61 system sources have no producer (2 more are partial), "
        "and the dashboard browser-automation evidence is still owed."
    )
    for line in evidence:
        print(f"  * {line}")
    importlib.import_module("atp_logging.persistence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
