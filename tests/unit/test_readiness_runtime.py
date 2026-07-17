"""L1 unit tests for the SRS-MD-006 runtime readiness fold (pure core)."""

from __future__ import annotations

import pytest
from atp_readiness.runtime import (
    REQUIRED_SERVICES,
    DuplicateSubCheckError,
    PaperPrerequisite,
    PaperStartHoldError,
    ReadinessAlertKind,
    ReadinessService,
    RuntimeReadinessError,
    assert_paper_ready_or_hold,
    build_runtime_report,
    fold_service_health,
    ingestion_is_fresh,
    release_hold_with_override,
)
from atp_reliability.restart import (
    REQUIRED_SUBCHECKS,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
)

T0 = 1_700_000_000_000_000_000


class RecordingSink:
    def __init__(self) -> None:
        self.alerts = []

    def dispatch(self, alert) -> None:
        self.alerts.append(alert)


class RaisingSink:
    def dispatch(self, alert) -> None:
        raise OSError("alert channel down")


class FixedCalendar:
    def __init__(self, close_ns: int) -> None:
        self.close_ns = close_ns

    def previous_session_close_ns(self, now_ns: int) -> int:
        return self.close_ns


def all_pass():
    return [
        SubCheckResult(check=c, status=SubCheckStatus.PASS)
        for c in sorted(REQUIRED_SUBCHECKS, key=lambda c: c.value)
    ]


def with_status(check: SubCheck, status: SubCheckStatus, alert_raised: bool = False):
    results = [r for r in all_pass() if r.check is not check]
    results.append(SubCheckResult(check=check, status=status, alert_raised=alert_raised))
    return results


# --------------------------------------------------------------------------- #
# Freshness boundary (SYS-76(c): not stale by more than one trading day)
# --------------------------------------------------------------------------- #


def test_freshness_boundary_is_exact_at_previous_session_close() -> None:
    calendar = FixedCalendar(T0)
    assert ingestion_is_fresh(T0, now_ns=T0 + 1, calendar=calendar)
    assert ingestion_is_fresh(T0 + 5, now_ns=T0 + 10, calendar=calendar)
    assert not ingestion_is_fresh(T0 - 1, now_ns=T0 + 1, calendar=calendar)


def test_no_ingestion_evidence_is_stale() -> None:
    assert not ingestion_is_fresh(None, now_ns=T0, calendar=FixedCalendar(T0))


@pytest.mark.parametrize("bad", [True, -1, 1.5, "x"])
def test_freshness_rejects_degenerate_timestamps(bad) -> None:
    with pytest.raises(RuntimeReadinessError):
        ingestion_is_fresh(bad, now_ns=T0, calendar=FixedCalendar(T0))  # type: ignore[arg-type]


def test_calendar_failure_propagates_never_defaults_fresh() -> None:
    class BrokenCalendar:
        def previous_session_close_ns(self, now_ns: int) -> int:
            raise RuntimeError("calendar source unavailable")

    with pytest.raises(RuntimeError):
        ingestion_is_fresh(T0, now_ns=T0, calendar=BrokenCalendar())


# --------------------------------------------------------------------------- #
# Service-health fold (SYS-76(e))
# --------------------------------------------------------------------------- #


def test_all_five_services_healthy_passes() -> None:
    result = fold_service_health({s: True for s in REQUIRED_SERVICES})
    assert result.status is SubCheckStatus.PASS


@pytest.mark.parametrize("absent", list(ReadinessService))
def test_missing_or_unhealthy_service_fails_closed(absent: ReadinessService) -> None:
    statuses = {s: True for s in REQUIRED_SERVICES if s is not absent}
    assert fold_service_health(statuses).status is SubCheckStatus.FAIL
    statuses[absent] = False
    assert fold_service_health(statuses).status is SubCheckStatus.FAIL


def test_unknown_service_key_is_refused() -> None:
    with pytest.raises(RuntimeReadinessError):
        fold_service_health({"look_alike_service": True})  # type: ignore[dict-item]


# --------------------------------------------------------------------------- #
# The fold (build_runtime_report)
# --------------------------------------------------------------------------- #


def test_all_pass_fold_is_ok_with_no_alerts() -> None:
    sink = RecordingSink()
    report = build_runtime_report(all_pass(), alert_sink=sink, timestamp_ns=T0)
    assert report.ok and not sink.alerts
    assert len(report.evidence) == len(REQUIRED_SUBCHECKS)


@pytest.mark.parametrize("failing", sorted(REQUIRED_SUBCHECKS, key=lambda c: c.value))
def test_each_single_subcheck_failure_blocks_and_alerts(failing: SubCheck) -> None:
    sink = RecordingSink()
    report = build_runtime_report(
        with_status(failing, SubCheckStatus.FAIL), alert_sink=sink, timestamp_ns=T0
    )
    assert not report.ok
    assert [f.key for f in report.errors] == [failing.value]
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.SUBCHECK_FAILURE]
    assert sink.alerts[0].key == failing.value


