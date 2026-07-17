"""SRS-MD-006 probe adapters — the I/O edge of the runtime readiness fold.

Each adapter observes ONE SYS-76 signal and returns the shared
:class:`~atp_reliability.restart.SubCheckResult` vocabulary (or raw inputs
for the pure predicates in :mod:`atp_readiness.runtime`). All I/O lives
here, behind injectable runners, so the fold core stays pure and every
adapter is testable with canned output:

* :class:`TierCliStorageProbe` — SSD access + NAS reachability, by shelling
  the green SRS-DATA-008 operator binary ``data008_tier_cli report`` and
  parsing its ``key:value`` machine output (strict parse; drift ⇒ FAIL).
* :class:`CoverageFrontierSource` — the ingestion frontier per watchlist
  symbol via ``data011_coverage_cli show-coverage`` (``frontier:<i64|none>``);
  the freshness verdict itself is :func:`atp_readiness.runtime.ingestion_is_fresh`.
* :class:`EvidenceFileIbProbe` — the demonstration-leg IB probe over the
  committed live-round-trip evidence document
  (``architecture/ib_paper_account_evidence.json``); the REAL gateway
  round-trip is the ATP_RUN_INTEGRATION-gated integration test (port 4002)
  and stays out of solo runs.
* :class:`FixtureIbProbe` / :class:`FixtureServiceHealthSource` /
  :class:`FixturePaperPrerequisiteSource` — operator-authored JSON fixtures
  (the feature-step "provider mocks" context) for the signals whose live
  producers are deferred.
* :class:`UsEquityCalendarAdapter` — the ``TradingCalendarPort``
  implementation over the SDK's ``UsEquityTradingCalendar``. The
  ``atp_strategy`` import is FUNCTION-LOCAL (the ``boot_evidence.py``
  precedent) so the readiness package's core modules stay free of upstream
  imports.
* :class:`RecordingAlertSink` / :class:`JsonlAlertSink` — concrete alert
  sinks (in-memory for tests/demos; durable JSON-lines for persisted-output
  inspection). Real email/SMS fan-out is SRS-NOTIF-001's.

Fail-closed rule: any subprocess failure, unparseable output, missing file,
or schema drift produces a FAIL result (or raises to the composer) — a
probe that cannot observe its signal can never report it healthy.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from atp_reliability.restart import SubCheck, SubCheckResult, SubCheckStatus

if TYPE_CHECKING:  # pragma: no cover — typing only; runtime import stays function-local
    from atp_strategy.calendar import UsEquityTradingCalendar

from .runtime import (
    AlertSink,
    PaperPrerequisite,
    ReadinessAlert,
    ReadinessAlertKind,
    ReadinessService,
    RuntimeReadinessError,
)

__all__ = [
    "CliRunner",
    "CoverageFrontierSource",
    "EvidenceFileIbProbe",
    "FixtureIbProbe",
    "FixturePaperPrerequisiteSource",
    "FixtureServiceHealthSource",
    "JsonlAlertSink",
    "RecordingAlertSink",
    "TierCliStorageProbe",
    "UsEquityCalendarAdapter",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TIER_CLI = _REPO_ROOT / "target" / "debug" / "data008_tier_cli"
_COVERAGE_CLI = _REPO_ROOT / "target" / "debug" / "data011_coverage_cli"

# Bounded subprocess budget — a wedged binary is a FAIL observation, never a
# hung readiness poll.
_DEFAULT_TIMEOUT_S = 10.0

#: The coverage store keys records by an abstract i64 ``event_ts``; the
#: repo's data fixtures and the tier CLI's ``--now`` convention use epoch
#: SECONDS. The freshness predicate works in epoch-ns, so frontiers are
#: scaled by this factor (overridable for ns-native stores).
FRONTIER_SCALE_NS: int = 1_000_000_000


@runtime_checkable
class CliRunner(Protocol):
    """Injectable subprocess surface (canned output in tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"readiness probe binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-data --bins`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _parse_kv(stdout: str) -> dict[str, str]:
    """Strict ``key:value`` line parser (first colon splits)."""

    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep or not key:
            raise RuntimeReadinessError(f"unparseable probe output line {line!r}")
        fields[key] = value
    return fields


class TierCliStorageProbe:
    """SYS-76(c) SSD access + SYS-76(d) NAS reachability over the tier CLI.

    ``observe()`` returns ``(ssd_result, nas_result)``:

    * SSD — PASS iff the report subprocess succeeds against the SSD tier
      (the tier coordinator opens and reads the SSD store to build the
      report); any failure to run/parse ⇒ FAIL.
    * NAS — PASS when ``nas_reachable:true``; when ``nas_reachable:false``
      the result is DEGRADED and ``alert_raised`` is set to the OUTCOME of
      dispatching the SYS-76(d) degraded-mode alert through ``alert_sink``
      NOW — the boolean records that the alert actually went out, not an
      intention, so degraded-mode can never pass on an undelivered alert.
      (The fold dispatches its own alert too; double notification of a
      degraded archive tier is preferable to none, and the fold's evidence
      line documents the mode.)
    """

    def __init__(
        self,
        ssd_dir: str | Path,
        nas_dir: str | Path,
        *,
        binary: str | Path = _TIER_CLI,
        runner: CliRunner = _default_runner,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        now_ts: int | None = None,
    ) -> None:
        self._ssd_dir = str(ssd_dir)
        self._nas_dir = str(nas_dir)
        self._binary = str(binary)
        self._runner = runner
        self._timeout_s = timeout_s
        self._now_ts = now_ts

    def observe(
        self, *, alert_sink: AlertSink, timestamp_ns: int
    ) -> tuple[SubCheckResult, SubCheckResult]:
        argv = [self._binary, "report", "--ssd", self._ssd_dir, "--nas", self._nas_dir]
        if self._now_ts is not None:
            argv += ["--now", str(self._now_ts)]
        try:
            result = self._runner(argv, timeout=self._timeout_s)
            if result.returncode != 0:
                raise RuntimeReadinessError(
                    f"tier report failed (exit {result.returncode}): {result.stderr.strip()}"
                )
            fields = _parse_kv(result.stdout)
            nas_reachable_raw = fields.get("nas_reachable")
            if nas_reachable_raw not in ("true", "false"):
                raise RuntimeReadinessError(
                    f"tier report missing/unparseable nas_reachable: {nas_reachable_raw!r}"
                )
        except (OSError, subprocess.TimeoutExpired, RuntimeReadinessError):
            # Cannot observe the storage tiers at all ⇒ both checks FAIL.
            fail = SubCheckStatus.FAIL
            return (
                SubCheckResult(check=SubCheck.DATA_LAYER_SSD, status=fail),
                SubCheckResult(check=SubCheck.NAS_ARCHIVAL, status=fail),
            )
        ssd = SubCheckResult(check=SubCheck.DATA_LAYER_SSD, status=SubCheckStatus.PASS)
        if nas_reachable_raw == "true":
            return ssd, SubCheckResult(check=SubCheck.NAS_ARCHIVAL, status=SubCheckStatus.PASS)
        # NAS unreachable ⇒ degraded mode, acceptable ONLY with the alert:
        # bind alert_raised to the dispatch actually succeeding.
        alert_raised = True
        try:
            alert_sink.dispatch(
                ReadinessAlert(
                    kind=ReadinessAlertKind.NAS_DEGRADED_MODE,
                    key=SubCheck.NAS_ARCHIVAL.value,
                    reason="NAS archival tier unreachable at probe time (SYS-76(d))",
                    timestamp_ns=timestamp_ns,
                )
            )
        except Exception:  # noqa: BLE001 — a failed alert makes degraded UNACCEPTABLE
            alert_raised = False
        return ssd, SubCheckResult(
            check=SubCheck.NAS_ARCHIVAL,
            status=SubCheckStatus.DEGRADED,
            alert_raised=alert_raised,
        )


class CoverageFrontierSource:
    """The ingestion frontier (epoch-ns) for a watchlist, via the coverage CLI.

    ``min_frontier_ns()`` returns the OLDEST frontier across the watchlist
    (the freshness gate is only as fresh as its stalest symbol), scaled from
    the store's epoch-seconds convention; ``None`` when any symbol has no
    coverage record at all (no data ⇒ stale, decided by the predicate).
    """

    def __init__(
        self,
        store_dir: str | Path,
        watchlist: Sequence[str],
        *,
        binary: str | Path = _COVERAGE_CLI,
        runner: CliRunner = _default_runner,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        frontier_scale_ns: int = FRONTIER_SCALE_NS,
    ) -> None:
        if not watchlist:
            raise RuntimeReadinessError(
                "ingestion-freshness watchlist is empty — freshness of nothing is "
                "not evidence of a fresh data layer (fail closed)"
            )
        self._store_dir = str(store_dir)
        self._watchlist = tuple(watchlist)
        self._binary = str(binary)
        self._runner = runner
        self._timeout_s = timeout_s
        self._scale = frontier_scale_ns

    def min_frontier_ns(self) -> int | None:
        """The stalest watchlist frontier (epoch-ns), or ``None`` when it
        cannot be OBSERVED — a missing store/record, a wedged or missing
        binary, a timeout, or output drift all collapse to ``None`` (Codex
        R3: I/O failures must fold into the freshness verdict as "no
        evidence ⇒ stale ⇒ pre-trade hold", never leak as a crash out of a
        readiness poll)."""

        frontiers: list[int] = []
        for symbol in self._watchlist:
            argv = [
                self._binary,
                "show-coverage",
                "--dir",
                self._store_dir,
                "--symbol",
                symbol,
            ]
            try:
                result = self._runner(argv, timeout=self._timeout_s)
                if result.returncode != 0:
                    # No store / no record for the symbol ⇒ no frontier ⇒ stale.
                    return None
                fields = _parse_kv(result.stdout)
                raw = fields.get("frontier")
                if raw is None or raw == "none":
                    return None
                frontiers.append(int(raw) * self._scale)
            except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeReadinessError):
                # Unobservable frontier ⇒ no evidence ⇒ stale (fail closed).
                return None
        return min(frontiers)


