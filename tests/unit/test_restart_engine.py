"""L1 unit tests for the pure restart-recovery engine (SRS-REL-002).

Exercises the end-to-end elapsed measurement, the required-phase / SYS-76 sub-check completeness
rules, the trade-ready predicate (including the NAS degraded-with-alert case), the exact integer
600 s boundary gate, the provable-failure-before-missing-evidence verdict order, the label lock,
and every fail-closed structural error — all in-process, no I/O.
"""

from __future__ import annotations

import pytest
from atp_reliability.restart import (
    DEFAULT_RTO_BUDGET_NS,
    NS_PER_SECOND,
    DuplicatePhaseError,
    DuplicateSubCheckError,
    EmptyRestartTimeline,
    GateOutcome,
    InvalidTimestamp,
    ObservedPhase,
    PhaseInversionError,
    PhaseOrderError,
    ReadinessOutcome,
    RestartPhase,
    RestartRecoveryTarget,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
    Verdict,
)
from atp_reliability.restart import compute_restart_recovery as _compute_raw

pytestmark = [pytest.mark.unit]

S = NS_PER_SECOND


def compute_restart_recovery(*, during_market_hours: bool | None = True, **kwargs: object):
    """Test shim: default the NFR-R6 market-hours scope to proven (``True``) so the many PASS-path
    tests read cleanly. The dedicated scope tests pass ``during_market_hours=None``/``False``
    explicitly to exercise the scope gate.
    """

    return _compute_raw(during_market_hours=during_market_hours, **kwargs)  # type: ignore[arg-type]


# A default contiguous timeline: trigger→ready in 130 s, well under the 600 s budget.
_DEFAULT_BOUNDS: dict[RestartPhase, tuple[int, int]] = {
    RestartPhase.PROXMOX_VM: (0, 10),
    RestartPhase.OS_BOOT: (10, 60),
    RestartPhase.DOCKER_DAEMON: (60, 90),
    RestartPhase.ATP_SERVICE_INIT: (90, 120),
    RestartPhase.READINESS_CHECK: (120, 130),
}


def phases(
    overrides: dict[RestartPhase, tuple[int, int] | None] | None = None,
) -> list[ObservedPhase]:
    """Build a phase list in seconds→ns; override or drop (``None``) individual phases."""
    bounds = dict(_DEFAULT_BOUNDS)
    if overrides:
        for phase, val in overrides.items():
            if val is None:
                bounds.pop(phase, None)
            else:
                bounds[phase] = val
    return [ObservedPhase(phase=p, start_ns=a * S, end_ns=b * S) for p, (a, b) in bounds.items()]


def ready(
    *,
    gate: GateOutcome = GateOutcome.READY,
    nas_degraded: bool = False,
    nas_alert: bool = False,
    drop: set[SubCheck] | None = None,
    fail: set[SubCheck] | None = None,
) -> ReadinessOutcome:
    """Build a readiness outcome; NAS may be degraded, sub-checks may be dropped or failed."""
    drop = drop or set()
    fail = fail or set()
    results: list[SubCheckResult] = []
    for sc in SubCheck:
        if sc in drop:
            continue
        if sc in fail:
            results.append(SubCheckResult(sc, SubCheckStatus.FAIL))
        elif sc is SubCheck.NAS_ARCHIVAL and nas_degraded:
            results.append(SubCheckResult(sc, SubCheckStatus.DEGRADED, nas_alert))
        else:
            results.append(SubCheckResult(sc, SubCheckStatus.PASS))
    return ReadinessOutcome(gate_state=gate, subchecks=tuple(results))


# --------------------------------------------------------------------------- #
# Happy path + the exact boundary.
# --------------------------------------------------------------------------- #


def test_compliant_restart_is_pass() -> None:
    art = compute_restart_recovery(phases=phases(), readiness=ready())
    assert art.verdict is Verdict.PASS
    assert art.certified is True
    assert art.elapsed_ns == 130 * S
    assert art.observed_span_ns == 130 * S
    assert art.missing_phases == ()
    assert art.missing_subchecks == ()
    assert art.readiness_trade_ready is True


