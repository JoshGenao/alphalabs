"""L2 property tests for the restart-recovery engine (SRS-REL-002).

Invariants over generated restart timelines: elapsed equals the end-to-end span for a complete
timeline; inter-phase gaps never *lower* the observed span; a later trade-ready never lowers
elapsed; a PASS is impossible without the complete phase set AND complete passing sub-checks; and
the observed span is always a lower bound on true elapsed.
"""

from __future__ import annotations

import pytest
from atp_reliability.restart import (
    NS_PER_SECOND,
    GateOutcome,
    ObservedPhase,
    ReadinessOutcome,
    RestartPhase,
    RestartRecoveryTarget,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
    Verdict,
    compute_restart_recovery,
)
from hypothesis import given
from hypothesis import strategies as st

pytestmark = [pytest.mark.property]

S = NS_PER_SECOND
_ORDER = list(RestartPhase)


def _full_ready() -> ReadinessOutcome:
    return ReadinessOutcome(
        gate_state=GateOutcome.READY,
        subchecks=tuple(SubCheckResult(sc, SubCheckStatus.PASS) for sc in SubCheck),
    )


@st.composite
def _contiguous_timeline(draw: st.DrawFn) -> tuple[list[ObservedPhase], int]:
    """A complete, contiguous, positive-duration 5-phase timeline. Returns (phases, elapsed_ns)."""
    start = draw(st.integers(min_value=0, max_value=10_000)) * S
    durations = [draw(st.integers(min_value=1, max_value=200)) * S for _ in _ORDER]
    phases: list[ObservedPhase] = []
    cursor = start
    for phase, dur in zip(_ORDER, durations, strict=True):
        phases.append(ObservedPhase(phase=phase, start_ns=cursor, end_ns=cursor + dur))
        cursor += dur
    return phases, cursor - start


@st.composite
def _gapped_timeline(draw: st.DrawFn) -> list[ObservedPhase]:
    """A complete 5-phase timeline with non-negative gaps between phases."""
    cursor = draw(st.integers(min_value=0, max_value=1_000)) * S
    phases: list[ObservedPhase] = []
    for phase in _ORDER:
        gap = draw(st.integers(min_value=0, max_value=100)) * S
        dur = draw(st.integers(min_value=1, max_value=100)) * S
        cursor += gap
        phases.append(ObservedPhase(phase=phase, start_ns=cursor, end_ns=cursor + dur))
        cursor += dur
    return phases


@given(_contiguous_timeline())
def test_contiguous_elapsed_equals_span_and_sum(data: tuple[list[ObservedPhase], int]) -> None:
    phases, elapsed = data
    art = compute_restart_recovery(phases=phases, readiness=_full_ready())
    # For a contiguous timeline: elapsed == observed span == sum of durations.
    assert art.elapsed_ns == elapsed
    assert art.observed_span_ns == elapsed
    assert sum(art.phase_durations_ns.values()) == elapsed


@given(_gapped_timeline())
def test_gaps_make_span_at_least_sum_of_durations(phases: list[ObservedPhase]) -> None:
    art = compute_restart_recovery(phases=phases, readiness=_full_ready())
    # Gaps can only INCREASE the end-to-end span relative to summed durations.
    assert art.observed_span_ns >= sum(art.phase_durations_ns.values())
    # Elapsed (both anchors present) equals the observed span.
    assert art.elapsed_ns == art.observed_span_ns


@given(_contiguous_timeline(), st.integers(min_value=1, max_value=1000))
def test_later_trade_ready_never_lowers_elapsed(
    data: tuple[list[ObservedPhase], int], extra_s: int
) -> None:
    phases, elapsed = data
    ready_phase = phases[-1]
    extended = phases[:-1] + [
        ObservedPhase(RestartPhase.READINESS_CHECK, ready_phase.start_ns, ready_phase.end_ns + extra_s * S)
    ]
    art0 = compute_restart_recovery(phases=phases, readiness=_full_ready())
    art1 = compute_restart_recovery(phases=extended, readiness=_full_ready())
    assert art1.elapsed_ns >= art0.elapsed_ns


@given(st.sets(st.sampled_from(list(SubCheck)), min_size=1))
def test_pass_impossible_with_any_subcheck_absent(dropped: set[SubCheck]) -> None:
    # A complete, fast, ready timeline — but drop >=1 sub-check → never PASS.
    subs = tuple(
        SubCheckResult(sc, SubCheckStatus.PASS) for sc in SubCheck if sc not in dropped
    )
    readiness = ReadinessOutcome(gate_state=GateOutcome.READY, subchecks=subs)
    # A fixed compliant timeline isolates the sub-check completeness invariant.
    fixed = [
        ObservedPhase(p, i * 10 * S, i * 10 * S + 5 * S) for i, p in enumerate(RestartPhase)
    ]
    art = compute_restart_recovery(phases=fixed, readiness=readiness)
    assert art.verdict is not Verdict.PASS


@given(st.sets(st.sampled_from(list(RestartPhase)), min_size=1, max_size=4))
def test_pass_impossible_with_any_phase_absent(present: set[RestartPhase]) -> None:
    # Fewer than all 5 phases can never certify (unless a provable breach makes it FAIL — still not
    # PASS). Build a small in-budget timeline over just the present phases (canonical order).
    ordered = [p for p in RestartPhase if p in present]
    phases = [
        ObservedPhase(p, i * 10 * S, i * 10 * S + 5 * S) for i, p in enumerate(ordered)
    ]
    art = compute_restart_recovery(phases=phases, readiness=_full_ready())
    assert art.verdict is not Verdict.PASS


@given(_gapped_timeline())
def test_observed_span_is_lower_bound_on_true_elapsed(phases: list[ObservedPhase]) -> None:
    # Drop the trigger phase: observed span over the rest must be <= the full-timeline elapsed.
    full = compute_restart_recovery(phases=phases, readiness=_full_ready())
    without_trigger = [p for p in phases if p.phase is not RestartPhase.PROXMOX_VM]
    partial = compute_restart_recovery(phases=without_trigger, readiness=_full_ready())
    assert partial.observed_span_ns <= full.elapsed_ns


@given(st.integers(min_value=1, max_value=10_000))
def test_relaxed_budget_gate_is_exact(budget_s: int) -> None:
    # elapsed == budget → PASS; elapsed == budget + 1ns → FAIL (exact integer gate, non-SRS label).
    target = RestartRecoveryTarget(requirement="TEST", budget_ns=budget_s * S)
    at = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 1),
        ObservedPhase(RestartPhase.OS_BOOT, 1, 2),
        ObservedPhase(RestartPhase.DOCKER_DAEMON, 2, 3),
        ObservedPhase(RestartPhase.ATP_SERVICE_INIT, 3, 4),
        ObservedPhase(RestartPhase.READINESS_CHECK, 4, budget_s * S),
    ]
    assert compute_restart_recovery(phases=at, readiness=_full_ready(), target=target).verdict is Verdict.PASS
    over = at[:-1] + [ObservedPhase(RestartPhase.READINESS_CHECK, 4, budget_s * S + 1)]
    assert compute_restart_recovery(phases=over, readiness=_full_ready(), target=target).verdict is Verdict.FAIL
