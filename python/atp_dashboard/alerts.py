"""Critical-alerts pane provider (``UI-1`` / SyRS SYS-46 + SYS-58).

Feeds the dashboard's critical-alerts pane: the "active critical alerts" leg of
the UI-1 primary operations view. The alert vocabulary is the stable
cross-surface contract already declared for the ``ALERTS`` WebSocket channel
(:mod:`atp_ws.channels`) and the ``GET /api/v1/alerts`` REST route
(:mod:`atp_api.routes`): ``alert_id``, ``raised_at``, ``severity``, ``channel``,
``delivery_status``, ``acknowledged``.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
Alert events are produced by the SRS-NOTIF-001 operator notifier
(``crates/atp-notification``: ``OperatorNotifier`` + ``NotificationEventStore``).
That feature CLOSED on 2026-09-01 — adapters, detection wiring and the durable
store all landed and delivered to a real operator. What is still missing is
**this pane reading that store**: nothing wires ``notification_events.store``
into the dashboard, so no honest live alert data reaches it today. Crucially, an empty
alert list must NOT render as "0 active alerts" — with detection unwired,
"no alerts observed" is not "no alerts occurring". The pane therefore carries an
explicit ``{"value": None, "data_source": "deferred:UI-1"}`` feed cell
(the account-panel convention) and the UI renders an "awaiting producer" state,
never an empty-but-reassuring table. The remaining work is exactly that provider
swap: read the real ``notification_events.store``, which now exists and carries
real events.

No ``ALERTS`` WebSocket publishing happens here either: that channel's declared
payload is per-alert events (``alert_id``, …); publishing deferred non-events
would drift the AsyncAPI contract. The pane is REST-poll-only until the real
producer lands.

A monitoring surface must not crash: the provider is a pure builder (no I/O, no
subprocess), so it always returns a well-formed, honest payload.

SRS trace
---------
``UI-1`` (primary operations view: critical alerts), SyRS ``SYS-46`` (operator
notification), ``SYS-58`` (resource threshold alerts), consuming the
``SRS-NOTIF-001`` notification-event store, which exists and holds real events.
"""

from __future__ import annotations

import time

from .provider import deferred_field_named

__all__ = [
    "ALERT_FEED_OWNER",
    "ALERT_FIELDS",
    "ALERT_SEVERITIES",
    "CriticalAlertsProvider",
]

#: The feature that owns the live alert feed.
#:
#: WAS "SRS-NOTIF-001" until that feature closed on 2026-09-01. Its dispatcher,
#: both transports, the detection wiring and the durable store all landed and
#: were proven end to end, so naming it here would point every "awaiting"
#: state at a DONE feature — a contradiction the operator would read on the
#: dashboard itself. What is genuinely missing is this pane READING the store
#: that already exists (`notification_events.store`), which is UI-1's own live
#: feed. The remaining work is a provider swap, not a producer build.
ALERT_FEED_OWNER = "UI-1"

#: The six per-alert fields the pane renders — exactly the ``ALERTS`` channel's
#: declared ``payload_fields`` (and the ``GET /api/v1/alerts`` response fields),
#: so the pane, the WS contract, and the REST contract never drift.
ALERT_FIELDS: tuple[str, ...] = (
    "alert_id",
    "raised_at",
    "severity",
    "channel",
    "delivery_status",
    "acknowledged",
)

#: The severity vocabulary of the SRS-NOTIF-001 trigger set (``event.rs``):
#: ``CRITICAL_FAILURE`` -> CRITICAL, ``IB_CONNECTIVITY_LOSS`` -> ERROR.
ALERT_SEVERITIES: tuple[str, ...] = ("CRITICAL", "ERROR")


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class CriticalAlertsProvider:
    """Assembles the UI-1 critical-alerts pane payloads.

    Deliberately **not** a :class:`atp_dashboard.provider.DashboardMetricsProvider`
    / ``ReadinessBackedProvider`` (those own the SRS-UI-001 PNL/METRICS/HEARTBEAT
    channels). This is a composition-time opt-in source, mounted like the
    SRS-UI-003 account provider, so a bare SRS-UI-001 dashboard never serves the
    alerts route.
    """

    def alerts_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/alerts``.

        Always ``ok: True`` — the builder cannot fail — with the alert *feed*
        carried as one explicit deferred cell naming its producer. ``alerts``
        is ``None`` (unknown), NOT ``[]``: an empty list at the JSON boundary
        would be all-clear-shaped, and a caller keying off ``ok`` + ``alerts``
        would read unknown alert state as "zero active alerts". Only a live
        SRS-NOTIF-001 feed may emit a list here. ``alert_fields`` pins the
        per-alert schema the real feed will use, so the rendered columns cannot
        drift from the contract.
        """

        return {
            "generated_at": _utc_iso(),
            "ok": True,
            "srs_ref": "UI-1",
            "feed": deferred_field_named(ALERT_FEED_OWNER),
            "alerts": None,
            "alert_fields": list(ALERT_FIELDS),
            "severities": list(ALERT_SEVERITIES),
        }
