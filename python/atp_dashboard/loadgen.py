"""Synthetic dashboard load harness at the NFR-SC1 strategy shape (SRS-UI-001).

Drives the dashboard's WebSocket fan-out with per-strategy traffic at the
NFR-SC1 strategy **shape** — ``1 live + 30 paper`` synthetic producers, each
publishing on the periodic dashboard channels (``PNL`` every 1 s, ``METRICS``
every 5 s) through the real :meth:`OperatorInterfaceRuntime.publish` seam — so
a browser session subscribed to the dashboard processes 31 producers' worth of
WebSocket traffic while its self-measured refresh latency is asserted against
the NFR-P2 5 s budget.

Scope (what this is, and is not)
--------------------------------
This is a **synthetic approximation** of the NFR-SC1 release baseline, not the
baseline itself: it exercises the dashboard-facing load path (per-strategy
event fan-out → WS hub → JSON encode → browser processing) but starts **no**
strategy containers, internal simulation engines, or market-data processing —
those producers are still-deferred features (``SRS-SIM-001`` strategy runtime,
``SRS-EXE-001`` live engine, ``SRS-MD-006/007`` market-data feeds; the docker
``phase1`` stack orchestrates them). Measuring dashboard refresh under the
fully orchestrated 1-live + 30-paper container stack is the deferred stronger
form of this evidence and remains owned by those features; this harness is the
operator-authorized, repeatable, checked-in load leg available today.

Honesty (no fabrication)
------------------------
Synthetic strategies carry **no fabricated metric values**: every metric field
is the honest deferred descriptor ``{"value": None, "data_source":
"deferred:load-harness(synthetic)"}`` (the panels keep rendering "—" with a
deferred badge). The WebSocket traffic is real regardless — the numeric
content is not what the fan-out path exercises. Payload keys are built from
each channel's declared ``payload_fields`` so the harness can never drift from
the atp_ws contract.

Non-vacuity
-----------
:meth:`SyntheticStrategyLoad.assert_load_ran` fails unless **every** synthetic
strategy published at least ``min_ticks_per_strategy`` events on **every**
loaded channel AND at least one event was actually delivered to a subscribed
WebSocket session — a load test whose load never ran (or was never observed by
the measured client) must fail, never pass vacuously.

SRS trace
---------
``SRS-UI-001`` (AC: refresh ≤5 s under release baseline load — this harness is
the synthetic, shape-matched leg of that evidence), ``NFR-SC1`` (1 live + 30
paper strategy shape), ``NFR-P2`` (5 s budget), ``SRS-API-001`` (``publish`` /
``register_publisher`` seam).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Iterable

from atp_runtime import OperatorInterfaceRuntime
from atp_ws import EVENT_CHANNELS, MAX_REFRESH_SECONDS, Channel

from .provider import DEFERRED, ReadinessBackedProvider, _utc_iso
from .publisher import _POLL_CEILING_S, cadence_for

#: ``data_source`` carried by every synthetic metric field — the "owner" is the
#: harness itself, so a reader can tell load traffic from a real deferred field.
LOAD_DATA_SOURCE = f"{DEFERRED}:load-harness(synthetic)"

#: NFR-SC1 strategy shape (counts only — the real producers are deferred).
BASELINE_LIVE_COUNT = 1
BASELINE_PAPER_COUNT = 30

#: The periodic per-strategy dashboard channels the baseline load drives.
LOAD_CHANNELS: tuple[str, ...] = (Channel.PNL, Channel.METRICS)


def _payload_fields(channel: str) -> tuple[str, ...]:
    """The channel's declared ``payload_fields`` from the atp_ws contract."""

    for spec in EVENT_CHANNELS:
        if spec.name == channel:
            return spec.payload_fields
    raise ValueError(f"unknown event channel {channel!r}")


