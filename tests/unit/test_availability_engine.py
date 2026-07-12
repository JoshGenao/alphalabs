"""L1 unit tests for the pure availability engine (SRS-REL-001).

Exercises the interval math, coverage accounting, exclusion semantics, the
integer per-mille verdict at the exact 0.999 boundary, and every fail-closed
error — all in-process, no I/O.
"""

from __future__ import annotations

import pytest
from atp_reliability.availability import (
    NS_PER_SECOND,
    AvailabilityTarget,
    CoveredSpan,
    DowntimeInterval,
    EmptyMeasurementWindow,
    InvertedInterval,
    MarketSessionWindow,
    NoTradingSessions,
    OutageCause,
    OverlappingSessions,
    Verdict,
    ZeroMarketExposure,
    compute_availability,
)

pytestmark = [pytest.mark.unit]

S = NS_PER_SECOND
SESSION_SECONDS = 23_400  # 6.5h regular session


def _session(start_s: int = 0, seconds: int = SESSION_SECONDS) -> MarketSessionWindow:
    return MarketSessionWindow(start_ns=start_s * S, end_ns=(start_s + seconds) * S)


def _covered(start_s: int = 0, seconds: int = SESSION_SECONDS) -> CoveredSpan:
    return CoveredSpan(start_ns=start_s * S, end_ns=(start_s + seconds) * S)


# These tests exercise the interval math over short windows, so they disable the
# rolling-30-day period gate. A relaxed target must NOT be labelled SRS-REL-001, so
# it carries a non-certifying requirement label (its artifact is honestly non-SRS).
NO_PERIOD = AvailabilityTarget(requirement="TEST-ENGINE", rolling_window_days=0)


def _run(sessions, covered, downtime, excluded=None, target=NO_PERIOD):
    ws = min(s.start_ns for s in sessions)
    we = max(s.end_ns for s in sessions)
    return compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=covered,
        downtime=downtime,
        excluded_windows=excluded,
        target=target,
    )


def test_basic_ratio_pass() -> None:
    art = _run(
        [_session()], [_covered()], [DowntimeInterval(100 * S, 120 * S, OutageCause.HOST_UNPLANNED)]
    )
    assert art.verdict is Verdict.PASS
    assert art.counted_downtime_ns == 20 * S
    assert art.effective_market_ns == SESSION_SECONDS * S
    assert art.unmeasured_market_ns == 0
    assert art.availability_ratio == pytest.approx(1 - 20 / SESSION_SECONDS)


def test_exact_boundary_is_pass() -> None:
    # exactly 0.1% downtime (23.4s over 23400s) -> PASS, inclusive.
    dt = DowntimeInterval(0, 23_400_000_000, OutageCause.HOST_UNPLANNED)
    art = _run([_session()], [_covered()], [dt])
    assert art.counted_downtime_ns == 23_400_000_000
    assert 1000 * art.counted_downtime_ns == art.effective_market_ns  # per-mille equality
    assert art.verdict is Verdict.PASS


def test_one_ns_over_boundary_is_fail() -> None:
    dt = DowntimeInterval(0, 23_400_000_001, OutageCause.HOST_UNPLANNED)
    art = _run([_session()], [_covered()], [dt])
    assert art.verdict is Verdict.FAIL


def test_downtime_outside_session_not_counted() -> None:
    # outage entirely after the session close contributes nothing.
    dt = DowntimeInterval(
        SESSION_SECONDS * S + 10 * S, SESSION_SECONDS * S + 40 * S, OutageCause.HOST_UNPLANNED
    )
    art = _run([_session()], [_covered()], [dt])
    assert art.counted_downtime_ns == 0
    assert art.verdict is Verdict.PASS


def test_overnight_outage_split_across_gap() -> None:
    # two sessions with a closed gap; one outage spanning both -> only in-session parts count.
    day1 = _session(0)
    day2 = _session(100_000)  # far later, well past day1 close
    outage = DowntimeInterval(
        start_ns=(SESSION_SECONDS - 60) * S,  # 60s before day1 close
        end_ns=(100_000 + 30) * S,  # 30s into day2
        cause=OutageCause.HOST_UNPLANNED,
    )
    art = _run([day1, day2], [_covered(0), _covered(100_000)], [outage])
    assert art.counted_downtime_ns == (60 + 30) * S  # only in-session portions


