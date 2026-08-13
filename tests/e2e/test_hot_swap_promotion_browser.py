"""L6 e2e — ``SRS-RESV-005`` Step 2: the operator promotes from the dashboard.

The browser leg of the acceptance evidence. The AC:

    The promoted strategy starts live with no open IB positions, preserves prior paper
    performance history, and uses the same strategy code/API behavior.

**What is real here.** Almost everything. A real ``sim004_persist_cli`` writes a real
SRS-SIM-004 paper snapshot; the real UI-5 pane is served by the real
``mount_dashboard`` composition; the pane's *current live strategy* comes from
SRS-RESV-005's real durable designation record and its *demotion-pending* cell from
SRS-RESV-004's real lockout, both read by their real binaries; the operator's click
POSTs to the real ``/api/v1/hot-swap`` route, which shells the real promotion binary,
which runs the real ``execute_hot_swap`` gate over the real ``LiveDesignation``
authority. No transport is intercepted — the browser talks to the real server.

**What is injected, and why.** Two things, both belonging to features that do not
exist yet, injected at the SOURCE seam their real producers will plug into rather
than by faking the wire:

* the promotion **candidate** — SRS-RESV-002's Reservoir ranking. Without it the
  pane's control is correctly inert (there is nothing to promote), so no browser
  walk of this requirement is possible until it is supplied.
* the **flat-account** and **code-identity** facts — SRS-EXE-006's IB position feed
  and SRS-ORCH-004's deployed-version registry, declared through
  ``fixture_safety_inputs``, which is the composition-level opt-in that makes the
  drill explicit. The route refuses to promote without that declaration.

Step 2 of the feature permits exactly this: "with the fixtures, mocks, or operator
controls needed by the requirement".

**What this therefore does and does not prove.** It proves the operator workflow end
to end: an armed control demotes the live strategy and promotes the candidate, the
durable authority follows, and the paper performance history survives byte-for-byte.
It does NOT prove the account was really flat — that fact is a fixture until
SRS-EXE-006 lands, which is why ``passes`` stays false.

Gated: ``pytest -m "not e2e"`` skips it. Runs under ``ATP_RUN_E2E=1`` with Playwright
browsers installed and cargo on PATH. Set ``ATP_CAPTURE_EVIDENCE=1`` to attach the
screenshots the AC step's record requires.
"""

from __future__ import annotations

import json
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
PERSIST_BIN = "sim004_persist_cli"

#: The two strategies the SRS-SIM-004 fixture snapshot actually contains.
DEMOTING = "reservoir-a"
CANDIDATE = "reservoir-b"

STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"
FEATURE = "SRS-RESV-005"


class _RankingStandIn:
    """Stands in for SRS-RESV-002's Reservoir ranking — the ONLY injected fact.

    Implements the trigger leg's shape. `trigger_config` returns ``None`` (that is
    SRS-RESV-003's file and this walk does not configure one, so the chips stay
    honestly deferred); `promotion_candidate` names the strategy the ranking would.
    """

    def __init__(self, candidate: str) -> None:
        self._candidate = candidate

    def trigger_config(self):
        return None

    def promotion_candidate(self):
        # A MAPPING, keyed as the provider reads it — a bare string is rejected by
        # _normalize_leg as a malformed leg (correctly: the pane fails closed on a
        # source whose shape it cannot trust), which leaves the control inert.
        return {"candidate_strategy_id": self._candidate}


