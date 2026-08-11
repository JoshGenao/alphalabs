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
            cargo,
            "build",
            "-p",
            "atp-orchestrator",
            "--bin",
            PROMOTE_BIN,
            "-p",
            "atp-simulation",
            "--bin",
            PERSIST_BIN,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
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
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert persisted.returncode == 0, f"seeding the paper store failed:\n{persisted.stderr}"
    assert "persisted:true" in persisted.stdout
    return store


def _swap(
    binaries,
    *,
    state: Path,
    paper: Path,
    demoting=DEMOTING,
    candidate=CANDIDATE,
    confirm=True,
    extra=(),
) -> subprocess.CompletedProcess[str]:
    argv = [
        str(binaries[PROMOTE_BIN]),
        "swap",
        "--state",
        str(state),
        "--demoting",
        demoting,
        "--candidate",
        candidate,
        "--paper-state",
        str(paper),
        "--demotion-lock",
        str(state.parent / "demotion-pending.json"),
        # This walk exercises the GATE, and the two safety facts it turns on have no
        # real producer yet (SRS-EXE-006 / SRS-ORCH-004). The binary refuses fixture
        # safety inputs unless the caller says out loud that it is running a drill,
        # which is what keeps the served REST path from promoting on them.
        "--allow-fixture-safety-inputs",
        # Every fixture safety fact is stated explicitly — the binary has no success
        # defaults, so an omitted one is an error rather than a silent "flat".
        # A case that needs a different value overrides it via `extra`.
        *([] if any(f == "--liquidation" for f in extra) else ["--liquidation", "flat"]),
        *([] if any(f == "--positions" for f in extra) else ["--positions", "flat"]),
        *(
            []
            if any(f == "--deployed-version" for f in extra)
            else ["--deployed-version", "sha256:" + "a" * 64]
        ),
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
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0
    assert _lines(status)["designated"] == CANDIDATE


# ----------------------------------------------------------------------------- #
# 2. AC clause 1 — starts live with no open IB positions
# ----------------------------------------------------------------------------- #


def test_open_positions_block_the_promotion(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(
        _swap(binaries, state=state, paper=paper_store, extra=["--positions", "AAPL:-100"])
    )

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "LIVE_POSITIONS_OPEN"
    assert proof["designation-after"] == DEMOTING
    assert _digest(state) == before


def test_an_unreadable_position_probe_is_not_a_flat_account(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(
        _swap(binaries, state=state, paper=paper_store, extra=["--positions", "unreadable"])
    )

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

    proof = _lines(
        _swap(binaries, state=state, paper=paper_store, extra=["--inject", "paper-drift"])
    )

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "PAPER_HISTORY_DRIFT"
    # Rolled back: a refused promotion must never leave the candidate live.
    assert proof["designation-after"] == "none"
    # And the rollback is DURABLE — the next process must not see a live candidate.
    status = subprocess.run(
        [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert _lines(status)["designated"] == "none"


# ----------------------------------------------------------------------------- #
# 4. AC clause 3 — same strategy code / API behavior
# ----------------------------------------------------------------------------- #


def test_a_missing_deployed_version_is_refused(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(
        _swap(binaries, state=state, paper=paper_store, extra=["--deployed-version", "missing"])
    )

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "CODE_IDENTITY_MISSING"
    assert _digest(state) == before


def test_code_identity_drift_rolls_the_designation_back(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(
        _swap(binaries, state=state, paper=paper_store, extra=["--inject", "version-drift"])
    )

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


def test_fixture_safety_inputs_must_be_declared_out_loud(binaries, paper_store, tmp_path):
    """The served REST path's protection, at its source.

    --positions (the flat-account fact) and --deployed-version (the code-identity
    fact) have no real producer in this build. Defaulting them silently is what let
    a served route report PROMOTED without proving either, so the binary refuses
    them unless the caller declares the drill.
    """
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)
    argv = [
        str(binaries[PROMOTE_BIN]),
        "swap",
        "--state",
        str(state),
        "--demoting",
        DEMOTING,
        "--candidate",
        CANDIDATE,
        "--paper-state",
        str(paper_store),
        "--demotion-lock",
        str(tmp_path / "demotion-pending.json"),
        "--confirm",
    ]  # deliberately WITHOUT --allow-fixture-safety-inputs

    result = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)

    assert result.returncode != 0
    assert "FIXTURE safety inputs" in result.stderr
    assert "SRS-EXE-006" in result.stderr and "SRS-ORCH-004" in result.stderr
    assert "promotion:PROMOTED" not in result.stdout
    # Refused before the state file was touched.
    assert _lines(result).get("designation-after") is None


# ----------------------------------------------------------------------------- #
# 7. Concurrency — two swaps must not both promote
# ----------------------------------------------------------------------------- #


def test_two_concurrent_swaps_cannot_both_promote(binaries, paper_store, tmp_path):
    """The single-live-strategy invariant under a real race.

    The gate loads the durable designation, decides, and writes it back. Without a
    lock over that WHOLE read-execute-write sequence, both attempts read the same
    live strategy, both promote the same candidate, and both report PROMOTED — two
    swaps acknowledged against one IB account, with the last rename deciding.

    Both attempts request the SAME swap (a -> b) on purpose: that is the pair that
    can genuinely both succeed on a stale read. With the lock, the first wins and
    the second sees `b` already live, so its execution-time revalidation refuses.

    Repeated, because a race that reproduces once in N runs is still a race — a
    single round can pass by luck on either side of the fix.

    Raised by the round-1 adversarial review; this is the regression lock.
    """
    import concurrent.futures

    for round_index in range(12):
        state = tmp_path / f"live-{round_index}.state"
        _seed_live(state, DEMOTING)

        def attempt(state_path: Path = state) -> dict[str, str]:
            # `state_path` is BOUND as a default rather than closed over: a lambda
            # capturing the loop variable would read whatever the loop had reached
            # by the time the thread ran, so a later round's file could be raced
            # instead of this one's — the test would still pass, for the wrong
            # reason. (ruff B023.)
            return _lines(_swap(binaries, state=state_path, paper=paper_store))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(attempt) for _ in range(2)]]

        promoted = [r for r in results if r.get("promotion") == "PROMOTED"]
        assert len(promoted) <= 1, (
            f"round {round_index}: both concurrent swaps reported PROMOTED — the "
            f"read-execute-write sequence is not serialized: {results}"
        )
        # The durable record must agree with whoever won.
        final = _lines(
            subprocess.run(
                [str(binaries[PROMOTE_BIN]), "status", "--state", str(state)],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )
        )["designated"]
        assert final == (CANDIDATE if promoted else DEMOTING)


def test_a_promotion_whose_journal_append_fails_is_not_reported_as_clean(
    binaries, paper_store, tmp_path
):
    """Round-2 adversarial review [high]: an unauditable live state change.

    The audit sink is best-effort — by the time it runs the designation is already
    written, so a sink failure cannot roll it back. But a candidate that is LIVE
    with no durable record of how it got there is an operator-reconciliation event,
    not a clean success, and the exit code must not say otherwise.

    The append is made to fail by pointing --log at a DIRECTORY, which is a real
    unwritable path rather than a monkeypatched function.
    """
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)
    blocked = tmp_path / "journal-is-a-directory.jsonl"
    blocked.mkdir()

    result = _swap(binaries, state=state, paper=paper_store, extra=["--log", str(blocked)])
    proof = _lines(result)

    # The swap DID happen — the requirement's ordering held and the candidate is live.
    assert proof["promotion"] == "PROMOTED"
    assert proof["designation-after"] == CANDIDATE
    # But it is not reported as a clean success.
    assert proof["promotion-recorded"] == "false"
    assert proof["swap-record-ordinal"] == "-"
    assert result.returncode != 0, "a promotion with no durable record must not exit 0"
    assert "unauditable" in result.stderr


def test_an_unconfigured_journal_is_not_an_append_failure(binaries, paper_store, tmp_path):
    """Three states, never two: a caller who asked for no journal has not suffered
    a write failure, and must not be reported as though they had."""
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    result = _swap(binaries, state=state, paper=paper_store)  # no --log
    proof = _lines(result)

    assert proof["promotion"] == "PROMOTED"
    assert proof["promotion-recorded"] == "not-configured"
    assert result.returncode == 0, "an unconfigured journal is a usage choice, not a failure"


def test_the_fixture_tier_has_no_success_defaults(binaries, paper_store, tmp_path):
    """Round-3 adversarial review [critical].

    Declaring the fixture TIER is not the same as stating the fixture FACTS. With
    success defaults, `--allow-fixture-safety-inputs` alone promoted on an unstated
    flat account and a dummy artifact hash — the silent success the opt-in was meant
    to stop, one layer further in.
    """
    state = tmp_path / "live.state"
    for omitted in ("--liquidation", "--positions", "--deployed-version"):
        _seed_live(state, DEMOTING)
        argv = [
            str(binaries[PROMOTE_BIN]),
            "swap",
            "--state",
            str(state),
            "--demoting",
            DEMOTING,
            "--candidate",
            CANDIDATE,
            "--paper-state",
            str(paper_store),
            "--demotion-lock",
            str(tmp_path / "demotion-pending.json"),
            "--allow-fixture-safety-inputs",
            "--confirm",
        ]
        for flag, value in (
            ("--liquidation", "flat"),
            ("--positions", "flat"),
            ("--deployed-version", "sha256:" + "a" * 64),
        ):
            if flag != omitted:
                argv += [flag, value]

        result = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True)

        assert result.returncode != 0, f"omitting {omitted} must not promote"
        assert f"{omitted} is required" in result.stderr
        assert "promotion:PROMOTED" not in result.stdout


def test_the_cli_refuses_an_empty_live_designation(binaries, paper_store, tmp_path):
    """Round-5 adversarial review [critical]: the guard belongs in the SHARED gate.

    The REST wrapper already refused this with NO_LIVE_STRATEGY_TO_DEMOTE, but the
    CLI is a declared SYS-49a operator arm and it went straight through — the gate
    treated "nothing is designated" as an acceptable starting state and promoted.
    A rule enforced only at the outermost surface is not enforced.
    """
    state = tmp_path / "live.state"  # deliberately never created: no live strategy

    result = _swap(binaries, state=state, paper=paper_store)
    proof = _lines(result)

    assert result.returncode != 0
    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "NO_LIVE_STRATEGY_TO_DEMOTE"
    assert proof["designation-after"] == "none"
    assert not state.exists(), "a refused swap must not create the designation record"


def test_a_durable_promotion_reports_its_persistence(binaries, paper_store, tmp_path):
    """Round-5 adversarial review [high]: REST maps a non-2xx to 'nothing mutated'.

    The proof line is what lets the surface tell a swap whose designation was
    published from one that never touched the record — a post-rename failure has
    already moved the live slot, so a retry would act on changed state.
    """
    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store))

    assert proof["promotion"] == "PROMOTED"
    assert proof["designation-persisted"] == "durable"


