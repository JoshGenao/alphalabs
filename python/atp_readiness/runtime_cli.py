"""SRS-MD-006 operator CLI — the fixture-driven readiness demonstration.

Runs ONE full SYS-76 readiness evaluation over the composed probe set —
exactly the feature-step verification context ("CLI workflows with fixture
market data, provider mocks, file reads, and persisted output inspection"):

* SSD access + NAS reachability — the REAL ``data008_tier_cli`` against the
  given store directories;
* ingestion freshness — the REAL ``data011_coverage_cli`` frontier for the
  watchlist, judged by the trading-calendar boundary at ``--now``;
* IB connectivity + account data — ``--ib-fixture`` (provider mock) or
  ``--ib-evidence`` (the committed live round-trip evidence document);
* system service health / paper prerequisites — operator fixtures;
* alerts — appended to a durable JSON-lines file for inspection.

Usage::

    python -m atp_readiness.runtime_cli
        --ssd <dir> --nas <dir>
        --watchlist AAPL[,MSFT...]
        (--ib-fixture <json> | --ib-evidence <json>)
        --services-fixture <json>
        --alerts <jsonl-path>
        [--now <epoch-ns>] [--exchange NYSE]
        [--paper-fixture <json>]
        [--override-actor A --override-reason R --override-audit-id ID]
        [--json]

Exit codes: 0 gate READY/OVERRIDDEN; 1 gate held (PRE_TRADE_BLOCKED);
2 refused input (unknown/duplicate/valueless argument, unparseable value).
The parser is a fail-closed allowlist — an argument the CLI does not
understand aborts instead of being ignored.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence

from atp_reliability.restart import SubCheck, SubCheckResult, SubCheckStatus

from .errors import PreTradeHoldError
from .gate import ReadinessGate
from .override import OperatorOverride
from .probes import (
    CoverageFrontierSource,
    EvidenceFileIbProbe,
    FixtureIbProbe,
    FixturePaperPrerequisiteSource,
    FixtureServiceHealthSource,
    JsonlAlertSink,
    TierCliStorageProbe,
    UsEquityCalendarAdapter,
)
from .runtime import (
    assert_paper_ready_or_hold,
    build_runtime_report,
    fold_service_health,
    ingestion_is_fresh,
    release_hold_with_override,
)

_FLAGS_WITH_VALUE = frozenset(
    {
        "--ssd",
        "--nas",
        "--watchlist",
        "--ib-fixture",
        "--ib-evidence",
        "--services-fixture",
        "--paper-fixture",
        "--alerts",
        "--now",
        "--exchange",
        "--override-actor",
        "--override-reason",
        "--override-audit-id",
    }
)
_BARE_FLAGS = frozenset({"--json"})


class _UsageError(Exception):
    pass


def _parse_args(argv: Sequence[str]) -> dict[str, str | bool]:
    """Fail-closed allowlist parser: unknown, duplicate, or valueless
    arguments are refused (exit 2), never ignored."""

    args: dict[str, str | bool] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _BARE_FLAGS:
            if token in args:
                raise _UsageError(f"duplicate argument {token}")
            args[token] = True
            index += 1
            continue
        if token in _FLAGS_WITH_VALUE:
            if token in args:
                raise _UsageError(f"duplicate argument {token}")
            if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
                raise _UsageError(f"argument {token} requires a value")
            args[token] = argv[index + 1]
            index += 2
            continue
        raise _UsageError(f"unknown argument {token!r}")
    return args


def _require(args: dict[str, str | bool], flag: str) -> str:
    value = args.get(flag)
    if not isinstance(value, str) or not value:
        raise _UsageError(f"missing required argument {flag}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = _parse_args(argv)
        ssd = _require(args, "--ssd")
        nas = _require(args, "--nas")
        watchlist = [s for s in _require(args, "--watchlist").split(",") if s]
        if not watchlist:
            raise _UsageError("--watchlist must name at least one symbol")
        services_fixture = _require(args, "--services-fixture")
        alerts_path = _require(args, "--alerts")
        ib_fixture = args.get("--ib-fixture")
        ib_evidence = args.get("--ib-evidence")
        if bool(ib_fixture) == bool(ib_evidence):
            raise _UsageError("exactly one of --ib-fixture / --ib-evidence is required")
        now_raw = args.get("--now")
        if isinstance(now_raw, str):
            try:
                now_ns = int(now_raw)
            except ValueError as exc:
                raise _UsageError(f"unparseable --now {now_raw!r}") from exc
            if now_ns < 0:
                raise _UsageError(f"--now must be >= 0; got {now_raw!r}")
        else:
            now_ns = time.time_ns()
        exchange = str(args.get("--exchange", "NYSE"))
        as_json = bool(args.get("--json", False))
    except _UsageError as exc:
        print(f"atp_readiness.runtime_cli: {exc}", file=sys.stderr)
        return 2

    alert_sink = JsonlAlertSink(alerts_path)
    calendar = UsEquityCalendarAdapter(exchange)

    # --- collect the five SYS-76 sub-check results ---------------------- #
    results: list[SubCheckResult] = []
    if isinstance(ib_fixture, str):
        results.extend(FixtureIbProbe(ib_fixture).observe())
    else:
        results.extend(EvidenceFileIbProbe(str(ib_evidence), now_ns=lambda: now_ns).observe())

    storage = TierCliStorageProbe(ssd, nas, now_ts=now_ns // 1_000_000_000)
    ssd_result, nas_result = storage.observe(alert_sink=alert_sink, timestamp_ns=now_ns)
    # SYS-76(c) folds SSD access AND ingestion freshness into one sub-check:
    # a fresh frontier cannot rescue an unreadable SSD, and vice versa.
    if ssd_result.status is SubCheckStatus.PASS:
        try:
            frontier_ns = CoverageFrontierSource(ssd, watchlist).min_frontier_ns()
            fresh = ingestion_is_fresh(frontier_ns, now_ns=now_ns, calendar=calendar)
        except Exception as exc:  # noqa: BLE001 — an unanswerable freshness read fails closed
            print(f"atp_readiness.runtime_cli: freshness read failed: {exc}", file=sys.stderr)
            fresh = False
        if not fresh:
            ssd_result = SubCheckResult(check=SubCheck.DATA_LAYER_SSD, status=SubCheckStatus.FAIL)
    results.append(ssd_result)
    results.append(nas_result)

    statuses = FixtureServiceHealthSource(services_fixture).statuses()
    results.append(fold_service_health(statuses))

    # --- fold through the gate ------------------------------------------ #
    import os

    gate = ReadinessGate.from_env(dict(os.environ))
    report = build_runtime_report(results, alert_sink=alert_sink, timestamp_ns=now_ns)
    held = False
    try:
        gate.assert_runtime_ready_or_hold(report)
    except PreTradeHoldError:
        held = True

    # --- optional operator override (must alert) ------------------------ #
    override_actor = args.get("--override-actor")
    if held and isinstance(override_actor, str):
        try:
            override = OperatorOverride(
                actor=override_actor,
                reason=_require(args, "--override-reason"),
                audit_trail_id=_require(args, "--override-audit-id"),
                timestamp_ns=now_ns,
            )
        except _UsageError as exc:
            print(f"atp_readiness.runtime_cli: {exc}", file=sys.stderr)
            return 2
        release_hold_with_override(gate, override, alert_sink=alert_sink)
        held = False

    # --- optional paper gate --------------------------------------------- #
    paper_fixture = args.get("--paper-fixture")
    paper_result: dict[str, object] | None = None
    if isinstance(paper_fixture, str):
        availability = FixturePaperPrerequisiteSource(paper_fixture).availability()
        try:
            assert_paper_ready_or_hold(availability, alert_sink=alert_sink, timestamp_ns=now_ns)
            paper_result = {"paper_ready": True}
        except Exception as exc:  # noqa: BLE001 — a paper hold must FAIL the command
            # Codex R1: when the operator asked for the paper gate, an unmet
            # paper prerequisite is a readiness failure — automation must not
            # read exit 0 and start paper strategies against a held gate.
            paper_result = {"paper_ready": False, "reason": str(exc)}
            held = True

    subcheck_rows = [
        {"check": r.check.value, "status": r.status.value, "alert_raised": r.alert_raised}
        for r in results
    ]
    payload: dict[str, object] = {
        "state": str(gate.state),
        "ready": not held,
        "subchecks": subcheck_rows,
        "readiness": gate.as_dashboard_payload(),
        "alerts_file": alerts_path,
        "srs_ref": "SRS-MD-006",
    }
    if paper_result is not None:
        payload["paper"] = paper_result

    if as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"state:{gate.state}")
        print(f"ready:{str(not held).lower()}")
        for row in subcheck_rows:
            print(
                f"subcheck.{row['check']}:{row['status']}"
                + (":alerted" if row["alert_raised"] else "")
            )
        if paper_result is not None:
            print(f"paper_ready:{str(paper_result['paper_ready']).lower()}")
        print(f"alerts_file:{alerts_path}")
    return 0 if not held else 1


if __name__ == "__main__":
    raise SystemExit(main())
