"""SRS-ORCH-005 / SyRS SYS-80 / NFR-S2 — strategy rollback — L7 domain (safety) test.

The acceptance's safety core: rolling a strategy back must restore EXACTLY the
retained previous deployed version, and rollback of the LIVE strategy must be
unreachable without the same explicit confirmation control as live promotion —
an unconfirmed live rollback is refused with the deployed state untouched (a
silent or mistargeted rollback of live capital is the hazard).

Three angles:

  1. Behavioral (Rust) — shells the named ``orch_5_rollback_contract`` and
     ``orch_5_cli_fail_closed`` cargo suites (retention semantics, every refusal
     arm side-effect-free, confirmation binding, durable state round-trip).
  2. Behavioral (operator bin) — drives the real ``orch005_rollback_cli`` over a
     temp state file: record v1 -> record v2 -> UNCONFIRMED live rollback refused
     (nonzero exit, state byte-identical) -> CONFIRMED live rollback lands on v1
     -> a second rollback rolls forward to v2.
  3. Surface (runtime dispatch) — mounts ``atp_orchestration.mount_rollback`` on
     a real ``OperatorInterfaceRuntime`` and proves: an unconfirmed REST rollback
     is 428 at the transport guard (the handler is never reached); a confirmed
     REST rollback reaches the real binary and reports the restored version; a
     non-rollback lifecycle action still returns the honest 501 naming
     SRS-ORCH-004; the CLI leg round-trips through the same registry; and one
     live loopback HTTP request exercises the REST leg over a real socket (the
     "dev server requests" evidence).

This is the paired ``tests/domain/`` diff for the safety-critical
orchestrator-lifecycle / deployment-version paths this feature touches.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

pytestmark = [pytest.mark.domain, pytest.mark.safety]

HASH_V1 = "sha256:" + "1" * 64
HASH_V2 = "sha256:" + "2" * 64


def _cargo() -> str | None:
    return shutil.which("cargo")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=ROOT, check=False, capture_output=True, text=True)


def _build_bin(cargo: str) -> Path:
    build = _run(cargo, "build", "-q", "-p", "atp-orchestrator", "--bin", "orch005_rollback_cli")
    assert build.returncode == 0, build.stdout + build.stderr
    return ROOT / "target" / "debug" / "orch005_rollback_cli"


def _seed(binary: Path, state: Path) -> None:
    for source_hash, ts in ((HASH_V1, "100"), (HASH_V2, "200")):
        done = _run(
            str(binary),
            "record",
            "--state",
            str(state),
            "--strategy",
            "alpha-1",
            "--hash",
            source_hash,
            "--observed-at",
            ts,
        )
        assert done.returncode == 0, done.stderr


def test_rust_rollback_suites_pass() -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    for suite in ("orch_5_rollback_contract", "orch_5_cli_fail_closed"):
        result = _run(cargo, "test", "-p", "atp-orchestrator", "--test", suite)
        assert result.returncode == 0, (
            f"{suite} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


def test_live_rollback_is_gated_and_restores_exactly_the_previous_version(
    tmp_path: Path,
) -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    binary = _build_bin(cargo)
    state = tmp_path / "rollback.state"
    _seed(binary, state)
    before = state.read_text()

    # UNCONFIRMED live rollback: refused, nonzero, state byte-identical.
    refused = _run(
        str(binary),
        "rollback",
        "--state",
        str(state),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
        "--live",
        "alpha-1",
    )
    assert refused.returncode != 0
    assert "confirmation" in refused.stderr.lower()
    assert state.read_text() == before, "a refused rollback must not touch the deployed state"

    # CONFIRMED live rollback restores exactly the retained previous version.
    confirmed = _run(
        str(binary),
        "rollback",
        "--state",
        str(state),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
        "--live",
        "alpha-1",
        "--acknowledge",
        "operator confirmed rollback of alpha-1",
        "--observed-at",
        "300",
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert f"rolled-back-to:{HASH_V1}@300" in confirmed.stdout
    assert "was-live:true" in confirmed.stdout

    # A SECOND rollback (naming v2, now the retained previous) rolls forward.
    forward = _run(
        str(binary),
        "rollback",
        "--state",
        str(state),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V2,
        "--observed-at",
        "400",
    )
    assert forward.returncode == 0, forward.stderr
    assert f"rolled-back-to:{HASH_V2}@400" in forward.stdout


def test_runtime_surfaces_dispatch_rollback_with_confirmation_parity(tmp_path: Path) -> None:
    cargo = _cargo()
    if cargo is None:
        pytest.skip("cargo not on PATH")
    binary = _build_bin(cargo)
    state = tmp_path / "rollback.state"
    _seed(binary, state)

    from atp_orchestration import mount_rollback
    from atp_runtime import OperatorInterfaceRuntime

    runtime = OperatorInterfaceRuntime()
    mount_rollback(runtime, state_path=state, binary=binary)
    lifecycle_path = "/api/v1/strategies/alpha-1/lifecycle"

    def post(payload: dict) -> tuple[int, dict]:
        return runtime.dispatch_rest("POST", lifecycle_path, json.dumps(payload).encode())

    # (1) UNCONFIRMED REST rollback: 428 at the transport guard — the handler (and
    # therefore the binary) is never reached; the same control live promotion uses.
    status, body = post({"action": "rollback", "target_version_hash": HASH_V1})
    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"

    # (2) Non-rollback lifecycle actions keep their honest 501 naming SRS-ORCH-004.
    status, body = post({"action": "restart", "confirm": True})
    assert status == 501
    assert body["error"]["detail"]["owner"] == "SRS-ORCH-004"

    # (3) CONFIRMED REST rollback reaches the real binary and reports the restored version.
    status, body = post({"action": "rollback", "target_version_hash": HASH_V1, "confirm": True})
    assert status == 200, body
    assert body["lifecycle_state"] == "rolled-back"
    assert body["deployment_version_hash"] == HASH_V1
    assert body["rolled_back_from"] == HASH_V2

    # (4) The CLI leg round-trips through the same registry: rolling forward to v2.
    code = runtime.cli_dispatcher().dispatch(
        ["strategy", "rollback", "alpha-1", "--target-version-hash", HASH_V2, "--confirm"]
    )
    assert code == 0

    # (5) A MISTARGETED rollback (naming the now-current v2's replaced... i.e. the
    # current version itself) maps to a structured BAD_REQUEST naming the retained hash.
    status, body = post({"action": "rollback", "target_version_hash": HASH_V2, "confirm": True})
    assert status == 400
    assert body["error"]["type"] == "TARGET_MISMATCH"
    assert HASH_V1 in body["error"]["message"]

    # (6) One live loopback HTTP request — the REST leg over a real socket.
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        request = urllib.request.Request(
            f"http://{host}:{port}{lifecycle_path}",
            data=json.dumps(
                {"action": "rollback", "target_version_hash": HASH_V1, "confirm": True}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            payload = json.loads(response.read())
        assert payload["lifecycle_state"] == "rolled-back"
        assert payload["deployment_version_hash"] == HASH_V1
    finally:
        runtime.stop()


def test_rollback_contract_is_registered() -> None:
    # Structural: the contract block + check script are wired so CI gates on them.
    tools_root = ROOT / "tools"
    if str(tools_root) not in sys.path:
        sys.path.insert(0, str(tools_root))
    from orchestrator_rollback_check import assert_rollback_static, contract_block, load_config

    config = load_config()
    block = contract_block(config)
    assert block["requirement"] == "SRS-ORCH-005"
    assert len(assert_rollback_static(config, ROOT)) == 5
