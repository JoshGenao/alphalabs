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
