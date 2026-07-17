"""SRS-MD-006 / SyRS SYS-76 / NFR-R6 — the startup readiness check before
enabling live trading, exercised end to end through the operator runtime.

L7 domain (safety) tests, one per acceptance clause:

* live strategy startup is BLOCKED until IB connectivity/authentication, IB
  account data, SSD data-layer access, ingestion freshness within one
  trading day, system service health, and NAS reachability all pass — and
  becomes ready the moment they do (driven through the real
  ``readiness wait`` CLI operation registered by ``wire_readiness``);
* NAS unreachable ⇒ degraded-mode operation is acceptable ONLY with an
  operator alert;
* ingestion stale beyond one trading day holds the gate;
* paper strategies may start only after the market-data subscription
  manager AND the internal simulation engine are available;
* a failure holds pre-trade unless manually overridden, and the override
  itself alerts the operator;
* a failing alert sink is surfaced, never swallowed.
"""

from __future__ import annotations

import pytest
from atp_config import REQUIRED_KEYS
from atp_readiness import GateState, ReadinessGate
from atp_readiness.override import OperatorOverride
from atp_readiness.runtime import (
    PaperPrerequisite,
    PaperStartHoldError,
    ReadinessAlertKind,
    assert_paper_ready_or_hold,
    build_runtime_report,
    evaluate_runtime_readiness,
    release_hold_with_override,
)
from atp_readiness.wiring import wire_readiness
from atp_reliability.restart import (
    REQUIRED_SUBCHECKS,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
)
from atp_runtime import OperatorInterfaceRuntime

pytestmark = [pytest.mark.domain, pytest.mark.safety]

T0 = 1_700_000_000_000_000_000


class RecordingSink:
    def __init__(self) -> None:
        self.alerts = []

    def dispatch(self, alert) -> None:
        self.alerts.append(alert)


def _env() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


def _all_pass() -> list[SubCheckResult]:
    return [
        SubCheckResult(check=c, status=SubCheckStatus.PASS)
        for c in sorted(REQUIRED_SUBCHECKS, key=lambda c: c.value)
    ]


def _one_failing(failing: SubCheck) -> list[SubCheckResult]:
    results = [r for r in _all_pass() if r.check is not failing]
    results.append(SubCheckResult(check=failing, status=SubCheckStatus.FAIL))
    return results


def _wired_runtime(results_box: dict, sink: RecordingSink):
    """A real operator runtime with the readiness-wait handler wired over a
    mutable probe-result box (the fixture/mocks verification context)."""

    env = _env()
    gate = ReadinessGate.from_env(env)
    runtime = OperatorInterfaceRuntime()
    wire_readiness(
        runtime,
        gate=gate,
        env=env,
        collect_results=lambda: results_box["results"],
        alert_sink=sink,
        now_ns=lambda: T0,
        poll_interval_s=0.0,
    )
    return runtime, gate


def _cli_wait(runtime: OperatorInterfaceRuntime, timeout: str = "0") -> int:
    import io

    out = io.StringIO()
    return runtime.cli_dispatcher().dispatch(
        ["readiness", "wait", "--timeout", timeout], stdout=out
    )


def test_live_startup_blocked_until_every_sys76_context_passes() -> None:
    # Walk every one of the five sub-checks: while ANY fails, `readiness
    # wait` (the launch surface's blocking consumer) times out non-ready;
    # when all pass, it returns ready with exit OK.
    sink = RecordingSink()
    for failing in sorted(REQUIRED_SUBCHECKS, key=lambda c: c.value):
        box = {"results": _one_failing(failing)}
        runtime, gate = _wired_runtime(box, sink)
        exit_code = _cli_wait(runtime)
        assert exit_code != 0, f"{failing.value} failing must block live startup"
        assert gate.state is GateState.PRE_TRADE_BLOCKED
        # The failure is addressable in the held payload (operator-visible).
        held_keys = {f["key"] for f in gate.as_dashboard_payload()["errors"]}
        assert failing.value in held_keys

    box = {"results": _all_pass()}
    runtime, gate = _wired_runtime(box, RecordingSink())
    exit_code = _cli_wait(runtime)
    assert exit_code == 0
    assert gate.state is GateState.READY