def test_overlapping_downtime_merged_no_double_count() -> None:
    a = DowntimeInterval(100 * S, 200 * S, OutageCause.HOST_UNPLANNED)
    b = DowntimeInterval(150 * S, 250 * S, OutageCause.HOST_UNPLANNED)
    art = _run([_session()], [_covered()], [a, b])
    assert art.counted_downtime_ns == 150 * S  # union [100,250), not 200


def test_non_counting_cause_ignored_in_ratio_but_in_breakdown() -> None:
    dt = DowntimeInterval(100 * S, 900 * S, OutageCause.IB_CONNECTIVITY)
    art = _run([_session()], [_covered()], [dt])
    assert art.counted_downtime_ns == 0
    assert art.verdict is Verdict.PASS
    assert art.downtime_ns_by_cause["ib_connectivity"] == 800 * S


def test_uncovered_market_is_inconclusive() -> None:
    art = _run([_session()], [], [])
    assert art.verdict is Verdict.INCONCLUSIVE
    assert art.unmeasured_market_ns == SESSION_SECONDS * S
    assert "unmeasured" in (art.inconclusive_reason or "")


def test_partial_coverage_is_inconclusive() -> None:
    art = _run([_session()], [_covered(0, SESSION_SECONDS - 100)], [])
    assert art.verdict is Verdict.INCONCLUSIVE
    assert art.unmeasured_market_ns == 100 * S


def test_definite_breach_with_partial_coverage_is_fail_not_inconclusive() -> None:
    # Regression (adversarial review): observed host-unplanned downtime already over the
    # 0.1% budget is a PROVABLE failure — it must be FAIL even though coverage is
    # incomplete (more coverage could only reveal MORE downtime, never rescue a PASS).
    dt = [DowntimeInterval(0, 30 * S, OutageCause.HOST_UNPLANNED)]  # 30s > 0.1% of 23400s
    covered = [_covered(0, 12_000)]  # only partially covered -> unmeasured > 0
    art = _run([_session()], covered, dt)
    assert art.unmeasured_market_ns > 0  # coverage IS incomplete
    assert art.counted_downtime_ns == 30 * S
    assert art.verdict is Verdict.FAIL  # yet the breach is definite -> FAIL, not INCONCLUSIVE


def test_downtime_span_counts_as_covered() -> None:
    # a host-unplanned span is self-covering (we observed the host down). A within-
    # budget 10s outage fills the coverage gap and still certifies.
    dt = DowntimeInterval(0, 10 * S, OutageCause.HOST_UNPLANNED)
    covered = [_covered(10, SESSION_SECONDS - 10)]  # covers everything except [0,10)
    art = _run([_session()], covered, [dt])
    assert art.unmeasured_market_ns == 0  # downtime span fills the coverage gap
    assert art.verdict is Verdict.PASS


def test_exclusion_outside_session_is_compliant() -> None:
    # SYS-75-style window well after close -> no effect, still certifiable.
    excl = [((SESSION_SECONDS + 3600) * S, (SESSION_SECONDS + 3900) * S)]
    art = _run([_session()], [_covered()], [], excluded=excl)
    assert art.excluded_in_session_ns == 0
    assert art.effective_market_ns == SESSION_SECONDS * S
    assert art.verdict is Verdict.PASS


def test_exclusion_inside_session_forces_inconclusive() -> None:
    # an exclusion leaking into market hours must not silently pad the ratio.
    excl = [(100 * S, 700 * S)]  # 600s inside the session
    art = _run([_session()], [_covered()], [], excluded=excl)
    assert art.excluded_in_session_ns == 600 * S
    assert art.effective_market_ns == (SESSION_SECONDS - 600) * S
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "exclusion" in (art.inconclusive_reason or "").lower()


def test_zero_market_exposure_when_excluded_covers_all() -> None:
    excl = [(0, SESSION_SECONDS * S)]
    with pytest.raises(ZeroMarketExposure):
        _run([_session()], [_covered()], [], excluded=excl)


def test_inverted_downtime_rejected() -> None:
    with pytest.raises(InvertedInterval):
        _run(
            [_session()],
            [_covered()],
            [DowntimeInterval(200 * S, 100 * S, OutageCause.HOST_UNPLANNED)],
        )