class SyntheticStrategyLoad:
    """Publishes per-strategy synthetic events at each channel's real cadence."""

    def __init__(
        self,
        runtime: OperatorInterfaceRuntime,
        *,
        live_count: int = BASELINE_LIVE_COUNT,
        paper_count: int = BASELINE_PAPER_COUNT,
        channels: Iterable[str] = LOAD_CHANNELS,
    ) -> None:
        if live_count < 0 or paper_count < 0:
            raise ValueError("strategy counts must be non-negative")
        if live_count + paper_count < 1:
            raise ValueError("synthetic load needs at least one strategy")
        self._runtime = runtime
        self._channels: tuple[str, ...] = tuple(channels)
        if not self._channels:
            raise ValueError("synthetic load needs at least one channel")
        # Fail fast on a non-periodic / unknown channel before any thread starts,
        # and pin each channel's declared payload shape once.
        self._cadences: dict[str, int] = {c: cadence_for(c) for c in self._channels}
        self._fields: dict[str, tuple[str, ...]] = {c: _payload_fields(c) for c in self._channels}
        self._strategy_ids: tuple[str, ...] = tuple(
            [f"synthetic-live-{n}" for n in range(1, live_count + 1)]
            + [f"synthetic-paper-{n:02d}" for n in range(1, paper_count + 1)]
        )
        self._lock = threading.Lock()
        self._published: dict[str, dict[str, int]] = {
            sid: dict.fromkeys(self._channels, 0) for sid in self._strategy_ids
        }
        self._delivered = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- observability (lock-guarded copies) ----- #

    @property
    def strategy_ids(self) -> tuple[str, ...]:
        return self._strategy_ids

    @property
    def published(self) -> dict[str, dict[str, int]]:
        """Per-strategy, per-channel publish counts (snapshot copy)."""

        with self._lock:
            return {sid: dict(counts) for sid, counts in self._published.items()}

    @property
    def delivered(self) -> int:
        """Total events delivered to subscribed WebSocket sessions."""

        with self._lock:
            return self._delivered

    # ----- lifecycle ----- #

    def start(self) -> None:
        """Register the load channels and start the ticker thread (not re-entrant)."""

        if self._thread is not None:
            raise RuntimeError("load already started; call stop() first")
        for channel in self._channels:
            # Set-add on the runtime: idempotent alongside DashboardPublisher's
            # ownership claim of the same SRS-UI-001 channels.
            self._runtime.register_publisher(channel)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="atp-dashboard-loadgen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the ticker to exit and join it with a bounded timeout."""

        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=MAX_REFRESH_SECONDS + 1)
            self._thread = None

    def publish_once(self) -> int:
        """Publish one event per strategy on every load channel (delivery count)."""

        return sum(self._publish_channel(channel) for channel in self._channels)

    # ----- non-vacuity gate ----- #

    def assert_load_ran(self, min_ticks_per_strategy: int = 2) -> None:
        """Raise unless the load demonstrably ran and was observed.

        Every synthetic strategy must have published ≥ ``min_ticks_per_strategy``
        events on **every** load channel, and ≥1 event must have been delivered
        to a live WebSocket subscriber (the measured client actually received
        load traffic). A silent or unobserved load fails here, never passes.
        """

        published = self.published
        short = [
            f"{sid}:{channel}={count}"
            for sid, counts in published.items()
            for channel, count in counts.items()
            if count < min_ticks_per_strategy
        ]
        if short:
            raise AssertionError(
                f"synthetic load under-published (< {min_ticks_per_strategy} "
                f"ticks): {', '.join(sorted(short)[:8])}"
            )
        if self.delivered < 1:
            raise AssertionError(
                "synthetic load was never delivered to a WebSocket "
                "subscriber — the measured client observed no load traffic"
            )

    # ----- internals ----- #

    def _payload(self, channel: str, strategy_id: str) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field in self._fields[channel]:
            if field == "strategy_id":
                payload[field] = strategy_id
            elif field == "as_of":
                payload[field] = _utc_iso()
            else:
                # Honest synthetic metric: never a fabricated number.
                payload[field] = {"value": None, "data_source": LOAD_DATA_SOURCE}
        return payload

    def _publish_channel(self, channel: str) -> int:
        delivered = 0
        for strategy_id in self._strategy_ids:
            delivered += self._runtime.publish(channel, self._payload(channel, strategy_id))
            with self._lock:
                self._published[strategy_id][channel] += 1
        with self._lock:
            self._delivered += delivered
        return delivered

    def _run(self) -> None:
        # Immediate first tick: every channel is due at start.
        next_fire: dict[str, float] = {c: time.monotonic() for c in self._channels}
        while not self._stop.is_set():
            now = time.monotonic()
            for channel in self._channels:
                if now >= next_fire[channel]:
                    self._publish_channel(channel)
                    next_fire[channel] = now + self._cadences[channel]
            soonest = min(next_fire.values())
            wait = max(0.0, min(soonest - time.monotonic(), _POLL_CEILING_S))
            self._stop.wait(wait)


# --------------------------------------------------------------------------- #
# Operator entrypoint — inspect the dashboard under baseline load manually.
# --------------------------------------------------------------------------- #


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {raw!r}")
    return value


def _non_negative_int(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {raw!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atp_dashboard.loadgen",
        description=(
            "Serve the dashboard under a synthetic NFR-SC1-shaped load "
            "(default 1 live + 30 paper synthetic strategies) for a bounded "
            "duration, then report publish/delivery counts."
        ),
    )
    parser.add_argument("--live", type=_non_negative_int, default=BASELINE_LIVE_COUNT)
    parser.add_argument("--paper", type=_non_negative_int, default=BASELINE_PAPER_COUNT)
    parser.add_argument(
        "--duration", type=_positive_int, default=60, help="seconds to hold the load"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=_non_negative_int, default=0, help="0 = ephemeral (printed)")
    args = parser.parse_args(argv)

    from .server import mount_dashboard  # local import: avoids a module cycle

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    load = SyntheticStrategyLoad(runtime, live_count=args.live, paper_count=args.paper)
    publisher.start()
    load.start()
    host, port = runtime.start(host=args.host, port=args.port)
    print(  # noqa: T201 - operator-facing startup line
        f"atp-dashboard under load ({args.live} live + {args.paper} paper) on "
        f"http://{host}:{port}/dashboard for {args.duration}s"
    )
    try:
        time.sleep(args.duration)
    finally:
        load.stop()
        publisher.stop()
        runtime.stop()

    published = load.published
    per_strategy = [sum(counts.values()) for counts in published.values()]
    print(  # noqa: T201 - operator-facing summary line
        f"published events: total={sum(per_strategy)} "
        f"min/strategy={min(per_strategy)} max/strategy={max(per_strategy)} "
        f"delivered-to-subscribers={load.delivered}"
    )
    try:
        load.assert_load_ran()
    except AssertionError as exc:
        print(f"LOAD DID NOT RUN: {exc}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
