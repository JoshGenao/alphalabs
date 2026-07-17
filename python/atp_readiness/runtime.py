"""SRS-MD-006 runtime readiness fold — the SYS-76 probe half of the gate.

The SDK gate (:mod:`atp_readiness.gate`, ERR-9 / SRS-ARCH-005) holds the
pre-trade state machine seeded by the STATIC configuration validator. This
module is the RUNTIME half the gate's contract deferred to SRS-MD-006: it
folds the five SYS-76 sub-check outcomes — IB Gateway connectivity/auth, IB
account data, SSD data layer + ingestion freshness, NAS reachability (with
the degraded-mode-requires-alert rule), and system service health — into an
:class:`atp_config.ReadinessReport` that
:meth:`~atp_readiness.gate.ReadinessGate.assert_runtime_ready_or_hold`
drives through the SAME pinned state machine, plus the SYS-76 paper-start
gate (market-data subscription manager + internal simulation engine).

Design (mirrors ``atp_reliability.restart`` and the MD-003 monitor):

* PURE core — no I/O, no clock reads, no subprocesses; every timestamp is
  injected and the trading calendar is a port. The I/O probe adapters live
  in :mod:`atp_readiness.probes`; the operator wiring in
  :mod:`atp_readiness.wiring`.
* Single vocabulary — :class:`~atp_reliability.restart.SubCheck` /
  ``SubCheckStatus`` / ``SubCheckResult`` / ``REQUIRED_SUBCHECKS`` and the
  pass rule :func:`~atp_reliability.restart.is_subcheck_satisfied` are
  imported from ``atp_reliability.restart`` (which imports no ``atp_*``
  package), never re-declared, so the SYS-76 semantics cannot fork.
* Fail closed everywhere — a missing or duplicate sub-check blocks; an
  unknown or absent service is unhealthy; an absent ingestion frontier is
  stale; a calendar that cannot answer raises; the alert sink is a REQUIRED
  argument (SYS-76 makes "alert the operator" first-class — a fold that can
  silently drop alerts is unrepresentable) and its failures propagate.

The concrete email/SMS fan-out behind :class:`AlertSink` belongs to
SRS-NOTIF-001 (``crates/atp-notification`` pins the Email+Sms channel set);
this port is the seam it will implement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from atp_config import ReadinessFailure, ReadinessReport
from atp_config.schema import Category, Severity
from atp_reliability.restart import (
    REQUIRED_SUBCHECKS,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
    is_subcheck_satisfied,
)

from .errors import ReadinessGateError

if TYPE_CHECKING:  # pragma: no cover — typing only; no runtime gate import
    from .gate import GateState, ReadinessGate
    from .override import OperatorOverride

__all__ = [
    "AlertSink",
    "DuplicateSubCheckError",
    "PaperPrerequisite",
    "PaperStartHoldError",
    "REQUIRED_SERVICES",
    "ReadinessAlert",
    "ReadinessAlertKind",
    "ReadinessService",
    "RuntimeReadinessError",
    "SRS_TRACE",
    "TradingCalendarPort",
    "assert_paper_ready_or_hold",
    "build_runtime_report",
    "evaluate_runtime_readiness",
    "fold_service_health",
    "ingestion_is_fresh",
    "release_hold_with_override",
]

#: SRS trace stamped on every runtime readiness failure and alert.
SRS_TRACE: tuple[str, ...] = ("SRS-MD-006", "SYS-76", "NFR-R6")

#: Maps each SYS-76 sub-check onto the closest SRS-ARCH-005 configuration
#: category (the six-member ``Category`` enum is pinned by the configuration
#: contract and deliberately not extended here). The mapping is itself pinned
#: by ``startup_readiness_runtime_contract`` so it cannot drift silently.
_SUBCHECK_CATEGORY: Mapping[SubCheck, Category] = {
    SubCheck.IB_CONNECTIVITY: Category.IB_ACCOUNT,
    SubCheck.IB_ACCOUNT: Category.IB_ACCOUNT,
    SubCheck.DATA_LAYER_SSD: Category.STORAGE_PATHS,
    SubCheck.NAS_ARCHIVAL: Category.STORAGE_PATHS,
    SubCheck.SYSTEM_SERVICES: Category.RESOURCE_LIMITS,
}


class RuntimeReadinessError(ReadinessGateError):
    """Base class for fail-closed runtime readiness errors."""


class DuplicateSubCheckError(RuntimeReadinessError):
    """The same SYS-76 sub-check was reported twice — refuse ambiguity."""


class PaperStartHoldError(RuntimeReadinessError):
    """Paper strategy startup is held (SYS-76 paper prerequisites unmet)."""

    def __init__(self, message: str, missing: tuple[str, ...]) -> None:
        super().__init__(message)
        self.missing = missing


class ReadinessService(StrEnum):
    """The five SYS-76(e) system-level services — the health-check authority."""

    EXECUTION_ENGINE = "execution_engine"
    INTERNAL_SIMULATION_ENGINE = "internal_simulation_engine"
    DATA_LAYER = "data_layer"
    NOTIFICATION_SUBSYSTEM = "notification_subsystem"
    DASHBOARD = "dashboard"


#: The COMPLETE SYS-76(e) service set, derived from the enum authority — a
#: caller cannot claim service health while silently omitting a service.
REQUIRED_SERVICES: frozenset[ReadinessService] = frozenset(ReadinessService)


class PaperPrerequisite(StrEnum):
    """SYS-76's paper-start prerequisites — BOTH must be available."""

    MARKET_DATA_SUBSCRIPTION_MANAGER = "market_data_subscription_manager"
    INTERNAL_SIMULATION_ENGINE = "internal_simulation_engine"