class _WithCooldown:
    """Stands in for SRS-RESV-006's cool-down window.

    The pane refuses to arm on ANY unknown safety field (`hotActionable` requires
    `hotCooldownActive !== null`), and the cool-down is SRS-RESV-006's — unbuilt. So
    without this the control is correctly inert and no browser walk of SRS-RESV-005 is
    possible at all. Reported "not in effect", the state a fresh system is in.

    Wrapped around the composite rather than folded into a leg, because no existing
    leg owns this fact and inventing an owner would misattribute it.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def trigger_config(self):
        return self._inner.trigger_config()

    def promotion_candidate(self):
        return self._inner.promotion_candidate()

    def live_state(self):
        state = self._inner.live_state()
        merged = dict(state) if state is not None else {}
        merged["cooldown"] = {"in_effect": False}
        return merged


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
            "-p",
            "atp-orchestrator",
            "--bin",
            DEMOTION_BIN,
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
    paths = {n: ROOT / "target" / "debug" / n for n in (PROMOTE_BIN, DEMOTION_BIN, PERSIST_BIN)}
    for name, path in paths.items():
        assert path.exists(), f"{name} not built at {path}"
    return paths


@pytest.fixture()
def live_dashboard(binaries, tmp_path) -> Iterator[tuple[str, Path, Path, Path]]:
    """The real pane + the real execution route, over real durable state."""
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

    source = _WithCooldown(
        CompositeHotSwapStatusSource(
            # SRS-RESV-002 stand-in.
            triggers=_RankingStandIn(CANDIDATE),
            # SRS-RESV-004's REAL lockout leg.
            demotion=CliHotSwapDemotionSource(lockout, binary=binaries[DEMOTION_BIN]),
            # SRS-RESV-005's REAL designation leg — the feature under test.
            promotion=CliHotSwapPromotionSource(state, binary=binaries[PROMOTE_BIN]),
        )
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
        # Declared DRILL: SRS-EXE-006 / SRS-ORCH-004 have no producer, and the route
        # refuses to promote unless a composer says out loud that these are fixtures.
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
        yield f"http://{host}:{port}/dashboard", state, paper, journal
    finally:
        publisher.stop()
        runtime.stop()


def _designated(binaries, state: Path) -> str:
    """The live strategy, read in a SEPARATE process from the durable record."""
    result = subprocess.run(
        [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        key, _, value = line.partition(":")
        if key == "designated":
            return value
    raise AssertionError(f"no `designated` line in {result.stdout!r}")


def test_the_operator_promotes_a_paper_strategy_from_the_dashboard(binaries, live_dashboard):
    """Step 2, end to end, through a real browser."""
    url, state, paper, journal = live_dashboard
    snapshot = paper / "paper_sim_state.snapshot"
    history_before = snapshot.read_bytes()
    assert _designated(binaries, state) == DEMOTING

    with evidence_browser(sync_api, FEATURE, 2) as cap:
        page = cap.page(url)

        # The pane must show the REAL live strategy, read from the durable record.
        page.wait_for_function(
            "document.querySelector('#hs-live') "
            "&& document.querySelector('#hs-live').textContent.trim() === '%s'" % DEMOTING,
            timeout=20_000,
        )
        cap.shot(page, "UI-5 Hot-Swap pane before the swap: reservoir-a is live", element="#hs")

        # The control arms only with the full safety picture resolved and clear.
        page.wait_for_function(
            "document.querySelector('#hs-btn') && !document.querySelector('#hs-btn').disabled",
            timeout=20_000,
        )
        button = page.locator("#hs-btn")

        button.click()  # arm
        page.wait_for_function(
            "document.querySelector('#hs-btn').dataset.armed === 'true'", timeout=10_000
        )
        cap.shot(page, "control ARMED — confirm within 5s to demote and promote", element="#hs")

        button.click()  # confirm — this POSTs to the REAL /api/v1/hot-swap route

        # The pane binds success to a durable read-back, never to the POST, so wait
        # for it to report the candidate as live.
        page.wait_for_function(
            "document.querySelector('#hs-live') "
            "&& document.querySelector('#hs-live').textContent.trim() === '%s'" % CANDIDATE,
            timeout=60_000,
        )
        cap.shot(
            page, "after the swap: reservoir-b is live, promoted via the dashboard", element="#hs"
        )

    # ---- the durable facts, asserted outside the browser --------------------- #

    # The swap really happened, in another process.
    assert _designated(binaries, state) == CANDIDATE

    # AC clause 2: the prior paper performance history is byte-identical on disk.
    assert snapshot.read_bytes() == history_before

    # The promotion is addressable in the durable journal.
    records = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    promoted = [r for r in records if r["promoted"]]
    assert len(promoted) == 1
    assert promoted[0]["candidate_strategy_id"] == CANDIDATE
    assert promoted[0]["demoting_strategy_id"] == DEMOTING


def test_the_dashboard_cannot_promote_over_an_unreadable_designation(binaries, live_dashboard):
    """The pane's control is gated on a fact it must not guess.

    A designation record that exists but cannot be read is not "no strategy is live".
    The pane must surface the failure and refuse to arm, because arming would aim a
    swap at a strategy nobody can prove is running.

    Asserted POSITIVELY — the cell must show the deferred placeholder and the source
    tag must name the awaited owner — because "the text is not reservoir-a" would also
    hold if the pane had never rendered at all.
    """
    url, state, _paper, journal = live_dashboard
    state.write_text("SOME-OTHER-TOOLS-FORMAT\ndesignated\treservoir-a\n")

    with evidence_browser(sync_api, FEATURE, 2) as cap:
        page = cap.page(url)
        # Wait for a COMPLETED fetch first, using a cell fed by a leg that still
        # works here (the candidate). Anchoring on the live cell alone would pass on
        # the INITIAL DOM — the static markup already ships `data-state="deferred"` —
        # so the assertions below would run before any poll had resolved and would
        # hold no matter what the source returned. Mutation-verified: without this,
        # accepting a foreign snapshot in the binary left the test green.
        page.wait_for_function(
            "document.querySelector('#hs-candidate') "
            "&& document.querySelector('#hs-candidate').textContent.trim() === '%s'" % CANDIDATE,
            timeout=20_000,
        )
        # Only now is "the live cell is deferred" a statement about the source.
        assert page.eval_on_selector("#hs-live", "e => e.dataset.state") == "deferred"
        cap.shot(
            page, "unreadable designation: the pane refuses to name a live strategy", element="#hs"
        )

        assert page.eval_on_selector("#hs-live", "e => e.textContent").strip() == "\u2014"
        assert "awaiting" in page.eval_on_selector("#hs-live-src", "e => e.textContent")
        # The tampered file's contents must not leak into the pane anywhere.
        assert DEMOTING not in page.eval_on_selector("#hs", "e => e.textContent")
        # And the control is inert: an unprovable live strategy can never be swapped.
        assert page.locator("#hs-btn").is_disabled()

    # A read failure is not a write: nothing was journalled, and no promotion ran.
    assert not journal.exists(), "an unreadable designation must not produce a swap record"
