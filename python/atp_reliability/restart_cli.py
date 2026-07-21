"""``python -m atp_reliability.restart_cli`` — the SRS-REL-002 restart-RTO report CLI.

Certifies a caller-supplied restart-timeline evidence fixture against the NFR-R6 objective
(restore a full system restart to a trade-ready state within 10 minutes) and emits the
:class:`~atp_reliability.restart.RestartRecoveryArtifact` plus a final machine-parseable line
``restart_recovery verdict:PASS|FAIL|INCONCLUSIVE elapsed_seconds:… observed_span_seconds:…``.
It gates the process exit code — ``0`` only on ``PASS`` (a certified ≤ 10-minute recovery),
``1`` on ``FAIL`` / ``INCONCLUSIVE``, ``2`` on refused/malformed input — mirroring the exit-code
and fail-closed-parsing discipline of the sibling ``atp_reliability`` (SRS-REL-001) availability
CLI and the Rust ``nfr_p95_cli`` / ``data013_ingestion_validation_cli`` binaries. In ``--json``
mode **stdout is pure JSON** (the verdict + timings are fields in the payload) and the human
summary line is written to stderr, so ``json.loads(stdout)`` always succeeds; in text mode both the
artifact and the summary line go to stdout.

Kept a **separate** module (run as ``python -m atp_reliability.restart_cli``) rather than folding
into the availability ``__main__`` so the SRS-REL-001 CLI contract stays byte-identical.

The elapsed measurement is a raw epoch-ns subtraction (the pure engine computes no calendar). But
the NFR-R6 "during market hours" qualifier **is** a required certification gate, and the scope is
**derived from the actual restart-trigger timestamp** (the ``proxmox_vm`` phase start) via the real
DST/holiday-aware ``UsEquityTradingCalendar`` — **not** a caller-supplied boolean (which would be
forgeable). A restart whose trigger falls outside a regular US-equity session yields
``INCONCLUSIVE`` even with compliant timings (a provable breach is still ``FAIL``). A
``restart_context.during_market_hours`` key is **rejected**; ``restart_context.exchange`` (default
``NYSE``) selects the calendar. There is **no** ``--budget`` flag — this tool verifies SRS-REL-002,
so neither the 10-minute objective nor the market-hours scope can be weakened while still emitting a
``requirement=SRS-REL-002`` PASS.

Fixture schema (JSON object)::

    {
      "phases": {                       # required; the observed boot timeline (epoch ns)
        "proxmox_vm":       [<start_ns>, <end_ns>],
        "os_boot":          [<start_ns>, <end_ns>],
        "docker_daemon":    [<start_ns>, <end_ns>],
        "atp_service_init": [<start_ns>, <end_ns>],
        "readiness_check":  [<start_ns>, <end_ns>]
      },
      "readiness": {                    # optional; absent => no readiness evidence => INCONCLUSIVE
        "gate_state": "ready",          # ready | overridden | pre_trade_blocked | initializing
        "subchecks": {                  # each => "pass" | "fail" | "degraded" | ["degraded", true]
          "ib_connectivity": "pass", "ib_account": "pass", "data_layer_ssd": "pass",
          "nas_archival": ["degraded", true], "system_services": "pass"
        }
      },
      "restart_context": {              # optional
        "exchange": "NYSE"              # calendar for the DERIVED market-hours scope (default NYSE).
                                        # 'during_market_hours' is NOT accepted — scope is derived
                                        # from the proxmox_vm trigger timestamp, not supplied.
      }
    }
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from .boot_evidence import BootEvidenceError, derive_during_market_hours
from .restart import (
    NS_PER_SECOND,
    GateOutcome,
    ObservedPhase,
    ReadinessOutcome,
    RestartError,
    RestartPhase,
    RestartRecoveryArtifact,
    RestartRecoveryTarget,
    SubCheck,
    SubCheckResult,
    SubCheckStatus,
    Verdict,
    compute_restart_recovery,
)

EXIT_PASS = 0
EXIT_NOT_CERTIFIED = 1
EXIT_REFUSED = 2


class _CliError(Exception):
    """Refused/malformed input — mapped to exit code 2."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """``object_pairs_hook`` that refuses duplicate JSON keys instead of silently last-wins.

    ``json.load`` collapses duplicate object keys before the engine's duplicate-phase / duplicate-
    sub-check guards can run, so a fixture with two ``"os_boot"`` phases (or two ``"nas_archival"``
    sub-checks) would otherwise be certified on the last value. Rejecting duplicates at parse time
    keeps that malformed evidence from being normalised into a certifying artifact.
    """

    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _CliError(f"duplicate JSON key {key!r} in fixture — refusing ambiguous evidence")
        seen[key] = value
    return seen


def _require_object(payload: object, label: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise _CliError(f"{label} must be a JSON object; got {payload!r}")
    return payload


def _parse_interval(raw: object, label: str) -> tuple[int, int]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise _CliError(f"{label} must be a [start_ns, end_ns] pair; got {raw!r}")
    start, end = raw
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        raise _CliError(f"{label} bounds must be integer ns; got {raw!r}")
    return start, end


def _parse_phase_key(raw: str) -> RestartPhase:
    try:
        return RestartPhase(raw)
    except ValueError as exc:
        allowed = ", ".join(p.value for p in RestartPhase)
        raise _CliError(f"unknown phase {raw!r}; allowed: {allowed}") from exc


def _parse_phases(payload: dict[str, object]) -> list[ObservedPhase]:
    if "phases" not in payload:
        raise _CliError("fixture requires a 'phases' object (the observed boot timeline)")
    phases_obj = _require_object(payload["phases"], "fixture 'phases'")
    if not phases_obj:
        raise _CliError("fixture 'phases' must be non-empty")
    observed: list[ObservedPhase] = []
    for key, raw in phases_obj.items():
        if not isinstance(key, str):
            raise _CliError(f"phase key must be a string; got {key!r}")
        phase = _parse_phase_key(key)
        start, end = _parse_interval(raw, f"phases[{key}]")
        observed.append(ObservedPhase(phase=phase, start_ns=start, end_ns=end))
    return observed


def _parse_gate_state(raw: object) -> GateOutcome:
    if not isinstance(raw, str):
        raise _CliError(f"readiness 'gate_state' must be a string; got {raw!r}")
    try:
        return GateOutcome(raw)
    except ValueError as exc:
        allowed = ", ".join(g.value for g in GateOutcome)
        raise _CliError(f"unknown gate_state {raw!r}; allowed: {allowed}") from exc


def _parse_subcheck_key(raw: str) -> SubCheck:
    try:
        return SubCheck(raw)
    except ValueError as exc:
        allowed = ", ".join(sc.value for sc in SubCheck)
        raise _CliError(f"unknown sub-check {raw!r}; allowed: {allowed}") from exc


def _parse_subcheck_value(raw: object, label: str) -> tuple[SubCheckStatus, bool]:
    """Parse a sub-check value: ``"pass"`` | ``"fail"`` | ``"degraded"`` | ``["degraded", <bool>]``.

    A bare string carries no operator alert (``alert_raised=False``); the 2-element form supplies
    the SYS-76(d) NAS operator-alert flag, which MUST be a real ``bool`` (not truthy coercion).
    """

    if isinstance(raw, str):
        status = raw
        alert = False
    elif isinstance(raw, (list, tuple)) and len(raw) == 2:
        status_raw, alert_raw = raw
        if not isinstance(status_raw, str):
            raise _CliError(f"{label} status must be a string; got {status_raw!r}")
        if not isinstance(alert_raw, bool):
            raise _CliError(f"{label} alert flag must be a boolean; got {alert_raw!r}")
        status = status_raw
        alert = alert_raw
    else:
        raise _CliError(
            f"{label} must be a status string or a [status, alert_bool] pair; got {raw!r}"
        )
    try:
        return SubCheckStatus(status), alert
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SubCheckStatus)
        raise _CliError(f"{label} unknown status {status!r}; allowed: {allowed}") from exc


def _parse_readiness(payload: dict[str, object]) -> ReadinessOutcome | None:
    # An ABSENT 'readiness' key means no readiness evidence was observed (-> INCONCLUSIVE); a
    # PRESENT-but-malformed 'readiness' is refused (never coerced to "no evidence").
    if "readiness" not in payload:
        return None
    readiness_obj = _require_object(payload["readiness"], "fixture 'readiness'")
    if "gate_state" not in readiness_obj:
        raise _CliError("fixture 'readiness' requires a 'gate_state'")
    gate_state = _parse_gate_state(readiness_obj["gate_state"])
    subchecks: list[SubCheckResult] = []
    if "subchecks" in readiness_obj:
        subchecks_obj = _require_object(readiness_obj["subchecks"], "readiness 'subchecks'")
        for key, raw in subchecks_obj.items():
            if not isinstance(key, str):
                raise _CliError(f"sub-check key must be a string; got {key!r}")
            check = _parse_subcheck_key(key)
            status, alert = _parse_subcheck_value(raw, f"subchecks[{key}]")
            subchecks.append(SubCheckResult(check=check, status=status, alert_raised=alert))
    return ReadinessOutcome(gate_state=gate_state, subchecks=tuple(subchecks))


def _parse_exchange(payload: dict[str, object]) -> str:
    """Return the ``restart_context.exchange`` calendar (default ``NYSE``) for scope derivation.

    Market-hours scope is **derived** from the restart-trigger timestamp (see
    :func:`_artifact_from_fixture`), never supplied — so a caller-provided
    ``restart_context.during_market_hours`` is **rejected** (it would be forgeable, letting an
    out-of-hours restart mint a PASS).
    """

    if "restart_context" not in payload:
        return "NYSE"
    ctx = _require_object(payload["restart_context"], "fixture 'restart_context'")
    if "during_market_hours" in ctx:
        raise _CliError(
            "restart_context 'during_market_hours' is not accepted — the market-hours scope is "
            "DERIVED from the restart-trigger timestamp via the trading calendar, not supplied "
            "(a caller-supplied boolean would be forgeable)"
        )
    exchange = ctx.get("exchange", "NYSE")
    if not isinstance(exchange, str):
        raise _CliError(f"restart_context 'exchange' must be a string; got {exchange!r}")
    return exchange


def _artifact_from_fixture(
    payload: object, target: RestartRecoveryTarget
) -> RestartRecoveryArtifact:
    root = _require_object(payload, "fixture root")
    phases = _parse_phases(root)
    readiness = _parse_readiness(root)
    exchange = _parse_exchange(root)
    # Derive market-hours scope from the ACTUAL restart-trigger timestamp (PROXMOX_VM phase start),
    # not a caller claim. Absent the trigger phase, scope is unknown (None) -> the engine reports the
    # missing phase (INCONCLUSIVE) before the scope gate is even reached.
    trigger = next((p for p in phases if p.phase is RestartPhase.PROXMOX_VM), None)
    during_market_hours: bool | None = None
    if trigger is not None:
        try:
            during_market_hours = derive_during_market_hours(trigger.start_ns, exchange=exchange)
        except BootEvidenceError:
            # Scope could not be determined (e.g. the trigger is outside the bundled calendar
            # horizon, or an unknown exchange). Do NOT refuse — degrade to unknown scope (None) so
            # the engine still evaluates the timeline. A provable over-budget breach must remain
            # FAIL regardless of scope and must never be hidden behind a refusal; unknown scope only
            # blocks a would-be PASS (-> INCONCLUSIVE), never a FAIL.
            during_market_hours = None
    return compute_restart_recovery(
        phases=phases,
        readiness=readiness,
        during_market_hours=during_market_hours,
        target=target,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atp_reliability.restart_cli",
        description="SRS-REL-002 full-system-restart RTO (trade-ready within 10 min) verification.",
    )
    parser.add_argument(
        "--fixture", metavar="PATH", required=True, help="JSON evidence fixture path"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The target is FIXED at the NFR-R6 objective (600 s). This tool verifies SRS-REL-002; it
    # deliberately exposes no flag to weaken the budget, so a PASS artifact labelled
    # requirement=SRS-REL-002 always means <= 10 minutes.
    target = RestartRecoveryTarget()

    try:
        try:
            with open(args.fixture, encoding="utf-8") as fh:
                payload = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
        except (OSError, json.JSONDecodeError) as exc:
            raise _CliError(f"cannot read fixture {args.fixture!r}: {exc}") from exc
        artifact = _artifact_from_fixture(payload, target)
    except _CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except RestartError as exc:
        print(f"error: measurement refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    elapsed = "n/a" if artifact.elapsed_ns is None else f"{artifact.elapsed_ns / NS_PER_SECOND:.3f}"
    span = f"{artifact.observed_span_ns / NS_PER_SECOND:.3f}"
    summary = (
        f"restart_recovery verdict:{artifact.verdict.value} "
        f"elapsed_seconds:{elapsed} observed_span_seconds:{span}"
    )
    if args.json:
        # --json => stdout is PURE JSON so a machine consumer can `json.loads(stdout)` without
        # tripping on a trailing non-JSON line; the human summary goes to stderr (the verdict and
        # both timings are already fields inside the JSON payload).
        print(json.dumps(artifact.as_dict(), indent=2, sort_keys=True))
        print(summary, file=sys.stderr)
    else:
        print(str(artifact))
        print(summary)
    return EXIT_PASS if artifact.verdict is Verdict.PASS else EXIT_NOT_CERTIFIED


def main() -> None:  # console-style entry point
    raise SystemExit(run())


if __name__ == "__main__":
    raise SystemExit(run())
