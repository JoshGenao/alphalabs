"""Data-quarantine pane provider (``SRS-DATA-013`` / SyRS SYS-77 / ERR-5).

SYS-77: records that fail validation are quarantined, and the system "shall alert
the operator via the dashboard and notification subsystem with the count and
nature of quarantined records". The notification half is
``data013_quarantine_alert_cli notify``; this module is the dashboard half. It
feeds the quarantine pane served at ``GET /dashboard/api/quarantine``.

Two legs, read independently
----------------------------
* **Counts and reasons** — ``data013_ingestion_validation_cli summary --ssd``
  over the durable quarantine store beside the SSD primary
  (``ATP_SSD_DATA_DIR``): each of the newest runs with its records in, records
  written, quarantined total, the count for each of the six SYS-77 reasons, and
  the primary write's outcome.
* **Alert delivery** — ``data013_quarantine_alert_cli status --ssd --store``
  over the notification audit store (``ATP_NOTIFICATION_STORE_DIR``): each run's
  alert state, decided by the same rule and the same settlement markers the
  ``notify`` pass uses, with each channel's outcome. It sends nothing.

A broken notification store blanks the alert column, never the counts, and a
missing quarantine store blanks the counts, never the configuration of the
alert leg. The two reads are not one snapshot: a run that one leg lists and the
other does not is reported with an UNKNOWN alert state, never a guessed one.

Honesty (the SRS-UI-001 convention, docs/playbooks/honest-surfaces.md)
---------------------------------------------------------------------
* Unknown is ``None``, never ``[]`` or ``0``. Unconfigured, unreadable and
  empty are three different ``state`` values, so "no quarantined records"
  appears only when a store was read and holds a clean run.
* The dashboard parses only the CLIs' ``key:value`` machine output (one format
  owner), strictly: an unknown key, a missing field, a count that does not add
  up, or a value outside its closed vocabulary is CLI drift, reported as
  unreadable rather than rendered.
* A monitoring surface must not crash: every failure becomes a payload.

SRS trace
---------
``SRS-DATA-013`` (validate before the primary write; quarantine; alert with
counts and reasons), SyRS ``SYS-77``, ERR-5.
"""

from __future__ import annotations

import copy
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