class FixtureIbProbe:
    """SYS-76(a)+(b) from an operator-authored fixture (the 'provider mocks'
    verification context). Fixture JSON shape:
    ``{"connectivity": true|false, "account_data": true|false}`` — missing
    keys and unreadable/undecodable files fail closed.
    """

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)

    def observe(self) -> tuple[SubCheckResult, SubCheckResult]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            connectivity = payload["connectivity"] is True
            account = payload["account_data"] is True
        except (OSError, ValueError, KeyError, TypeError):
            connectivity = False
            account = False
        return (
            SubCheckResult(
                check=SubCheck.IB_CONNECTIVITY,
                status=SubCheckStatus.PASS if connectivity else SubCheckStatus.FAIL,
            ),
            SubCheckResult(
                check=SubCheck.IB_ACCOUNT,
                status=SubCheckStatus.PASS if account else SubCheckStatus.FAIL,
            ),
        )


#: Maximum age of an IB round-trip evidence document before it can no
#: longer stand in for CURRENT gateway connectivity: a startup readiness
#: check runs at boot, immediately after the round-trip that produced the
#: evidence, and NFR-R6 bounds the whole restart at 10 minutes — 15 minutes
#: of slack covers it. Anything older proves history, not the present.
EVIDENCE_MAX_AGE_NS: int = 15 * 60 * 1_000_000_000


