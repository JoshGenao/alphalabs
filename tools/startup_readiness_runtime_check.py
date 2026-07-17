#!/usr/bin/env python3
"""SRS-MD-006 runtime readiness contract check (boot/CI parity).

Statically and behaviourally pins the ``startup_readiness_runtime_contract``
block in ``architecture/runtime_services.json`` against the shipped code:

* module paths exist; the SubCheck vocabulary is value-parity-pinned to
  ``atp_reliability.restart`` (single source, no fork);
* ``ReadinessGate.assert_runtime_ready_or_hold`` exists, refuses an
  unseeded gate, holds on an error-severity runtime report, and passes on a
  clean one — through the SAME pinned state machine;
* the fold is fail-closed: a missing sub-check blocks, a duplicate raises,
  NAS ``DEGRADED`` passes only with the alert, every failure dispatches an
  operator alert, and the alert sink is a REQUIRED keyword-only argument
  with no default;
* the freshness boundary is exact: fresh at the previous session close,
  stale one nanosecond earlier, stale on a ``None`` frontier;
* the paper gate requires BOTH prerequisites (missing key fails closed);
* the SDK gate module still carries none of the deferred-scope tokens (the
  ERR-9 leakage rule holds after the sibling-method extension).

Exit 0 with the PASS line on success; exit 1 with a diagnostic on the first
violation.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from atp_config import REQUIRED_KEYS, ReadinessReport  # noqa: E402
from atp_readiness import GateState, ReadinessGate  # noqa: E402
from atp_readiness import runtime as rt  # noqa: E402
from atp_readiness.errors import GateTransitionError, PreTradeHoldError  # noqa: E402
from atp_reliability import restart as restart_mod  # noqa: E402

CHECKS: list[str] = []


def _fail(message: str) -> None:
    print(f"SRS-MD-006 RUNTIME FAIL: {message}")
    raise SystemExit(1)


def _ok(message: str) -> None:
    CHECKS.append(message)


def _block() -> dict:
    raw = json.loads((ROOT / "architecture" / "runtime_services.json").read_text())
    block = raw.get("startup_readiness_runtime_contract")
    if not isinstance(block, dict):
        _fail("runtime_services.json is missing startup_readiness_runtime_contract")
    return block


class _RecordingSink:
    def __init__(self) -> None:
        self.alerts: list[rt.ReadinessAlert] = []

    def dispatch(self, alert: rt.ReadinessAlert) -> None:
        self.alerts.append(alert)


class _FixedCalendar:
    def __init__(self, close_ns: int) -> None:
        self._close_ns = close_ns

    def previous_session_close_ns(self, now_ns: int) -> int:
        return self._close_ns


def _valid_env() -> dict[str, str]:
    return {spec.name: spec.default for spec in REQUIRED_KEYS if spec.default is not None}


def _all_pass_results() -> list[restart_mod.SubCheckResult]:
    return [
        restart_mod.SubCheckResult(check=check, status=restart_mod.SubCheckStatus.PASS)
        for check in sorted(restart_mod.REQUIRED_SUBCHECKS, key=lambda c: c.value)
    ]


def main() -> int:
    block = _block()

    # --- static parity ---------------------------------------------------- #
    for rel in block["module_paths"]:
        if not (ROOT / rel).is_file():
            _fail(f"contract-named module missing: {rel}")
    _ok(f"{len(block['module_paths'])} contract-named modules present")

    if block["subcheck_source"] != "atp_reliability.restart":
        _fail("subcheck_source must be atp_reliability.restart (single vocabulary)")
    contract_subchecks = list(block["subchecks"])
    code_subchecks = sorted(c.value for c in restart_mod.SubCheck)
    if sorted(contract_subchecks) != code_subchecks:
        _fail(
            f"subcheck value parity broken: contract {sorted(contract_subchecks)} "
            f"vs atp_reliability.restart {code_subchecks}"
        )
    _ok("SubCheck vocabulary value-parity holds (atp_reliability.restart)")

    services = sorted(block["required_services"])
    if services != sorted(s.value for s in rt.ReadinessService):
        _fail("required_services parity broken vs ReadinessService enum")
    papers = sorted(block["paper_prerequisites"])
    if papers != sorted(p.value for p in rt.PaperPrerequisite):
        _fail("paper_prerequisites parity broken vs PaperPrerequisite enum")
    _ok("service + paper prerequisite vocabularies parity hold")

    if block["runtime_gate_method"] != "assert_runtime_ready_or_hold" or not hasattr(
        ReadinessGate, "assert_runtime_ready_or_hold"
    ):
        _fail("ReadinessGate.assert_runtime_ready_or_hold missing")
    _ok("runtime gate method present on ReadinessGate")

    # --- mandatory alert sink (no default) -------------------------------- #
    for fn_name in (
        "build_runtime_report",
        "assert_paper_ready_or_hold",
        "release_hold_with_override",
    ):
        signature = inspect.signature(getattr(rt, fn_name))
        parameter = signature.parameters.get("alert_sink")
        if parameter is None or parameter.default is not inspect.Parameter.empty:
            _fail(f"{fn_name} must take alert_sink as a REQUIRED argument (no default)")
        if parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            _fail(f"{fn_name} alert_sink must be keyword-only")
    _ok("alert_sink is required keyword-only on all three fold entry points (no default)")

    # --- behavioural: fold fail-closed ------------------------------------ #
    sink = _RecordingSink()
    report = rt.build_runtime_report(_all_pass_results(), alert_sink=sink, timestamp_ns=1)
    if not report.ok or sink.alerts:
        _fail("all-PASS fold must be ok with no alerts")
    sink = _RecordingSink()
    report = rt.build_runtime_report(_all_pass_results()[:-1], alert_sink=sink, timestamp_ns=1)
    if report.ok or len(sink.alerts) != 1:
        _fail("a MISSING sub-check must be an error-severity failure with an alert")
    _ok("missing sub-check fails closed with an operator alert")

    try:
        rt.build_runtime_report(
            _all_pass_results() + [_all_pass_results()[0]],
            alert_sink=_RecordingSink(),
            timestamp_ns=1,
        )
    except rt.DuplicateSubCheckError:
        _ok("duplicate sub-check refused")
    else:
        _fail("duplicate sub-check must raise DuplicateSubCheckError")

    def _nas(status: restart_mod.SubCheckStatus, alert_raised: bool) -> list:
        results = [
            r for r in _all_pass_results() if r.check is not restart_mod.SubCheck.NAS_ARCHIVAL
        ]
        results.append(
            restart_mod.SubCheckResult(
                check=restart_mod.SubCheck.NAS_ARCHIVAL, status=status, alert_raised=alert_raised
            )
        )
        return results

    sink = _RecordingSink()
    degraded_ok = rt.build_runtime_report(
        _nas(restart_mod.SubCheckStatus.DEGRADED, True), alert_sink=sink, timestamp_ns=1
    )
    if not degraded_ok.ok:
        _fail("NAS DEGRADED with alert_raised must pass the gate")
    if not any(a.kind is rt.ReadinessAlertKind.NAS_DEGRADED_MODE for a in sink.alerts):
        _fail("accepted NAS degraded-mode must still dispatch the SYS-76(d) alert")
    degraded_bad = rt.build_runtime_report(
        _nas(restart_mod.SubCheckStatus.DEGRADED, False),
        alert_sink=_RecordingSink(),
        timestamp_ns=1,
    )
    if degraded_bad.ok:
        _fail("NAS DEGRADED without the alert must FAIL")
    _ok("NAS degraded-mode passes only WITH the operator alert (and still alerts)")

    # --- behavioural: gate fold ------------------------------------------- #
    unseeded = ReadinessGate()
    try:
        unseeded.assert_runtime_ready_or_hold(ReadinessReport())
    except GateTransitionError:
        _ok("assert_runtime_ready_or_hold refuses an unseeded gate")
    else:
        _fail("assert_runtime_ready_or_hold must refuse an INITIALIZING gate")

    gate = ReadinessGate.from_env(_valid_env())
    sink = _RecordingSink()
    failing = rt.build_runtime_report(_all_pass_results()[:-1], alert_sink=sink, timestamp_ns=1)
    try:
        gate.assert_runtime_ready_or_hold(failing)
    except PreTradeHoldError:
        pass
    else:
        _fail("an error-severity runtime report must hold the gate")
    if gate.state is not GateState.PRE_TRADE_BLOCKED:
        _fail("held gate must be PRE_TRADE_BLOCKED")
    clean = rt.build_runtime_report(
        _all_pass_results(), alert_sink=_RecordingSink(), timestamp_ns=1
    )
    gate.assert_runtime_ready_or_hold(clean)
    if gate.state is not GateState.READY:
        _fail("a clean runtime report must return the gate to READY")
    _ok("runtime report drives the SAME pinned state machine (hold + recover)")

    # --- behavioural: freshness boundary ---------------------------------- #
    close_ns = 1_700_000_000_000_000_000
    calendar = _FixedCalendar(close_ns)
    if not rt.ingestion_is_fresh(close_ns, now_ns=close_ns + 5, calendar=calendar):
        _fail("frontier exactly at the previous session close must be FRESH")
    if rt.ingestion_is_fresh(close_ns - 1, now_ns=close_ns + 5, calendar=calendar):
        _fail("frontier one nanosecond before the session close must be STALE")
    if rt.ingestion_is_fresh(None, now_ns=close_ns, calendar=calendar):
        _fail("a None frontier (no ingestion evidence) must be STALE")
    _ok("freshness boundary exact at previous-session close (±1 ns) and None-stale")

    # --- behavioural: paper gate ------------------------------------------ #
    sink = _RecordingSink()
    rt.assert_paper_ready_or_hold(
        {p: True for p in rt.PaperPrerequisite}, alert_sink=sink, timestamp_ns=1
    )
    if sink.alerts:
        _fail("both-available paper gate must not alert")
    sink = _RecordingSink()
    try:
        rt.assert_paper_ready_or_hold(
            {rt.PaperPrerequisite.INTERNAL_SIMULATION_ENGINE: True},
            alert_sink=sink,
            timestamp_ns=1,
        )
    except rt.PaperStartHoldError as exc:
        if "market_data_subscription_manager" not in exc.missing or not sink.alerts:
            _fail("paper hold must name the missing prerequisite and alert")
    else:
        _fail("a missing paper prerequisite must hold paper startup")
    _ok("paper gate requires BOTH prerequisites (missing key fails closed, alerts)")

    # --- ERR-9 leakage rule still holds ----------------------------------- #
    gate_src = (ROOT / "python" / "atp_readiness" / "gate.py").read_text(encoding="utf-8")
    for token in ("ib_gateway", "ingestion_freshness", "nas_reach", "ssd_probe", "service_health"):
        if token in gate_src:
            _fail(f"gate.py leaked deferred-scope token {token!r} after the extension")
    _ok("SDK gate module still carries no runtime-probe tokens (ERR-9 leakage rule)")

    print(
        "SRS-MD-006 RUNTIME-PROBES PASS — SYS-76 runtime readiness fold + probes + "
        "readiness-wait consumer (real IB round-trip integration-gated; orchestrator "
        "launch-path consultation, SRS-NOTIF-001 alert fan-out, and per-service "
        "liveness substrate deferred)"
    )
    for line in CHECKS:
        print(f"  * {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
