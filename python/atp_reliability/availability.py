"""Pure, I/O-free market-hours availability measurement engine (SRS-REL-001).

SRS trace: ``SRS-REL-001`` (SyRS ``NFR-R1``, ``NFR-R6``, ``SYS-76``; StRS
``SN-2.05``, ``BG-6``). This module is the availability analog of the
``crates/atp-types/src/perf.rs`` latency-percentile substrate: a dependency-free
measurement core that ingests *caller-supplied* market-session windows, coverage
(positively-observed) spans, and classified downtime intervals, and produces an
:class:`AvailabilityVerificationArtifact` with a fail-closed verdict against the
NFR-R1 objective (≥ 99.9% availability during US equity market hours over a
rolling 30-day period).

Design invariants (mirrors the PERF-001 substrate + the NOTIF-001 no-fabrication
discipline):

* **Pure.** This module imports **no** ``atp_*`` package and reads **no** clock.
  Every instant is an injected epoch-nanosecond ``int``. The evidence adapters
  (``atp_reliability.evidence``) are the only place calendars and log stores are
  touched — keeping this engine as portable and deterministic as ``perf.rs``.
* **Integer nanoseconds internally.** All arithmetic is exact integer ns; the
  verdict is an integer per-mille inequality (``1000 * downtime <= effective``),
  never a lossy ``f64`` comparison at the 99.9% boundary.
* **No-data is not up.** Availability is measured only over *positively-observed*
  market-seconds. Market time with no coverage evidence is reported as
  ``unmeasured`` and **refuses certification** (verdict :attr:`Verdict.INCONCLUSIVE`)
  rather than being silently counted as uptime — the classic availability lie
  this engine is built to avoid.
* **Fail closed.** Inverted intervals, empty windows, zero market exposure, and
  exclusions that leak into market hours all raise or refuse to certify.

The NFR-R1 exclusions (planned maintenance; the SYS-75 scheduled IB Gateway
restart at ~23:45 ET) are carved from *both* the numerator and the denominator
(``effective_market_ns = total_market_ns - excluded_in_session_ns``). Because
NFR-R1 mandates every exclusion is scheduled *outside* market hours,
``excluded_in_session_ns`` is 0 on the compliant path; a non-zero value is
surfaced as a first-class field and forces :attr:`Verdict.INCONCLUSIVE` (it means
either maintenance leaked into market hours or downtime was laundered as planned).

The SyRS parenthetical "≤ 1.17 minutes downtime per trading day on average" is a
non-binding approximation: 0.1% of a 6.5-hour session is 23.4 s, not 70.2 s. The
machine gate is the ratio (:data:`AvailabilityTarget.target_per_mille` = 999); the
average is reported for information only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

NS_PER_SECOND = 1_000_000_000
SECONDS_PER_DAY = 86_400

#: NFR-R1 availability objective expressed in per-mille (≥ 99.9%).
DEFAULT_TARGET_PER_MILLE = 999

#: NFR-R1 measurement window: a rolling 30-day period.
DEFAULT_ROLLING_WINDOW_DAYS = 30

#: The market-hours boundary phrase, pinned to SyRS NFR-R1. Mirrored in
#: ``architecture/runtime_services.json#availability_measurement_contract`` and
#: re-asserted by the L3 contract test so the boundary cannot silently drift.
MARKET_HOURS_BOUNDARY = (
    "US equity market hours 09:30-16:00 ET, Mon-Fri, excluding market holidays; "
    "rolling 30-day period; planned maintenance and the SYS-75 scheduled IB "
    "Gateway restart excluded; unplanned host-level outages included"
)


class OutageCause(StrEnum):
    """Closed classification of a downtime interval, pinned to NFR-R1 scope.

    Only :attr:`HOST_UNPLANNED` counts toward the NFR-R1 availability ratio —
    NFR-R1's *included* scope is "unplanned host-level outages (hardware failure,
    kernel panic)". Everything else is either explicitly *excluded* by NFR-R1
    (:attr:`PLANNED_MAINTENANCE`, :attr:`IB_GATEWAY_RESTART`) or belongs to a
    different requirement (:attr:`IB_CONNECTIVITY` → NFR-R2,
    :attr:`CONTAINER_CHURN` → NFR-R5, :attr:`KILL_SWITCH_HALT` → SRS-SAFE-001)
    and does **not** count as host unavailability. Those non-counting causes are
    retained in the artifact's per-cause breakdown for audit, never in the ratio.
    """

    HOST_UNPLANNED = "host_unplanned"
    PLANNED_MAINTENANCE = "planned_maintenance"
    IB_GATEWAY_RESTART = "ib_gateway_restart"
    IB_CONNECTIVITY = "ib_connectivity"
    CONTAINER_CHURN = "container_churn"
    KILL_SWITCH_HALT = "kill_switch_halt"


#: Causes whose in-session duration counts against the NFR-R1 ratio. Kept as a
#: single-source constant so the "which classes count" contract cannot drift.
COUNTING_CAUSES: frozenset[OutageCause] = frozenset({OutageCause.HOST_UNPLANNED})

#: NFR-R1-excluded causes. A downtime interval with one of these causes defines an
#: exclusion window (in addition to any explicit ``excluded_windows``), so it is
#: carved from both numerator and denominator — and if it falls INSIDE market hours
#: it trips the in-session-exclusion guard (INCONCLUSIVE) rather than being silently
#: dropped from the numerator while inflating availability.
EXCLUDED_CAUSES: frozenset[OutageCause] = frozenset(
    {OutageCause.PLANNED_MAINTENANCE, OutageCause.IB_GATEWAY_RESTART}
)


class Verdict(StrEnum):
    """Three-valued availability verdict.

    ``INCONCLUSIVE`` is distinct from ``FAIL`` on purpose: refusing to certify
    because the evidence is insufficient or contaminated is *not* the same claim
    as "the system was unavailable". The CLI treats both ``FAIL`` and
    ``INCONCLUSIVE`` as a non-zero exit (a green is only ever ``PASS``).
    """

    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class AvailabilityError(Exception):
    """Base class for fail-closed availability-measurement errors."""


class EmptyMeasurementWindow(AvailabilityError):
    """The analysis window is empty or inverted (``end <= start``)."""


class InvertedInterval(AvailabilityError):
    """An interval has ``end < start`` (or a session has ``end <= start``)."""


class OverlappingSessions(AvailabilityError):
    """Two market-session windows overlap — sessions must be disjoint."""


class NoTradingSessions(AvailabilityError):
    """No trading session falls within the analysis window."""


class ZeroMarketExposure(AvailabilityError):
    """Total or effective market-seconds is zero — no denominator to divide by."""


@dataclass(frozen=True, slots=True)
class MarketSessionWindow:
    """A single trading-day regular session ``[start_ns, end_ns)`` (epoch ns)."""

    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class CoveredSpan:
    """A span ``[start_ns, end_ns)`` the monitor positively observed (up or down).

    Coverage answers "was the availability monitor observing during this span?".
    Market time not inside any covered span is ``unmeasured`` and cannot be
    certified.
    """

    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class DowntimeInterval:
    """An observed downtime span ``[start_ns, end_ns)`` with its NFR-R1 cause."""

    start_ns: int
    end_ns: int
    cause: OutageCause


#: The canonical requirement label. A target carrying this label is LOCKED to the
#: NFR-R1 gates so an artifact reading ``requirement: SRS-REL-001`` always means the
#: full objective was applied. Relaxed configurations (e.g. engine-math unit tests)
#: MUST use a different requirement label so their artifact is honestly non-SRS.
SRS_REL_001 = "SRS-REL-001"


@dataclass(frozen=True, slots=True)
class AvailabilityTarget:
    """The NFR-R1 objective + measurement parameters (single-source constants).

    A target labelled :data:`SRS_REL_001` is validated to the canonical gates
    (``target_per_mille=999``, ``rolling_window_days=30``, ``coverage_floor_ns=0``);
    constructing one with weakened gates raises ``ValueError`` — you cannot mint a
    relaxed target that still claims to certify SRS-REL-001.
    """

    requirement: str = SRS_REL_001
    target_per_mille: int = DEFAULT_TARGET_PER_MILLE
    rolling_window_days: int = DEFAULT_ROLLING_WINDOW_DAYS
    #: Max unmeasured market-ns tolerated before certification is refused. 0 =
    #: strict full coverage (exact with integer-ns session bounds).
    coverage_floor_ns: int = 0
    boundary: str = MARKET_HOURS_BOUNDARY

    def __post_init__(self) -> None:
        if self.requirement == SRS_REL_001 and (
            self.target_per_mille != DEFAULT_TARGET_PER_MILLE
            or self.rolling_window_days != DEFAULT_ROLLING_WINDOW_DAYS
            or self.coverage_floor_ns != 0
        ):
            raise ValueError(
                "an AvailabilityTarget labelled 'SRS-REL-001' must use the canonical "
                "NFR-R1 gates (target_per_mille=999, rolling_window_days=30, "
                "coverage_floor_ns=0); use a different requirement label for a relaxed "
                "or non-certifying configuration so its artifact is not mislabelled."
            )


# --------------------------------------------------------------------------- #
# Interval algebra — pure integer-ns set operations over half-open intervals.
# --------------------------------------------------------------------------- #

Interval = tuple[int, int]


def _merge(intervals: list[Interval]) -> list[Interval]:
    """Return the disjoint maximal union of ``intervals`` (drops empty spans).

    Touching intervals (``next.start == cur.end``) are merged; the total measure
    is identical either way, but a maximal union keeps downstream subtraction and
    intersection clean.
    """

    ivs = sorted((s, e) for s, e in intervals if e > s)
    out: list[Interval] = []
    for s, e in ivs:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _measure(intervals: list[Interval]) -> int:
    """Total length (ns) of the union of ``intervals`` — no double counting."""

    return sum(e - s for s, e in _merge(intervals))


def _intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Return ``union(a) ∩ union(b)`` as a disjoint interval list."""

    am = _merge(a)
    bm = _merge(b)
    out: list[Interval] = []
    i = j = 0
    while i < len(am) and j < len(bm):
        lo = max(am[i][0], bm[j][0])
        hi = min(am[i][1], bm[j][1])
        if lo < hi:
            out.append((lo, hi))
        if am[i][1] < bm[j][1]:
            i += 1
        else:
            j += 1
    return out