def test_exact_600s_boundary_is_pass() -> None:
    art = compute_restart_recovery(
        phases=phases({RestartPhase.READINESS_CHECK: (120, 600)}), readiness=ready()
    )
    assert art.elapsed_ns == 600 * S == DEFAULT_RTO_BUDGET_NS
    assert art.verdict is Verdict.PASS


def test_one_ns_over_budget_is_fail() -> None:
    over = [p for p in phases() if p.phase is not RestartPhase.READINESS_CHECK] + [
        ObservedPhase(RestartPhase.READINESS_CHECK, 120 * S, 600 * S + 1)
    ]
    art = compute_restart_recovery(phases=over, readiness=ready())
    assert art.observed_span_ns == 600 * S + 1
    assert art.verdict is Verdict.FAIL


# --------------------------------------------------------------------------- #
# Completeness: missing phase / missing sub-check → INCONCLUSIVE (never a silent PASS).
# --------------------------------------------------------------------------- #


def test_missing_readiness_phase_is_inconclusive_not_pass() -> None:
    # Drop the slow terminal phase; remaining span is small — must NOT squeak under as PASS.
    art = compute_restart_recovery(
        phases=phases({RestartPhase.READINESS_CHECK: None}), readiness=ready()
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "readiness_check" in art.missing_phases
    assert art.elapsed_ns is None  # no trade-ready anchor


def test_missing_subcheck_is_inconclusive_not_pass() -> None:
    art = compute_restart_recovery(phases=phases(), readiness=ready(drop={SubCheck.NAS_ARCHIVAL}))
    assert art.verdict is Verdict.INCONCLUSIVE
    assert art.missing_subchecks == ("nas_archival",)
    assert art.readiness_trade_ready is False


def test_no_readiness_evidence_is_inconclusive() -> None:
    art = compute_restart_recovery(phases=phases(), readiness=None)
    assert art.verdict is Verdict.INCONCLUSIVE
    assert art.gate_state is None
    assert art.missing_subchecks == tuple(sc.value for sc in SubCheck)


def test_caller_subcheck_list_cannot_understate_required_set() -> None:
    # Supplying only three sub-checks leaves two absent → the required set (the enum authority)
    # is not satisfied → INCONCLUSIVE. A caller cannot shrink the denominator.
    art = compute_restart_recovery(
        phases=phases(),
        readiness=ready(drop={SubCheck.NAS_ARCHIVAL, SubCheck.SYSTEM_SERVICES}),
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert set(art.missing_subchecks) == {"nas_archival", "system_services"}


# --------------------------------------------------------------------------- #
# Provable failure beats missing evidence.
# --------------------------------------------------------------------------- #


def test_anchors_over_budget_with_middle_missing_is_fail() -> None:
    # Only the two anchor phases present; their span alone breaches the budget → FAIL even though
    # the middle phases are missing (a provable breach must not be downgraded to INCONCLUSIVE).
    two = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 1 * S),
        ObservedPhase(RestartPhase.READINESS_CHECK, 700 * S, 701 * S),
    ]
    art = compute_restart_recovery(phases=two, readiness=ready())
    assert art.verdict is Verdict.FAIL
    assert art.observed_span_ns == 701 * S


def test_gap_between_phases_counts_against_budget() -> None:
    # Per-phase durations sum to ~5 s, but a gap pushes end-to-end over 600 s → FAIL.
    gapped = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 1 * S),
        ObservedPhase(RestartPhase.OS_BOOT, 10 * S, 11 * S),
        ObservedPhase(RestartPhase.DOCKER_DAEMON, 20 * S, 21 * S),
        ObservedPhase(RestartPhase.ATP_SERVICE_INIT, 30 * S, 31 * S),
        ObservedPhase(RestartPhase.READINESS_CHECK, 700 * S, 701 * S),
    ]
    art = compute_restart_recovery(phases=gapped, readiness=ready())
    total_duration = sum(art.phase_durations_ns.values())
    assert total_duration < 600 * S < art.observed_span_ns
    assert art.verdict is Verdict.FAIL


