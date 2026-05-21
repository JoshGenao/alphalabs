"""``ReadinessGate`` — pre-trade hold state machine for ERR-9 / SRS-MD-006.

The gate consumes the structured :class:`atp_config.ReadinessReport` produced
by :func:`atp_config.load_and_validate` and exposes:

* A discrete :class:`GateState` — ``INITIALIZING`` -> (``PRE_TRADE_BLOCKED``
  | ``READY``); ``PRE_TRADE_BLOCKED`` may transition to ``READY`` (on a
  re-evaluation that clears the errors) or to ``OVERRIDDEN`` (on a
  fully-audited operator override).
* Structured payload renderers (``as_dashboard_payload``, ``as_log_records``)
  that the downstream SRS-LOG-001 / SRS-UI-001 / SRS-API-001 surfaces consume
  when they land.
* An ``assert_ready_or_hold`` gate that raises
  :class:`atp_readiness.errors.PreTradeHoldError` while the state is held —
  the executable signal for service boot scripts.

The runtime readiness checks ERR-9 also references (IB connectivity, IB
account auth, SSD data layer access, ingestion freshness within one trading
day, system service health, NAS reachability) live in SRS-MD-006 and are
DEFERRED here; the gate's contract surface is the seam those checks will
plug into.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from atp_config import (
    ReadinessReport,
    Severity,
    load_and_validate,
)

from .errors import (
    GateTransitionError,
    OverrideAuditError,
    PreTradeHoldError,
)
from .override import OperatorOverride


class GateState(StrEnum):
    """Discrete states of the startup readiness gate.

    * ``INITIALIZING`` — before any readiness evaluation has been recorded.
    * ``PRE_TRADE_BLOCKED`` — at least one error-severity failure is present;
      live and paper order submission must be held per ERR-9.
    * ``READY`` — the SDK-surface half of readiness passes; the runtime half
      (SRS-MD-006) still gates live trading.
    * ``OVERRIDDEN`` — an operator has released the pre-trade hold with a
      fully-audited :class:`OperatorOverride`; SRS-LOG-001 + SRS-NOTIF-001
      consume the audit-trail record when they land.
    """

    INITIALIZING = "initializing"
    PRE_TRADE_BLOCKED = "pre_trade_blocked"
    READY = "ready"
    OVERRIDDEN = "overridden"


_ALLOWED_TRANSITIONS: frozenset[tuple[GateState, GateState]] = frozenset(
    {
        (GateState.INITIALIZING, GateState.PRE_TRADE_BLOCKED),
        (GateState.INITIALIZING, GateState.READY),
        (GateState.PRE_TRADE_BLOCKED, GateState.PRE_TRADE_BLOCKED),
        (GateState.PRE_TRADE_BLOCKED, GateState.READY),
        (GateState.PRE_TRADE_BLOCKED, GateState.OVERRIDDEN),
        (GateState.READY, GateState.PRE_TRADE_BLOCKED),
        (GateState.READY, GateState.READY),
        (GateState.OVERRIDDEN, GateState.PRE_TRADE_BLOCKED),
        (GateState.OVERRIDDEN, GateState.READY),
    }
)
"""Allowed (from_state, to_state) transitions.