__all__ = [
    "ALERT_STATES",
    "CHANNEL_OUTCOMES",
    "PRIMARY_OUTCOMES",
    "QUARANTINE_REASONS",
    "QuarantineCliRunner",
    "QuarantinePaneProvider",
    "QuarantineUnavailable",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Build with `cargo build -p atp-data --bin data013_ingestion_validation_cli
# -p atp-orchestrator --bin data013_quarantine_alert_cli`.
_SUMMARY_BINARY = _REPO_ROOT / "target" / "debug" / "data013_ingestion_validation_cli"
_ALERT_BINARY = _REPO_ROOT / "target" / "debug" / "data013_quarantine_alert_cli"

#: The browser aborts each poll after this long (``POLL_MS`` in assets/app.js).
BROWSER_POLL_BUDGET_S = 4.0

#: Per-invocation subprocess budget (seconds). Both reads run one after the
#: other under the provider's lock, so the WORST case is twice this, and it must
#: finish inside the browser's poll budget with room to serialize and send. A
#: budget that the browser outlasts never recovers: every poll aborts before the
#: read lands, and the next poll starts a fresh slow read (review r12).
#: tests/unit/test_dashboard_quarantine.py pins the sum against ``POLL_MS``.
_DEFAULT_TIMEOUT_S = 1.5

#: How many of the newest runs the pane shows. Both legs read the same number.
RUN_LIMIT = 20

#: How long one read of the two stores serves every request (seconds).
#:
#: The trade, stated: ``status`` loads the whole notification audit store and
#: lists every settlement marker, and ``summary`` lists every run file name, so
#: one read is O(history). Ingestion runs are batch jobs, minutes apart, so a
#: pane up to this many seconds behind loses nothing an operator acts on, while
#: an unthrottled pane would repeat that read on every 4 s poll of every open
#: tab. The body's ``generated_at`` says when the read happened. Pruning the
#: stores themselves is SRS-DATA-010's (``ingestion_validation_contract.deferred``).
MIN_READ_INTERVAL_S = 5.0

#: The six SYS-77 reasons, in the store's canonical order (``ALL_QUARANTINE_REASONS``).
QUARANTINE_REASONS: tuple[str, ...] = (
    "RANGE_VIOLATION",
    "OHLC_OUT_OF_BAND",
    "NEGATIVE_VOLUME",
    "NULL_REQUIRED_FIELD",
    "DUPLICATE_RECORD",
    "OPTION_FIELD_MISSING",
)

#: ``PrimaryWrite::as_str``.
PRIMARY_OUTCOMES: frozenset[str] = frozenset({"PENDING", "WRITTEN", "REFUSED", "FAILED"})

#: ``AlertState::as_str`` (atp-orchestrator ``quarantine_notification``).
ALERT_STATES: frozenset[str] = frozenset(
    {
        "NOT_NEEDED",
        "ALERTED",
        "ALERTED_PROVISIONAL",
        "IN_PROGRESS",
        "AWAITING_OUTCOME",
        "DUE",
        "SETTLED_UNVERIFIABLE",
    }
)

#: ``DeliveryOutcome::as_str`` and ``NotificationChannel::as_str`` (atp-notification).
CHANNEL_OUTCOMES: frozenset[str] = frozenset({"DELIVERED", "QUEUED", "FAILED", "SUPPRESSED"})
_CHANNELS: tuple[str, ...] = ("EMAIL", "PUSH")

_INDEXED = re.compile(r"run_(0|[1-9][0-9]*)_([a-z_A-Z]+)")
_INVALID_MARKER = re.compile(r"invalid_marker_(0|[1-9][0-9]*)")


class QuarantineUnavailable(Exception):
    """A leg could not be read (reported on the pane, never rendered as data)."""


class QuarantineCliRunner(Protocol):
    """The subprocess surface (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"{Path(argv[0]).name} not found at {argv[0]}; build it with cargo "
            "(see python/atp_dashboard/quarantine.py)"
        )
    # Every location is passed as a flag, so the subprocess gets an EMPTY
    # environment: neither CLI can fall back to an ambient ATP_SSD_DATA_DIR the
    # composition did not choose.
    return subprocess.run(
        argv, check=False, capture_output=True, text=True, timeout=timeout, env={}
    )


def _run(runner: QuarantineCliRunner, argv: list[str], timeout: float) -> str:
    name = Path(argv[0]).name
    try:
        completed = runner(argv, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise QuarantineUnavailable(f"{name} timed out after {error.timeout:g} s") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise QuarantineUnavailable(f"{name} could not run: {error}") from error
    if completed.returncode != 0:
        lines = (completed.stderr or "").strip().splitlines()
        # Both CLIs prefix their own name; the message already names the binary.
        detail = lines[0].removeprefix(f"{name}: ") if lines else "no message"
        raise QuarantineUnavailable(f"{name} refused (exit {completed.returncode}): {detail}")
    return completed.stdout


# --------------------------------------------------------------------------- #
# Strict key:value parsing
# --------------------------------------------------------------------------- #


def _pairs(stdout: str, origin: str) -> list[tuple[str, str]]:
    """Every ``key:value`` line, split on ``\\n`` only (never ``splitlines``, whose
    extra separators would let a value forge a line). A line with no colon, a
    duplicate key, or a control character in a value is drift."""

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in stdout.split("\n"):
        if line == "":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise QuarantineUnavailable(f"{origin}: a line has no key ({line[:60]!r})")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise QuarantineUnavailable(f"{origin}: a control character in {key!r}")
        if key in seen:
            raise QuarantineUnavailable(f"{origin}: {key!r} appears twice")
        seen.add(key)
        pairs.append((key, value))
    return pairs


def _count(value: str, field: str, origin: str) -> int:
    if not value.isdigit() or not value.isascii():
        raise QuarantineUnavailable(f"{origin}: {field} is not a count ({value!r})")
    return int(value)


def _bool(value: str, field: str, origin: str) -> bool:
    if value not in ("true", "false"):
        raise QuarantineUnavailable(f"{origin}: {field} is not true/false ({value!r})")
    return value == "true"


def _split(
    pairs: list[tuple[str, str]], header_keys: frozenset[str], origin: str
) -> tuple[dict[str, str], dict[int, dict[str, str]], dict[int, str]]:
    """Header fields, per-run fields, and indexed invalid markers. Anything else is drift."""

    header: dict[str, str] = {}
    runs: dict[int, dict[str, str]] = {}
    markers: dict[int, str] = {}
    for key, value in pairs:
        if key in header_keys:
            header[key] = value
            continue
        indexed = _INDEXED.fullmatch(key)
        if indexed is not None:
            runs.setdefault(int(indexed.group(1)), {})[indexed.group(2)] = value
            continue
        marker = _INVALID_MARKER.fullmatch(key)
        if marker is not None and "invalid_markers" in header_keys:
            markers[int(marker.group(1))] = value
            continue
        raise QuarantineUnavailable(f"{origin}: unknown key {key!r}")
    missing = sorted(header_keys - header.keys())
    if missing:
        raise QuarantineUnavailable(f"{origin}: missing {', '.join(missing)}")
    return header, runs, markers


def _contiguous(indexed: Mapping[int, object], count: int, what: str, origin: str) -> None:
    if sorted(indexed) != list(range(count)):
        raise QuarantineUnavailable(
            f"{origin}: {what} says {count}, but entries {sorted(indexed)} were printed"
        )


_SUMMARY_HEADER = frozenset({"quarantine_dir", "total_runs", "returned", "truncated"})
_READABLE_FIELDS = frozenset(
    {
        "id",
        "status",
        "recorded_at_millis",
        "observed_at_seconds",
        "records_in",
        "primary",
        "quarantined_total",
    }
    | {f"count_{reason}" for reason in QUARANTINE_REASONS}
)


def parse_summary(stdout: str) -> dict[str, object]:
    """Parse ``data013_ingestion_validation_cli summary`` output, fail-closed."""

    origin = "quarantine summary"
    header, flat, _ = _split(_pairs(stdout, origin), _SUMMARY_HEADER, origin)
    total = _count(header["total_runs"], "total_runs", origin)
    returned = _count(header["returned"], "returned", origin)
    truncated = _bool(header["truncated"], "truncated", origin)
    if returned > total or truncated != (returned < total):
        raise QuarantineUnavailable(
            f"{origin}: returned={returned}, total_runs={total}, truncated={truncated} disagree"
        )
    _contiguous(flat, returned, "returned", origin)
    return {
        "total_runs": total,
        "returned": returned,
        "truncated": truncated,
        "runs": [_summary_run(flat[i], i, origin) for i in range(returned)],
    }


def _summary_run(fields: dict[str, str], index: int, origin: str) -> dict[str, object]:
    where = f"{origin} run {index}"
    status = fields.get("status")
    run_id = fields.get("id", "")
    if not run_id:
        raise QuarantineUnavailable(f"{where}: no id")
    if status == "unreadable":
        if set(fields) != {"id", "status", "error"}:
            raise QuarantineUnavailable(f"{where}: unexpected fields {sorted(fields)}")
        return {"run_id": run_id, "status": "unreadable", "error": fields["error"]}
    if status != "readable":
        raise QuarantineUnavailable(f"{where}: status {status!r}")
    primary = fields.get("primary")
    expected = _READABLE_FIELDS | ({"written"} if primary == "WRITTEN" else set())
    if set(fields) != expected:
        raise QuarantineUnavailable(
            f"{where}: fields {sorted(set(fields) ^ expected)} missing or unexpected"
        )
    if primary not in PRIMARY_OUTCOMES:
        raise QuarantineUnavailable(f"{where}: primary {primary!r}")
    records_in = _count(fields["records_in"], "records_in", where)
    total = _count(fields["quarantined_total"], "quarantined_total", where)
    counts = {
        reason: _count(fields[f"count_{reason}"], f"count_{reason}", where)
        for reason in QUARANTINE_REASONS
    }
    if sum(counts.values()) != total:
        raise QuarantineUnavailable(
            f"{where}: the reason counts add to {sum(counts.values())}, not {total}"
        )
    written = _count(fields["written"], "written", where) if primary == "WRITTEN" else None
    if total > records_in or (written is not None and written + total > records_in):
        raise QuarantineUnavailable(
            f"{where}: {total} quarantined and {written} written of {records_in} records"
        )
    return {
        "run_id": run_id,
        "status": "readable",
        "recorded_at_millis": _count(fields["recorded_at_millis"], "recorded_at_millis", where),
        "observed_at_seconds": _count(fields["observed_at_seconds"], "observed_at_seconds", where),
        "records_in": records_in,
        "primary": primary,
        "written": written,
        "quarantined_total": total,
        "counts": counts,
    }


_STATUS_HEADER = frozenset({"notification_store", "total_runs", "returned", "invalid_markers"})


def parse_status(stdout: str) -> dict[str, object]:
    """Parse ``data013_quarantine_alert_cli status`` output, fail-closed."""

    origin = "alert status"
    header, flat, markers = _split(_pairs(stdout, origin), _STATUS_HEADER, origin)
    total = _count(header["total_runs"], "total_runs", origin)
    returned = _count(header["returned"], "returned", origin)
    if returned > total:
        raise QuarantineUnavailable(f"{origin}: returned={returned} exceeds total_runs={total}")
    invalid = _count(header["invalid_markers"], "invalid_markers", origin)
    _contiguous(flat, returned, "returned", origin)
    _contiguous(markers, invalid, "invalid_markers", origin)
    by_id: dict[str, dict[str, object]] = {}
    judged_by_id: dict[str, tuple[object, ...]] = {}
    for i in range(returned):
        fields = flat[i]
        where = f"{origin} run {i}"
        run_id = fields.get("id", "")
        state = fields.get("alert")
        if not run_id or state not in ALERT_STATES:
            raise QuarantineUnavailable(f"{where}: id {run_id!r}, alert {state!r}")
        attempt = fields.get("attempt")
        judged = _judged_state(fields, where)
        allowed = {"id", "alert", "settled", "attempt"} | _JUDGED_FIELDS[judged[0]]
        if judged[0] == "readable" and judged[2] == "WRITTEN":
            allowed |= {"written"}
        channels: dict[str, str] = {}
        attempt_at: int | None = None
        if attempt == "stored":
            allowed |= {"attempt_at_millis"} | {f"channel_{c}" for c in _CHANNELS}
            attempt_at = _count(fields.get("attempt_at_millis", ""), "attempt_at_millis", where)
            for channel in _CHANNELS:
                outcome = fields.get(f"channel_{channel}")
                if outcome is not None:
                    if outcome not in CHANNEL_OUTCOMES:
                        raise QuarantineUnavailable(f"{where}: {channel} outcome {outcome!r}")
                    channels[channel] = outcome
        elif attempt != "none":
            raise QuarantineUnavailable(f"{where}: attempt {attempt!r}")
        if not set(fields) <= allowed or "settled" not in fields:
            raise QuarantineUnavailable(
                f"{where}: unexpected fields {sorted(set(fields) - allowed)}"
            )
        if run_id in by_id:
            raise QuarantineUnavailable(f"{where}: run {run_id} listed twice")
        by_id[run_id] = {
            "state": state,
            "settled": _bool(fields["settled"], "settled", where),
            "attempt_at_millis": attempt_at,
            # A channel the stored event does not carry stays absent: absent is
            # not FAILED, and the pane renders it as unknown.
            "channels": channels,
        }
        judged_by_id[run_id] = judged
    return {"invalid_markers": invalid, "by_id": by_id, "judged": judged_by_id}


#: The run-state fields ``status`` prints beside each alert state, by run status.
_JUDGED_FIELDS: dict[object, set[str]] = {
    "readable": {"status", "records_in", "primary", "quarantined_total"},
    "unreadable": {"status"},
}


def _judged_state(fields: dict[str, str], where: str) -> tuple[object, ...]:
    """The run state an alert state was decided against, as a comparable tuple.

    The same tuple :func:`_state_of` builds from the counts leg, so the join can
    tell when the run changed between the two reads (review r13).
    """

    status = fields.get("status")
    if status == "unreadable":
        return ("unreadable",)
    if status != "readable":
        raise QuarantineUnavailable(f"{where}: status {status!r}")
    primary = fields.get("primary")
    if primary not in PRIMARY_OUTCOMES:
        raise QuarantineUnavailable(f"{where}: primary {primary!r}")
    if (primary == "WRITTEN") != ("written" in fields):
        raise QuarantineUnavailable(f"{where}: written must appear exactly when WRITTEN")
    written = _count(fields["written"], "written", where) if primary == "WRITTEN" else None
    return (
        "readable",
        _count(fields.get("records_in", ""), "records_in", where),
        primary,
        written,
        _count(fields.get("quarantined_total", ""), "quarantined_total", where),
    )


def _state_of(run: Mapping[str, object]) -> tuple[object, ...]:
    """The counts leg's view of a run, in :func:`_judged_state`'s shape."""

    if run["status"] == "unreadable":
        return ("unreadable",)
    return (
        "readable",
        run["records_in"],
        run["primary"],
        run["written"],
        run["quarantined_total"],
    )


# --------------------------------------------------------------------------- #
# The provider
# --------------------------------------------------------------------------- #


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _headline(runs: list[dict[str, object]]) -> str:
    """The pane's state, from the NEWEST run. Older runs are in the ledger."""

    if not runs:
        return "no_runs"
    newest = runs[0]
    if newest["status"] == "unreadable":
        return "run_unreadable"
    if newest["quarantined_total"]:
        return "quarantined"
    # A PENDING run that quarantined nothing YET can still gain conflicts.
    return "in_progress" if newest["primary"] == "PENDING" else "clean"


class QuarantinePaneProvider:
    """Assembles the ``GET /dashboard/api/quarantine`` body.

    One read of the two stores serves every request for
    :data:`MIN_READ_INTERVAL_S`; concurrent requests on the threaded server wait
    for that one read rather than each starting their own.
    """

    def __init__(
        self,
        *,
        ssd_dir: str | Path | None,
        notification_store_dir: str | Path | None,
        runner: QuarantineCliRunner | None = None,
        summary_binary: str | Path = _SUMMARY_BINARY,
        alert_binary: str | Path = _ALERT_BINARY,
        timeout: float = _DEFAULT_TIMEOUT_S,
        clock: Callable[[], str] = _utc_iso,
        min_interval_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ssd = None if ssd_dir is None else str(ssd_dir)
        self._store = None if notification_store_dir is None else str(notification_store_dir)
        self._runner: QuarantineCliRunner = runner if runner is not None else _default_runner
        self._summary_binary = str(summary_binary)
        self._alert_binary = str(alert_binary)
        self._timeout = float(timeout)
        self._clock = clock
        self._min_interval_s = float(
            MIN_READ_INTERVAL_S if min_interval_s is None else min_interval_s
        )
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last: tuple[float, dict[str, object]] | None = None

    def quarantine_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/quarantine``."""

        with self._lock:
            now = self._monotonic()
            if self._last is None or now - self._last[0] >= self._min_interval_s:
                body = self._read()
                # Stamped when the read FINISHED: a slow read must still serve the
                # whole interval, not arrive already expired.
                self._last = (self._monotonic(), body)
            # A copy, so no caller can edit what the next request is served.
            return copy.deepcopy(self._last[1])

    def _read(self) -> dict[str, object]:
        body: dict[str, object] = {
            "generated_at": self._clock(),
            "srs_ref": "SRS-DATA-013",
            "reasons": list(QUARANTINE_REASONS),
        }
        if self._ssd is None:
            return {
                **body,
                "state": "not_configured",
                "error": "ATP_SSD_DATA_DIR is not set: no quarantine store to read",
                "total_runs": None,
                "returned": None,
                "truncated": None,
                "runs": None,
                "alerts": self._alerts_unknown("the quarantine store is not configured"),
            }
        try:
            summary = parse_summary(
                _run(
                    self._runner,
                    [
                        self._summary_binary,
                        "summary",
                        "--ssd",
                        self._ssd,
                        "--limit",
                        str(RUN_LIMIT),
                    ],
                    self._timeout,
                )
            )
        except QuarantineUnavailable as unavailable:
            return {
                **body,
                "state": "unreadable",
                "error": str(unavailable),
                "total_runs": None,
                "returned": None,
                "truncated": None,
                "runs": None,
                "alerts": self._alerts_unknown("the quarantine store could not be read"),
            }
        runs: list[dict[str, object]] = summary["runs"]  # type: ignore[assignment]
        alerts = self._alert_leg(runs)
        return {
            **body,
            "state": _headline(runs),
            "error": None,
            "total_runs": summary["total_runs"],
            "returned": summary["returned"],
            "truncated": summary["truncated"],
            "runs": runs,
            "alerts": alerts,
        }

    @staticmethod
    def _alerts_unknown(why: str, *, configured: bool | None = None) -> dict[str, object]:
        return {
            "configured": configured,
            "ok": None,
            "error": why,
            "invalid_markers": None,
            "due": None,
        }

    def _alert_leg(self, runs: list[dict[str, object]]) -> dict[str, object]:
        """Attach each run's alert to it (``None`` where unknown) and summarise the leg."""

        for run in runs:
            run["alert"] = None
        if self._store is None:
            return self._alerts_unknown(
                "ATP_NOTIFICATION_STORE_DIR is not set: alert delivery cannot be read",
                configured=False,
            )
        try:
            status = parse_status(
                _run(
                    self._runner,
                    [
                        self._alert_binary,
                        "status",
                        "--ssd",
                        self._ssd or "",
                        "--store",
                        self._store,
                        "--limit",
                        str(RUN_LIMIT),
                    ],
                    self._timeout,
                )
            )
        except QuarantineUnavailable as unavailable:
            return {
                "configured": True,
                "ok": False,
                "error": str(unavailable),
                "invalid_markers": None,
                "due": None,
            }
        by_id: dict[str, dict[str, object]] = status["by_id"]  # type: ignore[assignment]
        judged: dict[str, tuple[object, ...]] = status["judged"]  # type: ignore[assignment]
        unmatched = 0
        due = 0
        for run in runs:
            run_id = str(run["run_id"])
            alert = by_id.get(run_id)
            # An alert describes ONE state of the run. If the run changed between
            # the two reads, this delivery belongs to a state the counts beside it
            # do not show, so it is not shown with them (review r13).
            if alert is not None and judged.get(run_id) != _state_of(run):
                alert = None
            run["alert"] = alert
            if alert is None:
                unmatched += 1
            elif alert["state"] == "DUE":
                due += 1
        return {
            "configured": True,
            "ok": True,
            # Runs the two reads disagree about stay UNKNOWN; say how many.
            "error": (
                f"{unmatched} run(s) appeared or changed between the two reads; "
                "their alert state is unknown"
                if unmatched
                else None
            ),
            "invalid_markers": status["invalid_markers"],
            # Counted over the runs SHOWN; null when any shown run is unknown,
            # because a partial count would read as a total.
            "due": None if unmatched else due,
        }
