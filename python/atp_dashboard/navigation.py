"""Primary-workflow navigation to the embedded research environment.

``SRS-RES-003`` / SyRS ``SYS-43``: "The dashboard shall provide direct
navigation to the embedded Jupyter research environment from the primary
operator workflow." Acceptance: *the operator can open the embedded Jupyter
environment from the primary dashboard workflow **without using a direct
service URL***.

This module owns the navigation *model* the dashboard's topbar renders — the
persistent entry an operator reaches without scrolling the panel deck. It is
deliberately NOT the embed itself: ``SRS-RES-001`` (:mod:`atp_dashboard.research`
+ the runtime reverse-proxy seam) owns the embedded environment, and SyRS v0.6
split ``SYS-43`` from ``SYS-34a`` precisely so the navigation affordance is a
requirement of its own.

Two facts, kept separate
------------------------
The navigation entry is actionable only when BOTH hold, and neither can stand
in for the other:

* **routable** (this module) — a *composition* fact: is the same-origin
  ``/research/`` prefix registered on this runtime at all? Static, cheap, and
  probe-free, so serving the navigation model costs no upstream traffic.
* **reachable** (``SRS-RES-001``) — a *live* fact: is the upstream answering
  right now? That stays the ``GET /dashboard/api/research`` probe's answer, and
  the SPA gates the navigation control on the freshest one it has (a stalled or
  degraded probe DISARMS the control rather than leaving a stale actionable
  affordance on screen).

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
A navigation entry that cannot route renders as an explicit deferred cell
naming its knob and owning feature; it is never a dead link that merely looks
actionable.

No direct service URL (the acceptance criterion, enforced structurally)
-----------------------------------------------------------------------
:func:`same_origin_target` is a fail-closed allow-list: a navigation target
must be a root-relative same-origin path, so the model is structurally unable
to carry a Jupyter service URL. The provider is composed with the same-origin
*prefix* and a boolean — it is never handed the upstream, so there is no
upstream for it to leak — and a rejected target is reported WITHOUT echoing it
(echoing is itself a leak).

SRS trace
---------
``SRS-RES-003`` (primary dashboard navigation), SyRS ``SYS-43``; StRS
``SN-1.18`` / ``SN-2.01``. The environment navigated to is ``SRS-RES-001``
(SyRS ``SYS-34a`` / IF-13), whose one-way container boundary is ``SRS-SEC-004``.
"""

from __future__ import annotations

import time

from .research import RESEARCH_PREFIX, UPSTREAM_ENV_KNOB

__all__ = [
    "NAVIGATION_SRS_REF",
    "RESEARCH_ENTRY_ID",
    "RESEARCH_ENTRY_LABEL",
    "RESEARCH_PANEL_ANCHOR",
    "PrimaryNavigationProvider",
    "same_origin_target",
]

#: The requirement this navigation model answers to.
NAVIGATION_SRS_REF = "SRS-RES-003"

#: Stable id of the research navigation entry (the SPA keys its control off it).
RESEARCH_ENTRY_ID = "research"

#: Operator-facing label of the research navigation entry.
RESEARCH_ENTRY_LABEL = "Research"

#: The SPA anchor the entry navigates to — the ``id`` of the research panel
#: section, which also makes ``/dashboard#research`` an addressable deep link.
RESEARCH_PANEL_ANCHOR = "research"

#: The feature that owns the embedded environment being navigated to.
_EMBED_OWNER = "SRS-RES-001"