class ReadinessAlertKind(StrEnum):
    """Why an operator alert was dispatched (SYS-76's alert clauses)."""

    SUBCHECK_FAILURE = "subcheck_failure"
    NAS_DEGRADED_MODE = "nas_degraded_mode"
    OPERATOR_OVERRIDE = "operator_override"
    PAPER_PREREQUISITE_FAILURE = "paper_prerequisite_failure"


@dataclass(frozen=True, slots=True)
class ReadinessAlert:
    """One operator alert produced by the readiness fold."""

    kind: ReadinessAlertKind
    key: str
    reason: str
    timestamp_ns: int
    srs_trace: tuple[str, ...] = SRS_TRACE


@runtime_checkable
class AlertSink(Protocol):
    """Operator-alert dispatch port (SYS-76 "alert the operator").

    ``dispatch`` is FALLIBLE and its failures PROPAGATE: SYS-76 makes the
    alert first-class, so a fold that silently swallows a failed dispatch
    would hide exactly the outage it exists to surface. The concrete
    email/SMS fan-out (``crates/atp-notification`` Email+Sms channel set) is
    SRS-NOTIF-001's; tests and the operator CLI use recording/JSONL sinks.
    """

    def dispatch(self, alert: ReadinessAlert) -> None: ...


@runtime_checkable
class TradingCalendarPort(Protocol):
    """The minimal calendar surface the freshness boundary needs.

    ``previous_session_close_ns(now_ns)`` returns the epoch-ns close instant
    of the most recent trading session that CLOSED at or before ``now_ns``.
    The concrete adapter (over the SDK's ``UsEquityTradingCalendar``) lives
    in :mod:`atp_readiness.probes`; injecting a port keeps this core free of
    any ``atp_strategy`` import (the gate's dependency-direction contract).
    A calendar that cannot answer must RAISE — the boundary never defaults
    to fresh.
    """

    def previous_session_close_ns(self, now_ns: int) -> int: ...


