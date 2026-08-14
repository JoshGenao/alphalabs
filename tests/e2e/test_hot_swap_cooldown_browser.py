"""L6 e2e — ``SRS-RESV-006`` Step 2: the SYS-49e cool-down, in a real browser.

The browser-automation + REST leg of the acceptance evidence. The AC:

    After successful swap, automatic triggers are ignored for the configured
    cool-down period defaulting to 7 calendar days; manual swap during cool-down
    requires confirmation warning; the cool-down start time is the timestamp of the
    most recent successful swap completion.

**What is real here.** The window is a real file written by the real
``resv006_hot_swap_cooldown_cli``; the pane's cool-down dial is resolved by the real
``CliHotSwapCooldownSource`` shelling that same binary; the operator's click POSTs to
the real ``/api/v1/hot-swap`` route, which shells the real ``resv005_hot_swap_promote_cli``,
which runs the real ``execute_hot_swap`` gate — the one this feature added the
cool-down to. Nothing about the cool-down is faked: the refusal a browser sees is
produced by the same predicate the Rust tests exercise.

**What is injected, and why.** Only SRS-RESV-002's ranking candidate (without one the
UI-5 control is correctly inert and there is no button to drive) and the
flat-account / code-identity facts SRS-EXE-006 and SRS-ORCH-004 will own, declared
through ``fixture_safety_inputs`` so the drill is explicit. Step 2 permits exactly
that: "with the fixtures, mocks, or operator controls needed by the requirement".

**Why this is the step that could not be run before.** It binds the dashboard stack,
which the parallel-agent protocol forbids while sibling agents hold leases. Every
prior session on this feature had a live sibling; this one does not.

Gated: ``pytest -m "not e2e"`` skips it. Runs under ``ATP_RUN_E2E=1`` with Playwright
browsers installed and cargo on PATH. ``ATP_CAPTURE_EVIDENCE=1`` attaches the
screenshots the AC step's record requires.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from atp_dashboard import (  # noqa: E402
    HotSwapStatusProvider,
    ReadinessBackedProvider,
    mount_dashboard,
)
from atp_hotswap import (  # noqa: E402
    CliHotSwapCooldownSource,
    CliHotSwapDemotionSource,
    CliHotSwapPromotionSource,
    CompositeHotSwapStatusSource,
)
from atp_orchestration import mount_hot_swap_execution  # noqa: E402
from atp_runtime import OperatorInterfaceRuntime  # noqa: E402

from .capture import evidence_browser  # noqa: E402

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
PROMOTE_BIN = "resv005_hot_swap_promote_cli"
DEMOTION_BIN = "resv004_hot_swap_demotion_cli"
COOLDOWN_BIN = "resv006_hot_swap_cooldown_cli"
PERSIST_BIN = "sim004_persist_cli"

#: The two strategies the SRS-SIM-004 fixture snapshot actually contains.
DEMOTING = "reservoir-a"
CANDIDATE = "reservoir-b"

STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"
FEATURE = "SRS-RESV-006"
#: The AC step this evidence belongs to.
STEP = 2


class _RankingStandIn:
    """SRS-RESV-002's Reservoir ranking — the only injected fact on the pane side."""

    def __init__(self, candidate: str) -> None:
        self._candidate = candidate

    def trigger_config(self):
        return None

    def promotion_candidate(self):
        return {"candidate_strategy_id": self._candidate}


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    build = subprocess.run(
        [
            cargo,
            "build",
            "-p",
            "atp-orchestrator",
            "--bin",
            PROMOTE_BIN,
            "--bin",
            DEMOTION_BIN,
            "--bin",
            COOLDOWN_BIN,
            "-p",
            "atp-simulation",
            "--bin",
            PERSIST_BIN,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    paths = {
        n: ROOT / "target" / "debug" / n
        for n in (PROMOTE_BIN, DEMOTION_BIN, COOLDOWN_BIN, PERSIST_BIN)
    }
    for name, path in paths.items():
        assert path.exists(), f"{name} not built at {path}"
    return paths


@pytest.fixture()
def live_dashboard(binaries, tmp_path) -> Iterator[tuple[str, Path]]:
    """The real pane + the real execution route, both over one real window file."""
    paper = tmp_path / "paper"
    paper.mkdir()
    seeded = subprocess.run(
        [str(binaries[PERSIST_BIN]), "persist", "--dir", str(paper)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "live.state"
    state.write_text(f"{STATE_MAGIC}\ndesignated\t{DEMOTING}\n")
    lockout = tmp_path / "demotion-pending.json"
    journal = tmp_path / "swaps.jsonl"
    cooldown = tmp_path / "cooldown.json"

    source = CompositeHotSwapStatusSource(
        triggers=_RankingStandIn(CANDIDATE),
        demotion=CliHotSwapDemotionSource(lockout, binary=binaries[DEMOTION_BIN]),
        promotion=CliHotSwapPromotionSource(state, binary=binaries[PROMOTE_BIN]),
        # The feature under test: the pane reads the SAME window the gate enforces.
        cooldown=CliHotSwapCooldownSource(cooldown, binary=binaries[COOLDOWN_BIN]),
    )

    runtime = OperatorInterfaceRuntime()
    publisher = mount_dashboard(
        runtime,
        ReadinessBackedProvider({}),
        hot_swap=HotSwapStatusProvider(source=source),
    )
    mount_hot_swap_execution(
        runtime,
        state_path=state,
        paper_state_dir=paper,
        log_path=journal,
        demotion_lock_path=lockout,
        cooldown_state_path=cooldown,
        fixture_safety_inputs={
            "positions": "flat",
            "deployed_version": "sha256:" + "a" * 64,
            "liquidation": "flat",
        },
        binary=binaries[PROMOTE_BIN],
    )
    publisher.start()
    host, port = runtime.start(host="127.0.0.1", port=0)
    try:
        yield f"http://{host}:{port}/dashboard", cooldown
    finally:
        publisher.stop()
        runtime.stop()


def _open_window(binaries, cooldown: Path, *, completed_at: int) -> None:
    """Open a REAL window, through the real binary."""
    result = subprocess.run(
        [
            str(binaries[COOLDOWN_BIN]),
            "record-completion",
            "--state",
            str(cooldown),
            "--demoted",
            "older-a",
            "--promoted",
            "older-b",
            "--completed-at",
            str(completed_at),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _now_seconds() -> int:
    import time

    return int(time.time())


def _open_pane(page, url: str) -> None:
    """Load the dashboard and wait for the pane's FIRST poll to land."""
    with page.expect_response(
        lambda response: "/dashboard/api/hot-swap" in response.url, timeout=20_000
    ):
        page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#hs-cooldown-value", timeout=15_000)


def _dial_state(page) -> str:
    return page.eval_on_selector("#hs-dial", "el => el.dataset.state")


def _arm(page) -> None:
    """Click the control and wait until it really is ARMED.

    Waiting on the state rather than assuming the click took it there: the control
    self-disarms after 5s, so a test that merely clicks twice can end up re-arming
    instead of confirming — which is silent, because both clicks succeed.
    """
    page.wait_for_function(
        "() => { const b = document.getElementById('hs-btn');"
        " return b && !b.disabled && b.dataset.armed !== 'true'; }",
        timeout=15_000,
    )
    page.click("#hs-btn")
    page.wait_for_function(
        "() => { const b = document.getElementById('hs-btn');"
        " return b && b.dataset.armed === 'true'; }",
        timeout=10_000,
    )


def test_the_cooldown_dial_and_the_gate_agree_in_a_real_browser(binaries, live_dashboard):
    """Step 2, end to end: the window an operator SEES is the one that refuses them.

    The AC's three clauses, walked through a browser and the real REST route:

      1. a window opened by a real swap completion renders as ACTIVE, with the
         countdown derived from the real expiry;
      2. arming inside that window shows SYS-49e's confirmation warning — and the
         swap is PERMITTED once the operator confirms, never blocked;
      3. the swap that results restarts the window at its OWN completion timestamp,
         which the pane then shows.
    """
    url, cooldown = live_dashboard
    started_at = _now_seconds() - 3_600  # one hour into a seven-day window

    with evidence_browser(sync_api, FEATURE, step=STEP) as cap:
        page = cap.page()

        # --- Clause 0: no window at all. The dial is READY and the control arms. ---
        _open_pane(page, url)
        assert _dial_state(page) == "expired", (
            "an absent window is NEVER_SWAPPED — genuinely clear, not unknown"
        )
        page.wait_for_function(
            "() => { const b = document.getElementById('hs-btn'); return b && !b.disabled; }",
            timeout=15_000,
        )
        cap.shot(
            page,
            "Hot-Swap pane with no cool-down window: the dial reads READY and the "
            "promote control is armable (SYS-49e clause 1, baseline)",
            element="#hs",
        )

        # --- Clause 1: a real completion opens a real window; the dial follows. ---
        _open_window(binaries, cooldown, completed_at=started_at)
        page.wait_for_function(
            "() => { const d = document.getElementById('hs-dial');"
            " return d && d.dataset.state === 'active'; }",
            timeout=20_000,
        )
        label = page.inner_text("#hs-cooldown-value")
        assert label.strip() not in ("", "— —", "READY"), (
            f"an ACTIVE window must show a countdown, got {label!r}"
        )
        cap.shot(
            page,
            "A recorded swap completion opens the SYS-49e window: the dial reads "
            "ACTIVE with the remaining time counted from the completion timestamp "
            "(clause 1, and clause 3's start time)",
            element="#hs",
        )

        # --- Clause 2: arming inside the window shows the confirmation warning. ---
        _arm(page)
        warning = page.inner_text("#hs-status")
        assert "COOL-DOWN ACTIVE" in warning and "SYS-49e" in warning, warning
        cap.shot(
            page,
            "Arming during the cool-down raises SYS-49e's confirmation warning — the "
            "manual swap is offered, not blocked (clause 2)",
            element="#hs",
        )

        # --- Clause 2, continued: confirming PERMITS the swap. ---
        #
        # A SECOND arm cycle, because the control disarms itself after
        # HOT_ARM_WINDOW_MS (5s) and the screenshot above legitimately takes longer
        # than that — the first real run of this test spent its arm window inside
        # `cap.shot` and then clicked a control that had already gone back to
        # resting, so the confirm click merely re-armed and no swap was ever
        # requested. Re-arm, then confirm with nothing in between.
        _arm(page)
        with page.expect_response(
            # The pane posts to "/api/v1/hot-swap?confirm=true" — the SYS-2d token is a
            # QUERY param, so an endswith() match on the bare path never fires.
            lambda r: "/api/v1/hot-swap" in r.url and r.request.method == "POST",
            timeout=30_000,
        ) as caught:
            page.click("#hs-btn")
        response = caught.value
        assert response.status == 200, (
            "SYS-49e PERMITS a manual swap during the window once the operator "
            f"acknowledges the warning; got {response.status} {response.text()}"
        )
        body = response.json()
        assert body["promotion_state"] == "PROMOTED", body
        # Clause 3, from the real producer: the swap restarted the window.
        assert body["cooldown_window"] == "STARTED", body

        page.wait_for_function(
            "() => { const d = document.getElementById('hs-dial');"
            " return d && d.dataset.state === 'active'; }",
            timeout=20_000,
        )
        cap.shot(
            page,
            "After the confirmed swap: the window has RESTARTED at the new swap's own "
            "completion timestamp, so the countdown is back to nearly seven days "
            "(clause 3 — the start time is the most recent successful completion)",
            element="#hs",
        )

    # The window on disk is the one the swap wrote, and it is now newer than the
    # one seeded above — read in a SEPARATE process, so this is the durable fact
    # rather than the page's copy of it.
    status = subprocess.run(
        [str(binaries[COOLDOWN_BIN]), "status", "--state", str(cooldown)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr
    fields = dict(line.split(":", 1) for line in status.stdout.splitlines() if ":" in line)
    assert fields["cooldown-state"] == "ACTIVE"
    assert int(fields["cooldown-started-at-seconds"]) > started_at, (
        "the swap must have restarted the window at its own completion timestamp"
    )


def test_an_unreadable_window_holds_the_promote_control_inert(binaries, live_dashboard):
    """UNKNOWN is not "no cool-down is in effect" — all the way to the button.

    The non-vacuity partner to the walk above: without this, a pane that simply
    never armed would satisfy every "held" assertion, and a pane that always armed
    would satisfy every "armed" one.
    """
    url, cooldown = live_dashboard
    cooldown.write_text("{ this is not a cool-down window")

    with evidence_browser(sync_api, FEATURE, step=STEP) as cap:
        page = cap.page()
        _open_pane(page, url)

        page.wait_for_function(
            "() => { const b = document.getElementById('hs-btn'); return b && b.disabled; }",
            timeout=20_000,
        )
        assert _dial_state(page) == "deferred", (
            "a window that cannot be read must render UNKNOWN, never 'no cool-down'"
        )
        sub = page.inner_text("#hs-cooldown-sub")
        assert "UNKNOWN" in sub, sub
        cap.shot(
            page,
            "A corrupt cool-down window: the dial reports UNKNOWN and the promote "
            "control is held inert — an unreadable window is never read as 'clear'",
            element="#hs",
        )
