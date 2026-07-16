"""Stdlib L4 TCP forwarder — the one-way research-proxy hop (SRS-RES-001).

Policy (fail closed):

* **Bind**: never ``0.0.0.0``. ``ATP_RESEARCH_PROXY_BIND`` (validated against
  the SRS-SEC-002 loopback/RFC-1918 set) when set; otherwise every address the
  container's own hostname resolves to that passes the same policy (compose
  bridge addresses are RFC 1918). No qualifying address → refuse to start.
* **Upstream**: fixed at construction; the name is re-resolved per connection
  and EVERY resolved address must pass the loopback/RFC-1918 policy, else the
  connection is refused before a byte is piped.
* **Bounded**: a connection-slot semaphore (no unbounded thread growth), a
  poll-based pump so ``stop()`` is deterministic, and paired-socket teardown —
  either side's EOF/error tears both down.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Mapping

from atp_runtime.rest_server import is_allowed_bind_host

__all__ = [
    "TcpForwarder",
    "allowed_listen_addresses",
    "resolve_private_upstream",
]

#: Copy chunk size for each pump direction.
_CHUNK_BYTES = 64 << 10

#: Pump poll cadence — teardown latency, NOT an idle kill (kernel channels
#: legitimately idle for minutes).
_POLL_SECONDS = 1.0

#: Accept-loop poll cadence for deterministic stop().
_ACCEPT_POLL_SECONDS = 0.5

_BIND_ENV_KNOB = "ATP_RESEARCH_PROXY_BIND"


class ForwarderPolicyError(Exception):
    """Raised when the forwarder's bind or upstream violates the address policy."""


def allowed_listen_addresses(env: Mapping[str, str]) -> list[str]:
    """The loopback/RFC-1918 addresses this process may listen on (fail closed).

    ``ATP_RESEARCH_PROXY_BIND`` wins when set (a single validated address).
    Otherwise the container's own hostname is resolved and every RFC-1918/
    loopback address it yields is bound; public/unspecified addresses are
    dropped, and an empty result refuses start-up rather than widening.
    """

    explicit = env.get(_BIND_ENV_KNOB, "").strip()
    if explicit:
        if not is_allowed_bind_host(explicit):
            raise ForwarderPolicyError(
                f"{_BIND_ENV_KNOB}={explicit!r} is not loopback/RFC 1918 (SRS-SEC-002); "
                f"refusing to bind"
            )
        return [explicit]
    candidates: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if is_allowed_bind_host(address) and address not in candidates:
            candidates.append(address)
    if "127.0.0.1" not in candidates:
        # Loopback is always policy-clean and keeps a hostname that resolves
        # nowhere useful (dev laptops) from refusing start-up.
        candidates.append("127.0.0.1")
    return candidates


def resolve_private_upstream(host: str, port: int) -> tuple[str, int]:
    """Resolve the fixed upstream; EVERY address must be loopback/RFC 1918.

    Returns the first validated address (the caller dials that literal IP, so
    what was validated is what is connected to).
    """

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ForwarderPolicyError(f"upstream {host!r} did not resolve: {exc}") from exc
    if not infos:
        raise ForwarderPolicyError(f"upstream {host!r} resolved to no addresses")
    validated: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if not is_allowed_bind_host(address):
            raise ForwarderPolicyError(
                f"upstream {host!r} resolves to non-private address {address!r}; "
                f"refusing to forward (SRS-SEC-002 address policy)"
            )
        validated.append(address)
    return validated[0], port


class TcpForwarder:
    """Pipe every accepted connection to the FIXED upstream (one-way hop).

    ``start()`` returns the bound ``(host, port)`` (``listen_port=0`` picks an
    ephemeral port — used by the unit tests); ``stop()`` is deterministic and
    tears down the accept loop plus every live pump.
    """

    def __init__(
        self,
        listen_host: str,
        listen_port: int,
        upstream_host: str,
        upstream_port: int,
        *,
        max_connections: int = 64,
    ) -> None:
        if not is_allowed_bind_host(listen_host):
            raise ForwarderPolicyError(
                f"listen host {listen_host!r} is not loopback/RFC 1918; refusing to bind"
            )
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._upstream_host = upstream_host
        self._upstream_port = upstream_port
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stopped = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> tuple[str, int]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self._listen_host, self._listen_port))
        listener.listen(16)
        listener.settimeout(_ACCEPT_POLL_SECONDS)
        self._listener = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="atp-research-proxy-accept", daemon=True
        )
        self._accept_thread.start()
        return listener.getsockname()[0], listener.getsockname()[1]

    def stop(self) -> None:
        self._stopped.set()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=_ACCEPT_POLL_SECONDS * 4)
        if self._listener is not None:
            self._listener.close()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopped.is_set():
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                client.close()  # bounded: refuse rather than grow without limit
                continue
            threading.Thread(
                target=self._serve_connection,
                args=(client,),
                name="atp-research-proxy-conn",
                daemon=True,
            ).start()

    def _serve_connection(self, client: socket.socket) -> None:
        try:
            try:
                address, port = resolve_private_upstream(self._upstream_host, self._upstream_port)
                upstream = socket.create_connection((address, port), timeout=5.0)
            except (ForwarderPolicyError, OSError):
                client.close()
                return
            closed = threading.Event()
            downstream = threading.Thread(
                target=self._pump,
                args=(upstream, client, closed),
                name="atp-research-proxy-downstream",
                daemon=True,
            )
            downstream.start()
            self._pump(client, upstream, closed)
            downstream.join(timeout=_POLL_SECONDS * 4)
            client.close()
            upstream.close()
        finally:
            self._slots.release()

    def _pump(self, src: socket.socket, dst: socket.socket, closed: threading.Event) -> None:
        try:
            src.settimeout(_POLL_SECONDS)
            while not closed.is_set() and not self._stopped.is_set():
                try:
                    data = src.recv(_CHUNK_BYTES)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                try:
                    dst.sendall(data)
                except OSError:
                    break
        finally:
            closed.set()
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
