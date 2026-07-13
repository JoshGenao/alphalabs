"""Reservoir ranking overview provider (``SRS-UI-003`` / SyRS SYS-48).

Feeds the dashboard's Reservoir-overview panel and the ``RESERVOIR_RANKING``
WebSocket channel: the SyRS SYS-43b "Reservoir overview showing all paper
strategies' simulated performance with current ranking and momentum scores",
ranked over the SYS-48 shared evaluation window (selectable from 1, 7, 15, 30,
60, or 90 calendar days, defaulting to 30) using risk-adjusted return metrics
(Sharpe / Sortino) plus a momentum score.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
The ranking *output* — the ordered strategies and their Sharpe / Sortino /
momentum scores — is produced by the SYS-48 ranking engine ``SRS-RESV-002``,
which is unbuilt (the ``RESERVOIR_RANKING`` WS channel and the
``GET /api/v1/reservoir/ranking`` REST route are schema-only; there is no
ranking CLI or store). Every ranking-result field is therefore carried as an
explicit ``{"value": None, "data_source": "deferred:SRS-RESV-002"}`` cell —
never fabricated. In particular ``rankings`` is a deferred cell (``value`` is
``None``), **not** an empty list, because ``[]`` would masquerade as "ranked,
zero strategies" (the inventory "absent ≠ empty" rule).

The evaluation-window *selector configuration* (:data:`ALLOWED_EVAL_WINDOWS` /
:data:`DEFAULT_EVAL_WINDOW`) is real SYS-48 constant data — it describes a UI
*input*, not a ranking *result*, and is surfaced on the REST snapshot so the
panel's window control is driven by the contract rather than hard-coded in the
browser (mirroring UI-3's date-picker defaults). When the ranking engine lands
this becomes a provider-cell swap; the selected window will then drive the
contract route ``GET /api/v1/reservoir/ranking``.

SRS trace
---------
``SRS-UI-003`` (Reservoir panel), SyRS ``SYS-48`` (ranking + momentum over a
shared configurable window), ``SRS-RESV-002`` (the ranking engine), ``NFR-P2``
(the RESERVOIR_RANKING channel's ≤5 s cadence).
"""

from __future__ import annotations

import time

from atp_ws import Channel

from .provider import deferred_field_named

__all__ = [
    "ALLOWED_EVAL_WINDOWS",
    "DEFAULT_EVAL_WINDOW",
    "RESERVOIR_CHANNEL",
    "RESERVOIR_FIELD_OWNERS",
    "RESERVOIR_RANKING_FIELDS",
    "ReservoirRankingProvider",
]

#: The feature that owns the ranking output (SYS-48 ranking engine).
_RANKING_OWNER = "SRS-RESV-002"

#: SYS-48: the shared evaluation window is selectable from these calendar-day
#: values. REAL constants (a UI input), not a ranking result.
ALLOWED_EVAL_WINDOWS: tuple[int, ...] = (1, 7, 15, 30, 60, 90)

#: SYS-48: the evaluation window defaults to 30 calendar days.
DEFAULT_EVAL_WINDOW: int = 30

#: The five ranking-result fields (the ``RESERVOIR_RANKING`` channel's
#: ``payload_fields`` minus the real ``as_of`` timestamp). All are deferred to
#: the ranking engine — including ``eval_window_days`` (the window the engine
#: *actually ranked over*, distinct from the UI selector's configured value).
RESERVOIR_RANKING_FIELDS: tuple[str, ...] = (
    "eval_window_days",
    "rankings",
    "sharpe",
    "sortino",
    "momentum_score",
)

#: The feature that owns each still-deferred ranking field's live producer.
RESERVOIR_FIELD_OWNERS: dict[str, str] = {
    field: _RANKING_OWNER for field in RESERVOIR_RANKING_FIELDS
}


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ReservoirRankingProvider:
    """Assembles the SRS-UI-003 Reservoir-ranking payloads.

    A composition-time opt-in source (like the SRS-UI-002 inventory), so a bare
    SRS-UI-001 dashboard never claims the ranking channel or serves the reservoir
    route. Pure builder: no I/O, no subprocess — always returns a well-formed,
    honest payload with every ranking field deferred to the SYS-48 engine.
    """

    def _ranking_fields(self) -> dict[str, object]:
        """The five deferred ranking cells (``value`` is always ``None``;
        ``rankings`` is a deferred cell, never ``[]``)."""

        return {
            field: deferred_field_named(owner) for field, owner in RESERVOIR_FIELD_OWNERS.items()
        }

    def reservoir_ranking_events(self) -> list[dict[str, object]]:
        """The RESERVOIR_RANKING events for one publish tick.

        One event whose keys are exactly the channel's declared ``payload_fields``
        (so the WS contract and the rendered panel never drift): a real ``as_of``
        stamp plus the five honest deferred cells.
        """

        event: dict[str, object] = {"as_of": _utc_iso()}
        event.update(self._ranking_fields())
        return [event]

    def reservoir_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/reservoir`` (first paint).

        Carries the five deferred ranking cells **plus** the real SYS-48 selector
        configuration (``allowed_windows`` / ``default_window``) that drives the
        panel's evaluation-window control. Always ``ok: True`` — the builder cannot
        fail — never a fabricated ranking.
        """

        snapshot: dict[str, object] = {
            "generated_at": _utc_iso(),
            "ok": True,
            "as_of": _utc_iso(),
            # Real SYS-48 selector config (a UI input, kept clearly separate from
            # the deferred ranking results below).
            "allowed_windows": list(ALLOWED_EVAL_WINDOWS),
            "default_window": DEFAULT_EVAL_WINDOW,
            "srs_ref": "SRS-UI-003",
        }
        snapshot.update(self._ranking_fields())
        return snapshot


#: The channel this provider publishes (kept next to the provider so the
#: publisher and the safety test share one authority).
RESERVOIR_CHANNEL: str = Channel.RESERVOIR_RANKING
