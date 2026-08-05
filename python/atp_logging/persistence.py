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
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .dispatcher import RoutedLogDispatcher, validate_log_record
from .errors import LogClassError, LogRecordError
from .records import LogClass, LogRecord, Severity, Source
from .redaction import DEFAULT_REDACTOR, SecretRedactor

__all__ = [
    "JsonlLogStore",
    "LogStoreClassMismatchError",
    "LogStoreCorruptionError",
    "LogStoreError",
    "LogStoreMissingError",
    "LogStoreRotationError",
    "MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION",
    "RecordPosition",
    "SCHEMA_VERSION_KEY",
    "SEGMENT_SCHEMA_VERSION",
    "build_separated_log_dispatcher",
    "iter_records",
    "iter_records_with_positions",
    "query",
    "record_at",
    "read_records",
    "read_records_bounded",
    "read_tail",
]

#: The segment line-envelope layout this build WRITES (SRS-DATA-015 / SyRS
#: SYS-66). **Version history:** v1 = one ``LogRecord.as_dict()`` object per
#: line, plus this version key.
#:
#: The version travels per LINE because a segment is an append-only file that
#: rotates: a file-level header would have to be re-established on every
#: rotation and could not describe lines appended by a build newer than the
#: header. A self-describing line survives rotation, concatenation, and a torn
#: tail.
SEGMENT_SCHEMA_VERSION = 1

#: The oldest line envelope this build still READS. A line with no
#: ``schema_version`` key predates SRS-DATA-015 and is read at this floor, in
#: place — an existing audit trail is never rewritten to remain queryable
#: (rewriting an audit log to read it would destroy the very property it
#: exists to provide).
MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION = 1

#: The key the envelope version travels under.
SCHEMA_VERSION_KEY = "schema_version"


@dataclass(frozen=True, slots=True)
class RecordPosition:
    """Where a record physically lives: the identity a cursor can trust.

    A consumer that resumes across reads (the ``LOGS`` publisher) cannot use the
    record's VALUE as its bookmark: two audit records may legitimately be
    byte-identical — a retried operation writing the same message with the same
    correlation id in the same nanosecond — and mistaking the second for the
    first silently drops it. A position is unique even when the content is not.

    ``device``/``inode`` identify the segment FILE rather than its name, so a
    rotation (which renames ``system.jsonl`` to ``system.jsonl.1`` and creates a
    fresh active file) is seen for what it is: the same bytes under a new name,
    not a new record. ``end_offset`` is the byte offset just past the record's
    line, so positions within a segment compare in write order.
    """

    device: int
    inode: int
    end_offset: int

    @property
    def token(self) -> str:
        """An OPAQUE, stable id for this record, for callers that must dedupe.

        The format is deliberately not contractual — it is an identity to compare,
        never a location to parse. What it guarantees: two DISTINCT persisted
        records always get different tokens (even when byte-identical and written
        in the same nanosecond), and one record keeps its token across a rotation
        (a rename preserves the inode).
        """

        return f"{self.device}-{self.inode}-{self.end_offset}"


class LogStoreError(LogRecordError):
    """Base class for persistent-log-store failures.

    Subclasses :class:`~atp_logging.LogRecordError` so a caller can catch
    the whole logging family (dispatch + persistence) with one
    ``except LogRecordError`` clause.
    """


class LogStoreMissingError(LogStoreError):
    """Raised when a store's ACTIVE segment is not there at read time.

    Distinct from :class:`LogStoreCorruptionError` (present but unreadable) and
    from an empty result (present, nothing matched): a configured trail that has
    gone is an operator problem in its own right. Raising it from the READ makes
    the answer atomic — an ``exists()`` pre-check can be overtaken by a deletion
    or a rotation between the check and the open, and would then read as a clean
    empty scan.
    """


class LogStoreClassMismatchError(LogStoreError):
    """Raised when a store physically contains a record of the OTHER class.

    The store refuses to WRITE a wrong-class record, so a file holding one means
    the separation was broken by something outside this API — legacy data, a
    hand-edited file, a bad recovery. SRS-LOG-001 exists to keep the two trails
    apart, so the read side treats it the way it treats corruption: fail closed,
    loudly. Silently filtering it out would hide a broken invariant, and serving
    it would carry the breakage onward.
    """


