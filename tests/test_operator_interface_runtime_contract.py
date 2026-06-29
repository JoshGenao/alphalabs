"""L3 contract — the runtime serves the full declared operator surface.

Where the per-surface checks (rest_api / cli / websocket) and the cross-surface
``operator_workflow_surface`` check verify the *declarative* contract, this
suite verifies the *runtime* honours it: every declared REST route and CLI
command is reachable (matched + dispatched, never a 404/405/usage error), the
runtime-owned operations answer with real data, every owner is accounted for,
and the WebSocket channel set matches the declared channels.

SRS trace: SRS-API-001.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from atp_api import ROUTES
from atp_cli import COMMANDS
from atp_runtime import VALID_CHANNELS, OperatorInterfaceRuntime, validate_owners
from atp_runtime.contract import load_contract_block
from atp_ws import EVENT_CHANNELS

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runtime() -> OperatorInterfaceRuntime:
    return OperatorInterfaceRuntime()


def _concrete_path(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "sample", template)


def _argv_for(command) -> list[str]:
    argv = [command.group.value]
    if command.name:
        argv.append(command.name)
    for argument in command.arguments:
        if not argument.required:
            continue
        if argument.is_flag:
            argv.append(argument.name)
        elif argument.name.startswith("--"):
            argv += [argument.name, "sample"]
        else:
            argv.append("sample")
    return argv


@pytest.mark.parametrize("route", ROUTES, ids=lambda r: f"{r.method.value} {r.path}")
def test_every_rest_route_is_served(runtime, route):
    path = _concrete_path(route.path)
    if route.requires_confirmation:
        path = f"{path}?confirm=true"
    status, _ = runtime.dispatch_rest(route.method.value, path, b"{}")
    # Matched + dispatched: runtime-owned (200) or deferred (501) — never a
    # routing miss (404) or method mismatch (405).
    assert status in (200, 501), f"{route.method.value} {route.path} -> {status}"


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c.invocation)
def test_every_cli_command_is_dispatched(runtime, command):
    dispatcher = runtime.cli_dispatcher()
    try:
        code = dispatcher.dispatch(_argv_for(command), stdout=io.StringIO())
    except SystemExit as exc:  # argparse usage error => argv build is wrong
        pytest.fail(f"{command.invocation} raised SystemExit({exc.code})")
    # Runtime-owned (0), readiness-not-ready (5), or deferred NOT_IMPLEMENTED
    # (64) — every declared command dispatches; none is a usage error (2).
    assert code in (0, 5, 64), f"{command.invocation} -> exit {code}"


def test_runtime_owned_operations_return_real_data(runtime):
    status, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    assert status == 200 and "workflows" in body

    out = io.StringIO()
    assert runtime.cli_dispatcher().dispatch(["admin", "version", "--json"], stdout=out) == 0
    assert json.loads(out.getvalue())["runtime_version"]

    # Config view redacts every secret key.
    out = io.StringIO()
    runtime.cli_dispatcher().dispatch(["admin", "config", "--json"], stdout=out)
    keys = json.loads(out.getvalue())["keys"]
    assert keys, "config catalogue is empty"
    assert all(k["value"] == "***REDACTED***" for k in keys if k["secret"])


def test_owner_map_is_validated_against_the_contract():
    block = load_contract_block(ROOT)
    # raises on any unmapped/untracked REST / CLI / WebSocket owner — every owner
    # must be `runtime` or a feature named in the contract deferred[]
    validate_owners(block, ROUTES, COMMANDS, EVENT_CHANNELS)


def test_validate_owners_rejects_an_owner_absent_from_deferred():
    # An owner that is neither `runtime` nor in deferred[] must be rejected, so a
    # new surface cannot land attributed to an untracked feature.
    block = {"deferred": [{"feature": "SRS-EXE-001", "what": "x"}]}
    with pytest.raises(ValueError):
        validate_owners(block, ROUTES, COMMANDS, EVENT_CHANNELS)


def test_websocket_channel_set_matches_declared_channels():
    assert VALID_CHANNELS == frozenset(c.name.value for c in EVENT_CHANNELS)


def test_logs_workflow_deferred_owner_is_exactly_srs_log_001(runtime):
    """The LOGS workflow must depend only on SRS-LOG-001 — not on the runtime
    feature itself (no self-defer) and not on SRS-NOTIF-001 (the shared `admin`
    group's `alerts` command is a different, non-required workflow)."""

    _, body = runtime.dispatch_rest("GET", "/api/v1/system/status")
    logs = next(w for w in body["workflows"] if w["id"] == "LOGS")
    assert logs["deferred_owners"] == ["SRS-LOG-001"]
    # The shared admin group contributes only `admin logs`: REST logs + CLI
    # admin-logs + LOGS WS channel = 3 operations, not the whole admin group.
    assert logs["total_operations"] == 3


def test_websocket_event_envelope_matches_declared_asyncapi_shape():
    """The runtime's EVENT frame must use the field names the AsyncAPI contract
    declares as required (`{type, channel, data}`), so a client generated from
    the published API-3 contract can read event bodies from this runtime."""

    from atp_runtime.ws_frames import decode_frame
    from atp_runtime.ws_protocol import WsSession
    from atp_ws import build_asyncapi

    # Pull the declared required fields for an EVENT message off the AsyncAPI doc.
    required: set[str] | None = None
    for channel in build_asyncapi()["channels"].values():
        payload = channel.get("subscribe", {}).get("message", {}).get("payload", {})
        if payload.get("properties", {}).get("type", {}).get("const") == "EVENT":
            required = set(payload["required"])
            break
    assert required == {"type", "channel", "data"}

    # The runtime emits exactly those fields.
    sent: list[bytes] = []
    session = WsSession(sent.append)
    session.handle_text(json.dumps({"type": "SUBSCRIBE", "channels": ["LOGS"]}))
    sent.clear()
    assert session.deliver("LOGS", {"value": 1}) is True
    frame, _ = decode_frame(sent[0], require_mask=False)
    assert frame is not None
    assert set(json.loads(frame.text).keys()) == required


def test_documented_cli_entrypoint_runs_the_real_dispatcher_in_a_subprocess():
    """`python -m atp_runtime` (the working operator CLI) must dispatch through
    the runtime — runtime-owned commands return real data + exit codes, not the
    `python -m atp_cli` API-4 stub's NOT_IMPLEMENTED."""

    env = {**os.environ, "PYTHONPATH": str(ROOT / "python")}

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "atp_runtime", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )

    version = run(["admin", "version", "--json"])
    assert version.returncode == 0, version.stderr
    assert json.loads(version.stdout)["component"] == "atp-operator-interface-runtime"

    readiness = run(["readiness", "check", "--json"])
    assert readiness.returncode == 5  # NOT_READY while domain workflows deferred
    assert json.loads(readiness.stdout)["ready"] is False

    kill_switch = run(["kill-switch", "activate"])
    assert kill_switch.returncode == 3  # CONFIRMATION_REQUIRED, never dispatched

    deferred = run(["strategy", "list", "--json"])
    assert deferred.returncode == 64  # NOT_IMPLEMENTED deferred envelope
    assert json.loads(deferred.stdout)["error"]["detail"]["owner"] == "SRS-ORCH-004"


def test_runtime_contract_evidence_script_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "operator_interface_runtime_check.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OPERATOR INTERFACE RUNTIME" in result.stdout.upper()