#: Characters a navigation target may never contain. ``:`` blocks every scheme
#: (and ``user:pass@host`` credentials), ``\`` blocks the backslash spellings
#: browsers normalise into ``//``, and ``@`` blocks userinfo outright.
_FORBIDDEN_TARGET_CHARS = frozenset(":\\@")


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def same_origin_target(target: object) -> str | None:
    """Return ``target`` iff it is a same-origin, root-relative path, else ``None``.

    The fail-closed allow-list behind the ``SRS-RES-003`` acceptance criterion
    ("without using a direct service URL"). Accepted: a non-empty ``str``
    beginning with exactly one ``/``, free of whitespace and control
    characters, of scheme/userinfo punctuation, and of ``..`` segments — i.e. a
    path the browser can only resolve against the dashboard's own origin.
    Everything else (an absolute URL, a protocol-relative ``//host``, a
    scheme-relative or bare relative path, a non-string) is rejected.
    """

    if not isinstance(target, str) or not target:
        return None
    if any(char <= " " or char == "\x7f" for char in target):
        return None
    if any(char in _FORBIDDEN_TARGET_CHARS for char in target):
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    if ".." in target.split("/"):
        return None
    return target


class PrimaryNavigationProvider:
    """Assembles the ``SRS-RES-003`` primary-navigation snapshot.

    Composed alongside the ``SRS-RES-001`` research embed (see
    :func:`atp_dashboard.server.mount_dashboard`): navigation to an embed that
    is not mounted would be navigation to nothing, so a bare ``SRS-UI-001``
    dashboard serves no navigation route at all and the SPA renders the entry's
    explicit "not mounted" state.

    ``research_routable`` is the composition fact that the same-origin proxy
    prefix is registered on this runtime — NOT a claim that Jupyter is up. The
    provider performs no I/O: serving the model never probes an upstream.
    """

    def __init__(
        self,
        *,
        research_routable: bool,
        research_state_route: str,
        research_prefix: str = RESEARCH_PREFIX,
    ) -> None:
        self._research_routable = bool(research_routable)
        self._research_state_route = research_state_route
        self._research_prefix = research_prefix

    @classmethod
    def for_research(
        cls,
        research: object,
        *,
        state_route: str,
    ) -> PrimaryNavigationProvider:
        """Derive the navigation model from a mounted research provider.

        Routability is read from the provider's ``upstream`` being configured —
        the same condition ``mount_dashboard`` uses to register the same-origin
        proxy, so the two can never disagree. The upstream VALUE is not read,
        stored, or served.
        """

        return cls(
            research_routable=getattr(research, "upstream", None) is not None,
            research_state_route=state_route,
        )

    def _research_entry(self) -> dict[str, object]:
        """The one navigation entry this feature owns."""

        entry: dict[str, object] = {
            "id": RESEARCH_ENTRY_ID,
            "label": RESEARCH_ENTRY_LABEL,
            "panel_anchor": RESEARCH_PANEL_ANCHOR,
            "srs_ref": _EMBED_OWNER,
        }
        target = same_origin_target(self._research_prefix)
        state_route = same_origin_target(self._research_state_route)
        if target is None or state_route is None:
            # Fail closed. The rejected value is NOT echoed — reporting it back
            # would republish exactly the direct service URL the acceptance
            # criterion forbids.
            entry.update(
                {
                    "routable": False,
                    "target": None,
                    "state_route": None,
                    "detail": (
                        "navigation refused: a target must be a same-origin path "
                        "(SYS-43 — no direct service URL)"
                    ),
                }
            )
            return entry
        entry["state_route"] = state_route
        if not self._research_routable:
            entry.update(
                {
                    "routable": False,
                    "target": None,
                    "detail": (
                        f"research environment not configured (set {UPSTREAM_ENV_KNOB}); "
                        f"embed owner {_EMBED_OWNER}"
                    ),
                }
            )
            return entry
        entry.update(
            {
                "routable": True,
                "target": target,
                "detail": (
                    f"same-origin {target} registered on this runtime; "
                    f"live reachability from {state_route}"
                ),
            }
        )
        return entry

    def navigation_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/navigation``.

        GET-only and read-only: the snapshot describes where the primary
        workflow can navigate, never a control action. ``entries`` is a list so
        later primary-workflow destinations can join it without a shape change.
        """

        return {
            "generated_at": _utc_iso(),
            "srs_ref": NAVIGATION_SRS_REF,
            "entries": [self._research_entry()],
        }
