"""L4 boundary — the SRS-EXE-001 live-designation surface on the operator runtime.

Drives ``POST /api/v1/strategies/{strategy_id}/promote-live``, ``live promote`` and
``live show`` through the REAL runtime dispatchers (the same ones the HTTP server and
``python -m atp_runtime`` use) and the REAL ``exe001_live_designation_cli``, against a
real durable snapshot on disk. Nothing in the chain is a double except the snapshot's
directory.

Also proves the arm is SHIPPED, not only mountable: ``serve()``'s composition helper
mounts it on its own opt-in knob, refuses a malformed knob, and refuses to come up
without the shared designation snapshot.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from atp_api.routes import ROUTES
from atp_dashboard.server import _mount_live_designation_arm
from atp_orchestration import mount_live_designation
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.boundary

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = "exe001_live_designation_cli"
PROMOTE = "/api/v1/strategies/{}/promote-live"
STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"


@pytest.fixture(scope="module")
def binary() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH; cannot build the designation binary")
    build = subprocess.run(
        [cargo, "build", "-p", "atp-orchestrator", "--bin", BIN],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    return REPO_ROOT / "target" / "debug" / BIN


@pytest.fixture()
def runtime(binary: Path, tmp_path: Path) -> tuple[OperatorInterfaceRuntime, Path]:
    state = tmp_path / "designation"
    rt = OperatorInterfaceRuntime()
    mount_live_designation(rt, state_path=state, binary=binary)
    return rt, state


def _cli(rt: OperatorInterfaceRuntime, *argv: str) -> tuple[int, dict | str]:
    """Run one CLI command. ``live show`` declares ``--json``; ``live promote`` does
    not, and prints a success body as indented JSON and an error as one text line."""

    out = io.StringIO()
    json_flag = ["--json"] if argv[:2] == ("live", "show") else []
    code = rt.cli_dispatcher().dispatch([*argv, *json_flag], stdout=out)
    text = out.getvalue()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text.strip()


def _designated(state: Path) -> str | None:
    if not state.exists():
        return None
    lines = state.read_text().splitlines()
    assert lines[0] == STATE_MAGIC
    ids = [line.split("\t", 1)[1] for line in lines[1:] if line.startswith("designated\t")]
    return ids[0] if ids else None


def test_promote_live_requires_confirmation_and_writes_nothing_without_it(runtime) -> None:
    rt, state = runtime
    status, body = rt.dispatch_rest("POST", PROMOTE.format("live-a"))
    assert status == 428, body
    assert not state.exists()


def test_a_confirmed_promote_designates_the_strategy_durably(runtime) -> None:
    rt, state = runtime
    status, body = rt.dispatch_rest("POST", PROMOTE.format("live-a") + "?confirm=true")
    assert status == 200, body
    assert body["strategy_id"] == "live-a"
    assert body["is_live"] is True
    assert isinstance(body["promoted_at"], str) and body["promoted_at"]
    assert body["warning"] is None
    assert _designated(state) == "live-a"

    # Re-promoting the live strategy moves nothing, so it claims no promotion time.
    status, again = rt.dispatch_rest("POST", PROMOTE.format("live-a") + "?confirm=true")
    assert status == 200, again
    assert again["promoted_at"] is None


def test_a_second_live_strategy_is_refused_and_the_first_stays_live(runtime) -> None:
    rt, state = runtime
    assert rt.dispatch_rest("POST", PROMOTE.format("live-a") + "?confirm=true")[0] == 200
    before = state.read_bytes()
    status, body = rt.dispatch_rest("POST", PROMOTE.format("paper-1") + "?confirm=true")
    assert status == 400, body
    assert body["error"]["type"] == "LIVE_STRATEGY_ALREADY_DESIGNATED", body
    assert "Hot-Swap" in body["error"]["message"]
    assert state.read_bytes() == before


def test_invalid_ids_and_unknown_body_fields_are_refused_before_the_binary(runtime) -> None:
    rt, state = runtime
    status, body = rt.dispatch_rest("POST", PROMOTE.format("-bad") + "?confirm=true")
    assert status == 400 and body["error"]["type"] == "INVALID_STRATEGY_ID", body
    status, body = rt.dispatch_rest(
        "POST", PROMOTE.format("live-a") + "?confirm=true", json.dumps({"force": True}).encode()
    )
    assert status == 400 and body["error"]["type"] == "UNKNOWN_REQUEST_FIELD", body
    assert not state.exists()


def test_cli_promote_and_show_share_the_snapshot_with_rest(runtime) -> None:
    rt, state = runtime
    code, body = _cli(rt, "live", "show")
    assert code == 0 and body == {"designated": None}, body

    code, _ = _cli(rt, "live", "promote", "live-a")
    assert code == 3, "an unconfirmed CLI promote must exit CONFIRMATION_REQUIRED"
    assert not state.exists()

    code, body = _cli(rt, "live", "promote", "live-a", "--confirm")
    assert code == 0 and body["strategy_id"] == "live-a", body
    code, body = _cli(rt, "live", "show")
    assert code == 0 and body == {"designated": "live-a"}, body

    code, body = _cli(rt, "live", "promote", "paper-1", "--confirm")
    assert code == 2, body  # USAGE_ERROR: a second live strategy is a Hot-Swap
    assert "already the designated live strategy" in str(body)
    assert _designated(state) == "live-a"


def test_an_unreadable_snapshot_is_not_reported_as_nobody_live(runtime) -> None:
    rt, state = runtime
    state.write_text("not a designation snapshot\n")
    code, body = _cli(rt, "live", "show")
    assert code != 0, body
    assert body["error"]["type"] == "LIVE_DESIGNATION_UNREADABLE", body


def test_the_workflow_is_fully_served_once_mounted(runtime) -> None:
    rt, _ = runtime
    workflow = next(w for w in rt.status_snapshot()["workflows"] if w["id"] == "LIVE_DESIGNATION")
    assert workflow["fully_served"] is True, workflow
    bare = OperatorInterfaceRuntime()
    status, body = bare.dispatch_rest("POST", PROMOTE.format("live-a") + "?confirm=true")
    assert status == 501 and "SRS-EXE-001" in json.dumps(body), body


def test_the_documented_response_matches_what_the_handler_returns(runtime) -> None:
    rt, _ = runtime
    route = next(r for r in ROUTES if r.path.endswith("/promote-live"))
    status, body = rt.dispatch_rest("POST", PROMOTE.format("live-a") + "?confirm=true")
    assert status == 200
    assert set(body) == set(route.response_fields)
    python_types = {"string": str, "boolean": bool}
    for name, declared in route.field_types:
        if name not in body:
            continue  # a request field
        allowed = tuple(type(None) if t == "null" else python_types[t] for t in declared.split("|"))
        assert isinstance(body[name], allowed), (name, declared, body[name])


# --------------------------------------------------------------------------- #
# The shipped composition
# --------------------------------------------------------------------------- #


def test_serve_mounts_the_arm_only_on_its_own_opt_in(tmp_path: Path) -> None:
    state = str(tmp_path / "designation")
    off = OperatorInterfaceRuntime()
    _mount_live_designation_arm(off, {"ATP_HOT_SWAP_DESIGNATION_STATE": state})
    assert off.dispatch_rest("POST", PROMOTE.format("a") + "?confirm=true")[0] == 501

    on = OperatorInterfaceRuntime()
    _mount_live_designation_arm(
        on, {"ATP_LIVE_DESIGNATION_ROUTES": "1", "ATP_HOT_SWAP_DESIGNATION_STATE": state}
    )
    workflow = next(w for w in on.status_snapshot()["workflows"] if w["id"] == "LIVE_DESIGNATION")
    assert workflow["fully_served"] is True


def test_serve_refuses_a_malformed_opt_in_or_a_missing_snapshot() -> None:
    with pytest.raises(ValueError, match="must be 1 or unset"):
        _mount_live_designation_arm(
            OperatorInterfaceRuntime(), {"ATP_LIVE_DESIGNATION_ROUTES": "yes"}
        )
    with pytest.raises(ValueError, match="ATP_HOT_SWAP_DESIGNATION_STATE"):
        _mount_live_designation_arm(
            OperatorInterfaceRuntime(), {"ATP_LIVE_DESIGNATION_ROUTES": "1"}
        )
