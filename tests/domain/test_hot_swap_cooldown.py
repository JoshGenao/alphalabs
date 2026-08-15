"""SRS-RESV-006 / SyRS SYS-49e / StRS SN-1.25 — the Hot-Swap cool-down window.

> After a successful swap, automatic triggers are ignored for the configured cool-down
> period (default 7 calendar days); a manual swap during cool-down requires a confirmation
> warning; the cool-down start time is the timestamp of the most recent successful swap
> completion.

L7 domain (safety) test. Every case here drives the REAL cargo-built binaries against a
REAL file — ``resv006_hot_swap_cooldown_cli`` opens the window,
``resv003_hot_swap_trigger_cli`` is gated by it — so the acceptance criterion is
demonstrated end to end across two processes rather than inside one test harness.

The three clauses map to the three sections below. The suppression cases each assert the
audit log is UNTOUCHED, because "ignored" has to mean ignored: a gate that fired the
triggers and discarded the proposals would satisfy a ``selected:NONE`` assertion while
writing a swap trigger to the durable trail that never happened.

Time is injected with ``--now`` throughout, so a seven-day window is exercised in
milliseconds and the boundary is exact rather than approximately observed. The DEFAULT is
the real clock — pinned separately by
``crates/atp-orchestrator/tests/resv_6_cli_fail_closed.rs``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]

REPO_ROOT = Path(__file__).resolve().parents[2]

COMPLETED_AT = 1_715_000_000
SEVEN_DAYS = 7 * 86_400
DURING = COMPLETED_AT + 3_600
AFTER = COMPLETED_AT + SEVEN_DAYS + 1


def _binary(name: str) -> Path:
    path = REPO_ROOT / "target" / "debug" / name
    if not path.exists():
        pytest.skip(f"{name} not built; run `cargo build -p atp-orchestrator --bin {name}`")
    return path


@pytest.fixture(scope="module", autouse=True)
def _built() -> None:
    """Build both binaries once, or skip the module.

    A `cargo build` of one crate's two bins — not `cargo test --workspace`, so the
    ~37-fixed-name-scratch-dir collision rule does not apply, and each worktree has its own
    `target/` anyway.
    """

    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    subprocess.run(
        [
            cargo,
            "build",
            "-q",
            "-p",
            "atp-orchestrator",
            "--bin",
            "resv003_hot_swap_trigger_cli",
            "--bin",
            "resv006_hot_swap_cooldown_cli",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _kv(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key] = value
    return values


def _cooldown(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_binary("resv006_hot_swap_cooldown_cli")), *args],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _evaluate(state: Path, log: Path, now: int) -> subprocess.CompletedProcess[str]:
    """An automatic pass with EVERY trigger armed and every condition met.

    So anything that does not fire below is the cool-down doing it, not a missing
    precondition.
    """

    return subprocess.run(
        [
            str(_binary("resv003_hot_swap_trigger_cli")),
            "evaluate",
            "--live",
            "live-a",
            "--live-drawdown",
            "9000",
            "--drawdown-threshold",
            "1000",
            "--top-ranked",
            "--highest-momentum",
            "--rank",
            "cand-b:1:2.5:1.9",
            "--log",
            str(log),
            "--cooldown-state",
            str(state),
            "--now",
            str(now),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _manual(state: Path, log: Path, now: int, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(_binary("resv003_hot_swap_trigger_cli")),
            "manual",
            "--demoting",
            "live-a",
            "--candidate",
            "cand-c",
            "--log",
            str(log),
            "--cooldown-state",
            str(state),
            "--now",
            str(now),
            *extra,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _log_lines(log: Path) -> int:
    if not log.exists():
        return 0
    return len([line for line in log.read_text().splitlines() if line.strip()])


def _open_window(state: Path, completed_at: int = COMPLETED_AT) -> None:
    result = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        "live-a",
        "--promoted",
        "cand-b",
        "--completed-at",
        str(completed_at),
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# Clause 1 — automatic triggers are ignored for the configured period
# --------------------------------------------------------------------------- #


def test_a_recorded_completion_suppresses_automatic_triggers_for_seven_days(tmp_path) -> None:
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"

    # Before any swap: every armed trigger fires and is logged.
    before = _evaluate(state, log, COMPLETED_AT)
    assert before.returncode == 0, before.stderr
    assert _kv(before.stdout)["cooldown-state"] == "NEVER_SWAPPED"
    assert _kv(before.stdout)["selected"] != "NONE"
    fired_records = _log_lines(log)
    assert fired_records >= 1

    _open_window(state)

    # One hour in, and one second before expiry: suppressed, and NOTHING was logged.
    for now in (DURING, COMPLETED_AT + SEVEN_DAYS - 1):
        during = _evaluate(state, log, now)
        assert during.returncode == 0, (
            f"an ACTIVE window is healthy, not degraded — it must exit zero: {during.stderr}"
        )
        values = _kv(during.stdout)
        assert values["cooldown-state"] == "ACTIVE", during.stdout
        assert values["cooldown-suppressed"] == "true", during.stdout
        assert values["selected"] == "NONE", during.stdout
        assert values["fired-count"] == "0", during.stdout
        assert _log_lines(log) == fired_records, (
            "a suppressed pass must append no audit record — 'ignored' means ignored"
        )


def test_the_triggers_fire_again_the_moment_the_window_expires(tmp_path) -> None:
    # The non-vacuity control for the test above: without it, a wholly broken evaluator that
    # never fires anything would pass every suppression assertion perfectly.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)

    assert _kv(_evaluate(state, log, DURING).stdout)["selected"] == "NONE"

    at_expiry = _evaluate(state, log, COMPLETED_AT + SEVEN_DAYS)
    assert at_expiry.returncode == 0, at_expiry.stderr
    values = _kv(at_expiry.stdout)
    assert values["cooldown-state"] == "EXPIRED", at_expiry.stdout
    assert values["selected"] != "NONE", at_expiry.stdout
    assert _log_lines(log) >= 1, "an expired window must let the audit trail resume"


def test_the_window_start_is_the_completion_timestamp_not_the_write_time(tmp_path) -> None:
    # The AC's third clause, verbatim. A store that stamped its own write time would extend
    # every window by the recording latency — and the test would never notice, because the
    # two are seconds apart in practice. Recording a completion six days OLD makes the
    # difference six days wide.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    six_days_ago = COMPLETED_AT - 6 * 86_400
    _open_window(state, completed_at=six_days_ago)

    values = _kv(_evaluate(state, log, COMPLETED_AT).stdout)
    assert values["cooldown-started-at-seconds"] == str(six_days_ago), values
    assert values["cooldown-expires-at-seconds"] == str(six_days_ago + SEVEN_DAYS), values
    # One day left, so still suppressed...
    assert values["cooldown-state"] == "ACTIVE"
    # ...and expired one day later, NOT seven days after the write.
    assert _kv(_evaluate(state, log, six_days_ago + SEVEN_DAYS).stdout)["cooldown-state"] == (
        "EXPIRED"
    )


def test_the_cooldown_survives_a_process_restart(tmp_path) -> None:
    # Every invocation here is a fresh process; the window is only durable if it is on disk.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)
    assert state.exists(), "the window must be persisted, not held in memory"

    status = _cooldown("status", "--state", str(state), "--now", str(DURING))
    assert status.returncode == 0, status.stderr
    assert _kv(status.stdout)["cooldown-in-effect"] == "true"
    assert _kv(_evaluate(state, log, DURING).stdout)["cooldown-state"] == "ACTIVE"


def test_shortening_the_configured_period_reopens_the_trigger_path(tmp_path) -> None:
    # "the CONFIGURED cool-down period, defaulting to 7 days" — configurable in both
    # directions, and a running window is judged by the new length.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)
    two_days_in = COMPLETED_AT + 2 * 86_400
    assert _kv(_evaluate(state, log, two_days_in).stdout)["cooldown-state"] == "ACTIVE"

    configured = _cooldown("configure", "--state", str(state), "--set-days", "1")
    assert configured.returncode == 0, configured.stderr
    assert _kv(configured.stdout)["completion-preserved"] == "true"

    values = _kv(_evaluate(state, log, two_days_in).stdout)
    assert values["cooldown-state"] == "EXPIRED", values
    assert values["selected"] != "NONE"


def test_a_corrupt_window_never_reads_as_no_cooldown(tmp_path) -> None:
    # THE fail-open this feature exists to prevent. An unreadable window that resolved to
    # "no cool-down" is a false all-clear authorising an automatic live-strategy swap.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    state.write_text("{ not our file }")

    result = _evaluate(state, log, DURING)
    assert result.returncode != 0, "an unreadable window is a FAILED pass, not a quiet one"
    values = _kv(result.stdout)
    assert values["cooldown-state"] == "UNKNOWN", result.stdout
    assert values["cooldown-suppressed"] == "true", result.stdout
    assert values["selected"] == "NONE", result.stdout
    assert _log_lines(log) == 0


# --------------------------------------------------------------------------- #
# Clause 2 — a manual swap during cool-down requires a confirmation warning
# --------------------------------------------------------------------------- #


def test_a_manual_swap_during_cooldown_requires_confirmation_and_says_why(tmp_path) -> None:
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)

    refused = _manual(state, log, DURING)
    assert refused.returncode != 0
    values = _kv(refused.stdout)
    assert values["manual-refused"] == "COOLDOWN_CONFIRMATION_REQUIRED", refused.stdout
    assert values["cooldown-confirmation-required"] == "true"
    assert values["cooldown-confirmed"] == "false"
    # A "confirmation warning" is CONTENT, not a boolean: an operator overriding a safety
    # window has to be told which window and until when.
    warning = values["cooldown-warning"]
    assert "SYS-49e" in warning, warning
    assert str(COMPLETED_AT + SEVEN_DAYS) in warning, warning
    assert _log_lines(log) == 0, "a refused swap proposed nothing, so it logs nothing"


def test_the_same_manual_swap_fires_once_the_operator_acknowledges(tmp_path) -> None:
    # The paired twin. SRS-RESV-003 guarantees manual promotion is ALWAYS available, so the
    # cool-down must add a confirmation and never a block: the identical command plus one
    # flag has to succeed, or the invariant is broken.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)
    assert _manual(state, log, DURING).returncode != 0

    fired = _manual(state, log, DURING, "--confirm-cooldown")
    assert fired.returncode == 0, fired.stderr
    values = _kv(fired.stdout)
    assert values["manual-logged"] == "true", fired.stdout
    assert values["manual-always-available"] == "true"
    # The override is recorded, because it is the event an audit reader most needs to find.
    assert values["cooldown-override"] == "true", fired.stdout
    assert _log_lines(log) == 1


def test_a_manual_swap_outside_a_window_needs_no_acknowledgement(tmp_path) -> None:
    # SRS-RESV-003's behaviour, unchanged: no window, no confirmation.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)

    fired = _manual(state, log, AFTER)
    assert fired.returncode == 0, fired.stderr
    values = _kv(fired.stdout)
    assert values["cooldown-state"] == "EXPIRED"
    assert values["cooldown-confirmation-required"] == "false"
    assert values["cooldown-override"] == "false"
    assert _log_lines(log) == 1


def test_a_manual_swap_under_an_unreadable_window_also_requires_acknowledgement(tmp_path) -> None:
    # Same fail-closed direction as the automatic arm: a build that cannot prove the window
    # is clear must not let a manual swap through as though it had.
    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    state.write_text("")

    refused = _manual(state, log, DURING)
    assert refused.returncode != 0
    assert _kv(refused.stdout)["cooldown-state"] == "UNKNOWN", refused.stdout
    assert _kv(refused.stdout)["manual-refused"] == "COOLDOWN_CONFIRMATION_REQUIRED"
    assert _log_lines(log) == 0


# --------------------------------------------------------------------------- #
# Clause 3 — the window only ever moves forward
# --------------------------------------------------------------------------- #


def test_an_older_completion_cannot_shorten_a_running_window(tmp_path) -> None:
    # A clock that stepped backwards between two swaps would otherwise pull the window's
    # start backwards and retire a live safety interval early.
    state = tmp_path / "cd.json"
    _open_window(state)

    older = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        "live-a",
        "--promoted",
        "cand-b",
        "--completed-at",
        str(COMPLETED_AT - 3_600),
    )
    assert older.returncode != 0, "a backwards clock must be operator-actionable"
    values = _kv(older.stdout)
    assert values["completion-recorded"] == "false"
    assert values["kept-stored-completion-at-seconds"] == str(COMPLETED_AT)

    status = _cooldown("status", "--state", str(state), "--now", str(DURING))
    assert _kv(status.stdout)["cooldown-started-at-seconds"] == str(COMPLETED_AT)


def test_a_zero_day_period_is_refused_and_leaves_the_window_intact(tmp_path) -> None:
    # A zero-length window would silently defeat SYS-49e while looking like configuration.
    state = tmp_path / "cd.json"
    _open_window(state)
    before = state.read_text()

    refused = _cooldown("configure", "--state", str(state), "--set-days", "0")
    assert refused.returncode != 0
    assert state.read_text() == before, "a refused period must not have touched the window"


def test_the_recording_surface_names_its_deferred_production_writer(tmp_path) -> None:
    # The write path is real and durable, but the caller that SHOULD invoke it is
    # SRS-RESV-005's promotion — the only code that can observe a swap completing. Saying so
    # on the proof stream keeps this operator surface from reading as the runtime binding.
    state = tmp_path / "cd.json"
    result = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        "live-a",
        "--promoted",
        "cand-b",
        "--completed-at",
        str(COMPLETED_AT),
    )
    assert result.returncode == 0, result.stderr
    assert _kv(result.stdout)["deferred-writer"] == "SRS-RESV-005"


# --------------------------------------------------------------------------- #
# The window survives an id this format cannot represent (adversarial review r2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "demoted", "promoted"),
    [
        ("quote-in-demoted", 'al"pha', "cand-b"),
        ("backslash-in-promoted", "live-a", "cand\\b"),
        ("newline-in-demoted", "al\npha", "cand-b"),
    ],
)
def test_an_unrepresentable_id_is_refused_without_disarming_a_running_window(
    tmp_path, label: str, demoted: str, promoted: str
) -> None:
    """A refused completion must not become a permanent suppression, or a silent one.

    ``serialize`` hand-builds one JSON line and ``StrategyId::new`` accepts any string, so an
    id carrying ``"`` or ``\\`` used to produce a durable line this build's own reader could
    not parse. Both failure directions are safety failures, and this asserts against both at
    once: the operator is told NO, and the seven-day window that was already running is still
    the thing suppressing the automatic triggers afterwards — not an ``UNKNOWN`` that
    suppresses for a reason nobody can act on, and not a cleared file that suppresses nothing.
    """

    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"
    _open_window(state)
    before = state.read_text()

    refused = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        demoted,
        "--promoted",
        promoted,
        "--completed-at",
        str(COMPLETED_AT + 100),
    )
    assert refused.returncode != 0, f"{label}: an unrepresentable id must be refused"
    assert state.read_text() == before, f"{label}: a refused completion must not touch the file"

    # The ORIGINAL window is still readable and still the one in force — the refusal left a
    # window an operator can reason about, not a corrupt one.
    status = _cooldown("status", "--state", str(state), "--now", str(DURING))
    assert status.returncode == 0, status.stderr
    fields = _kv(status.stdout)
    assert fields["cooldown-state"] == "ACTIVE"
    assert fields["cooldown-started-at-seconds"] == str(COMPLETED_AT)

    evaluated = _evaluate(state, log, DURING)
    assert _kv(evaluated.stdout)["cooldown-suppressed"] == "true"
    assert _log_lines(log) == 0, f"{label}: a suppressed pass must write no trigger record"


# --------------------------------------------------------------------------- #
# The EXECUTION handoff — a real swap, a real window, a real suppression
# --------------------------------------------------------------------------- #
#
# Adversarial review r2 (`cooldown-execution-bypass`). The cases above prove the
# TRIGGER layer is gated, but a trigger only mints a proposal — nothing forced a
# swap to have come from one. These drive the whole loop across THREE real
# binaries and one real file:
#
#     resv005 swap  ->  the window opens at the swap's own completion timestamp
#                   ->  resv003 evaluate is suppressed
#                   ->  resv005 swap is itself refused until acknowledged
#
# so "the cool-down start time is the timestamp of the most recent successful swap
# completion" is demonstrated by the producer the requirement actually names,
# rather than by the operator CLI standing in for it.


def _now_wall_seconds() -> int:
    import time

    return int(time.time())


PROMOTE_BIN = "resv005_hot_swap_promote_cli"
PERSIST_BIN = "sim004_persist_cli"
#: The two strategies the SRS-SIM-004 fixture snapshot actually contains.
SWAP_DEMOTING = "reservoir-a"
SWAP_CANDIDATE = "reservoir-b"
DESIGNATION_MAGIC = "RESV005-LIVE-DESIGNATION-STATE v1"


@pytest.fixture(scope="module")
def swap_binaries() -> dict[str, Path]:
    """Build the promotion binary and the paper-snapshot writer it reads."""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not on PATH")
    build = subprocess.run(
        [
            cargo,
            "build",
            "-q",
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
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, f"cargo build failed:\n{build.stderr}"
    paths = {name: REPO_ROOT / "target" / "debug" / name for name in (PROMOTE_BIN, PERSIST_BIN)}
    for name, path in paths.items():
        assert path.exists(), f"{name} was not built at {path}"
    return paths


def _swap(
    swap_binaries: dict[str, Path],
    tmp_path: Path,
    *,
    cooldown_state: Path,
    now: int | None,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """One REAL demote-then-promote attempt, gated by `cooldown_state`."""
    paper = tmp_path / "paper"
    if not paper.exists():
        paper.mkdir()
        seeded = subprocess.run(
            [str(swap_binaries[PERSIST_BIN]), "persist", "--dir", str(paper)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert seeded.returncode == 0, f"seeding the paper store failed:\n{seeded.stderr}"

    designation = tmp_path / "live.state"
    designation.write_text(f"{DESIGNATION_MAGIC}\ndesignated\t{SWAP_DEMOTING}\n")

    return subprocess.run(
        [
            str(swap_binaries[PROMOTE_BIN]),
            "swap",
            "--state",
            str(designation),
            "--demoting",
            SWAP_DEMOTING,
            "--candidate",
            SWAP_CANDIDATE,
            "--paper-state",
            str(paper),
            "--demotion-lock",
            str(tmp_path / "demotion-pending.json"),
            "--cooldown-state",
            str(cooldown_state),
            # `None` means: do not pin the clock, so the binary reads the REAL one —
            # twice, once per instant. Every other case pins it for determinism.
            *([] if now is None else ["--now", str(now)]),
            "--liquidation",
            "flat",
            "--positions",
            "flat",
            "--deployed-version",
            "sha256:" + "a" * 64,
            "--allow-fixture-safety-inputs",
            "--confirm",
            *extra,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_a_real_swap_opens_the_window_that_then_suppresses_the_triggers(
    swap_binaries, tmp_path
) -> None:
    """The whole SYS-49e loop, across three binaries and one file.

    This is the case that closes `cooldown-execution-bypass`: the window is opened
    by the PRODUCTION producer (a completed swap), not by the operator CLI standing
    in for it, and the suppression it causes is observed by a separate process.
    """

    state, log = tmp_path / "cd.json", tmp_path / "t.jsonl"

    # Before the swap: every armed trigger fires.
    before = _evaluate(state, log, COMPLETED_AT - 10)
    assert _kv(before.stdout)["cooldown-state"] == "NEVER_SWAPPED"
    assert _kv(before.stdout)["selected"] != "NONE"
    fired_records = _log_lines(log)
    assert fired_records >= 1, "the pre-swap pass must actually fire, or nothing is proven"

    swapped = _swap(swap_binaries, tmp_path, cooldown_state=state, now=COMPLETED_AT)
    assert swapped.returncode == 0, f"the swap must succeed:\n{swapped.stderr}"
    fields = _kv(swapped.stdout)
    assert fields["promotion"] == "PROMOTED"
    assert fields["cooldown-window"] == "STARTED"
    # SYS-49e clause 3, from the real producer: the window starts at the swap's own
    # completion timestamp, not at the time the file happened to be written.
    assert fields["cooldown-window-started-at-seconds"] == str(COMPLETED_AT)

    window = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING)).stdout)
    assert window["cooldown-state"] == "ACTIVE"
    assert window["cooldown-started-at-seconds"] == str(COMPLETED_AT)

    # And a DIFFERENT process now sees the automatic triggers suppressed.
    during = _evaluate(state, log, DURING)
    assert _kv(during.stdout)["cooldown-suppressed"] == "true"
    assert _kv(during.stdout)["selected"] == "NONE"
    assert _log_lines(log) == fired_records, (
        "a suppressed pass must append no audit record — 'ignored' means ignored"
    )


def test_a_second_swap_inside_the_window_is_refused_until_acknowledged(
    swap_binaries, tmp_path
) -> None:
    """SYS-49a(a): a manual swap stays AVAILABLE during a window, with a warning.

    The refusal and the override are asserted as a PAIR against one window, so a
    gate that blocked every swap would fail the second half and a gate that blocked
    none would fail the first.
    """

    state = tmp_path / "cd.json"
    _open_window(state)

    refused = _swap(swap_binaries, tmp_path, cooldown_state=state, now=DURING)
    assert refused.returncode != 0, "a swap inside the window must be refused"
    fields = _kv(refused.stdout)
    assert fields["cooldown-state"] == "ACTIVE"
    assert fields["cooldown-in-effect"] == "true"
    assert fields["promotion"] == "BLOCKED"
    assert fields["refusal"] == "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED"
    # NOT_STARTED, not DEMOTION_PENDING: nothing ran, so there is no lockout to
    # wait on and nothing to reconcile.
    assert fields["demotion-outcome"] == "NOT_STARTED"
    assert fields["designation-after"] == SWAP_DEMOTING, "the live slot must be untouched"
    assert "SYS-49e permits a manual swap" in refused.stderr

    # The IDENTICAL call, acknowledged, fires.
    confirmed = _swap(
        swap_binaries,
        tmp_path,
        cooldown_state=state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    assert confirmed.returncode == 0, f"an acknowledged swap must fire:\n{confirmed.stderr}"
    confirmed_fields = _kv(confirmed.stdout)
    assert confirmed_fields["promotion"] == "PROMOTED"
    assert confirmed_fields["cooldown-confirmed"] == "true"
    assert confirmed_fields["designation-after"] == SWAP_CANDIDATE
    # The override RESTARTS the window at the new swap's completion.
    assert confirmed_fields["cooldown-window-started-at-seconds"] == str(DURING)


def test_the_real_binary_stamps_the_window_from_its_own_clock(swap_binaries, tmp_path) -> None:
    """Adversarial review r5 [high] — the window starts when the swap COMPLETED.

    Without `--now` the binary reads the real clock TWICE: once to classify the
    existing window when the attempt starts, and again to stamp the completion after
    the promotion has succeeded. This drives the real binary with no `--now` at all
    and asserts both instants are real and correctly ordered — a single read reused
    for both would make a swap that took the whole SYS-49b timeout open a window
    already 60 seconds old.
    """

    state = tmp_path / "cd.json"
    before = _now_wall_seconds()
    swapped = _swap(swap_binaries, tmp_path, cooldown_state=state, now=None)
    after = _now_wall_seconds()
    assert swapped.returncode == 0, swapped.stderr

    fields = _kv(swapped.stdout)
    assert fields["cooldown-window"] == "STARTED"
    observed = int(fields["observed-at-seconds"])
    started = int(fields["cooldown-window-started-at-seconds"])

    # Both are real instants from this run, not the frozen constant this binary
    # used to carry.
    assert before <= observed <= after, (observed, before, after)
    assert before <= started <= after, (started, before, after)
    # And the completion is never EARLIER than the observation — the ordering the
    # requirement turns on.
    assert started >= observed, (
        f"the window opened at {started}, before the swap it belongs to was even "
        f"observed at {observed}"
    )


def test_a_window_exists_only_alongside_a_durably_moved_designation(
    swap_binaries, tmp_path
) -> None:
    """Adversarial review r6 [high] — no window without a durable swap, and none missing.

    The gate designates the candidate live IN MEMORY; the CLI publishes that
    afterwards. Recording the cool-down inside the gate meant a publish failing before
    its rename left a seven-day window for a swap the durable authority never
    accepted. The window is now a token redeemed only after the publish.

    Forcing a mid-rename publish failure is not reachable through this binary, so that
    direction is pinned at the Rust layer
    (``resv_6_a_swap_whose_publish_failed_opens_no_window``, which drops the token).
    What IS observable here is the pairing the invariant produces, in BOTH directions
    and both read from separate processes: a swap that succeeded has moved the durable
    designation AND opened a window; a swap that was refused has done neither.
    """

    state = tmp_path / "cd.json"
    designation = tmp_path / "live.state"

    def designated() -> str:
        result = subprocess.run(
            [str(swap_binaries[PROMOTE_BIN]), "status", "--state", str(designation)],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return _kv(result.stdout)["designated"]

    # --- The REFUSED direction: neither moved. ---
    _open_window(state)  # an ACTIVE window refuses an unacknowledged swap
    before_window = state.read_text()
    refused = _swap(swap_binaries, tmp_path, cooldown_state=state, now=DURING)
    assert refused.returncode != 0
    assert designated() == SWAP_DEMOTING, "a refused swap must not move the designation"
    assert state.read_text() == before_window, "a refused swap must not touch the window"

    # --- The SUCCEEDED direction: both moved, together. ---
    confirmed = _swap(
        swap_binaries,
        tmp_path,
        cooldown_state=state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert _kv(confirmed.stdout)["designation-persisted"] == "durable"
    assert designated() == SWAP_CANDIDATE, "the swap must have moved the designation"

    window = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING + 1)).stdout)
    assert window["cooldown-state"] == "ACTIVE"
    assert window["cooldown-started-at-seconds"] == str(DURING), (
        "the window that exists must be the one this durable swap opened"
    )


@pytest.mark.parametrize(
    ("label", "demoting", "candidate"),
    [
        ("quote-in-demoting", 'reservoir-a"x', SWAP_CANDIDATE),
        ("backslash-in-candidate", SWAP_DEMOTING, "reservoir-b\\x"),
    ],
)
def test_an_unrecordable_id_refuses_the_swap_before_anything_is_published(
    swap_binaries, tmp_path, label: str, demoting: str, candidate: str
) -> None:
    """Adversarial review r11 [critical] — the gap between r2's rule and r4's probe.

    `cooldown_store` refuses an id carrying `"` or `\\` (its record is one hand-built
    JSON line, and its reader returns raw still-escaped text, so escaping would
    round-trip a DIFFERENT strategy id back). The pre-flight added in r4 proved the
    FILE was writable — which is not the same as proving THIS completion can be
    written. So a swap named with such an id passed the probe, ran, published the
    live designation, and only then failed to open its window: a durably promoted
    strategy with the automatic triggers still armed.

    The pre-flight now takes the ids and applies the same rule, so the refusal
    arrives while nothing has happened.
    """

    state = tmp_path / "cd.json"
    designation = tmp_path / "live.state"
    designation.write_text(f"{DESIGNATION_MAGIC}\ndesignated\t{demoting}\n")

    result = subprocess.run(
        [
            str(swap_binaries[PROMOTE_BIN]),
            "swap",
            "--state",
            str(designation),
            "--demoting",
            demoting,
            "--candidate",
            candidate,
            "--paper-state",
            str(tmp_path / "paper"),
            "--demotion-lock",
            str(tmp_path / "demotion-pending.json"),
            "--cooldown-state",
            str(state),
            "--now",
            str(COMPLETED_AT),
            "--liquidation",
            "flat",
            "--positions",
            "flat",
            "--deployed-version",
            "sha256:" + "a" * 64,
            "--allow-fixture-safety-inputs",
            "--confirm",
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, f"{label}: an unrecordable id must refuse the swap"
    fields = _kv(result.stdout)
    assert fields["refusal"] == "HOT_SWAP_COOLDOWN_UNRECORDABLE", result.stdout
    assert fields["demotion-outcome"] == "NOT_STARTED", result.stdout
    # NOTHING was published: the designation file is byte-identical to how it started,
    # and no window exists at all.
    assert designation.read_text() == f"{DESIGNATION_MAGIC}\ndesignated\t{demoting}\n"
    assert not state.exists(), f"{label}: a refused swap must not create a window"


def test_the_window_an_operator_reads_is_the_one_the_gate_enforced(swap_binaries, tmp_path) -> None:
    """Adversarial review r10 [critical] — one resolver, so the two cannot disagree.

    The gate used to take a `&CooldownState` from its caller, which made the printed
    `cooldown-state:` line and the state the gate acted on two INDEPENDENT reads —
    and made a caller-supplied window forgeable outright, since `CooldownState` is a
    public enum. The gate now reads the window itself through
    `HotSwapCooldownPort::resolve_window`, and the CLI resolves its proof lines
    through the same port.

    Asserted in BOTH directions, because agreement on one outcome is not agreement:
    an ACTIVE window must both PRINT active and REFUSE, and an EXPIRED one must both
    print expired and let the swap through.
    """

    # ACTIVE: the printed state and the enforced decision agree.
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    active_state = active_dir / "cd.json"
    _open_window(active_state)
    refused = _swap(swap_binaries, active_dir, cooldown_state=active_state, now=DURING)
    active_fields = _kv(refused.stdout)
    assert active_fields["cooldown-state"] == "ACTIVE"
    assert active_fields["cooldown-in-effect"] == "true"
    assert active_fields["refusal"] == "HOT_SWAP_COOLDOWN_CONFIRMATION_REQUIRED", (
        "the state the operator was shown must be the state that refused them"
    )

    # EXPIRED: the same agreement, in the direction that lets the swap through — the
    # non-vacuity half. Without it, a gate that refused everything would pass above.
    expired_dir = tmp_path / "expired"
    expired_dir.mkdir()
    expired_state = expired_dir / "cd.json"
    _open_window(expired_state)
    fired = _swap(swap_binaries, expired_dir, cooldown_state=expired_state, now=AFTER)
    expired_fields = _kv(fired.stdout)
    assert fired.returncode == 0, fired.stderr
    assert expired_fields["cooldown-state"] == "EXPIRED"
    assert expired_fields["cooldown-in-effect"] == "false"
    assert expired_fields["promotion"] == "PROMOTED"


def test_a_reported_promotion_always_carries_a_window_outcome(swap_binaries, tmp_path) -> None:
    """Adversarial review r8 [high] — the observable shadow of a compile-time guarantee.

    `HotSwapPromoted` is now opaque: `into_completed` is the only way to read what a
    swap did, and it redeems the SYS-49e window on the way. A caller therefore cannot
    report a promotion without having handled one — that half is enforced by the
    compiler and proven by two ``compile_fail`` doctests in `hot_swap_promotion.rs`,
    which build as external consumers of the crate.

    What a domain test CAN check is the shadow that discipline casts on the wire: no
    `promotion:PROMOTED` line without a `cooldown-window:` line beside it, on any
    path. Asserted across three outcomes so it is not one lucky case — a success, a
    refusal, and a success whose window failed to open.
    """

    # 1. A clean success.
    ok_state = tmp_path / "ok.json"
    ok = _swap(swap_binaries, tmp_path, cooldown_state=ok_state, now=COMPLETED_AT)
    assert ok.returncode == 0, ok.stderr
    ok_fields = _kv(ok.stdout)
    assert ok_fields["promotion"] == "PROMOTED"
    assert ok_fields["cooldown-window"] == "STARTED"

    # 2. A refusal reports no promotion, and therefore owes no window.
    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    blocked_state = blocked_dir / "cd.json"
    _open_window(blocked_state)
    blocked = _swap(swap_binaries, blocked_dir, cooldown_state=blocked_state, now=DURING)
    assert blocked.returncode != 0
    blocked_fields = _kv(blocked.stdout)
    assert blocked_fields["promotion"] == "BLOCKED"
    assert "cooldown-window" not in blocked_fields, (
        "a swap that did not promote owes no window and must not claim one"
    )

    # 3. A success whose window did NOT open still reports the outcome — the pairing
    #    holds in the case it most matters, which is the fail-open.
    kept_dir = tmp_path / "kept"
    kept_dir.mkdir()
    kept_state = kept_dir / "cd.json"
    _open_window(kept_state, completed_at=AFTER + 10_000)
    kept = _swap(
        swap_binaries,
        kept_dir,
        cooldown_state=kept_state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    kept_fields = _kv(kept.stdout)
    assert kept_fields["promotion"] == "PROMOTED"
    assert kept_fields["cooldown-window"] == "NOT_STARTED"


def test_a_failed_window_write_tells_the_operator_the_right_instant(
    swap_binaries, tmp_path
) -> None:
    """Adversarial review r7 [high] — the repair instruction is executable text.

    Round 5 separated "when the attempt started" from "when the swap completed",
    because a seven-day window stamped with the former is short by however long the
    swap took. Round 7 found that defect still living in the REMEDIATION: the failure
    message told the operator to reopen the window with the START instant, so anyone
    who followed it re-created by hand what the fix had just removed.

    Reached through the one path where the write fails but the pre-flight passes: a
    completion NEWER than this swap's is already stored, so `record_completion` keeps
    it (a window only ever moves forward) and reports `KeptNewer`, which the sink
    surfaces as an error.
    """

    state = tmp_path / "cd.json"
    # A completion from the future — newer than anything this swap can offer.
    _open_window(state, completed_at=AFTER + 10_000)

    # Acknowledged, because a future completion also reads as an ACTIVE window (a
    # clock that disagrees with history fails closed) and the confirmation gate would
    # otherwise refuse before the write is ever attempted.
    result = _swap(
        swap_binaries,
        tmp_path,
        cooldown_state=state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    assert result.returncode != 0, "a swap whose window did not open must not exit clean"
    fields = _kv(result.stdout)
    assert fields["promotion"] == "PROMOTED", "the swap itself succeeded"
    assert fields["cooldown-window"] == "NOT_STARTED", result.stdout

    # The remediation names the COMPLETION instant and says so in as many words.
    assert "record-completion --completed-at" in result.stderr, result.stderr
    assert "the instant this swap COMPLETED" in result.stderr, result.stderr
    assert "do not substitute the time the attempt started" in result.stderr, result.stderr
    assert "THE COOL-DOWN IS NOT IN EFFECT" in result.stderr, result.stderr


def test_an_interrupted_swap_leaves_a_window_that_still_suppresses(swap_binaries, tmp_path) -> None:
    """Adversarial review r13 [critical] — the interruption between publish and confirm.

    Round 6 moved the window write to AFTER the durable publish, which fixed one
    direction and opened the other. The publish and the window are two separate file
    writes; a crash, a kill or a full disk in between left the candidate live with no
    window at all, so the automatic triggers stayed armed on a strategy that had just
    been promoted. That is SYS-49e's exact failure, and it failed OPEN and silently —
    the one shape this feature exists to make impossible.

    The window is now written twice: provisionally before the demotion, confirmed
    after the publish. This is the state a killed process leaves behind, driven
    through the REAL binaries: the record is produced by the real writer and only the
    confirmation bit is cleared, which is precisely what an interruption does.

    Three readers, three processes, one guarantee — the interrupted swap suppresses:
    """
    state = tmp_path / "cd.json"
    log = tmp_path / "triggers.jsonl"
    _open_window(state)

    # The interruption: phase one's record, phase two never reached.
    record = json.loads(state.read_text())
    assert "provisional_completed_at_seconds" not in record, (
        "the real writer must leave no in-flight marker behind; if one is already "
        "here, the test is asserting nothing about the interrupted case"
    )
    # Phase one's record, in its own slot (r15), with phase two never reached. The
    # CONFIRMED triple is deliberately left absent: this is the first swap on this
    # store, so an interruption leaves only the in-flight marker.
    record["provisional_completed_at_seconds"] = record.pop("last_completed_at_seconds")
    record["provisional_demoted_strategy_id"] = record.pop("last_demoted_strategy_id")
    record["provisional_promoted_strategy_id"] = record.pop("last_promoted_strategy_id")
    state.write_text(json.dumps(record))

    # 1. The operator surface says a window is in effect...
    status = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING)).stdout)
    assert status["cooldown-state"] == "ACTIVE"
    assert status["cooldown-in-effect"] == "true"
    # ...and says WHICH KIND, because a window an operator cannot tell apart from a
    # completed one is a window they cannot resolve.
    assert status["cooldown-completion-provisional"] == "true"

    # 2. The automatic triggers are suppressed — the whole point. Before r13 this
    #    state did not exist: the process died and left NO window here.
    evaluated = _evaluate(state, log, DURING)
    assert evaluated.returncode == 0, evaluated.stderr
    fields = _kv(evaluated.stdout)
    assert fields["cooldown-state"] == "ACTIVE"
    assert fields["cooldown-suppressed"] == "true"
    assert fields["selected"] == "NONE"
    assert fields["fired-count"] == "0"
    assert _log_lines(log) == 0, "a suppressed evaluation must arm nothing"

    # 3. And a manual swap still needs the confirmation warning, so SYS-49a(a) holds
    #    across the interrupted state too.
    manual = _manual(state, log, DURING)
    assert manual.returncode != 0
    assert "COOLDOWN_CONFIRMATION_REQUIRED" in manual.stdout + manual.stderr


def test_an_operator_can_clear_a_stranded_provisional_window(tmp_path) -> None:
    """The repair path the contract's residual entry names, exercised end to end.

    Adversarial review r14 rewrote ``hot_swap_cooldown_contract.deferred`` to say what
    ACTUALLY remains after r13 closed the fail-open: an unconfirmed window that
    over-suppresses until an operator resolves it with the cool-down CLI. That entry
    is a claim about a repair path, and a claim in the registry with nothing
    exercising it is how the previous entry came to describe a mechanism under a name
    that no longer existed.

    So: strand a window, and check an operator really can get out of it.
    """
    state = tmp_path / "cd.json"
    log = tmp_path / "triggers.jsonl"
    _open_window(state)

    record = json.loads(state.read_text())
    record["provisional_completed_at_seconds"] = record.pop("last_completed_at_seconds")
    record["provisional_demoted_strategy_id"] = record.pop("last_demoted_strategy_id")
    record["provisional_promoted_strategy_id"] = record.pop("last_promoted_strategy_id")
    state.write_text(json.dumps(record))
    assert (
        _kv(_cooldown("status", "--state", str(state), "--now", str(DURING)).stdout)[
            "cooldown-completion-provisional"
        ]
        == "true"
    )

    # The repair: the operator records the completion they confirmed actually happened.
    # It goes through the SAME monotone writer the swap uses, so the repair cannot
    # shorten a window that a later real swap already opened.
    repaired = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        "live-a",
        "--promoted",
        "cand-b",
        "--completed-at",
        str(COMPLETED_AT + 60),
    )
    assert repaired.returncode == 0, repaired.stderr

    status = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING)).stdout)
    assert status["cooldown-completion-provisional"] == "false", (
        "the repair must clear the provisional flag, or the operator has no way out"
    )
    assert status["cooldown-state"] == "ACTIVE", "and the window itself must survive it"
    assert status["cooldown-started-at-seconds"] == str(COMPLETED_AT + 60)

    # ...and the window still suppresses afterwards, which is the point of repairing it
    # rather than deleting the file.
    evaluated = _evaluate(state, log, DURING)
    assert evaluated.returncode == 0, evaluated.stderr
    assert _kv(evaluated.stdout)["cooldown-suppressed"] == "true"


def test_the_repair_cannot_shorten_a_newer_window(tmp_path) -> None:
    # The other direction, and the reason the repair goes through the monotone writer:
    # an operator reconciling a stranded marker must not be able to pull a LIVE
    # window's start backwards and retire a safety interval early.
    state = tmp_path / "cd.json"
    _open_window(state, completed_at=COMPLETED_AT + 10_000)

    stale = _cooldown(
        "record-completion",
        "--state",
        str(state),
        "--demoted",
        "live-a",
        "--promoted",
        "cand-b",
        "--completed-at",
        str(COMPLETED_AT),
    )
    assert stale.returncode != 0, "an older completion must not silently win"
    status = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING)).stdout)
    assert status["cooldown-started-at-seconds"] == str(COMPLETED_AT + 10_000)


def test_an_acknowledged_manual_swap_buys_one_swap_not_a_lifted_cooldown(
    swap_binaries, tmp_path
) -> None:
    """Adversarial review r17, end to end across three real binaries.

    The waiver is per-swap and MANUAL-only. SYS-49a(a) lets an operator promote during
    a window by confirming; SYS-49e still ignores the automatic triggers throughout.
    Those are two rules about two callers, and the gate used to take a bare
    acknowledgement that could not tell them apart.

    What that means where an operator can see it: acknowledging one swap does not
    unlock the automatic path — not for the old window, and not for the new one the
    swap just opened.
    """
    state = tmp_path / "cd.json"
    log = tmp_path / "triggers.jsonl"
    _open_window(state)

    # Automatic evaluation inside the window: suppressed, as always.
    assert _kv(_evaluate(state, log, DURING).stdout)["cooldown-suppressed"] == "true"

    # The operator acknowledges and the manual swap FIRES — the asymmetry SYS-49a(a)
    # requires. Without this the assertions below would hold on a gate that refuses
    # everything, which would silently disable the whole promotion path.
    confirmed = _swap(
        swap_binaries,
        tmp_path,
        cooldown_state=state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    assert confirmed.returncode == 0, confirmed.stderr

    # ...and the automatic path is STILL suppressed, now by the window that swap just
    # opened. The acknowledgement bought one swap, not a lifted cool-down.
    after = _kv(_evaluate(state, log, DURING + 60).stdout)
    assert after["cooldown-state"] == "ACTIVE"
    assert after["cooldown-suppressed"] == "true"
    assert after["selected"] == "NONE"
    assert after["fired-count"] == "0"
    assert after["cooldown-started-at-seconds"] == str(DURING), (
        "the running window is the one THIS swap opened, so the seven days restart "
        "from its completion — an acknowledgement does not shorten what follows it"
    )
    assert _log_lines(log) == 0, "no automatic trigger armed at any point"


def test_a_confirmed_window_is_reported_as_confirmed(swap_binaries, tmp_path) -> None:
    """The other direction, without which the case above proves nothing.

    A surface that reported `provisional:true` unconditionally would satisfy every
    assertion in the interrupted test and be useless. This is a REAL swap, through the
    real gate and the real publish, and its window must come out confirmed.
    """
    state = tmp_path / "cd.json"
    result = _swap(swap_binaries, tmp_path, cooldown_state=state, now=DURING)
    assert result.returncode == 0, result.stderr

    status = _kv(_cooldown("status", "--state", str(state), "--now", str(DURING + 1)).stdout)
    assert status["cooldown-state"] == "ACTIVE"
    assert status["cooldown-completion-provisional"] == "false", (
        "a swap that ran to completion must confirm its own window; a provisional one "
        "left behind by a healthy swap would train an operator to ignore the flag"
    )


def test_a_swap_is_refused_when_its_window_could_not_be_recorded(swap_binaries, tmp_path) -> None:
    """Adversarial review r4 [critical] — the fail-open, closed at the only point it can be.

    A swap that completes and THEN cannot record its window leaves the automatic
    triggers armed against the strategy just promoted, and nothing can undo it: the
    designation has moved and the book is flat, so rolling back would be strictly
    worse. The requirement can therefore only be guaranteed before the swap runs.

    Driven against a genuinely unwritable directory, not a stubbed error, so the
    real `cooldown_store` write path is what fails.
    """
    import os
    import stat

    readonly = tmp_path / "readonly"
    readonly.mkdir()
    state = readonly / "cd.json"
    _open_window(state)
    before = state.read_text()
    # Drop write permission on the DIRECTORY: the store publishes by writing a
    # scratch file beside the target and renaming it, so this is what a real
    # unwritable store looks like to it.
    os.chmod(readonly, stat.S_IRUSR | stat.S_IXUSR)
    try:
        refused = _swap(swap_binaries, tmp_path, cooldown_state=state, now=DURING)
        assert refused.returncode != 0, "an unrecordable window must refuse the swap"
        fields = _kv(refused.stdout)
        assert fields["refusal"] == "HOT_SWAP_COOLDOWN_UNRECORDABLE", refused.stdout
        assert fields["demotion-outcome"] == "NOT_STARTED", refused.stdout
        assert fields["promotion"] == "BLOCKED", refused.stdout
        assert fields["designation-after"] == SWAP_DEMOTING, "the live slot must be untouched"
        assert "Nothing was demoted" in refused.stderr
    finally:
        os.chmod(readonly, stat.S_IRWXU)

    # The window that was already there is untouched — a refused swap changes nothing.
    assert state.read_text() == before


def test_a_swap_cannot_run_against_an_unreadable_window(swap_binaries, tmp_path) -> None:
    """UNKNOWN is not "no cool-down is in effect" (CLAUDE.md rule 3).

    A corrupt window must refuse the EXECUTION, not just the proposal — otherwise
    damaging the state file is a way to switch the cool-down off.

    The refusal is the UNRECORDABLE one, not the confirmation. A corrupt window
    cannot be classified AND cannot be updated (the store re-reads before it writes,
    so the completion write would fail too), and acknowledging a warning does not
    repair a file. Sending the operator to `--confirm-cooldown` here would send them
    in a circle; the accurate remedy is to repair the window. The state is still
    reported as UNKNOWN on the proof stream, so nothing is hidden.
    """

    state = tmp_path / "cd.json"
    state.write_text("{not json at all")

    refused = _swap(swap_binaries, tmp_path, cooldown_state=state, now=DURING)
    assert refused.returncode != 0
    fields = _kv(refused.stdout)
    assert fields["cooldown-state"] == "UNKNOWN"
    assert fields["refusal"] == "HOT_SWAP_COOLDOWN_UNRECORDABLE"
    assert fields["designation-after"] == SWAP_DEMOTING
    # And confirming does NOT get past it — the refusal is genuinely unwaivable.
    still_refused = _swap(
        swap_binaries,
        tmp_path,
        cooldown_state=state,
        now=DURING,
        extra=("--confirm-cooldown",),
    )
    assert still_refused.returncode != 0
    assert _kv(still_refused.stdout)["refusal"] == "HOT_SWAP_COOLDOWN_UNRECORDABLE"