def test_over_budget_beats_missing_readiness() -> None:
    art = compute_restart_recovery(
        phases=phases({RestartPhase.READINESS_CHECK: (120, 700)}), readiness=None
    )
    assert art.verdict is Verdict.FAIL  # provable breach, not INCONCLUSIVE


# --------------------------------------------------------------------------- #
# Readiness must have passed, not merely run.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("gate", [GateOutcome.PRE_TRADE_BLOCKED, GateOutcome.INITIALIZING])
def test_readiness_gate_not_ready_is_fail(gate: GateOutcome) -> None:
    art = compute_restart_recovery(phases=phases(), readiness=ready(gate=gate))
    assert art.verdict is Verdict.FAIL
    assert art.readiness_trade_ready is False


def test_overridden_gate_does_not_certify() -> None:
    # An OVERRIDDEN gate means a failed SYS-76 check was manually bypassed (reachable only from
    # PRE_TRADE_BLOCKED) — a human override, not an automatic recovery, so it must NOT certify PASS
    # even with all sub-checks nominally passing.
    art = compute_restart_recovery(phases=phases(), readiness=ready(gate=GateOutcome.OVERRIDDEN))
    assert art.verdict is Verdict.FAIL
    assert art.readiness_trade_ready is False
    assert art.verdict_reason is not None and "OVERRIDDEN" in art.verdict_reason


def test_failed_subcheck_is_fail() -> None:
    art = compute_restart_recovery(phases=phases(), readiness=ready(fail={SubCheck.IB_ACCOUNT}))
    assert art.verdict is Verdict.FAIL


def test_nas_degraded_with_alert_is_pass_without_alert_is_fail() -> None:
    ok = compute_restart_recovery(
        phases=phases(), readiness=ready(nas_degraded=True, nas_alert=True)
    )
    assert ok.verdict is Verdict.PASS
    assert ok.subcheck_status["nas_archival"] == "degraded(alert=yes)"

    bad = compute_restart_recovery(
        phases=phases(), readiness=ready(nas_degraded=True, nas_alert=False)
    )
    assert bad.verdict is Verdict.FAIL
    assert bad.subcheck_status["nas_archival"] == "degraded(alert=no)"


def test_non_nas_degraded_is_fail() -> None:
    # DEGRADED is only meaningful for NAS; a DEGRADED SSD check is not-passing.
    r = ReadinessOutcome(
        gate_state=GateOutcome.READY,
        subchecks=tuple(
            SubCheckResult(
                sc,
                SubCheckStatus.DEGRADED if sc is SubCheck.DATA_LAYER_SSD else SubCheckStatus.PASS,
            )
            for sc in SubCheck
        ),
    )
    assert compute_restart_recovery(phases=phases(), readiness=r).verdict is Verdict.FAIL


# --------------------------------------------------------------------------- #
# Structural refusals — fail closed (raise), never a verdict.
# --------------------------------------------------------------------------- #


def test_empty_timeline_raises() -> None:
    with pytest.raises(EmptyRestartTimeline):
        compute_restart_recovery(phases=[], readiness=ready())


def test_inverted_or_zero_duration_phase_raises() -> None:
    with pytest.raises(PhaseInversionError):
        compute_restart_recovery(
            phases=[ObservedPhase(RestartPhase.PROXMOX_VM, 10 * S, 10 * S)], readiness=None
        )
    with pytest.raises(PhaseInversionError):
        compute_restart_recovery(
            phases=[ObservedPhase(RestartPhase.PROXMOX_VM, 10 * S, 5 * S)], readiness=None
        )