def test_missing_subcheck_fails_closed_with_alert() -> None:
    sink = RecordingSink()
    report = build_runtime_report(all_pass()[:-1], alert_sink=sink, timestamp_ns=T0)
    assert not report.ok and len(sink.alerts) == 1
    assert "not observed" in report.errors[0].reason


def test_duplicate_subcheck_is_refused() -> None:
    with pytest.raises(DuplicateSubCheckError):
        build_runtime_report(
            all_pass() + [all_pass()[0]], alert_sink=RecordingSink(), timestamp_ns=T0
        )


def test_nas_degraded_passes_only_with_alert_and_still_alerts() -> None:
    sink = RecordingSink()
    report = build_runtime_report(
        with_status(SubCheck.NAS_ARCHIVAL, SubCheckStatus.DEGRADED, alert_raised=True),
        alert_sink=sink,
        timestamp_ns=T0,
    )
    assert report.ok, "SYS-76(d): degraded WITH alert is acceptable"
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.NAS_DEGRADED_MODE]

    report = build_runtime_report(
        with_status(SubCheck.NAS_ARCHIVAL, SubCheckStatus.DEGRADED, alert_raised=False),
        alert_sink=RecordingSink(),
        timestamp_ns=T0,
    )
    assert not report.ok, "degraded WITHOUT the operator alert must fail"


def test_degraded_on_a_non_nas_subcheck_fails() -> None:
    report = build_runtime_report(
        with_status(SubCheck.IB_CONNECTIVITY, SubCheckStatus.DEGRADED, alert_raised=True),
        alert_sink=RecordingSink(),
        timestamp_ns=T0,
    )
    assert not report.ok


def test_alert_sink_failure_propagates() -> None:
    with pytest.raises(OSError):
        build_runtime_report(
            with_status(SubCheck.IB_ACCOUNT, SubCheckStatus.FAIL),
            alert_sink=RaisingSink(),
            timestamp_ns=T0,
        )


# --------------------------------------------------------------------------- #
# Paper gate + override
# --------------------------------------------------------------------------- #


def test_paper_gate_requires_both_prerequisites() -> None:
    sink = RecordingSink()
    assert_paper_ready_or_hold(
        {p: True for p in PaperPrerequisite}, alert_sink=sink, timestamp_ns=T0
    )
    assert not sink.alerts

    sink = RecordingSink()
    with pytest.raises(PaperStartHoldError) as held:
        assert_paper_ready_or_hold(
            {PaperPrerequisite.INTERNAL_SIMULATION_ENGINE: True},
            alert_sink=sink,
            timestamp_ns=T0,
        )
    assert held.value.missing == ("market_data_subscription_manager",)
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.PAPER_PREREQUISITE_FAILURE]


def test_paper_gate_missing_key_is_unavailable() -> None:
    with pytest.raises(PaperStartHoldError) as held:
        assert_paper_ready_or_hold({}, alert_sink=RecordingSink(), timestamp_ns=T0)
    assert set(held.value.missing) == {p.value for p in PaperPrerequisite}


def test_override_release_alerts_after_audit_commits() -> None:
    from atp_config import REQUIRED_KEYS
    from atp_readiness import GateState, ReadinessGate
    from atp_readiness.errors import PreTradeHoldError
    from atp_readiness.override import OperatorOverride

    env = {s.name: s.default for s in REQUIRED_KEYS if s.default is not None}
    gate = ReadinessGate.from_env(env)
    failing = build_runtime_report(
        with_status(SubCheck.IB_CONNECTIVITY, SubCheckStatus.FAIL),
        alert_sink=RecordingSink(),
        timestamp_ns=T0,
    )
    with pytest.raises(PreTradeHoldError):
        gate.assert_runtime_ready_or_hold(failing)
    assert gate.state is GateState.PRE_TRADE_BLOCKED

    override = OperatorOverride(
        actor="ops", reason="known IB outage", audit_trail_id="AUD-1", timestamp_ns=T0
    )
    sink = RecordingSink()
    release_hold_with_override(gate, override, alert_sink=sink)
    assert gate.state is GateState.OVERRIDDEN
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.OPERATOR_OVERRIDE]
    assert sink.alerts[0].key == "AUD-1"

    # Ordering (Codex R2): with a RAISING sink the alert is NOT delivered,
    # so the hold must remain in place — a released pre-trade hold must
    # never exist without its operator alert.
    gate2 = ReadinessGate.from_env(env)
    with pytest.raises(PreTradeHoldError):
        gate2.assert_runtime_ready_or_hold(failing)
    override2 = OperatorOverride(
        actor="ops", reason="known IB outage", audit_trail_id="AUD-2", timestamp_ns=T0
    )
    with pytest.raises(OSError):
        release_hold_with_override(gate2, override2, alert_sink=RaisingSink())
    assert gate2.state is GateState.PRE_TRADE_BLOCKED, "undelivered alert => hold stays"
    assert gate2.overrides == (), "no override may be recorded without its alert"
