"""Kill-switch REST/CLI handlers (SRS-SAFE-001 on the SRS-API-001 runtime).

One transport-free activate handler serves both ``POST /api/v1/kill-switch``
and ``kill-switch activate`` (the SDK pinned identical semantics on both
surfaces); a status handler serves ``kill-switch status``.

Activation flow — ordered so a failure at any point can never re-fire the
liquidate sequence on retry:

1. Replay guard: a persisted last-activation record short-circuits to its
   stored response (same ``activation_id``, HTTP 200, NO second backend
   call). A corrupt record fails closed — corruption must not look like
   "never activated".
2. ``backend.activate(activation_id)`` — the Rust gate runs the sequence.
   A backend that cannot run raises ``KILL_SWITCH_BACKEND_UNAVAILABLE``
   (500); a hung backend raises ``TimeoutError`` (504 / CLI exit TIMEOUT).
   Neither is ever success-shaped.
3. The last-activation record is persisted durably (the replay guard arms
   BEFORE anything else can fail).
4. The ``ACTIVATION`` + ``HALTED`` SRS-LOG-001 SYSTEM records are written
   durably; the activation→durable-HALTED-write latency is measured against
   the 1-second budget and stored on the record. A failed audit write is an
   AC violation this layer owns: the handler raises
   ``KILL_SWITCH_AUDIT_WRITE_FAILED`` (500) carrying the sequence outcome in
   ``detail`` — the sequence itself already ran and is guarded against
   replay, so a retry returns the persisted record instead of re-firing.
5. The SDK-pinned response body (exactly the frozen ``response_fields``:
   ``activation_id`` / ``activated_at`` / ``cancelled_orders`` /
   ``liquidation_orders`` / ``paper_engines_halted`` /
   ``ib_gateway_disconnected``) is returned with HTTP 200.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from atp_logging.persistence import JsonlLogStore
from atp_runtime.errors import ErrorCategory, InterfaceError
from atp_runtime.registry import HandlerResult, Request

from .audit import build_activation_record, build_halted_record
from .backend import KillSwitchBackend, KillSwitchBackendError
from .state import (
    LastActivationCorruptError,
    load_last_activation,
    persist_last_activation,
)

#: The SRS-LOG-001 observability budget (SRS-SAFE-001 AC): the HALTED
#: transition must be durably observable within 1 second of activation.
#: Mirrors atp-types KILL_SWITCH_HALT_OBSERVABILITY_BUDGET_MS.
HALT_OBSERVABILITY_BUDGET_MS = 1_000


def _isoformat_utc(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds"
    )


def _report_int(value: object, field: str) -> int:
    """A report numeric that is not an integer means the report cannot be
    trusted (version skew / truncation) — fail closed, never coerce."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise InterfaceError(
            ErrorCategory.INTERNAL_ERROR,
            f"kill-switch report field {field!r} is not an integer — refusing "
            "an untrustworthy activation report",
            type="KILL_SWITCH_BACKEND_UNAVAILABLE",
            detail={"field": field},
        )
    return value


def _response_from_report(report: Mapping[str, object]) -> dict[str, object]:
    """Build EXACTLY the SDK-pinned response fields from a gate report."""

    summary = report.get("paper_halt_summary")
    halted = 0
    if isinstance(summary, Mapping):
        halted = _report_int(
            summary.get("transitioned", 0), "paper_halt_summary.transitioned"
        ) + _report_int(summary.get("already_halted", 0), "paper_halt_summary.already_halted")
    ib_disconnect = report.get("ib_disconnect")
    disconnected = isinstance(ib_disconnect, Mapping) and ib_disconnect.get("status") == "SUCCEEDED"
    return {
        "activation_id": str(report["activation_id"]),
        "activated_at": _isoformat_utc(
            _report_int(report["activated_at_epoch_ms"], "activated_at_epoch_ms")
        ),
        "cancelled_orders": report["resting_order_cancels"],
        "liquidation_orders": report["liquidations"],
        "paper_engines_halted": halted,
        "ib_gateway_disconnected": bool(disconnected),
    }


def _load_guard(state_dir: Path) -> dict[str, object] | None:
    try:
        return load_last_activation(state_dir)
    except LastActivationCorruptError as error:
        raise InterfaceError(
            ErrorCategory.INTERNAL_ERROR,
            "kill-switch activation state is corrupt — refusing to treat it as "
            "never-activated (a replay would re-run the liquidate sequence); "
            "resolve the state file manually",
            type="KILL_SWITCH_STATE_CORRUPT",
            detail={"reason": str(error)},
        ) from error


