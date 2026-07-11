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

AC-2 (external-host connect against a *non-RFC1918* interface fails, under the
default config): the Phase 1 deployment target is an RFC1918-only network (a
``10.0.0.x`` server — ``10.0.0.0/8`` is itself RFC 1918), so it binds no
publicly-routable interface at all; the literal "connect against a non-RFC1918
interface" can never be exercised there — that interface does not exist. The
provable, strictly stronger form of AC-2 is the structural invariant asserted by
``test_no_non_rfc1918_binding_under_default_config``: under the default config the
service is confined to loopback / RFC 1918, and the runtime refuses — fail closed,
before any socket — to bind ANY non-RFC1918 address, so no external (non-RFC1918)
endpoint ever exists to connect to.

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


def test_service_not_listening_on_local_non_loopback_interface(default_bound_runtime) -> None:
    """Solo empirical reinforcement of AC-2: the loopback-bound service accepts NO
    connection on this host's real (non-loopback) interface.

    Confirms — beyond the reported bind address — that the socket is not on
    ``0.0.0.0``: a connect addressed to the primary non-loopback interface
    (typically an RFC 1918 LAN address) is refused. Reinforces the structural AC-2
    invariant asserted by ``test_no_non_rfc1918_binding_under_default_config``.
    """

    _, port = default_bound_runtime
    local_iface = _primary_non_loopback_ipv4()
    if local_iface is None:
        pytest.skip("no non-loopback interface available on this host")
    with pytest.raises(OSError):  # ConnectionRefusedError / TimeoutError are OSError
        with socket.create_connection((local_iface, port), timeout=2):
            pass


# Representative publicly-routable (non-RFC1918) addresses — IPv4 documentation
# ranges (TEST-NET-1/3) + well-known public resolvers, IPv4 and IPv6. None is
# loopback / RFC 1918, so under the default policy the runtime must refuse to bind
# every one of them.
_PUBLIC_NON_RFC1918_HOSTS = (
    "8.8.8.8",
    "1.2.3.4",
    "203.0.113.7",
    "198.51.100.9",
    "2001:4860:4860::8888",
    "2606:4700:4700::1111",
)


def test_no_non_rfc1918_binding_under_default_config(default_bound_runtime) -> None:
    """SRS-SEC-002 AC-2 (structural, RFC1918-only deployment): under the default
    configuration no non-RFC1918 (publicly-routable) endpoint can exist to connect
    to — so an external-host connect against a non-RFC1918 interface fails.

    The Phase 1 target is an RFC1918-only network (a ``10.0.0.x`` server;
    ``10.0.0.0/8`` is itself RFC 1918), so it binds no publicly-routable interface
    at all — the literal "connect against a non-RFC1918 interface" can never be
    exercised there. This asserts the strictly stronger, solo-provable invariant:

    * the DEFAULT bind is confined to loopback / RFC 1918 (never ``is_global``); and
    * the runtime refuses — fail closed, before any socket opens — to bind ANY
      publicly-routable address (IPv4 and IPv6), so the service can never be placed
      on a non-RFC1918 interface under the configuration this runtime provides; and
    * where the host *does* bind a publicly-routable interface, the literal
      external-host connect against it is refused (retained, runs-when-present).

    Holds for every public address, not just one the host happens to bind.
    """

    host, port = default_bound_runtime
    # (1) Default is confined to loopback / RFC 1918 — never a public interface.
    assert is_allowed_bind_host(host), f"default bind host {host!r} not in the allowed set"
    assert not ipaddress.ip_address(host).is_global, (
        f"default bind host {host!r} is publicly routable"
    )

    # (2) No publicly-routable interface can be bound: every non-RFC1918 host is
    #     refused, fail closed, before any socket opens.
    for public in _PUBLIC_NON_RFC1918_HOSTS:
        assert is_allowed_bind_host(public) is False, f"{public} must be refused"
        runtime = OperatorInterfaceRuntime()
        with pytest.raises(BindPolicyError):
            runtime.start(host=public, port=0)
        # Fail-closed: no socket was opened, so there is no address to report.
        with pytest.raises(RuntimeError):
            runtime.bound_address()

    # (3) Literal external-host evidence, retained: if this host actually binds a
    #     publicly-routable (non-RFC1918) interface, a connect against it fails —
    #     the loopback-bound service is not there. RFC1918-only deployment targets
    #     (e.g. a 10.0.0.x server) bind none, so this is a no-op there; it is NOT a
    #     skip — (1) and (2) already prove the invariant unconditionally.
    external_iface = _primary_non_loopback_ipv4()
    if external_iface is not None and ipaddress.ip_address(external_iface).is_global:
        with pytest.raises(OSError):  # ConnectionRefusedError / TimeoutError are OSError
            with socket.create_connection((external_iface, port), timeout=2):
                pass
