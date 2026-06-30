"""L1 unit tests for the SRS-LOG-001 persistent log sinks.

Covers :class:`atp_logging.persistence.JsonlLogStore` and its read/query
surface in isolation: round-trip fidelity, the system-vs-strategy
separation guard, durability across reopen, size-based rotation +
retention, torn-tail tolerance vs mid-file corruption, the
``GET /api/v1/logs`` query filters, and input validation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_logging import (  # noqa: E402
    LogClass,
    LogClassError,
    LogPayloadError,
    LogRecord,
    Severity,
    Source,
)
from atp_logging.persistence import (  # noqa: E402
    JsonlLogStore,
    LogStoreCorruptionError,
    LogStoreError,
    build_separated_log_dispatcher,
    query,
    read_records,
)

pytestmark = [pytest.mark.unit]


def _system(
    *,
    ts: int = 1_000,
    severity: Severity = Severity.INFO,
    source: Source = Source.KILL_SWITCH,
    event_type: str = "ACTIVATION",
    message: str = "msg",
    correlation_id: str = "corr-1",
) -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=severity,
        source=source,
        event_type=event_type,
        message=message,
        correlation_id=correlation_id,
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def _strategy(*, ts: int = 2_000, strategy_id: str = "alpha") -> LogRecord:
    return LogRecord(
        timestamp_ns=ts,
        severity=Severity.INFO,
        source=Source.STRATEGY,
        event_type="user_signal",
        message="strategy emitted",
        correlation_id="s-1",
        log_class=LogClass.STRATEGY,
        strategy_id=strategy_id,
    )


def test_write_read_roundtrip_preserves_all_fields(tmp_path: Path) -> None:
    rec = _system(severity=Severity.ERROR, message="ünïcode ✓", correlation_id="c-9")
    with JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM) as store:
        store.write(rec)
    [back] = read_records(tmp_path / "system.jsonl")
    assert back == rec  # frozen dataclass equality over all eight fields


def test_separate_files_for_system_and_strategy(tmp_path: Path) -> None:
    dispatcher, system_store, strategy_store = build_separated_log_dispatcher(tmp_path)
    with system_store, strategy_store:
        dispatcher.dispatch(_system())
        dispatcher.dispatch(_strategy())
    assert (tmp_path / "system.jsonl").exists()
    assert (tmp_path / "strategy.jsonl").exists()
    sys_lines = (tmp_path / "system.jsonl").read_text().splitlines()
    strat_lines = (tmp_path / "strategy.jsonl").read_text().splitlines()
    assert len(sys_lines) == 1 and json.loads(sys_lines[0])["log_class"] == "system"
    assert len(strat_lines) == 1 and json.loads(strat_lines[0])["log_class"] == "strategy"


def test_store_refuses_wrong_class(tmp_path: Path) -> None:
    with JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM) as system_store:
        with pytest.raises(LogClassError):
            system_store.write(_strategy())
    # The wrong-class record left no trace.
    assert read_records(tmp_path / "system.jsonl") == []


def test_direct_write_validates_schema(tmp_path: Path) -> None:
    # A caller bypassing the dispatcher and writing straight to the store must
    # still be held to the full schema — no malformed audit entry can land.
    sys_path = tmp_path / "system.jsonl"
    strat_path = tmp_path / "strategy.jsonl"

    def patched(base: LogRecord, **overrides: object) -> LogRecord:
        rec = base
        for name, value in overrides.items():
            object.__setattr__(rec, name, value)  # bypass the frozen dataclass
        return rec

    with JsonlLogStore(sys_path, log_class=LogClass.SYSTEM) as store:
        # Invalid timestamp.
        with pytest.raises(LogPayloadError):
            store.write(patched(_system(), timestamp_ns=-1))
        # Empty required string.
        with pytest.raises(LogPayloadError):
            store.write(patched(_system(), message="   "))
        # Out-of-taxonomy event_type for the source.
        with pytest.raises(LogPayloadError):
            store.write(patched(_system(), event_type="NOT_A_REAL_EVENT"))
        # Forbidden strategy_id on a SYSTEM record.
        with pytest.raises(LogClassError):
            store.write(patched(_system(), strategy_id="leaked"))

    with JsonlLogStore(strat_path, log_class=LogClass.STRATEGY) as strat_store:
        # STRATEGY record with an empty strategy_id.
        with pytest.raises(LogClassError):
            strat_store.write(patched(_strategy(), strategy_id=""))

    # None of the rejected records were persisted.
    assert read_records(sys_path) == []
    assert read_records(strat_path) == []


def test_durable_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(_system(ts=10))
        store.write(_system(ts=20, source=Source.IB_GATEWAY, event_type="DISCONNECT"))
    # A fresh store instance sees both records.
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as reopened:
        records = reopened.read()
    assert [r.timestamp_ns for r in records] == [10, 20]


def test_rotation_retains_bounded_window(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    # Tiny max_bytes forces a rotation on (almost) every record.
    with JsonlLogStore(
        path, log_class=LogClass.SYSTEM, max_bytes=200, max_files=2, fsync=False
    ) as store:
        for i in range(10):
            store.write(_system(ts=i, correlation_id=f"c-{i}"))
        records = store.read()
    # With max_files=2 rotated segments plus the active one, the oldest
    # records are evicted; the most recent survive in insertion order.
    timestamps = [r.timestamp_ns for r in records]
    assert timestamps == sorted(timestamps)  # chronological across segments
    assert timestamps[-1] == 9
    assert len(records) < 10  # bounded retention dropped the oldest
    # The active + at most max_files rotated segments exist; nothing beyond.
    assert not (path.with_name("system.jsonl.3")).exists()


def test_torn_trailing_fragment_is_dropped_not_fabricated(tmp_path: Path) -> None:
    path = tmp_path / "system.jsonl"
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(_system(ts=1))
        store.write(_system(ts=2))
    # Simulate a crash mid-write: an unterminated fragment with no newline.
    with open(path, "ab") as fh:
        fh.write(b'{"timestamp_ns": 3, "sever')
    records = read_records(path)
    assert [r.timestamp_ns for r in records] == [1, 2]  # torn fragment dropped


def test_complete_corrupt_line_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_bytes(b'{"timestamp_ns": 1}\nnot-json-at-all\n')
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_out_of_domain_enum_value_is_corruption(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    payload = _system().as_dict()
    payload["severity"] = "BOGUS"
    path.write_bytes((json.dumps(payload) + "\n").encode())
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_torn_multibyte_tail_preserves_good_records(tmp_path: Path) -> None:
    # A crash can tear a write mid-way through a multi-byte UTF-8 character.
    # The reader must drop only the torn tail, not fail decoding the whole
    # file (which would lose every good record before it).
    path = tmp_path / "system.jsonl"
    good = _system(ts=1, message="café ✓")  # exercises multi-byte content too
    with JsonlLogStore(path, log_class=LogClass.SYSTEM) as store:
        store.write(good)
    with open(path, "ab") as fh:
        fh.write('{"message": "tor'.encode())  # complete prefix...
        fh.write(b"\xe2\x9c")  # ...then a TRUNCATED 3-byte UTF-8 char (✓ cut short)
    records = read_records(path)
    assert records == [good]


def test_non_utf8_complete_line_is_corruption(tmp_path: Path) -> None:
    path = tmp_path / "bad.jsonl"
    # A complete (newline-terminated) line with invalid UTF-8 is corruption.
    path.write_bytes(b"\xff\xfe not utf8\n")
    with pytest.raises(LogStoreCorruptionError):
        read_records(path)


def test_read_fails_closed_on_invariant_violating_line(tmp_path: Path) -> None:
    # A structurally-valid JSON line that nonetheless violates the log-record
    # invariants (tampered file, or a stale-format writer) must be treated as
    # corruption on read, NOT served — the GET /api/v1/logs seam fails closed.
    path = tmp_path / "system.jsonl"
    base = _system().as_dict()
    violations: list[dict[str, object]] = [
        {**base, "strategy_id": "leaked"},  # SYSTEM record must not carry strategy_id
        {**base, "timestamp_ns": -1},  # impossible timestamp
        {**base, "message": "   "},  # empty required field
        {**base, "source": "strategy"},  # SYSTEM record with a STRATEGY source
        {**base, "event_type": "NOT_A_REAL_EVENT"},  # out-of-taxonomy event
    ]
    for bad in violations:
        path.write_bytes((json.dumps(bad) + "\n").encode())
        with pytest.raises(LogStoreCorruptionError):
            read_records(path)


def test_missing_file_reads_empty(tmp_path: Path) -> None:
    assert read_records(tmp_path / "never_written.jsonl") == []


def test_query_filters(tmp_path: Path) -> None:
    records = [
        _system(
            ts=100,
            severity=Severity.DEBUG,
            source=Source.ORDER_ROUTING,
            event_type="ROUTING_DECISION",
            correlation_id="a",
        ),
        _system(
            ts=200,
            severity=Severity.WARN,
            source=Source.IB_GATEWAY,
            event_type="DISCONNECT",
            correlation_id="b",
        ),
        _system(
            ts=300,
            severity=Severity.CRITICAL,
            source=Source.KILL_SWITCH,
            event_type="ACTIVATION",
            correlation_id="c",
        ),
    ]
    # Minimum-severity filter (inclusive, SYS-61 order).
    assert [r.timestamp_ns for r in query(records, min_severity=Severity.WARN)] == [200, 300]
    # Source filter.
    assert [r.timestamp_ns for r in query(records, source=Source.KILL_SWITCH)] == [300]
    # Event-type + correlation-id.
    assert [r.timestamp_ns for r in query(records, event_type="DISCONNECT")] == [200]
    assert [r.timestamp_ns for r in query(records, correlation_id="a")] == [100]
    # Time window (inclusive bounds).
    assert [r.timestamp_ns for r in query(records, start_ns=200, end_ns=300)] == [200, 300]
    # Limit + newest_first.
    assert [r.timestamp_ns for r in query(records, newest_first=True, limit=2)] == [300, 200]


def test_invalid_constructor_args(tmp_path: Path) -> None:
    with pytest.raises(LogStoreError):
        JsonlLogStore(tmp_path / "x.jsonl", log_class="system")  # type: ignore[arg-type]
    with pytest.raises(LogStoreError):
        JsonlLogStore(tmp_path / "x.jsonl", log_class=LogClass.SYSTEM, max_bytes=0)
    with pytest.raises(LogStoreError):
        JsonlLogStore(tmp_path / "x.jsonl", log_class=LogClass.SYSTEM, max_bytes=True)  # type: ignore[arg-type]
    with pytest.raises(LogStoreError):
        JsonlLogStore(tmp_path / "x.jsonl", log_class=LogClass.SYSTEM, max_files=0)


def test_query_validates_inputs() -> None:
    with pytest.raises(LogStoreError):
        query([], log_class="system")  # type: ignore[arg-type]
    with pytest.raises(LogStoreError):
        query([], min_severity="WARN")  # type: ignore[arg-type]
    with pytest.raises(LogStoreError):
        query([], limit=-1)


def test_build_separated_rejects_same_filename(tmp_path: Path) -> None:
    with pytest.raises(LogStoreError):
        build_separated_log_dispatcher(
            tmp_path, system_filename="logs.jsonl", strategy_filename="logs.jsonl"
        )


def test_build_separated_rejects_aliased_and_unsafe_filenames(tmp_path: Path) -> None:
    # Equivalent paths that differ as strings but alias the same file, plus
    # absolute / traversal names that would escape the log directory — all
    # must be refused so the two sinks stay physically separate.
    for sys_name, strat_name in [
        ("system.jsonl", "./system.jsonl"),  # alias of the system file
        ("system.jsonl", "sub/strategy.jsonl"),  # separator → escapes basename
        ("system.jsonl", "../strategy.jsonl"),  # parent traversal
        ("/abs/system.jsonl", "strategy.jsonl"),  # absolute
        ("system.jsonl", ".."),  # directory ref
    ]:
        with pytest.raises(LogStoreError):
            build_separated_log_dispatcher(
                tmp_path, system_filename=sys_name, strategy_filename=strat_name
            )
    # And nothing was left wired to a shared file.
    assert not (tmp_path / "system.jsonl").exists() or read_records(tmp_path / "system.jsonl") == []


def test_build_separated_rejects_samefile_symlink_alias(tmp_path: Path) -> None:
    # A symlinked directory makes two bare filenames resolve to one file even
    # though neither name has a separator; the os.path.samefile guard catches it.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    # Pre-create system.jsonl under real; then point the strategy store at the
    # same file via the symlinked directory using a hard alias on disk.
    (real / "shared.jsonl").touch()
    (real / "alias.jsonl").symlink_to(real / "shared.jsonl")
    with pytest.raises(LogStoreError):
        build_separated_log_dispatcher(
            real, system_filename="shared.jsonl", strategy_filename="alias.jsonl"
        )


def test_closed_store_rejects_write_and_close_is_idempotent(tmp_path: Path) -> None:
    store = JsonlLogStore(tmp_path / "system.jsonl", log_class=LogClass.SYSTEM)
    store.write(_system())
    store.close()
    store.close()  # idempotent
    with pytest.raises(LogStoreError):
        store.write(_system())
