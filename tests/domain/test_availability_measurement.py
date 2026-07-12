"""L7 domain safety test for the SRS-REL-001 availability measurement substrate.

Marked ``safety`` + ``domain`` so the deterministic critic recognises this file as
the paired safety-path test for the reliability substrate. The availability
verdict is a safety-relevant claim (it certifies whether the trading platform met
its market-hours uptime objective), so these tests turn on the invariants that
keep the claim honest:

* the SYS-75 scheduled IB Gateway restart (an NFR-R1 exclusion) is **never**
  counted as market-hours downtime;
* an unplanned host-level outage during market hours **is** counted and lowers the
  ratio (it cannot be silently dropped);
* an unclosed outage is treated as DOWN to the window end (the worst failure must
  not read as zero downtime — no fabrication of recovery);
* kill-switch HALTED and single-container churn are **not** counted as host
  downtime (they are SRS-SAFE-001 / NFR-R5 scope, not host availability);
* market time with no coverage evidence **refuses** certification (no "no-data =
  100% up" lie);
* the engine and downtime reconstruction read **no** wall clock (every instant is
  injected — no clock-skew fabrication);
* a regular session is exactly 23,400 s across both DST transitions.

Scope: these exercise the in-process measurement mechanism over fixtures + the
real trading calendar. The real host-liveness feed that produces positive coverage
from live operation, and the 30-real-day proof that yields the end-to-end >= 99.9%
evidence, are deferred (see ``availability_measurement_contract.deferred``); this
file asserts the mechanism is correct and honest, not that the objective is met.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from atp_logging.records import LogClass, LogRecord, Severity, Source
from atp_reliability.availability import (
    NS_PER_SECOND,
    AvailabilityTarget,
    CoveredSpan,
    DowntimeInterval,
    MarketSessionWindow,
    OutageCause,
    Verdict,
    compute_availability,
)
from atp_reliability.evidence import (
    HealthTransition,
    downtime_from_log_records,
    market_sessions,
    reconstruct_downtime,
    sys75_exclusion_windows,
)
from atp_strategy.calendar import UsEquityTradingCalendar

pytestmark = [pytest.mark.domain, pytest.mark.safety]

S = NS_PER_SECOND
CAL = UsEquityTradingCalendar.for_exchange("NYSE")


def _cover_all(sessions: list[MarketSessionWindow]) -> list[CoveredSpan]:
    # Test-local: model a fully-monitored window. There is deliberately NO such helper
    # in the shippable package (it would let a caller synthesise coverage and mint a
    # false PASS); tests that need full coverage build the spans themselves.
    return [CoveredSpan(s.start_ns, s.end_ns) for s in sessions]


# Most invariant tests use a single trading day, so they disable the rolling-30-day
# period gate to isolate the invariant under test (the period gate has its own tests:
# test_short_calendar_window_cannot_certify + test_full_rolling_window_certifies).
# A relaxed target must carry a non-SRS requirement label so the artifact is not
# mislabelled as a certified SRS-REL-001 result.
NO_PERIOD = AvailabilityTarget(requirement="TEST-ENGINE", rolling_window_days=0)


def _one_session_day(date: _dt.date):
    ws, we, sessions = market_sessions(CAL, date, date)
    return ws, we, sessions


def test_sys75_restart_window_never_counted_as_downtime() -> None:
    date = _dt.date(2026, 1, 20)  # regular Tuesday session
    ws, we, sessions = _one_session_day(date)
    excluded = sys75_exclusion_windows(date, date)  # 23:45 ET, outside the 09:30-16:00 session
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[],
        excluded_windows=excluded,
        target=NO_PERIOD,
    )
    assert art.excluded_in_session_ns == 0  # the restart never touches market hours
    assert art.effective_market_ns == 23_400 * S
    assert art.verdict is Verdict.PASS


def test_ib_gateway_restart_cause_inside_sys75_window_contributes_zero() -> None:
    date = _dt.date(2026, 1, 20)
    ws, we, sessions = _one_session_day(date)
    excluded = sys75_exclusion_windows(date, date)
    restart_start, restart_end = excluded[0]
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=max(we, restart_end),
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[DowntimeInterval(restart_start, restart_end, OutageCause.IB_GATEWAY_RESTART)],
        excluded_windows=excluded,
        target=NO_PERIOD,
    )
    assert art.counted_downtime_ns == 0
    assert art.verdict is Verdict.PASS


@pytest.mark.parametrize("cause", [OutageCause.PLANNED_MAINTENANCE, OutageCause.IB_GATEWAY_RESTART])
def test_in_session_excluded_cause_downtime_cannot_be_laundered(cause: OutageCause) -> None:
    # Regression (adversarial review): an excluded-cause (planned / IB-restart) downtime
    # that falls INSIDE market hours must NOT be silently treated as uptime. It defines
    # an in-session exclusion -> INCONCLUSIVE (either a data inconsistency or a real
    # NFR-R1 violation: exclusions must be scheduled outside market hours).
    _, we, sessions = _one_session_day(_dt.date(2026, 1, 20))
    open_ns = sessions[0].start_ns
    laundered = DowntimeInterval(open_ns + 100 * S, open_ns + 700 * S, cause)  # 600s in-session
    art = compute_availability(
        window_start_ns=sessions[0].start_ns,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[laundered],  # NO matching excluded_windows entry supplied
        target=NO_PERIOD,
    )
    assert art.excluded_in_session_ns == 600 * S  # carved from the denominator, not ignored
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "exclusion" in (art.inconclusive_reason or "").lower()


def test_unplanned_host_outage_in_market_hours_is_counted() -> None:
    date = _dt.date(2026, 1, 20)
    _, we, sessions = _one_session_day(date)
    open_ns = sessions[0].start_ns  # place the outage inside the 09:30-16:00 session
    outage = DowntimeInterval(
        open_ns + 100 * S, open_ns + 130 * S, OutageCause.HOST_UNPLANNED
    )  # 30s
    art = compute_availability(
        window_start_ns=sessions[0].start_ns,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[outage],
        target=NO_PERIOD,
    )
    assert art.counted_downtime_ns == 30 * S
    assert art.verdict is Verdict.FAIL  # 30s over 23400s = 0.128% breaches 99.9%


def test_unclosed_outage_counts_to_window_end() -> None:
    date = _dt.date(2026, 1, 20)
    ws, we, sessions = _one_session_day(date)
    # a single DOWN edge with no matching UP -> host is DOWN to the window end.
    transitions = [HealthTransition(ws + 10 * S, "host", True)]
    downtime = reconstruct_downtime(
        transitions,
        window_start_ns=ws,
        window_end_ns=we,
        source_causes={"host": OutageCause.HOST_UNPLANNED},
    )
    assert len(downtime) == 1
    assert downtime[0].end_ns == we  # DOWN to the end, not silently dropped
    assert downtime[0].cause is OutageCause.HOST_UNPLANNED


def test_same_timestamp_down_up_does_not_fabricate_downtime() -> None:
    # Regression (adversarial review): a DOWN and an UP at the exact same instant is a
    # zero-duration blip, NOT an outage. UP-first ordering used to fabricate a full-
    # window host outage (boundary-open [start, T] + unclosed [T, end]).
    ws, we, sessions = _one_session_day(_dt.date(2026, 1, 20))
    t = sessions[0].start_ns + 100 * S
    transitions = [
        HealthTransition(t, "host", False),  # UP listed first on purpose
        HealthTransition(t, "host", True),  # DOWN at the same instant
    ]
    downtime = reconstruct_downtime(
        transitions,
        window_start_ns=ws,
        window_end_ns=we,
        source_causes={"host": OutageCause.HOST_UNPLANNED},
    )
    assert sum(d.end_ns - d.start_ns for d in downtime) == 0  # no fabricated downtime

    # a genuine DOWN-then-later-UP still records the real outage.
    real = reconstruct_downtime(
        [HealthTransition(t, "host", True), HealthTransition(t + 40 * S, "host", False)],
        window_start_ns=ws,
        window_end_ns=we,
        source_causes={"host": OutageCause.HOST_UNPLANNED},
    )
    assert sum(d.end_ns - d.start_ns for d in real) == 40 * S


def test_repeated_up_edges_do_not_re_emit_boundary_open() -> None:
    # A stream of UP edges with no preceding DOWN yields ONE boundary-open interval
    # (down from window start until the first UP), not one per UP edge.
    ws, we, sessions = _one_session_day(_dt.date(2026, 1, 20))
    t0 = sessions[0].start_ns + 50 * S
    transitions = [
        HealthTransition(t0, "host", False),
        HealthTransition(t0 + 10 * S, "host", False),
        HealthTransition(t0 + 20 * S, "host", False),
    ]
    downtime = reconstruct_downtime(
        transitions,
        window_start_ns=ws,
        window_end_ns=we,
        source_causes={"host": OutageCause.HOST_UNPLANNED},
    )
    assert len(downtime) == 1
    assert downtime[0].start_ns == ws and downtime[0].end_ns == t0


def _system_record(ts: int, source: Source, event_type: str) -> LogRecord:
    # SYSTEM records carry strategy_id=None (SRS-LOG-001 schema) — the realistic shape.
    return LogRecord(ts, Severity.ERROR, source, event_type, "msg", "corr", LogClass.SYSTEM)


def test_interleaved_container_and_kill_switch_records_produce_no_host_downtime() -> None:
    # Regression (adversarial review): container-lifecycle records are NOT mapped —
    # SYSTEM records carry no per-container identifier, so pairing would be ambiguous
    # (one container's START closing another's OOM_KILL). Two interleaved container
    # streams + a kill-switch HALTED must yield NO downtime intervals at all, while a
    # real IB disconnect/connect pair IS still reconstructed (single logical gateway).
    ws, we, sessions = _one_session_day(_dt.date(2026, 1, 20))
    o = sessions[0].start_ns
    records = [
        _system_record(o + 60 * S, Source.CONTAINER_LIFECYCLE, "OOM_KILL"),  # container A
        _system_record(o + 65 * S, Source.CONTAINER_LIFECYCLE, "OOM_KILL"),  # container B
        _system_record(o + 90 * S, Source.CONTAINER_LIFECYCLE, "CONTAINER_START"),  # A back
        _system_record(o + 95 * S, Source.CONTAINER_LIFECYCLE, "CONTAINER_START"),  # B back
        _system_record(o + 100 * S, Source.KILL_SWITCH, "HALTED"),
        _system_record(o + 200 * S, Source.IB_GATEWAY, "DISCONNECT"),
        _system_record(o + 230 * S, Source.IB_GATEWAY, "CONNECT"),
    ]
    downtime = downtime_from_log_records(records, window_start_ns=ws, window_end_ns=we)
    # ONLY the IB gateway pair is reconstructed; no container / kill-switch intervals.
    assert len(downtime) == 1
    assert downtime[0].cause is OutageCause.IB_CONNECTIVITY
    assert downtime[0].end_ns - downtime[0].start_ns == 30 * S
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=downtime,
        target=NO_PERIOD,
    )
    assert art.counted_downtime_ns == 0  # IB connectivity is non-counting for NFR-R1
    assert art.verdict is Verdict.PASS


def test_uncovered_market_refuses_certification() -> None:
    date = _dt.date(2026, 1, 20)
    ws, we, sessions = _one_session_day(date)
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=[],  # no positive observation at all
        downtime=[],
        target=NO_PERIOD,  # isolate the coverage-refusal from the period gate
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert art.unmeasured_market_ns == 23_400 * S
    assert "unmeasured" in (art.inconclusive_reason or "")


def test_engine_reads_no_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    def _boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise AssertionError("availability measurement must not read a wall clock")

    monkeypatch.setattr(time, "time", _boom)
    monkeypatch.setattr(time, "time_ns", _boom)
    monkeypatch.setattr(time, "monotonic", _boom)
    monkeypatch.setattr(time, "monotonic_ns", _boom)

    from atp_reliability.availability import MarketSessionWindow

    ws = 0
    we = 23_400 * S
    sessions = [MarketSessionWindow(0, we)]
    transitions = [HealthTransition(10 * S, "host", True), HealthTransition(20 * S, "host", False)]
    downtime = reconstruct_downtime(
        transitions,
        window_start_ns=ws,
        window_end_ns=we,
        source_causes={"host": OutageCause.HOST_UNPLANNED},
    )
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=[CoveredSpan(0, we)],
        downtime=downtime,
    )
    assert art.counted_downtime_ns == 10 * S  # ran to completion with the clock disabled


@pytest.mark.parametrize(
    "date",
    [
        _dt.date(2026, 3, 9),  # Monday after spring-forward (2026-03-08)
        _dt.date(2026, 11, 2),  # Monday after fall-back (2026-11-01)
    ],
)
def test_regular_session_is_23400_seconds_across_dst(date: _dt.date) -> None:
    _, _, sessions = _one_session_day(date)
    assert len(sessions) == 1
    assert sessions[0].end_ns - sessions[0].start_ns == 23_400 * S


def test_short_calendar_window_cannot_certify_even_with_full_coverage() -> None:
    # Regression (adversarial review): a one-day window is NOT a rolling 30-day
    # period, so it must NOT certify SRS-REL-001 even with full coverage + zero
    # downtime. The DEFAULT target (30 days) is used deliberately.
    ws, we, sessions = _one_session_day(_dt.date(2026, 1, 20))
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[],
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "rolling" in (art.inconclusive_reason or "").lower()


def test_full_rolling_window_certifies() -> None:
    # The real certification path: EXACTLY a 30-calendar-day range with full coverage and
    # no host-unplanned downtime certifies PASS under the DEFAULT 30-day target.
    start, end = _dt.date(2026, 1, 5), _dt.date(2026, 2, 3)  # exactly 30 calendar days
    ws, we, sessions = market_sessions(CAL, start, end)
    assert (we - ws) == 30 * 86_400 * S
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[],
        excluded_windows=sys75_exclusion_windows(start, end),
    )
    assert art.session_count >= 20  # ~21 trading days in the range
    assert art.verdict is Verdict.PASS


def test_longer_than_rolling_window_cannot_certify() -> None:
    # Regression (adversarial review): NFR-R1 is a ROLLING 30-day metric. A 60-day range
    # with full coverage and zero downtime must NOT certify — a longer window would let a
    # failing 30-day sub-period be diluted into a passing average. Exactly-30-days only.
    start, end = _dt.date(2026, 1, 5), _dt.date(2026, 3, 5)  # 60 calendar days
    ws, we, sessions = market_sessions(CAL, start, end)
    assert (we - ws) > 30 * 86_400 * S
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[],
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "diluted" in (art.inconclusive_reason or "").lower()


def test_dst_crossing_30_calendar_day_window_certifies() -> None:
    # Regression (adversarial review): a 30-CALENDAR-day range spanning the spring DST
    # transition (2026-03-08) still certifies. Because market_sessions builds the window
    # in UTC-midnight bounds (DST has no effect in UTC), the window is EXACTLY 30*24 h,
    # so the strict elapsed-ns gate passes without any special DST handling.
    start, end = _dt.date(2026, 2, 23), _dt.date(2026, 3, 24)  # 30 calendar days, spans DST
    ws, we, sessions = market_sessions(CAL, start, end)
    assert (we - ws) == 30 * 86_400 * S  # UTC window is exactly 30 days despite the DST hop
    art = compute_availability(
        window_start_ns=ws,
        window_end_ns=we,
        sessions=sessions,
        covered=_cover_all(sessions),
        downtime=[],
        excluded_windows=sys75_exclusion_windows(start, end),
    )
    assert art.verdict is Verdict.PASS


def test_raw_sub_30_day_window_cannot_certify() -> None:
    # Regression (adversarial review): a raw 29 d 23 h window is genuinely under the
    # rolling period — the STRICT elapsed-ns gate refuses it. There is no caller-supplied
    # metadata or DST slack to borrow (the DST-robustness lives entirely in the UTC window
    # market_sessions builds), so no arbitrary raw caller can certify a short window.
    dur = (30 * 86_400 - 3600) * S  # 29 d 23 h
    sessions = [MarketSessionWindow(0, 23_400 * S), MarketSessionWindow(dur - 23_400 * S, dur)]
    art = compute_availability(
        window_start_ns=0,
        window_end_ns=dur,
        sessions=sessions,
        covered=[CoveredSpan(0, 23_400 * S), CoveredSpan(dur - 23_400 * S, dur)],
        downtime=[],
    )
    assert art.verdict is Verdict.INCONCLUSIVE
    assert "rolling" in (art.inconclusive_reason or "").lower()


def test_calendar_cli_cannot_certify_without_coverage(capsys: pytest.CaptureFixture[str]) -> None:
    # Regression: the calendar path has no coverage oracle (the host-liveness feed is
    # deferred), so it must NEVER emit a certifying PASS — no flag may synthesise
    # coverage. Without observed coverage the verdict is INCONCLUSIVE and exit != 0.
    from atp_reliability.cli import EXIT_NOT_CERTIFIED, run

    code = run(["--calendar", "--start", "2026-01-20", "--end", "2026-01-20"])
    out = capsys.readouterr().out
    assert code == EXIT_NOT_CERTIFIED
    assert "verdict:INCONCLUSIVE" in out
    assert "verdict:PASS" not in out
    # and there is no coverage-synthesising flag on the parser at all.
    from atp_reliability.cli import build_parser

    assert "--assume-full-coverage" not in build_parser().format_help()
