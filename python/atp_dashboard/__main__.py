"""``python -m atp_dashboard`` — run the SRS-UI-001 web dashboard.

Binds loopback by default (``ATP_DASHBOARD_BIND_HOST``, default ``127.0.0.1``);
the port is ``ATP_DASHBOARD_PORT``, falling back to ``ATP_DEV_PORT`` (the agent's
private dev port for a parallel-safe smoke test), then ``8080``. The bind host is
validated fail-closed by ``runtime.start`` (SRS-SEC-002).
"""

from __future__ import annotations

import os

from .server import serve


def _resolve_port() -> int:
    raw = os.environ.get("ATP_DASHBOARD_PORT") or os.environ.get("ATP_DEV_PORT") or "8080"
    return int(raw)


def main() -> None:
    host = os.environ.get("ATP_DASHBOARD_BIND_HOST", "127.0.0.1")
    serve(host=host, port=_resolve_port())


if __name__ == "__main__":
    main()