class LogStoreRotationError(LogStoreError):
    """Raised when a read could not get a rotation-consistent view of the trail.

    A scan enumerates segment paths and then opens them; a rotation landing in
    that window can move a segment past a path already visited, so the scan
    misses records that ARE in the store. Returning that page would be a
    successful-looking answer that is quietly short — the false negative an
    audit query must never give. Readers retry, and raise this when the store
    rotates through every attempt.
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
        redactor: SecretRedactor | None = None,
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
        # SRS-SEC-001: the store is the persistence boundary, so it must NEVER
        # default to zero redaction. When no value-aware redactor is injected we
        # fall back to the always-on pattern-based DEFAULT_REDACTOR — production
        # boot injects SecretRedactor(atp_config.secret_values(env)) for full
        # IB/SMTP/SMS value coverage.
        self._redactor = redactor if redactor is not None else DEFAULT_REDACTOR
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

        # SRS-SEC-001: redact credentials at the persistence boundary — the
        # LAST line of defence. A record written directly to the store
        # (bypassing the dispatcher) is scrubbed here, so an IB/SMTP/SMS secret
        # can never land in the on-disk audit trail in plaintext. ``_redactor``
        # is always set (DEFAULT_REDACTOR at minimum), so there is no
        # zero-redaction path. Redaction preserves the schema (message /
        # correlation_id stay non-empty).
        record = self._redactor.redact_record(record)

        # SRS-DATA-015: the persisted ENVELOPE carries the segment's schema
        # version. It is added here, by the format owner, rather than inside
        # ``LogRecord.as_dict()`` — that dict is the SRS-LOG-001 SDK record
        # schema shared with the API/UI sinks, and a storage concern must not
        # widen it. ``as_dict()`` therefore stays byte-identical.
        line = (
            json.dumps(
                {**record.as_dict(), SCHEMA_VERSION_KEY: SEGMENT_SCHEMA_VERSION},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
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
        dropped. The directory is fsync-ed TWICE: once so the renames survive a
        crash, and once after the new active segment is created so its directory
        entry does too — fsync-ing a file makes its contents durable, not its
        name.
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
        if self._fsync:
            # The NEW active segment needs its own directory fsync. fsync-ing the
            # file (which the caller's write does next) makes its CONTENTS
            # durable but not its directory entry, so a crash could leave the
            # first post-rotation record — a kill-switch activation, an IB
            # disconnect — written to a file whose name never existed. The
            # rename above is durable by the fsync before this; the create is
            # only durable because of this one.
            self._fsync_dir(self._path.parent)
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

        Fails closed with :class:`LogStoreClassMismatchError` if any persisted
        record belongs to the other class. :meth:`write` enforces separation on
        the way in, but a file that was recovered from backup, restored onto the
        wrong path, or edited by hand can still hold a foreign record — and this
        object is the one that CLAIMS a class, so it is the boundary that owes
        the guarantee. Without the check the same contamination has two
        different wrong outcomes depending on how the caller happens to filter:
        unfiltered, the foreign record is returned as if it belonged here;
        filtered on anything else, it is silently dropped and the broken
        separation leaves no trace at all. Both are worse than an error.
        """

        with self._lock:
            segments = self._all_segments_oldest_first()
            records: list[LogRecord] = []
            for seg in segments:
                # Only the active segment can carry a torn trailing fragment.
                for record in _read_segment(seg, tolerate_torn_tail=(seg == self._path)):
                    # BEFORE `query`, deliberately: filtering first would decide
                    # whether the evidence survives based on an unrelated filter.
                    if record.log_class is not self._log_class:
                        raise LogStoreClassMismatchError(
                            f"{seg}: holds a {record.log_class.value} record but is the "
                            f"{self._log_class.value} store (SRS-LOG-001 separation is "
                            f"broken for this trail)"
                        )
                    records.append(record)
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


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``json`` ``object_pairs_hook`` that refuses a duplicated key.

    Python's default is last-value-wins, which silently resolves an ambiguity
    that must not be resolved: a record declaring both ``"schema_version":99``
    and ``"schema_version":1`` would be read as v1 and served, when what it
    actually says is that this build cannot trust it. Mirrors the Rust
    ``atp_types::json_scan`` gate, so the two languages agree about which
    persisted records are readable.
    """

    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key {key!r}")
        seen[key] = value
    return seen


def _check_segment_schema_version(payload: Mapping[str, object], *, where: str) -> None:
    """Fail closed unless this build can read ``payload``'s declared envelope.

    Absent key → a pre-SRS-DATA-015 line, read at
    :data:`MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION` (the AC's "older schema
    versions remain queryable without bulk migration"). Present → must be a
    real ``int`` (``bool`` is an ``int`` subclass and is rejected) inside the
    supported range.

    A line from a NEWER build is corruption *for this reader*: its fields may
    mean something else, and the read surface (``GET /api/v1/logs``, the
    SYS-44b timeout lookup) must never serve an audit record it cannot
    actually parse.
    """

    if SCHEMA_VERSION_KEY not in payload:
        return
    version = payload[SCHEMA_VERSION_KEY]
    if isinstance(version, bool) or not isinstance(version, int):
        raise LogStoreCorruptionError(
            f"{where}: stored line declares a non-integer {SCHEMA_VERSION_KEY} ({version!r})"
        )
    if not (MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION <= version <= SEGMENT_SCHEMA_VERSION):
        raise LogStoreCorruptionError(
            f"{where}: stored line declares {SCHEMA_VERSION_KEY} {version}, outside the "
            f"supported range [{MIN_SUPPORTED_SEGMENT_SCHEMA_VERSION}, "
            f"{SEGMENT_SCHEMA_VERSION}] — refusing to serve a record this build cannot parse"
        )


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
    _check_segment_schema_version(payload, where=where)
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
                payload = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LogStoreCorruptionError(
                    f"{path}:{lineno}: complete line is not valid JSON: {exc}"
                ) from exc
            records.append(_record_from_mapping(payload, where=f"{path}:{lineno}"))
    return records


def read_records(
    path: str | os.PathLike[str],
    *,
    max_files: int = 5,
    expect_class: LogClass | None = None,
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

    ``expect_class`` is how a caller that KNOWS which trail it opened gets the
    same fail-closed separation :meth:`JsonlLogStore.read` gives — checked
    before any filter, so a filter cannot decide whether the contamination is
    visible. It is optional because this function is also the generic reader
    for a path whose class is not known up front (a recovery tool, a test
    fixture); every caller that does know its class should pass it.
    """

    base = Path(path)
    segments: list[Path] = []
    for index in range(max_files, 0, -1):
        seg = base.with_name(f"{base.name}.{index}")
        if seg.exists():
            segments.append(seg)
    records: list[LogRecord] = []
    for seg in (*segments, base):
        for record in _read_segment(seg, tolerate_torn_tail=(seg == base)):
            if expect_class is not None and record.log_class is not expect_class:
                raise LogStoreClassMismatchError(
                    f"{seg}: holds a {record.log_class.value} record but is the "
                    f"{expect_class.value} store (SRS-LOG-001 separation is broken "
                    f"for this trail)"
                )
            records.append(record)
    return query(records, **filters)  # type: ignore[arg-type]


def _filter_predicate(
    *,
    log_class: LogClass | None = None,
    min_severity: Severity | None = None,
    source: Source | None = None,
    event_type: str | None = None,
    correlation_id: str | None = None,
    start_ns: int | None = None,
    end_ns: int | None = None,
) -> Callable[[LogRecord], bool]:
    """The single ``GET /api/v1/logs`` filter, shared by every read path.

    :func:`query` (in-memory) and :func:`read_records_bounded` (streaming) must
    answer identically — one predicate, so a filter fix cannot land on only one
    of the two paths.
    """

    if log_class is not None and not isinstance(log_class, LogClass):
        raise LogStoreError(f"query log_class must be a LogClass or None; got {log_class!r}")
    if min_severity is not None and not isinstance(min_severity, Severity):
        raise LogStoreError(f"query min_severity must be a Severity or None; got {min_severity!r}")
    if source is not None and not isinstance(source, Source):
        raise LogStoreError(f"query source must be a Source or None; got {source!r}")

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

    return keep


#: How many times a read re-tries a store that rotated mid-scan before failing
#: closed. Rotation is a rare, bounded event; a store rotating through every
#: attempt is a real condition, not something to retry forever.
ROTATION_READ_ATTEMPTS = 3


def active_identity(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    """The active segment's ``(device, inode)``, or ``None`` when it is absent.

    Identity rather than name: rotation RENAMES the file, so a name-based check
    would see no change at exactly the moment everything moved.
    """

    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino)


def _segment_paths(base: Path, max_files: int) -> list[Path]:
    """Rotated segments (oldest first) followed by the active segment."""

    segments: list[Path] = []
    for index in range(max_files, 0, -1):
        seg = base.with_name(f"{base.name}.{index}")
        if seg.exists():
            segments.append(seg)
    segments.append(base)
    return segments


def _iter_segment_records(
    path: Path, *, tolerate_torn_tail: bool, required: bool = False, start_offset: int = 0
) -> Iterator[tuple[RecordPosition, LogRecord]]:
    """Stream one segment's records without materialising the file.

    Same rules as :func:`_read_segment`, line by line: every newline-terminated
    line must decode into a valid record, and only an UNTERMINATED trailing
    fragment (a crash mid-write) may be dropped, and only on the active segment.

    Each record is paired with its :class:`RecordPosition` — the physical
    identity of the line that produced it.

    ``start_offset`` resumes at a byte offset a previous read stopped at. It must
    land on a record boundary — the caller gets it from a ``RecordPosition``,
    which is by construction just past a terminator.
    """

    # Open FIRST and let absence surface from the open itself. An `exists()`
    # test here would be a TOCTOU: a deletion or rotation landing between the
    # check and the open turns a vanished trail into a clean empty scan.
    try:
        handle = path.open("rb")
    except FileNotFoundError as error:
        if required:
            raise LogStoreMissingError(f"{path}: the active log segment is not there") from error
        return  # a rotated segment that is simply absent is not an error
    with handle:
        stat = os.fstat(handle.fileno())
        device, inode = stat.st_dev, stat.st_ino
        lineno = 0
        if start_offset:
            handle.seek(start_offset)
        while True:
            raw_line = handle.readline()
            if not raw_line:
                return
            # readline() leaves the handle exactly after this line, so tell() is
            # the line's end offset — a stable physical identity for the record.
            end_offset = handle.tell()
            lineno += 1
            # A resumed read starts mid-file, so a line NUMBER would be relative
            # to the resume point and point an operator at the wrong line. Name
            # the byte offset instead, which is absolute either way.
            where = f"{path}@{end_offset}" if start_offset else f"{path}:{lineno}"
            if not raw_line.endswith(b"\n"):
                # No terminator: this is the last line in the file and it was
                # torn mid-write. Tolerated only on the active segment; never
                # parsed (it may even cut a multi-byte UTF-8 character).
                if tolerate_torn_tail:
                    return
                raise LogStoreCorruptionError(
                    f"{path}: rotated segment ended without a terminating newline"
                )
            line = raw_line[:-1]
            if not line:
                continue
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LogStoreCorruptionError(
                    f"{where}: a complete line is not valid UTF-8: {exc}"
                ) from exc
            try:
                payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LogStoreCorruptionError(
                    f"{where}: complete line is not valid JSON: {exc}"
                ) from exc
            yield (
                RecordPosition(device=device, inode=inode, end_offset=end_offset),
                _record_from_mapping(payload, where=where),
            )


def record_at(
    path: str | os.PathLike[str],
    position: RecordPosition,
    *,
    max_files: int = 5,
    expect_class: LogClass | None = None,
) -> LogRecord | None:
    """The record currently occupying ``position``, or ``None`` if it is gone.

    A poller that remembers a position needs to know, in O(1), whether that slot
    still holds what it left there — so it can resume from it instead of walking
    the trail from the beginning every time. ``None`` means the slot is no longer
    resumable: its segment rotated away, the file was replaced, or it was
    truncated back past that offset.

    A slot that exists but holds UNREADABLE bytes raises rather than returning
    ``None``: "the trail changed" and "the trail is corrupt" are different facts
    and a resume decision must not silently turn the second into the first.

    Identity here is physical AND content-based on purpose: the caller compares
    the returned record with the one it published. An evicted segment's inode can
    be reused and a later record can land at the same offset, so the position
    alone can collide; requiring the record too means a false match needs the
    same physical slot and identical content at once.
    """

    if position.end_offset <= 0:
        return None
    base = Path(path)
    for segment in _segment_paths(base, max_files):
        try:
            handle = segment.open("rb")
        except FileNotFoundError:
            continue
        with handle:
            stat = os.fstat(handle.fileno())
            if (stat.st_dev, stat.st_ino) != (position.device, position.inode):
                continue
            if stat.st_size < position.end_offset:
                # The file this position named is still here but no longer
                # reaches that offset: truncated or rewritten. Not resumable.
                return None
            line = _line_ending_at(handle, position.end_offset)
            if line is None:
                return None
            where = f"{segment}@{position.end_offset}"
            try:
                payload = json.loads(line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise LogStoreCorruptionError(
                    f"{where}: complete line is unreadable: {exc}"
                ) from exc
            record = _record_from_mapping(payload, where=where)
            if expect_class is not None and record.log_class is not expect_class:
                raise LogStoreClassMismatchError(
                    f"{segment}: holds a {record.log_class.value} record but is the "
                    f"{expect_class.value} store (SRS-LOG-001 separation is broken "
                    f"for this trail)"
                )
            return record
    return None


#: Windows tried, smallest first, when reading one line backwards. A log line is
#: usually a few hundred bytes, so the first window almost always suffices; the
#: larger ones only exist so a legitimately long message still resumes. Starting
#: at the largest would make every resume pay 64KiB to read ~200 bytes — a fixed
#: cost, but a fixed cost on a one-second ticker is still a cost.
_LINE_LOOKBACK_WINDOWS = (1024, 16 * 1024, 64 * 1024)


def _line_ending_at(handle: IO[bytes], end_offset: int) -> bytes | None:
    """The raw line (no terminator) whose newline sits at ``end_offset``.

    Reads backwards from ``end_offset`` in growing windows rather than scanning
    from the start of the file — the whole point of resuming is not to touch the
    history again. Returns ``None`` when the offset is not a record boundary, or
    when the line is longer than the largest window: falling back to the full
    scan is slow but correct, and guessing would not be.
    """

    for window in _LINE_LOOKBACK_WINDOWS:
        start = max(0, end_offset - window)
        handle.seek(start)
        block = handle.read(end_offset - start)
        if not block.endswith(b"\n"):
            # Not a record boundary: whatever wrote here is not what we left.
            return None
        body = block[:-1]
        cut = body.rfind(b"\n")
        if cut != -1:
            return body[cut + 1 :]
        if start == 0:
            # Reached the head of the file: the whole block IS the line.
            return body
    return None


def iter_records_with_positions(
    path: str | os.PathLike[str],
    *,
    max_files: int = 5,
    require_active: bool = False,
    expect_class: LogClass | None = None,
    resume_after: RecordPosition | None = None,
) -> Iterator[tuple[RecordPosition, LogRecord]]:
    """Stream every persisted record with its physical :class:`RecordPosition`.

    For a consumer that must remember where it stopped ACROSS reads — the
    ``LOGS`` WebSocket publisher — and cannot use the record's value to do it:
    two audit records may legitimately be byte-identical (a retried operation
    writing the same message in the same nanosecond), and treating the second as
    "the one I already sent" would silently drop it. A position is unique even
    when the content is not.

    ``resume_after`` starts the stream just past a position instead of at the
    beginning of the trail. Without it a poller pays for the whole history on
    every tick, however little was appended — and an audit trail is append-only
    and unbounded by default, so that cost only ever grows. Segments older than
    the resume point are never opened. Verify the position first with
    :func:`record_at`; if it is not resumable, do not pass it — a resume that
    silently fell back to a full scan would republish the history.

    Raises:
        LogStoreRotationError: ``resume_after`` names a segment that is no longer
            in the trail. Refusing is the safe answer: skipping to the newest
            segment would drop the records between, and starting from the oldest
            would re-publish what the caller already delivered.
    """

    base = Path(path)
    segments = _segment_paths(base, max_files)
    start_index, start_offset = 0, 0
    if resume_after is not None:
        start_index = -1
        for index, segment in enumerate(segments):
            try:
                stat = segment.stat()
            except OSError:
                continue
            if (stat.st_dev, stat.st_ino) == (resume_after.device, resume_after.inode):
                start_index, start_offset = index, resume_after.end_offset
                break
        if start_index == -1:
            raise LogStoreRotationError(
                f"{base}: the segment holding the resume position "
                f"{resume_after.token} is no longer in the trail"
            )

    for offset_index, segment in enumerate(segments[start_index:]):
        is_active = segment == base
        for position, record in _iter_segment_records(
            segment,
            tolerate_torn_tail=is_active,
            required=require_active and is_active,
            start_offset=start_offset if offset_index == 0 else 0,
        ):
            # Checked BEFORE any caller filter: filtering first would drop the
            # evidence that this store's separation is broken.
            if expect_class is not None and record.log_class is not expect_class:
                raise LogStoreClassMismatchError(
                    f"{segment}: holds a {record.log_class.value} record but is the "
                    f"{expect_class.value} store (SRS-LOG-001 separation is broken "
                    f"for this trail)"
                )
            yield position, record


def iter_records(
    path: str | os.PathLike[str],
    *,
    max_files: int = 5,
    require_active: bool = False,
) -> Iterator[LogRecord]:
    """Stream every persisted record at ``path`` in write order.

    The constant-memory counterpart to :func:`read_records`: rotated segments
    oldest-first, then the active segment, one record at a time. An audit trail
    is append-only and unbounded by default (rotation is opt-in), so an operator
    surface that must not be able to OOM the runtime reads through this rather
    than materialising the whole trail.

    Every complete line is still parsed and validated, so corruption ANYWHERE in
    the trail still fails the read closed — the property a tail-only reader would
    quietly give up.
    """

    for _position, record in iter_records_with_positions(
        path, max_files=max_files, require_active=require_active
    ):
        yield record


#: Block size for the backwards tail reader. Big enough that a page of log
#: lines usually comes out of one or two reads, small enough that the reader's
#: memory stays trivial next to the trail it is scanning.
_TAIL_BLOCK_BYTES = 64 * 1024


def _iter_segment_lines_reverse(
    path: Path, *, tolerate_torn_tail: bool, required: bool = False
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(end_offset, line_bytes)`` from the END of a segment backwards.

    Reads fixed blocks from the tail so the work is proportional to what the
    caller CONSUMES, not to the file. The caller stops when its page is full and
    the rest of the segment is never touched.

    ``end_offset`` is the byte offset just past the line's terminator, matching
    the forward reader's :class:`RecordPosition` exactly — the two readers must
    name the same record the same way.
    """

    # Open FIRST and let absence surface from the open, exactly as the forward
    # reader does. An `exists()` pre-check up in read_tail cannot cover the
    # window between the check and this open — a deletion landing there would
    # otherwise turn a vanished active trail into a clean, empty page.
    try:
        handle = path.open("rb")
    except FileNotFoundError as error:
        if required:
            raise LogStoreMissingError(f"{path}: the active log segment is not there") from error
        return  # a rotated segment that is simply absent is not an error
    with handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return

        # A file not ending in a newline has a torn trailing fragment: it is
        # dropped (crash mid-write) on the active segment and is corruption on a
        # rotated one — the same rule the forward reader applies.
        handle.seek(size - 1)
        ends_with_newline = handle.read(1) == b"\n"
        if not ends_with_newline and not tolerate_torn_tail:
            raise LogStoreCorruptionError(
                f"{path}: rotated segment ended without a terminating newline"
            )

        cursor = size
        pending = b""  # bytes BEFORE the current block boundary, not yet a line
        pending_end = size  # absolute offset just past `pending`
        while cursor > 0:
            block = min(_TAIL_BLOCK_BYTES, cursor)
            cursor -= block
            handle.seek(cursor)
            buffer = handle.read(block) + pending
            buffer_start = cursor
            # Everything after the first newline in `buffer` is whole lines.
            first_newline = buffer.find(b"\n")
            if first_newline == -1 and cursor > 0:
                # No line boundary in this block: carry it all and read more.
                pending = buffer
                pending_end = buffer_start + len(buffer)
                continue
            head_end = 0 if cursor == 0 else first_newline + 1
            head = buffer[:head_end]
            body = buffer[head_end:]
            # Walk `body`'s lines from the END so the newest come out first.
            end = buffer_start + len(buffer)
            while body:
                terminator = body.rfind(b"\n")
                if terminator == len(body) - 1:
                    # `body` ends with a terminator: this is a complete line.
                    line_end = buffer_start + head_end + len(body)
                    start = body.rfind(b"\n", 0, terminator)
                    line = body[start + 1 : terminator]
                    body = body[: start + 1]
                    if line:
                        yield line_end, line
                    end = line_end
                    continue
                # A trailing fragment with no terminator: the torn tail.
                fragment_end = buffer_start + head_end + len(body)
                if fragment_end != size or not tolerate_torn_tail:
                    # Only the very end of the file may be torn; anything else
                    # is a boundary artefact and must not be dropped silently.
                    if fragment_end != size:
                        raise LogStoreCorruptionError(
                            f"{path}: unterminated line at offset {fragment_end}"
                        )
                body = body[: body.rfind(b"\n") + 1] if b"\n" in body else b""
                del end
                end = fragment_end
            pending = head
            pending_end = buffer_start + head_end
        del pending_end


def read_tail(
    path: str | os.PathLike[str],
    *,
    limit: int,
    max_files: int = 5,
    require_active: bool = False,
    expect_class: LogClass | None = None,
    **filters: object,
) -> list[tuple[RecordPosition, LogRecord]]:
    """Return the newest ``limit`` matching records, reading only the tail.

    The counterpart to :func:`read_records_bounded` for a surface that POLLS.
    ``read_records_bounded`` walks the whole trail to report an exact total; on a
    four-second dashboard refresh against an append-only store that becomes
    O(total history) of disk I/O and JSON parsing forever. This reads backwards
    from the newest segment and stops as soon as the page is full, so the cost is
    proportional to the PAGE.

    The trade is explicit and belongs to the caller: there is no exact match
    total (nothing counted what was never read), and corruption is detected only
    within the window actually scanned. A surface that needs either — the
    operator's own ``GET /api/v1/logs`` query — uses the full read instead.

    Raises:
        LogStoreMissingError: If ``require_active`` and the active segment is gone.
        LogStoreCorruptionError: On an unreadable line inside the scanned window.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LogStoreError(f"read_tail limit must be a positive int; got {limit!r}")
    base = Path(path)
    keep = _filter_predicate(**filters)  # type: ignore[arg-type]

    for _attempt in range(ROTATION_READ_ATTEMPTS):
        before = active_identity(base)
        page = _read_tail_once(
            base,
            limit=limit,
            max_files=max_files,
            keep=keep,
            require_active=require_active,
            expect_class=expect_class,
        )
        if active_identity(base) == before:
            return page
    raise LogStoreRotationError(
        f"{base}: the store rotated during every read attempt "
        f"({ROTATION_READ_ATTEMPTS}); refusing to return a possibly-short page"
    )


def _read_tail_once(
    base: Path,
    *,
    limit: int,
    max_files: int,
    keep: Callable[[LogRecord], bool],
    require_active: bool = False,
    expect_class: LogClass | None = None,
) -> list[tuple[RecordPosition, LogRecord]]:
    newest_first: list[tuple[RecordPosition, LogRecord]] = []
    for segment in reversed(_segment_paths(base, max_files)):
        is_active = segment == base
        try:
            stat = segment.stat()
        except OSError as error:
            if require_active and is_active:
                raise LogStoreMissingError(
                    f"{segment}: the active log segment is not there"
                ) from error
            continue
        identity = (stat.st_dev, stat.st_ino)
        for end_offset, line in _iter_segment_lines_reverse(
            segment,
            tolerate_torn_tail=is_active,
            required=require_active and is_active,
        ):
            record = _record_from_line(line, where=f"{segment}:@{end_offset}")
            if expect_class is not None and record.log_class is not expect_class:
                raise LogStoreClassMismatchError(
                    f"{segment}: holds a {record.log_class.value} record but is the "
                    f"{expect_class.value} store (SRS-LOG-001 separation is broken "
                    f"for this trail)"
                )
            if not keep(record):
                continue
            newest_first.append(
                (
                    RecordPosition(device=identity[0], inode=identity[1], end_offset=end_offset),
                    record,
                )
            )
            if len(newest_first) >= limit:
                return newest_first
    return newest_first


def _record_from_line(line: bytes, *, where: str) -> LogRecord:
    """Decode one complete stored line (shared by both read directions)."""

    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LogStoreCorruptionError(
            f"{where}: a complete line is not valid UTF-8: {exc}"
        ) from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LogStoreCorruptionError(f"{where}: complete line is not valid JSON: {exc}") from exc
    return _record_from_mapping(payload, where=where)


def read_records_bounded(
    path: str | os.PathLike[str],
    *,
    limit: int,
    max_files: int = 5,
    require_active: bool = False,
    expect_class: LogClass | None = None,
    **filters: object,
) -> tuple[list[tuple[RecordPosition, LogRecord]], int]:
    """Return the newest ``limit`` matching records, plus the TOTAL match count.

    Memory is bounded by ``limit`` regardless of how large the trail is: matches
    stream through a fixed-size window and only the newest ``limit`` are kept.
    The returned count is still exact — counting costs nothing — so a caller can
    report "showing 100 of 40,000" honestly without holding 40,000 records.

    Returns:
        ``([(position, record), ...] newest-first, matched_total)``. Positions
        travel with the records because an operator surface that merges two feeds
        (a poll and a live channel) needs a real identity to dedupe on — record
        VALUES are not unique, by design.

    Raises:
        LogStoreError: If ``limit`` is not a positive int.
        LogStoreCorruptionError: On any complete-but-unreadable line.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LogStoreError(f"read_records_bounded limit must be a positive int; got {limit!r}")
    keep = _filter_predicate(**filters)  # type: ignore[arg-type]

    # Rotation-stable: take the active segment's identity before and after the
    # scan and retry if it moved. Without this a rotation landing mid-scan can
    # skip records the store still holds, and the caller would get a
    # successful-looking page that is quietly short.
    for _attempt in range(ROTATION_READ_ATTEMPTS):
        before = active_identity(path)
        window: deque[tuple[RecordPosition, LogRecord]] = deque(maxlen=limit)
        matched = 0
        for position, record in iter_records_with_positions(
            path, max_files=max_files, require_active=require_active, expect_class=expect_class
        ):
            if not keep(record):
                continue
            matched += 1
            window.append((position, record))
        if active_identity(path) == before:
            newest_first = list(window)
            newest_first.reverse()
            return newest_first, matched
    raise LogStoreRotationError(
        f"{path}: the store rotated during every read attempt "
        f"({ROTATION_READ_ATTEMPTS}); refusing to return a possibly-short page"
    )


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

    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise LogStoreError(f"query limit must be a non-negative int or None; got {limit!r}")

    keep = _filter_predicate(
        log_class=log_class,
        min_severity=min_severity,
        source=source,
        event_type=event_type,
        correlation_id=correlation_id,
        start_ns=start_ns,
        end_ns=end_ns,
    )

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
    redactor: SecretRedactor | None = None,
) -> tuple[RoutedLogDispatcher, JsonlLogStore, JsonlLogStore]:
    """Wire a SYSTEM store and a *separate* STRATEGY store to a dispatcher.

    This is the SRS-LOG-001 boot path: it materialises the two persistent
    sinks the AC requires ("separate persistent system logs from user
    strategy logs") under ``directory`` and registers them on a
    :class:`~atp_logging.RoutedLogDispatcher` so a single ``dispatch`` call
    routes each record to the correct physical file.

    Returns the ``(dispatcher, system_store, strategy_store)`` triple; the
    caller owns closing the two stores (or using them as context managers).

    Credential redaction (SRS-SEC-001) is always installed on the dispatcher
    **and** both stores, so secrets are scrubbed on every path — including a
    record written directly to a store, bypassing the dispatcher. When
    ``redactor`` is omitted it falls back to the always-on pattern-based
    ``DEFAULT_REDACTOR`` (never zero redaction); the boot layer should inject the
    value-aware ``SecretRedactor(atp_config.secret_values(env))`` for full
    IB/SMTP/SMS value coverage.

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
    # Never wire a zero-redaction path: fall back to the pattern-based default
    # (SRS-SEC-001) so both stores AND the dispatcher redact even when the boot
    # layer forgot to inject the value-aware redactor.
    redactor = redactor if redactor is not None else DEFAULT_REDACTOR
    base = Path(directory)
    system_store = JsonlLogStore(
        base / system_filename,
        log_class=LogClass.SYSTEM,
        max_bytes=max_bytes,
        max_files=max_files,
        fsync=fsync,
        redactor=redactor,
    )
    try:
        strategy_store = JsonlLogStore(
            base / strategy_filename,
            log_class=LogClass.STRATEGY,
            max_bytes=max_bytes,
            max_files=max_files,
            fsync=fsync,
            redactor=redactor,
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
    dispatcher = RoutedLogDispatcher(redactor=redactor)
    dispatcher.register_sink(LogClass.SYSTEM, system_store)
    dispatcher.register_sink(LogClass.STRATEGY, strategy_store)
    return dispatcher, system_store, strategy_store
