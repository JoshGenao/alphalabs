"""Heartbeat freshness provider (``SRS-MD-003`` / SyRS SYS-39, NFR-P5).

Feeds the dashboard's ``HEARTBEAT`` channel — declared by :mod:`atp_ws` with
payload fields ``feed`` / ``last_tick_at`` / ``staleness_seconds`` /
``is_stale`` and a 1 s cadence — and the system-health reflection, from the
ONE source of truth for staleness: the Rust ``HeartbeatFreshnessMonitor`` in
``crates/atp-market-data``, driven through its operator binary
``md003_heartbeat_cli`` (the repo's subprocess → Rust CLI → parse-stdout
boundary, exactly as :mod:`atp_dashboard.backtests` shells ``bt009_store_cli``).

Each poll replays the configured observation script (fixture ticks / broker
heartbeats — the feature-step verification context; the deferred live feed
loop will stream real IB observations instead, see
``heartbeat_freshness_contract.deferred[]``) and appends one
``evaluate <now_ns>`` directive stamped from THIS process's wall clock — the
single wall-clock read in the whole path, taken at the outermost edge so the
Rust core stays deterministic. Because the publisher re-polls every second,
successive evaluations form exactly the "continuous monitoring" loop the AC
requires: a feed whose last observation ages past the NFR-P5 15-second
budget flips stale within one poll cadence.

Logging (the AC's "logged" leg)
-------------------------------
Every feed's Fresh ↔ Stale flip is written as a SYSTEM
:class:`~atp_logging.records.LogRecord` (``HEARTBEAT_STALE`` /
``HEARTBEAT_RECOVERED``) to the injected
:class:`~atp_logging.persistence.JsonlLogStore` — market-data feeds under
``Source.MARKET_DATA``, the broker feed under ``Source.IB_GATEWAY`` (both
event types are AC-pinned in ``EVENT_TYPES_BY_SOURCE``). Because each poll
runs the CLI in a FRESH process (whose in-process transition baseline resets
with it), the once-per-flip guarantee lives HERE: this provider persists each
feed's last-logged staleness across polls and writes a record only when the
merged verdict actually changes — so a feed that stays stale for a minute
yields one record, not sixty. A failing log write must never kill the
monitoring tick (observability over completeness — the freshness state is
already committed on the Rust side): the record is QUEUED and retried on
every subsequent poll until it lands (so a transient sink failure during a
brief stale incident loses neither the incident nor its recovery), the
outage is surfaced as ``log_write_ok: false`` until the queue drains, and a
bounded queue under sustained failure drops oldest-first with an explicit
``dropped_log_records`` count — honest loss accounting, never silent.
``SEQUENCE_GAP`` lines the CLI may also emit belong to SRS-MD-007's logging
seam and are NOT written here — they still fold into each line's merged
``stale`` verdict.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
A missing binary, an unreadable observation script, a wedged CLI, or ANY
drift in the kv machine grammar is reported as an explicit unavailable state
(``ok: false`` + the reason) whose single channel row is ``is_stale: true`` —
a broken monitor must read as "not proven fresh" (fail closed), never crash
the dashboard, and never masquerade as a healthy feed. A feed that has never
been observed arrives with ``staleness_seconds`` / ``last_tick_at`` ``None``
(no fabricated age) and ``is_stale: true``.

SRS trace
---------
``SRS-MD-003`` (continuous heartbeat freshness), SyRS ``SYS-39`` / ``NFR-P5``
(15 s threshold, dashboard display), ``SRS-LOG-001`` (transition records),
consuming ``SRS-UI-001``'s publisher/meta-route seams.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TypedDict, runtime_checkable

from atp_logging import LogClass, LogRecord, Severity, Source
from atp_logging.persistence import JsonlLogStore
from atp_ws import Channel

__all__ = [
    "HEARTBEAT_CHANNEL",
    "CliHeartbeatSource",
    "HeartbeatCliRunner",
    "HeartbeatFreshnessProvider",
    "HeartbeatUnavailable",
    "THRESHOLD_MS",
]

#: The channel this provider feeds (kept next to the provider so the
#: publisher and the tests share one authority).
HEARTBEAT_CHANNEL: str = Channel.HEARTBEAT

#: The NFR-P5 staleness budget, milliseconds. A named constant mirroring the
#: Rust authority ``atp_types::HEARTBEAT_STALENESS_THRESHOLD_MS`` (pinned to
#: it by the contract test); staleness is detected strictly OVER this value.
THRESHOLD_MS: int = 15_000

# Default location of the cargo-built operator binary, relative to the repo
# root (python/atp_dashboard/heartbeat.py -> parents[2] == repo root). Build
# it with ``cargo build -p atp-market-data --bin md003_heartbeat_cli``.
_DEFAULT_BINARY = Path(__file__).resolve().parents[2] / "target" / "debug" / "md003_heartbeat_cli"

# Per-invocation subprocess budget (seconds). Deliberately BELOW the
# HEARTBEAT channel's declared 1 s cadence, so even a wedged binary resolves
# to a fail-closed unavailable row within the cadence budget instead of
# stretching a tick past its refresh contract (the real CLI replays a
# fixture script in single-digit milliseconds).
_DEFAULT_TIMEOUT_S = 0.9

_TRANSITION_EVENT_TYPES = frozenset({"HEARTBEAT_STALE", "HEARTBEAT_RECOVERED"})

# Ceiling on transition records queued for retry while the durable sink is
# down. Beyond it the OLDEST records are dropped and counted (surfaced on the
# snapshot as ``dropped_log_records``) — bounded memory with honest loss
# accounting under a sustained multi-hour sink outage.
_MAX_PENDING_LOG_RECORDS = 4096


class HeartbeatUnavailable(Exception):
    """The freshness monitor cannot be read right now (reported, never fabricated)."""


class StatusRow(TypedDict):
    """One feed's parsed ``status`` line: the continuously-displayed verdict."""

    feed: str
    last_observation_ns: int | None
    staleness_ms: int | None
    never_observed: bool
    time_stale: bool
    gap_stale: bool
    stale: bool
    threshold_ms: int


