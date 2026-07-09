"""L7 domain/safety — SRS-SEC-002 default-safe network binding.

The dashboard/API service must bind only to loopback / RFC 1918 addresses by
default; a publicly-routable bind must fail closed. This is the SRS-SEC-002
*attributed* end-to-end evidence (its mechanism is the operator-interface
runtime shared with SRS-UI-001 / API-001):

* started with the DEFAULT host, the runtime opens its real socket on a
  loopback interface only (never ``0.0.0.0`` / ``::``) — so a connection
  arriving on any external interface has nothing to reach;
* a public / unspecified host is refused *before* any socket opens;
* the bind-host classifier draws the loopback + RFC 1918 boundary exactly
  (including the addresses just outside RFC 1918 and the ``is_private``-but-
  unspecified ``0.0.0.0`` / ``::``);
* the ``tools/network_binding_check.py`` inspection evidence passes.

SRS trace: SRS-SEC-002 (NFR-S3 / StRS SN-2.01).
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
from collections.abc import Iterator

import pytest
from atp_dashboard import ReadinessBackedProvider, mount_dashboard
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import BindPolicyError
from atp_runtime.rest_server import assert_bind_allowed, is_allowed_bind_host

pytestmark = [pytest.mark.domain, pytest.mark.safety]


@pytest.fixture()
def default_bound_runtime() -> Iterator[tuple[str, int]]:
    """Start the runtime on its DEFAULT host (no ``host=`` override) → loopback."""

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start()  # DEFAULT host — must resolve to loopback only
    try:
        yield host, port
    finally:
        publisher.stop()
        runtime.stop()


def test_default_bind_is_loopback_only_not_all_interfaces(default_bound_runtime) -> None:
    host, port = default_bound_runtime
    address = ipaddress.ip_address(host)
    assert address.is_loopback, f"default bind host {host!r} is not loopback"
    assert not address.is_unspecified, "default bind must not be 0.0.0.0 / :: (all interfaces)"

    # The socket really is listening on loopback: a loopback connect succeeds.
    with socket.create_connection((host, port), timeout=5) as sock:
        assert sock.getpeername()[0] == host

    # ...and it answers HTTP on the mounted dashboard.
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/dashboard")
        assert conn.getresponse().status == 200
    finally:
        conn.close()


def test_public_and_unspecified_bind_fails_closed_before_socket() -> None:
    for host in ("0.0.0.0", "::", "8.8.8.8"):
        runtime = OperatorInterfaceRuntime()
        with pytest.raises(BindPolicyError):
            runtime.start(host=host, port=0)
        # Fail-closed: no socket was opened, so there is no address to report.
        with pytest.raises(RuntimeError):
            runtime.bound_address()


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "::1", "localhost", "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.10"],
)
def test_loopback_and_rfc1918_allowed(host: str) -> None:
    assert is_allowed_bind_host(host) is True
    assert_bind_allowed(host)  # does not raise


@pytest.mark.parametrize(
    "host",
    # public, unspecified (0.0.0.0/::), link-local, CGNAT, and the ranges just
    # outside RFC 1918 (172.15/172.32) — all must be refused.
    [
        "0.0.0.0",
        "::",
        "8.8.8.8",
        "1.2.3.4",
        "169.254.1.1",
        "100.64.0.1",
        "172.15.0.1",
        "172.32.0.1",
        "not-an-ip",
    ],
)
def test_public_unspecified_and_near_rfc1918_refused(host: str) -> None:
    assert is_allowed_bind_host(host) is False
    with pytest.raises(BindPolicyError):
        assert_bind_allowed(host)


def test_network_binding_inspection_check_passes() -> None:
    # tools/ is on sys.path via tests/conftest.py.
    import network_binding_check

    evidence = network_binding_check.run_checks()  # raises ContractCheckError on any violation
    assert len(evidence) == 5
