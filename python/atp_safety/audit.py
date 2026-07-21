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

import re
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


#: The ordered field vocabulary :func:`build_liquidation_timeout_record` writes
#: into its message, and :func:`parse_liquidation_timeout_message` reads back.
#: One tuple, so writer and reader cannot drift apart silently.
LIQUIDATION_TIMEOUT_FIELDS: tuple[str, ...] = (
    "disposition",
    "transports",
    "order_id",
    "symbol",
    "side",
    "quantity",
    "operator_alert",
    "liquidation_cancel",
    "ib_disconnect",
    "manual_resolution_required",
)

#: Anchored, in-order, whole-string inverse of the writer's message. Every
#: value is a single NON-SPACE token, because every value the writer emits is
#: one: a space inside a value would make the message ambiguous, and the final
#: field is anchored to end-of-string so a drifted or appended message cannot
#: be read as a valid record with a trailing tail (``manual_resolution_required
#: =True trailing`` must NOT parse — the caller compares that field exactly, so
#: a sloppy match there would silently suppress the operator's MANUAL
#: RESOLUTION REQUIRED warning).
_LIQUIDATION_TIMEOUT_RE = re.compile(
    "^kill-switch liquidation timeout: "
    + " ".join(f"{field}=(?P<{field}>[^ ]+)" for field in LIQUIDATION_TIMEOUT_FIELDS)
    + "$"
)

#: Closed vocabularies for the fields whose MEANING the pane keys off. A value
#: outside them is drift, and drift must not be silently downgraded into a
#: reassuring reading (an unrecognised ``manual_resolution_required`` would
#: otherwise compare unequal to "True" and read as "no manual resolution
#: needed"). ``disposition``/``symbol``/``order_id`` stay open — they are
#: rendered verbatim and resolve nothing on their own.
_LIQUIDATION_TIMEOUT_VOCABULARY: dict[str, frozenset[str]] = {
    "transports": frozenset({"FIXTURE", "LIVE"}),
    "side": frozenset({"BUY", "SELL"}),
    "operator_alert": frozenset({"SUCCEEDED", "FAILED", "NOT_ATTEMPTED", "UNKNOWN"}),
    "liquidation_cancel": frozenset({"SUCCEEDED", "FAILED", "NOT_ATTEMPTED", "UNKNOWN"}),
    "ib_disconnect": frozenset({"SUCCEEDED", "FAILED", "NOT_ATTEMPTED", "UNKNOWN"}),
    "manual_resolution_required": frozenset({"True", "False"}),
}


def parse_liquidation_timeout_message(message: str) -> dict[str, str] | None:
    """Read a ``LIQUIDATION_TIMEOUT`` record's message back into its fields.

    The strict inverse of :func:`build_liquidation_timeout_record`'s message —
    kept in this module, beside its writer, because a reader that drifts from
    its writer is exactly how a display surface starts inventing facts. A
    consumer (the UI-4 status pane) renders the SYS-44b timeout / notification
    legs from this, so the contract is all-or-nothing:

    * returns every field of :data:`LIQUIDATION_TIMEOUT_FIELDS` verbatim, or
    * returns ``None`` — **never** a partial dict. A message this module did
      not write (format drift, truncation, an appended tail, a foreign
      producer, a value outside its declared vocabulary) is UNKNOWN, and the
      caller must render it as unknown. Silently dropping or mis-splitting one
      field would let a missing ``ib_disconnect`` read as "nothing to report",
      or a drifted ``manual_resolution_required`` compare unequal to ``True``
      and suppress the operator's MANUAL RESOLUTION REQUIRED warning.
    """

    match = _LIQUIDATION_TIMEOUT_RE.fullmatch(message)
    if match is None:
        return None
    parsed = match.groupdict()
    # Ambiguity check. The message is space-separated ``k=v``, so a value that
    # itself carries a ``<known field>=`` token (an instrument symbol is not
    # this module's data — it originates at the broker/strategy boundary) would
    # shift every LATER field's capture: a crafted symbol could otherwise make
    # this reader hand the pane an ``operator_alert=SUCCEEDED`` nobody wrote.
    # An ambiguous message is not a message this module can read back: UNKNOWN.
    if any(
        f"{field}=" in value for value in parsed.values() for field in LIQUIDATION_TIMEOUT_FIELDS
    ):
        return None
    # Vocabulary check on the fields the pane reasons about. Drift here fails
    # closed rather than reading as the reassuring branch.
    for field, allowed in _LIQUIDATION_TIMEOUT_VOCABULARY.items():
        if parsed[field] not in allowed:
            return None
    if not parsed["quantity"].lstrip("-").isdigit():
        return None
    return {field: parsed[field] for field in LIQUIDATION_TIMEOUT_FIELDS}