class Observation(TypedDict):
    """One CLI evaluation: the per-feed snapshot rows, the transition events,
    and the wall-clock instant the evaluation was stamped with."""

    statuses: list[StatusRow]
    events: list[dict[str, object]]
    evaluated_at_ns: int


@runtime_checkable
class HeartbeatCliRunner(Protocol):
    """The subprocess surface the source depends on (injectable for tests)."""

    def __call__(
        self, argv: list[str], *, input: str, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _default_runner(
    argv: list[str], *, input: str, timeout: float
) -> subprocess.CompletedProcess[str]:
    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"heartbeat monitor binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-market-data --bin md003_heartbeat_cli`"
        )
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        input=input,
        timeout=timeout,
    )


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_from_ns(ns: int) -> str:
    """Epoch-nanoseconds as an ISO-8601 ``Z`` string (second precision)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ns / 1_000_000_000))


def _parse_kv_line(line: str) -> dict[str, str]:
    """One CLI output line (``kind key=value key=value ...``) as a dict."""

    fields: dict[str, str] = {}
    for token in line.split()[1:]:
        key, sep, value = token.partition("=")
        if not sep or not key:
            raise HeartbeatUnavailable(f"malformed kv token {token!r} in line {line!r}")
        fields[key] = value
    return fields


def _parse_optional_int(fields: dict[str, str], key: str, line: str) -> int | None:
    raw = fields.get(key)
    if raw is None:
        raise HeartbeatUnavailable(f"missing {key!r} in line {line!r}")
    if raw == "none":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HeartbeatUnavailable(f"unparseable {key}={raw!r} in line {line!r}") from exc


def _parse_int(fields: dict[str, str], key: str, line: str) -> int:
    value = _parse_optional_int(fields, key, line)
    if value is None:
        raise HeartbeatUnavailable(f"unexpected none for {key!r} in line {line!r}")
    return value


def _parse_bool(fields: dict[str, str], key: str, line: str) -> bool:
    raw = fields.get(key)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise HeartbeatUnavailable(f"unparseable {key}={raw!r} in line {line!r}")


def _feed_token(fields: dict[str, str], line: str) -> str:
    """The dashboard feed identifier for one status/event line.

    ``market_data:<SYMBOL>`` for a consolidated line, ``ib_gateway`` for the
    broker API connection (the vendor is named HERE, at the operator surface,
    matching the dashboard's account panel language — the Rust core stays
    vendor-neutral).
    """

    kind = fields.get("feed")
    if kind == "broker":
        return "ib_gateway"
    if kind == "market_data":
        symbol = fields.get("symbol")
        if not symbol:
            raise HeartbeatUnavailable(f"market_data line without symbol: {line!r}")
        return f"market_data:{symbol}"
    raise HeartbeatUnavailable(f"unknown feed kind {kind!r} in line {line!r}")


class CliHeartbeatSource:
    """Reads freshness by replaying an observation script through the CLI.

    ``observations_path`` is the fixture / operator-maintained directive
    script (ticks, broker heartbeats, watches — everything except the final
    evaluation instant). Each :meth:`observe` call appends
    ``evaluate <now_ns()>`` and parses the resulting snapshot.
    """

    def __init__(
        self,
        observations_path: str | Path,
        *,
        binary: str | Path = _DEFAULT_BINARY,
        runner: HeartbeatCliRunner = _default_runner,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self._observations_path = Path(observations_path)
        self._binary = str(binary)
        self._runner = runner
        self._timeout_s = timeout_s
        self._now_ns = now_ns

    def observe(self) -> Observation:
        """One evaluation: ``{"statuses": [...], "events": [...], "evaluated_at_ns": int}``.

        Raises:
            HeartbeatUnavailable: the script is unreadable, the binary is
                missing / wedged / refuses the script, or its output drifts
                from the kv machine grammar.
        """

        try:
            script = self._observations_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise HeartbeatUnavailable(
                f"observation script unreadable at {self._observations_path}: {exc}"
            ) from exc

        evaluated_at_ns = int(self._now_ns())
        script = f"{script}\nevaluate {evaluated_at_ns}\n"

        try:
            result = self._runner([self._binary, "-"], input=script, timeout=self._timeout_s)
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
            raise HeartbeatUnavailable(f"heartbeat CLI failed to run: {exc}") from exc
        if result.returncode != 0:
            raise HeartbeatUnavailable(
                f"heartbeat CLI refused the observation script (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

        statuses: list[StatusRow] = []
        events: list[dict[str, object]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("status "):
                statuses.append(self._parse_status(line, evaluated_at_ns))
            elif line.startswith("event "):
                events.append(self._parse_event(line))
            elif line:
                raise HeartbeatUnavailable(f"unrecognized CLI output line {line!r}")
        if not statuses:
            # Fail closed: a script that watches nothing produces zero rows —
            # a monitor that is monitoring nothing must never read as healthy
            # (SYS-39 requires CONTINUOUS monitoring of both feed kinds).
            raise HeartbeatUnavailable(
                f"observation script {self._observations_path} watches no feeds "
                "(no watch-security / watch-broker / tick / broker-heartbeat "
                "directives) — the monitor is not monitoring anything"
            )
        if not any(row["feed"] == "ib_gateway" for row in statuses):
            # SYS-39 names the broker API connection as a REQUIRED monitored
            # feed; a script that never watches it leaves broker freshness
            # unknown, which must not present as a healthy monitor.
            raise HeartbeatUnavailable(
                f"observation script {self._observations_path} does not watch "
                "the broker heartbeat (missing watch-broker / broker-heartbeat "
                "directive) — SYS-39 requires the broker API connection to be "
                "monitored"
            )
        if not any(row["feed"].startswith("market_data:") for row in statuses):
            # SYS-39's other half: "market data ... freshness" — a broker-only
            # script monitors no market-data line at all, which must not
            # present as successful market-data monitoring. (The deferred
            # live feed loop watches the consolidated subscription set and
            # owns the legitimate zero-active-subscriptions state; a
            # fixture / operator script here must name the lines it claims
            # to monitor.)
            raise HeartbeatUnavailable(
                f"observation script {self._observations_path} watches no "
                "market-data line (no watch-security / tick directive) — "
                "SYS-39 requires market-data feed freshness to be monitored"
            )
        return {"statuses": statuses, "events": events, "evaluated_at_ns": evaluated_at_ns}

    def _parse_status(self, line: str, evaluated_at_ns: int) -> StatusRow:
        fields = _parse_kv_line(line)
        row_evaluated_at = _parse_int(fields, "evaluated_at_ns", line)
        if row_evaluated_at != evaluated_at_ns:
            # A status row from an embedded `evaluate` directive inside the
            # observation script is a historical replay, not THIS poll's
            # verdict; only the appended final evaluation may be displayed
            # as current.
            raise HeartbeatUnavailable(
                f"status row for a foreign evaluation instant ({row_evaluated_at} != "
                f"{evaluated_at_ns}): observation scripts must not contain their own "
                "`evaluate` directives"
            )
        return {
            "feed": _feed_token(fields, line),
            "last_observation_ns": _parse_optional_int(fields, "last_observation_ns", line),
            "staleness_ms": _parse_optional_int(fields, "staleness_ms", line),
            "never_observed": _parse_bool(fields, "never_observed", line),
            "time_stale": _parse_bool(fields, "time_stale", line),
            "gap_stale": _parse_bool(fields, "gap_stale", line),
            "stale": _parse_bool(fields, "stale", line),
            "threshold_ms": _parse_int(fields, "threshold_ms", line),
        }

    def _parse_event(self, line: str) -> dict[str, object]:
        fields = _parse_kv_line(line)
        kind = fields.get("kind")
        if kind is None:
            raise HeartbeatUnavailable(f"event line without kind: {line!r}")
        if kind == "SEQUENCE_GAP":
            # MD-007's event; carried through for visibility but logged by
            # MD-007's own seam, not this provider.
            return {"kind": kind, "symbol": fields.get("symbol", "")}
        if kind not in _TRANSITION_EVENT_TYPES:
            raise HeartbeatUnavailable(f"unknown event kind {kind!r} in line {line!r}")
        return {
            "kind": kind,
            "feed": _feed_token(fields, line),
            "staleness_ms": _parse_optional_int(fields, "staleness_ms", line),
            "last_observation_ns": _parse_optional_int(fields, "last_observation_ns", line),
            "evaluated_at_ns": _parse_int(fields, "evaluated_at_ns", line),
            "threshold_ms": _parse_int(fields, "threshold_ms", line),
        }


class HeartbeatFreshnessProvider:
    """Turns monitor observations into HEARTBEAT events, health, and log records."""

    def __init__(
        self,
        source: CliHeartbeatSource,
        *,
        log_store: JsonlLogStore,
    ) -> None:
        # The sink is REQUIRED: SRS-MD-003 makes "logged" a first-class
        # acceptance leg, so a monitor that displays health while silently
        # dropping its HEARTBEAT_STALE/RECOVERED audit trail must be
        # unrepresentable — a composition without durable log storage is a
        # configuration error, not a degraded mode.
        if log_store is None:  # defensive: the annotation alone can't stop None
            raise ValueError(
                "HeartbeatFreshnessProvider requires a durable log_store — "
                "SRS-MD-003's 'logged' acceptance leg is mandatory"
            )
        self._source = source
        self._log_store = log_store
        self._log_write_ok = True
        # feed -> the merged staleness verdict this provider last LOGGED (or,
        # for an initially-fresh feed, last saw). The cross-poll transition
        # baseline: the CLI process is fresh per poll, so once-per-flip
        # logging is enforced here. Guarded by _lock — the publisher's
        # heartbeat ticker and the runtime's REST poll threads both observe.
        self._logged_stale: dict[str, bool] = {}
        # Transition records whose durable write FAILED, queued for retry on
        # every subsequent poll (oldest first, so the audit stream stays
        # chronological). The flip baseline advances on the FACT of a flip;
        # durability is this queue's job — otherwise a transient sink failure
        # during a brief stale incident would lose both the incident and its
        # recovery (the baseline would already read fresh==fresh next poll).
        self._pending_records: list[LogRecord] = []
        # Records dropped from an overflowing pending queue under SUSTAINED
        # sink failure — honest loss accounting, surfaced on the snapshot.
        self._dropped_log_records = 0
        # The evaluation instant and full observation most recently
        # COMMITTED. The CLI runs outside the lock (a REST poll must not
        # stall behind the WS ticker's subprocess), so a slower, OLDER
        # evaluation can finish after a newer one — its verdicts are history
        # and must neither regress the transition baseline (fabricated
        # flips) NOR be served to any operator surface (an older fresh
        # snapshot overwriting a newer committed stale one); such callers
        # get the cached committed observation instead.
        self._last_committed_eval_ns: int | None = None
        self._last_committed_observation: Observation | None = None
        self._lock = threading.Lock()

    # ----- WS channel events (the "displayed" leg, 1 s cadence) ----- #

    def heartbeat_events(self) -> list[dict[str, object]]:
        """The HEARTBEAT events for one publish tick.

        One event per monitored feed, carrying the channel's declared
        ``payload_fields`` (``feed`` / ``last_tick_at`` / ``staleness_seconds``
        / ``is_stale``) plus honest extras (``never_observed``, the gap/time
        split, the threshold). On monitor failure: ONE fail-closed event
        (``is_stale: true``, ``data_source: "unavailable"``) — a broken
        watcher must never render as a healthy green row.
        """

        try:
            observation = self._observe_and_log()
        except HeartbeatUnavailable as exc:
            return [
                {
                    "feed": "monitor",
                    "last_tick_at": None,
                    "staleness_seconds": None,
                    "is_stale": True,
                    "never_observed": True,
                    "threshold_seconds": THRESHOLD_MS / 1000,
                    "data_source": "unavailable",
                    "reason": str(exc),
                }
            ]
        events: list[dict[str, object]] = []
        for row in observation["statuses"]:
            staleness_ms = row["staleness_ms"]
            last_ns = row["last_observation_ns"]
            events.append(
                {
                    "feed": row["feed"],
                    "last_tick_at": _iso_from_ns(last_ns) if last_ns is not None else None,
                    "staleness_seconds": (
                        staleness_ms / 1000 if staleness_ms is not None else None
                    ),
                    "is_stale": row["stale"],
                    "never_observed": row["never_observed"],
                    "time_stale": row["time_stale"],
                    "gap_stale": row["gap_stale"],
                    "threshold_seconds": row["threshold_ms"] / 1000,
                    "data_source": "md003_heartbeat_cli",
                }
            )
        return events

    # ----- REST snapshot + system-health reflection ----- #

    def heartbeat_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/heartbeat``."""

        try:
            observation = self._observe_and_log()
        except HeartbeatUnavailable as exc:
            return {
                "generated_at": _utc_iso(),
                "ok": False,
                "state": "UNAVAILABLE",
                "reason": str(exc),
                "feeds": [],
                "any_stale": True,
                "threshold_ms": THRESHOLD_MS,
                "log_write_ok": self._log_write_ok,
                "pending_log_records": len(self._pending_records),
                "dropped_log_records": self._dropped_log_records,
                "srs_ref": "SRS-MD-003",
            }
        statuses = observation["statuses"]
        return {
            "generated_at": _utc_iso(),
            "ok": True,
            "evaluated_at_ns": observation["evaluated_at_ns"],
            "feeds": statuses,
            "any_stale": any(row["stale"] for row in statuses),
            "threshold_ms": THRESHOLD_MS,
            "log_write_ok": self._log_write_ok,
            "pending_log_records": len(self._pending_records),
            "dropped_log_records": self._dropped_log_records,
            "srs_ref": "SRS-MD-003",
        }

    def health_summary(self) -> dict[str, object]:
        """The ``market_data_heartbeat`` section of the system-health payload.

        Fail-safe like the readiness gate's health: an unreadable monitor is
        an explicit UNAVAILABLE (with ``any_stale: true`` — not proven fresh),
        never an exception into the dashboard poll and never a green.
        """

        try:
            observation = self._observe_and_log()
        except HeartbeatUnavailable as exc:
            return {
                "ok": False,
                "state": "UNAVAILABLE",
                "any_stale": True,
                "stale_feeds": [],
                "threshold_ms": THRESHOLD_MS,
                "reason": str(exc),
                "data_source": "md003_heartbeat_cli",
            }
        statuses = observation["statuses"]
        stale_feeds = sorted(str(row["feed"]) for row in statuses if row["stale"])
        return {
            "ok": not stale_feeds,
            "state": "STALE" if stale_feeds else "FRESH",
            "any_stale": bool(stale_feeds),
            "stale_feeds": stale_feeds,
            "watched_feeds": len(statuses),
            "threshold_ms": THRESHOLD_MS,
            "data_source": "md003_heartbeat_cli",
        }

    # ----- logging (the "logged" leg) ----- #

    def _observe_and_log(self) -> Observation:
        observation = self._source.observe()
        evaluated_at_ns = int(observation["evaluated_at_ns"])
        with self._lock:
            # Monotonic-evaluation guard: concurrent callers (WS ticker, REST
            # poll, health poll) evaluate outside the lock, so completions can
            # arrive out of order. Only an observation NEWER than the last
            # committed one may advance the transition baseline, write log
            # records, OR be served to an operator surface — an older
            # evaluation applied late would report flips that never happened,
            # and an older fresh snapshot must not overwrite a newer
            # committed stale one on the channel / health surfaces. Late
            # callers get the cached committed observation.
            last = self._last_committed_eval_ns
            if last is not None and evaluated_at_ns <= last:
                assert self._last_committed_observation is not None
                return self._last_committed_observation
            self._last_committed_eval_ns = evaluated_at_ns
            self._last_committed_observation = observation
            # Retry queued (previously failed) records FIRST, oldest first,
            # so the audit stream stays chronological across sink outages.
            still_pending = [r for r in self._pending_records if not self._try_write(r)]
            self._pending_records = still_pending
            for row in observation["statuses"]:
                feed = str(row["feed"])
                # HEARTBEAT_STALE/RECOVERED records classify TIME staleness
                # only: a gap-only incident (time_stale false, gap_stale
                # true) is SRS-MD-007's SEQUENCE_GAP audit class and must not
                # be misfiled as heartbeat staleness. The merged row["stale"]
                # verdict remains the display / health / MD-004 surface.
                time_stale = bool(row["time_stale"])
                previous = self._logged_stale.get(feed)
                if previous == time_stale:
                    continue
                if previous is None and not time_stale:
                    # First sighting of a healthy feed: nothing to log —
                    # record the baseline so a later flip logs exactly once.
                    self._logged_stale[feed] = time_stale
                    continue
                # A flip (or an initially-stale first sighting, mirroring the
                # Rust monitor's fail-closed first announcement) is a FACT:
                # advance the baseline unconditionally, and if its record
                # cannot be written now, queue it — a transient sink failure
                # during a brief stale incident must lose NEITHER the
                # incident nor its recovery (holding the baseline back would
                # skip both once the feed reads fresh again next poll).
                self._logged_stale[feed] = time_stale
                record = self._build_log_record(row, time_stale, evaluated_at_ns)
                if not self._try_write(record):
                    self._enqueue_pending(record)
            # The audit trail is intact iff nothing remains queued: one
            # feed's failed write is never masked by another feed's success,
            # and the flag recovers only when every queued record has landed.
            self._log_write_ok = not self._pending_records
        return observation

    def _build_log_record(self, row: StatusRow, stale: bool, evaluated_at_ns: int) -> LogRecord:
        feed = str(row["feed"])
        source = Source.IB_GATEWAY if feed == "ib_gateway" else Source.MARKET_DATA
        kind = "HEARTBEAT_STALE" if stale else "HEARTBEAT_RECOVERED"
        staleness_ms = row["staleness_ms"]
        age = f"{staleness_ms} ms" if staleness_ms is not None else "never observed"
        return LogRecord(
            timestamp_ns=evaluated_at_ns,
            severity=Severity.WARN if stale else Severity.INFO,
            source=source,
            event_type=kind,
            message=(
                f"{feed} heartbeat {'stale' if stale else 'recovered'} "
                f"(age {age}, threshold {row['threshold_ms']} ms)"
            ),
            correlation_id=f"md003:{feed}:{evaluated_at_ns}",
            log_class=LogClass.SYSTEM,
            strategy_id=None,
        )

    def _try_write(self, record: LogRecord) -> bool:
        try:
            self._log_store.write(record)
        except Exception:  # noqa: BLE001 — observability must not kill the tick
            # The freshness state is already committed (Rust side) and the
            # event is still displayed; the lost audit write stays queued for
            # retry and is surfaced on the snapshot for the operator.
            return False
        return True

    def _enqueue_pending(self, record: LogRecord) -> None:
        self._pending_records.append(record)
        # Bound the queue under SUSTAINED sink failure (memory safety beats
        # unbounded buffering); dropped records are counted and surfaced —
        # honest loss accounting, never silent.
        overflow = len(self._pending_records) - _MAX_PENDING_LOG_RECORDS
        if overflow > 0:
            del self._pending_records[:overflow]
            self._dropped_log_records += overflow
