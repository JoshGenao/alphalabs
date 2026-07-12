"""L2 property tests for the availability engine (SRS-REL-001).

Invariants over generated evidence: the ratio stays in [0, 1]; excluded and
out-of-session intervals never lower it; adding counted downtime never raises it;
merged-vs-split overlapping downtime is identical; and a PASS is impossible
without full coverage.
"""

from __future__ import annotations

import pytest
from atp_reliability.availability import (
    NS_PER_SECOND,
    AvailabilityTarget,
    CoveredSpan,
    DowntimeInterval,
    MarketSessionWindow,
    OutageCause,
    Verdict,
    ZeroMarketExposure,
    compute_availability,
)
from hypothesis import given
from hypothesis import strategies as st

S = NS_PER_SECOND
SESSION = 23_400  # seconds

pytestmark = [pytest.mark.property]


# Single-session invariants: disable the rolling-30-day period gate so these tests
# isolate the coverage / downtime / exclusion math (the gate is tested separately).
# A relaxed target must carry a non-SRS requirement label.
_NO_PERIOD = AvailabilityTarget(requirement="TEST-ENGINE", rolling_window_days=0)


def _one_session_run(covered_spans, downtime, excluded=None):
    sess = [MarketSessionWindow(0, SESSION * S)]
    return compute_availability(
        window_start_ns=0,
        window_end_ns=SESSION * S,
        sessions=sess,
        covered=covered_spans,
        downtime=downtime,
        excluded_windows=excluded,
        target=_NO_PERIOD,
    )


# A downtime interval within the session, in seconds.
_interval = st.builds(
    lambda a, b: (min(a, b), max(a, b)),
    st.integers(min_value=0, max_value=SESSION),
    st.integers(min_value=0, max_value=SESSION),
)


@given(st.lists(_interval, max_size=8))
def test_ratio_in_unit_interval(intervals) -> None:
    dt = [DowntimeInterval(a * S, b * S, OutageCause.HOST_UNPLANNED) for a, b in intervals]
    art = _one_session_run([CoveredSpan(0, SESSION * S)], dt)
    assert 0.0 <= art.availability_ratio <= 1.0


@given(st.lists(_interval, max_size=8))
def test_full_coverage_downtime_never_increases_availability(intervals) -> None:
    base = _one_session_run([CoveredSpan(0, SESSION * S)], [])
    dt = [DowntimeInterval(a * S, b * S, OutageCause.HOST_UNPLANNED) for a, b in intervals]
    withdt = _one_session_run([CoveredSpan(0, SESSION * S)], dt)
    assert withdt.availability_ratio <= base.availability_ratio


@given(_interval)
def test_out_of_session_downtime_never_affects_ratio(iv) -> None:
    a, b = iv
    inside = DowntimeInterval(a * S, b * S, OutageCause.HOST_UNPLANNED)
    # a copy shifted entirely after the session close
    outside = DowntimeInterval(
        (SESSION + a + 1) * S, (SESSION + b + 2) * S, OutageCause.HOST_UNPLANNED
    )
    r1 = _one_session_run([CoveredSpan(0, SESSION * S)], [inside])
    r2 = _one_session_run([CoveredSpan(0, SESSION * S)], [inside, outside])
    assert r1.counted_downtime_ns == r2.counted_downtime_ns


@given(st.lists(_interval, max_size=6))
def test_non_counting_causes_never_change_ratio(intervals) -> None:
    counted = [DowntimeInterval(0, 10 * S, OutageCause.HOST_UNPLANNED)]
    noise = [DowntimeInterval(a * S, b * S, OutageCause.IB_CONNECTIVITY) for a, b in intervals]
    r1 = _one_session_run([CoveredSpan(0, SESSION * S)], counted)
    r2 = _one_session_run([CoveredSpan(0, SESSION * S)], counted + noise)
    assert r1.counted_downtime_ns == r2.counted_downtime_ns
    assert r1.availability_ratio == r2.availability_ratio


@given(st.lists(_interval, min_size=1, max_size=8))
def test_overlap_merge_equals_union(intervals) -> None:
    dt = [DowntimeInterval(a * S, b * S, OutageCause.HOST_UNPLANNED) for a, b in intervals]
    art = _one_session_run([CoveredSpan(0, SESSION * S)], dt)
    # independent union-length reference over 1-second granularity
    covered_secs = set()
    for a, b in intervals:
        covered_secs.update(range(a, b))
    assert art.counted_downtime_ns == len(covered_secs) * S


@given(
    st.integers(min_value=1, max_value=SESSION - 1),
    st.lists(_interval, max_size=4),
)
def test_pass_requires_full_coverage(gap_start, intervals) -> None:
    # leave a 1s+ hole in coverage -> can never be PASS.
    covered = [CoveredSpan(0, gap_start * S), CoveredSpan((gap_start + 1) * S, SESSION * S)]
    dt = [
        DowntimeInterval(a * S, b * S, OutageCause.IB_CONNECTIVITY) for a, b in intervals
    ]  # non-counting, cannot self-cover the hole
    art = _one_session_run(covered, dt)
    assert art.verdict is not Verdict.PASS
    assert art.unmeasured_market_ns >= S


@given(_interval)
def test_excluded_window_never_lowers_effective_below_downtime(iv) -> None:
    a, b = iv
    excl = [(a * S, b * S)]
    try:
        art = _one_session_run([CoveredSpan(0, SESSION * S)], [], excluded=excl)
    except ZeroMarketExposure:
        # an exclusion covering the whole session leaves no denominator -> fail closed.
        return
    assert art.effective_market_ns >= 0
    assert art.counted_downtime_ns <= art.effective_market_ns
