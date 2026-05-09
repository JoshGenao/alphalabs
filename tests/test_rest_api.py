from __future__ import annotations

import inspect
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
SNAPSHOT_PATH = ROOT / "python" / "atp_api" / "openapi.json"

if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

import atp_api  # noqa: E402
from atp_api import (  # noqa: E402
    AUTH_MODEL,
    BIND_HOST,
    ROUTES,
    Capability,
    Method,
    Route,
    build_openapi,
    render_snapshot,
)


class RestAPIContractTest(unittest.TestCase):
    def test_api_2_contract_script_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, "tools/rest_api_check.py"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("API-2 PASS", result.stdout)


class RouteShapeTest(unittest.TestCase):
    def test_every_route_is_well_formed(self) -> None:
        self.assertGreater(len(ROUTES), 0)
        for route in ROUTES:
            with self.subTest(path=route.path, method=route.method):
                self.assertIsInstance(route, Route)
                self.assertIsInstance(route.method, Method)
                self.assertIsInstance(route.capability, Capability)
                self.assertTrue(route.path.startswith("/api/v1/"), route.path)
                self.assertTrue(route.summary, "summary must be non-empty")
                self.assertTrue(route.srs_refs, "srs_refs must be non-empty")
                for ref in route.srs_refs:
                    self.assertIsInstance(ref, str)
                    self.assertTrue(ref, "srs_refs entry must be non-empty")


class CapabilityCoverageTest(unittest.TestCase):
    def test_every_capability_has_at_least_one_route(self) -> None:
        declared = {route.capability for route in ROUTES}
        self.assertEqual(declared, set(Capability))

    def test_all_eleven_buckets_present(self) -> None:
        self.assertEqual(len(Capability), 11)


class OpenAPISnapshotInSyncTest(unittest.TestCase):
    def test_snapshot_byte_equal_to_render(self) -> None:
        self.assertTrue(SNAPSHOT_PATH.exists(), SNAPSHOT_PATH)
        on_disk = SNAPSHOT_PATH.read_text(encoding="utf-8")
        regenerated = render_snapshot()
        if on_disk != regenerated:
            self.fail(
                "openapi.json drift; regenerate via "
                "`python3 tools/rest_api_check.py --update`"
            )

    def test_snapshot_is_deterministic(self) -> None:
        first = render_snapshot()
        second = render_snapshot()
        self.assertEqual(first, second)

    def test_snapshot_parses_as_openapi_3_1(self) -> None:
        document = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(document["openapi"], "3.1.0")
        self.assertEqual(document["info"]["title"], "ATP Operator REST API")
        self.assertIn("/api/v1/kill-switch", document["paths"])


class LoopbackPolicyTest(unittest.TestCase):
    def test_bind_host_is_loopback(self) -> None:
        self.assertEqual(BIND_HOST, "127.0.0.1")

    def test_auth_model_is_local_single_user(self) -> None:
        self.assertEqual(AUTH_MODEL, "local-single-user")

    def test_openapi_carries_policy_extensions(self) -> None:
        document = build_openapi()
        self.assertEqual(document["x-bind-host"], "127.0.0.1")
        self.assertEqual(document["x-auth-model"], "local-single-user")


class ConfirmationGuardTest(unittest.TestCase):
    def test_kill_switch_requires_confirmation(self) -> None:
        kill_routes = [
            route for route in ROUTES if route.capability is Capability.KILL_SWITCH
        ]
        self.assertTrue(kill_routes)
        for route in kill_routes:
            self.assertTrue(
                route.requires_confirmation,
                f"{route.method} {route.path} must require confirmation",
            )
            self.assertIn("confirm", route.request_fields)

    def test_live_designation_requires_confirmation(self) -> None:
        live_routes = [
            route for route in ROUTES if route.capability is Capability.LIVE_DESIGNATION
        ]
        self.assertTrue(live_routes)
        for route in live_routes:
            self.assertTrue(route.requires_confirmation)
            self.assertIn("confirm", route.request_fields)


class PublicDocstringsTest(unittest.TestCase):
    def test_every_export_has_docstring(self) -> None:
        for name in atp_api.__all__:
            with self.subTest(name=name):
                obj = getattr(atp_api, name)
                if inspect.isclass(obj) or inspect.isfunction(obj):
                    docstring = inspect.getdoc(obj) or ""
                    self.assertTrue(
                        docstring.strip(),
                        f"{name} is missing a docstring",
                    )


if __name__ == "__main__":
    unittest.main()
