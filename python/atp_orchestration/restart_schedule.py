"""SRS-MD-005 / SyRS SYS-75 — resolve the configured IB Gateway restart time to an instant.

The Rust side owns the *arithmetic*: ``atp_types::RestartWindow`` classifies an
epoch-nanosecond instant into a SYS-75 phase and derives the authoritative
``ConnectivityState``. It deliberately does not own the *calendar*.

SYS-75 puts the restart at "approximately 23:45 ET for live accounts,
configurable", and US Eastern shifts by an hour twice a year. The Rust
workspace has no third-party crates and therefore no timezone database, so a
Rust implementation would have to hand-roll DST — forking an authority this
repo already has. ``atp_strategy.calendar.EASTERN`` is a real
``zoneinfo("America/New_York")``; ``atp_reliability.boot_evidence`` already
resolves market-hours scope through it. This module reuses it.

The split is therefore: **Python resolves, Rust classifies.** A missed DST
adjustment would move the suspension window by an hour — trading suspended at
the wrong time, and a real restart arriving unsuppressed — so getting it from a
tz database rather than an operator's twice-yearly edit is the point.

Nothing here reads the wall clock unless asked to. Every function takes the
date or instant it operates on, so a run is reproducible.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "NANOS_PER_SECOND",
    "DEFAULT_RESTART_ET",
    "DEFAULT_RESTART_WINDOW_SECONDS",
    "DEFAULT_RESTART_SUSPEND_LEAD_SECONDS",
    "RestartScheduleError",
    "RestartSchedule",
    "parse_restart_time",
    "resolve_restart_instant_ns",
]

NANOS_PER_SECOND: Final[int] = 1_000_000_000

#: SyRS SYS-75 reference restart time, US Eastern. Mirrored by
#: ``ATP_IB_RESTART_ET`` in the SRS-ARCH-005 configuration catalogue.
DEFAULT_RESTART_ET: Final[str] = "23:45"

#: SyRS SYS-75(b). Mirrored by ``ATP_IB_RESTART_WINDOW_SECONDS`` and by
#: ``atp_types::DEFAULT_RESTART_WINDOW_SECONDS``.
DEFAULT_RESTART_WINDOW_SECONDS: Final[int] = 300

#: SyRS SYS-75(a). Mirrored by ``ATP_IB_RESTART_SUSPEND_LEAD_SECONDS`` and by
#: ``atp_types::DEFAULT_RESTART_SUSPEND_LEAD_SECONDS``.
DEFAULT_RESTART_SUSPEND_LEAD_SECONDS: Final[int] = 60

_TIME_RE: Final[re.Pattern[str]] = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")

#: Bounds mirroring the catalogue validators. A window longer than a day or a
#: lead longer than an hour cannot describe a gateway restart, and accepting one
#: would suspend trading for a span nobody intended.
_MAX_WINDOW_SECONDS: Final[int] = 86_400
_MAX_LEAD_SECONDS: Final[int] = 3_600


class RestartScheduleError(ValueError):
    """A restart schedule could not be built.

    Always raised, never defaulted. A malformed value must not silently fall
    back to 23:45: an operator who mistyped the restart time would get a window
    that looks configured and suspends at the wrong hour, which is worse than a
    startup failure they can read.
    """


def parse_restart_time(value: str) -> tuple[int, int]:
    """Parse an ``HH:MM`` 24-hour local-Eastern restart time.

    Fails closed on anything else — including the shapes that *look* tolerable,
    such as ``"23:45:00"`` or a bare hour. Accepting a near-miss is how a config
    typo becomes a silently wrong window.
    """

    if not isinstance(value, str):
        raise RestartScheduleError(f"restart time must be a string; got {type(value).__name__}")
    match = _TIME_RE.match(value.strip())
    if match is None:
        raise RestartScheduleError(
            f"restart time {value!r} is not HH:MM (24-hour, US Eastern) — "
            "SyRS SYS-75 defaults to 23:45"
        )
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if not 0 <= hour <= 23:
        raise RestartScheduleError(f"restart hour {hour} is outside 0..23 (from {value!r})")
    if not 0 <= minute <= 59:
        raise RestartScheduleError(f"restart minute {minute} is outside 0..59 (from {value!r})")
    return hour, minute


@dataclass(frozen=True, slots=True)
class RestartSchedule:
    """The configured SYS-75 schedule, validated.

    ``restart_et`` is a local Eastern wall-clock time, so the same schedule
    yields different UTC instants either side of a DST transition. That is the
    intent: IB restarts on its own local clock, not on UTC.
    """

    restart_et: str = DEFAULT_RESTART_ET
    window_seconds: int = DEFAULT_RESTART_WINDOW_SECONDS
    suspend_lead_seconds: int = DEFAULT_RESTART_SUSPEND_LEAD_SECONDS

    def __post_init__(self) -> None:
        parse_restart_time(self.restart_et)
        _require_positive_bounded(
            self.window_seconds, "window_seconds", _MAX_WINDOW_SECONDS, "SyRS SYS-75(b)"
        )
        _require_positive_bounded(
            self.suspend_lead_seconds,
            "suspend_lead_seconds",
            _MAX_LEAD_SECONDS,
            "SyRS SYS-75(a)",
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "RestartSchedule":
        """Read ``ATP_IB_RESTART_*``.

        A **missing** key takes the documented catalogue default; a **malformed**
        one raises. That asymmetry is deliberate and matches the IB connection
        config: an unset value means "the operator accepted the default", while
        a present-but-wrong value means they tried to say something and it did
        not parse.
        """

        source = os.environ if env is None else env
        return cls(
            restart_et=source.get("ATP_IB_RESTART_ET") or DEFAULT_RESTART_ET,
            window_seconds=_int_from_env(
                source, "ATP_IB_RESTART_WINDOW_SECONDS", DEFAULT_RESTART_WINDOW_SECONDS
            ),
            suspend_lead_seconds=_int_from_env(
                source,
                "ATP_IB_RESTART_SUSPEND_LEAD_SECONDS",
                DEFAULT_RESTART_SUSPEND_LEAD_SECONDS,
            ),
        )

    def restart_instant_ns(self, session_date: _dt.date) -> int:
        """The epoch-nanosecond instant of the restart on ``session_date`` (ET)."""

        return resolve_restart_instant_ns(session_date, self.restart_et)

    def window_ns(self) -> int:
        return self.window_seconds * NANOS_PER_SECOND

    def suspend_lead_ns(self) -> int:
        return self.suspend_lead_seconds * NANOS_PER_SECOND

    def cli_args(self, session_date: _dt.date) -> list[str]:
        """The flags that hand this schedule to ``md005_connectivity_restart_window_cli``.

        Built here rather than at each call site so the resolved instant and the
        two durations always travel together — a caller that passed the instant
        but kept default durations would evaluate a window the operator never
        configured.
        """

        return [
            "--restart-ns",
            str(self.restart_instant_ns(session_date)),
            "--lead-seconds",
            str(self.suspend_lead_seconds),
            "--window-seconds",
            str(self.window_seconds),
        ]


def resolve_restart_instant_ns(session_date: _dt.date, restart_et: str = DEFAULT_RESTART_ET) -> int:
    """Resolve ``restart_et`` on ``session_date`` to epoch nanoseconds.

    Uses the DST-aware ``atp_strategy.calendar.EASTERN`` zone, so 23:45 ET is
    03:45 UTC in EDT and 04:45 UTC in EST without the caller knowing which.

    Two Eastern edge cases, both handled explicitly rather than by whatever
    ``zoneinfo`` happens to default to:

    * **Ambiguous** (the autumn fall-back hour occurs twice). Take the FIRST
      occurrence (``fold=0``), which is the earlier instant — suspension starts
      earlier and the window opens earlier, so a real restart is inside it
      either way.
    * **Non-existent** (the spring forward hour is skipped). ``zoneinfo``
      normalises rather than raising, and a silently shifted maintenance window
      is exactly the failure this module exists to prevent — so it is refused.

    23:45 falls in neither for US Eastern (transitions happen at 02:00 local),
    but the restart time is configurable and a future operator may not know
    that.
    """

    from atp_strategy.calendar import EASTERN

    if not isinstance(session_date, _dt.date) or isinstance(session_date, _dt.datetime):
        raise RestartScheduleError(
            f"session_date must be a datetime.date; got {type(session_date).__name__}"
        )
    hour, minute = parse_restart_time(restart_et)
    naive = _dt.datetime(session_date.year, session_date.month, session_date.day, hour, minute)
    local = naive.replace(tzinfo=EASTERN, fold=0)

    # A non-existent local time normalises to a DIFFERENT wall clock when round
    # -tripped through UTC. Detect it rather than accept a shifted window.
    round_tripped = local.astimezone(_dt.timezone.utc).astimezone(EASTERN)
    if (round_tripped.hour, round_tripped.minute) != (hour, minute):
        raise RestartScheduleError(
            f"{restart_et} does not exist on {session_date.isoformat()} in US Eastern "
            "(a daylight-saving transition skips it) — the restart time must name a "
            "real local instant, not one the clock jumps over"
        )

    epoch_seconds = local.timestamp()
    return int(round(epoch_seconds)) * NANOS_PER_SECOND


def _int_from_env(source: dict[str, str] | os._Environ[str], key: str, default: int) -> int:
    raw = source.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise RestartScheduleError(f"{key}={raw!r} is not an integer") from error


def _require_positive_bounded(value: int, name: str, maximum: int, trace: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RestartScheduleError(f"{name} must be an int; got {type(value).__name__}")
    if value <= 0:
        raise RestartScheduleError(
            f"{name} must be positive ({trace}); got {value}. Zero would collapse the "
            "window, so a restart would page as a genuine outage."
        )
    if value > maximum:
        raise RestartScheduleError(
            f"{name}={value} exceeds {maximum} ({trace}) — a span that long cannot "
            "describe a gateway restart, and accepting it would suspend trading for "
            "a period nobody intended."
        )
