"""SRS-RESV-005 / SyRS SYS-49d — a paper strategy reaches live ONLY after a successful demotion.

L7 domain (safety) test for the Hot-Swap promotion gate. This is a trading-safety
invariant, not a workflow nicety: a promotion that runs while the previous live
strategy still holds IB positions puts two strategies' exposure on one account, and
a promotion that proceeds on an *unreadable* position probe does the same thing
while reporting success. AGENTS.md's core constraint — "exactly one strategy may
execute against the IB live account at any time" — is what this gate defends at the
one moment the live slot changes hands.

The test drives the REAL operator binary ``resv005_hot_swap_promote_cli`` in fresh
OS processes, over a REAL SRS-SIM-004 paper-state snapshot written by the REAL
``sim004_persist_cli``. Nothing here is a stand-in that could agree with itself:
the paper performance history is the one the simulation engine actually persisted,
and "preserved" is asserted as the snapshot file being **byte-identical** on disk
after a successful promotion.

Deliberately NOT shelling a cargo test: a harness that shells another test proves
nothing unless it also proves the inner test asserts (docs/playbooks/test-integrity.md
r4/r5). Driving the binary and parsing its real proof lines removes that question.

Angles covered:

  1. **Ordering** — a demotion that timed out promotes nothing, and the durable
     live-designation record is byte-identical afterwards.
  2. **Clause 1 (flat start)** — open positions refuse; an unreadable probe refuses
     with a DIFFERENT machine reason, because "we could not check" must never be
     handled as "there is nothing there".
  3. **Clause 2 (paper history preserved)** — missing and unreadable history each
     refuse; a successful promotion leaves the persisted snapshot byte-identical;
     injected drift rolls the designation back rather than shipping a live strategy
     whose history was reset.
  4. **Clause 3 (same strategy code)** — a missing or drifted deployed version
     refuses, and drift rolls back.
  5. **Single-live** — a third strategy holding the slot is never promoted over, and
     the promotion survives the process boundary (a separate ``status`` process
     agrees), which is what makes the invariant hold for the REST surface.
  6. **Fail-closed operator input** — no ``--confirm`` touches nothing at all, and a
     foreign state file is refused rather than read as "nothing is live".
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMOTE_BIN = "resv005_hot_swap_promote_cli"
PERSIST_BIN = "sim004_persist_cli"

#: The two strategies the SRS-SIM-004 fixture snapshot actually contains.
DEMOTING = "reservoir-a"
CANDIDATE = "reservoir-b"

STATE_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"


def _cargo() -> str:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH; cannot build the operator binaries")
    return cargo


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    """Build both operator binaries once, and hand back their real paths."""
    cargo = _cargo()
    build = subprocess.run(
        [
            cargo, "build",
            "-p", "atp-orchestrator", "--bin", PROMOTE_BIN,
            "-p", "atp-simulation", "--bin", PERSIST_BIN,
        ],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stderr}"
    paths = {name: REPO_ROOT / "target" / "debug" / name for name in (PROMOTE_BIN, PERSIST_BIN)}
    for name, path in paths.items():
        assert path.exists(), f"{name} was not built at {path}"
    return paths


@pytest.fixture()
def paper_store(binaries: dict[str, Path], tmp_path: Path) -> Path:
    """A REAL SRS-SIM-004 paper-state snapshot, written by the real persist CLI."""
    store = tmp_path / "paper"
    store.mkdir()
    persisted = subprocess.run(
        [str(binaries[PERSIST_BIN]), "persist", "--dir", str(store)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert persisted.returncode == 0, f"seeding the paper store failed:\n{persisted.stderr}"
    assert "persisted:true" in persisted.stdout
    return store


def _swap(binaries, *, state: Path, paper: Path, demoting=DEMOTING, candidate=CANDIDATE,
          confirm=True, extra=()) -> subprocess.CompletedProcess[str]:
    argv = [
        str(binaries[PROMOTE_BIN]), "swap",
        "--state", str(state),
        "--demoting", demoting,
        "--candidate", candidate,
        "--paper-state", str(paper),
        *extra,
    ]
    if confirm:
        argv.append("--confirm")
    return subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)


def _lines(completed: subprocess.CompletedProcess[str]) -> dict[str, str]:
    """The binary's deterministic ``key:value`` proof lines."""
    out: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            out.setdefault(key, value)
    return out


def _seed_live(state: Path, strategy: str) -> str:
    """Put `strategy` in the durable live slot; return the file's digest."""
    state.write_text(f"{STATE_MAGIC}\ndesignated\t{strategy}\n")
    return hashlib.sha256(state.read_bytes()).hexdigest()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ----------------------------------------------------------------------------- #
# 1. Ordering — the requirement itself
# ----------------------------------------------------------------------------- #


