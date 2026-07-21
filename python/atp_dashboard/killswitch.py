"""Kill-switch status pane provider (``UI-4`` / SyRS SYS-44a + SYS-44b).

Feeds the dashboard's *Kill Switch — Liquidate Sequence* panel: the status
feedback half of UI-4 ("User can activate kill switch and see cancellation,
liquidation submission, timeout, notification, and disconnect status"). The
*activation* half is the confirm-then-POST affordance in ``app.js``, which
targets the contract route ``POST /api/v1/kill-switch`` — this module adds no
mutating surface, only a read.

Where the facts come from
-------------------------
Both sources are durable artefacts the SRS-SAFE-001 / SRS-SAFE-002 operator
layer already writes; nothing here re-derives or re-runs anything:

* **the last-activation record** — ``atp_safety.state.load_last_activation``
  reads the ``kill_switch_last_activation.json`` the activate handler persists
  (halt / cancellation / liquidation / disconnect legs, the NFR-P3 mark, and
  the 1-second HALTED observability latency);
* **the SYS-44b timeout record** — the newest ``LIQUIDATION_TIMEOUT`` record in
  the SRS-LOG-001 system log, read back through
  :func:`atp_safety.audit.parse_liquidation_timeout_message` (its writer's own
  inverse) for the timeout + notification legs.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
This is the highest-stakes display in the system: a fabricated "IB
DISCONNECTED ✓" is a lie about whether a liquidation completed. Every rule
here fails **closed**:

* Unknown is ``UNKNOWN`` with ``value: None`` and a ``deferred:<owner>`` data
  source — never a green, never a blank that reads as fine.
* ``activated`` is ``None`` when the record cannot be read. It is ``False``
  only when a readable state directory genuinely holds no activation; an
  unreadable or corrupt one must never render as "never activated" (the same
  fail-closed rule ``atp_safety.handlers._load_guard`` applies to the replay
  guard).
* Order lists are ``None`` when unknown, never ``[]`` — an empty list at the
  JSON boundary is all-clear-shaped.
* A boolean leg (``within_nfr_p3``, ``ib_gateway_disconnected``, ``ran_clean``)
  is honoured only when it is strictly a ``bool``; anything else is UNKNOWN.
  A genuine ``False`` is a loud failure, not a blank.
* The transport tier travels with the timeout record: ``FIXTURE`` drill
  evidence is labelled as such and can never masquerade as live SYS-44b
  history.
* The SYS-44b record correlates by **order id**, not by activation id, and the
  activation report carries no id for the liquidations it submitted — so no key
  links the two. The timeout / notification rungs therefore stay UNKNOWN and
  the record is shown as explicitly *uncorrelated* evidence: resolving them
  from it would assert a link the data does not carry (see ``_UNCORRELATED``).
* A last-activation record is honoured only if it can substantiate one (a real
  ``activation_id``, a report, a response, consistent ids). A readable-but-
  drifted object is unavailable, not an activation — over-claiming that the
  sequence ran is as misleading as denying it.

The provider is fail-safe: an unreadable source becomes an explicit
unavailable snapshot (``ok: False`` + the reason), never a crash and never an
empty masquerade. The two sources fail independently — an unreadable log does
not blank the activation legs, and vice versa.

There is no kill-switch WebSocket channel (``atp_ws.channels.Channel`` declares
none), so this pane is REST-poll-only: publishing on a channel the AsyncAPI
contract does not declare would be fabrication at the transport layer.

SRS trace
---------
``UI-4`` (dashboard kill-switch control + status feedback), tracing
``SRS-SAFE-001`` (SYS-44a activation sequence, NFR-P3) and ``SRS-SAFE-002``
(SYS-44b unfilled-liquidation timeout), reading the ``SRS-LOG-001`` system log.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from atp_logging import Source
from atp_logging.persistence import LogStoreCorruptionError, read_records
from atp_safety.audit import parse_liquidation_timeout_message
from atp_safety.state import LastActivationCorruptError, load_last_activation

from .provider import DEFERRED

__all__ = [
    "KILL_SWITCH_ACTIVATION_OWNER",
    "KILL_SWITCH_NOTIFY_OWNER",
    "KILL_SWITCH_TIMEOUT_OWNER",
    "KILL_SWITCH_SEQUENCE",
    "HALT_OBSERVABILITY_BUDGET_MS",
    "LIQUIDATION_BUDGET_MS",
    "DurableKillSwitchStatusSource",
    "KillSwitchStatusProvider",
    "KillSwitchStatusSource",
    "KillSwitchStatusUnavailable",
]

#: Owner of the activation legs (halt / cancellation / liquidation / disconnect):
#: the SRS-SAFE-001 kill-switch runtime. Until its handler is composed onto the
#: operator runtime (``atp_safety.wire_kill_switch``) no activation record can
#: exist, so these legs render as deferred-to-SRS-SAFE-001.
KILL_SWITCH_ACTIVATION_OWNER = "SRS-SAFE-001"

#: Owner of the SYS-44b unfilled-liquidation timeout leg.
KILL_SWITCH_TIMEOUT_OWNER = "SRS-SAFE-002"

#: Owner of the operator email/SMS notification leg (the SYS-44b escalation's
#: delivery transports).
KILL_SWITCH_NOTIFY_OWNER = "SRS-NOTIF-001"

#: The SRS-LOG-001 observability budget for the HALTED transition (SRS-SAFE-001
#: AC). Mirrors ``atp_safety.handlers.HALT_OBSERVABILITY_BUDGET_MS``.
HALT_OBSERVABILITY_BUDGET_MS = 1_000

#: The NFR-P3 budget: cancels + liquidation submission complete within 5 s.
LIQUIDATION_BUDGET_MS = 5_000

#: Status vocabulary. ``UNKNOWN`` is the fail-closed default for every leg;
#: the other four mirror the Rust gate's ``SideEffectOutcome`` statuses plus a
#: ``MIXED`` aggregate for a multi-order phase that did not land uniformly.
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_ATTEMPTED = "NOT_ATTEMPTED"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_MIXED = "MIXED"

_SIDE_EFFECT_STATUSES = frozenset({STATUS_NOT_ATTEMPTED, STATUS_SUCCEEDED, STATUS_FAILED})

#: The rendered ladder: the real SRS-SAFE-001 phase order (halt → cancel →
#: liquidate → disconnect) with the SRS-SAFE-002 escalation branching off it.
#: ``branch`` marks the two legs that belong to the SYS-44b timeout path rather
#: than to the activation sequence itself.
KILL_SWITCH_SEQUENCE: tuple[dict[str, object], ...] = (
    {"phase": "halt", "label": "PAPER ENGINES HALTED", "branch": False},
    {"phase": "cancellation", "label": "CANCELLATION", "branch": False},
    {"phase": "liquidation", "label": "LIQUIDATION SUBMISSION", "branch": False},
    {"phase": "timeout", "label": "UNFILLED TIMEOUT", "branch": True},
    {"phase": "notification", "label": "OPERATOR NOTIFICATION", "branch": True},
    {"phase": "disconnect", "label": "IB DISCONNECT", "branch": False},
)

#: Why the SYS-44b legs can never resolve today.
#:
#: ``build_liquidation_timeout_record`` correlates by the domain ORDER id; the
#: activation report carries no id for the liquidation orders it submitted, so
#: there is no key linking a timeout record to an activation. The newest record
#: may therefore belong to an earlier activation, or to an operator CLI drill,
#: or to no displayed activation at all. Rendering it as *this* sequence's
#: timeout/notification outcome would assert a link the data does not carry —
#: an operator could read "notification SUCCEEDED" for an escalation that was
#: never sent for this liquidation. So the record's content is shown, labelled,
#: and the rungs stay UNKNOWN. Resolving them is SRS-SAFE-002's to enable, by
#: carrying an activation id into the record.
_UNCORRELATED = "latest SYS-44b record (NOT correlated to this activation)"

#: The declared transport tiers of a SYS-44b outcome (``atp_safety.timeout``).
_TRANSPORT_TIERS = frozenset({"FIXTURE", "LIVE"})

#: Default log file name written by the SRS-LOG-001 SYSTEM store.
_SYSTEM_LOG_NAME = "system.jsonl"


class KillSwitchStatusUnavailable(Exception):
    """A kill-switch status source cannot be read right now.

    Reported to the operator verbatim — never swallowed into a clean-looking
    snapshot, and never treated as "nothing happened".
    """


@runtime_checkable
class KillSwitchStatusSource(Protocol):
    """The durable artefacts the pane reads.

    The two legs fail independently on purpose: an unreadable SRS-LOG-001 store
    must not blank an otherwise-readable activation record (and vice versa), so
    each raises :class:`KillSwitchStatusUnavailable` on its own.
    """

    def last_activation(self) -> Mapping[str, object] | None:
        """The persisted last-activation record, or ``None`` if this state
        directory genuinely holds no activation. Raises
        :class:`KillSwitchStatusUnavailable` when it cannot be read."""
        ...

    def last_timeout(self) -> Mapping[str, object] | None:
        """The newest SYS-44b ``LIQUIDATION_TIMEOUT`` record's parsed fields
        plus ``recorded_at_ns``, or ``None`` when no such record exists.
        Raises :class:`KillSwitchStatusUnavailable` when the log cannot be
        read."""
        ...


class DurableKillSwitchStatusSource:
    """Reads the two durable artefacts the SRS-SAFE-001/002 layer writes.

    ``state_dir`` is the directory ``atp_safety.state.persist_last_activation``
    writes into; ``log_dir`` holds the SRS-LOG-001 SYSTEM ``system.jsonl``. The
    dashboard never parses either format itself — the activation record goes
    through ``atp_safety.state`` and the timeout record through its writer's own
    inverse in ``atp_safety.audit`` — so there is one format owner per artefact.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path,
        log_dir: str | Path | None = None,
        max_log_files: int = 5,
    ) -> None:
        self._state_dir = Path(state_dir)
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self._max_log_files = int(max_log_files)

    @property
    def log_configured(self) -> bool:
        return self._log_dir is not None

    def last_activation(self) -> Mapping[str, object] | None:
        try:
            return load_last_activation(self._state_dir)
        except LastActivationCorruptError as corrupt:
            # Corrupt is NOT "never activated": the liquidate sequence may well
            # have run. Same fail-closed stance as the replay guard.
            raise KillSwitchStatusUnavailable(
                f"kill-switch activation state is corrupt (refusing to render it "
                f"as never-activated): {corrupt}"
            ) from corrupt
        except OSError as error:
            raise KillSwitchStatusUnavailable(
                f"kill-switch activation state unreadable: {error}"
            ) from error

    def last_timeout(self) -> Mapping[str, object] | None:
        if self._log_dir is None:
            raise KillSwitchStatusUnavailable(
                "no SRS-LOG-001 system log directory configured for the SYS-44b "
                "liquidation-timeout record (set ATP_KILL_SWITCH_LOG_DIR)"
            )
        try:
            records = read_records(
                self._log_dir / _SYSTEM_LOG_NAME,
                max_files=self._max_log_files,
                source=Source.KILL_SWITCH,
                event_type="LIQUIDATION_TIMEOUT",
                newest_first=True,
                limit=1,
            )
        except FileNotFoundError:
            # No system log yet is an honest "no timeout record", not an error.
            return None
        except (LogStoreCorruptionError, OSError, ValueError) as error:
            raise KillSwitchStatusUnavailable(
                f"SRS-LOG-001 system log unreadable for the SYS-44b timeout record: {error}"
            ) from error
        if not records:
            return None
        record = records[0]
        fields = parse_liquidation_timeout_message(record.message)
        if fields is None:
            raise KillSwitchStatusUnavailable(
                "the newest LIQUIDATION_TIMEOUT record does not match the "
                "SYS-44b record format this dashboard can read — refusing to "
                "guess its timeout / notification outcome"
            )
        return {**fields, "recorded_at_ns": record.timestamp_ns}


