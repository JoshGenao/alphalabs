"""Dashboard WebSocket publisher (SRS-UI-001; SRS-UI-002 inventory channel).

Drives the live-update side of the dashboard: claims the three channels
SRS-UI-001 owns (``PNL`` / ``METRICS`` / ``HEARTBEAT``) — plus, when a
strategy-inventory provider is mounted, the SRS-UI-002 ``STRATEGY_STATE``
channel — via :meth:`OperatorInterfaceRuntime.register_publisher`, then runs a
single daemon ticker thread that publishes each channel's current payload at
its declared ``refresh_seconds`` cadence (each ``≤ MAX_REFRESH_SECONDS`` — the
NFR-P2 5 s ceiling) through :meth:`OperatorInterfaceRuntime.publish`.
``STRATEGY_STATE`` publishes one summary event plus one event per recorded
strategy per tick (the per-strategy shape the atp_ws contract declares).

An **immediate first tick** is emitted per channel on start (rather than
sleep-then-publish) so a freshly-subscribed client sees data well inside the 5 s
budget on the fast 1 s channels. Shutdown is deterministic: a
:class:`threading.Event` interrupts the sleep and the thread is joined with a
bounded timeout — no leaked daemon thread (mirrors the runtime's ``atp-ws-writer``
discipline).

SRS trace
---------
``SRS-UI-001`` (owned publishers), ``NFR-P2`` (≤5 s cadence), ``SRS-API-001``
(``publish`` / ``register_publisher`` seam).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from functools import partial

from atp_runtime import OperatorInterfaceRuntime
from atp_ws import EVENT_CHANNELS, MAX_REFRESH_SECONDS

from .account import ACCOUNT_CHANNEL, AccountStatusProvider
from .heartbeat import HEARTBEAT_CHANNEL, HeartbeatFreshnessProvider
from .inventory import INVENTORY_CHANNEL, StrategyInventoryProvider
from .provider import OWNED_CHANNELS, DashboardMetricsProvider
from .reservoir import RESERVOIR_CHANNEL, ReservoirRankingProvider

#: Longest a fast poll may sleep between due-time checks — keeps ``stop()``
#: responsive even when the soonest channel is a full ``MAX_REFRESH_SECONDS`` off.
_POLL_CEILING_S = 0.5


def cadence_for(channel: str) -> int:
    """Return a channel's publish cadence in seconds from the atp_ws contract.

    Raises if the channel is unknown or declares a cadence outside
    ``[1, MAX_REFRESH_SECONDS]`` (an event-driven ``0`` is not a periodic
    dashboard channel and must not be scheduled here).
    """

    for spec in EVENT_CHANNELS:
        if spec.name == channel:
            seconds = spec.refresh_seconds
            if not 1 <= seconds <= MAX_REFRESH_SECONDS:
                raise ValueError(
                    f"channel {channel!r} cadence {seconds}s is not in "
                    f"[1, {MAX_REFRESH_SECONDS}] (NFR-P2)"
                )
            return seconds
    raise ValueError(f"unknown event channel {channel!r}")


class DashboardPublisher:
    """Periodically publishes the SRS-UI-001 channels from a provider."""

    def __init__(
        self,
        runtime: OperatorInterfaceRuntime,
        provider: DashboardMetricsProvider,
        *,
        channels: Iterable[str] = OWNED_CHANNELS,
        inventory: StrategyInventoryProvider | None = None,
        account: AccountStatusProvider | None = None,
        reservoir: ReservoirRankingProvider | None = None,
        heartbeat: HeartbeatFreshnessProvider | None = None,
    ) -> None:
        self._runtime = runtime
        self._provider = provider
        self._inventory = inventory
        self._heartbeat = heartbeat
        self._channels: tuple[str, ...] = tuple(channels)
        # Opt-in single-event channels (SRS-UI-003 account + Reservoir) ride the
        # MAIN ticker: each is a pure-Python deferred builder (no subprocess, no
        # blocking I/O), so — unlike the inventory channel, which needs its own
        # thread precisely to contain a shelled CLI that may hang to its timeout —
        # they cannot starve the fast PNL/HEARTBEAT ticks and need no isolation.
        extra: dict[str, Callable[[], list[dict[str, object]]]] = {}
        if account is not None:
            extra[ACCOUNT_CHANNEL] = account.account_status_events
        if reservoir is not None:
            extra[RESERVOIR_CHANNEL] = reservoir.reservoir_ranking_events
        self._extra = extra
        # When the SRS-MD-003 freshness provider is mounted, HEARTBEAT moves
        # OFF the main ticker onto its own isolated ticker (the inventory
        # pattern, and for the same reason: the provider shells a bounded
        # subprocess, and a wedged binary must delay only the HEARTBEAT
        # channel — whose panel dot then goes stale honestly — never the 1 s
        # PNL tick feeding the NFR-P2 gauge). Unmounted, HEARTBEAT stays on
        # the main ticker publishing the provider's honest deferred cells.
        main = list(self._channels) + [c for c in extra if c not in self._channels]
        if heartbeat is not None and HEARTBEAT_CHANNEL in main:
            main.remove(HEARTBEAT_CHANNEL)
        self._main_channels: tuple[str, ...] = tuple(main)
        # Fail fast on a mis-declared cadence before any thread starts. The
        # inventory channel joins the schedule only when its provider is mounted
        # (SRS-UI-002 is composition-time opt-in, like the dashboard itself).
        scheduled = list(self._main_channels)
        if heartbeat is not None:
            scheduled.append(HEARTBEAT_CHANNEL)
        if inventory is not None:
            scheduled.append(INVENTORY_CHANNEL)
        self._scheduled: tuple[str, ...] = tuple(scheduled)
        self._cadences: dict[str, int] = {c: cadence_for(c) for c in self._scheduled}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._inventory_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        """Every channel this publisher claims (owned + mounted opt-in channels)."""

        return self._scheduled

    def start(self) -> None:
        """Register the owned publishers and start the ticker thread(s) (not re-entrant).

        The inventory channel runs on its OWN daemon ticker: its provider shells
        a subprocess (bounded, but up to its timeout), and a wedged binary must
        delay only STRATEGY_STATE (whose panel dot goes stale honestly) — never
        starve the 1 s PNL/HEARTBEAT ticks that feed the NFR-P2 gauge.
        """

        if self._thread is not None:
            raise RuntimeError("publisher already started; call stop() first")
        for channel in self._scheduled:
            self._runtime.register_publisher(channel)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="atp-dashboard-publisher", daemon=True
        )
        self._thread.start()
        if self._inventory is not None:
            self._inventory_thread = threading.Thread(
                target=self._run_inventory, name="atp-dashboard-inventory", daemon=True
            )
            self._inventory_thread.start()
        if self._heartbeat is not None:
            self._heartbeat_thread = threading.Thread(
                target=self._run_heartbeat, name="atp-dashboard-heartbeat", daemon=True
            )
            self._heartbeat_thread.start()

    def stop(self) -> None:
        """Signal the tickers to exit and join them with a bounded timeout."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=MAX_REFRESH_SECONDS + 1)
            self._thread = None
        if self._inventory_thread is not None:
            self._inventory_thread.join(timeout=MAX_REFRESH_SECONDS + 1)
            self._inventory_thread = None
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=MAX_REFRESH_SECONDS + 1)
            self._heartbeat_thread = None

    def publish_once(self) -> dict[str, int]:
        """Publish the current payload for every claimed channel once (delivery counts)."""

        counts = {channel: self._publish_channel(channel) for channel in self._main_channels}
        if self._heartbeat is not None:
            counts[HEARTBEAT_CHANNEL] = self._publish_heartbeat()
        if self._inventory is not None:
            counts[INVENTORY_CHANNEL] = self._publish_inventory()
        return counts

    def _publish_channel(self, channel: str) -> int:
        """Publish one main-ticker channel once: an owned provider payload, or —
        for an opt-in single-event source (account / Reservoir) — each event it
        emits this tick (returns the total delivery count)."""

        builder = self._extra.get(channel)
        if builder is not None:
            return sum(self._runtime.publish(channel, event) for event in builder())
        return self._runtime.publish(channel, self._provider.channel_payload(channel))

    def _publish_inventory(self) -> int:
        """One STRATEGY_STATE tick: the summary event + one event per strategy."""

        assert self._inventory is not None  # only scheduled when mounted
        return sum(
            self._runtime.publish(INVENTORY_CHANNEL, event)
            for event in self._inventory.strategy_state_events()
        )

    def _publish_heartbeat(self) -> int:
        """One HEARTBEAT tick: one event per monitored feed (SRS-MD-003)."""

        assert self._heartbeat is not None  # only scheduled when mounted
        return sum(
            self._runtime.publish(HEARTBEAT_CHANNEL, event)
            for event in self._heartbeat.heartbeat_events()
        )

    @staticmethod
    def _guarded(tick: Callable[[], object], channel: str) -> None:
        """Run one channel tick, containing ANY failure to that tick.

        A monitoring publisher must keep publishing: one channel's bad tick (a
        provider bug, a drifted subprocess, a transient OSError) must never kill
        the ticker thread and silently starve every other channel — the outage
        would be invisible exactly when observability matters. The failed
        channel simply misses this tick; its panel freshness dot reports the
        gap honestly.
        """

        try:
            tick()
        except Exception:  # noqa: BLE001 - observability must not crash
            pass

    def _run(self) -> None:
        # Immediate first tick: every main-ticker channel is due at start (owned
        # SRS-UI-001 channels + the opt-in SRS-UI-003 account / Reservoir channels).
        next_fire: dict[str, float] = {c: time.monotonic() for c in self._main_channels}
        while not self._stop.is_set():
            now = time.monotonic()
            for channel in self._main_channels:
                if now >= next_fire[channel]:
                    self._guarded(partial(self._publish_channel, channel), channel)
                    next_fire[channel] = now + self._cadences[channel]
            soonest = min(next_fire.values())
            wait = max(0.0, min(soonest - time.monotonic(), _POLL_CEILING_S))
            self._stop.wait(wait)

    def _run_inventory(self) -> None:
        # The isolated STRATEGY_STATE ticker (see start()). Immediate first tick.
        cadence = self._cadences[INVENTORY_CHANNEL]
        next_fire = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() >= next_fire:
                self._guarded(self._publish_inventory, INVENTORY_CHANNEL)
                next_fire = time.monotonic() + cadence
            wait = max(0.0, min(next_fire - time.monotonic(), _POLL_CEILING_S))
            self._stop.wait(wait)

    def _run_heartbeat(self) -> None:
        # The isolated SRS-MD-003 HEARTBEAT ticker (see __init__ / start()).
        # Immediate first tick; each tick re-evaluates freshness against the
        # provider's wall clock, forming the AC's continuous monitoring loop.
        cadence = self._cadences[HEARTBEAT_CHANNEL]
        next_fire = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() >= next_fire:
                self._guarded(self._publish_heartbeat, HEARTBEAT_CHANNEL)
                next_fire = time.monotonic() + cadence
            wait = max(0.0, min(next_fire - time.monotonic(), _POLL_CEILING_S))
            self._stop.wait(wait)
