"""``python -m atp_research_proxy`` — the compose-deployed one-way hop.

Env knobs (all defaulted for the phase1 compose topology):

* ``ATP_RESEARCH_PROXY_PORT``  — listen port (default ``8890``).
* ``ATP_RESEARCH_PROXY_BIND``  — explicit listen address (validated
  loopback/RFC-1918); unset → every policy-clean address the container's own
  hostname resolves to.
* ``ATP_RESEARCH_UPSTREAM_HOST`` / ``ATP_RESEARCH_UPSTREAM_PORT`` — the FIXED
  upstream (default ``phase1-jupyter:8888``), re-validated per connection.

Blocks until SIGINT/SIGTERM (the container's PID-1 contract), mirroring
``atp_dashboard.serve``.
"""

from __future__ import annotations

import os
import signal
import threading
from types import FrameType

from .forwarder import TcpForwarder, allowed_listen_addresses


def main() -> None:
    env = dict(os.environ)
    port = int(env.get("ATP_RESEARCH_PROXY_PORT", "8890"))
    upstream_host = env.get("ATP_RESEARCH_UPSTREAM_HOST", "phase1-jupyter")
    upstream_port = int(env.get("ATP_RESEARCH_UPSTREAM_PORT", "8888"))

    forwarders = [
        TcpForwarder(address, port, upstream_host, upstream_port)
        for address in allowed_listen_addresses(env)
    ]
    bound = [forwarder.start() for forwarder in forwarders]
    listeners = ", ".join(f"{host}:{bound_port}" for host, bound_port in bound)
    print(  # noqa: T201 - operator-facing startup line
        f"atp-research-proxy forwarding {listeners} -> "
        f"{upstream_host}:{upstream_port} (one-way hop, SRS-RES-001)"
    )

    stopped = threading.Event()

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    try:
        stopped.wait()
    finally:
        for forwarder in forwarders:
            forwarder.stop()


if __name__ == "__main__":
    main()
