"""Loopback stand-ins for the two SRS-NOTIF-001 operator transports.

The real ``data013_quarantine_alert_cli notify`` talks to an SMTP relay (IF-10)
and to ntfy (IF-11), and both adapters refuse anything that is not loopback or
RFC 1918. These two servers speak exactly the part of each protocol the
adapters use, on 127.0.0.1, so a test can drive the REAL binary, the REAL
dispatcher and the REAL audit store end to end and read back what each channel
received.

* ``FakeRelay`` — an SMTP relay that advertises AUTH, accepts AUTH PLAIN, and
  answers the end of DATA with ``250 Ok: queued as <id>``: the hand-off the
  email adapter records as QUEUED (the message is still inside a relay).
* ``FakeNtfy`` — an ntfy publish endpoint that answers ``POST /<topic>`` with
  HTTP 200 and an ntfy-shaped JSON object carrying an ``id``: what the push
  adapter records as DELIVERED.

``fail=True`` makes either one refuse (SMTP ``554`` at end of data, HTTP 503),
so a test can show an alert that reached no channel.

Not a test module (no ``test_`` prefix): imported by the boundary and e2e tests.
"""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

TOPIC = "atp-e2e-topic"
TOKEN = "e2e-push-token"


class _Threaded(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeRelay:
    """An SMTP relay on 127.0.0.1 that records each message body it queues."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.messages: list[str] = []
        relay = self

        class Handler(socketserver.StreamRequestHandler):
            def _reply(self, line: str) -> None:
                self.wfile.write((line + "\r\n").encode())
                self.wfile.flush()

            def handle(self) -> None:
                self._reply("220 relay.e2e ESMTP")
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    verb = raw.decode(errors="replace").strip().split(" ", 1)[0].upper()
                    if verb == "EHLO":
                        self._reply("250-relay.e2e")
                        self._reply("250 AUTH PLAIN")
                    elif verb == "AUTH":
                        self._reply("235 2.7.0 Authentication successful")
                    elif verb in ("MAIL", "RCPT"):
                        self._reply("250 2.1.0 Ok")
                    elif verb == "DATA":
                        self._reply("354 End data with <CR><LF>.<CR><LF>")
                        body: list[str] = []
                        while True:
                            line = self.rfile.readline().decode(errors="replace")
                            if line in (".\r\n", ".\n", ""):
                                break
                            body.append(line)
                        if relay.fail:
                            self._reply("554 5.0.0 Transaction failed")
                        else:
                            relay.messages.append("".join(body))
                            self._reply(f"250 2.0.0 Ok: queued as E2E{len(relay.messages)}")
                    elif verb == "QUIT":
                        self._reply("221 2.0.0 Bye")
                        return
                    else:
                        self._reply("502 5.5.2 Command not recognized")

        self._server = _Threaded(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeNtfy:
    """An ntfy publish endpoint on 127.0.0.1 that records each published body."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[str] = []
        ntfy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 (http.server's name)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode(errors="replace")
                if ntfy.fail or self.path != f"/{TOPIC}":
                    self.send_response(503)
                    payload = b'{"code":50301,"error":"e2e fault"}'
                else:
                    ntfy.published.append(body)
                    self.send_response(200)
                    payload = (
                        '{"id":"e2e%d","time":1786939980,"event":"message","topic":"%s"}'
                        % (len(ntfy.published), TOPIC)
                    ).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        class Server(HTTPServer):
            allow_reuse_address = True

            def server_bind(self) -> None:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                super().server_bind()

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def transport_env(relay: FakeRelay, ntfy: FakeNtfy) -> dict[str, str]:
    """The environment the notify CLI needs to reach these two servers, and nothing else."""

    return {
        "ATP_SMTP_RELAY_HOST": "127.0.0.1",
        "ATP_SMTP_RELAY_PORT": str(relay.port),
        "ATP_SMTP_RELAY_USER": "atp-e2e",
        "ATP_SMTP_API_KEY": "e2e-relay-key",
        "ATP_SMTP_SENDER": "atp@example.com",
        "ATP_OPERATOR_EMAIL": "operator@example.com",
        "ATP_PUSH_HOST": "127.0.0.1",
        "ATP_PUSH_PORT": str(ntfy.port),
        "ATP_PUSH_TOPIC": TOPIC,
        "ATP_PUSH_TOKEN": TOKEN,
    }


@contextmanager
def operator_transports(
    *, fail: bool = False
) -> Iterator[tuple[FakeRelay, FakeNtfy, dict[str, str]]]:
    """Both transports, up for the duration of the block."""

    relay, ntfy = FakeRelay(fail=fail), FakeNtfy(fail=fail)
    try:
        yield relay, ntfy, transport_env(relay, ntfy)
    finally:
        relay.close()
        ntfy.close()