def _require_ns(value: object, what: str) -> int:
    """Validate an epoch-ns value: a non-negative real int (bool rejected)."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeReadinessError(
            f"{what} must be a non-negative integer epoch-ns; got {value!r}"
        )
    return value


def ingestion_is_fresh(
    frontier_ns: int | None,
    *,
    now_ns: int,
    calendar: TradingCalendarPort,
) -> bool:
    """SYS-76(c) boundary: "the most recent ingestion date is not stale by
    more than one trading day".

    THE single predicate (MD-003 ``heartbeat_age_ns_is_stale`` discipline):
    fresh iff the ingestion frontier reaches the close of the most recent
    COMPLETED trading session at or before ``now_ns`` — exactly at the
    session-close instant is fresh; one nanosecond before it is stale.
    Weekends/holidays cost nothing because the boundary is defined by
    trading sessions, not wall-clock days. ``frontier_ns is None`` (no
    ingestion evidence at all) is STALE — no data is not fresh data.
    """

    now_ns = _require_ns(now_ns, "now_ns")
    if frontier_ns is None:
        return False
    frontier_ns = _require_ns(frontier_ns, "frontier_ns")
    boundary_ns = _require_ns(
        calendar.previous_session_close_ns(now_ns), "previous_session_close_ns"
    )
    return frontier_ns >= boundary_ns


def fold_service_health(statuses: Mapping[ReadinessService, bool]) -> SubCheckResult:
    """SYS-76(e): all five services healthy ⇒ PASS; anything else ⇒ FAIL.

    Fail closed: a service ABSENT from ``statuses`` is unhealthy (unknown is
    never healthy), and a key outside :class:`ReadinessService` is refused —
    a caller cannot pad the mapping with look-alike names.
    """

    for key in statuses:
        if not isinstance(key, ReadinessService):
            raise RuntimeReadinessError(
                f"unknown service key {key!r}; service health is keyed by ReadinessService"
            )
    unhealthy = sorted(
        service.value for service in REQUIRED_SERVICES if not statuses.get(service, False)
    )
    if unhealthy:
        return SubCheckResult(check=SubCheck.SYSTEM_SERVICES, status=SubCheckStatus.FAIL)
    return SubCheckResult(check=SubCheck.SYSTEM_SERVICES, status=SubCheckStatus.PASS)


def build_runtime_report(
    results: Sequence[SubCheckResult],
    *,
    alert_sink: AlertSink,
    timestamp_ns: int,
) -> ReadinessReport:
    """Fold the five SYS-76 sub-check results into a ``ReadinessReport``.

    * The required set comes from :data:`REQUIRED_SUBCHECKS` (the enum
      authority), never from the caller's list: a MISSING sub-check is an
      error-severity failure (fail closed), a DUPLICATE raises.
    * Each present result is judged by
      :func:`~atp_reliability.restart.is_subcheck_satisfied` — the single
      source of the NAS degraded-with-alert rule.
    * Every failing sub-check produces a failure entry AND dispatches a
      :class:`ReadinessAlert` through ``alert_sink`` (SYS-76: "the system
      shall alert the operator and hold in a pre-trade state").
    * A NAS ``DEGRADED`` result that PASSES (``alert_raised=True``) is not a
      failure, but SYS-76(d) still requires the degraded-mode operator
      alert: one is dispatched and an evidence line records the mode.
    """

    timestamp_ns = _require_ns(timestamp_ns, "timestamp_ns")
    seen: dict[SubCheck, SubCheckResult] = {}
    for result in results:
        if result.check in seen:
            raise DuplicateSubCheckError(
                f"sub-check {result.check.value!r} reported more than once"
            )
        seen[result.check] = result

    failures: list[ReadinessFailure] = []
    evidence: list[str] = []
    for check in sorted(REQUIRED_SUBCHECKS, key=lambda c: c.value):
        observed = seen.get(check)
        if observed is None:
            reason = (
                f"SYS-76 sub-check {check.value!r} was not observed — an unobserved "
                "readiness check cannot pass (fail closed)"
            )
            failures.append(
                ReadinessFailure(
                    key=check.value,
                    category=_SUBCHECK_CATEGORY[check],
                    severity=Severity.ERROR,
                    reason=reason,
                    srs_trace=SRS_TRACE,
                )
            )
            alert_sink.dispatch(
                ReadinessAlert(
                    kind=ReadinessAlertKind.SUBCHECK_FAILURE,
                    key=check.value,
                    reason=reason,
                    timestamp_ns=timestamp_ns,
                )
            )
            continue
        if is_subcheck_satisfied(observed):
            evidence.append(f"SYS-76 sub-check {check.value}: {observed.status.value}")
            if check is SubCheck.NAS_ARCHIVAL and observed.status is SubCheckStatus.DEGRADED:
                # Degraded-mode operation is acceptable ONLY with the
                # operator alert — dispatch it even though the gate passes.
                alert_sink.dispatch(
                    ReadinessAlert(
                        kind=ReadinessAlertKind.NAS_DEGRADED_MODE,
                        key=check.value,
                        reason=(
                            "NAS archival tier unreachable; operating in degraded "
                            "mode (SYS-76(d)) — SSD-only until NAS recovers"
                        ),
                        timestamp_ns=timestamp_ns,
                    )
                )
                evidence.append(
                    "SYS-76(d) degraded-mode operator alert dispatched (NAS unreachable)"
                )
            continue
        reason = (
            f"SYS-76 sub-check {check.value!r} did not pass "
            f"(status {observed.status.value!r}"
            + (
                ", degraded without operator alert"
                if observed.status is SubCheckStatus.DEGRADED
                else ""
            )
            + ")"
        )
        failures.append(
            ReadinessFailure(
                key=check.value,
                category=_SUBCHECK_CATEGORY[check],
                severity=Severity.ERROR,
                reason=reason,
                srs_trace=SRS_TRACE,
            )
        )
        alert_sink.dispatch(
            ReadinessAlert(
                kind=ReadinessAlertKind.SUBCHECK_FAILURE,
                key=check.value,
                reason=reason,
                timestamp_ns=timestamp_ns,
            )
        )
    return ReadinessReport(failures=failures, evidence=evidence)


def assert_paper_ready_or_hold(
    availability: Mapping[PaperPrerequisite, bool],
    *,
    alert_sink: AlertSink,
    timestamp_ns: int,
) -> None:
    """SYS-76 paper gate: paper strategies may start only once the market
    data subscription manager AND the internal simulation engine are
    available. A prerequisite absent from ``availability`` is unavailable
    (fail closed). A hold dispatches an operator alert, then raises
    :class:`PaperStartHoldError` naming the missing prerequisites.
    """

    timestamp_ns = _require_ns(timestamp_ns, "timestamp_ns")
    for key in availability:
        if not isinstance(key, PaperPrerequisite):
            raise RuntimeReadinessError(
                f"unknown paper prerequisite {key!r}; keyed by PaperPrerequisite"
            )
    missing = tuple(
        prerequisite.value
        for prerequisite in sorted(PaperPrerequisite, key=lambda p: p.value)
        if not availability.get(prerequisite, False)
    )
    if not missing:
        return
    reason = "paper strategy startup held: prerequisite(s) unavailable: " + ", ".join(missing)
    alert_sink.dispatch(
        ReadinessAlert(
            kind=ReadinessAlertKind.PAPER_PREREQUISITE_FAILURE,
            key=",".join(missing),
            reason=reason,
            timestamp_ns=timestamp_ns,
        )
    )
    raise PaperStartHoldError(reason, missing)


def release_hold_with_override(
    gate: "ReadinessGate", override: "OperatorOverride", *, alert_sink: AlertSink
) -> None:
    """Release a pre-trade hold via operator override, WITH the alert.

    SYS-76: a failure holds pre-trade "until the failure is resolved or
    manually overridden" — and an override is itself operator-alertable
    (the human bypassed a failed readiness check). Ordering (Codex R2): the
    mandatory OPERATOR_OVERRIDE alert is delivered FIRST; only then does the
    gate's audited transition commit. A sink failure therefore leaves the
    hold IN PLACE (fail closed — a released pre-trade hold must never exist
    without its operator alert). If the subsequent audited transition is
    refused (incomplete audit fields, wrong source state), the delivered
    alert stands as the audit artifact of the ATTEMPT and the hold remains.
    """

    alert_sink.dispatch(
        ReadinessAlert(
            kind=ReadinessAlertKind.OPERATOR_OVERRIDE,
            key=override.audit_trail_id,
            reason=(f"pre-trade hold override by {override.actor}: {override.reason}"),
            timestamp_ns=override.timestamp_ns,
        )
    )
    gate.operator_override(override)


def evaluate_runtime_readiness(
    gate: "ReadinessGate",
    env: Mapping[str, str],
    results: Sequence[SubCheckResult],
    *,
    alert_sink: AlertSink,
    timestamp_ns: int,
    atp_env: str | None = None,
) -> "GateState":
    """One full readiness evaluation: static half + runtime half.

    Re-runs the SRS-ARCH-005 static validator (``gate.reevaluate``) and then
    folds the runtime sub-check results through
    :meth:`~atp_readiness.gate.ReadinessGate.assert_runtime_ready_or_hold`.
    Both halves are recomputed on every call, so a later poll can never wipe
    a runtime failure with a stale static PASS (or vice versa). Returns the
    gate's resulting state; raises ``PreTradeHoldError`` when held (the
    fold has already dispatched the per-failure operator alerts).

    Override lifetime (Codex R3): SYS-76 holds "until the failure is
    resolved or manually overridden" — an audited ``OVERRIDDEN`` gate must
    not be silently demoted by the next poll re-observing the SAME runtime
    condition the operator already bypassed. While the gate is
    ``OVERRIDDEN``: a STATIC configuration regression still demotes it (the
    SDK contract's audited behaviour — an override does not bless config
    drift); persisting or new RUNTIME failures leave the override standing
    (each evaluation still dispatches their operator alerts, so the bypass
    stays loudly visible); a fully clean evaluation promotes to ``READY``,
    ending the override's tenure through the normal transition.
    """

    from .gate import GateState, ReadinessGate  # local: typing cycle avoidance

    if gate.state is GateState.OVERRIDDEN:
        report = build_runtime_report(results, alert_sink=alert_sink, timestamp_ns=timestamp_ns)
        # Probe the static half WITHOUT touching the audited override state:
        # a throwaway gate evaluates the same env.
        static_probe = ReadinessGate.from_env(env, atp_env=atp_env)
        if static_probe.state is GateState.PRE_TRADE_BLOCKED:
            # A static configuration regression is NEW evidence the override
            # never covered — demote through the audited path.
            gate.reevaluate(env, atp_env=atp_env)
            gate.assert_ready_or_hold()
        if not report.ok:
            # Runtime failures persist under the audited override: the
            # override stands (their alerts were dispatched above).
            return gate.state
        gate.reevaluate(env, atp_env=atp_env)
        gate.assert_runtime_ready_or_hold(report)
        return gate.state

    gate.reevaluate(env, atp_env=atp_env)
    report = build_runtime_report(results, alert_sink=alert_sink, timestamp_ns=timestamp_ns)
    gate.assert_runtime_ready_or_hold(report)
    return gate.state
