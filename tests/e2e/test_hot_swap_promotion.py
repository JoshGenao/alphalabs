"""L6 e2e — ``SRS-RESV-005``: promote a paper strategy to live only after successful demotion.

The REST + durable-state leg of the acceptance evidence (feature Step 2 / Step 3). The AC:

    The promoted strategy starts live with no open IB positions, preserves prior paper
    performance history, and uses the same strategy code/API behavior.

**Nothing here is faked below the transport.** A real ``sim004_persist_cli`` writes a real
SRS-SIM-004 paper snapshot; the real ``mount_hot_swap_execution`` composition serves
``POST /api/v1/hot-swap`` over a real HTTP server; the handler shells the real
``resv005_hot_swap_promote_cli``, which drives the real ``execute_hot_swap`` gate over the
real ``LiveDesignation`` authority. What the operator POSTs is what the durable record says
afterwards, and the assertions check the record, not the response.

Two fixtures remain, and they are the ones whose producers are genuinely deferred: the IB
position feed (SRS-EXE-006) and the deployed-version registry (SRS-ORCH-004). The binary
labels every run ``transports:FIXTURE`` for exactly that reason.

**Status: written, NOT run in the session that authored it.** It binds an HTTP port and
shells cargo, which the parallel-agent protocol forbids alongside sibling agents. The
browser half of Step 2 is additionally blocked on ``SRS-RESV-002``: the UI-5 promote control
is inert until the Reservoir ranking names a candidate, so there is no button to click yet.
That case is present and skipped with an explicit reason rather than omitted, so the gap is
visible in the test report instead of only in a note.

Gated: ``pytest -m "not e2e"`` skips it; runs under ``ATP_RUN_E2E=1`` with cargo on PATH.
"""

from __future__ import annotations

import http.client
import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from atp_orchestration import mount_hot_swap_execution
from atp_runtime import OperatorInterfaceRuntime

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PROMOTE_BIN = "resv005_hot_swap_promote_cli"
PERSIST_BIN = "sim004_persist_cli"
DEMOTING = "reservoir-a"
CANDIDATE = "reservoir-b"
STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    build = subprocess.run(
        [
            cargo, "build",
            "-p", "atp-orchestrator", "--bin", PROMOTE_BIN,
            "-p", "atp-simulation", "--bin", PERSIST_BIN,
        ],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stderr
    return {name: ROOT / "target" / "debug" / name for name in (PROMOTE_BIN, PERSIST_BIN)}


@pytest.fixture
def live_stack(binaries, tmp_path) -> Iterator[tuple[tuple[str, int], Path, Path, Path]]:
    """A real runtime serving the real handler over the real binary."""
    paper = tmp_path / "paper"
    paper.mkdir()
    seeded = subprocess.run(
        [str(binaries[PERSIST_BIN]), "persist", "--dir", str(paper)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "live.state"
    state.write_text(f"{STATE_MAGIC}\ndesignated\t{DEMOTING}\n")
    journal = tmp_path / "swaps.jsonl"

    runtime = OperatorInterfaceRuntime()
    mount_hot_swap_execution(
        runtime,
        state_path=state,
        paper_state_dir=paper,
        log_path=journal,
        # Declared DRILL: the flat-account and code-identity producers are deferred
        # (SRS-EXE-006 / SRS-ORCH-004), and an undeclared composition refuses.
        fixture_safety_inputs={
            "positions": "flat",
            "deployed_version": "sha256:" + "a" * 64,
            "liquidation": "flat",
        },
        binary=binaries[PROMOTE_BIN],
    )
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield (host, port), state, paper, journal
    finally:
        runtime.stop()


def _post(where: tuple[str, int], path: str, body: dict) -> tuple[int, dict]:
    host, port = where
    conn = http.client.HTTPConnection(host, port, timeout=120)
    try:
        payload = json.dumps(body).encode()
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


def _designated(binaries, state: Path) -> str:
    """The live strategy, read in a SEPARATE process from the durable record."""
    result = subprocess.run(
        [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key == "designated":
            return value
    raise AssertionError(f"no `designated` line in: {result.stdout!r}")


def test_the_rest_route_promotes_and_the_durable_record_agrees(binaries, live_stack):
    where, state, paper, journal = live_stack
    snapshot = paper / "paper_sim_state.snapshot"
    history_before = snapshot.read_bytes()

    status, body = _post(where, "/api/v1/hot-swap?confirm=true",
                         {"candidate_strategy_id": CANDIDATE, "confirm": True})

    assert status == 200
    assert body["promotion_state"] == "PROMOTED"
    assert body["demotion_state"] == "DEMOTED"
    assert isinstance(body["swap_id"], str) and body["swap_id"]

    # AC clause 1+3 are enforced inside the gate; the two clauses observable from
    # OUT here are the durable ones:
    #   - the promotion actually took effect, in another process;
    assert _designated(binaries, state) == CANDIDATE
    #   - and the prior paper performance history is byte-identical on disk.
    assert snapshot.read_bytes() == history_before

    # swap_id addresses a real record, not a counter the response invented.
    lines = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    ordinal = int(body["swap_id"].removeprefix("sw-"))
    assert 1 <= ordinal <= len(lines)
    record = lines[ordinal - 1]
    assert record["event_type"] == "PROMOTION"
    assert record["promoted"] is True
    assert record["candidate_strategy_id"] == CANDIDATE


def test_a_blocked_swap_leaves_the_live_strategy_untouched(binaries, live_stack, tmp_path):
    where, state, _paper, _journal = live_stack

    # A candidate with no paper history cannot evidence "preserves prior paper
    # performance history", so the gate must refuse — and refuse without moving
    # the live designation.
    status, body = _post(where, "/api/v1/hot-swap?confirm=true",
                         {"candidate_strategy_id": "never-ran-as-paper", "confirm": True})

    assert status == 200, "the gate RAN; a non-2xx would tell the pane nothing mutated"
    assert body["promotion_state"] == "BLOCKED"
    assert _designated(binaries, state) == DEMOTING


def test_an_unconfirmed_swap_is_refused_before_anything_runs(binaries, live_stack):
    where, state, _paper, journal = live_stack

    status, body = _post(where, "/api/v1/hot-swap",
                         {"candidate_strategy_id": CANDIDATE})

    assert status == 428
    assert body["error"]["category"] == "CONFIRMATION_REQUIRED"
    assert _designated(binaries, state) == DEMOTING
    assert not journal.exists(), "an unconfirmed swap must not journal an attempt"


@pytest.mark.skip(reason="UI-5's promote control is inert until SRS-RESV-002's Reservoir "
                         "ranking names a candidate, so there is no armed button to drive. "
                         "Unskip with the browser walk once SRS-RESV-002 lands.")
def test_the_dashboard_promote_control_drives_the_swap() -> None:
    raise NotImplementedError(
        "Browser walk: load /dashboard, wait for the UI-5 Hot-Swap pane to resolve a "
        "candidate and an explicit demotion_pending:false, arm and confirm the promote "
        "control, then assert the pane reports the swap_id and that the durable status "
        "read agrees the candidate is live."
    )