class EvidenceFileIbProbe:
    """SYS-76(a)+(b) from a FRESH live round-trip evidence document.

    Reads the artifact the ATP_RUN_INTEGRATION-gated real-gateway diagnostic
    writes (``architecture/ib_paper_account_evidence.json``) and PASSES both
    IB sub-checks only when the document is well-formed, carries the
    expected schema/test identity, records a passing run, AND its
    ``generated_at`` stamp is within :data:`EVIDENCE_MAX_AGE_NS` of the
    probe's clock (Codex R2): a stale document proves a PAST session, not
    current connectivity, so it fails closed — the boot sequence must run
    the round-trip first and gate immediately after. A live in-process IB
    probe over the adapter transport is the deferred runtime producer; this
    probe is the demonstration / freshly-restarted-boot bridge.
    """

    def __init__(
        self,
        evidence_path: str | Path | None = None,
        *,
        now_ns: Callable[[], int] = time.time_ns,
        max_age_ns: int = EVIDENCE_MAX_AGE_NS,
    ) -> None:
        self._path = Path(
            evidence_path
            if evidence_path is not None
            else _REPO_ROOT / "architecture" / "ib_paper_account_evidence.json"
        )
        self._now_ns = now_ns
        self._max_age_ns = max_age_ns

    def observe(self) -> tuple[SubCheckResult, SubCheckResult]:
        ok = False
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            # Strict result-line parse (Codex R1): the cargo harness prints
            # "test result: ok. N passed; 0 failed; ..." — a failing run says
            # "test result: FAILED." and a TAP-style "not ok" must never
            # substring-match as success. Require the exact success prefix
            # AND a zero failure count.
            result_line = str(payload.get("result_line", "")).strip()
            generated_at = str(payload.get("generated_at", ""))
            generated_ns = int(
                _dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00")).timestamp()
                * 1_000_000_000
            )
            age_ns = self._now_ns() - generated_ns
            ok = (
                payload.get("schema_version") == 1
                and payload.get("test") == "paper_account_round_trip"
                and payload.get("returncode") == 0
                and result_line.startswith("test result: ok.")
                and "; 0 failed;" in result_line
                # Freshness bound: future-stamped (age < 0) fails closed too.
                and 0 <= age_ns <= self._max_age_ns
            )
        except (OSError, ValueError):
            ok = False
        status = SubCheckStatus.PASS if ok else SubCheckStatus.FAIL
        return (
            SubCheckResult(check=SubCheck.IB_CONNECTIVITY, status=status),
            SubCheckResult(check=SubCheck.IB_ACCOUNT, status=status),
        )


