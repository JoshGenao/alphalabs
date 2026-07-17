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
That feature is not yet delivered: its SMTP/SMS adapters, its detection wiring
(IB connectivity loss, critical failures), and its store-path configuration are
all deferred, so **no honest live alert data exists today**. Crucially, an empty
alert list must NOT render as "0 active alerts" — with detection unwired,
"no alerts observed" is not "no alerts occurring". The pane therefore carries an
explicit ``{"value": None, "data_source": "deferred:SRS-NOTIF-001"}`` feed cell
(the account-panel convention) and the UI renders an "awaiting producer" state,
never an empty-but-reassuring table. When SRS-NOTIF-001 lands this becomes a
provider swap reading the real ``notification_events.store``.

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
``SRS-NOTIF-001`` notification-event seam when it lands.
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

#: The feature that owns the live alert feed: the SRS-NOTIF-001 operator
#: notifier (detection wiring + notification-event store + delivery adapters).
ALERT_FEED_OWNER = "SRS-NOTIF-001"

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
