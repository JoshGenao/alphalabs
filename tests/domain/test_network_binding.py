"""L7 domain/safety — SRS-SEC-002 default-safe network binding.

The dashboard/API service must bind only to loopback / RFC 1918 addresses by
default; a publicly-routable bind must fail closed. This is the SRS-SEC-002
*attributed* evidence (its mechanism is the operator-interface runtime shared
with SRS-UI-001 / API-001). The solo-provable parts:

* started with the DEFAULT host, the runtime opens its real socket on a
  loopback interface only (never ``0.0.0.0`` / ``::``) — so a connection
  arriving on any external interface has nothing to reach;
* the loopback-bound service refuses a connection addressed to this host's real
  (non-loopback) interface — an empirical proxy that it is not on ``0.0.0.0``;
* a public / unspecified host is refused *before* any socket opens;
* the bind-host classifier draws the loopback + RFC 1918 boundary exactly
  (including the addresses just outside RFC 1918 and the ``is_private``-but-
  unspecified ``0.0.0.0`` / ``::``);
* the ``tools/network_binding_check.py`` inspection evidence passes.

DEFERRED (why SRS-SEC-002 lands serialized, passes:false): the literal AC-2
external-host connect against a *non-RFC1918* (publicly-routable) interface needs
a public interface bound on the host, which a NAT'd dev box / CI runner does not
have; that empirical evidence is captured on the deployed Phase 1 stack from an
external host (operator / verified-e2e). See
``test_external_host_connect_on_non_rfc1918_interface_is_refused``.

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
    assert evidence and all(isinstance(item, str) and item for item in evidence)


def _primary_non_loopback_ipv4() -> str | None:
    """Best-effort primary non-loopback IPv4 of this host (sends no packets).

    A UDP ``connect`` only fixes the outbound route, so ``getsockname`` yields the
    interface the kernel would use — without any traffic. Returns ``None`` when the
    host has no usable non-loopback IPv4 (e.g. an isolated CI runner).
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    if ip.startswith("127.") or ip in ("0.0.0.0", ""):
        return None
    return ip


# The literal SRS-SEC-002 acceptance test — an external-host connect against a
# *non-RFC1918* (publicly-routable) interface fails — needs a public interface
# bound on the host. A NAT'd dev box / CI runner has only an RFC 1918 interface,
# so that empirical evidence is a DEFERRED live verification, captured on the
# deployed Phase 1 stack from an external host (operator / verified-e2e). This is
# why SRS-SEC-002 lands serialized (passes stays false).
_DEFERRED_EXTERNAL_HOST = (
    "SRS-SEC-002 external-host refusal on a non-RFC1918 (publicly-routable) interface "
    "is a DEFERRED live verification: this host binds no public interface. Capture it on "
    "the deployed Phase 1 stack from an external host (operator / verified-e2e)."
)


def _primary_non_rfc1918_ipv4() -> str | None:
    """This host's primary interface IPv4 *only if* it is publicly routable.

    Returns ``None`` for loopback and any private (RFC 1918 / CGNAT / link-local)
    address — i.e. the usual NAT'd case, where the literal non-RFC1918 acceptance
    test cannot run.
    """

    ip = _primary_non_loopback_ipv4()
    if ip is None:
        return None
    return ip if ipaddress.ip_address(ip).is_global else None


def test_service_not_listening_on_local_non_loopback_interface(default_bound_runtime) -> None:
    """Solo proxy for AC-2: the loopback-bound service accepts NO connection on
    this host's real (non-loopback) interface.

    Empirically confirms — beyond the reported bind address — that the socket is
    not on ``0.0.0.0``: a connect addressed to the primary non-loopback interface
    (typically an RFC 1918 LAN address) is refused. It is a *proxy* for, not a
    substitute for, the deferred non-RFC1918 external-host check below.
    """

    _, port = default_bound_runtime
    local_iface = _primary_non_loopback_ipv4()
    if local_iface is None:
        pytest.skip("no non-loopback interface available on this host")
    with pytest.raises(OSError):  # ConnectionRefusedError / TimeoutError are OSError
        with socket.create_connection((local_iface, port), timeout=2):
            pass


def test_external_host_connect_on_non_rfc1918_interface_is_refused(default_bound_runtime) -> None:
    """SRS-SEC-002 AC-2 (literal): an external-host connect against a non-RFC1918
    interface fails under the default (loopback-only) configuration.

    Runs only when the host actually binds a publicly-routable interface; on a
    NAT'd host it skips as an explicit DEFERRED live verification (see
    ``_DEFERRED_EXTERNAL_HOST``) — SRS-SEC-002 stays serialized until that
    evidence is captured on the deployed stack.
    """

    _, port = default_bound_runtime
    public_iface = _primary_non_rfc1918_ipv4()
    if public_iface is None:
        pytest.skip(_DEFERRED_EXTERNAL_HOST)
    with pytest.raises(OSError):
        with socket.create_connection((public_iface, port), timeout=2):
            pass