def _validated_activation(
    record: Mapping[str, object] | None,
) -> Mapping[str, object] | None:
    """Accept a last-activation record only if it can actually substantiate one.

    ``load_last_activation`` guarantees the file parsed as a JSON object — not
    that the object *is* an activation. Without this gate an empty or
    schema-drifted ``{}`` would read as ``activated: true`` with ``ok: true``
    and no errors: the pane would announce that the liquidate sequence ran,
    with every leg UNKNOWN and nothing to back it up. An activation the
    dashboard cannot substantiate is not an activation it may claim, so a
    drifted record is reported as unavailable (``activated: null``) exactly
    like a corrupt one.

    Required: a non-empty string ``activation_id``, mapping-shaped ``report``
    and ``response``, and — when the response names an id — agreement between
    the two (a record stitched from two different activations describes
    neither).
    """

    if record is None:
        return None
    activation_id = record.get("activation_id")
    if not isinstance(activation_id, str) or not activation_id.strip():
        raise KillSwitchStatusUnavailable(
            "kill-switch activation record carries no activation_id — refusing "
            "to render it as a recorded activation"
        )
    report = record.get("report")
    response = record.get("response")
    if not isinstance(report, Mapping) or not isinstance(response, Mapping):
        raise KillSwitchStatusUnavailable(
            f"kill-switch activation record {activation_id} is missing its "
            "report/response — refusing to render an unsubstantiated activation"
        )
    # Three-way identity agreement, all REQUIRED. The writer stamps the same id
    # onto the record, the report and the response, so a missing or differing
    # one means the file was stitched or is version-skewed — and every leg the
    # pane renders comes out of that report/response. Accepting a partial match
    # would let one activation's cancellation/liquidation/disconnect evidence
    # appear under another activation's receipt: false post-liquidation proof,
    # the exact class this module exists to prevent.
    for part, holder in (("report", report), ("response", response)):
        echoed = holder.get("activation_id")
        if not isinstance(echoed, str) or not echoed.strip():
            raise KillSwitchStatusUnavailable(
                f"kill-switch activation record {activation_id} has a {part} that "
                "names no activation — refusing to attribute its evidence"
            )
        if echoed != activation_id:
            raise KillSwitchStatusUnavailable(
                f"kill-switch activation record identity disagrees: record names "
                f"{activation_id} but its {part} names {echoed}"
            )
    return record


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _deferred(owner: str) -> dict[str, object]:
    return {"value": None, "data_source": f"{DEFERRED}:{owner}"}