def _subtract(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Return ``union(a) \\ union(b)`` as a disjoint interval list."""

    bm = _merge(b)
    out: list[Interval] = []
    for s, e in _merge(a):
        cur = s
        for bs, be in bm:
            if be <= cur:
                continue
            if bs >= e:
                break
            if bs > cur:
                out.append((cur, bs))
            cur = max(cur, be)
            if cur >= e:
                break
        if cur < e:
            out.append((cur, e))
    return out


def _require_ordered(start_ns: int, end_ns: int, *, allow_empty: bool, label: str) -> None:
    """Reject an inverted (and optionally empty) interval — fail closed."""

    if end_ns < start_ns:
        raise InvertedInterval(f"{label} is inverted: end_ns {end_ns} < start_ns {start_ns}")
    if not allow_empty and end_ns == start_ns:
        raise InvertedInterval(f"{label} is empty: end_ns == start_ns == {start_ns}")


def _rolling_period_mismatch(
    window_duration_ns: int,
    tgt: "AvailabilityTarget",
) -> str | None:
    """Return an INCONCLUSIVE reason unless the window is EXACTLY the rolling period,
    else ``None`` (also ``None`` when the gate is disabled, ``rolling_window_days == 0``).

    NFR-R1 is a *rolling* 30-day metric: availability must hold over the rolling window,
    not merely on average over a longer span. A verification artifact therefore measures
    exactly one rolling window; a LONGER window is refused so downtime cannot be diluted
    across it (a failing 30-day sub-period hidden inside a passing 60-day average), and a
    SHORTER window is refused because it does not cover the period. The comparison is an
    exact elapsed-ns equality — DST-robust because ``market_sessions`` builds the window
    in UTC-midnight bounds, so an N-day period is exactly ``N * 86_400`` s regardless of
    any DST transition inside it.
    """

    required_days = tgt.rolling_window_days
    if required_days <= 0:
        return None
    if window_duration_ns == required_days * SECONDS_PER_DAY * NS_PER_SECOND:
        return None
    return (
        f"measurement window spans "
        f"{window_duration_ns / (SECONDS_PER_DAY * NS_PER_SECOND):.2f} days; NFR-R1 "
        f"certifies exactly one rolling {required_days}-day period (not a longer or "
        "shorter span — downtime must not be diluted across a longer window)"
    )


# --------------------------------------------------------------------------- #
# Verification artifact.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AvailabilityVerificationArtifact:
    """The SRS-REL-001 verification artifact — the analysis/system-test evidence.

    All canonical quantities are exact integer nanoseconds; ``*_seconds`` views
    and ``availability_ratio`` are convenience floats for humans/JSON.
    """

    requirement: str
    boundary: str
    window_start_ns: int
    window_end_ns: int
    session_count: int
    total_market_ns: int
    excluded_in_session_ns: int
    effective_market_ns: int
    covered_market_ns: int
    unmeasured_market_ns: int
    counted_downtime_ns: int
    measured_up_ns: int
    target_per_mille: int
    availability_ratio: float
    avg_downtime_per_session_seconds: float
    downtime_ns_by_cause: dict[str, int]
    verdict: Verdict
    inconclusive_reason: str | None = field(default=None)

    @property
    def certified(self) -> bool:
        """True only when the artifact proves the NFR-R1 objective (``PASS``)."""

        return self.verdict is Verdict.PASS

    def as_dict(self) -> dict[str, object]:
        """Render as a JSON-serialisable dict (exact ns + convenience seconds)."""

        return {
            "requirement": self.requirement,
            "boundary": self.boundary,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "session_count": self.session_count,
            "total_market_ns": self.total_market_ns,
            "total_market_seconds": self.total_market_ns / NS_PER_SECOND,
            "excluded_in_session_ns": self.excluded_in_session_ns,
            "excluded_in_session_seconds": self.excluded_in_session_ns / NS_PER_SECOND,
            "effective_market_ns": self.effective_market_ns,
            "effective_market_seconds": self.effective_market_ns / NS_PER_SECOND,
            "covered_market_ns": self.covered_market_ns,
            "covered_market_seconds": self.covered_market_ns / NS_PER_SECOND,
            "unmeasured_market_ns": self.unmeasured_market_ns,
            "unmeasured_market_seconds": self.unmeasured_market_ns / NS_PER_SECOND,
            "counted_downtime_ns": self.counted_downtime_ns,
            "counted_downtime_seconds": self.counted_downtime_ns / NS_PER_SECOND,
            "measured_up_ns": self.measured_up_ns,
            "target_per_mille": self.target_per_mille,
            "availability_ratio": self.availability_ratio,
            "avg_downtime_per_session_seconds": self.avg_downtime_per_session_seconds,
            "downtime_seconds_by_cause": {
                cause: ns / NS_PER_SECOND for cause, ns in self.downtime_ns_by_cause.items()
            },
            "verdict": self.verdict.value,
            "certified": self.certified,
            "inconclusive_reason": self.inconclusive_reason,
        }

    def __str__(self) -> str:
        """A stable, inspectable rendering — the human-readable artifact."""

        lines = [
            f"{self.requirement} availability verification artifact",
            f"  boundary: {self.boundary}",
            f"  window: [{self.window_start_ns}, {self.window_end_ns}] ns "
            f"({self.session_count} trading sessions)",
            f"  total market: {self.total_market_ns / NS_PER_SECOND:.3f} s",
            f"  excluded in-session: {self.excluded_in_session_ns / NS_PER_SECOND:.3f} s",
            f"  effective market: {self.effective_market_ns / NS_PER_SECOND:.3f} s",
            f"  covered (measured): {self.covered_market_ns / NS_PER_SECOND:.3f} s",
            f"  unmeasured: {self.unmeasured_market_ns / NS_PER_SECOND:.3f} s",
            f"  counted downtime (host-unplanned): "
            f"{self.counted_downtime_ns / NS_PER_SECOND:.3f} s",
            f"  avg downtime / session: {self.avg_downtime_per_session_seconds:.3f} s",
            f"  availability: {self.availability_ratio * 100:.4f}% "
            f"(target: >= {self.target_per_mille / 10:.1f}%)",
        ]
        if self.downtime_ns_by_cause:
            lines.append("  downtime by cause:")
            for cause in sorted(self.downtime_ns_by_cause):
                lines.append(
                    f"    {cause}: {self.downtime_ns_by_cause[cause] / NS_PER_SECOND:.3f} s"
                )
        verdict_line = f"  verdict: {self.verdict.value}"
        if self.inconclusive_reason is not None:
            verdict_line += f" ({self.inconclusive_reason})"
        lines.append(verdict_line)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The engine.
# --------------------------------------------------------------------------- #


def compute_availability(
    *,
    window_start_ns: int,
    window_end_ns: int,
    sessions: list[MarketSessionWindow],
    covered: list[CoveredSpan],
    downtime: list[DowntimeInterval],
    excluded_windows: list[Interval] | None = None,
    target: AvailabilityTarget | None = None,
) -> AvailabilityVerificationArtifact:
    """Compute the NFR-R1 availability artifact from caller-supplied evidence.

    Args:
        window_start_ns / window_end_ns: the rolling analysis window (epoch ns). For a
            certifying run this is the UTC-midnight period built by ``market_sessions``
            (exactly N*24 h for an N-day period, DST-robust); the rolling-period gate is
            a STRICT elapsed-ns comparison, so a shorter window cannot certify.
        sessions: market-hours windows (one per trading day). Clipped to the
            analysis window, so rolling-window edge sessions contribute their
            in-window part rather than being rejected.
        covered: spans the monitor positively observed. Market time outside every
            covered span is ``unmeasured`` and blocks certification.
        downtime: classified downtime intervals. Only :data:`COUNTING_CAUSES`
            (host-unplanned) count toward the ratio; all causes appear in the
            per-cause breakdown. A downtime span implies coverage of itself (we
            observed the host down).
        excluded_windows: NFR-R1 exclusion spans (planned maintenance + the
            SYS-75 restart). Carved from both numerator and denominator.
        target: the NFR-R1 objective; defaults to :class:`AvailabilityTarget`.

    Raises:
        EmptyMeasurementWindow, InvertedInterval, OverlappingSessions,
        NoTradingSessions, ZeroMarketExposure — all fail-closed.
    """

    tgt = target or AvailabilityTarget()
    excluded = list(excluded_windows or [])

    # 1. Validate the analysis window.
    if window_end_ns <= window_start_ns:
        raise EmptyMeasurementWindow(
            f"window is empty/inverted: [{window_start_ns}, {window_end_ns}]"
        )

    # 2. Validate + clip sessions to the analysis window (edge-session safe).
    if not sessions:
        raise NoTradingSessions("no market sessions supplied")
    clipped_sessions: list[Interval] = []
    for s in sessions:
        _require_ordered(s.start_ns, s.end_ns, allow_empty=False, label="session")
        lo = max(s.start_ns, window_start_ns)
        hi = min(s.end_ns, window_end_ns)
        if hi > lo:
            clipped_sessions.append((lo, hi))
    clipped_sessions.sort()
    for prev, nxt in zip(clipped_sessions, clipped_sessions[1:], strict=False):
        if nxt[0] < prev[1]:
            raise OverlappingSessions(f"sessions overlap: {prev} and {nxt}")
    if not clipped_sessions:
        raise NoTradingSessions("no market session overlaps the analysis window")

    session_union = _merge(clipped_sessions)
    total_market_ns = _measure(session_union)
    if total_market_ns <= 0:
        raise ZeroMarketExposure("total market exposure is zero")

    # 3. Exclusions (semantics B): carve from numerator + denominator. The excluded
    #    set is the explicit ``excluded_windows`` PLUS every downtime interval whose
    #    cause is NFR-R1-excluded (planned maintenance / SYS-75 IB restart) — so an
    #    excluded-cause downtime that leaks into market hours cannot be silently
    #    dropped from the numerator; it lands in ``excluded_in_session`` and forces
    #    INCONCLUSIVE via the guard in step 7.
    for i, (es, ee) in enumerate(excluded):
        _require_ordered(es, ee, allow_empty=True, label=f"excluded_window[{i}]")
    for d in downtime:
        _require_ordered(d.start_ns, d.end_ns, allow_empty=True, label=f"downtime[{d.cause.value}]")
    all_excluded = list(excluded) + [
        (d.start_ns, d.end_ns) for d in downtime if d.cause in EXCLUDED_CAUSES
    ]
    excluded_in_session = _intersect(session_union, all_excluded)
    excluded_in_session_ns = _measure(excluded_in_session)
    effective_region = _subtract(session_union, all_excluded)
    effective_market_ns = _measure(effective_region)
    if effective_market_ns <= 0:
        raise ZeroMarketExposure("effective market exposure is zero after exclusions")

    # 4. Counted downtime: host-unplanned only, clipped to effective region.
    counting_spans = [(d.start_ns, d.end_ns) for d in downtime if d.cause in COUNTING_CAUSES]
    counted_downtime_region = _intersect(counting_spans, effective_region)
    counted_downtime_ns = _measure(counted_downtime_region)

    # Per-cause breakdown: raw in-session footprint (audit — not exclusion-netted).
    downtime_ns_by_cause: dict[str, int] = {}
    for cause in OutageCause:
        spans = [(d.start_ns, d.end_ns) for d in downtime if d.cause is cause]
        if not spans:
            continue
        in_session = _measure(_intersect(spans, session_union))
        if in_session > 0:
            downtime_ns_by_cause[cause.value] = in_session

    # 5. Coverage. A *counted* host-unplanned span self-covers (observing the host
    #    down is a positive observation). Non-counting subsystem spans (IB/container)
    #    are deliberately NOT a coverage oracle — the host-availability oracle is the
    #    explicit ``covered`` feed (see P0 #2 in the design: logs never witness host
    #    death, so subsystem signals must not certify coverage).
    covered_spans = [(c.start_ns, c.end_ns) for c in covered]
    for i, (cs, ce) in enumerate(covered_spans):
        _require_ordered(cs, ce, allow_empty=True, label=f"covered[{i}]")
    covered_region = _intersect(covered_spans + counting_spans, effective_region)
    covered_market_ns = _measure(covered_region)
    unmeasured_market_ns = effective_market_ns - covered_market_ns
    measured_up_ns = covered_market_ns - counted_downtime_ns

    # 6. Availability ratio (pessimistic: unmeasured counts as not-available).
    availability_ratio = measured_up_ns / effective_market_ns
    avg_downtime_per_session_seconds = counted_downtime_ns / (NS_PER_SECOND * len(clipped_sessions))

    # 7. Verdict — three-valued, fail-closed, integer per-mille gate.
    #    The rolling-period precondition is checked FIRST: NFR-R1 is a measurement over
    #    a rolling 30-day period, so a period shorter than that cannot certify no matter
    #    how clean the ratio. NFR-R1 is a ROLLING 30-day metric, so a certifying run must
    #    be EXACTLY the rolling window — a longer window would let downtime be diluted
    #    (a failing 30-day sub-period hidden in a passing 60-day average). The gate is an
    #    exact elapsed-ns equality — DST-robust because the certifying window is
    #    UTC-midnight (``market_sessions``), so an N-day period is exactly N*24 h.
    window_duration_ns = window_end_ns - window_start_ns
    period_reason = _rolling_period_mismatch(window_duration_ns, tgt)
    # A definite breach: the OBSERVED (counted) host-unplanned downtime alone already
    # exceeds the budget over the full effective period. Since counted downtime is real
    # and more coverage could only reveal MORE downtime, this is FAIL — not a coverage
    # question — so it is decided BEFORE the unmeasured-coverage INCONCLUSIVE (a
    # provable failure must never be downgraded to "insufficient evidence").
    downtime_breaches_budget = (
        1000 * counted_downtime_ns > (1000 - tgt.target_per_mille) * effective_market_ns
    )
    inconclusive_reason: str | None = None
    if period_reason is not None:
        verdict = Verdict.INCONCLUSIVE
        inconclusive_reason = period_reason
    elif excluded_in_session_ns > 0:
        verdict = Verdict.INCONCLUSIVE
        inconclusive_reason = (
            f"{excluded_in_session_ns / NS_PER_SECOND:.3f} s of exclusions fall inside "
            "market hours — NFR-R1 requires exclusions outside market hours"
        )
    elif downtime_breaches_budget:
        # observed downtime already over budget — provable failure, even if coverage
        # is incomplete.
        verdict = Verdict.FAIL
    elif unmeasured_market_ns > tgt.coverage_floor_ns:
        verdict = Verdict.INCONCLUSIVE
        inconclusive_reason = (
            f"{unmeasured_market_ns / NS_PER_SECOND:.3f} s of market hours are unmeasured "
            "(no coverage evidence) — cannot certify availability"
        )
    else:
        # fully measured within budget (and the period/exclusion preconditions hold).
        verdict = Verdict.PASS

    return AvailabilityVerificationArtifact(
        requirement=tgt.requirement,
        boundary=tgt.boundary,
        window_start_ns=window_start_ns,
        window_end_ns=window_end_ns,
        session_count=len(clipped_sessions),
        total_market_ns=total_market_ns,
        excluded_in_session_ns=excluded_in_session_ns,
        effective_market_ns=effective_market_ns,
        covered_market_ns=covered_market_ns,
        unmeasured_market_ns=unmeasured_market_ns,
        counted_downtime_ns=counted_downtime_ns,
        measured_up_ns=measured_up_ns,
        target_per_mille=tgt.target_per_mille,
        availability_ratio=availability_ratio,
        avg_downtime_per_session_seconds=avg_downtime_per_session_seconds,
        downtime_ns_by_cause=downtime_ns_by_cause,
        verdict=verdict,
        inconclusive_reason=inconclusive_reason,
    )


__all__ = [
    "AvailabilityError",
    "AvailabilityTarget",
    "AvailabilityVerificationArtifact",
    "COUNTING_CAUSES",
    "CoveredSpan",
    "DEFAULT_ROLLING_WINDOW_DAYS",
    "DEFAULT_TARGET_PER_MILLE",
    "DowntimeInterval",
    "EXCLUDED_CAUSES",
    "EmptyMeasurementWindow",
    "InvertedInterval",
    "MARKET_HOURS_BOUNDARY",
    "MarketSessionWindow",
    "NS_PER_SECOND",
    "NoTradingSessions",
    "OutageCause",
    "OverlappingSessions",
    "SRS_REL_001",
    "Verdict",
    "ZeroMarketExposure",
    "compute_availability",
]