def test_a_timed_out_demotion_promotes_nothing(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    result = _swap(binaries, state=state, paper=paper_store, extra=["--liquidation", "timeout"])
    proof = _lines(result)

    assert result.returncode != 0, "a blocked promotion must not exit 0"
    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "DEMOTION_REFUSED"
    assert proof["demotion-outcome"] == "DEMOTION_PENDING"
    # The demoting strategy is STILL live: a failed demotion must not leave the
    # account unattended, and the candidate must not have been promoted.
    assert proof["designation-after"] == DEMOTING
    assert _digest(state) == before, "the durable live record must be byte-identical"


def test_a_flat_demotion_promotes_the_candidate_across_the_process_boundary(
    binaries, paper_store, tmp_path
):
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store))
    assert proof["promotion"] == "PROMOTED"
    assert proof["designation-after"] == CANDIDATE

    # A SEPARATE process must agree — the REST surface sits behind exactly this
    # boundary, so an in-process-only invariant would not hold for it.
    status = subprocess.run(
        [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert status.returncode == 0
    assert _lines(status)["designated"] == CANDIDATE


# ----------------------------------------------------------------------------- #
# 2. AC clause 1 — starts live with no open IB positions
# ----------------------------------------------------------------------------- #


def test_open_positions_block_the_promotion(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store,
                         extra=["--positions", "AAPL:-100"]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "LIVE_POSITIONS_OPEN"
    assert proof["designation-after"] == DEMOTING
    assert _digest(state) == before


def test_an_unreadable_position_probe_is_not_a_flat_account(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store,
                         extra=["--positions", "unreadable"]))

    assert proof["promotion"] == "BLOCKED"
    # The DISTINCT reason is the safety property: an unreadable probe must not be
    # collapsed into either "flat" (which would promote) or "open".
    assert proof["refusal"] == "LIVE_POSITIONS_UNPROVABLE"
    assert proof["designation-after"] == DEMOTING
    assert _digest(state) == before


# ----------------------------------------------------------------------------- #
# 3. AC clause 2 — preserves prior paper performance history
# ----------------------------------------------------------------------------- #


def test_a_successful_promotion_leaves_the_paper_history_byte_identical(
    binaries, paper_store, tmp_path
):
    snapshot = paper_store / "paper_sim_state.snapshot"
    before = _digest(snapshot)
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store))

    assert proof["promotion"] == "PROMOTED"
    # The AC clause, asserted on the artifact rather than on a claim: the persisted
    # paper performance history is untouched by going live.
    assert _digest(snapshot) == before
    # And the gate reports the fingerprint it verified, so the record is auditable.
    assert proof["paper-history"].count(":") == 2


def test_a_candidate_with_no_paper_history_is_refused(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store, candidate="never-ran-as-paper"))

    assert proof["promotion"] == "BLOCKED"
    # Absent history is not trivially-preserved history.
    assert proof["refusal"] == "PAPER_HISTORY_MISSING"
    assert _digest(state) == before


def test_an_unreadable_paper_store_is_refused(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=tmp_path / "no-such-store"))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "PAPER_HISTORY_UNREADABLE"
    assert _digest(state) == before


def test_paper_history_drift_rolls_the_designation_back(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store,
                         extra=["--inject", "paper-drift"]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "PAPER_HISTORY_DRIFT"
    # Rolled back: a refused promotion must never leave the candidate live.
    assert proof["designation-after"] == "none"
    # And the rollback is DURABLE — the next process must not see a live candidate.
    status = subprocess.run(
        [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert _lines(status)["designated"] == "none"


# ----------------------------------------------------------------------------- #
# 4. AC clause 3 — same strategy code / API behavior
# ----------------------------------------------------------------------------- #


def test_a_missing_deployed_version_is_refused(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store,
                         extra=["--deployed-version", "missing"]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "CODE_IDENTITY_MISSING"
    assert _digest(state) == before


def test_code_identity_drift_rolls_the_designation_back(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store,
                         extra=["--inject", "version-drift"]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "CODE_IDENTITY_DRIFT"
    assert proof["designation-after"] == "none"


# ----------------------------------------------------------------------------- #
# 5. Single-live invariant
# ----------------------------------------------------------------------------- #


def test_a_third_live_strategy_is_never_promoted_over(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, "some-other-live-strategy")

    proof = _lines(_swap(binaries, state=state, paper=paper_store))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "UNEXPECTED_LIVE_STRATEGY"
    # The unrelated live strategy keeps the slot, untouched.
    assert proof["designation-after"] == "some-other-live-strategy"
    assert _digest(state) == before


# ----------------------------------------------------------------------------- #
# 6. Fail-closed operator input
# ----------------------------------------------------------------------------- #


def test_without_confirmation_nothing_is_touched(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"

    result = _swap(binaries, state=state, paper=paper_store, confirm=False)

    assert result.returncode != 0
    assert "--confirm is required" in result.stderr
    # NFR-S2: the confirmation gate runs before ANY state is read or written, so an
    # unconfirmed invocation cannot have created the durable record.
    assert not state.exists(), "an unconfirmed swap must not create the live-designation record"


def test_a_foreign_state_file_is_refused_not_read_as_nothing_is_live(
    binaries, paper_store, tmp_path
):
    state = tmp_path / "live.state"
    state.write_text("SOME-OTHER-TOOLS-FORMAT\ndesignated\treservoir-a\n")

    result = _swap(binaries, state=state, paper=paper_store)

    assert result.returncode != 0
    assert "refusing a foreign or truncated file" in result.stderr
    # The critical part: it did NOT proceed as though no strategy were live.
    assert "promotion:PROMOTED" not in result.stdout


def test_the_operator_surface_labels_its_fixture_tier(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store))

    # A run of this tool is evidence about the GATE, not about a live IB account.
    # The label is what stops a reader inferring the wrong one.
    assert proof["transports"] == "FIXTURE"