def _strict_bool(value: object) -> bool | None:
    """A tri-state read of a report boolean: ``None`` unless it is strictly a
    ``bool``. A truthy string or a missing key is UNKNOWN, never ``True``."""

    return value if isinstance(value, bool) else None


def _strict_int(value: object) -> int | None:
    """A report count, or ``None`` if it is not strictly an ``int``.

    A count that arrives as a string (version skew, a truncated record) means
    the report cannot be trusted — the pane renders no ratio at all rather than
    a coerced one, mirroring ``atp_safety.handlers._report_int``.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _side_effect_status(value: object) -> tuple[str, str | None]:
    """``(status, reason)`` of one ``SideEffectOutcome``-shaped cell."""

    outcome = _mapping(value)
    if outcome is None:
        return STATUS_UNKNOWN, None
    status = outcome.get("status")
    if not isinstance(status, str) or status not in _SIDE_EFFECT_STATUSES:
        return STATUS_UNKNOWN, None
    reason = outcome.get("reason")
    return status, reason if isinstance(reason, str) else None


def _order_rows(entries: object, *, kind: str) -> list[dict[str, object]] | None:
    """Normalise a cancel / liquidation list for the pane's table.

    Fail closed as a whole: any entry that is not shaped as the gate's report
    declares makes the WHOLE list unknown (``None``). A partially-parsed list
    would under-report how many orders the sequence touched — the one number an
    operator counts on after a liquidation.
    """

    if not isinstance(entries, list):
        return None
    rows: list[dict[str, object]] = []
    for entry in entries:
        cell = _mapping(entry)
        if cell is None:
            return None
        status, reason = _side_effect_status(cell.get("outcome"))
        if status == STATUS_UNKNOWN:
            return None
        symbol = cell.get("symbol")
        if not isinstance(symbol, str):
            return None
        rows.append(
            {
                "kind": kind,
                "symbol": symbol,
                "order_id": cell.get("order_id") if isinstance(cell.get("order_id"), str) else None,
                "broker_order_id": (
                    cell.get("broker_order_id")
                    if isinstance(cell.get("broker_order_id"), str)
                    else None
                ),
                "side": cell.get("side") if isinstance(cell.get("side"), str) else None,
                "quantity": cell.get("quantity") if isinstance(cell.get("quantity"), int) else None,
                "status": status,
                "reason": reason,
            }
        )
    return rows


def _aggregate_status(rows: list[dict[str, object]] | None) -> str:
    if rows is None:
        return STATUS_UNKNOWN
    if not rows:
        # A readable, genuinely empty phase: there was nothing to cancel /
        # liquidate. Distinct from UNKNOWN, and never rendered as SUCCEEDED.
        return STATUS_NOT_ATTEMPTED
    statuses = {str(row["status"]) for row in rows}
    if STATUS_FAILED in statuses:
        return STATUS_FAILED
    if statuses == {STATUS_SUCCEEDED}:
        return STATUS_SUCCEEDED
    if statuses == {STATUS_NOT_ATTEMPTED}:
        return STATUS_NOT_ATTEMPTED
    return STATUS_MIXED


#: Data-source labels for a resolved rung — which durable artefact the status
#: was read from.
SOURCE_ACTIVATION_RECORD = "kill_switch_activation_record"


def _leg(
    spec: Mapping[str, object],
    *,
    order: int,
    status: str,
    detail: str,
    owner: str,
    source: str | None,
) -> dict[str, object]:
    """One rung of the rendered ladder.

    ``source`` names the artefact the status was read from. A rung is resolved
    ONLY when it has a source *and* a status that is not ``UNKNOWN``; otherwise
    it is an explicit deferred cell (``value: None``) naming the feature that
    owes the fact, and the pane draws it hatched rather than resolved.
    """

    cell: dict[str, object] = {
        "phase": spec["phase"],
        "label": spec["label"],
        "branch": spec["branch"],
        "order": order,
        "owner": owner,
        "status": status,
        "detail": detail,
    }
    if source is not None and status != STATUS_UNKNOWN:
        cell["value"] = status
        cell["data_source"] = source
    else:
        cell.update(_deferred(owner))
    return cell


class KillSwitchStatusProvider:
    """Assembles the UI-4 kill-switch status payload.

    A composition-time opt-in source (like the SRS-UI-003 account provider), so
    a bare SRS-UI-001 dashboard neither serves the route nor implies a pane.
    Constructed with ``source=None`` when no state directory is configured: the
    snapshot is then an explicit unavailable one, not a quiet all-clear.
    """

    def __init__(self, source: KillSwitchStatusSource | None = None) -> None:
        self._source = source

    def kill_switch_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/kill-switch``."""

        activation: Mapping[str, object] | None = None
        timeout: Mapping[str, object] | None = None
        errors: list[str] = []
        activation_readable = False
        timeout_readable = False

        if self._source is None:
            errors.append(
                "no kill-switch state directory configured (set ATP_KILL_SWITCH_STATE): "
                "activation status is UNKNOWN, not 'never activated'"
            )
        else:
            try:
                candidate = self._source.last_activation()
                activation = _validated_activation(candidate)
                activation_readable = True
            except KillSwitchStatusUnavailable as unavailable:
                activation = None
                errors.append(str(unavailable))
            try:
                timeout = self._source.last_timeout()
                timeout_readable = True
            except KillSwitchStatusUnavailable as unavailable:
                errors.append(str(unavailable))

        report = _mapping(activation.get("report")) if activation is not None else None
        response = _mapping(activation.get("response")) if activation is not None else None

        cancels = _order_rows(
            response.get("cancelled_orders") if response is not None else None, kind="CANCEL"
        )
        liquidations = _order_rows(
            response.get("liquidation_orders") if response is not None else None,
            kind="LIQUIDATION",
        )

        sequence = [
            self._halt_leg(activation, report, order=1),
            self._cancellation_leg(cancels, activation is not None, order=2),
            self._liquidation_leg(liquidations, report, activation is not None, order=3),
            self._timeout_leg(timeout, order=4),
            self._notification_leg(timeout, order=5),
            self._disconnect_leg(report, response, activation is not None, order=6),
        ]

        transports = timeout.get("transports") if timeout is not None else None
        tier = (
            transports if isinstance(transports, str) and transports in _TRANSPORT_TIERS else None
        )

        return {
            "generated_at": _utc_iso(),
            "srs_ref": "UI-4",
            "ok": not errors,
            "errors": errors or None,
            # Tri-state. False ONLY when a readable state directory holds no
            # activation; None whenever the truth could not be established.
            "activated": (activation is not None) if activation_readable else None,
            "activation_id": self._identity(activation, "activation_id"),
            "activated_at": (
                response.get("activated_at")
                if response is not None and isinstance(response.get("activated_at"), str)
                else None
            ),
            "ran_clean": _strict_bool(activation.get("ran_clean")) if activation else None,
            "audit_recorded": _strict_bool(activation.get("audit_recorded"))
            if activation
            else None,
            "within_nfr_p3": _strict_bool(report.get("within_nfr_p3")) if report else None,
            "halted_log_latency_ms": self._latency(activation),
            "halt_observability_budget_ms": HALT_OBSERVABILITY_BUDGET_MS,
            "liquidation_budget_ms": LIQUIDATION_BUDGET_MS,
            "liquidations_submitted_ms": self._submitted_ms(report),
            "sequence": sequence,
            # None (unknown), never [] — an empty table would read as "the
            # sequence touched no orders".
            "orders": (
                None
                if cancels is None and liquidations is None
                else (cancels or []) + (liquidations or [])
            ),
            # The SYS-44b record is correlated by ORDER id, not activation id
            # (see _UNCORRELATED): it is the latest timeout event, and the
            # payload says so rather than letting a consumer assume otherwise.
            "timeout_correlated": False,
            "timeout_record": (
                None
                if timeout is None
                else {
                    "order_id": timeout.get("order_id"),
                    "symbol": timeout.get("symbol"),
                    "side": timeout.get("side"),
                    "quantity": timeout.get("quantity"),
                    "disposition": timeout.get("disposition"),
                    "manual_resolution_required": timeout.get("manual_resolution_required"),
                    "recorded_at_ns": timeout.get("recorded_at_ns"),
                }
            ),
            "timeout_readable": timeout_readable,
            # FIXTURE drill evidence is labelled; unknown tier stays None so the
            # pane can never imply the evidence was live.
            "tier": tier,
        }

    # -------------------------------------------------------------- legs --- #

    @staticmethod
    def _identity(activation: Mapping[str, object] | None, key: str) -> str | None:
        if activation is None:
            return None
        value = activation.get(key)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _latency(activation: Mapping[str, object] | None) -> float | None:
        if activation is None:
            return None
        value = activation.get("halted_log_latency_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    @staticmethod
    def _submitted_ms(report: Mapping[str, object] | None) -> int | None:
        timings = _mapping(report.get("timings")) if report is not None else None
        if timings is None:
            return None
        return _strict_int(timings.get("liquidations_submitted_ms"))

    def _halt_leg(
        self,
        activation: Mapping[str, object] | None,
        report: Mapping[str, object] | None,
        *,
        order: int,
    ) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[0]
        if report is None:
            return _leg(
                spec,
                order=order,
                status=STATUS_UNKNOWN,
                detail="no activation record",
                owner=KILL_SWITCH_ACTIVATION_OWNER,
                source=None,
            )
        status, _ = _side_effect_status(report.get("paper_halt"))
        summary = _mapping(report.get("paper_halt_summary"))
        all_halted = _strict_bool(report.get("all_engines_halted"))
        detail = "engine counts unavailable"
        if summary is not None:
            total = _strict_int(summary.get("engines_total"))
            transitioned = _strict_int(summary.get("transitioned"))
            already = _strict_int(summary.get("already_halted"))
            if total is not None and transitioned is not None and already is not None:
                detail = f"{transitioned + already} / {total} engines HALTED"
            else:
                # Untrustworthy counts: do not render a ratio at all.
                status = STATUS_UNKNOWN
        if all_halted is False:
            # The fleet did NOT fully halt — that outranks a SUCCEEDED status
            # on the halt call itself.
            status = STATUS_FAILED
            detail += " — NOT all engines halted"
        elif all_halted is None and status != STATUS_UNKNOWN:
            status = STATUS_UNKNOWN
            detail += " — fleet confirmation missing"
        latency = self._latency(activation)
        if latency is not None:
            detail += f" · logged in {latency:.0f} ms / {HALT_OBSERVABILITY_BUDGET_MS} ms"
        return _leg(
            spec,
            order=order,
            status=status,
            detail=detail,
            owner=KILL_SWITCH_ACTIVATION_OWNER,
            source=SOURCE_ACTIVATION_RECORD,
        )

    @staticmethod
    def _cancellation_leg(
        cancels: list[dict[str, object]] | None, has_activation: bool, *, order: int
    ) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[1]
        status = _aggregate_status(cancels)
        if cancels is None:
            detail = "no activation record" if not has_activation else "cancel outcomes unreadable"
        elif not cancels:
            detail = "no resting orders to cancel"
        else:
            failed = sum(1 for row in cancels if row["status"] == STATUS_FAILED)
            detail = f"{len(cancels)} resting order(s)" + (f" — {failed} FAILED" if failed else "")
        return _leg(
            spec,
            order=order,
            status=status,
            detail=detail,
            owner=KILL_SWITCH_ACTIVATION_OWNER,
            source=SOURCE_ACTIVATION_RECORD if cancels is not None else None,
        )

    def _liquidation_leg(
        self,
        liquidations: list[dict[str, object]] | None,
        report: Mapping[str, object] | None,
        has_activation: bool,
        *,
        order: int,
    ) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[2]
        status = _aggregate_status(liquidations)
        if liquidations is None:
            detail = (
                "no activation record" if not has_activation else "liquidation outcomes unreadable"
            )
        elif not liquidations:
            detail = "no open positions to liquidate"
        else:
            failed = sum(1 for row in liquidations if row["status"] == STATUS_FAILED)
            detail = f"{len(liquidations)} market order(s)" + (
                f" — {failed} FAILED" if failed else ""
            )
        submitted = self._submitted_ms(report)
        within = _strict_bool(report.get("within_nfr_p3")) if report else None
        if submitted is not None:
            detail += f" · submitted in {submitted} ms / {LIQUIDATION_BUDGET_MS} ms"
        if within is False:
            status = STATUS_FAILED
            detail += " — NFR-P3 BREACHED"
        return _leg(
            spec,
            order=order,
            status=status,
            detail=detail,
            owner=KILL_SWITCH_ACTIVATION_OWNER,
            source=SOURCE_ACTIVATION_RECORD if liquidations is not None else None,
        )

    @staticmethod
    def _timeout_leg(timeout: Mapping[str, object] | None, *, order: int) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[3]
        if timeout is None:
            return _leg(
                spec,
                order=order,
                status=STATUS_UNKNOWN,
                detail="no SYS-44b timeout record",
                owner=KILL_SWITCH_TIMEOUT_OWNER,
                source=None,
            )
        detail = f"{_UNCORRELATED}: {timeout.get('disposition')} · order {timeout.get('order_id')}"
        if str(timeout.get("manual_resolution_required")).lower() == "true":
            detail += " — MANUAL RESOLUTION REQUIRED"
        # UNKNOWN, deliberately: see _UNCORRELATED. The record's content is
        # shown so the operator can act on it, but this rung cannot be resolved
        # FROM it — that would assert a link the data does not carry.
        return _leg(
            spec,
            order=order,
            status=STATUS_UNKNOWN,
            detail=detail,
            owner=KILL_SWITCH_TIMEOUT_OWNER,
            source=None,
        )

    @staticmethod
    def _notification_leg(timeout: Mapping[str, object] | None, *, order: int) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[4]
        if timeout is None:
            return _leg(
                spec,
                order=order,
                status=STATUS_UNKNOWN,
                detail="no SYS-44b timeout record",
                owner=KILL_SWITCH_NOTIFY_OWNER,
                source=None,
            )
        detail = f"{_UNCORRELATED}: operator page {timeout.get('operator_alert')}"
        transports = timeout.get("transports")
        if isinstance(transports, str) and transports in _TRANSPORT_TIERS:
            detail += f" · {transports} transport"
        return _leg(
            spec,
            order=order,
            status=STATUS_UNKNOWN,
            detail=detail,
            owner=KILL_SWITCH_NOTIFY_OWNER,
            source=None,
        )

    @staticmethod
    def _disconnect_leg(
        report: Mapping[str, object] | None,
        response: Mapping[str, object] | None,
        has_activation: bool,
        *,
        order: int,
    ) -> dict[str, object]:
        spec = KILL_SWITCH_SEQUENCE[5]
        if report is None and response is None:
            return _leg(
                spec,
                order=order,
                status=STATUS_UNKNOWN,
                detail="no activation record" if not has_activation else "disconnect unreadable",
                owner=KILL_SWITCH_ACTIVATION_OWNER,
                source=None,
            )
        status, reason = _side_effect_status(report.get("ib_disconnect") if report else None)
        disconnected = (
            _strict_bool(response.get("ib_gateway_disconnected")) if response is not None else None
        )
        if disconnected is False:
            # The SDK-pinned flag says the gateway is still CONNECTED after a
            # liquidation. That outranks any rosier per-call status — the whole
            # point of the leg is that the operator learns IB is still live.
            status = STATUS_FAILED
        elif disconnected is None:
            # No trustworthy ``ib_gateway_disconnected`` flag. The per-call
            # outcome alone says the disconnect was ATTEMPTED, not that the
            # gateway is down, and this is the leg where over-claiming is
            # worst: "IB gateway disconnected" off a truncated record would
            # tell an operator the broker link is closed when it may be open.
            # Missing proof is UNKNOWN.
            status = STATUS_UNKNOWN
        elif status == STATUS_UNKNOWN:
            # ``disconnected is True``, no readable per-call outcome: the
            # response's own pinned field is the contract's proof. Trust it.
            status = STATUS_SUCCEEDED
        if status == STATUS_UNKNOWN:
            detail = "gateway-disconnected proof missing from the activation record"
        else:
            detail = (
                "IB gateway disconnected" if status == STATUS_SUCCEEDED else f"disconnect {status}"
            )
            if reason:
                detail += f" — {reason}"
        return _leg(
            spec,
            order=order,
            status=status,
            detail=detail,
            owner=KILL_SWITCH_ACTIVATION_OWNER,
            source=SOURCE_ACTIVATION_RECORD,
        )
