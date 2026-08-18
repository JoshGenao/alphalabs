"""L6 e2e — ``SRS-RESV-004``: Hot-Swap demotion before promotion, in the browser.

The Step 2 leg of the acceptance evidence: exercise the requirement with browser automation
against the dashboard, using the operator controls the requirement implies.

Nothing here is faked. A real ``resv004_hot_swap_demotion_cli`` runs a real demotion — the real
SYS-49b sequence, the real flat-confirmation probe, the real ``resolve_demotion`` gate, the real
SRS-NOTIF-001 dispatcher — and writes a real durable demotion-pending lockout. The production
``mount_default_dashboard`` composition reads that same file through the shipped env knob, and a
real headless browser renders the result. The only fixtures are the IB socket and the SMTP/push
transports (the deferred ``atp-adapters`` / SRS-NOTIF-001 legs).

Driving the SHIPPED composition matters: a fixture that mounted the provider by hand would prove
its own wiring rather than the one an operator gets. ``serve()`` failing to mount a route is a
real, shipped bug that every hand-mounted test misses (RESV-003 r7).

Gated: ``pytest -m "not e2e"`` skips it; runs under ``ATP_RUN_E2E=1`` with Playwright browsers
installed (``playwright install chromium``) and cargo on PATH. The server binds an EPHEMERAL
port, so it does not collide with a sibling agent's dashboard.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

# Guard collection: the import must not error when Playwright is absent — the collection-time
# skip in conftest runs *after* module import.
sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import mount_default_dashboard  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]

DEMOTING = "live-momentum"
CANDIDATE = "paper-reversal"


@pytest.fixture(scope="module")
def demotion_binary() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH; cannot build the demotion CLI")
    build = subprocess.run(
        [cargo, "build", "-q", "-p", "atp-orchestrator", "--bin", "resv004_hot_swap_demotion_cli"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stdout}\n{build.stderr}"
    return REPO_ROOT / "target" / "debug" / "resv004_hot_swap_demotion_cli"


@pytest.fixture()
def live_dashboard(demotion_binary: Path, tmp_path: Path) -> Iterator[tuple[str, Path, Path]]:
    """The PRODUCTION composition, reading a real durable demotion-pending lockout.

    Yields ``(dashboard_url, state_path, binary)``. The env knob is the ONLY wiring, exactly as
    an operator's process would configure it.
    """

    state = tmp_path / "demotion-pending.json"
    env = {"ATP_HOT_SWAP_DEMOTION_STATE": str(state)}
    runtime = OperatorInterfaceRuntime()
    publisher = mount_default_dashboard(runtime, env)
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", state, demotion_binary
    finally:
        publisher.stop()
        runtime.stop()


def _cli(binary: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _open_pane(page, url: str) -> None:
    """Load the dashboard and wait for the Hot-Swap pane's FIRST poll to land.

    The rungs exist from first paint, rendered deferred until a snapshot arrives, so reading
    them straight after ``goto`` samples the pre-fetch placeholder rather than the state under
    test. Waiting on the response itself keeps this deterministic without a sleep.
    """

    with page.expect_response(
        lambda response: "/dashboard/api/hot-swap" in response.url, timeout=20_000
    ):
        page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector('#hs-track .hs__rung[data-phase="demotion_pending"]', timeout=15_000)


def _await_note_tone(page, expected: str) -> str:
    """Wait for the pane's own poll to bring the note to ``expected``, return its text.

    The pane refreshes itself within the NFR-P2 budget, so a lockout engaged out-of-band
    reaches the browser without a reload — which is how an operator would actually see it.
    """

    page.wait_for_function(
        """(expected) => {
             const note = document.getElementById("hs-note");
             return !!note && note.dataset.tone === expected;
           }""",
        arg=expected,
        timeout=25_000,
    )
    return page.eval_on_selector("#hs-note", "el => el.textContent")


def _await_note_text(page, needle: str) -> str:
    """Wait for the note to CONTAIN ``needle``, return its text.

    Used where the tone alone cannot prove a transition — an unreadable lockout and a held one
    are both error-toned, so waiting on the tone would return the previous state immediately
    and assert nothing.
    """

    page.wait_for_function(
        """(needle) => {
             const note = document.getElementById("hs-note");
             return !!note && note.textContent.includes(needle);
           }""",
        arg=needle,
        timeout=25_000,
    )
    return page.eval_on_selector("#hs-note", "el => el.textContent")


def _rung_status(page, phase: str) -> str:
    return page.eval_on_selector(
        f'#hs-track .hs__rung[data-phase="{phase}"]', "el => el.dataset.status"
    )


def test_resv_004_demotion_before_promotion_end_to_end(live_dashboard) -> None:
    """The whole acceptance criterion, in the browser, with nothing faked."""

    url, state, binary = live_dashboard

    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        try:
            # ---- No lockout: a RESOLVED "nothing is pending" ------------------- #
            # Non-vacuous: the cell reads a real false from a mounted producer, not an absent
            # value. Before this feature the same pane rendered this cell deferred.
            _open_pane(page, url)
            note = _await_note_tone(page, "ok")
            assert "no demotion is pending" in note, note
            assert _rung_status(page, "demotion_pending") == "UNKNOWN"

            # ---- A demotion that reaches flat completes, and stays unblocked ---- #
            flat = _cli(
                binary,
                "demote",
                "--demoting",
                DEMOTING,
                "--candidate",
                CANDIDATE,
                "--state",
                str(state),
                "--expect",
                "flat",
                "--position",
                "AAPL:100",
                "--resting",
                "AAPL",
                "--flat-after-seconds",
                "12",
            )
            assert flat.returncode == 0, f"{flat.stdout}\n{flat.stderr}"
            assert "paper-transitions:1" in flat.stdout
            assert not state.exists(), "a flat demotion must leave NO demotion-pending lockout"

            # ---- SYS-49c: a liquidation timeout, seen by the operator ---------- #
            # The positions never reach flat, so the real 60 s wait elapses on the simulated
            # clock, the unfilled order is cancelled, email + push go out, and the lockout is
            # engaged. The browser must show it without a reload.
            timeout = _cli(
                binary,
                "demote",
                "--demoting",
                DEMOTING,
                "--candidate",
                CANDIDATE,
                "--state",
                str(state),
                "--expect",
                "demotion-pending",
                "--position",
                "AAPL:100",
                "--resting",
                "AAPL",
            )
            assert timeout.returncode == 0, f"{timeout.stdout}\n{timeout.stderr}"
            assert "unfilled-order-cancels:1" in timeout.stdout
            assert "operator-page-delivered-email:true" in timeout.stdout
            assert "operator-page-delivered-push:true" in timeout.stdout
            assert state.exists(), "the demotion-pending lockout must have reached disk"

            note = _await_note_tone(page, "error")
            assert "DEMOTION-PENDING" in note, note
            assert "promotion is blocked until manual resolution" in note, note
            # The changeover ladder's timeout branch now reads BLOCKED, not UNKNOWN.
            assert _rung_status(page, "demotion_pending") == "BLOCKED"

            # ---- Promotion stays blocked, even for a swap that would go flat --- #
            blocked = _cli(
                binary,
                "demote",
                "--demoting",
                DEMOTING,
                "--candidate",
                CANDIDATE,
                "--state",
                str(state),
                "--expect",
                "blocked-pending",
                "--position",
                "AAPL:100",
                "--resting",
                "AAPL",
                "--flat-after-seconds",
                "1",
            )
            assert blocked.returncode == 0, f"{blocked.stdout}\n{blocked.stderr}"
            assert "sequence-ran:false" in blocked.stdout
            assert "error-type:HotSwapDemotionPending" in blocked.stdout

            # ---- A corrupt lockout renders UNKNOWN, never "none pending" ------- #
            # Both this and a held lockout are error-toned, so the transition is asserted on
            # the TEXT: the pane must name the read failure rather than quietly showing a
            # clean snapshot — and it must not claim either "pending" or "none pending".
            state.write_text('{"magic":"NOT-OURS"}\n', encoding="utf-8")
            note = _await_note_text(page, "UNREADABLE")
            assert "no demotion is pending" not in note, note
            assert "DEMOTION-PENDING —" not in note, note
            assert page.eval_on_selector("#hs-note", "el => el.dataset.tone") == "error"
            # The timeout rung drops back to UNKNOWN: an unreadable lockout substantiates
            # nothing, so no phase may stay lit on its evidence.
            assert _rung_status(page, "demotion_pending") == "UNKNOWN"

            # ---- Manual resolution clears it, and the pane follows ------------- #
            state.unlink()
            _cli(
                binary,
                "demote",
                "--demoting",
                DEMOTING,
                "--candidate",
                CANDIDATE,
                "--state",
                str(state),
                "--expect",
                "demotion-pending",
                "--position",
                "AAPL:100",
            )
            _await_note_tone(page, "error")
            resolved = _cli(
                binary,
                "resolve",
                "--state",
                str(state),
                "--confirm",
                "operator: AAPL flattened by hand",
            )
            assert resolved.returncode == 0, resolved.stderr
            note = _await_note_tone(page, "ok")
            assert "no demotion is pending" in note, note
            assert _rung_status(page, "demotion_pending") == "UNKNOWN"
        finally:
            browser.close()
