"""SRS-LOG-001 audit records for a kill-switch activation.

Two SYSTEM records per activation, both correlated by the activation id so
the log query seam (`source=kill_switch`, ``correlation_id=<activation_id>``)
reconstructs the event:

* ``ACTIVATION`` (severity CRITICAL) — the SRS-LOG-001 "kill-switch
  activations" event: the full sequence summary (cancels / liquidations /
  disconnect outcomes and the NFR-P3 mark).
* ``HALTED`` (severity WARN) — the SRS-SAFE-001 AC's own observable: "paper
  simulation engines transition to the HALTED state", with the fleet counts.
  Written as its own record so the 1-second observability clause is judged
  against a first-class, queryable event.
"""

from __future__ import annotations

import time
from typing import Mapping

from atp_logging import LogClass, LogRecord, Severity, Source


def _summary_counts(report: Mapping[str, object]) -> tuple[int, int, int]:
    summary = report.get("paper_halt_summary")
    if not isinstance(summary, Mapping):
        return (0, 0, 0)
    return (
        int(summary.get("engines_total", 0)),
        int(summary.get("transitioned", 0)),
        int(summary.get("already_halted", 0)),
    )


def build_activation_record(
    report: Mapping[str, object], *, timestamp_ns: int | None = None
) -> LogRecord:
    """The ``ACTIVATION`` system record for one activation report."""

    cancels = report.get("resting_order_cancels")
    liquidations = report.get("liquidations")
    timings = report.get("timings")
    liquidations_submitted_ms = (
        timings.get("liquidations_submitted_ms") if isinstance(timings, Mapping) else None
    )
    ib_disconnect = report.get("ib_disconnect")
    disconnect_status = (
        ib_disconnect.get("status") if isinstance(ib_disconnect, Mapping) else "UNKNOWN"
    )
    message = (
        "kill switch activated: "
        f"cancels={len(cancels) if isinstance(cancels, list) else 0} "
        f"liquidations={len(liquidations) if isinstance(liquidations, list) else 0} "
        f"ib_disconnect={disconnect_status} "
        f"liquidations_submitted_ms={liquidations_submitted_ms} "
        f"fully_clean={report.get('fully_clean')} "
        f"within_nfr_p3={report.get('within_nfr_p3')}"
    )
    return LogRecord(
        timestamp_ns=timestamp_ns if timestamp_ns is not None else time.time_ns(),
        severity=Severity.CRITICAL,
        source=Source.KILL_SWITCH,
        event_type="ACTIVATION",
        message=message,
        correlation_id=str(report["activation_id"]),
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )


def build_halted_record(
    report: Mapping[str, object], *, timestamp_ns: int | None = None
) -> LogRecord:
    """The ``HALTED`` system record — the AC's 1-second observable."""

    engines_total, transitioned, already_halted = _summary_counts(report)
    paper_halt = report.get("paper_halt")
    status = paper_halt.get("status") if isinstance(paper_halt, Mapping) else "UNKNOWN"
    message = (
        f"paper engines HALTED: status={status} engines_total={engines_total} "
        f"transitioned={transitioned} already_halted={already_halted} "
        f"all_engines_halted={report.get('all_engines_halted')}"
    )
    return LogRecord(
        timestamp_ns=timestamp_ns if timestamp_ns is not None else time.time_ns(),
        severity=Severity.WARN,
        source=Source.KILL_SWITCH,
        event_type="HALTED",
        message=message,
        correlation_id=str(report["activation_id"]),
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
