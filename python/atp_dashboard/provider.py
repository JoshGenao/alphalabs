"""Dashboard metric providers (SRS-UI-001).

Assemble the four SRS-UI-001 metric groups into the payloads the dashboard
publishes over the pre-declared :mod:`atp_ws` channels and serves from the
system-snapshot endpoint:

* **live performance** → the ``PNL`` channel,
* **benchmark-relative performance** → the ``METRICS`` channel,
* **system health** → the ``HEARTBEAT`` channel + the readiness snapshot,
* **latency** → the system snapshot (self-measured refresh latency lives on the
  client; pipeline/order percentiles are deferred to SRS-PERF-001).

Honesty (no fabrication)
------------------------
The metric *values* are produced by features still blocked **on** this one —
``SRS-BT-004`` (Sharpe/Sortino/alpha/beta/drawdown), ``SRS-BT-005``
(benchmark-vs-SPY), ``SRS-PERF-001`` (latency percentiles), ``SRS-MD-006`` /
``SRS-MD-007`` (readiness probes, market-data heartbeat). Until those land, each
such field carries ``value=None`` and a ``data_source`` of ``deferred:<owner>``
— never a fabricated number, mirroring the repo's ``Option<f64>`` "None = honestly
undefined" convention. The only real signal today is the readiness snapshot
(:meth:`atp_readiness.ReadinessGate.as_dashboard_payload`).

SRS trace
---------
``SRS-UI-001`` (dashboard metric groups), ``SYS-36`` / ``NFR-P2`` (≤5 s refresh),
``SRS-SEC-002`` (loopback), consuming ``SRS-API-001`` (runtime) and
``atp_readiness`` (system health).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from atp_readiness import ReadinessGate
from atp_ws import MAX_REFRESH_SECONDS, Channel

#: Marker for a field whose live producer is a not-yet-built feature. The value
#: is always ``None`` when this is set — the dashboard renders it as an explicit
#: deferred "—" rather than a number.
DEFERRED = "deferred"
#: Marker for a field carrying a real, currently-produced value.
LIVE = "live"

#: NFR-P2 dashboard-refresh budget (ms). A named constant, not a measurement.
REFRESH_BUDGET_MS = 5_000

#: The three event channels SRS-UI-001 owns (by ``srs_refs`` in atp_ws.channels).
OWNED_CHANNELS: tuple[str, ...] = (Channel.PNL, Channel.METRICS, Channel.HEARTBEAT)

#: The feature that owns each still-deferred metric field's live producer.
FIELD_OWNERS: Mapping[str, str] = {
    # PNL — live P&L from the simulation / live execution engines.
    "daily_pnl": "SRS-BT-004",
    "cumulative_pnl": "SRS-BT-004",
    "unrealized_pnl": "SRS-BT-004",
    # METRICS — risk-adjusted metrics and benchmark-relative return.
    "sharpe": "SRS-BT-004",
    "sortino": "SRS-BT-004",
    "alpha": "SRS-BT-004",
    "beta": "SRS-BT-004",
    "max_drawdown": "SRS-BT-004",
    "benchmark_return": "SRS-BT-005",
    # HEARTBEAT — market-data / broker staleness watcher.
    "feed": "SRS-MD-007",
    "last_tick_at": "SRS-MD-007",
    "staleness_seconds": "SRS-MD-007",
    "is_stale": "SRS-MD-007",
}


def deferred_field(name: str) -> dict[str, object]:
    """Return an honest deferred field descriptor (``value`` is always ``None``)."""

    owner = FIELD_OWNERS.get(name, "unassigned")
    return deferred_field_named(owner)


def deferred_field_named(owner: str) -> dict[str, object]:
    """A deferred field descriptor whose owning feature id is passed explicitly."""

    return {"value": None, "data_source": f"{DEFERRED}:{owner}"}


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@runtime_checkable
class DashboardMetricsProvider(Protocol):
    """Source of the SRS-UI-001 dashboard payloads.

    Implementations must return, for each owned channel, a payload whose keys
    are exactly the channel's declared ``payload_fields`` (so the WS contract and
    the rendered panels never drift), and a system snapshot for the REST poll.
    """

    def channel_payload(self, channel: str) -> dict[str, object]:
        """Payload for one owned channel (``PNL`` / ``METRICS`` / ``HEARTBEAT``)."""
        ...

    def system_snapshot(self) -> dict[str, object]:
        """Health + latency snapshot served at ``GET /dashboard/api/system``."""
        ...


class ReadinessBackedProvider:
    """Provider whose system health is the **real** readiness snapshot.

    Trading metric values remain honestly deferred; the readiness gate is the
    one live signal available today. The gate is constructed once and its
    dashboard payload cached for ``cache_ttl_s`` so a ≤5 s poll does not re-run
    the full config validator on every request.
    """

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        cache_ttl_s: float = 2.0,
    ) -> None:
        self._env = dict(env)
        self._cache_ttl_s = cache_ttl_s
        self._cached_health: dict[str, object] | None = None
        self._cached_at: float = 0.0

    # ----- health (real, cached) ----- #

    def _health(self) -> dict[str, object]:
        now = time.monotonic()
        if self._cached_health is not None and (now - self._cached_at) < self._cache_ttl_s:
            return self._cached_health
        health = self._evaluate_readiness()
        self._cached_health = health
        self._cached_at = now
        return health

    def _evaluate_readiness(self) -> dict[str, object]:
        """Build the health payload from the readiness gate, failing safe.

        A monitoring surface must not crash because config is incomplete: if the
        gate cannot be built/evaluated, report an explicit unavailable health
        state (still a real, honest signal) rather than raising.
        """

        try:
            gate = ReadinessGate.from_env(self._env)
            payload = gate.as_dashboard_payload()
        except Exception as exc:  # noqa: BLE001 - observability must not crash
            return {
                "data_source": LIVE,
                "ok": False,
                "state": "UNAVAILABLE",
                "errors": [f"readiness gate unavailable: {exc}"],
                "warnings": [],
                "evidence": {},
                "overrides": [],
            }
        payload["data_source"] = LIVE
        return payload

    # ----- latency (self-measured on the client; percentiles deferred) ----- #

    def _latency(self) -> dict[str, object]:
        return {
            "refresh_budget_ms": REFRESH_BUDGET_MS,  # NFR-P2, real
            "observed_refresh_ms": {
                # The dashboard measures its own refresh latency client-side; the
                # server cannot observe the browser's render cadence.
                "value": None,
                "data_source": "client-measured",
            },
            "order_signal_to_ack_p95_ms": deferred_field_named("SRS-PERF-001"),
            "pipeline_fanout_p95_ms": deferred_field_named("SRS-PERF-001"),
        }

    # ----- channel payloads ----- #

    def channel_payload(self, channel: str) -> dict[str, object]:
        if channel not in OWNED_CHANNELS:
            raise ValueError(f"provider does not own channel {channel!r}")
        if channel == Channel.PNL:
            payload: dict[str, object] = {"strategy_id": None, "as_of": _utc_iso()}
            for field in ("daily_pnl", "cumulative_pnl", "unrealized_pnl"):
                payload[field] = deferred_field(field)
            return payload
        if channel == Channel.METRICS:
            payload = {"strategy_id": None, "as_of": _utc_iso()}
            for field in ("sharpe", "sortino", "alpha", "beta", "max_drawdown", "benchmark_return"):
                payload[field] = deferred_field(field)
            return payload
        # HEARTBEAT — market-data feed not connected until SRS-MD-007.
        payload = {}
        for field in ("feed", "last_tick_at", "staleness_seconds", "is_stale"):
            payload[field] = deferred_field(field)
        return payload

    def system_snapshot(self) -> dict[str, object]:
        return {
            "generated_at": _utc_iso(),
            "refresh_budget_ms": REFRESH_BUDGET_MS,
            "max_refresh_seconds": MAX_REFRESH_SECONDS,
            "health": self._health(),
            "latency": self._latency(),
            "srs_ref": "SRS-UI-001",
        }