The set is intentionally enumerated rather than computed so the contract
metadata block (``architecture/runtime_services.json#startup_readiness_gate_contract.allowed_transitions``)
and the gate stay in pinned parity — the L3 contract test cross-checks this
set against the JSON catalogue.
"""


_OVERRIDE_REQUIRED_FIELDS: tuple[str, ...] = (
    "actor",
    "reason",
    "audit_trail_id",
    "timestamp_ns",
)
"""SRS-MD-006 audit-trail fields the gate enforces on every operator override."""


class ReadinessGate:
    """State machine enforcing the ERR-9 pre-trade hold contract.

    Construct via :meth:`ReadinessGate.from_env` (the typical boot path) or
    via the default constructor for tests. The gate is single-threaded by
    design: every transition method assumes the caller owns the gate.
    """

    def __init__(self) -> None:
        self._state: GateState = GateState.INITIALIZING
        self._report: ReadinessReport | None = None
        self._overrides: list[OperatorOverride] = []

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #

    @classmethod
    def from_env(cls, env: Mapping[str, str], *, atp_env: str | None = None) -> ReadinessGate:
        """Build a gate seeded with the result of the SRS-ARCH-005 validator.

        Calls :func:`atp_config.load_and_validate` on ``env`` and transitions
        ``INITIALIZING`` -> ``PRE_TRADE_BLOCKED`` (when the report contains
        any error-severity failure) or ``INITIALIZING`` -> ``READY``.
        """

        _assert_env_is_mapping(env)
        gate = cls()
        report = load_and_validate(env, atp_env=atp_env)
        gate._apply_report(report)
        return gate

    # ------------------------------------------------------------------ #
    # Public accessors
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> GateState:
        """Current gate state."""

        return self._state

    @property
    def report(self) -> ReadinessReport:
        """Latest readiness report.

        Raises :class:`RuntimeError` when accessed before any evaluation has
        seeded the gate (i.e. while ``state`` is still ``INITIALIZING``).
        """

        if self._report is None:
            raise RuntimeError("ReadinessGate has no report yet; call from_env or reevaluate first")
        return self._report

    @property
    def overrides(self) -> tuple[OperatorOverride, ...]:
        """Immutable snapshot of the recorded operator overrides, oldest first."""

        return tuple(self._overrides)

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

    def reevaluate(self, env: Mapping[str, str], *, atp_env: str | None = None) -> None:
        """Re-run the SRS-ARCH-005 validator and apply the new report.

        Allowed source states: any state except ``INITIALIZING`` (the gate
        must have been seeded by :meth:`from_env` first). Re-evaluation does
        not clear recorded operator overrides — the audit trail is durable.
        """

        if self._state is GateState.INITIALIZING:
            raise GateTransitionError(
                "reevaluate requires a seeded gate; call ReadinessGate.from_env first"
            )
        _assert_env_is_mapping(env)
        report = load_and_validate(env, atp_env=atp_env)
        self._apply_report(report)

    def operator_override(self, override: OperatorOverride) -> None:
        """Release the pre-trade hold with a fully-audited operator override.

        Allowed source state: ``PRE_TRADE_BLOCKED`` only. The override must
        be a real :class:`OperatorOverride` instance with non-empty
        ``actor`` / ``reason`` / ``audit_trail_id`` and a finite,
        non-negative integer ``timestamp_ns``. ``bool`` is rejected on
        ``timestamp_ns`` because Python treats it as an ``int`` subclass.
        Successful overrides are appended to the gate's audit log in the
        order they were issued.
        """

        if not isinstance(override, OperatorOverride):
            raise OverrideAuditError(
                f"operator_override expected an OperatorOverride; got {type(override).__name__}"
            )

        self._assert_override_audit_complete(override)
        self._assert_transition(self._state, GateState.OVERRIDDEN)
        self._overrides.append(override)
        self._state = GateState.OVERRIDDEN

    def assert_ready_or_hold(self) -> None:
        """Raise :class:`PreTradeHoldError` when the gate is held.

        ``READY`` and ``OVERRIDDEN`` pass; ``PRE_TRADE_BLOCKED`` raises;
        ``INITIALIZING`` raises because a gate that has not yet been
        evaluated cannot be considered ready.
        """

        if self._state is GateState.INITIALIZING:
            raise GateTransitionError(
                "ReadinessGate is INITIALIZING; call from_env or reevaluate first"
            )
        if self._state is GateState.PRE_TRADE_BLOCKED:
            raise PreTradeHoldError(self.report)

    # ------------------------------------------------------------------ #
    # Structured payload renderers
    # ------------------------------------------------------------------ #

    def as_log_records(self) -> list[dict[str, Any]]:
        """Render the gate state as the JSON-line payload SRS-LOG-001 consumes.

        Each record carries ``timestamp_ns``, ``severity``, ``key``,
        ``category``, ``reason``, and ``srs_trace`` (the fields enumerated in
        ``architecture/runtime_services.json#startup_readiness_gate_contract.required_log_record_fields``).
        Overrides are emitted as ``severity='override'`` records so the same
        log surface can thread them into the audit log when SRS-LOG-001
        lands.
        """

        timestamp_ns = time.time_ns()
        records: list[dict[str, Any]] = []
        for failure in self.report.failures:
            records.append(
                {
                    "timestamp_ns": timestamp_ns,
                    "severity": failure.severity.value,
                    "key": failure.key,
                    "category": failure.category.value,
                    "reason": failure.reason,
                    "srs_trace": list(failure.srs_trace),
                }
            )
        for override in self._overrides:
            records.append(
                {
                    "timestamp_ns": override.timestamp_ns,
                    "severity": "override",
                    "key": "operator_override",
                    "category": "pre_trade_release",
                    "reason": override.reason,
                    "srs_trace": ["ERR-9", "SRS-MD-006", "SRS-LOG-001"],
                    "actor": override.actor,
                    "audit_trail_id": override.audit_trail_id,
                }
            )
        return records

    def as_dashboard_payload(self) -> dict[str, Any]:
        """Render the gate state as the JSON document SRS-UI-001 / SRS-API-001 consume.

        Fields enumerated in
        ``architecture/runtime_services.json#startup_readiness_gate_contract.required_dashboard_payload_fields``.
        ``ok`` is true when the gate has reached ``READY`` or ``OVERRIDDEN``;
        ``state`` is always the current :class:`GateState` value.
        """

        report = self.report
        return {
            "state": self._state.value,
            "ok": self._state in (GateState.READY, GateState.OVERRIDDEN),
            "errors": [f.as_dict() for f in report.errors],
            "warnings": [f.as_dict() for f in report.warnings],
            "evidence": list(report.evidence),
            "overrides": [o.as_dict() for o in self._overrides],
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _apply_report(self, report: ReadinessReport) -> None:
        target = GateState.READY if not _has_error(report) else GateState.PRE_TRADE_BLOCKED
        self._assert_transition(self._state, target)
        self._report = report
        self._state = target

    def _assert_transition(self, from_state: GateState, to_state: GateState) -> None:
        if (from_state, to_state) not in _ALLOWED_TRANSITIONS:
            raise GateTransitionError(
                f"forbidden gate transition: {from_state.value} -> {to_state.value}"
            )

    def _assert_override_audit_complete(self, override: OperatorOverride) -> None:
        for field_name in _OVERRIDE_REQUIRED_FIELDS:
            if not hasattr(override, field_name):
                raise OverrideAuditError(
                    f"OperatorOverride is missing required audit field {field_name!r}"
                )
        for field_name in ("actor", "reason", "audit_trail_id"):
            value = getattr(override, field_name)
            if not isinstance(value, str):
                raise OverrideAuditError(
                    f"OperatorOverride.{field_name} must be a non-empty string; "
                    f"got {type(value).__name__}"
                )
            if not value.strip():
                raise OverrideAuditError(f"OperatorOverride.{field_name} must be non-empty")
        timestamp_ns = override.timestamp_ns
        if isinstance(timestamp_ns, bool) or not isinstance(timestamp_ns, int):
            raise OverrideAuditError(
                "OperatorOverride.timestamp_ns must be a non-negative int "
                "(bool is rejected); got "
                f"{type(timestamp_ns).__name__}"
            )
        if timestamp_ns < 0 or not math.isfinite(timestamp_ns):
            raise OverrideAuditError(
                "OperatorOverride.timestamp_ns must be a non-negative finite "
                f"integer; got {timestamp_ns!r}"
            )


def _has_error(report: ReadinessReport) -> bool:
    return any(f.severity is Severity.ERROR for f in report.failures)


def _assert_env_is_mapping(env: Any) -> None:
    if not isinstance(env, Mapping):
        raise TypeError(
            "ReadinessGate.from_env / reevaluate require a Mapping[str, str]; "
            f"got {type(env).__name__}"
        )


__all__ = ["GateState", "ReadinessGate"]
