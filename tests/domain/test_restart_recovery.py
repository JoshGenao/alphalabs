"""L7 domain safety test for the SRS-REL-002 restart-recovery measurement substrate.

Marked ``safety`` + ``domain`` so the deterministic critic recognises this as the paired
safety-path test for the RTO substrate. The restart-recovery verdict is a safety-relevant claim
(it certifies whether a full system restart restored the trading platform to a *trade-ready* state
within the NFR-R6 10-minute recovery target), so these tests turn on the invariants that keep the
claim honest:

* a restart is certified PASS **only** when the complete boot timeline is observed, elapsed is
  within budget, AND the SYS-76 readiness check actually reached a trade-ready state with every
  sub-check passing — a readiness check that merely *ran* is never mistaken for trade-ready;
* **inter-phase gaps count** against the budget (idle / retry / crash-loop time is real
  not-trade-ready wall-clock — it cannot be hidden by summing per-phase durations);
* a **provable** budget breach (or an observed not-ready readiness) is ``FAIL``, decided before any
  missing-evidence ``INCONCLUSIVE`` — a definite failure is never downgraded to "insufficient
  evidence";
* dropping a required phase or a SYS-76 sub-check **refuses** certification (a caller cannot
  understate the required set to squeak under budget);
* NAS degraded-mode is trade-ready **only** with the SYS-76(d) operator alert;
* the ``SRS-REL-002`` label locks the 10-minute budget (a weakened SRS-labelled target raises);
* the engine reads **no** wall clock (every instant is injected — no clock-skew fabrication).

Scope: these exercise the in-process measurement mechanism over fixtures. The real system-test run
on the reference Proxmox/Docker deployment, and the SYS-76 runtime readiness probes (deferred to
SRS-MD-006), are deferred (see ``restart_recovery_contract.deferred``); this file asserts the
mechanism is correct and honest, not that the 10-minute objective is met on real hardware.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from atp_reliability.restart import (
    DEFAULT_RTO_BUDGET_NS,
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
)
from atp_reliability.restart import compute_restart_recovery as _compute_raw

pytestmark = [pytest.mark.domain, pytest.mark.safety]

S = NS_PER_SECOND


def compute_restart_recovery(*, during_market_hours: bool | None = True, **kwargs: object):
    """Test shim: default the NFR-R6 market-hours scope to proven so PASS-path domain tests read
    cleanly; scope-specific behavior is covered in the L1/L3 suites.
    """

    return _compute_raw(during_market_hours=during_market_hours, **kwargs)  # type: ignore[arg-type]


def _phases(ready_end_s: int = 130) -> list[ObservedPhase]:
    bounds = [
        (RestartPhase.PROXMOX_VM, 0, 10),
        (RestartPhase.OS_BOOT, 10, 60),
        (RestartPhase.DOCKER_DAEMON, 60, 90),
        (RestartPhase.ATP_SERVICE_INIT, 90, 120),
        (RestartPhase.READINESS_CHECK, 120, ready_end_s),
    ]
    return [ObservedPhase(p, a * S, b * S) for p, a, b in bounds]


def _ready(**kw: object) -> ReadinessOutcome:
    gate = kw.get("gate", GateOutcome.READY)
    nas_degraded = kw.get("nas_degraded", False)
    nas_alert = kw.get("nas_alert", False)
    subs = []
    for sc in SubCheck:
        if sc is SubCheck.NAS_ARCHIVAL and nas_degraded:
            subs.append(SubCheckResult(sc, SubCheckStatus.DEGRADED, bool(nas_alert)))
        else:
            subs.append(SubCheckResult(sc, SubCheckStatus.PASS))
    return ReadinessOutcome(gate_state=gate, subchecks=tuple(subs))  # type: ignore[arg-type]


def test_certifies_only_a_complete_in_budget_trade_ready_restart() -> None:
    art = compute_restart_recovery(phases=_phases(), readiness=_ready())
    assert art.certified is True
    assert art.verdict is Verdict.PASS
    assert art.elapsed_ns <= DEFAULT_RTO_BUDGET_NS


def test_readiness_that_only_ran_is_not_trade_ready() -> None:
    # The readiness-check phase is present (it ran and finished in time) but the gate is still
    # pre-trade-blocked → the platform is NOT trade-ready → FAIL, never PASS.
    art = compute_restart_recovery(
        phases=_phases(), readiness=_ready(gate=GateOutcome.PRE_TRADE_BLOCKED)
    )
    assert art.verdict is Verdict.FAIL
    assert art.readiness_trade_ready is False


def test_gap_time_is_not_hidden() -> None:
    # A crash-loop gap before readiness: durations tiny, but end-to-end > 10 min → FAIL.
    gapped = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 1 * S),
        ObservedPhase(RestartPhase.OS_BOOT, 5 * S, 6 * S),
        ObservedPhase(RestartPhase.DOCKER_DAEMON, 10 * S, 11 * S),
        ObservedPhase(RestartPhase.ATP_SERVICE_INIT, 15 * S, 16 * S),
        ObservedPhase(RestartPhase.READINESS_CHECK, 900 * S, 901 * S),
    ]
    art = compute_restart_recovery(phases=gapped, readiness=_ready())
    assert sum(art.phase_durations_ns.values()) < DEFAULT_RTO_BUDGET_NS
    assert art.verdict is Verdict.FAIL


def test_provable_breach_beats_missing_evidence() -> None:
    # Over budget AND missing phases AND no readiness → still FAIL (provable breach wins).
    two = [
        ObservedPhase(RestartPhase.PROXMOX_VM, 0, 1 * S),
        ObservedPhase(RestartPhase.READINESS_CHECK, 700 * S, 701 * S),
    ]
    assert compute_restart_recovery(phases=two, readiness=None).verdict is Verdict.FAIL


def test_dropping_a_phase_or_subcheck_refuses_certification() -> None:
    no_phase = compute_restart_recovery(
        phases=[p for p in _phases() if p.phase is not RestartPhase.DOCKER_DAEMON],
        readiness=_ready(),
    )
    assert no_phase.verdict is Verdict.INCONCLUSIVE
    assert "docker_daemon" in no_phase.missing_phases

    dropped_sub = ReadinessOutcome(
        gate_state=GateOutcome.READY,
        subchecks=tuple(
            SubCheckResult(sc, SubCheckStatus.PASS)
            for sc in SubCheck
            if sc is not SubCheck.DATA_LAYER_SSD
        ),
    )
    no_sub = compute_restart_recovery(phases=_phases(), readiness=dropped_sub)
    assert no_sub.verdict is Verdict.INCONCLUSIVE
    assert "data_layer_ssd" in no_sub.missing_subchecks


def test_nas_degraded_requires_operator_alert() -> None:
    assert compute_restart_recovery(
        phases=_phases(), readiness=_ready(nas_degraded=True, nas_alert=True)
    ).verdict is Verdict.PASS
    assert compute_restart_recovery(
        phases=_phases(), readiness=_ready(nas_degraded=True, nas_alert=False)
    ).verdict is Verdict.FAIL


def test_srs_label_locks_the_ten_minute_budget() -> None:
    with pytest.raises(ValueError, match="SRS-REL-002"):
        RestartRecoveryTarget(requirement="SRS-REL-002", budget_ns=20 * 60 * S)


def test_engine_reads_no_wall_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _time

    monkeypatch.setattr(
        _time, "time_ns", lambda: (_ for _ in ()).throw(AssertionError("clock read"))
    )
    assert compute_restart_recovery(phases=_phases(), readiness=_ready()).verdict is Verdict.PASS


def test_label_lock_guard_survives_python_dash_O() -> None:
    # __post_init__ uses an explicit raise (not assert, which `python -O` strips), so a weakened
    # SRS-REL-002 target must still be rejected under optimization.
    python_root = Path(__file__).resolve().parents[2] / "python"
    code = (
        f"import sys; sys.path.insert(0, {str(python_root)!r});"
        "from atp_reliability.restart import RestartRecoveryTarget;"
        "RestartRecoveryTarget(requirement='SRS-REL-002', budget_ns=1)"
    )
    result = subprocess.run(
        [sys.executable, "-O", "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0
    assert "SRS-REL-002" in result.stderr
