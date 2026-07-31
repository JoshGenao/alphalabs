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

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from .provider import DEFERRED

__all__ = [
    "CHANGEOVER_SEQUENCE",
    "CliHotSwapTriggerSource",
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


class HotSwapStatusUnavailable(Exception):
    """A Hot-Swap status source cannot be read right now.

    Reported to the operator verbatim — never swallowed into a clean-looking
    snapshot, and never treated as "no swap in progress".
    """


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


# --------------------------------------------------------------------------- #
# The concrete SRS-RESV-003 trigger-configuration source
#
# Lives here, beside the Protocol it implements, for the reason the earlier draft of it
# found the hard way: the pane degrades on `HotSwapStatusUnavailable`, so a source that
# raises a same-named class from another module sails straight past
# `HotSwapStatusProvider`'s except-clause and 500s the whole snapshot route instead of
# rendering the honest error state. Same placement as `CliHeartbeatSource`
# (`.heartbeat`) and `RollbackSnapshotInventorySource` (`.inventory`): the dashboard owns
# its sources and shells the operator binaries itself, and the dependency runs one way
# (atp_orchestration imports this for the REST arm; nothing here imports it back).
# --------------------------------------------------------------------------- #


class HotSwapTriggerOutputUnreadable(HotSwapStatusUnavailable):
    """The trigger CLI answered, but not in a shape this reader can trust.

    A subclass so the pane's blanket `except HotSwapStatusUnavailable` still degrades
    honestly, while the REST arm can tell an unparseable answer apart from an unreadable
    file and name the right cause.
    """


#: Default location of the cargo-built operator binary, relative to the repo root
#: (python/atp_dashboard/hotswap.py -> parents[2] == repo root). Build it with
#: ``cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli``.
_DEFAULT_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv003_hot_swap_trigger_cli"
)

#: Per-invocation subprocess budget (seconds): a wedged binary surfaces an explicit
#: unavailable state, never an indefinite hang of the operator surface.
_DEFAULT_TIMEOUT_S = 30.0


class HotSwapTriggerCliRunner(Protocol):
    """The subprocess surface this module depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the trigger CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"hot-swap trigger binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def parse_trigger_cli_output(stdout: str) -> dict[str, str]:
    """Parse the bin's deterministic ``key:value`` proof lines."""

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key] = value
    return values


def strict_trigger_bool(values: dict[str, str], key: str) -> bool:
    """Read a boolean proof line, refusing anything that is not literally ``true``/``false``.

    A missing or misspelled key must not read as ``False``: every flag here answers "may an
    automatic Hot-Swap fire", and the absent-means-off reading is precisely the false
    all-clear this surface exists to avoid.
    """

    raw = values.get(key)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise HotSwapTriggerOutputUnreadable(
        f"resv003_hot_swap_trigger_cli emitted no readable {key!r} line "
        f"(got {raw!r}); refusing to report a trigger configuration that cannot be evidenced"
    )


class CliHotSwapTriggerSource:
    """A concrete ``atp_dashboard.hotswap.HotSwapStatusSource`` backed by the real binary.

    Implements the protocol structurally (duck-typed, like every other provider source here),
    so the module that declares it never has to name an implementation.

    Only :meth:`trigger_config` resolves. :meth:`live_state` and :meth:`promotion_candidate`
    return ``None`` — no ``SRS-RESV-002``/``004``/``005``/``006`` producer persists a
    queryable fact yet, and inventing one would be exactly the fabrication the pane's
    deferred cells exist to prevent. The three legs fail INDEPENDENTLY, as the protocol
    requires: an unreadable trigger configuration must not blank an otherwise readable live
    state, so each raises on its own rather than through a shared cached read.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        binary: str | Path | None = None,
        runner: HotSwapTriggerCliRunner | None = None,
        timeout: float | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else _DEFAULT_BINARY
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout) if timeout is not None else _DEFAULT_TIMEOUT_S
        self._log_path = str(log_path) if log_path is not None else None

    # ------------------------------------------------------------------ #
    # HotSwapStatusSource
    # ------------------------------------------------------------------ #

    def trigger_config(self) -> dict[str, object] | None:
        """The persisted automatic-trigger enabled-state (``SRS-RESV-003``).

        ``None`` when nothing has ever been configured (no file) — the pane may then state
        the all-disabled default truthfully. Raises :class:`HotSwapStatusUnavailable` when
        the configuration exists but cannot be read.
        """

        completed = self._invoke(["config", "--state", self._state_path])
        if completed.returncode != 0:
            raise HotSwapStatusUnavailable(
                f"hot-swap trigger configuration unreadable: {completed.stderr.strip()}"
            )
        values = parse_trigger_cli_output(completed.stdout)
        source = values.get("config-source")
        if source == "default":
            # Genuinely never configured. `None` is the protocol's "no fact yet", which the
            # pane renders as the honest default rather than as an operator choice.
            return None
        if source != "persisted":
            raise HotSwapStatusUnavailable(
                f"hot-swap trigger configuration reported an unknown source {source!r}"
            )
        return self._config_payload(values)

    def live_state(self) -> dict[str, object] | None:
        """No producer persists a queryable live-swap state (owner ``SRS-RESV-004``/``005``/
        ``006``), so there is no fact to report and none is invented."""

        return None

    def promotion_candidate(self) -> dict[str, object] | None:
        """No ranking engine names a candidate yet (owner ``SRS-RESV-002``)."""

        return None

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _config_payload(values: dict[str, str]) -> dict[str, object]:
        """The shape ``HotSwapStatusProvider`` reads.

        The per-trigger detail is NESTED under each ``kind`` because that is how the pane
        looks it up (``triggers[kind]["enabled"]`` — ``atp_dashboard.hotswap``'s
        ``auto_triggers_live`` comprehension). A flat ``<kind>_enabled`` key parses fine and
        resolves nothing: every chip silently keeps rendering its deferred placeholder while
        the aggregate above it goes live, which reads as "configured, but no trigger is" —
        worse than an honest deferral because it looks resolved.
        """

        drawdown_enabled = strict_trigger_bool(values, "drawdown-demotion-enabled")
        drawdown: dict[str, object] = {"enabled": drawdown_enabled}
        if drawdown_enabled:
            raw = values.get("drawdown-demotion-threshold-bps")
            if raw is None or not raw.isdigit():
                raise HotSwapStatusUnavailable(
                    "hot-swap drawdown trigger reports enabled with no readable threshold "
                    f"(got {raw!r})"
                )
            drawdown["threshold_bps"] = int(raw)
        return {
            "any_enabled": strict_trigger_bool(values, "any-automatic-enabled"),
            "manual_promotion_available": strict_trigger_bool(values, "manual-promotion-available"),
            "default_disabled": strict_trigger_bool(values, "default-disabled"),
            "drawdown_demotion": drawdown,
            "top_ranked_promotion": {
                "enabled": strict_trigger_bool(values, "top-ranked-promotion-enabled")
            },
            "highest_momentum_promotion": {
                "enabled": strict_trigger_bool(values, "highest-momentum-promotion-enabled")
            },
        }

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise HotSwapStatusUnavailable(
                f"resv003_hot_swap_trigger_cli timed out after {self._timeout}s"
            ) from expired
        except OSError as launch_error:
            raise HotSwapStatusUnavailable(
                "resv003_hot_swap_trigger_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli`): "
                f"{launch_error}"
            ) from launch_error