def test_recovery_unblocks_the_waiting_launch() -> None:
    # The wait loop re-evaluates BOTH halves per poll: a probe set that
    # recovers mid-wait releases the launch without operator action.
    sink = RecordingSink()
    box = {"results": _one_failing(SubCheck.IB_CONNECTIVITY)}
    runtime, gate = _wired_runtime(box, sink)
    exit_code = _cli_wait(runtime)
    assert exit_code != 0

    box["results"] = _all_pass()
    exit_code = _cli_wait(runtime)
    assert exit_code == 0 and gate.state is GateState.READY


def test_nas_unreachable_degraded_mode_requires_operator_alert() -> None:
    def nas(status: SubCheckStatus, alert_raised: bool) -> list[SubCheckResult]:
        results = [r for r in _all_pass() if r.check is not SubCheck.NAS_ARCHIVAL]
        results.append(
            SubCheckResult(check=SubCheck.NAS_ARCHIVAL, status=status, alert_raised=alert_raised)
        )
        return results

    # Degraded WITH the alert: acceptable — and the SYS-76(d) alert fires.
    sink = RecordingSink()
    report = build_runtime_report(
        nas(SubCheckStatus.DEGRADED, True), alert_sink=sink, timestamp_ns=T0
    )
    assert report.ok
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.NAS_DEGRADED_MODE]

    # Degraded WITHOUT the alert: not acceptable — the gate must hold.
    report = build_runtime_report(
        nas(SubCheckStatus.DEGRADED, False), alert_sink=RecordingSink(), timestamp_ns=T0
    )
    assert not report.ok


def test_ingestion_stale_beyond_one_trading_day_holds() -> None:
    from atp_readiness.runtime import ingestion_is_fresh

    class Calendar:
        def previous_session_close_ns(self, now_ns: int) -> int:
            return T0

    assert ingestion_is_fresh(T0, now_ns=T0 + 1, calendar=Calendar())
    assert not ingestion_is_fresh(T0 - 1, now_ns=T0 + 1, calendar=Calendar()), (
        "a frontier before the previous session close is stale by more than "
        "one trading day and must hold the gate"
    )
    assert not ingestion_is_fresh(None, now_ns=T0, calendar=Calendar())


def test_paper_start_requires_subscription_manager_and_sim_engine() -> None:
    sink = RecordingSink()
    assert_paper_ready_or_hold(
        {p: True for p in PaperPrerequisite}, alert_sink=sink, timestamp_ns=T0
    )
    assert not sink.alerts

    for missing in PaperPrerequisite:
        availability = {p: p is not missing for p in PaperPrerequisite}
        sink = RecordingSink()
        with pytest.raises(PaperStartHoldError) as held:
            assert_paper_ready_or_hold(availability, alert_sink=sink, timestamp_ns=T0)
        assert held.value.missing == (missing.value,)
        assert sink.alerts, "a paper hold must alert the operator"


def test_failure_holds_pre_trade_unless_override_and_override_alerts() -> None:
    env = _env()
    gate = ReadinessGate.from_env(env)
    from atp_readiness.errors import PreTradeHoldError

    sink = RecordingSink()
    with pytest.raises(PreTradeHoldError):
        evaluate_runtime_readiness(
            gate,
            env,
            _one_failing(SubCheck.IB_ACCOUNT),
            alert_sink=sink,
            timestamp_ns=T0,
        )
    assert gate.state is GateState.PRE_TRADE_BLOCKED
    assert any(a.kind is ReadinessAlertKind.SUBCHECK_FAILURE for a in sink.alerts)

    override = OperatorOverride(
        actor="ops", reason="IB maintenance window", audit_trail_id="AUD-77", timestamp_ns=T0
    )
    sink = RecordingSink()
    release_hold_with_override(gate, override, alert_sink=sink)
    assert gate.state is GateState.OVERRIDDEN
    assert [a.kind for a in sink.alerts] == [ReadinessAlertKind.OPERATOR_OVERRIDE]
    assert gate.overrides[-1].audit_trail_id == "AUD-77"


