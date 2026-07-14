"""Pure, I/O-free full-system-restart RTO certification engine (SRS-REL-002).

SRS trace: ``SRS-REL-002`` (SyRS ``NFR-R6``, ``SYS-76``; StRS ``SN-2.05``). This is the
**recovery-time (RTO) analog** of the ``atp_reliability.availability`` (SRS-REL-001)
substrate: a dependency-free, clock-free measurement core that ingests a *caller-supplied*
restart timeline — the ordered boot phases and the SYS-76 readiness outcome — and produces a
:class:`RestartRecoveryArtifact` with a fail-closed three-valued verdict against the NFR-R6
objective: a full system restart must restore the platform to a **trade-ready state within 10
minutes** (Proxmox VM availability → OS boot → Docker daemon → ATP service init → SYS-76
readiness check).

Design invariants (mirror the SRS-REL-001 substrate + the certification-honesty discipline):

* **Pure.** Imports **no** ``atp_*`` runtime package and reads **no** clock. Every instant is an
  injected epoch-nanosecond ``int``. Evidence collection (``atp_reliability.boot_evidence``) and
  the CLI are the only layers that touch a host; this engine stays portable and deterministic.
  (``Verdict`` and ``NS_PER_SECOND`` are reused from the sibling availability engine — pure
  constants, no I/O.)
* **Integer nanoseconds internally.** All arithmetic is exact integer ns; the RTO verdict is an
  integer inequality (``elapsed_ns <= budget_ns``), never a lossy ``f64`` compare at the 600 s
  boundary.
* **End-to-end elapsed, not sum-of-phase-durations.** The clock runs from the restart trigger
  (``PROXMOX_VM`` phase start) to trade-ready (``READINESS_CHECK`` phase end). **Inter-phase gaps
  count against the budget** — idle time, retry loops, and crash-loops before readiness are real
  not-trade-ready wall-clock time. Summing per-phase durations would silently hide those gaps.
* **No-data ≠ trade-ready.** The complete required-phase set and the complete SYS-76 sub-check set
  are derived from the enum *authorities* (:data:`REQUIRED_PHASES` / :data:`REQUIRED_SUBCHECKS`),
  never from a caller-supplied list that could understate them. A missing required phase or
  sub-check is reported and **refuses certification** (:attr:`Verdict.INCONCLUSIVE`) rather than
  being silently treated as satisfied — a caller cannot drop the slow phase to squeak under budget.
* **Readiness must have *passed*, not merely run.** The ``READINESS_CHECK`` phase supplies only
  *timing*. Trade-ready additionally requires the SYS-76 gate to reach ``READY`` with every
  sub-check passing (NAS may be ``DEGRADED`` **only** with the operator alert raised, per
  SYS-76(d)). A manual ``OVERRIDDEN`` of a *failed* check does **not** certify — it is a human
  bypass (reachable only from ``PRE_TRADE_BLOCKED``), not an automatic recovery, so it is treated
  as *not* trade-ready. A readiness check observed *not* trade-ready is a positively-observed
  failure of the objective → :attr:`Verdict.FAIL`.
* **Provable failure beats missing evidence.** If the observed timeline span *alone* already
  breaches the 10-minute budget → ``FAIL`` — decided **before** any missing-phase/sub-check
  ``INCONCLUSIVE`` (a definite breach must never be downgraded to "insufficient evidence"). The
  observed span is an honest *lower bound* on true elapsed, valid even when phases are absent.
* **Fail closed.** Inverted/zero-duration phases, duplicate phases/sub-checks, a non-chronological
  phase *start* order, and evidence where a phase completes *after* the readiness check
  (trade-ready) all raise (refuse), never silently produce a verdict. Phases are chronological
  milestones that may legitimately **overlap/nest** — on a real boot Docker starts *during*
  userspace OS boot — so overlap itself is not an error; the end-to-end span (``max(end) -
  min(start)``) measures the recovery time regardless.

The NFR-R6 "during market hours" qualifier scopes *which* restart events the requirement applies
to. It is **not** a term in the elapsed arithmetic (a raw epoch-ns subtraction, DST-irrelevant) and
this engine computes **no** calendar; instead an ``SRS-REL-002`` PASS **requires** the caller to
prove scope via ``during_market_hours=True``. Without that proof (``None`` unknown, or ``False``
out-of-hours) an otherwise-passing run is downgraded to :attr:`Verdict.INCONCLUSIVE` — a PASS
certification must not claim the market-hours objective for an out-of-scope or scope-unknown event.
A provable breach is still :attr:`Verdict.FAIL` regardless of scope (the scope gate never hides a
real breach). Non-SRS (informational) targets do not require the scope evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .availability import NS_PER_SECOND, Verdict

#: NFR-R6 recovery-time objective: trade-ready within 10 minutes.
DEFAULT_RTO_SECONDS = 600
DEFAULT_RTO_BUDGET_NS = DEFAULT_RTO_SECONDS * NS_PER_SECOND  # 600_000_000_000

#: Epoch nanoseconds fit in a signed 64-bit integer (the perf.rs / i64 convention; ~year 2262). A
#: timestamp outside ``[0, 2**63)`` is not a real epoch-ns value and is refused — this also keeps
#: the arithmetic and the float ``*_seconds`` rendering views from overflowing on a pathological
#: giant integer (``math.isfinite`` / ``int -> float`` would otherwise raise ``OverflowError``).
_MAX_NS = 2**63 - 1

#: The canonical requirement label. A target carrying this label is LOCKED to the NFR-R6 budget so
#: an artifact reading ``requirement: SRS-REL-002`` always means the full 10-minute objective was
#: applied. Relaxed configurations (engine-math unit tests) MUST use a different requirement label
#: so their artifact is honestly non-SRS.
SRS_REL_002 = "SRS-REL-002"

#: The NFR-R6 boundary phrase, pinned to the SyRS objective. Mirrored in
#: ``architecture/runtime_services.json#restart_recovery_contract`` and re-asserted by the L3
#: contract test so the boundary cannot silently drift.
RESTART_BOUNDARY = (
    "full system restart to trade-ready state within 10 minutes: Proxmox VM availability, "
    "OS boot, Docker daemon startup, ATP service initialization, and SYS-76 readiness-check "
    "completion (NFR-R6)"
)


class RestartPhase(StrEnum):
    """The ordered NFR-R6 restart phases. Declaration order IS chronological order.

    The restart trigger (``t0``) is the start of :attr:`PROXMOX_VM`; trade-ready is the end of
    :attr:`READINESS_CHECK`. Both are required members of :data:`REQUIRED_PHASES`, so neither can
    be dropped without tripping the missing-phase ``INCONCLUSIVE``.
    """

    PROXMOX_VM = "proxmox_vm"  # Proxmox VM availability — the restart-trigger / t0 anchor
    OS_BOOT = "os_boot"  # guest OS boot
    DOCKER_DAEMON = "docker_daemon"  # Docker daemon startup
    ATP_SERVICE_INIT = "atp_service_init"  # ATP service (Docker Compose stack) initialization
    READINESS_CHECK = "readiness_check"  # SYS-76 readiness-check completion — trade-ready anchor


#: The COMPLETE required phase set, derived from the enum authority (never a caller list). A caller
#: who omits a phase produces a non-empty ``missing`` set → INCONCLUSIVE.
REQUIRED_PHASES: tuple[RestartPhase, ...] = tuple(RestartPhase)

#: Canonical chronological rank of each phase (declaration order).
_PHASE_ORDER: dict[RestartPhase, int] = {p: i for i, p in enumerate(RestartPhase)}


class SubCheck(StrEnum):
    """The SYS-76 readiness sub-checks (a)-(e) — the authority for the complete sub-check set."""

    IB_CONNECTIVITY = "ib_connectivity"  # (a) IB Gateway connectivity established + authenticated
    IB_ACCOUNT = "ib_account"  # (b) IB account accessible + account data received
    DATA_LAYER_SSD = "data_layer_ssd"  # (c) SSD tier accessible + ingestion not stale > 1 day
    NAS_ARCHIVAL = "nas_archival"  # (d) NAS reachable (degraded acceptable WITH operator alert)
    SYSTEM_SERVICES = "system_services"  # (e) exec/sim/data/notification/dashboard healthy


#: The COMPLETE SYS-76 sub-check set, derived from the enum authority. A caller cannot claim
#: readiness "passed" while silently omitting the stale-data or service-health check.
REQUIRED_SUBCHECKS: frozenset[SubCheck] = frozenset(SubCheck)


class SubCheckStatus(StrEnum):
    """Outcome of a single SYS-76 sub-check.

    ``DEGRADED`` is meaningful only for :attr:`SubCheck.NAS_ARCHIVAL` (SYS-76(d): degraded-mode
    operation is acceptable if NAS is unavailable *with* an operator alert). ``DEGRADED`` on any
    other sub-check, or NAS ``DEGRADED`` without the alert, is treated as *not passing*.
    """

    PASS = "pass"
    FAIL = "fail"
    DEGRADED = "degraded"


class GateOutcome(StrEnum):
    """The SYS-76 readiness-gate state. Values are pinned to ``atp_readiness.GateState``.

    Declared locally (identical string values) so this engine stays free of any ``atp_*`` import;
    the L3 contract test cross-checks value parity against ``atp_readiness.GateState`` so the
    vocabulary cannot silently drift. Only :attr:`READY` **certifies** a clean trade-ready recovery
    (see :data:`TRADE_READY_STATES`). :attr:`OVERRIDDEN` is reachable in the ``atp_readiness`` state
    machine **only** from :attr:`PRE_TRADE_BLOCKED` — i.e. a SYS-76 readiness check *failed* and an
    operator manually released the hold — so it is a human bypass, not the system restoring itself
    to trade-ready, and it does **not** certify an automatic NFR-R6 recovery.
    :attr:`INITIALIZING` and :attr:`PRE_TRADE_BLOCKED` are likewise not trade-ready.
    """

    INITIALIZING = "initializing"
    PRE_TRADE_BLOCKED = "pre_trade_blocked"
    READY = "ready"
    OVERRIDDEN = "overridden"


#: Gate states that certify a CLEAN (automatic) trade-ready recovery for SRS-REL-002. Only
#: :attr:`GateOutcome.READY` qualifies — see the :class:`GateOutcome` note: an ``OVERRIDDEN`` gate
#: means a failed readiness check was manually bypassed, which must not mint a clean NFR-R6 PASS.
#: Kept as a single-source constant so the "which states certify" contract cannot drift.
TRADE_READY_STATES: frozenset[GateOutcome] = frozenset({GateOutcome.READY})


# --------------------------------------------------------------------------- #
# Fail-closed error hierarchy.
# --------------------------------------------------------------------------- #


class RestartError(Exception):
    """Base class for fail-closed restart-recovery measurement errors."""


class EmptyRestartTimeline(RestartError):
    """No restart phases were supplied — there is no timeline to measure."""


class PhaseInversionError(RestartError):
    """A phase has ``end_ns <= start_ns`` (a boot phase must have positive duration)."""


class DuplicatePhaseError(RestartError):
    """The same :class:`RestartPhase` appears more than once."""


class PhaseOrderError(RestartError):
    """A phase begins before its canonical predecessor, or completes after the readiness check.

    Both are impossible orderings: a canonically-later phase cannot *start* before an earlier one,
    and nothing may *complete* after trade-ready (the readiness-check end). Overlap/nesting between
    the *start* of one phase and the *end* of another is legitimate and is NOT an error.
    """


class DuplicateSubCheckError(RestartError):
    """The same :class:`SubCheck` appears more than once in the readiness outcome."""


class InvalidTimestamp(RestartError):
    """A supplied timestamp is not a finite, non-negative integer nanosecond value."""


# --------------------------------------------------------------------------- #
# Evidence value objects.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ObservedPhase:
    """A single observed restart phase ``[start_ns, end_ns)`` (epoch ns)."""

    phase: RestartPhase
    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class SubCheckResult:
    """The observed result of one SYS-76 sub-check.

    ``alert_raised`` is only consulted for :attr:`SubCheck.NAS_ARCHIVAL` ``DEGRADED``; it records
    whether the SYS-76(d) operator alert accompanied degraded-mode operation.
    """

    check: SubCheck
    status: SubCheckStatus
    alert_raised: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessOutcome:
    """The SYS-76 readiness-check *result* (distinct from the ``READINESS_CHECK`` phase *timing*).

    A ``None`` outcome (passed to :func:`compute_restart_recovery`) means no readiness evidence was
    observed at all; a present outcome with some :data:`REQUIRED_SUBCHECKS` member absent means the
    complete SYS-76 set was not observed. Both refuse certification.
    """

    gate_state: GateOutcome
    subchecks: tuple[SubCheckResult, ...] = ()


@dataclass(frozen=True, slots=True)
class RestartRecoveryTarget:
    """The NFR-R6 objective (single-source constants).

    A target labelled :data:`SRS_REL_002` is validated to the canonical 10-minute budget;
    constructing one with a weakened budget/boundary raises ``ValueError`` — you cannot mint a
    relaxed target that still claims to certify SRS-REL-002.
    """

    requirement: str = SRS_REL_002
    budget_ns: int = DEFAULT_RTO_BUDGET_NS
    boundary: str = RESTART_BOUNDARY

    def __post_init__(self) -> None:
        # Explicit raise (NOT assert, which ``python -O`` strips) so a weakened SRS-labelled target
        # can never silently certify.
        if self.requirement == SRS_REL_002 and (
            self.budget_ns != DEFAULT_RTO_BUDGET_NS or self.boundary != RESTART_BOUNDARY
        ):
            raise ValueError(
                "a RestartRecoveryTarget labelled 'SRS-REL-002' must use the canonical NFR-R6 "
                f"budget ({DEFAULT_RTO_BUDGET_NS} ns = {DEFAULT_RTO_SECONDS} s = 10 min) and "
                "boundary; use a different requirement label for a relaxed or non-certifying "
                "configuration so its artifact is not mislabelled."
            )
        if not _is_valid_ns(self.budget_ns) or self.budget_ns <= 0:
            raise ValueError(f"budget_ns must be a positive integer ns; got {self.budget_ns!r}")


# --------------------------------------------------------------------------- #
# Verification artifact.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RestartRecoveryArtifact:
    """The SRS-REL-002 verification artifact — the RTO system-test evidence.

    Canonical quantities are exact integer nanoseconds; ``*_seconds`` views are convenience floats
    for humans/JSON. ``elapsed_ns`` is ``None`` when an anchor phase is absent (elapsed cannot be
    fully computed); ``observed_span_ns`` is always the provable lower bound on true elapsed.
    """

    requirement: str
    boundary: str
    budget_ns: int
    t0_ns: int | None
    trade_ready_ns: int | None
    elapsed_ns: int | None
    observed_span_ns: int
    phases_present: tuple[str, ...]
    missing_phases: tuple[str, ...]
    phase_durations_ns: dict[str, int]
    gate_state: str | None
    subcheck_status: dict[str, str]
    missing_subchecks: tuple[str, ...]
    readiness_trade_ready: bool
    during_market_hours: bool | None
    verdict: Verdict
    verdict_reason: str | None = field(default=None)

    @property
    def certified(self) -> bool:
        """True only when the artifact proves the NFR-R6 objective (``PASS``)."""

        return self.verdict is Verdict.PASS

    def as_dict(self) -> dict[str, object]:
        """Render as a JSON-serialisable dict (exact ns + convenience seconds)."""

        def _sec(ns: int | None) -> float | None:
            return None if ns is None else ns / NS_PER_SECOND

        return {
            "requirement": self.requirement,
            "boundary": self.boundary,
            "budget_ns": self.budget_ns,
            "budget_seconds": self.budget_ns / NS_PER_SECOND,
            "t0_ns": self.t0_ns,
            "trade_ready_ns": self.trade_ready_ns,
            "elapsed_ns": self.elapsed_ns,
            "elapsed_seconds": _sec(self.elapsed_ns),
            "observed_span_ns": self.observed_span_ns,
            "observed_span_seconds": self.observed_span_ns / NS_PER_SECOND,
            "phases_present": list(self.phases_present),
            "missing_phases": list(self.missing_phases),
            "phase_durations_seconds": {
                name: ns / NS_PER_SECOND for name, ns in self.phase_durations_ns.items()
            },
            "gate_state": self.gate_state,
            "subcheck_status": dict(self.subcheck_status),
            "missing_subchecks": list(self.missing_subchecks),
            "readiness_trade_ready": self.readiness_trade_ready,
            "during_market_hours": self.during_market_hours,
            "verdict": self.verdict.value,
            "certified": self.certified,
            "verdict_reason": self.verdict_reason,
        }

    def __str__(self) -> str:
        """A stable, inspectable rendering — the human-readable artifact."""

        elapsed = "n/a" if self.elapsed_ns is None else f"{self.elapsed_ns / NS_PER_SECOND:.3f} s"
        lines = [
            f"{self.requirement} restart-recovery verification artifact",
            f"  boundary: {self.boundary}",
            f"  budget: {self.budget_ns / NS_PER_SECOND:.1f} s",
            f"  elapsed (trade-ready - trigger): {elapsed}",
            f"  observed span (lower bound): {self.observed_span_ns / NS_PER_SECOND:.3f} s",
            f"  phases present: {', '.join(self.phases_present) or '(none)'}",
        ]
        if self.missing_phases:
            lines.append(f"  phases MISSING: {', '.join(self.missing_phases)}")
        lines.append(f"  readiness gate: {self.gate_state or '(none)'}")
        if self.subcheck_status:
            lines.append("  sub-checks:")
            for name in sorted(self.subcheck_status):
                lines.append(f"    {name}: {self.subcheck_status[name]}")
        if self.missing_subchecks:
            lines.append(f"  sub-checks MISSING: {', '.join(self.missing_subchecks)}")
        lines.append(f"  trade-ready: {self.readiness_trade_ready}")
        lines.append(f"  during market hours: {self.during_market_hours}")
        verdict_line = f"  verdict: {self.verdict.value}"
        if self.verdict_reason is not None:
            verdict_line += f" ({self.verdict_reason})"
        lines.append(verdict_line)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _is_valid_ns(value: object) -> bool:
    """True iff ``value`` is an ``int`` in the valid epoch-ns range ``[0, 2**63)``.

    ``bool`` is a subclass of ``int`` in Python, so it is rejected explicitly — a ``True``
    timestamp must never be coerced to ``1 ns``. The upper bound refuses a pathological giant
    integer that would otherwise overflow the ``int -> float`` rendering views (``math.isfinite``
    is deliberately NOT used: it raises ``OverflowError`` on a huge ``int`` rather than returning a
    clean ``False``, which would escape the fail-closed refusal path).
    """

    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 0 <= value <= _MAX_NS


def _subcheck_passes(result: SubCheckResult) -> bool:
    """True iff a present sub-check result satisfies SYS-76.

    ``PASS`` always passes; NAS ``DEGRADED`` passes only with ``alert_raised`` (SYS-76(d)); every
    other status (``FAIL``; ``DEGRADED`` on a non-NAS check; NAS ``DEGRADED`` without alert) fails.
    """

    if result.status is SubCheckStatus.PASS:
        return True
    if (
        result.check is SubCheck.NAS_ARCHIVAL
        and result.status is SubCheckStatus.DEGRADED
        and result.alert_raised
    ):
        return True
    return False


# --------------------------------------------------------------------------- #
# The engine.
# --------------------------------------------------------------------------- #


def compute_restart_recovery(
    *,
    phases: Sequence[ObservedPhase],
    readiness: ReadinessOutcome | None,
    during_market_hours: bool | None = None,
    target: RestartRecoveryTarget | None = None,
) -> RestartRecoveryArtifact:
    """Compute the NFR-R6 restart-recovery artifact from caller-supplied timeline evidence.

    Args:
        phases: the observed restart phases (``[start_ns, end_ns)``). Validated for positive
            duration, uniqueness, and chronological start order (phases may overlap/nest; nothing
            may complete after the readiness check). The required set is :data:`REQUIRED_PHASES`;
            any absent member is reported as missing and blocks certification.
        readiness: the SYS-76 readiness *result* (gate state + sub-check results), or ``None`` when
            no readiness evidence was observed. Trade-ready requires the gate in
            :data:`TRADE_READY_STATES` and every :data:`REQUIRED_SUBCHECKS` member present and
            passing (NAS ``DEGRADED`` only with the operator alert).
        during_market_hours: whether this restart occurred *during US-equity market hours* — the
            scope NFR-R6 applies to. Only the literal ``True`` proves in-scope. For a target
            labelled :data:`SRS_REL_002`, a ``PASS`` requires this proof: if it is ``None``
            (unknown) or ``False`` (out of scope), an otherwise-passing run is downgraded to
            ``INCONCLUSIVE`` (a provable breach still ``FAIL``s). Non-SRS targets do not require it.
        target: the NFR-R6 objective; defaults to the canonical 10-minute :class:`RestartRecoveryTarget`.

    Raises:
        EmptyRestartTimeline, PhaseInversionError, DuplicatePhaseError, PhaseOrderError,
        DuplicateSubCheckError, InvalidTimestamp — all fail-closed.
    """

    tgt = target or RestartRecoveryTarget()
    scope_required = tgt.requirement == SRS_REL_002
    market_hours_proven = during_market_hours is True

    # 1. Validate the phase timeline (structural refusals precede any verdict).
    if not phases:
        raise EmptyRestartTimeline("no restart phases supplied")

    by_phase: dict[RestartPhase, ObservedPhase] = {}
    for p in phases:
        if not isinstance(p.phase, RestartPhase):
            raise RestartError(f"phase must be a RestartPhase; got {p.phase!r}")
        if not _is_valid_ns(p.start_ns) or not _is_valid_ns(p.end_ns):
            raise InvalidTimestamp(
                f"phase {p.phase.value} bounds must be finite non-negative int ns; "
                f"got [{p.start_ns!r}, {p.end_ns!r}]"
            )
        if p.end_ns <= p.start_ns:
            raise PhaseInversionError(
                f"phase {p.phase.value} is inverted/zero-duration: "
                f"end_ns {p.end_ns} <= start_ns {p.start_ns}"
            )
        if p.phase in by_phase:
            raise DuplicatePhaseError(f"phase {p.phase.value} supplied more than once")
        by_phase[p.phase] = p

    # 2. Ordering checks over the present phases (in canonical order). Phases are chronological
    #    MILESTONES that may legitimately overlap/nest (on a real boot Docker starts DURING
    #    userspace OS boot), so overlap is NOT an error. The honest invariants are: (a) canonical
    #    START order — a canonically-later phase must not begin before an earlier one; and (b) the
    #    readiness check, when present, is the TERMINAL event — nothing may complete after
    #    trade-ready.
    ordered = sorted(by_phase.values(), key=lambda op: _PHASE_ORDER[op.phase])
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        if nxt.start_ns < prev.start_ns:
            raise PhaseOrderError(
                f"phase {nxt.phase.value} (canonically after {prev.phase.value}) starts earlier: "
                f"{nxt.start_ns} < {prev.start_ns}"
            )
    ready_phase = by_phase.get(RestartPhase.READINESS_CHECK)
    if ready_phase is not None:
        for op in ordered:
            if op.end_ns > ready_phase.end_ns:
                raise PhaseOrderError(
                    f"phase {op.phase.value} ends at {op.end_ns}, after the readiness-check "
                    f"(trade-ready) end {ready_phase.end_ns} — nothing may complete after trade-ready"
                )

    # 3. Spans + anchors. ``observed_span`` = ``max(present end) - min(present start)`` — the
    #    provable lower bound on true elapsed, correct even with overlapping/nested phases (a nested
    #    phase must not shrink the span). ``elapsed`` is the exact end-to-end recovery time and needs
    #    BOTH anchor phases; when both are present (and the readiness terminal invariant holds)
    #    ``elapsed_ns == observed_span_ns``.
    observed_span_ns = max(op.end_ns for op in ordered) - min(op.start_ns for op in ordered)
    trigger = by_phase.get(RestartPhase.PROXMOX_VM)
    t0_ns = trigger.start_ns if trigger is not None else None
    trade_ready_ns = ready_phase.end_ns if ready_phase is not None else None
    elapsed_ns = (
        trade_ready_ns - t0_ns if (t0_ns is not None and trade_ready_ns is not None) else None
    )

    phase_durations_ns = {op.phase.value: op.end_ns - op.start_ns for op in ordered}
    phases_present = tuple(op.phase.value for op in ordered)
    missing_phases = tuple(
        p.value for p in REQUIRED_PHASES if p not in by_phase
    )  # canonical order preserved

    # 4. Readiness evaluation.
    gate_state: str | None = None
    subcheck_status: dict[str, str] = {}
    missing_subchecks: tuple[str, ...] = tuple(sc.value for sc in _sorted_subchecks(REQUIRED_SUBCHECKS))
    readiness_present = readiness is not None
    readiness_gate_ready = False
    any_subcheck_failing = False
    if readiness is not None:
        if not isinstance(readiness.gate_state, GateOutcome):
            raise RestartError(f"readiness gate_state must be a GateOutcome; got {readiness.gate_state!r}")
        gate_state = readiness.gate_state.value
        readiness_gate_ready = readiness.gate_state in TRADE_READY_STATES
        seen: dict[SubCheck, SubCheckResult] = {}
        for r in readiness.subchecks:
            if not isinstance(r.check, SubCheck):
                raise RestartError(f"sub-check must be a SubCheck; got {r.check!r}")
            if not isinstance(r.status, SubCheckStatus):
                raise RestartError(f"sub-check status must be a SubCheckStatus; got {r.status!r}")
            if r.check in seen:
                raise DuplicateSubCheckError(f"sub-check {r.check.value} supplied more than once")
            seen[r.check] = r
            passes = _subcheck_passes(r)
            label = r.status.value
            if r.check is SubCheck.NAS_ARCHIVAL and r.status is SubCheckStatus.DEGRADED:
                label = f"degraded(alert={'yes' if r.alert_raised else 'no'})"
            subcheck_status[r.check.value] = label
            if not passes:
                any_subcheck_failing = True
        missing = REQUIRED_SUBCHECKS - set(seen)
        missing_subchecks = tuple(sc.value for sc in _sorted_subchecks(missing))

    # A readiness outcome is *observed not trade-ready* (a positively-observed objective failure)
    # when the gate is not a trade-ready state OR any PRESENT sub-check fails. A merely-absent
    # sub-check is missing evidence (INCONCLUSIVE), not an observed failure.
    readiness_observed_not_ready = readiness_present and (
        not readiness_gate_ready or any_subcheck_failing
    )
    # Fully trade-ready requires a present outcome, a ready gate, no failing sub-check, and NO
    # missing sub-check.
    readiness_trade_ready = (
        readiness_present
        and readiness_gate_ready
        and not any_subcheck_failing
        and not missing_subchecks
    )

    # 5. Verdict — three-valued, fail-closed. Provable failure is decided BEFORE any
    #    missing-evidence INCONCLUSIVE (a definite breach must never read as "insufficient
    #    evidence"). ``observed_span`` (lower bound) drives the breach check so it holds even when
    #    an anchor phase is absent.
    over_budget = observed_span_ns > tgt.budget_ns
    verdict_reason: str | None = None
    if over_budget:
        verdict = Verdict.FAIL
        verdict_reason = (
            f"observed restart span {observed_span_ns / NS_PER_SECOND:.3f} s exceeds the "
            f"{tgt.budget_ns / NS_PER_SECOND:.1f} s (10 min) NFR-R6 budget"
        )
    elif readiness_observed_not_ready:
        verdict = Verdict.FAIL
        if readiness is not None and readiness.gate_state is GateOutcome.OVERRIDDEN:
            verdict_reason = (
                "readiness hold was manually OVERRIDDEN (a failed SYS-76 check was bypassed) — an "
                "automatic NFR-R6 recovery to trade-ready cannot be certified from an override"
            )
        else:
            verdict_reason = (
                f"readiness check did not reach a trade-ready state (gate={gate_state}"
                + (", a sub-check failed" if any_subcheck_failing else "")
                + ") — the restart did not restore trade-ready"
            )
    elif missing_phases:
        verdict = Verdict.INCONCLUSIVE
        verdict_reason = (
            f"restart timeline incomplete — missing phase(s): {', '.join(missing_phases)}; "
            "cannot certify the full boot-to-trade-ready recovery time"
        )
    elif not readiness_present:
        verdict = Verdict.INCONCLUSIVE
        verdict_reason = "no SYS-76 readiness evidence — cannot certify a trade-ready state"
    elif missing_subchecks:
        verdict = Verdict.INCONCLUSIVE
        verdict_reason = (
            f"SYS-76 readiness incomplete — missing sub-check(s): {', '.join(missing_subchecks)}; "
            "cannot certify the readiness check passed"
        )
    elif scope_required and not market_hours_proven:
        # The RTO measurement is clean, but NFR-R6 scopes the objective to MARKET-HOURS restarts.
        # An SRS-REL-002 PASS must prove the event was in scope; without that proof (unknown or
        # explicitly out-of-hours) the certification cannot be minted. (A provable breach above is
        # still FAIL — this gate only blocks an unproven-scope PASS, never hides a real breach.)
        verdict = Verdict.INCONCLUSIVE
        verdict_reason = (
            "NFR-R6 scopes the objective to full-system restarts DURING market hours; "
            "during_market_hours is not proven true — cannot certify SRS-REL-002 (supply "
            "restart_context.during_market_hours=true, or use a non-SRS label for an informational "
            "report)"
        )
    else:
        # Complete phase set, complete passing sub-checks, ready gate, elapsed within budget, and
        # (for SRS-REL-002) the market-hours scope proven.
        verdict = Verdict.PASS

    return RestartRecoveryArtifact(
        requirement=tgt.requirement,
        boundary=tgt.boundary,
        budget_ns=tgt.budget_ns,
        t0_ns=t0_ns,
        trade_ready_ns=trade_ready_ns,
        elapsed_ns=elapsed_ns,
        observed_span_ns=observed_span_ns,
        phases_present=phases_present,
        missing_phases=missing_phases,
        phase_durations_ns=phase_durations_ns,
        gate_state=gate_state,
        subcheck_status=subcheck_status,
        missing_subchecks=missing_subchecks,
        readiness_trade_ready=readiness_trade_ready,
        during_market_hours=during_market_hours,
        verdict=verdict,
        verdict_reason=verdict_reason,
    )


def _sorted_subchecks(checks: frozenset[SubCheck] | set[SubCheck]) -> list[SubCheck]:
    """Return sub-checks in canonical (declaration) order for stable output."""

    order = {sc: i for i, sc in enumerate(SubCheck)}
    return sorted(checks, key=lambda sc: order[sc])


__all__ = [
    "DEFAULT_RTO_BUDGET_NS",
    "DEFAULT_RTO_SECONDS",
    "DuplicatePhaseError",
    "DuplicateSubCheckError",
    "EmptyRestartTimeline",
    "GateOutcome",
    "InvalidTimestamp",
    "NS_PER_SECOND",
    "ObservedPhase",
    "PhaseInversionError",
    "PhaseOrderError",
    "REQUIRED_PHASES",
    "REQUIRED_SUBCHECKS",
    "ReadinessOutcome",
    "RestartError",
    "RestartPhase",
    "RestartRecoveryArtifact",
    "RestartRecoveryTarget",
    "SRS_REL_002",
    "SubCheck",
    "SubCheckResult",
    "SubCheckStatus",
    "TRADE_READY_STATES",
    "Verdict",
    "compute_restart_recovery",
]
