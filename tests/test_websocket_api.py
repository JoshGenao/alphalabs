from __future__ import annotations

import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_ws" / "asyncapi.json"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_ws  # noqa: E402
from atp_ws import (  # noqa: E402
    AUTH_MODEL,
    BIND_HOST,
    CLIENT_COMMANDS,
    EVENT_CHANNELS,
    MAX_REFRESH_SECONDS,
    WS_PATH,
    Channel,
    ClientCommand,
    EventChannel,
    MessageType,
    build_asyncapi,
    render_snapshot,
)


class WebSocketAPIContractTest(unittest.TestCase):
    def test_api_3_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/websocket_api_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-3 PASS", result.stdout)


class EventChannelShapeTest(unittest.TestCase):
    def test_every_channel_is_well_formed(self) -> None:
        self.assertGreater(len(EVENT_CHANNELS), 0)
        for event in EVENT_CHANNELS:
            with self.subTest(channel=event.name):
                self.assertIsInstance(event, EventChannel)
                self.assertIsInstance(event.name, Channel)
                self.assertTrue(event.summary, "summary must be non-empty")
                self.assertTrue(event.srs_refs, "srs_refs must be non-empty")
                for ref in event.srs_refs:
                    self.assertIsInstance(ref, str)
                    self.assertTrue(ref)
                self.assertGreater(
                    len(event.payload_fields),
                    0,
                    "payload_fields must be non-empty",
                )
                self.assertGreaterEqual(event.refresh_seconds, 0)
                self.assertLessEqual(event.refresh_seconds, MAX_REFRESH_SECONDS)
                self.assertTrue(event.requires_subscription)


class ChannelCoverageTest(unittest.TestCase):
    def test_every_channel_has_at_least_one_event(self) -> None:
        declared = {event.name for event in EVENT_CHANNELS}
        self.assertEqual(declared, set(Channel))

    def test_all_eight_buckets_present(self) -> None:
        self.assertEqual(len(Channel), 8)


class AsyncAPISnapshotInSyncTest(unittest.TestCase):
    def test_snapshot_byte_equal_to_render(self) -> None:
        self.assertTrue(SNAPSHOT_PATH.exists(), SNAPSHOT_PATH)
        on_disk = SNAPSHOT_PATH.read_text(encoding="utf-8")
        regenerated = render_snapshot()
        if on_disk != regenerated:
            self.fail(
                "asyncapi.json drift; regenerate via "
                "`python3 tools/websocket_api_check.py --update`"
            )

    def test_snapshot_is_deterministic(self) -> None:
        first = render_snapshot()
        second = render_snapshot()
        self.assertEqual(first, second)

    def test_snapshot_parses_as_asyncapi_2_6(self) -> None:
        document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["asyncapi"], "2.6.0")
        self.assertEqual(document["info"]["title"], "ATP Operator WebSocket API")
        self.assertIn("/ws/v1/pnl", document["channels"])
        self.assertIn("/ws/v1/heartbeat", document["channels"])


class LoopbackPolicyTest(unittest.TestCase):
    def test_bind_host_is_loopback(self) -> None:
        self.assertEqual(BIND_HOST, "127.0.0.1")

    def test_auth_model_is_local_single_user(self) -> None:
        self.assertEqual(AUTH_MODEL, "local-single-user")

    def test_ws_path_is_versioned(self) -> None:
        self.assertEqual(WS_PATH, "/ws/v1")

    def test_asyncapi_carries_policy_extensions(self) -> None:
        document = build_asyncapi()
        self.assertEqual(document["x-bind-host"], "127.0.0.1")
        self.assertEqual(document["x-auth-model"], "local-single-user")
        self.assertEqual(document["x-ws-path"], "/ws/v1")


class SubscribeProtocolTest(unittest.TestCase):
    def test_subscribe_and_unsubscribe_route_channels(self) -> None:
        for type_ in (MessageType.SUBSCRIBE, MessageType.UNSUBSCRIBE):
            commands = [c for c in CLIENT_COMMANDS if c.type is type_]
            self.assertTrue(commands, f"{type_.value} command missing")
            for command in commands:
                self.assertIsInstance(command, ClientCommand)
                self.assertIn("channels", command.request_fields)
                self.assertIs(command.response_message, MessageType.ACK)

    def test_heartbeat_ping_pairs_with_pong(self) -> None:
        pings = [c for c in CLIENT_COMMANDS if c.type is MessageType.HEARTBEAT_PING]
        self.assertTrue(pings)
        for command in pings:
            self.assertIs(command.response_message, MessageType.HEARTBEAT_PONG)
            self.assertIn("SYS-39", command.srs_refs)

    def test_refresh_budget_respects_nfr_p2(self) -> None:
        self.assertEqual(MAX_REFRESH_SECONDS, 5)
        for event in EVENT_CHANNELS:
            with self.subTest(channel=event.name):
                self.assertLessEqual(event.refresh_seconds, MAX_REFRESH_SECONDS)


class PublicDocstringsTest(unittest.TestCase):
    def test_every_export_has_docstring(self) -> None:
        for name in atp_ws.__all__:
            with self.subTest(name=name):
                obj = getattr(atp_ws, name)
                if inspect.isclass(obj) or inspect.isfunction(obj):
                    docstring = inspect.getdoc(obj) or ""
                    self.assertTrue(
                        docstring.strip(),
                        f"{name} is missing a docstring",
                    )


if __name__ == "__main__":
    unittest.main()
