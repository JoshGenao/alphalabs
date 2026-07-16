"""L1 unit — the one-way research-proxy TCP forwarder (SRS-RES-001)."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from atp_research_proxy.forwarder import (
    ForwarderPolicyError,
    TcpForwarder,
    allowed_listen_addresses,
    resolve_private_upstream,
)


def _echo_upstream() -> tuple[socket.socket, int]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            data = conn.recv(4096)
            if data:
                conn.sendall(b"echo:" + data)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return listener, listener.getsockname()[1]


def test_bytes_round_trip_through_the_hop() -> None:
    listener, upstream_port = _echo_upstream()
    forwarder = TcpForwarder("127.0.0.1", 0, "127.0.0.1", upstream_port)
    host, port = forwarder.start()
    try:
        with socket.create_connection((host, port), timeout=5) as client:
            client.sendall(b"kernel bytes")
            assert client.recv(4096) == b"echo:kernel bytes"
    finally:
        forwarder.stop()
        listener.close()


def test_public_listen_host_refused() -> None:
    with pytest.raises(ForwarderPolicyError):
        TcpForwarder("0.0.0.0", 0, "127.0.0.1", 9)  # noqa: S104 - the refusal under test
    with pytest.raises(ForwarderPolicyError):
        TcpForwarder("8.8.8.8", 0, "127.0.0.1", 9)


def test_public_upstream_resolution_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def selective(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host == "research-upstream.invalid":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8888))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", selective)
    with pytest.raises(ForwarderPolicyError, match="non-private"):
        resolve_private_upstream("research-upstream.invalid", 8888)


def test_connection_to_public_resolving_upstream_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def selective(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host == "research-upstream.invalid":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 8888))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", selective)
    forwarder = TcpForwarder("127.0.0.1", 0, "research-upstream.invalid", 8888)
    host, port = forwarder.start()
    try:
        with socket.create_connection((host, port), timeout=5) as client:
            client.settimeout(5)
            # The hop refuses the public upstream and closes: EOF, no bytes.
            assert client.recv(4096) == b""
    finally:
        forwarder.stop()


def test_explicit_bind_env_is_validated() -> None:
    assert allowed_listen_addresses({"ATP_RESEARCH_PROXY_BIND": "127.0.0.1"}) == ["127.0.0.1"]
    with pytest.raises(ForwarderPolicyError):
        allowed_listen_addresses({"ATP_RESEARCH_PROXY_BIND": "0.0.0.0"})  # noqa: S104
    with pytest.raises(ForwarderPolicyError):
        allowed_listen_addresses({"ATP_RESEARCH_PROXY_BIND": "8.8.8.8"})


def test_default_bind_addresses_are_all_policy_clean() -> None:
    for address in allowed_listen_addresses({}):
        from atp_runtime.rest_server import is_allowed_bind_host

        assert is_allowed_bind_host(address)


def test_stop_is_deterministic() -> None:
    listener, upstream_port = _echo_upstream()
    forwarder = TcpForwarder("127.0.0.1", 0, "127.0.0.1", upstream_port)
    forwarder.start()
    started = time.monotonic()
    forwarder.stop()
    assert time.monotonic() - started < 5.0
    listener.close()
