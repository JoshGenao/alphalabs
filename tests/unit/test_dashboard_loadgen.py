"""L1 unit — SRS-UI-001 synthetic NFR-SC1-shaped load harness.

Uses a fake runtime (records register/publish calls) so the load generator is
tested without a socket or browser. Verifies the NFR-SC1 strategy shape, the
contract-exact payload keys, honest (never fabricated) synthetic metrics, the
non-vacuity gate in both failure directions, and a clean bounded stop.
"""

from __future__ import annotations

import threading

import pytest
from atp_dashboard import LOAD_CHANNELS, LOAD_DATA_SOURCE, SyntheticStrategyLoad
from atp_ws import EVENT_CHANNELS, Channel

pytestmark = pytest.mark.unit


class _FakeRuntime:
    """Minimal stand-in for OperatorInterfaceRuntime's publisher seam."""

    def __init__(self, deliveries: int = 1) -> None:
        self.deliveries = deliveries
        self.registered: list[str] = []
        self.published: list[tuple[str, object]] = []
        self._lock = threading.Lock()

    def register_publisher(self, channel: str) -> None:
        self.registered.append(channel)

    def publish(self, channel: str, payload: object) -> int:
        with self._lock:
            self.published.append((channel, payload))
        return self.deliveries


def _fields(channel: str) -> tuple[str, ...]:
    return next(spec.payload_fields for spec in EVENT_CHANNELS if spec.name == channel)


def test_default_shape_is_nfr_sc1_one_live_thirty_paper() -> None:
    load = SyntheticStrategyLoad(_FakeRuntime())
    assert len(load.strategy_ids) == 31
    assert len(set(load.strategy_ids)) == 31
    assert load.strategy_ids[0] == "synthetic-live-1"
    assert sum(1 for sid in load.strategy_ids if sid.startswith("synthetic-paper-")) == 30


@pytest.mark.parametrize("channel", LOAD_CHANNELS)
def test_payload_keys_are_exactly_the_channel_contract(channel: str) -> None:
    runtime = _FakeRuntime()
    load = SyntheticStrategyLoad(runtime, live_count=1, paper_count=1)
    load.publish_once()
    payloads = [p for c, p in runtime.published if c == channel]
    assert len(payloads) == 2  # one per strategy
    for payload in payloads:
        assert tuple(payload) == _fields(channel)


@pytest.mark.parametrize("channel", LOAD_CHANNELS)
def test_synthetic_metrics_are_honest_never_fabricated(channel: str) -> None:
    runtime = _FakeRuntime()
    SyntheticStrategyLoad(runtime, live_count=1, paper_count=0).publish_once()
    for _, payload in runtime.published:
        for name, cell in payload.items():
            if name in ("strategy_id", "as_of"):
                continue
            assert cell == {"value": None, "data_source": LOAD_DATA_SOURCE}, (
                f"{channel}.{name} fabricated a synthetic value"
            )


def test_publish_once_counts_per_strategy_and_channel() -> None:
    load = SyntheticStrategyLoad(_FakeRuntime(deliveries=3), live_count=1, paper_count=2)
    load.publish_once()
    load.publish_once()
    published = load.published
    assert set(published) == set(load.strategy_ids)
    for counts in published.values():
        assert counts == dict.fromkeys(LOAD_CHANNELS, 2)
    # 3 strategies × 2 channels × 2 rounds × 3 deliveries each.
    assert load.delivered == 36


def test_assert_load_ran_passes_when_published_and_delivered() -> None:
    load = SyntheticStrategyLoad(_FakeRuntime(), live_count=1, paper_count=2)
    load.publish_once()
    load.publish_once()
    load.assert_load_ran(min_ticks_per_strategy=2)


def test_assert_load_ran_rejects_underpublished_load() -> None:
    load = SyntheticStrategyLoad(_FakeRuntime(), live_count=1, paper_count=2)
    load.publish_once()
    with pytest.raises(AssertionError, match="under-published"):
        load.assert_load_ran(min_ticks_per_strategy=2)


def test_assert_load_ran_rejects_undelivered_load() -> None:
    # Publishing into a hub with zero subscribers must NOT count as evidence.
    load = SyntheticStrategyLoad(_FakeRuntime(deliveries=0), live_count=1, paper_count=2)
    load.publish_once()
    load.publish_once()
    with pytest.raises(AssertionError, match="never delivered"):
        load.assert_load_ran(min_ticks_per_strategy=2)


def test_degenerate_shapes_and_channels_are_rejected() -> None:
    with pytest.raises(ValueError):
        SyntheticStrategyLoad(_FakeRuntime(), live_count=-1, paper_count=30)
    with pytest.raises(ValueError):
        SyntheticStrategyLoad(_FakeRuntime(), live_count=0, paper_count=0)
    with pytest.raises(ValueError):
        SyntheticStrategyLoad(_FakeRuntime(), channels=())
    with pytest.raises(ValueError):
        SyntheticStrategyLoad(_FakeRuntime(), channels=("NOPE",))
    with pytest.raises(ValueError):
        # Event-driven channels (refresh_seconds=0) are not periodic load.
        SyntheticStrategyLoad(_FakeRuntime(), channels=(Channel.LOGS,))


def test_start_registers_channels_runs_and_stops_cleanly() -> None:
    runtime = _FakeRuntime()
    load = SyntheticStrategyLoad(runtime, live_count=1, paper_count=2)
    load.start()
    with pytest.raises(RuntimeError):
        load.start()  # not re-entrant
    try:
        deadline = threading.Event()
        # The immediate first tick publishes every strategy on every channel.
        for _ in range(200):
            if all(
                all(count >= 1 for count in counts.values()) for counts in load.published.values()
            ):
                break
            deadline.wait(0.05)
        else:
            pytest.fail("load ticker never published its immediate first tick")
    finally:
        load.stop()
    assert set(runtime.registered) == set(LOAD_CHANNELS)
    before = threading.active_count()
    assert all(t.name != "atp-dashboard-loadgen" for t in threading.enumerate()), before