def test_a_refused_swap_reports_the_designation_unchanged(binaries, paper_store, tmp_path):
    state = tmp_path / "live.state"
    before = _seed_live(state, DEMOTING)

    proof = _lines(_swap(binaries, state=state, paper=paper_store, extra=["--positions", "AAPL:5"]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["designation-persisted"] == "unchanged"
    assert _digest(state) == before


def test_no_promotion_record_survives_a_failed_designation_publish(binaries, paper_store, tmp_path):
    """Round-6 adversarial review [high]: an audit record must not outlive the swap.

    The gate records the promotion event while the designation is still only in
    memory. If the durable publish then fails BEFORE its rename, an already-appended
    journal would claim `promoted:true` for a state change the durable authority
    never accepted — and recovery tooling would reconcile from a false record.

    The publish is made to fail by pointing --state at a path whose PARENT does not
    exist, which is a real pre-publish failure (the scratch file cannot be created)
    rather than a monkeypatched function.
    """
    journal = tmp_path / "swaps.jsonl"
    unwritable_state = tmp_path / "no-such-dir" / "live.state"

    result = _swap(
        binaries, state=unwritable_state, paper=paper_store, extra=["--log", str(journal)]
    )

    assert result.returncode != 0
    assert not unwritable_state.exists(), "nothing may have been published"
    # The load side treats a missing file as an empty designation, so this attempt
    # is refused for NO_LIVE_STRATEGY_TO_DEMOTE before it ever reaches the publish —
    # either way, no record may claim a promotion.
    if journal.exists():
        import json as _json

        records = [_json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
        assert not any(r["promoted"] for r in records), (
            f"a promotion record survived a swap that never published: {records}"
        )


def test_a_blocked_swap_is_still_journalled(binaries, paper_store, tmp_path):
    """Deferring the append must not lose the audit value of a REFUSED swap: a
    blocked attempt is exactly the transition an operator needs in the log."""
    import json as _json

    state = tmp_path / "live.state"
    _seed_live(state, DEMOTING)
    journal = tmp_path / "swaps.jsonl"

    proof = _lines(
        _swap(
            binaries,
            state=state,
            paper=paper_store,
            extra=["--positions", "AAPL:7", "--log", str(journal)],
        )
    )

    assert proof["promotion"] == "BLOCKED"
    records = [_json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    assert records[0]["promoted"] is False
    assert records[0]["refusal"] == "LIVE_POSITIONS_OPEN"


def test_a_stale_demoting_id_is_refused_before_the_demotion_runs(binaries, paper_store, tmp_path):
    """Round-7 adversarial review [critical], at the CLI arm.

    `resolve_demotion` is not read-only: on its timeout branch it engages the
    durable lockout, cancels unfilled liquidation orders and pages the operator. A
    swap that queued behind another one can arrive naming a strategy that is no
    longer live, so refusing only after the demotion ran would fire all of that
    against the wrong strategy.

    Observable here: the refusal is UNEXPECTED_LIVE_STRATEGY, the journal records
    the demotion as never confirmed, and the durable lockout file is never brought
    into being.
    """
    import json as _json

    state = tmp_path / "live.state"
    before = _seed_live(state, "some-other-live-strategy")
    journal = tmp_path / "swaps.jsonl"
    lock = tmp_path / "demotion-pending.json"

    proof = _lines(_swap(binaries, state=state, paper=paper_store, extra=["--log", str(journal)]))

    assert proof["promotion"] == "BLOCKED"
    assert proof["refusal"] == "UNEXPECTED_LIVE_STRATEGY"
    assert proof["designation-after"] == "some-other-live-strategy"
    assert _digest(state) == before
    assert not lock.exists(), "the demotion lockout must not be engaged for a stale id"

    record = [_json.loads(line) for line in journal.read_text().splitlines() if line.strip()][0]
    assert record["promoted"] is False
    assert record["flat_confirmed"] is False, "no demotion ran, so nothing was confirmed flat"


def test_a_pre_demotion_refusal_never_prints_a_confirmed_demotion(binaries, paper_store, tmp_path):
    """Round-9 adversarial review [critical], at the CLI proof-line boundary.

    The live-slot guards run before any demotion side effect (r7), but
    `flat_confirmed()` was a denylist, so those refusals printed
    `demotion-outcome:FLAT_CONFIRMED` — and the REST handler maps that to
    `demotion_state: DEMOTED`. A swap in which no demotion ran would have told the
    operator one succeeded.
    """
    for seeded, expected in (
        (None, "NO_LIVE_STRATEGY_TO_DEMOTE"),
        ("some-other-live-strategy", "UNEXPECTED_LIVE_STRATEGY"),
    ):
        state = tmp_path / f"live-{expected}.state"
        if seeded is not None:
            _seed_live(state, seeded)

        proof = _lines(_swap(binaries, state=state, paper=paper_store))

        assert proof["refusal"] == expected
        assert proof["demotion-outcome"] == "NOT_STARTED", (
            "a refusal that never ran a demotion is neither flat-confirmed NOR "
            "demotion-pending: nothing started, so no lockout exists to wait on"
        )
