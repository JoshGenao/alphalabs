"""L1 unit — SRS-UI-001 dashboard publisher: cadence, registration, clean stop.

Uses a fake runtime (records register/publish calls) so the ticker logic is
tested without binding a socket. Verifies the publisher claims exactly its owned
channels at NFR-P2-legal cadences, is not re-entrant, and leaves no daemon thread
behind after :meth:`stop`.
"""

from __future__ import annotations

import threading
import time

import pytest
from atp_dashboard import OWNED_CHANNELS, DashboardPublisher
from atp_dashboard.publisher import cadence_for
from atp_ws import MAX_REFRESH_SECONDS

pytestmark = pytest.mark.unit


class _FakeRuntime:
    """Minimal stand-in for OperatorInterfaceRuntime's publisher seam."""

    def __init__(self) -> None:
        self.registered: list[str] = []
        self.published: list[tuple[str, object]] = []
        self._lock = threading.Lock()

    def register_publisher(self, channel: str) -> None:
        self.registered.append(channel)

    def is_publisher_registered(self, channel: str) -> bool:
        return channel in self.registered

    def publish(self, channel: str, payload: object) -> int:
        with self._lock:
            self.published.append((channel, payload))
        return 1


class _FakeProvider:
    def channel_payload(self, channel: str) -> dict[str, object]:
        return {"channel": channel}

    def system_snapshot(self) -> dict[str, object]:
        return {}


def test_cadences_are_within_the_nfr_p2_ceiling() -> None:
    assert cadence_for("PNL") == 1
    assert cadence_for("HEARTBEAT") == 1
    assert cadence_for("METRICS") == 5
    for channel in OWNED_CHANNELS:
        assert 1 <= cadence_for(channel) <= MAX_REFRESH_SECONDS


def test_cadence_for_rejects_unknown_channel() -> None:
    with pytest.raises(ValueError):
        cadence_for("NOPE")


def test_start_registers_exactly_the_owned_channels() -> None:
    runtime = _FakeRuntime()
    pub = DashboardPublisher(runtime, _FakeProvider())
    assert pub.channels == tuple(OWNED_CHANNELS)
    pub.start()
    try:
        assert sorted(runtime.registered) == sorted(OWNED_CHANNELS)
    finally:
        pub.stop()


def test_start_is_not_reentrant() -> None:
    pub = DashboardPublisher(_FakeRuntime(), _FakeProvider())
    pub.start()
    try:
        with pytest.raises(RuntimeError):
            pub.start()
    finally:
        pub.stop()


def test_immediate_first_tick_publishes_every_channel_fast() -> None:
    runtime = _FakeRuntime()
    pub = DashboardPublisher(runtime, _FakeProvider())
    pub.start()
    try:
        # The immediate first tick fires all channels well inside the 5s budget.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with runtime._lock:
                seen = {c for c, _ in runtime.published}
            if seen >= set(OWNED_CHANNELS):
                break
            time.sleep(0.02)
        assert seen >= set(OWNED_CHANNELS)
    finally:
        pub.stop()


def test_publish_once_returns_delivery_counts() -> None:
    pub = DashboardPublisher(_FakeRuntime(), _FakeProvider())
    counts = pub.publish_once()
    assert set(counts) == set(OWNED_CHANNELS)
    assert all(v == 1 for v in counts.values())


def test_stop_leaves_no_daemon_thread() -> None:
    before = {t.name for t in threading.enumerate()}
    pub = DashboardPublisher(_FakeRuntime(), _FakeProvider())
    pub.start()
    time.sleep(0.1)
    pub.stop()
    # The ticker thread must be joined and gone.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        alive = {t.name for t in threading.enumerate() if t.is_alive()}
        if "atp-dashboard-publisher" not in (alive - before):
            break
        time.sleep(0.05)
    leaked = {
        t for t in threading.enumerate() if t.name == "atp-dashboard-publisher" and t.is_alive()
    }
    assert not leaked, "publisher ticker thread leaked after stop()"