class KillSwitchActivateHandler:
    """Registered for ``POST /api/v1/kill-switch`` AND ``kill-switch activate``."""

    def __init__(
        self,
        *,
        backend: KillSwitchBackend,
        system_log_store: JsonlLogStore,
        state_dir: Path,
    ) -> None:
        self._backend = backend
        self._store = system_log_store
        self._state_dir = Path(state_dir)

    def handle(self, request: Request) -> HandlerResult:
        # Defense in depth: the transport already enforces the confirmation
        # guard (428 / exit 3); a direct registry caller must not bypass it.
        if not request.confirmed:
            raise InterfaceError(
                ErrorCategory.CONFIRMATION_REQUIRED,
                "kill-switch activation requires a confirmation token (UI-4 / SRS-SAFE-001)",
            )

        replay = _load_guard(self._state_dir)
        if replay is not None:
            response = replay.get("response")
            if not isinstance(response, Mapping):
                raise InterfaceError(
                    ErrorCategory.INTERNAL_ERROR,
                    "persisted kill-switch activation record carries no response",
                    type="KILL_SWITCH_STATE_CORRUPT",
                )
            # If the original activation's durable audit writes FAILED, the
            # AC-required ACTIVATION + HALTED records never landed — retry
            # them on replay (nothing was written before, so this duplicates
            # nothing) rather than leaving the log silent forever. Still
            # failing is still surfaced; the replay guard keeps the sequence
            # itself from re-firing either way.
            if not replay.get("audit_recorded", False):
                self._retry_pending_audit(replay)
            return HandlerResult(200, dict(response))

        activation_id = f"act-{uuid.uuid4().hex[:16]}"
        activated_monotonic_ns = time.monotonic_ns()
        try:
            outcome = self._backend.activate(activation_id)
        except KillSwitchBackendError as error:
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                f"kill-switch backend could not run the activation: {error}",
                type="KILL_SWITCH_BACKEND_UNAVAILABLE",
                detail={"reason": str(error)},
            ) from error

        response = _response_from_report(outcome.report)

        # Arm the replay guard BEFORE the audit writes: whatever fails from
        # here on, a retry replays this record instead of re-firing.
        record: dict[str, object] = {
            "activation_id": activation_id,
            "response": response,
            "report": dict(outcome.report),
            "ran_clean": bool(outcome.ran_clean),
            "audit_recorded": False,
            "halted_log_latency_ms": None,
            "persisted_at_ns": time.time_ns(),
        }
        persist_last_activation(self._state_dir, record)

        try:
            self._store.write(build_activation_record(outcome.report))
            self._store.write(build_halted_record(outcome.report))
        except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "kill-switch activation RAN but the durable SRS-LOG-001 audit "
                "write failed — the HALTED transition is not observably logged "
                "(a retry will replay the persisted record, not re-fire)",
                type="KILL_SWITCH_AUDIT_WRITE_FAILED",
                detail={"reason": str(error), "response": response},
            ) from error
        halted_log_latency_ms = (time.monotonic_ns() - activated_monotonic_ns) / 1_000_000.0

        record["audit_recorded"] = True
        record["halted_log_latency_ms"] = halted_log_latency_ms
        persist_last_activation(self._state_dir, record)

        return HandlerResult(200, response)

    def _retry_pending_audit(self, replay: dict[str, object]) -> None:
        """Retry the durable ACTIVATION + HALTED writes a prior activation
        left unwritten. On success the record flips ``audit_recorded`` (the
        measured 1-second latency stays ``None`` — the original activation
        moment is long past, and fabricating a latency would be dishonest).
        Still failing re-raises the same structured error, so a silent log
        can never masquerade as an audited activation."""

        report = replay.get("report")
        if not isinstance(report, Mapping):
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "persisted kill-switch activation record carries no report — "
                "cannot write the pending SRS-LOG-001 audit records",
                type="KILL_SWITCH_STATE_CORRUPT",
            )
        try:
            self._store.write(build_activation_record(report))
            self._store.write(build_halted_record(report))
        except Exception as error:  # noqa: BLE001 - surfaced, never swallowed
            raise InterfaceError(
                ErrorCategory.INTERNAL_ERROR,
                "kill-switch activation already RAN but its durable SRS-LOG-001 "
                "audit records are STILL unwritten — the HALTED transition remains "
                "unlogged (replay guard intact; the sequence will not re-fire)",
                type="KILL_SWITCH_AUDIT_WRITE_FAILED",
                detail={"reason": str(error)},
            ) from error
        updated = dict(replay)
        updated["audit_recorded"] = True
        persist_last_activation(self._state_dir, updated)


class KillSwitchStatusHandler:
    """Registered for ``kill-switch status`` (CLI)."""

    def __init__(self, *, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)

    def handle(self, request: Request) -> HandlerResult:
        record = _load_guard(self._state_dir)
        if record is None:
            # Honest empty: never synthesize an activation that did not happen.
            return HandlerResult(200, {"activated": False, "last_activation": None})
        report = record.get("report")
        within_nfr_p3 = report.get("within_nfr_p3") if isinstance(report, Mapping) else None
        return HandlerResult(
            200,
            {
                "activated": True,
                "last_activation": {
                    "activation_id": record.get("activation_id"),
                    "response": record.get("response"),
                    "ran_clean": record.get("ran_clean"),
                    "within_nfr_p3": within_nfr_p3,
                    "audit_recorded": record.get("audit_recorded"),
                    "halted_log_latency_ms": record.get("halted_log_latency_ms"),
                    "halt_observability_budget_ms": HALT_OBSERVABILITY_BUDGET_MS,
                },
            },
        )
