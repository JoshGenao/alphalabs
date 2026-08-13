"""Hot-Swap controls + status pane provider (``UI-5`` / SyRS SYS-49a..e).

Feeds the dashboard's *Hot-Swap — Changeover Console* panel: the status half of
UI-5 ("User can trigger manual promotion, inspect demotion-pending state, view
cool-down expiry, and see automatic-trigger configuration"), tracing
``SRS-RESV-003`` (manual + configurable automatic triggers), ``SRS-RESV-004``
(demote-before-promote, demotion-pending on timeout), ``SRS-RESV-005`` (promote
only after successful demotion) and ``SRS-RESV-006`` (cool-down). The *manual
promotion* half is the confirm-then-POST affordance in ``app.js`` that targets
the contract route ``POST /api/v1/hot-swap`` — this module adds no mutating
surface, only a read.

Where the facts come from
-------------------------
Every live Hot-Swap fact this pane displays has a DEFERRED producer today, so
the pane renders all of them as honest ``{"value": None, "data_source":
"deferred:<owner>"}`` cells and never fabricates one:

* the current live strategy + successful-promotion state — ``SRS-RESV-005``;
* the demotion-pending state (the SYS-49b liquidation-timeout outcome) —
  ``SRS-RESV-004``;
* the cool-down window (start / expiry / in-effect) — ``SRS-RESV-006``;
* the per-trigger automatic enable state — ``SRS-RESV-003`` (the trigger
  DECISION layer is built in Rust against injected ports, but nothing persists
  a queryable enabled-state yet);
* the promotion candidate (the Reservoir's top-ranked paper strategy) —
  ``SRS-RESV-002`` (the SYS-48 ranking engine). This cell GATES the manual
  promotion control: with no candidate the control is inert (a dashboard must
  never present an actionable promote with nothing to promote).

The *trigger catalog* and the SYS-49 defaults (:data:`TRIGGER_CATALOG`,
:data:`COOLDOWN_DAYS_DEFAULT`, :data:`DEMOTION_TIMEOUT_SECONDS_DEFAULT`) are
REAL constant data — they describe the control surface (which triggers exist,
that automatic triggers default to disabled, the SYS-49b/e default windows), not
a live result. They are surfaced so the panel's chips and cool-down dial are
driven by the contract rather than hard-coded in the browser (mirroring the
SRS-UI-003 Reservoir selector config). When the RESV producers land this becomes
a provider-cell swap; the ``HotSwapStatusSource`` protocol below is that seam.

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
Hot-Swap moves REAL money between a paper strategy and the single live slot, so
every rule here fails **closed**:

* Unknown is a deferred cell (``value: None`` + ``deferred:<owner>``) — never a
  green, never a blank that reads as "fine".
* ``demotion_pending`` / the cool-down ``in_effect`` flag are tri-state
  (true / false / **None**): ``None`` whenever the truth cannot be established;
  a genuine ``False`` is a loud fact, not a blank.
* A strategy id / timestamp is honoured only when it is strictly a non-empty
  ``str``; a bool/number/other is UNKNOWN.
* The changeover-sequence rungs (stop-signals → cancel → liquidate → flat /
  demotion-pending fork → promote) are resolved ONLY when a source substantiates
  them; otherwise each is an explicit deferred cell the pane draws hatched.
* ``promotion_candidate`` is a deferred cell (``value: None``), NOT an empty
  string — an empty candidate would masquerade as "a candidate exists".

The provider is fail-safe: with no source (the state today — no RESV producer
persists anything) it returns a well-formed all-deferred snapshot with
``ok: True`` (nothing is broken; the producers are simply unbuilt). A wired
source that cannot be read yields ``ok: False`` + the verbatim reason — never a
crash, never a fabricated fact. The three source legs fail independently.

There is no Hot-Swap WebSocket channel (``atp_ws.channels.Channel`` declares
none), so this pane is REST-poll-only: publishing on a channel the AsyncAPI
contract does not declare would be fabrication at the transport layer.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from atp_hotswap import (
    CliHotSwapDemotionSource,
    CliHotSwapPromotionSource,
    CliHotSwapTriggerSource,
    CompositeHotSwapStatusSource,
    HotSwapStatusUnavailable,
    HotSwapTriggerCliRunner,
    HotSwapTriggerCliUnavailable,
    HotSwapTriggerOutputUnreadable,
    parse_trigger_cli_output,
    strict_trigger_bool,
)

from .provider import DEFERRED

__all__ = [
    "CHANGEOVER_SEQUENCE",
    "CliHotSwapDemotionSource",
    "CliHotSwapPromotionSource",
    "CliHotSwapTriggerSource",
    "CompositeHotSwapStatusSource",
    "COOLDOWN_DAYS_DEFAULT",
    "DEMOTION_TIMEOUT_SECONDS_DEFAULT",
    "HOT_SWAP_CANDIDATE_OWNER",
    "HOT_SWAP_COOLDOWN_OWNER",
    "HOT_SWAP_DEMOTION_OWNER",
    "HOT_SWAP_PROMOTION_OWNER",
    "HOT_SWAP_TRIGGER_OWNER",
    "TRIGGER_CATALOG",
    "HotSwapStatusProvider",
    "HotSwapStatusSource",
    "HotSwapStatusUnavailable",
    "HotSwapTriggerCliRunner",
    "HotSwapTriggerCliUnavailable",
    "HotSwapTriggerOutputUnreadable",
    "parse_trigger_cli_output",
    "strict_trigger_bool",
]

#: Owner of the automatic-trigger enabled-state (the SYS-49a trigger DECISION +
#: CONFIGURATION layer). Its Rust core exists against injected ports; no queryable
#: durable enabled-state does, so the live per-trigger flags render deferred here.
HOT_SWAP_TRIGGER_OWNER = "SRS-RESV-003"

#: Owner of the demotion sequence + the SYS-49b demotion-pending timeout state.
HOT_SWAP_DEMOTION_OWNER = "SRS-RESV-004"

#: Owner of the successful-promotion / current-live-strategy fact (SYS-49d).
HOT_SWAP_PROMOTION_OWNER = "SRS-RESV-005"

#: Owner of the SYS-49e cool-down window (start / expiry / in-effect).
HOT_SWAP_COOLDOWN_OWNER = "SRS-RESV-006"

#: Owner of the promotion candidate — the Reservoir's top-ranked paper strategy
#: (the SYS-48 ranking engine). Gates the manual-promotion control.
HOT_SWAP_CANDIDATE_OWNER = "SRS-RESV-002"

#: SYS-49e: automatic triggers are ignored for this many calendar days after a
#: successful swap. REAL constant (a control default), not a live result.
COOLDOWN_DAYS_DEFAULT = 7

#: SYS-49b: demotion waits this many seconds for flat confirmation before it
#: enters the demotion-pending state. REAL constant.
DEMOTION_TIMEOUT_SECONDS_DEFAULT = 60

#: SYS-49a: the three automatic Hot-Swap triggers, each enable/disable-able per
#: type and DEFAULTING TO DISABLED. A typed tuple (not a bare ``object`` inside
#: the catalog) so the kinds can be derived without an unchecked iteration.
_AUTOMATIC_TRIGGERS: tuple[dict[str, object], ...] = (
    {"kind": "drawdown_demotion", "label": "Drawdown demotion", "default_enabled": False},
    {"kind": "top_ranked_promotion", "label": "Top-ranked promotion", "default_enabled": False},
    {
        "kind": "highest_momentum_promotion",
        "label": "Highest-momentum promotion",
        "default_enabled": False,
    },
)

#: SYS-49a: the Hot-Swap trigger catalog. Manual promotion is ALWAYS available;
#: the automatic triggers default to disabled. REAL schema (which controls exist
#: + their defaults), never a live enabled-state — that is a deferred cell
#: (``auto_triggers_live`` below).
TRIGGER_CATALOG: dict[str, object] = {
    "manual": {
        "kind": "manual",
        "label": "Manual promotion",
        "automatic": False,
        "default_enabled": True,
    },
    "automatic": _AUTOMATIC_TRIGGERS,
    #: SYS-49a: automatic triggers default to disabled.
    "automatic_default": "disabled",
}

#: The kinds of the three automatic triggers, in catalog order — the per-trigger
#: live enabled-state cells mirror this tuple.
_AUTOMATIC_KINDS: tuple[str, ...] = tuple(str(trigger["kind"]) for trigger in _AUTOMATIC_TRIGGERS)

#: The rendered changeover ladder: the SYS-49b demotion order (stop new signals →
#: cancel resting orders → liquidate to flat) with the demotion-pending timeout
#: branching off it, then the SYS-49d promotion. ``stage`` splits the DEMOTE side
#: from the PROMOTE side; ``branch`` marks the SYS-49b timeout fork. The JS mirror
#: (``HS_PHASES`` in app.js) must match this order + these phases exactly; a
#: payload that disagrees is drift and is refused wholesale.
CHANGEOVER_SEQUENCE: tuple[dict[str, object], ...] = (
    {
        "phase": "demote_signals",
        "label": "STOP NEW SIGNALS",
        "stage": "demote",
        "branch": False,
        "owner": HOT_SWAP_DEMOTION_OWNER,
    },
    {
        "phase": "demote_cancel",
        "label": "CANCEL RESTING ORDERS",
        "stage": "demote",
        "branch": False,
        "owner": HOT_SWAP_DEMOTION_OWNER,
    },
    {
        "phase": "demote_liquidate",
        "label": "LIQUIDATE TO FLAT",
        "stage": "demote",
        "branch": False,
        "owner": HOT_SWAP_DEMOTION_OWNER,
    },
    {
        "phase": "demotion_pending",
        "label": "DEMOTION-PENDING (TIMEOUT)",
        "stage": "demote",
        "branch": True,
        "owner": HOT_SWAP_DEMOTION_OWNER,
    },
    {
        "phase": "promote",
        "label": "PROMOTE CANDIDATE LIVE",
        "stage": "promote",
        "branch": False,
        "owner": HOT_SWAP_PROMOTION_OWNER,
    },
)

#: Status vocabulary for a changeover rung. ``UNKNOWN`` is the fail-closed
#: default; the rest describe an observed demotion/promotion phase.
STATUS_UNKNOWN = "UNKNOWN"
STATUS_PENDING = "PENDING"
STATUS_DONE = "DONE"
STATUS_BLOCKED = "BLOCKED"
STATUS_FAILED = "FAILED"

_RUNG_STATUSES = frozenset({STATUS_PENDING, STATUS_DONE, STATUS_BLOCKED, STATUS_FAILED})

#: Data-source label for a resolved cell — which durable artefact the value was
#: read from. There is no such artefact today; a future ``HotSwapStatusSource``
#: (RESV-004/005/006 + the SRS-LOG-001 HOT_SWAP records) supplies it.
SOURCE_HOT_SWAP_STATE = "hot_swap_state"


@runtime_checkable
class HotSwapStatusSource(Protocol):
    """The durable Hot-Swap facts the pane reads — the FLIP SEAM.

    No concrete implementation ships today (no RESV producer persists anything
    queryable). When ``SRS-RESV-004``/``005``/``006`` land the durable swap
    record + the ``SRS-LOG-001`` ``HOT_SWAP`` events, and ``SRS-RESV-003`` a
    durable trigger config, a concrete source implements this protocol and is
    passed to :class:`HotSwapStatusProvider`; every deferred cell below then
    swaps to a real value with no change to the pane. The three legs fail
    independently: an unreadable trigger config must not blank an otherwise
    readable live state (and vice versa), so each raises
    :class:`HotSwapStatusUnavailable` on its own.
    """

    def live_state(self) -> Mapping[str, object] | None:
        """The current live strategy + demotion-pending + cool-down state
        (``SRS-RESV-004``/``005``/``006``), or ``None`` when no swap history
        exists yet. Raises :class:`HotSwapStatusUnavailable` when unreadable."""
        ...

    def trigger_config(self) -> Mapping[str, object] | None:
        """The persisted automatic-trigger enabled-state (``SRS-RESV-003``), or
        ``None`` when unconfigured. Raises :class:`HotSwapStatusUnavailable`
        when unreadable."""
        ...

    def promotion_candidate(self) -> Mapping[str, object] | None:
        """The Reservoir's top-ranked promotion candidate (``SRS-RESV-002``), or
        ``None`` when the ranking engine names none. Raises
        :class:`HotSwapStatusUnavailable` when unreadable."""
        ...


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _deferred(owner: str) -> dict[str, object]:
    return {"value": None, "data_source": f"{DEFERRED}:{owner}"}


def _resolved(value: object) -> dict[str, object]:
    return {"value": value, "data_source": SOURCE_HOT_SWAP_STATE}


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _normalize_leg(
    value: Mapping[str, object] | None, name: str, errors: list[str]
) -> Mapping[str, object] | None:
    """A source leg must be a mapping or ``None``. A non-mapping return (version
    skew / corrupt state) is recorded as an ``ok:false`` reason and dropped to
    ``None`` (the leg renders deferred) — never passed on to ``.get()`` to crash."""

    if value is None or isinstance(value, Mapping):
        return value
    errors.append(
        f"hot-swap {name} returned a non-mapping value ({type(value).__name__}) — "
        "refusing to render it; leg deferred"
    )
    return None


def _strict_bool(value: object) -> bool | None:
    """Tri-state read of a source boolean: ``None`` unless strictly a ``bool``.
    A truthy string or a missing key is UNKNOWN, never ``True``."""

    return value if isinstance(value, bool) else None


def _strict_str(value: object) -> str | None:
    """A non-empty string, or ``None``. A blank/other is UNKNOWN — never coerced
    to an empty value that would read as "present"."""

    return value if isinstance(value, str) and value.strip() else None


def _bool_cell(state: Mapping[str, object] | None, key: str, owner: str) -> dict[str, object]:
    """A tri-state boolean cell: resolved only when the source strictly carries a
    ``bool``, otherwise deferred (``value: None``)."""

    if state is None:
        return _deferred(owner)
    value = _strict_bool(state.get(key))
    return _resolved(value) if value is not None else _deferred(owner)


def _str_cell(state: Mapping[str, object] | None, key: str, owner: str) -> dict[str, object]:
    """A string cell: resolved only for a non-empty ``str``, else deferred."""

    if state is None:
        return _deferred(owner)
    value = _strict_str(state.get(key))
    return _resolved(value) if value is not None else _deferred(owner)


class HotSwapStatusProvider:
    """Assembles the UI-5 Hot-Swap controls + status payload.

    A composition-time opt-in source (like the SRS-UI-003 Reservoir provider), so
    a bare SRS-UI-001 dashboard neither serves the route nor implies a pane.
    Constructed with ``source=None`` (the state today: no RESV producer persists
    a queryable fact) it returns a well-formed all-deferred snapshot — every
    live cell an explicit deferred placeholder, never a fabricated swap state.
    """

    def __init__(self, source: HotSwapStatusSource | None = None) -> None:
        self._source = source

    def hot_swap_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/hot-swap``."""

        live: Mapping[str, object] | None = None
        triggers: Mapping[str, object] | None = None
        candidate: Mapping[str, object] | None = None
        errors: list[str] = []

        if self._source is not None:
            try:
                live = self._source.live_state()
            except HotSwapStatusUnavailable as unavailable:
                errors.append(str(unavailable))
            try:
                triggers = self._source.trigger_config()
            except HotSwapStatusUnavailable as unavailable:
                errors.append(str(unavailable))
            try:
                candidate = self._source.promotion_candidate()
            except HotSwapStatusUnavailable as unavailable:
                errors.append(str(unavailable))

        # Normalize each source return to a Mapping or None BEFORE any .get(): a
        # wired source that returns a malformed non-mapping value (version skew /
        # corrupt state) is a leg-specific error, not a crash — the pane must still
        # emit its fail-closed ok:false snapshot with that leg deferred.
        live = _normalize_leg(live, "live_state", errors)
        triggers = _normalize_leg(triggers, "trigger_config", errors)
        candidate = _normalize_leg(candidate, "promotion_candidate", errors)

        cooldown = _mapping(live.get("cooldown")) if live is not None else None

        return {
            "generated_at": _utc_iso(),
            "srs_ref": "UI-5",
            # ok is True for a well-formed all-deferred pane (producers unbuilt is
            # not a fault); False only when a WIRED source could not be read.
            "ok": not errors,
            "errors": errors or None,
            # ---- REAL static SYS-49 control schema (never a live result) ---- #
            "trigger_catalog": TRIGGER_CATALOG,
            "cooldown_days_default": COOLDOWN_DAYS_DEFAULT,
            "demotion_timeout_seconds_default": DEMOTION_TIMEOUT_SECONDS_DEFAULT,
            # ---- DEFERRED live facts (each fails closed to value: None) ----- #
            "current_live_strategy_id": _str_cell(
                live, "current_live_strategy_id", HOT_SWAP_PROMOTION_OWNER
            ),
            # Gates the manual-promotion control: value None -> control inert.
            "promotion_candidate": _str_cell(
                candidate, "candidate_strategy_id", HOT_SWAP_CANDIDATE_OWNER
            ),
            # Tri-state: value true/false/None. None whenever the truth is unknown.
            "demotion_pending": _bool_cell(live, "demotion_pending", HOT_SWAP_DEMOTION_OWNER),
            "demotion_detail": _str_cell(live, "demotion_detail", HOT_SWAP_DEMOTION_OWNER),
            "cooldown": {
                "in_effect": _bool_cell(cooldown, "in_effect", HOT_SWAP_COOLDOWN_OWNER),
                "started_at": _str_cell(cooldown, "started_at", HOT_SWAP_COOLDOWN_OWNER),
                "expires_at": _str_cell(cooldown, "expires_at", HOT_SWAP_COOLDOWN_OWNER),
            },
            # The SYS-49a aggregate the contract's /hot-swap/status route names,
            # plus the per-trigger detail the chips render. Both deferred.
            "auto_triggers_enabled": _bool_cell(triggers, "any_enabled", HOT_SWAP_TRIGGER_OWNER),
            "auto_triggers_live": [
                {
                    "kind": kind,
                    "enabled": _bool_cell(
                        _mapping(triggers.get(kind)) if triggers is not None else None,
                        "enabled",
                        HOT_SWAP_TRIGGER_OWNER,
                    ),
                }
                for kind in _AUTOMATIC_KINDS
            ],
            "changeover_sequence": [self._rung(spec, live) for spec in CHANGEOVER_SEQUENCE],
        }

    # -------------------------------------------------------------- rungs --- #

    @staticmethod
    def _rung(spec: Mapping[str, object], live: Mapping[str, object] | None) -> dict[str, object]:
        """One rung of the changeover ladder.

        Resolved ONLY when a source substantiates this phase with a status in the
        closed vocabulary; otherwise an explicit deferred cell (``value: None``)
        naming the owner, which the pane draws hatched rather than resolved.
        """

        phase = str(spec["phase"])
        owner = str(spec["owner"])
        cell: dict[str, object] = {
            "phase": phase,
            "label": spec["label"],
            "stage": spec["stage"],
            "branch": spec["branch"],
            "owner": owner,
            "status": STATUS_UNKNOWN,
            "detail": "",
        }
        rungs = _mapping(live.get("sequence")) if live is not None else None
        observed = _mapping(rungs.get(phase)) if rungs is not None else None
        status = observed.get("status") if observed is not None else None
        if isinstance(status, str) and status in _RUNG_STATUSES:
            cell["status"] = status
            cell["value"] = status
            cell["data_source"] = SOURCE_HOT_SWAP_STATE
            detail = observed.get("detail") if observed is not None else None
            cell["detail"] = detail if isinstance(detail, str) else ""
        else:
            cell.update(_deferred(owner))
        return cell
