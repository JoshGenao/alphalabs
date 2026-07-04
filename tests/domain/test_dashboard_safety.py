"""L7 domain — SRS-UI-001 dashboard safety invariants.

A monitoring surface must never become an unguarded control plane. This anchors
that mounting the dashboard:

* adds **no mutating** endpoint — a POST to a dashboard path is refused, and the
  live-strategy / kill-switch confirmation guard (UI-4 / SRS-SAFE-001) is
  unchanged;
* keeps the loopback / RFC-1918 bind policy (SRS-SEC-002) fail-closed;
* never fabricates a metric value for a producer that is still deferred
  (SRS-BT-004 / SRS-BT-005 / SRS-PERF-001 / SRS-MD-006/007);
* claims exactly the three WebSocket publishers SRS-UI-001 owns — no control
  channel.

SRS trace: SRS-UI-001, SRS-SEC-002 (bind), UI-4 / SRS-SAFE-001 (kill-switch
confirmation), SRS-BT-004/005 · SRS-PERF-001 (deferred producers).
"""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator

import pytest
from atp_dashboard import OWNED_CHANNELS, ReadinessBackedProvider, mount_dashboard
from atp_dashboard.provider import DEFERRED
from atp_runtime import OperatorInterfaceRuntime
from atp_runtime.errors import BindPolicyError
from atp_runtime.rest_server import assert_bind_allowed, is_allowed_bind_host

pytestmark = [pytest.mark.domain, pytest.mark.safety]


@pytest.fixture()
def mounted_runtime() -> Iterator[tuple[OperatorInterfaceRuntime, str, int]]:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield runtime, host, port
    finally:
        publisher.stop()
        runtime.stop()


def _request(host: str, port: int, method: str, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        raw = response.read() or b"{}"
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        return response.status, body
    finally:
        conn.close()


def test_dashboard_surfaces_are_read_only(mounted_runtime) -> None:
    _, host, port = mounted_runtime
    # The asset + snapshot paths are GET-only; a POST is not a registered route.
    assert _request(host, port, "POST", "/dashboard")[0] in (404, 405)
    assert _request(host, port, "POST", "/dashboard/api/system")[0] in (404, 405)
    assert _request(host, port, "PUT", "/dashboard/api/system")[0] in (404, 405)
    # No dashboard path is a mutating trading-control route.
    assert _request(host, port, "DELETE", "/dashboard")[0] in (404, 405)


def test_kill_switch_confirmation_guard_is_unchanged(mounted_runtime) -> None:
    _, host, port = mounted_runtime
    # Mounting the dashboard must not weaken the SRS-SAFE-001 confirmation guard.
    status, body = _request(host, port, "POST", "/api/v1/kill-switch")
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"


def test_dashboard_bind_is_loopback_or_rfc1918_only() -> None:
    # SRS-SEC-002: loopback / RFC 1918 accepted; all-interfaces + public refused.
    for allowed in ("127.0.0.1", "10.1.2.3", "192.168.1.9"):
        assert is_allowed_bind_host(allowed)
        assert_bind_allowed(allowed)  # does not raise
    for refused in ("0.0.0.0", "8.8.8.8", "169.254.1.1"):
        assert not is_allowed_bind_host(refused)
        with pytest.raises(BindPolicyError):
            assert_bind_allowed(refused)


def test_start_on_public_host_is_refused_even_with_dashboard_mounted() -> None:
    runtime = OperatorInterfaceRuntime()
    mount_dashboard(runtime, ReadinessBackedProvider({}))
    with pytest.raises(BindPolicyError):
        runtime.start(host="8.8.8.8", port=0)


@pytest.mark.parametrize("channel", OWNED_CHANNELS)
def test_provider_never_fabricates_a_deferred_value(channel: str) -> None:
    payload = ReadinessBackedProvider({}).channel_payload(channel)
    for name, cell in payload.items():
        if isinstance(cell, dict) and str(cell.get("data_source", "")).startswith(DEFERRED):
            assert cell["value"] is None, f"{channel}.{name} fabricated a deferred value"


def test_publisher_claims_only_the_owned_channels() -> None:
    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(runtime, ReadinessBackedProvider({}))
    publisher.start()
    try:
        for channel in OWNED_CHANNELS:
            assert runtime.is_publisher_registered(channel)
        # Not a control channel: the dashboard never claims to publish, e.g., a
        # kill-switch or account-mutation stream.
        assert not runtime.is_publisher_registered("ACCOUNT_STATUS")
    finally:
        publisher.stop()
        runtime.stop()