def test_duplicate_phase_raises() -> None:
    with pytest.raises(DuplicatePhaseError):
        compute_restart_recovery(
            phases=[
                ObservedPhase(RestartPhase.OS_BOOT, 0, 10 * S),
                ObservedPhase(RestartPhase.OS_BOOT, 20 * S, 30 * S),
            ],
            readiness=None,
        )


def test_non_chronological_phase_order_raises() -> None:
    # DOCKER_DAEMON (canonically after OS_BOOT) starts before OS_BOOT → order error.
    with pytest.raises(PhaseOrderError):
        compute_restart_recovery(
            phases=[
                ObservedPhase(RestartPhase.OS_BOOT, 100 * S, 110 * S),
                ObservedPhase(RestartPhase.DOCKER_DAEMON, 10 * S, 20 * S),
            ],
            readiness=None,
        )


def test_legitimate_overlap_is_accepted() -> None:
    # On a real boot, docker.service starts DURING userspace OS boot — DOCKER_DAEMON nests inside
    # OS_BOOT. This is valid reference evidence and must NOT be refused; the span is max-min.
    nested = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 5 * S),
        ObservedPhase(RestartPhase.OS_BOOT, 5 * S, 35 * S),
        ObservedPhase(RestartPhase.DOCKER_DAEMON, 25 * S, 28 * S),  # nested inside OS_BOOT
        ObservedPhase(RestartPhase.ATP_SERVICE_INIT, 40 * S, 90 * S),
        ObservedPhase(RestartPhase.READINESS_CHECK, 90 * S, 100 * S),
    ]
    art = compute_restart_recovery(phases=nested, readiness=ready())
    assert art.verdict is Verdict.PASS
    assert art.observed_span_ns == 100 * S  # max(end)=100, min(start)=0
    assert art.elapsed_ns == 100 * S


def test_nested_phase_does_not_shrink_the_span() -> None:
    # A phase whose end is earlier than an overlapping phase must not shrink observed_span:
    # OS_BOOT ends at 35 s but DOCKER (canonically later) ends at 28 s — span still uses max end.
    phases_list = [
        ObservedPhase(RestartPhase.OS_BOOT, 5 * S, 35 * S),
        ObservedPhase(RestartPhase.DOCKER_DAEMON, 25 * S, 28 * S),
    ]
    art = compute_restart_recovery(phases=phases_list, readiness=None)
    assert art.observed_span_ns == 30 * S  # 35 - 5, not 28 - 5


def test_phase_completing_after_readiness_raises() -> None:
    # Nothing may finish after trade-ready (the readiness-check end) — contradictory evidence.
    with pytest.raises(PhaseOrderError, match="after the readiness-check"):
        compute_restart_recovery(
            phases=[
                ObservedPhase(RestartPhase.PROXMOX_VM, 0, 5 * S),
                ObservedPhase(
                    RestartPhase.ATP_SERVICE_INIT, 10 * S, 700 * S
                ),  # ends after readiness
                ObservedPhase(RestartPhase.READINESS_CHECK, 20 * S, 30 * S),
            ],
            readiness=ready(),
        )


def test_huge_timestamp_is_refused_not_crash() -> None:
    # A pathological giant integer must fail closed (InvalidTimestamp), never crash via
    # math.isfinite / int->float OverflowError.
    with pytest.raises(InvalidTimestamp):
        compute_restart_recovery(
            phases=[ObservedPhase(RestartPhase.PROXMOX_VM, 0, 10**400)], readiness=None
        )


def test_duplicate_subcheck_raises() -> None:
    with pytest.raises(DuplicateSubCheckError):
        compute_restart_recovery(
            phases=phases(),
            readiness=ReadinessOutcome(
                gate_state=GateOutcome.READY,
                subchecks=(
                    SubCheckResult(SubCheck.IB_ACCOUNT, SubCheckStatus.PASS),
                    SubCheckResult(SubCheck.IB_ACCOUNT, SubCheckStatus.PASS),
                ),
            ),
        )


