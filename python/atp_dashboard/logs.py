"""Dashboard log pane provider (SRS-LOG-001 / SyRS SYS-61 + SYS-38).

Feeds the dashboard's log pane — the AC clause "both log classes are viewable
from the dashboard". The two classes are returned as **separate** cells read
from **separate** stores: the payload shape itself carries the separation this
feature exists to provide, so a rendering bug cannot merge two audit trails that
the storage layer kept apart.

Events are rendered by :func:`atp_logs_service.render_event`, the same function
behind ``GET /api/v1/logs`` and the ``LOGS`` WebSocket channel, so the pane, the
REST response, and the published event can never drift into three shapes.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
* **Unreadable is not empty.** A corrupt store, a permission error, or a missing
  parent directory yields ``records: None`` plus ``ok: False`` and the reason.
  An empty list means "this store was read successfully and matched nothing";
  the two must never be confused on an audit surface.
* **Unmounted is not empty either.** A provider composed without a store path
  is not constructible — the paths are required — so the pane is either mounted
  with real stores or absent entirely (``mount_dashboard`` registers no route,
  and the SPA renders its explicit not-mounted state).
* **Coverage is stated, not implied.** :data:`SOURCE_COVERAGE` names, for each
  of the eight SyRS SYS-61 system sources, whether a producer exists anywhere in
  the tree and which feature owns the missing ones. Without it, a pane showing
  three kinds of event reads as "the system emitted three kinds of event"
  — when in truth five categories have nothing that could ever write them.
* The pane is read-only. It offers no control that would mutate the trail.

A monitoring surface must not crash: every read is guarded and degraded into an
explicit cell rather than an exception.

SRS trace
---------
``SRS-LOG-001`` (both log classes viewable from the dashboard), SyRS ``SYS-61``
(system event taxonomy + severities), ``SYS-38`` (user strategy logs),
``SRS-UI-001`` (the dashboard this mounts on).
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from atp_logging.persistence import read_tail
from atp_logging.records import EVENT_TYPES_BY_SOURCE, LogClass, Severity, Source
from atp_logs_service import EVENT_FIELDS, render_event

__all__ = [
    "LOG_PANE_SRS_REF",
    "SOURCE_COVERAGE",
    "LogPaneProvider",
    "SourceCoverage",
]

#: The requirement this pane answers to.
LOG_PANE_SRS_REF = "SRS-LOG-001"

#: Default number of records rendered per class (newest first). The pane is a
#: monitoring surface, not an export: the full trail is served by
#: ``GET /api/v1/logs``.
DEFAULT_PANE_RECORDS = 100


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """Whether a SYS-61 system source has anything that writes it, and who owns it.

    Attributes:
        produced: The source's declared event types that something in the tree
            actually writes. The tri-state (``produced`` / ``partial`` /
            ``deferred``) is DERIVED from this by :meth:`state_for`, so the two
            cannot disagree.
        owners: Feature ids that own the missing producer(s). Empty when the
            source is fully produced.
        note: One line naming precisely what is and is not written.
    """

    produced: tuple[str, ...]
    owners: tuple[str, ...]
    note: str

    def unproduced(self, source: Source) -> tuple[str, ...]:
        """Declared event types of ``source`` that nothing in the tree writes."""

        return tuple(name for name in EVENT_TYPES_BY_SOURCE[source] if name not in self.produced)

    def state_for(self, source: Source) -> str:
        """``produced`` / ``partial`` / ``deferred``, DERIVED from :attr:`produced`.

        Derived rather than declared: a hand-written state is a second copy of
        the same fact, and the copy is what goes stale when a producer lands or a
        new event type is declared. The pane's whole claim is that it does not
        overstate coverage, so the claim must not rest on a string somebody
        remembered to update.
        """

        missing = self.unproduced(source)
        if not missing:
            return "produced"
        return "deferred" if len(missing) == len(EVENT_TYPES_BY_SOURCE[source]) else "partial"

    def as_dict(self, source: Source) -> dict[str, object]:
        return {
            "source": source.value,
            "state": self.state_for(source),
            "owners": list(self.owners),
            "note": self.note,
            "event_types": list(EVENT_TYPES_BY_SOURCE[source]),
            # Per EVENT TYPE, not just per source. A source marked "partial"
            # tells an operator that something is missing but not what, and the
            # gap that hid here was SEQUENCE_GAP: declared for market_data, with
            # no producer anywhere (SRS-MD-007 owns the seam), while the source's
            # note accounted only for the other three. A coverage strip that
            # under-reports is the failure mode it exists to prevent.
            "produced_event_types": [
                name for name in EVENT_TYPES_BY_SOURCE[source] if name in self.produced
            ],
            "unproduced_event_types": list(self.unproduced(source)),
        }


#: Producer coverage for the eight SYS-61 system sources, as of this feature's
#: own session. This is a statement about the TREE, not about a particular
#: composition: a ``produced`` source still emits nothing in a runtime whose
#: composer never wired that producer's store (see ``coverage_note``).
SOURCE_COVERAGE: Mapping[Source, SourceCoverage] = {
    Source.ORDER_ROUTING: SourceCoverage(
        produced=(),
        owners=("SRS-EXE-001", "SRS-EXE-002"),
        note=(
            "No producer writes routing decisions or outcomes. Live routing is "
            "SRS-EXE-001; simulated routing is SRS-EXE-002."
        ),
    ),
    Source.INGESTION: SourceCoverage(
        produced=(),
        owners=("SRS-DATA-001",),
        note="No producer writes ingestion job start/completion/failure records.",
    ),
    Source.CONTAINER_LIFECYCLE: SourceCoverage(
        produced=(),
        owners=("SRS-ORCH-001",),
        note=(
            "No producer writes container start/stop/restart/OOM-kill records. "
            "The orchestrator that manages the containers is built; emitting "
            "SYS-61 records from it is unclaimed."
        ),
    ),
    Source.IB_GATEWAY: SourceCoverage(
        produced=("HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"),
        owners=("SRS-SAFE-003",),
        note=(
            "HEARTBEAT_STALE / HEARTBEAT_RECOVERED are written by the SRS-MD-003 "
            "freshness monitor. CONNECT / DISCONNECT / RECONNECT state changes "
            "have no producer."
        ),
    ),
    Source.KILL_SWITCH: SourceCoverage(
        produced=("ACTIVATION", "HALTED", "LIQUIDATION_TIMEOUT"),
        owners=(),
        note=(
            "ACTIVATION and HALTED are written by the SRS-SAFE-001 activation "
            "handler; LIQUIDATION_TIMEOUT by the SRS-SAFE-002 timeout path."
        ),
    ),
    Source.HOT_SWAP: SourceCoverage(
        produced=(),
        owners=("SRS-RESV-004", "SRS-RESV-005"),
        note="No producer writes promotion or demotion records.",
    ),
    Source.RESOURCE_MONITOR: SourceCoverage(
        produced=(),
        owners=("SRS-ORCH-003",),
        note=(
            "No producer writes SYS-58 threshold alerts. The host-memory safety "
            "margin enforcement is built; emitting SYS-61 records from it is "
            "unclaimed."
        ),
    ),
    Source.MARKET_DATA: SourceCoverage(
        produced=("HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"),
        owners=("SRS-MD-001", "SRS-MD-007"),
        note=(
            "HEARTBEAT_STALE / HEARTBEAT_RECOVERED are written by the SRS-MD-003 "
            "freshness monitor. SUBSCRIPTION_CHANGE has no producer (SRS-MD-001). "
            "SEQUENCE_GAP has none either and is not merely unbuilt: IB's API "
            "exposes no tick sequence, so the SRS-MD-007 seam that would own it "
            "has nothing to detect a gap WITH — atp_dashboard/heartbeat.py "
            "refuses to write one rather than infer it."
        ),
    ),
}

_COVERAGE_NOTE = (
    "Class cells report a PAGE: `ok` means the read succeeded, not that the trail "
    "is sound — a tail read validates only the lines it scanned, so corruption "
    "further back is neither detected nor denied (`integrity_scope: page`). "
    "GET /api/v1/logs scans the whole trail and fails closed; that is where a "
    "clean bill of health comes from. "
    "Producer coverage describes what exists in the codebase, not what this "
    "runtime is wired to write: a 'produced' source still emits nothing in a "
    "composition whose owner was never mounted. An empty class cell therefore "
    "means 'this store was read and matched no records' — never 'no such events "
    "occurred'."
)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class LogPaneProvider:
    """Serves the dashboard log pane at ``GET /dashboard/api/logs``.

    Args:
        system_store_path: Active segment of the SYSTEM store — REQUIRED.
        strategy_store_path: Active segment of the STRATEGY store — REQUIRED,
            and a different physical file.
        max_records: Records rendered per class, newest first.
        max_files: Rotation depth of the stores being read.
    """

    def __init__(
        self,
        *,
        system_store_path: str | os.PathLike[str],
        strategy_store_path: str | os.PathLike[str],
        max_records: int = DEFAULT_PANE_RECORDS,
        max_files: int = 5,
    ) -> None:
        system = Path(system_store_path)
        strategy = Path(strategy_store_path)
        if _same_target(system, strategy):
            raise ValueError(
                "system and strategy store paths resolve to the same file "
                f"({system} / {strategy}); SRS-LOG-001 requires separate persistent sinks"
            )
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records <= 0:
            raise ValueError(f"max_records must be a positive int; got {max_records!r}")
        self._paths: dict[LogClass, Path] = {
            LogClass.SYSTEM: system,
            LogClass.STRATEGY: strategy,
        }
        self._max_records = max_records
        self._max_files = max_files

    # ----- the pane payload ----- #

    def logs_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/logs``.

        ``ok`` is the AND of both class cells: the pane is only healthy when
        BOTH audit trails could be read. A caller keying off ``ok`` must not
        read "the system log is fine" from a snapshot whose strategy store is
        corrupt.
        """

        system = self._class_cell(LogClass.SYSTEM)
        strategy = self._class_cell(LogClass.STRATEGY)
        return {
            "generated_at": _utc_iso(),
            "ok": bool(system["ok"]) and bool(strategy["ok"]),
            "srs_ref": LOG_PANE_SRS_REF,
            "classes": {
                LogClass.SYSTEM.value: system,
                LogClass.STRATEGY.value: strategy,
            },
            "event_fields": list(EVENT_FIELDS),
            "severities": [severity.value for severity in Severity],
            "source_coverage": [
                coverage.as_dict(source) for source, coverage in SOURCE_COVERAGE.items()
            ],
            "coverage_note": _COVERAGE_NOTE,
        }

    def _class_cell(self, log_class: LogClass) -> dict[str, object]:
        path = self._paths[log_class]
        # Fast path only; the read below is the authoritative check.
        if not path.exists():
            # A CONFIGURED trail that is not there is a failure, not an empty
            # log. Reading it succeeds trivially and yields zero records, which
            # renders exactly like a healthy quiet system — so a deleted store, a
            # mispointed ATP_LOG_DIR, or a writer that never started would show
            # the operator a reassuring green pane over missing audit history.
            # The pane exists to make that visible, so it fails closed.
            return {
                "ok": False,
                "log_class": log_class.value,
                "records": None,
                "matched": None,
                "page_only": True,
                "integrity_scope": None,
                "truncated": None,
                "store": path.name,
                "store_present": False,
                "error": (
                    f"the configured {log_class.value} log store does not exist ({path}); "
                    "this is a missing audit trail, not an empty one"
                ),
            }
        try:
            # Bounded read: the pane shows a page, and an unbounded audit trail
            # must not be materialised to produce it.
            # TAIL read, not a full scan. This provider is POLLED (every few
            # seconds, forever) against an append-only store, so counting the
            # whole trail to report an exact total would make the pane cost
            # O(all history) on every refresh — a self-inflicted load that grows
            # without bound. Reading backwards from the newest segment costs the
            # PAGE instead. Authoritative about absence: a trail that vanishes
            # mid-read is reported by the read, not missed by a stale pre-check.
            page = read_tail(
                path,
                limit=self._max_records,
                max_files=self._max_files,
                require_active=True,
                # A contaminated store fails the read rather than being quietly
                # filtered: separation breaking is exactly what this pane exists
                # to make visible.
                expect_class=log_class,
                log_class=log_class,
            )
        except Exception as error:  # noqa: BLE001 - a monitoring surface must not crash
            return {
                "ok": False,
                "log_class": log_class.value,
                # NOT [] — an unreadable audit trail must never render as an
                # empty one. The SPA shows the error, not a reassuring "0".
                "records": None,
                "matched": None,
                "page_only": True,
                "integrity_scope": None,
                "truncated": None,
                "store": path.name,
                "store_present": path.exists(),
                "error": f"{type(error).__name__}: {error}",
            }
        return {
            "ok": True,
            "log_class": log_class.value,
            "records": [render_event(record, position) for position, record in page],
            # UNKNOWN, deliberately: a tail read counts only what it read, and
            # inventing a total would be the pane's own kind of fabrication. The
            # operator's GET /api/v1/logs query does the full scan and reports
            # the exact figure. `page_only` says which kind of answer this is.
            "matched": None,
            "page_only": True,
            # SCOPE of the integrity claim. ``ok`` means "this READ succeeded",
            # never "this trail is sound": a tail read validates only the lines
            # it scanned, so a corrupt line further back is neither detected nor
            # denied here. Saying so is the difference between reporting a page
            # and implying a clean audit history. Full-trail verification lives
            # on GET /api/v1/logs, which scans and fails closed.
            "integrity_scope": "page",
            "truncated": len(page) >= self._max_records,
            "store": path.name,
            # "no store file" and "store file with no records" both read as zero
            # records; the pane distinguishes them so a deleted or misconfigured
            # trail cannot render as a reassuringly quiet one.
            "store_present": path.exists(),
            "error": None,
        }


def _same_target(first: Path, second: Path) -> bool:
    """Whether two store paths denote the same physical file (alias-aware)."""

    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:  # pragma: no cover - stat failure falls back to text compare
        pass
    return os.path.normcase(first.resolve()) == os.path.normcase(second.resolve())