def test_override_with_undeliverable_alert_keeps_the_hold() -> None:
    # Codex R2 regression: SYS-76 requires the override to alert the
    # operator — if the alert cannot be delivered, the pre-trade hold must
    # remain (a silently-released hold is the dangerous direction).
    from atp_readiness.errors import PreTradeHoldError

    env = _env()
    gate = ReadinessGate.from_env(env)
    with pytest.raises(PreTradeHoldError):
        gate.assert_runtime_ready_or_hold(
            build_runtime_report(
                _one_failing(SubCheck.IB_CONNECTIVITY),
                alert_sink=RecordingSink(),
                timestamp_ns=T0,
            )
        )
    assert gate.state is GateState.PRE_TRADE_BLOCKED

    class RaisingSink:
        def dispatch(self, alert) -> None:
            raise OSError("alert channel down")

    override = OperatorOverride(
        actor="ops", reason="attempted bypass", audit_trail_id="AUD-X", timestamp_ns=T0
    )
    with pytest.raises(OSError):
        release_hold_with_override(gate, override, alert_sink=RaisingSink())
    assert gate.state is GateState.PRE_TRADE_BLOCKED, (
        "an override whose operator alert cannot be delivered must not release the hold"
    )
    assert gate.overrides == ()


def test_active_override_survives_runtime_reevaluation() -> None:
    # Codex R3 regression: SYS-76 holds "until resolved or manually
    # overridden" — the next poll re-observing the SAME failing runtime
    # condition must not silently demote an audited override. A clean
    # evaluation later promotes to READY; a STATIC config regression still
    # demotes.
    from atp_readiness.errors import PreTradeHoldError

    env = _env()
    gate = ReadinessGate.from_env(env)
    sink = RecordingSink()
    failing = _one_failing(SubCheck.IB_CONNECTIVITY)
    with pytest.raises(PreTradeHoldError):
        evaluate_runtime_readiness(gate, env, failing, alert_sink=sink, timestamp_ns=T0)
    release_hold_with_override(
        gate,
        OperatorOverride(
            actor="ops", reason="known outage", audit_trail_id="AUD-L", timestamp_ns=T0
        ),
        alert_sink=RecordingSink(),
    )
    assert gate.state is GateState.OVERRIDDEN

    # Same failing runtime condition on the next poll: override stands, and
    # the failure still alerts (the bypass stays loudly visible).
    sink = RecordingSink()
    state = evaluate_runtime_readiness(gate, env, failing, alert_sink=sink, timestamp_ns=T0 + 1)
    assert state is GateState.OVERRIDDEN
    assert any(a.kind is ReadinessAlertKind.SUBCHECK_FAILURE for a in sink.alerts)

    # Conditions resolve: the normal READY transition ends the override.
    state = evaluate_runtime_readiness(
        gate, env, _all_pass(), alert_sink=RecordingSink(), timestamp_ns=T0 + 2
    )
    assert state is GateState.READY

    # Static regression under an override DOES demote (config drift is new
    # evidence the override never covered).
    gate2 = ReadinessGate.from_env(env)
    with pytest.raises(PreTradeHoldError):
        evaluate_runtime_readiness(gate2, env, failing, alert_sink=RecordingSink(), timestamp_ns=T0)
    release_hold_with_override(
        gate2,
        OperatorOverride(
            actor="ops", reason="known outage", audit_trail_id="AUD-M", timestamp_ns=T0
        ),
        alert_sink=RecordingSink(),
    )
    broken_env = dict(env)
    broken_env.pop("DATABENTO_API_KEY", None)
    with pytest.raises(PreTradeHoldError):
        evaluate_runtime_readiness(
            gate2, broken_env, failing, alert_sink=RecordingSink(), timestamp_ns=T0 + 1
        )
    assert gate2.state is GateState.PRE_TRADE_BLOCKED


def test_unobservable_ingestion_frontier_fails_closed_not_crash(tmp_path) -> None:
    # Codex R3 regression: a missing/wedged coverage binary folds into the
    # stale verdict (None), never leaks an exception out of a readiness poll.
    from atp_readiness.probes import CoverageFrontierSource

    source = CoverageFrontierSource(tmp_path, ["AAPL"], binary=tmp_path / "no-such-binary")
    assert source.min_frontier_ns() is None


def test_alert_sink_failure_is_surfaced_not_swallowed() -> None:
    class RaisingSink:
        def dispatch(self, alert) -> None:
            raise OSError("alert channel down")

    with pytest.raises(OSError):
        build_runtime_report(
            _one_failing(SubCheck.SYSTEM_SERVICES), alert_sink=RaisingSink(), timestamp_ns=T0
        )
