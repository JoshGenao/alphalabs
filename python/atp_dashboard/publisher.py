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
from collections.abc import Iterable

from atp_runtime import OperatorInterfaceRuntime
from atp_ws import EVENT_CHANNELS, MAX_REFRESH_SECONDS

from .inventory import INVENTORY_CHANNEL, StrategyInventoryProvider
from .provider import OWNED_CHANNELS, DashboardMetricsProvider

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
    ) -> None:
        self._runtime = runtime
        self._provider = provider
        self._inventory = inventory
        self._channels: tuple[str, ...] = tuple(channels)
        # Fail fast on a mis-declared cadence before any thread starts. The
        # inventory channel joins the schedule only when its provider is mounted
        # (SRS-UI-002 is composition-time opt-in, like the dashboard itself).
        scheduled = list(self._channels)
        if inventory is not None:
            scheduled.append(INVENTORY_CHANNEL)
        self._scheduled: tuple[str, ...] = tuple(scheduled)
        self._cadences: dict[str, int] = {c: cadence_for(c) for c in self._scheduled}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def channels(self) -> tuple[str, ...]:
        """Every channel this publisher claims (owned + the mounted inventory)."""

        return self._scheduled

    def start(self) -> None:
        """Register the owned publishers and start the ticker thread (not re-entrant)."""

        if self._thread is not None:
            raise RuntimeError("publisher already started; call stop() first")
        for channel in self._scheduled:
            self._runtime.register_publisher(channel)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="atp-dashboard-publisher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the ticker to exit and join it with a bounded timeout."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=MAX_REFRESH_SECONDS + 1)
            self._thread = None

    def publish_once(self) -> dict[str, int]:
        """Publish the current payload for every claimed channel once (delivery counts)."""

        counts = {
            channel: self._runtime.publish(channel, self._provider.channel_payload(channel))
            for channel in self._channels
        }
        if self._inventory is not None:
            counts[INVENTORY_CHANNEL] = self._publish_inventory()
        return counts

    def _publish_inventory(self) -> int:
        """One STRATEGY_STATE tick: the summary event + one event per strategy."""

        assert self._inventory is not None  # only scheduled when mounted
        return sum(
            self._runtime.publish(INVENTORY_CHANNEL, event)
            for event in self._inventory.strategy_state_events()
        )

    def _run(self) -> None:
        # Immediate first tick: every channel is due at start.
        next_fire: dict[str, float] = {c: time.monotonic() for c in self._scheduled}
        while not self._stop.is_set():
            now = time.monotonic()
            for channel in self._scheduled:
                if now >= next_fire[channel]:
                    if channel == INVENTORY_CHANNEL:
                        self._publish_inventory()
                    else:
                        self._runtime.publish(channel, self._provider.channel_payload(channel))
                    next_fire[channel] = now + self._cadences[channel]
            soonest = min(next_fire.values())
            wait = max(0.0, min(soonest - time.monotonic(), _POLL_CEILING_S))
            self._stop.wait(wait)