def test_overlapping_sessions_rejected() -> None:
    a = MarketSessionWindow(0, 100 * S)
    b = MarketSessionWindow(50 * S, 150 * S)
    with pytest.raises(OverlappingSessions):
        compute_availability(
            window_start_ns=0,
            window_end_ns=150 * S,
            sessions=[a, b],
            covered=[],
            downtime=[],
        )


def test_empty_window_rejected() -> None:
    with pytest.raises(EmptyMeasurementWindow):
        compute_availability(
            window_start_ns=100 * S,
            window_end_ns=100 * S,
            sessions=[_session()],
            covered=[],
            downtime=[],
        )


def test_no_sessions_rejected() -> None:
    with pytest.raises(NoTradingSessions):
        compute_availability(
            window_start_ns=0,
            window_end_ns=100 * S,
            sessions=[],
            covered=[],
            downtime=[],
        )


def test_session_outside_window_clipped_not_rejected() -> None:
    # rolling-window edge: a session partly outside the analysis window contributes
    # only its in-window part (no rejection).
    sess = [MarketSessionWindow(0, 1000 * S)]
    art = compute_availability(
        window_start_ns=400 * S,
        window_end_ns=900 * S,
        sessions=sess,
        covered=[CoveredSpan(400 * S, 900 * S)],
        downtime=[],
    )
    assert art.total_market_ns == 500 * S


def test_as_dict_roundtrip_fields() -> None:
    art = _run(
        [_session()], [_covered()], [DowntimeInterval(0, 10 * S, OutageCause.HOST_UNPLANNED)]
    )
    d = art.as_dict()
    assert d["verdict"] == "PASS"
    assert d["certified"] is True
    assert d["counted_downtime_ns"] == 10 * S
    assert d["counted_downtime_seconds"] == 10.0
    assert d["target_per_mille"] == 999


def test_srs_target_locks_the_canonical_gates() -> None:
    # A target labelled SRS-REL-001 cannot weaken any gate.
    assert AvailabilityTarget().requirement == "SRS-REL-001"  # default is valid
    for bad in (
        {"target_per_mille": 500},
        {"rolling_window_days": 0},
        {"coverage_floor_ns": 1_000},
    ):
        with pytest.raises(ValueError, match="SRS-REL-001"):
            AvailabilityTarget(**bad)
    # a relaxed config is allowed only under a non-SRS label.
    relaxed = AvailabilityTarget(requirement="TEST-ENGINE", rolling_window_days=0)
    assert relaxed.requirement == "TEST-ENGINE"


def test_relaxed_target_artifact_is_not_labelled_srs() -> None:
    art = _run([_session()], [_covered()], [])
    assert art.requirement == "TEST-ENGINE"  # NO_PERIOD carries the non-SRS label
    assert art.as_dict()["requirement"] == "TEST-ENGINE"


def test_custom_target_per_mille_100pct_requires_zero_downtime() -> None:
    # 100% target, no downtime tolerated (period gate disabled to isolate the ratio gate).
    strict = AvailabilityTarget(
        requirement="TEST-ENGINE", target_per_mille=1000, rolling_window_days=0
    )
    breach = _run(
        [_session()],
        [_covered()],
        [DowntimeInterval(0, 1 * S, OutageCause.HOST_UNPLANNED)],
        target=strict,
    )
    assert breach.verdict is Verdict.FAIL
    clean = _run([_session()], [_covered()], [], target=strict)
    assert clean.verdict is Verdict.PASS


def test_short_window_cannot_certify_rolling_period() -> None:
    # A one-session (6.5h) window under the DEFAULT 30-day target cannot certify,
    # even with full coverage and zero downtime — the requirement is a rolling period.
    art = compute_availability(
        window_start_ns=0,
        window_end_ns=SESSION_SECONDS * S,
        sessions=[_session()],
        covered=[_covered()],
        downtime=[],
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    reason = (art.inconclusive_reason or "").lower()
    assert "rolling" in reason and "period" in reason


def test_window_meeting_rolling_period_can_certify() -> None:
    # A 30-day window (two sessions 29 days apart), fully covered, no downtime -> PASS.
    thirty_days = 30 * 86_400
    sessions = [_session(0), _session(29 * 86_400)]
    covered = [_covered(0), _covered(29 * 86_400)]
    art = compute_availability(
        window_start_ns=0,
        window_end_ns=thirty_days * S,
        sessions=sessions,
        covered=covered,
        downtime=[],
    )
    assert art.verdict is Verdict.PASS
