"""Research-environment embed provider (``SRS-RES-001`` / IF-13 / SyRS SYS-34a).

Feeds the dashboard's Research panel: the embedded Jupyter research
environment, reachable from the dashboard at the same-origin
:data:`RESEARCH_PREFIX` through the runtime's reverse-proxy seam
(``OperatorInterfaceRuntime.register_proxy_route``) — never a separate service
URL (SYS-34a / IF-13: "proxied through dashboard HTTPS; not a standalone
external endpoint").

Honesty (no fabrication — the SRS-UI-001 convention)
----------------------------------------------------
The panel state is only ever probe-derived:

* **not configured** — no ``ATP_RESEARCH_UPSTREAM`` set: an explicit deferred
  cell naming the knob and the owning deployment leg; never a fabricated URL.
* **unreachable** — a configured upstream that refused/timed out a bounded live
  probe: ``upstream_reachable: false`` with the socket-level reason.
* **reachable** — the upstream answered an HTTP status (ANY status counts as
  reachability — a 404/403 still proves a live server); only then is
  ``embed_path`` (the same-origin iframe target) populated.

Contract: the upstream must serve under ``base_url == RESEARCH_PREFIX``
because the runtime proxy forwards request paths verbatim (no rewriting — see
``atp_runtime/proxy.py``); the JupyterLab container is launched with
``--ServerApp.base_url=/research/`` accordingly.

SRS trace
---------
``SRS-RES-001`` (dashboard-embedded research environment), IF-13 (proxied
through the dashboard), SyRS ``SYS-34a`` (embedded view, no separate URL),
``SYS-34c`` (independence — the probe touches only the research upstream),
``SRS-SEC-004`` (the one-way boundary the proxy preserves).
"""

from __future__ import annotations

import http.client
import time
from urllib.parse import urlsplit

__all__ = [
    "RESEARCH_PREFIX",
    "UPSTREAM_ENV_KNOB",
    "ResearchEnvironmentProvider",
]

#: The same-origin path prefix the research environment is embedded under —
#: both the runtime proxy prefix and the JupyterLab ``base_url``.
RESEARCH_PREFIX = "/research/"

#: The env knob naming the fixed upstream (e.g. ``http://127.0.0.1:8888`` in
#: dev; ``http://phase1-research-proxy:8890`` in the compose stack).
UPSTREAM_ENV_KNOB = "ATP_RESEARCH_UPSTREAM"

#: The deployment leg that provisions the upstream (JupyterLab image + one-way
#: research-proxy topology).
_DEPLOYMENT_OWNER = "SRS-RES-001"


def _utc_iso() -> str:
    """Current UTC time as an ISO-8601 ``Z`` string (real wall-clock stamp)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ResearchEnvironmentProvider:
    """Assembles the SRS-RES-001 research-embed snapshot.

    A composition-time opt-in source (like the SRS-UI-002 inventory): a bare
    SRS-UI-001 dashboard serves neither the research route nor the proxy.
    ``upstream`` is the fixed proxy upstream (or ``None`` when unconfigured);
    the snapshot's reachability is a bounded LIVE probe against it — never a
    cached or fabricated "connected" state.
    """

    def __init__(self, upstream: str | None, *, probe_timeout: float = 1.0) -> None:
        self._upstream = upstream or None
        self._probe_timeout = probe_timeout

    @property
    def upstream(self) -> str | None:
        """The fixed upstream URL this provider was composed with (or None)."""

        return self._upstream

    def _probe(self) -> tuple[bool, int | None, str]:
        """One bounded HTTP GET against ``{upstream}{RESEARCH_PREFIX}``.

        Returns ``(reachable, status_code, detail)``. ANY HTTP status proves a
        live upstream; only a socket-level failure (refused / timeout / DNS)
        reads as unreachable.
        """

        assert self._upstream is not None  # caller gates on configuration
        split = urlsplit(self._upstream)
        host = split.hostname or ""
        port = split.port if split.port is not None else 80
        connection = http.client.HTTPConnection(host, port, timeout=self._probe_timeout)
        try:
            connection.request("GET", RESEARCH_PREFIX)
            response = connection.getresponse()
            response.read()
            return True, response.status, f"upstream answered HTTP {response.status}"
        except (OSError, http.client.HTTPException) as exc:
            return False, None, f"upstream probe failed: {exc}"
        finally:
            connection.close()

    def research_snapshot(self) -> dict[str, object]:
        """The REST poll body served at ``GET /dashboard/api/research``.

        GET-only, read-only: the snapshot carries reachability *state*, never a
        control affordance. ``embed_path`` is populated only when the live
        probe proved the upstream reachable.
        """

        snapshot: dict[str, object] = {
            "generated_at": _utc_iso(),
            "prefix": RESEARCH_PREFIX,
            "srs_ref": "SRS-RES-001",
        }
        if self._upstream is None:
            snapshot.update(
                {
                    "ok": False,
                    "configured": False,
                    "upstream_reachable": None,
                    "embed_path": None,
                    "detail": (
                        f"research upstream not configured (set {UPSTREAM_ENV_KNOB}); "
                        f"deployment leg owner {_DEPLOYMENT_OWNER}"
                    ),
                }
            )
            return snapshot
        reachable, status_code, detail = self._probe()
        snapshot.update(
            {
                "ok": reachable,
                "configured": True,
                "upstream_reachable": reachable,
                "status_code": status_code,
                "embed_path": f"{RESEARCH_PREFIX}lab" if reachable else None,
                "detail": detail,
            }
        )
        return snapshot