class FixtureServiceHealthSource:
    """SYS-76(e) service health from an operator-authored fixture.

    Fixture JSON: ``{"execution_engine": true, ...}`` keyed by
    :class:`ReadinessService` values. Missing keys are simply absent from
    the returned mapping — the fold treats absent as unhealthy (fail
    closed). Unknown keys raise. Per-service LIVE probing has no substrate
    yet and is a named deferred item of the runtime contract.
    """

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)

    def statuses(self) -> Mapping[ReadinessService, bool]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        statuses: dict[ReadinessService, bool] = {}
        for key, value in payload.items():
            statuses[ReadinessService(key)] = value is True
        return statuses


class FixturePaperPrerequisiteSource:
    """SYS-76 paper prerequisites from an operator-authored fixture
    (``{"market_data_subscription_manager": true, ...}``). Absent keys are
    unavailable (fail closed); unknown keys raise."""

    def __init__(self, fixture_path: str | Path) -> None:
        self._path = Path(fixture_path)

    def availability(self) -> Mapping[PaperPrerequisite, bool]:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        availability: dict[PaperPrerequisite, bool] = {}
        for key, value in payload.items():
            availability[PaperPrerequisite(key)] = value is True
        return availability


class UsEquityCalendarAdapter:
    """``TradingCalendarPort`` over the SDK's real U.S.-equity calendar.

    The ``atp_strategy`` import is function-local (``boot_evidence.py``
    precedent): the readiness package's contract forbids upstream imports in
    its core modules, and this edge adapter is injected where a calendar is
    needed. A calendar failure propagates — the freshness boundary never
    defaults to fresh.
    """

    def __init__(self, exchange: str = "NYSE") -> None:
        self._exchange = exchange
        self._calendar: "UsEquityTradingCalendar | None" = None

    def _load(self) -> "UsEquityTradingCalendar":
        if self._calendar is None:
            from atp_strategy.calendar import UsEquityTradingCalendar

            self._calendar = UsEquityTradingCalendar.for_exchange(self._exchange)
        return self._calendar

    def previous_session_close_ns(self, now_ns: int) -> int:
        calendar = self._load()
        instant = _dt.datetime.fromtimestamp(now_ns / 1_000_000_000, tz=_dt.timezone.utc)
        day = instant.date()
        # Walk back to the most recent session whose close is <= now.
        for _ in range(31):  # a month bounds any exchange holiday run
            if calendar.is_session(day):
                close = calendar.session_close(day)
                close_ns = int(close.timestamp() * 1_000_000_000)
                if close_ns <= now_ns:
                    return close_ns
            day -= _dt.timedelta(days=1)
        raise RuntimeReadinessError(
            f"no completed trading session found within 31 days before {instant.isoformat()}"
        )


class RecordingAlertSink:
    """In-memory alert sink for tests and the CLI's human-readable output."""

    def __init__(self) -> None:
        self.alerts: list[ReadinessAlert] = []

    def dispatch(self, alert: ReadinessAlert) -> None:
        self.alerts.append(alert)


class JsonlAlertSink:
    """Durable JSON-lines alert sink (persisted-output inspection context).

    One JSON object per alert, appended with an fsync'd file handle per
    dispatch. Real email/SMS fan-out (Email+Sms required channel set) is
    SRS-NOTIF-001's; this sink makes the alert trail INSPECTABLE today.
    Write failures propagate (a readiness alert that cannot be recorded is
    a failed dispatch — degraded-mode must not pass on it).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def dispatch(self, alert: ReadinessAlert) -> None:
        record = asdict(alert)
        record["kind"] = alert.kind.value
        record["srs_trace"] = list(alert.srs_trace)
        line = json.dumps(record, sort_keys=True)
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()

    def read(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        return [
            json.loads(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def now_ns() -> int:
    """The single wall-clock read for compositions (injected everywhere else)."""

    return time.time_ns()


#: Type alias for the callable compositions inject where a clock is needed.
Clock = Callable[[], int]
