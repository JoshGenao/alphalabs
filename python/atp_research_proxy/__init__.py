"""One-way research-proxy hop (``SRS-RES-001`` / IF-13 / ``SRS-SEC-004``).

A stdlib L4 TCP forwarder deployed as the compose service
``phase1-research-proxy``: the only member of BOTH ``atp_research_net``
(Jupyter's isolated internal network) and ``atp_research_edge_net`` (the
internal network the dashboard/API reaches it on). The dashboard's runtime
reverse-proxy (``atp_runtime.proxy``) targets this hop, which pipes bytes to
its FIXED upstream — the Jupyter container — so the live-control-bearing
``phase1-dashboard-api`` never joins ``atp_research_net``
(``tools/jupyter_isolation_check.py`` enforces that statically).

One-way by construction: the forwarder's upstream is fixed at start-up, so a
connection initiated FROM the Jupyter container can only loop back to Jupyter
itself — there is no forwarding path toward the dashboard, the execution
engine, or anything else. An L4 pipe passes HTTP *and* the kernel WebSocket
traffic transparently; all protocol-level policy (header hygiene, smuggling
refusals, bounded bodies) lives in the dashboard-side ``atp_runtime.proxy``.
"""

from .forwarder import TcpForwarder, allowed_listen_addresses, resolve_private_upstream

__all__ = [
    "TcpForwarder",
    "allowed_listen_addresses",
    "resolve_private_upstream",
]
