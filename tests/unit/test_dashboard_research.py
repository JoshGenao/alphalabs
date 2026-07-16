"""L1 unit — SRS-RES-001 research-embed provider honesty.

The panel state must only ever be probe-derived: an unconfigured provider is
an explicit deferred cell, an unreachable upstream reads unreachable with the
socket-level reason, and ``embed_path`` exists ONLY when a live probe proved
the upstream reachable. Never a fabricated "connected".
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from atp_dashboard.research import (
    RESEARCH_PREFIX,
    UPSTREAM_ENV_KNOB,
    ResearchEnvironmentProvider,
)


class _Stub(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: object, **kwargs: object) -> None:
        return


@pytest.fixture()
def stub_upstream() -> Iterator[int]:
    _Stub.status = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_unconfigured_is_an_explicit_deferred_cell() -> None:
    snapshot = ResearchEnvironmentProvider(None).research_snapshot()
    assert snapshot["ok"] is False
    assert snapshot["configured"] is False
    assert snapshot["upstream_reachable"] is None
    assert snapshot["embed_path"] is None
    detail = str(snapshot["detail"])
    assert UPSTREAM_ENV_KNOB in detail  # names the knob, not a fake URL
    assert "SRS-RES-001" in detail


def test_empty_string_upstream_reads_as_unconfigured() -> None:
    provider = ResearchEnvironmentProvider("")
    assert provider.upstream is None
    assert provider.research_snapshot()["configured"] is False


def test_reachable_upstream_yields_embed_path(stub_upstream: int) -> None:
    provider = ResearchEnvironmentProvider(f"http://127.0.0.1:{stub_upstream}")
    snapshot = provider.research_snapshot()
    assert snapshot["ok"] is True
    assert snapshot["configured"] is True
    assert snapshot["upstream_reachable"] is True
    assert snapshot["status_code"] == 200
    assert snapshot["embed_path"] == f"{RESEARCH_PREFIX}lab"
    assert snapshot["prefix"] == RESEARCH_PREFIX


def test_any_http_status_counts_as_reachable(stub_upstream: int) -> None:
    # A 404 from the upstream still proves a LIVE server (reachability, not
    # health): the probe reports it honestly with the status code.
    _Stub.status = 404
    provider = ResearchEnvironmentProvider(f"http://127.0.0.1:{stub_upstream}")
    snapshot = provider.research_snapshot()
    assert snapshot["upstream_reachable"] is True
    assert snapshot["status_code"] == 404


def test_unreachable_upstream_never_fabricates(stub_upstream: int) -> None:
    provider = ResearchEnvironmentProvider("http://127.0.0.1:1")
    snapshot = provider.research_snapshot()
    assert snapshot["ok"] is False
    assert snapshot["configured"] is True
    assert snapshot["upstream_reachable"] is False
    assert snapshot["status_code"] is None
    assert snapshot["embed_path"] is None
    assert "probe failed" in str(snapshot["detail"])


def test_probe_timeout_is_bounded() -> None:
    # An upstream that accepts but never answers must fail within the probe
    # timeout — the dashboard poll can never hang on the research cell.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        provider = ResearchEnvironmentProvider(
            f"http://127.0.0.1:{listener.getsockname()[1]}", probe_timeout=0.5
        )
        started = time.monotonic()
        snapshot = provider.research_snapshot()
        elapsed = time.monotonic() - started
        assert snapshot["upstream_reachable"] is False
        assert elapsed < 3.0
    finally:
        listener.close()
