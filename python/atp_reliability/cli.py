"""``python -m atp_reliability`` — the SRS-REL-001 availability report CLI.

Emits the availability verification artifact plus a final machine-parseable line
``availability:NN.NNNN verdict:PASS|FAIL|INCONCLUSIVE`` and gates the process exit
code: ``0`` only on ``PASS`` (a certified ≥99.9% result); ``1`` on ``FAIL`` /
``INCONCLUSIVE``; ``2`` on refused/malformed input. Mirrors the exit-code + verdict
discipline of ``crates/atp-types/src/bin/nfr_p95_cli.rs`` and
``crates/atp-data/src/bin/data013_ingestion_validation_cli.rs`` — deterministic,
fail-closed parsing, no wall-clock read.

Two evidence paths — both derive the COMPLETE market-session set from the trading
calendar over a date range, so a certifying result always measures every US-equity
session in the period (sessions are never caller-supplied):

* ``--fixture PATH`` — a JSON evidence file: ``exchange`` / ``start_date`` /
  ``end_date`` (the period) plus ``covered`` / ``downtime`` / ``excluded_windows``
  (the observed evidence, epoch-ns). Sessions come from the calendar; an uncovered
  trading day therefore shows up as unmeasured -> INCONCLUSIVE, not a silent PASS.
* ``--calendar --start D --end D`` — derive market sessions from the real trading
  calendar and the SYS-75 exclusion windows; optionally read subsystem downtime
  from a ``--log-store`` JSONL. Calendar mode has **no coverage oracle** (the
  host-liveness feed is deferred), so it is honestly ``INCONCLUSIVE`` — there is
  deliberately no flag to synthesise coverage and certify. A certifying ``PASS``
  requires explicit coverage evidence, which today only ``--fixture`` supplies.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections.abc import Sequence

from .availability import (
    AvailabilityError,
    AvailabilityTarget,
    AvailabilityVerificationArtifact,
    CoveredSpan,
    DowntimeInterval,
    MarketSessionWindow,
    OutageCause,
    Verdict,
    compute_availability,
)
from .evidence import (
    market_sessions,
    sys75_exclusion_windows,
)

EXIT_PASS = 0
EXIT_NOT_CERTIFIED = 1
EXIT_REFUSED = 2


class _CliError(Exception):
    """Refused/malformed input — mapped to exit code 2."""


def _fixture_list(payload: dict[str, object], key: str) -> list[object]:
    """Return a required-to-be-a-list optional field, or ``[]`` when the key is absent.

    An ABSENT key defaults to ``[]``; a PRESENT key must be a JSON array. This refuses
    the ``.get(key, []) or []`` foot-gun where a falsy malformed value (``null`` /
    ``false`` / ``0`` / ``""``) would be silently coerced to "no evidence" and certify a
    corrupt fixture as clean.
    """

    if key not in payload:
        return []
    val = payload[key]
    if not isinstance(val, list):
        raise _CliError(f"fixture {key!r} must be a JSON array when present; got {val!r}")
    return val


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


def _parse_cause(raw: object, label: str) -> OutageCause:
    if not isinstance(raw, str):
        raise _CliError(f"{label} cause must be a string; got {raw!r}")
    try:
        return OutageCause(raw)
    except ValueError as exc:
        allowed = ", ".join(c.value for c in OutageCause)
        raise _CliError(f"{label} unknown cause {raw!r}; allowed: {allowed}") from exc


def _calendar_period(
    exchange: str, start: str, end: str
) -> tuple[_dt.date, _dt.date, int, int, list[MarketSessionWindow]]:
    """Derive the COMPLETE trading-session set for ``[start, end]`` from the calendar.

    Both CLI paths route through here so a certifying result always measures every
    US-equity market-hours session in the period — sessions are never caller-supplied
    (an incomplete list would understate the denominator and hide unmeasured days).
    """

    from atp_strategy.calendar import UsEquityTradingCalendar

    try:
        start_date = _dt.date.fromisoformat(start)
        end_date = _dt.date.fromisoformat(end)
    except ValueError as exc:
        raise _CliError(f"invalid start/end date: {exc}") from exc
    try:
        calendar = UsEquityTradingCalendar.for_exchange(exchange)
        window_start, window_end, sessions = market_sessions(calendar, start_date, end_date)
    except Exception as exc:  # horizon exceeded / unsupported exchange / no session
        raise _CliError(f"calendar error: {exc}") from exc
    return start_date, end_date, window_start, window_end, sessions


def _artifact_from_fixture(
    payload: object, target: AvailabilityTarget
) -> AvailabilityVerificationArtifact:
    if not isinstance(payload, dict):
        raise _CliError("fixture root must be a JSON object")
    if "sessions" in payload or "window_start_ns" in payload or "window_end_ns" in payload:
        raise _CliError(
            "fixture must specify the period as 'start_date'/'end_date' (+ optional "
            "'exchange'); the certifying session set is derived from the trading "
            "calendar so it is always complete. Raw 'sessions'/'window_*_ns' are "
            "rejected because an incomplete list understates the denominator and "
            "hides unmeasured market days."
        )
    start = payload.get("start_date")
    end = payload.get("end_date")
    if not isinstance(start, str) or not isinstance(end, str):
        raise _CliError("fixture requires string 'start_date' and 'end_date' (YYYY-MM-DD)")
    exchange = payload.get("exchange", "NYSE")
    if not isinstance(exchange, str):
        raise _CliError("fixture 'exchange' must be a string")
    _, _, window_start, window_end, sessions = _calendar_period(exchange, start, end)
    covered = [
        CoveredSpan(*_parse_interval(iv, f"covered[{i}]"))
        for i, iv in enumerate(_fixture_list(payload, "covered"))
    ]
    downtime: list[DowntimeInterval] = []
    for i, iv in enumerate(_fixture_list(payload, "downtime")):
        if not isinstance(iv, (list, tuple)) or len(iv) != 3:
            raise _CliError(f"downtime[{i}] must be [start_ns, end_ns, cause]; got {iv!r}")
        start_ns, end_ns = _parse_interval(iv[:2], f"downtime[{i}]")
        cause = _parse_cause(iv[2], f"downtime[{i}]")
        downtime.append(DowntimeInterval(start_ns=start_ns, end_ns=end_ns, cause=cause))
    excluded = [
        _parse_interval(iv, f"excluded_windows[{i}]")
        for i, iv in enumerate(_fixture_list(payload, "excluded_windows"))
    ]
    return compute_availability(
        window_start_ns=window_start,
        window_end_ns=window_end,
        sessions=sessions,
        covered=covered,
        downtime=downtime,
        excluded_windows=excluded,
        target=target,
    )


def _artifact_from_calendar(
    args: argparse.Namespace, target: AvailabilityTarget
) -> AvailabilityVerificationArtifact:
    if args.start is None or args.end is None:
        raise _CliError("--calendar requires --start and --end (YYYY-MM-DD)")
    start_date, end_date, window_start, window_end, sessions = _calendar_period(
        args.exchange, args.start, args.end
    )
    excluded = sys75_exclusion_windows(start_date, end_date)
    downtime: list[DowntimeInterval] = []
    if args.log_store is not None:
        from atp_logging.errors import LogRecordError
        from atp_logging.persistence import read_records
        from atp_logging.records import LogClass

        from .evidence import downtime_from_log_records

        # A corrupt / unreadable log store is a DEGRADED-evidence condition, not a
        # crash: map it to the CLI refusal contract (exit 2) so operators see the
        # degraded state instead of a traceback. LogRecordError covers store
        # corruption; OSError covers filesystem failures; ValueError covers a
        # malformed transition timestamp during reconstruction.
        try:
            records = read_records(args.log_store, log_class=LogClass.SYSTEM)
            downtime = downtime_from_log_records(
                records, window_start_ns=window_start, window_end_ns=window_end
            )
        except (LogRecordError, OSError, ValueError) as exc:
            raise _CliError(f"log store {args.log_store!r} unreadable/corrupt: {exc}") from exc
    # Calendar mode has NO coverage oracle: the host-liveness feed that produces
    # positive coverage (and is the only thing that can witness a host-level outage)
    # is deferred. There is deliberately no flag to synthesise coverage from the
    # session windows and certify -- that would be the "no-data = up" lie this
    # substrate refuses. Without observed coverage the verdict is honestly
    # INCONCLUSIVE; a real PASS requires explicit coverage evidence (fixture mode,
    # or the deferred feed once it lands).
    covered: list[CoveredSpan] = []
    return compute_availability(
        window_start_ns=window_start,
        window_end_ns=window_end,
        sessions=sessions,
        covered=covered,
        downtime=downtime,
        excluded_windows=excluded,
        target=target,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atp_reliability",
        description="SRS-REL-001 market-hours availability verification report.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", metavar="PATH", help="JSON evidence fixture path")
    mode.add_argument(
        "--calendar",
        action="store_true",
        help="derive market sessions + SYS-75 exclusions from the trading calendar",
    )
    parser.add_argument("--exchange", default="NYSE", help="exchange for --calendar (default NYSE)")
    parser.add_argument("--start", help="window start date YYYY-MM-DD (--calendar)")
    parser.add_argument("--end", help="window end date YYYY-MM-DD (--calendar)")
    parser.add_argument("--log-store", help="LogRecord JSONL to read subsystem downtime from")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # The target is FIXED at the NFR-R1 objective (999 per-mille). This tool verifies
    # SRS-REL-001; it deliberately exposes no flag to weaken the target, so a PASS
    # artifact labelled requirement=SRS-REL-001 always means >= 99.9%.
    target = AvailabilityTarget()

    try:
        if args.fixture is not None:
            try:
                with open(args.fixture, encoding="utf-8") as fh:
                    payload = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise _CliError(f"cannot read fixture {args.fixture!r}: {exc}") from exc
            artifact = _artifact_from_fixture(payload, target)
        else:
            artifact = _artifact_from_calendar(args, target)
    except _CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except AvailabilityError as exc:
        print(f"error: measurement refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.json:
        print(json.dumps(artifact.as_dict(), indent=2, sort_keys=True))
    else:
        print(str(artifact))
    print(f"availability:{artifact.availability_ratio * 100:.4f} verdict:{artifact.verdict.value}")
    return EXIT_PASS if artifact.verdict is Verdict.PASS else EXIT_NOT_CERTIFIED


def main() -> None:  # console-style entry point
    raise SystemExit(run())
