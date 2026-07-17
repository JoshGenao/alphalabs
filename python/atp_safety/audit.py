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

Plus the SRS-SAFE-002 / SyRS SYS-44b record:

* ``LIQUIDATION_TIMEOUT`` (severity CRITICAL) — the "log the unfilled order
  details" leg: the unfilled liquidation order (id, symbol, side, quantity)
  and each SYS-44b side-effect outcome (operator page / cancel / disconnect),
  correlated by the domain order id. Built from the
  ``safe002_liquidation_timeout_cli`` outcome by
  :func:`build_liquidation_timeout_record`.
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


def _cleanup_status(outcome: Mapping[str, object], leg: str) -> str:
    cleanup = outcome.get("cleanup")
    if not isinstance(cleanup, Mapping):
        return "UNKNOWN"
    side_effect = cleanup.get(leg)
    if not isinstance(side_effect, Mapping):
        return "UNKNOWN"
    return str(side_effect.get("status", "UNKNOWN"))


def build_liquidation_timeout_record(
    outcome: Mapping[str, object], *, timestamp_ns: int | None = None
) -> LogRecord:
    """The ``LIQUIDATION_TIMEOUT`` system record for one SYS-44b outcome.

    ``outcome`` is the parsed ``outcome:{json}`` payload of
    ``safe002_liquidation_timeout_cli resolve`` (see
    :class:`atp_safety.timeout.RustCliLiquidationTimeoutBackend`). The record
    carries the SYS-44b "unfilled order details" verbatim — order id, symbol,
    side, quantity — plus the disposition and each side-effect outcome, so
    the durable log line alone tells the operator what happened and what
    still needs manual resolution.
    """

    order = outcome.get("unfilled_order")
    if not isinstance(order, Mapping):
        raise ValueError(
            "liquidation-timeout outcome carries no unfilled_order — refusing to "
            "write a LIQUIDATION_TIMEOUT record without the SYS-44b order details"
        )
    order_id = str(order.get("order_id", "")).strip()
    if not order_id:
        raise ValueError(
            "liquidation-timeout outcome has a blank unfilled_order.order_id — "
            "the record's correlation id must be the domain order id"
        )
    message = (
        "kill-switch liquidation timeout: "
        f"disposition={outcome.get('disposition')} "
        # The transport tier travels INTO the durable record so FIXTURE-drill
        # evidence can never masquerade as live SYS-44b history.
        f"transports={outcome.get('transports')} "
        f"order_id={order_id} "
        f"symbol={order.get('symbol')} "
        f"side={order.get('side')} "
        f"quantity={order.get('quantity')} "
        f"operator_alert={_cleanup_status(outcome, 'operator_alert')} "
        f"liquidation_cancel={_cleanup_status(outcome, 'liquidation_cancel')} "
        f"ib_disconnect={_cleanup_status(outcome, 'ib_disconnect')} "
        f"manual_resolution_required={outcome.get('manual_resolution_required')}"
    )
    return LogRecord(
        timestamp_ns=timestamp_ns if timestamp_ns is not None else time.time_ns(),
        severity=Severity.CRITICAL,
        source=Source.KILL_SWITCH,
        event_type="LIQUIDATION_TIMEOUT",
        message=message,
        correlation_id=order_id,
        log_class=LogClass.SYSTEM,
        strategy_id=None,
    )
