"""L1 unit — the UI-1 critical-alerts provider (pure builder, no I/O).

The pane's honesty contract: while the SRS-NOTIF-001 alert feed is deferred the
snapshot must carry the feed as one explicit deferred cell (never a fabricated
alert, never a bare empty list that could render as "0 active alerts"), and the
per-alert schema must be pinned to the ``ALERTS`` channel / ``GET /api/v1/alerts``
contract so the rendered columns cannot drift.
"""

from __future__ import annotations

import pytest
from atp_api.routes import ROUTES
from atp_dashboard import (
    ALERT_FEED_OWNER,
    ALERT_FIELDS,
    CriticalAlertsProvider,
)
from atp_ws import EVENT_CHANNELS, Channel

pytestmark = pytest.mark.unit


def test_snapshot_is_honest_and_well_formed() -> None:
    snap = CriticalAlertsProvider().alerts_snapshot()
    assert snap["ok"] is True
    assert snap["srs_ref"] == "UI-1"
    # The feed is one explicit deferred cell naming its producer feature.
    assert snap["feed"] == {"value": None, "data_source": f"deferred:{ALERT_FEED_OWNER}"}
    assert ALERT_FEED_OWNER == "SRS-NOTIF-001"
    # No alert events exist (and none may be fabricated) while the feed is deferred.
    assert snap["alerts"] == []


def test_alert_fields_pin_the_ws_channel_contract() -> None:
    """The pane's per-alert schema is exactly the ALERTS channel's declared
    ``payload_fields`` — the cross-surface vocabulary shared with SRS-NOTIF-001."""

    snap = CriticalAlertsProvider().alerts_snapshot()
    assert tuple(snap["alert_fields"]) == ALERT_FIELDS
    alerts_channel = next(c for c in EVENT_CHANNELS if c.name == Channel.ALERTS)
    assert ALERT_FIELDS == alerts_channel.payload_fields


def test_alert_fields_pin_the_rest_contract_route() -> None:
    """...and the ``GET /api/v1/alerts`` contract route's response fields."""

    route = next(r for r in ROUTES if r.path == "/api/v1/alerts")
    assert tuple(CriticalAlertsProvider().alerts_snapshot()["alert_fields"]) == tuple(
        route.response_fields
    )


def test_builder_is_pure_and_repeatable() -> None:
    provider = CriticalAlertsProvider()
    first = provider.alerts_snapshot()
    second = provider.alerts_snapshot()
    # Everything except the wall-clock stamp is deterministic.
    for key in ("ok", "srs_ref", "feed", "alerts", "alert_fields", "severities"):
        assert first[key] == second[key]
