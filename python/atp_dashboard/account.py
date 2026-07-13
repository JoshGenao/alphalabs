"""Account-level IB status provider (``SRS-UI-003`` / SyRS SYS-43b + SYS-46).

Feeds the dashboard's account-status panel and the ``ACCOUNT_STATUS`` WebSocket
channel: the account-level view SyRS SYS-43b names — total IB account equity,
account P&L (daily and cumulative), margin usage, buying power remaining, and
the IB connection state — "as reported by the IB account".

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
Every one of these fields is produced by the **live IB brokerage account**,
which is reached through the SRS-EXE-006 IB Gateway adapter
(:mod:`atp-adapters`). That adapter's ``account_status()`` defaults to
``not_configured``; its only real implementation is behind the non-default
``ib-live-transport`` cargo feature over a live socket (operator-initiated per
SyRS SYS-2e, fixed port 4002) and cannot run in the parallel agent pool. The
readiness gate carries no broker-connection signal either (its IB-connectivity
probe is itself deferred to SRS-MD-006). There is therefore **no** solo-runnable
account data source today, so every field is carried as an explicit
``{"value": None, "data_source": "deferred:SRS-EXE-006"}`` cell — never a
fabricated number — mirroring the repo's ``Option<f64>`` "None = honestly
undefined" convention. When the live adapter lands this becomes a provider-cell
swap (the panel + WS payload shape + REST route already match the contract).

A monitoring surface must not crash: the provider is a pure builder (no I/O, no
subprocess), so it always returns a well-formed, honest payload.

SRS trace
---------
``SRS-UI-003`` (account panel), SyRS ``SYS-43b`` (account-level dashboard view),
``SYS-46`` (IB connectivity-loss context), ``NFR-P2`` (the ACCOUNT_STATUS
channel's ≤5 s cadence), consuming the SRS-EXE-006 IB account seam when it lands.
"""

from __future__ import annotations

import time

from atp_ws import Channel

from .provider import deferred_field_named

__all__ = [
    "ACCOUNT_CHANNEL",
    "ACCOUNT_FIELD_OWNERS",
    "ACCOUNT_STATUS_FIELDS",
    "AccountStatusProvider",
]

#: The feature that owns every account field's live producer: the SRS-EXE-006 IB
#: Gateway adapter (the account view is "as reported by the IB account").
_ACCOUNT_OWNER = "SRS-EXE-006"

#: The six account fields the panel renders (the ``ACCOUNT_STATUS`` channel's
#: ``payload_fields`` minus the real ``as_of`` timestamp).
ACCOUNT_STATUS_FIELDS: tuple[str, ...] = (
    "equity",
    "daily_pnl",
    "cumulative_pnl",
    "margin_usage",
    "buying_power",
    "ib_connection_state",
)

#: The feature that owns each still-deferred account field's live producer.
ACCOUNT_FIELD_OWNERS: dict[str, str] = {field: _ACCOUNT_OWNER for field in ACCOUNT_STATUS_FIELDS}


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class AccountStatusProvider:
    """Assembles the SRS-UI-003 account-status payloads.

    Deliberately **not** a :class:`atp_dashboard.provider.DashboardMetricsProvider`
    / ``ReadinessBackedProvider`` (those own the SRS-UI-001 PNL/METRICS/HEARTBEAT
    channels and reject ``ACCOUNT_STATUS``). This is a composition-time opt-in
    source, mounted like the SRS-UI-002 inventory, so a bare SRS-UI-001 dashboard
    never claims the account channel or serves the account route.
    """

    def _account_fields(self) -> dict[str, object]:
        """The six deferred account cells (``value`` is always ``None``)."""

        return {field: deferred_field_named(owner) for field, owner in ACCOUNT_FIELD_OWNERS.items()}

    def account_status_events(self) -> list[dict[str, object]]:
        """The ACCOUNT_STATUS events for one publish tick.

        One event whose keys are exactly the channel's declared ``payload_fields``
        (so the WS contract and the rendered panel never drift): a real ``as_of``
        stamp plus the six honest deferred cells. The single-event shape (no
        summary/rows split) reflects that the account view is one account, not a
        per-strategy collection.
        """

        event: dict[str, object] = {"as_of": _utc_iso()}
        event.update(self._account_fields())
        return [event]

    def account_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/account`` (first paint).

        Always ``ok: True`` — the builder cannot fail — with every account field an
        explicit deferred cell. An honest, well-formed snapshot, never a fabricated
        balance nor an empty masquerade.
        """

        snapshot: dict[str, object] = {
            "generated_at": _utc_iso(),
            "ok": True,
            "as_of": _utc_iso(),
            "srs_ref": "SRS-UI-003",
        }
        snapshot.update(self._account_fields())
        return snapshot


#: The channel this provider publishes (kept next to the provider so the
#: publisher and the safety test share one authority).
ACCOUNT_CHANNEL: str = Channel.ACCOUNT_STATUS
