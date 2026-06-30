"""Concrete persistent log sinks for SRS-LOG-001's runtime half.

The :mod:`atp_logging` SDK surface (``records`` / ``dispatcher`` / ``errors``)
pins the structured :class:`~atp_logging.LogRecord` schema, the
:class:`~atp_logging.RoutedLogDispatcher` routing boundary, and the
:class:`~atp_logging.LogSink` protocol. This module ships the **runtime
half** SRS-LOG-001 requires on top of that seam:

* :class:`JsonlLogStore` — a durable, append-only, JSON-Lines
  :class:`~atp_logging.LogSink` bound to exactly one
  :class:`~atp_logging.LogClass`. Each store writes to its own physical
  file, so SYSTEM events and user STRATEGY logs are persisted to
  *separate* sinks — the literal SRS-LOG-001 requirement ("separate
  persistent system logs from user strategy logs"). The store *also*
  refuses a record of the wrong ``log_class`` at ``write`` time, so a
  caller that bypasses the dispatcher still cannot cross-contaminate the
  two audit trails.
* :func:`build_separated_log_dispatcher` — the boot wiring that binds a
  SYSTEM store and a *separate* STRATEGY store to a single
  :class:`~atp_logging.RoutedLogDispatcher`.
* :func:`read_records` / :meth:`JsonlLogStore.read` / :func:`query` — the
  read surface the live ``GET /api/v1/logs`` REST handler (SRS-API-001),
  the ``admin logs`` CLI runner, and the dashboard log pane (SRS-UI-001)
  consume once they land. The query filters mirror the
  ``GET /api/v1/logs`` parameters pinned in ``python/atp_api/openapi.json``
  (``log_class`` / ``severity`` / ``source`` / ``event_type`` /
  ``correlation_id`` / ``start_time`` / ``end_time``).

Durability model
----------------
Each :meth:`JsonlLogStore.write` appends one ``json.dumps(record.as_dict())``
line terminated by ``"\\n"``, then ``flush()`` + ``os.fsync()`` (on by
default) so a kill-switch activation or an IB disconnect survives a process
crash. A torn final write (a crash between ``write`` and ``fsync``) leaves a
trailing fragment with no terminating newline; the reader drops *only* that
unterminated trailing fragment and never fabricates a record from it. A
*complete* line (newline-terminated) that fails to parse is treated as
corruption and raises :class:`LogStoreCorruptionError` rather than being
silently skipped.

Language / architecture boundary
--------------------------------
This sink is Python because it is the **dashboard/API backend** for the
operator log surfaces — it backs the Python ``GET /api/v1/logs`` REST
endpoint, the dashboard log pane, and the ``admin logs`` CLI. AGENTS.md
permits the dashboard backend/API in another language "if it does not
become a core runtime service"; this store is operator-facing read/display
infrastructure, not part of the Rust trading core (execution, data,
simulation). The Rust core runtime services emit their own log records
locally per the ``log_record_contract`` (Rust core must not depend on the
Python SDK); a core-runtime durable sink + the Rust→operator-store
forwarding path is a separate concern and stays deferred.

Scope / honest boundaries
-------------------------
* A single store instance is owned by a single writing process (the logging
  runtime). Writes are guarded by an in-process lock so multiple emitter
  *threads* are safe; cross-*process* concurrent writers to the same file
  are out of scope (the runtime owns one writer per class).
* Rotation is opt-in (``max_bytes=None`` by default → unbounded append, so
  no audit record is ever evicted). When ``max_bytes`` is set, the store
  keeps at most ``max_files`` rotated segments; records older than the
  retained window are intentionally dropped per the documented retention
  policy (standard log rotation), not silently lost.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path

from .dispatcher import RoutedLogDispatcher, validate_log_record
from .errors import LogClassError, LogRecordError
from .records import LogClass, LogRecord, Severity, Source

__all__ = [
    "JsonlLogStore",
    "LogStoreCorruptionError",
    "LogStoreError",
    "build_separated_log_dispatcher",
    "query",
    "read_records",
]


class LogStoreError(LogRecordError):
    """Base class for persistent-log-store failures.

    Subclasses :class:`~atp_logging.LogRecordError` so a caller can catch
    the whole logging family (dispatch + persistence) with one
    ``except LogRecordError`` clause.
    """


class LogStoreCorruptionError(LogStoreError):
    """Raised when a *complete* (newline-terminated) stored line cannot be
    decoded back into a :class:`~atp_logging.LogRecord`.

    A torn *trailing* fragment (no terminating newline, the signature of a
    crash mid-write) is tolerated and dropped; only a fully-written line
    that is nonetheless unparseable signals real corruption. The reader
    fails closed rather than fabricating or silently skipping an audit
    record.
    """


# Severity rank for the minimum-severity query filter. ``Severity`` is a
# ``StrEnum`` with no inherent ordering, so the canonical comparison order
# (DEBUG < INFO < WARN < ERROR < CRITICAL, per SyRS SYS-61) is pinned here.
_SEVERITY_RANK: dict[Severity, int] = {severity: index for index, severity in enumerate(Severity)}


def _severity_rank(severity: Severity) -> int:
    try:
        return _SEVERITY_RANK[severity]
    except KeyError as exc:  # pragma: no cover — defensive enum guard
        raise LogStoreError(f"unknown Severity member {severity!r}") from exc


class JsonlLogStore:
    """Durable, append-only JSON-Lines sink bound to one :class:`LogClass`.

    The store implements the :class:`~atp_logging.LogSink` protocol so it can
    be registered on a :class:`~atp_logging.RoutedLogDispatcher`. Two stores
    bound to different files (one SYSTEM, one STRATEGY) realise the
    SRS-LOG-001 "separate persistent sinks" requirement; the
    ``log_class`` binding makes the separation defensive — a record of the
    wrong class is refused at ``write`` rather than landing in the wrong
    physical file.

    Args:
        path: the active log-segment file. Parent directories are created
            on construction.
        log_class: the :class:`LogClass` this store accepts; ``write``
            raises :class:`~atp_logging.LogClassError` for any other class.
        max_bytes: rotation threshold in bytes. ``None`` (default) means
            unbounded append (no record is ever evicted). When set, the
            active segment is rotated once a further append would exceed
            this size.
        max_files: number of rotated segments retained when ``max_bytes``
            is set (the active segment plus up to ``max_files`` ``.N``
            segments). Must be >= 1. Ignored when ``max_bytes is None``.
        fsync: when ``True`` (default) every append is flushed and
            ``os.fsync``-ed so it survives a crash; tests may disable it
            for speed.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        log_class: LogClass,
        max_bytes: int | None = None,
        max_files: int = 5,
        fsync: bool = True,
    ) -> None:
        if not isinstance(log_class, LogClass):
            raise LogStoreError(
                f"JsonlLogStore.log_class must be a LogClass; got {type(log_class).__name__}"
            )
        if max_bytes is not None and (
            isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
        ):
            raise LogStoreError(
                f"JsonlLogStore.max_bytes must be None or an int; got {type(max_bytes).__name__}"
            )
        if isinstance(max_bytes, int) and not isinstance(max_bytes, bool) and max_bytes <= 0:
            raise LogStoreError(f"JsonlLogStore.max_bytes must be > 0 when set; got {max_bytes}")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise LogStoreError(f"JsonlLogStore.max_files must be an int >= 1; got {max_files!r}")

        self._path = Path(path)
        self._log_class = log_class
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._fsync = bool(fsync)
        self._lock = threading.Lock()
        self._closed = False

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open the active segment in binary append mode for exact byte
        # accounting (rotation is size-based) and append atomicity.
        self._fh = open(self._path, "ab")  # noqa: SIM115 — handle lives with the store
        self._size = self._path.stat().st_size

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        """The active log-segment file path."""

        return self._path

    @property
    def log_class(self) -> LogClass:
        """The :class:`LogClass` this store accepts."""

        return self._log_class

    # ------------------------------------------------------------------ #
    # LogSink protocol
    # ------------------------------------------------------------------ #

    def write(self, record: LogRecord) -> None:
        """Append ``record`` durably to this store's segment.

        The record is fully validated (via
        :func:`~atp_logging.dispatcher.validate_log_record`) before it is
        written, so a caller that bypasses the dispatcher still cannot land
        a schema-invalid entry in the audit trail.

        Raises:
            LogClassError: ``record.log_class`` is not this store's class
                (the defensive separation guard), or a STRATEGY/SYSTEM
                cross-field invariant is violated.
            LogPayloadError: a field has an invalid type/range, an empty
                required string, or an out-of-taxonomy source/event_type.
            LogStoreError: the store is closed, or ``record`` is not a
                :class:`~atp_logging.LogRecord`.
        """

        if not isinstance(record, LogRecord):
            raise LogStoreError(
                f"JsonlLogStore.write expected a LogRecord; got {type(record).__name__}"
            )
        if record.log_class is not self._log_class:
            raise LogClassError(
                f"{self._log_class.value!r} store refuses a record with "
                f"log_class={record.log_class.value!r}; system and strategy logs "
                "are persisted to separate sinks"
            )
        # Full SDK schema + log-class validation — the SAME invariants the
        # dispatcher enforces — so a record written directly to the store
        # (bypassing the dispatcher) cannot land a malformed audit entry
        # (invalid timestamp, empty field, forbidden strategy_id, or an
        # out-of-taxonomy source/event_type).
        validate_log_record(record)

        line = (
            json.dumps(record.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")

        with self._lock:
            if self._closed:
                raise LogStoreError("JsonlLogStore is closed; cannot write")
            if (
                self._max_bytes is not None
                and self._size > 0
                and self._size + len(line) > self._max_bytes
            ):
                self._rotate_locked()
            self._fh.write(line)
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())
            self._size += len(line)

    # ------------------------------------------------------------------ #
    # Rotation
    # ------------------------------------------------------------------ #

    def _rotate_locked(self) -> None:
        """Rotate the active segment. Caller must hold ``self._lock``.

        ``active`` -> ``active.1`` -> ``active.2`` -> ... up to
        ``active.{max_files}``; the oldest (``active.{max_files}``) is
        dropped. The directory is fsync-ed so the renames survive a crash.
        """

        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())
        self._fh.close()

        oldest = self._segment_path(self._max_files)
        if oldest.exists():
            oldest.unlink()
        for index in range(self._max_files - 1, 0, -1):
            src = self._segment_path(index)
            if src.exists():
                src.rename(self._segment_path(index + 1))
        self._path.rename(self._segment_path(1))
        if self._fsync:
            self._fsync_dir(self._path.parent)

        self._fh = open(self._path, "ab")  # noqa: SIM115 — handle lives with the store
        self._size = 0

    def _segment_path(self, index: int) -> Path:
        return self._path.with_name(f"{self._path.name}.{index}")

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        # fsync the directory so a rename/create is durable. Not all
        # platforms permit opening a directory for fsync; tolerate that.
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:  # pragma: no cover — platform dependent
            return
        try:
            os.fsync(fd)
        except OSError:  # pragma: no cover — platform dependent
            pass
        finally:
            os.close(fd)

    def _all_segments_oldest_first(self) -> list[Path]:
        """The active + rotated segments in chronological (insertion) order."""

        segments: list[Path] = []
        for index in range(self._max_files, 0, -1):
            seg = self._segment_path(index)
            if seg.exists():
                segments.append(seg)
        if self._path.exists():
            segments.append(self._path)
        return segments

    # ------------------------------------------------------------------ #
    # Read surface
    # ------------------------------------------------------------------ #

    def read(self, **filters: object) -> list[LogRecord]:
        """Read this store's persisted records, newest segment last.

        Holds the store lock for a consistent snapshot (no torn read against
        a concurrent rotation). ``filters`` are forwarded to :func:`query`.
        """

        with self._lock:
            segments = self._all_segments_oldest_first()
            records: list[LogRecord] = []
            for seg in segments:
                # Only the active segment can carry a torn trailing fragment.
                records.extend(_read_segment(seg, tolerate_torn_tail=(seg == self._path)))
        return query(records, **filters)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Flush, fsync, and close the active segment handle (idempotent)."""

        with self._lock:
            if self._closed:
                return
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())
            self._fh.close()
            self._closed = True

    def __enter__(self) -> JsonlLogStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------- #
# Module-level read + query helpers
# ---------------------------------------------------------------------- #


def _record_from_mapping(payload: object, *, where: str) -> LogRecord:
    """Reconstruct a :class:`LogRecord` from a decoded JSON mapping.

    Raises :class:`LogStoreCorruptionError` if the mapping is the wrong
    shape, carries an out-of-domain enum value, or violates the log-record
    invariants (e.g. a SYSTEM line carrying a ``strategy_id``, a negative
    ``timestamp_ns``, an empty field, or an out-of-taxonomy source/event).
    The read path fails closed on a tampered or stale-format line rather
    than serving an audit record the write path would have refused.
    """

    if not isinstance(payload, dict):
        raise LogStoreCorruptionError(f"{where}: stored line is not a JSON object")
    try:
        severity = Severity(payload["severity"])
        source = Source(payload["source"])
        log_class = LogClass(payload["log_class"])
        record = LogRecord(
            timestamp_ns=payload["timestamp_ns"],
            severity=severity,
            source=source,
            event_type=payload["event_type"],
            message=payload["message"],
            correlation_id=payload["correlation_id"],
            log_class=log_class,
            strategy_id=payload.get("strategy_id"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise LogStoreCorruptionError(
            f"{where}: stored line is not a valid LogRecord: {exc}"
        ) from exc
    # Re-validate the reconstructed record against the SAME invariants the
    # write path enforces, so the read surface (the GET /api/v1/logs seam)
    # never serves a record that violates the SRS-LOG-001 separation/audit
    # guarantees — it fails closed as corruption instead.
    try:
        validate_log_record(record)
    except LogRecordError as exc:
        raise LogStoreCorruptionError(
            f"{where}: stored record violates log invariants: {exc}"
        ) from exc
    return record


def _read_segment(path: Path, *, tolerate_torn_tail: bool) -> list[LogRecord]:
    """Read one segment file into records.

    Every newline-terminated line must decode into a valid record (else
    :class:`LogStoreCorruptionError`). When ``tolerate_torn_tail`` is set,
    an unterminated trailing fragment (the signature of a crash mid-write)
    is dropped rather than parsed.
    """

    if not path.exists():
        return []
    raw = path.read_bytes()
    if not raw:
        return []

    # Split on bytes at the LAST newline so a torn trailing fragment (which
    # may even cut a multi-byte UTF-8 char) is never fed to the strict
    # decoder — decoding it would otherwise raise and lose every good record
    # before it. Everything up to and including the last newline is complete
    # lines; everything after is the unterminated tail.
    last_newline = raw.rfind(b"\n")
    complete_bytes = b"" if last_newline == -1 else raw[: last_newline + 1]
    tail_bytes = raw if last_newline == -1 else raw[last_newline + 1 :]

    if tail_bytes:
        # A non-empty tail means the buffer did not end with a newline: a
        # torn write. Tolerated only on the active segment.
        if not tolerate_torn_tail:
            raise LogStoreCorruptionError(
                f"{path}: rotated segment ended without a terminating newline"
            )
        # else: drop the torn trailing fragment (crash mid-write).

    records: list[LogRecord] = []
    if complete_bytes:
        try:
            text = complete_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LogStoreCorruptionError(
                f"{path}: a complete line is not valid UTF-8: {exc}"
            ) from exc
        # ``complete_bytes`` ends with "\n", so split yields a trailing "".
        for lineno, line in enumerate(text.split("\n"), start=1):
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LogStoreCorruptionError(
                    f"{path}:{lineno}: complete line is not valid JSON: {exc}"
                ) from exc
            records.append(_record_from_mapping(payload, where=f"{path}:{lineno}"))
    return records


def read_records(
    path: str | os.PathLike[str],
    *,
    max_files: int = 5,
    **filters: object,
) -> list[LogRecord]:
    """Read records persisted at ``path`` (active + rotated segments).

    A lock-free reader for callers that do not hold a :class:`JsonlLogStore`
    instance (e.g. the live ``GET /api/v1/logs`` handler reading a store
    another process owns). ``max_files`` must match the writer's rotation
    depth so every rotated segment is discovered. ``filters`` are forwarded
    to :func:`query`. A concurrent rotation may briefly hide a record being
    moved between segments; hold the writer's :meth:`JsonlLogStore.read` for
    a fully consistent snapshot.
    """

    base = Path(path)
    segments: list[Path] = []
    for index in range(max_files, 0, -1):
        seg = base.with_name(f"{base.name}.{index}")
        if seg.exists():
            segments.append(seg)
    records: list[LogRecord] = []
    for seg in segments:
        records.extend(_read_segment(seg, tolerate_torn_tail=False))
    records.extend(_read_segment(base, tolerate_torn_tail=True))
    return query(records, **filters)  # type: ignore[arg-type]


def query(
    records: Iterable[LogRecord],
    *,
    log_class: LogClass | None = None,
    min_severity: Severity | None = None,
    source: Source | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[LogRecord]:
    """Filter ``records`` by the ``GET /api/v1/logs`` query dimensions.

    The filters mirror the REST parameters pinned in
    ``python/atp_api/openapi.json``: ``log_class`` (exact), ``severity``
    (minimum severity, inclusive, per the SyRS SYS-61 order),
    ``source``/``event_type``/``correlation_id`` (exact), and the
    ``start_time``/``end_time`` window (here as ``start_ns``/``end_ns``
    inclusive bounds on ``timestamp_ns``). ``limit`` caps the result;
    ``newest_first`` reverses the natural insertion order before the cap so
    a limited query returns the most recent records.

    Insertion order is preserved (the audit order in which records were
    written); ``timestamp_ns`` is caller-supplied and not assumed monotone,
    so the result is not re-sorted by timestamp.
    """

    if log_class is not None and not isinstance(log_class, LogClass):
        raise LogStoreError(f"query log_class must be a LogClass or None; got {log_class!r}")
    if min_severity is not None and not isinstance(min_severity, Severity):
        raise LogStoreError(f"query min_severity must be a Severity or None; got {min_severity!r}")
    if source is not None and not isinstance(source, Source):
        raise LogStoreError(f"query source must be a Source or None; got {source!r}")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise LogStoreError(f"query limit must be a non-negative int or None; got {limit!r}")

    min_rank = None if min_severity is None else _severity_rank(min_severity)

    def keep(record: LogRecord) -> bool:
        if log_class is not None and record.log_class is not log_class:
            return False
        if min_rank is not None and _severity_rank(record.severity) < min_rank:
            return False
        if source is not None and record.source is not source:
            return False
        if event_type is not None and record.event_type != event_type:
            return False
        if correlation_id is not None and record.correlation_id != correlation_id:
            return False
        if start_ns is not None and record.timestamp_ns < start_ns:
            return False
        if end_ns is not None and record.timestamp_ns > end_ns:
            return False
        return True

    matched: Iterator[LogRecord] = (r for r in records if keep(r))
    result = list(matched)
    if newest_first:
        result.reverse()
    if limit is not None:
        result = result[:limit]
    return result


def _validate_segment_filename(name: str, label: str) -> None:
    """Reject anything that is not a bare filename within the log directory.

    A name with a path separator, an absolute path, or a ``.``/``..`` segment
    could alias the other class's file (``./system.jsonl`` ≡ ``system.jsonl``)
    or escape ``directory`` entirely — both break the SRS-LOG-001 separation.
    """

    if not isinstance(name, str) or not name:
        raise LogStoreError(f"{label} must be a non-empty filename; got {name!r}")
    if (
        os.path.isabs(name)
        or os.sep in name
        or (os.altsep is not None and os.altsep in name)
        or name in (os.curdir, os.pardir)
    ):
        raise LogStoreError(
            f"{label} must be a bare filename within the log directory "
            f"(no path separators, not absolute, not '.'/'..'); got {name!r}"
        )


def build_separated_log_dispatcher(
    directory: str | os.PathLike[str],
    *,
    max_bytes: int | None = None,
    max_files: int = 5,
    fsync: bool = True,
    system_filename: str = "system.jsonl",
    strategy_filename: str = "strategy.jsonl",
) -> tuple[RoutedLogDispatcher, JsonlLogStore, JsonlLogStore]:
    """Wire a SYSTEM store and a *separate* STRATEGY store to a dispatcher.

    This is the SRS-LOG-001 boot path: it materialises the two persistent
    sinks the AC requires ("separate persistent system logs from user
    strategy logs") under ``directory`` and registers them on a
    :class:`~atp_logging.RoutedLogDispatcher` so a single ``dispatch`` call
    routes each record to the correct physical file.

    Returns the ``(dispatcher, system_store, strategy_store)`` triple; the
    caller owns closing the two stores (or using them as context managers).

    The two sinks MUST resolve to different physical files — that is the
    SRS-LOG-001 separation guarantee. Each filename must therefore be a bare
    name within ``directory`` (no path separators, not absolute, not ``.``/
    ``..``), and after both segments are opened their inode/device identity
    is cross-checked with :func:`os.path.samefile` so an alias the string
    comparison misses (``./system.jsonl``, a case-insensitive collision, a
    symlink) cannot silently funnel both classes into one file.
    """

    _validate_segment_filename(system_filename, "system_filename")
    _validate_segment_filename(strategy_filename, "strategy_filename")
    if system_filename == strategy_filename:
        raise LogStoreError(
            f"system and strategy sinks must use different files; both were {system_filename!r}"
        )
    base = Path(directory)
    system_store = JsonlLogStore(
        base / system_filename,
        log_class=LogClass.SYSTEM,
        max_bytes=max_bytes,
        max_files=max_files,
        fsync=fsync,
    )
    try:
        strategy_store = JsonlLogStore(
            base / strategy_filename,
            log_class=LogClass.STRATEGY,
            max_bytes=max_bytes,
            max_files=max_files,
            fsync=fsync,
        )
    except BaseException:
        system_store.close()
        raise
    # Airtight physical-separation check: both files now exist, so an alias
    # the lexical guard missed shows up as the same inode/device.
    if os.path.samefile(system_store.path, strategy_store.path):
        system_store.close()
        strategy_store.close()
        raise LogStoreError(
            "system and strategy sinks resolved to the same physical file "
            f"({system_store.path} ≡ {strategy_store.path}); they must be separate"
        )
    dispatcher = RoutedLogDispatcher()
    dispatcher.register_sink(LogClass.SYSTEM, system_store)
    dispatcher.register_sink(LogClass.STRATEGY, strategy_store)
    return dispatcher, system_store, strategy_store