@pytest.mark.parametrize("bad", [True, 1.5, "0", None, -1])
def test_non_integer_timestamp_raises(bad: object) -> None:
    with pytest.raises(InvalidTimestamp):
        compute_restart_recovery(
            phases=[ObservedPhase(RestartPhase.PROXMOX_VM, 0, bad)],  # type: ignore[arg-type]
            readiness=None,
        )


# --------------------------------------------------------------------------- #
# Label lock + artifact rendering.
# --------------------------------------------------------------------------- #


def test_srs_rel_002_target_locks_budget() -> None:
    with pytest.raises(ValueError, match="SRS-REL-002"):
        RestartRecoveryTarget(requirement="SRS-REL-002", budget_ns=700 * S)
    with pytest.raises(ValueError, match="SRS-REL-002"):
        RestartRecoveryTarget(requirement="SRS-REL-002", boundary="weakened")
    # A relaxed budget is fine under a NON-SRS label.
    relaxed = RestartRecoveryTarget(requirement="TEST", budget_ns=5 * S)
    assert relaxed.budget_ns == 5 * S


def test_relaxed_label_can_fail_a_short_budget() -> None:
    art = compute_restart_recovery(
        phases=phases(), readiness=ready(), target=RestartRecoveryTarget("TEST", budget_ns=5 * S)
    )
    assert art.verdict is Verdict.FAIL  # 130 s > 5 s


def test_srs_pass_requires_market_hours_scope() -> None:
    # NFR-R6 applies to market-hours restarts: an SRS-REL-002 PASS requires proven scope.
    assert (
        compute_restart_recovery(
            phases=phases(), readiness=ready(), during_market_hours=True
        ).verdict
        is Verdict.PASS
    )
    unknown = compute_restart_recovery(phases=phases(), readiness=ready(), during_market_hours=None)
    assert unknown.verdict is Verdict.INCONCLUSIVE
    assert "market hours" in (unknown.verdict_reason or "")
    out_of_hours = compute_restart_recovery(
        phases=phases(), readiness=ready(), during_market_hours=False
    )
    assert out_of_hours.verdict is Verdict.INCONCLUSIVE


def test_provable_breach_beats_missing_scope() -> None:
    # An over-budget restart is FAIL even with unknown scope — the scope gate never hides a breach.
    over = [p for p in phases() if p.phase is not RestartPhase.READINESS_CHECK] + [
        ObservedPhase(RestartPhase.READINESS_CHECK, 120 * S, 700 * S)
    ]
    assert (
        compute_restart_recovery(phases=over, readiness=ready(), during_market_hours=None).verdict
        is Verdict.FAIL
    )


def test_non_srs_label_does_not_require_scope() -> None:
    # A non-SRS (informational) target certifies without market-hours proof.
    art = _compute_raw(
        phases=phases(),
        readiness=ready(),
        during_market_hours=None,
        target=RestartRecoveryTarget("TEST-INFRA"),
    )
    assert art.verdict is Verdict.PASS


def test_artifact_as_dict_and_str_are_stable() -> None:
    art = compute_restart_recovery(phases=phases(), readiness=ready())
    d = art.as_dict()
    assert d["verdict"] == "PASS"
    assert d["elapsed_seconds"] == 130.0
    assert d["budget_seconds"] == 600.0
    assert d["requirement"] == "SRS-REL-002"
    text = str(art)
    assert "verdict: PASS" in text
    assert "SRS-REL-002" in text


def test_engine_reads_no_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # The engine must derive everything from injected instants; poison the wall clock and confirm
    # a compute still succeeds (no clock read → no clock-skew fabrication).
    import time as _time

    monkeypatch.setattr(
        _time, "time_ns", lambda: (_ for _ in ()).throw(AssertionError("clock read"))
    )
    art = compute_restart_recovery(phases=phases(), readiness=ready())
    assert art.verdict is Verdict.PASS
