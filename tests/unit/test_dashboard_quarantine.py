"""L1 unit tests: the SRS-DATA-013 quarantine pane provider (SyRS SYS-77).

The pane is honest only if three things hold, and these tests pin each one:

* unconfigured, unreadable and "no runs yet" are three different states, and
  none of them carries a count (``None``, never ``[]`` or ``0``);
* the CLI output is parsed strictly: drift is reported as unreadable, never
  rendered as data;
* the two legs fail independently: a broken notification store blanks the
  alert column, never the counts, and a run the two reads disagree about is
  UNKNOWN rather than guessed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from atp_dashboard.quarantine import (  # noqa: E402
    QUARANTINE_REASONS,
    QuarantinePaneProvider,
    QuarantineUnavailable,
    parse_status,
    parse_summary,
)

pytestmark = pytest.mark.unit

RUN_A = "00000001793014164151-0000033797-000000"
RUN_B = "00000001793014164114-0000033795-000000"


def _summary_run(
    i: int, run_id: str, *, held: int, written: int = 4, primary: str = "WRITTEN"
) -> str:
    counts = [1] * held + [0] * (6 - held)
    lines = [
        f"run_{i}_id:{run_id}",
        f"run_{i}_status:readable",
        f"run_{i}_recorded_at_millis:1793014164151",
        f"run_{i}_observed_at_seconds:1700000000",
        f"run_{i}_records_in:10",
        f"run_{i}_primary:{primary}",
    ]
    if primary == "WRITTEN":
        lines.append(f"run_{i}_written:{written}")
    lines.append(f"run_{i}_quarantined_total:{held}")
    lines += [f"run_{i}_count_{r}:{c}" for r, c in zip(QUARANTINE_REASONS, counts, strict=True)]
    return "\n".join(lines)


def _summary(*runs: str, total: int | None = None) -> str:
    total = len(runs) if total is None else total
    head = [
        "quarantine_dir:/ssd/quarantine",
        f"total_runs:{total}",
        f"returned:{len(runs)}",
        f"truncated:{str(len(runs) < total).lower()}",
    ]
    return "\n".join(head + list(runs)) + "\n"


def _status(*runs: tuple[str, str, str]) -> str:
    lines = [
        "notification_store:/alerts",
        f"total_runs:{len(runs)}",
        f"returned:{len(runs)}",
        "invalid_markers:0",
    ]
    for i, (run_id, state, attempt) in enumerate(runs):
        lines += [f"run_{i}_id:{run_id}", f"run_{i}_alert:{state}", f"run_{i}_settled:false"]
        if attempt == "stored":
            lines += [
                f"run_{i}_attempt:stored",
                f"run_{i}_attempt_at_millis:1793014164200",
                f"run_{i}_channel_EMAIL:QUEUED",
                f"run_{i}_channel_PUSH:DELIVERED",
            ]
        else:
            lines.append(f"run_{i}_attempt:none")
    return "\n".join(lines) + "\n"


class FakeRunner:
    """Answers each binary from a table; records every argv."""

    def __init__(self, summary: object, status: object = None) -> None:
        self.answers = {"summary": summary, "status": status}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        answer = self.answers[argv[1]]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, tuple):
            code, err = answer
            return subprocess.CompletedProcess(argv, code, "", err)
        return subprocess.CompletedProcess(argv, 0, answer, "")


def _provider(runner: FakeRunner, *, ssd: str | None = "/ssd", store: str | None = "/alerts"):
    return QuarantinePaneProvider(
        ssd_dir=ssd,
        notification_store_dir=store,
        runner=runner,
        summary_binary="/bin/summary_cli",
        alert_binary="/bin/alert_cli",
        clock=lambda: "2026-10-26T00:00:00Z",
    )


# --------------------------------------------------------------------------- #
# The three "no count" states
# --------------------------------------------------------------------------- #


def test_unconfigured_reads_nothing_and_carries_no_count() -> None:
    runner = FakeRunner(_summary())
    body = _provider(runner, ssd=None).quarantine_snapshot()
    assert body["state"] == "not_configured"
    assert body["runs"] is None and body["total_runs"] is None
    assert "ATP_SSD_DATA_DIR" in body["error"]
    assert runner.calls == [], "nothing is read when nothing is configured"
    assert body["alerts"]["ok"] is None and body["alerts"]["due"] is None


def test_an_unreadable_store_is_not_an_empty_one() -> None:
    runner = FakeRunner(
        (1, "summary_cli: quarantine store unavailable: the SSD primary directory is missing")
    )
    body = _provider(runner).quarantine_snapshot()
    assert body["state"] == "unreadable"
    assert body["runs"] is None and body["total_runs"] is None
    assert "SSD primary directory is missing" in body["error"]


def test_no_runs_is_its_own_state_with_an_empty_list() -> None:
    body = _provider(FakeRunner(_summary(), _status())).quarantine_snapshot()
    assert body["state"] == "no_runs"
    assert body["runs"] == [] and body["total_runs"] == 0


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(cmd="summary_cli", timeout=3.0),
        FileNotFoundError("summary_cli not found"),
    ],
    ids=["timeout", "missing-binary"],
)
def test_a_cli_that_cannot_run_is_unreadable(error: BaseException) -> None:
    body = _provider(FakeRunner(error)).quarantine_snapshot()
    assert body["state"] == "unreadable"
    assert body["runs"] is None


# --------------------------------------------------------------------------- #
# Headline from the newest run
# --------------------------------------------------------------------------- #


def test_the_newest_run_sets_the_headline() -> None:
    quarantined = _summary(
        _summary_run(0, RUN_A, held=6), _summary_run(1, RUN_B, held=0, written=10)
    )
    body = _provider(FakeRunner(quarantined, _status())).quarantine_snapshot()
    assert body["state"] == "quarantined"
    run = body["runs"][0]
    assert run["quarantined_total"] == 6 and run["written"] == 4
    assert run["counts"] == dict.fromkeys(QUARANTINE_REASONS, 1)

    clean_first = _summary(
        _summary_run(0, RUN_A, held=0, written=10), _summary_run(1, RUN_B, held=6)
    )
    assert _provider(FakeRunner(clean_first, _status())).quarantine_snapshot()["state"] == "clean"


def test_a_pending_run_with_nothing_held_is_in_progress_not_clean() -> None:
    pending = _summary(_summary_run(0, RUN_A, held=0, primary="PENDING"))
    body = _provider(FakeRunner(pending, _status())).quarantine_snapshot()
    assert body["state"] == "in_progress"
    assert body["runs"][0]["written"] is None


def test_an_unreadable_newest_run_is_reported_not_skipped() -> None:
    unreadable = _summary(
        f"run_0_id:{RUN_A}\nrun_0_status:unreadable\nrun_0_error:corrupt quarantine run: checksum mismatch",
        _summary_run(1, RUN_B, held=0, written=10),
    )
    body = _provider(FakeRunner(unreadable, _status())).quarantine_snapshot()
    assert body["state"] == "run_unreadable"
    assert body["runs"][0] == {
        "run_id": RUN_A,
        "status": "unreadable",
        "error": "corrupt quarantine run: checksum mismatch",
        "alert": None,
    }


# --------------------------------------------------------------------------- #
# Strict parsing: drift is unreadable, never data
# --------------------------------------------------------------------------- #


GOOD = _summary(_summary_run(0, RUN_A, held=6))


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda s: s.replace("run_0_count_NEGATIVE_VOLUME:1", "run_0_count_NEGATIVE_VOLUME:2"),
            "add to",
        ),
        (lambda s: s.replace("run_0_count_RANGE_VIOLATION:1\n", ""), "missing or unexpected"),
        (lambda s: s + "run_0_extra:1\n", "missing or unexpected"),
        (lambda s: s + "surprise:1\n", "unknown key"),
        (
            lambda s: s.replace("run_0_primary:WRITTEN", "run_0_primary:MAYBE"),
            "missing or unexpected",
        ),
        (lambda s: s.replace("run_0_written:4", "run_0_written:5"), "of 10 records"),
        (
            lambda s: (
                s.replace("total_runs:1", "total_runs:3")
                .replace("returned:1", "returned:2")
                .replace("truncated:false", "truncated:true")
            ),
            "entries",
        ),
        (lambda s: s.replace("truncated:false", "truncated:true"), "disagree"),
        (lambda s: s.replace("run_0_records_in:10", "run_0_records_in:-1"), "not a count"),
        (
            lambda s: s.replace(f"run_0_id:{RUN_A}", f"run_0_id:{RUN_A}\rforged"),
            "control character",
        ),
        (lambda s: s + "total_runs:1\n", "appears twice"),
        (lambda s: s + "no colon here\n", "no key"),
    ],
    ids=[
        "counts-do-not-add-up",
        "missing-reason",
        "unknown-run-field",
        "unknown-header",
        "unknown-primary",
        "written-exceeds-records",
        "returned-mismatch",
        "truncated-mismatch",
        "negative-count",
        "control-character",
        "duplicate-key",
        "line-without-key",
    ],
)
def test_summary_drift_is_refused(mutate, needle: str) -> None:
    with pytest.raises(QuarantineUnavailable, match=needle):
        parse_summary(mutate(GOOD))
    # And through the provider it is the unreadable state, with no partial runs.
    body = _provider(FakeRunner(mutate(GOOD), _status())).quarantine_snapshot()
    assert body["state"] == "unreadable" and body["runs"] is None


def test_the_good_fixture_parses() -> None:
    assert parse_summary(GOOD)["runs"][0]["quarantined_total"] == 6


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda s: s.replace("run_0_alert:ALERTED", "run_0_alert:PROBABLY"), "alert"),
        (lambda s: s.replace("run_0_channel_PUSH:DELIVERED", "run_0_channel_PUSH:SENT"), "outcome"),
        (lambda s: s.replace("run_0_settled:false", "run_0_settled:no"), "true/false"),
        (lambda s: s.replace("run_0_attempt:stored", "run_0_attempt:maybe"), "attempt"),
        (lambda s: s.replace("invalid_markers:0", "invalid_markers:1"), "entries"),
    ],
    ids=["unknown-state", "unknown-outcome", "settled-not-bool", "unknown-attempt", "marker-count"],
)
def test_status_drift_is_refused(mutate, needle: str) -> None:
    good = _status((RUN_A, "ALERTED", "stored"))
    parse_status(good)
    with pytest.raises(QuarantineUnavailable, match=needle):
        parse_status(mutate(good))


# --------------------------------------------------------------------------- #
# The alert leg is independent, and never guessed
# --------------------------------------------------------------------------- #


def test_alert_delivery_is_joined_to_each_run() -> None:
    body = _provider(FakeRunner(GOOD, _status((RUN_A, "ALERTED", "stored")))).quarantine_snapshot()
    alert = body["runs"][0]["alert"]
    assert alert["state"] == "ALERTED"
    assert alert["channels"] == {"EMAIL": "QUEUED", "PUSH": "DELIVERED"}
    assert body["alerts"] == {
        "configured": True,
        "ok": True,
        "error": None,
        "invalid_markers": 0,
        "due": 0,
    }


def test_a_broken_alert_store_blanks_the_alert_column_not_the_counts() -> None:
    runner = FakeRunner(GOOD, (2, "alert_cli: notification store unreadable: bad magic"))
    body = _provider(runner).quarantine_snapshot()
    assert body["state"] == "quarantined"
    assert body["runs"][0]["quarantined_total"] == 6
    assert body["runs"][0]["alert"] is None
    assert body["alerts"]["ok"] is False and "bad magic" in body["alerts"]["error"]
    assert body["alerts"]["due"] is None


def test_an_unset_alert_store_is_unknown_not_nothing_due() -> None:
    runner = FakeRunner(GOOD)
    body = _provider(runner, store=None).quarantine_snapshot()
    assert body["runs"][0]["alert"] is None
    assert body["alerts"]["configured"] is False and body["alerts"]["due"] is None
    assert "ATP_NOTIFICATION_STORE_DIR" in body["alerts"]["error"]
    assert [call[1] for call in runner.calls] == ["summary"], "the alert CLI was never run"


def test_a_run_only_one_read_saw_is_unknown_and_the_due_count_is_withheld() -> None:
    body = _provider(
        FakeRunner(
            _summary(_summary_run(0, RUN_A, held=6), _summary_run(1, RUN_B, held=6)),
            _status((RUN_B, "DUE", "none")),
        )
    ).quarantine_snapshot()
    assert body["runs"][0]["alert"] is None, "RUN_A appeared between the two reads"
    assert body["runs"][1]["alert"]["state"] == "DUE"
    assert body["alerts"]["due"] is None, "a partial count would read as a total"
    assert "between the two reads" in body["alerts"]["error"]


def test_both_clis_get_explicit_locations_and_the_same_limit() -> None:
    runner = FakeRunner(GOOD, _status((RUN_A, "DUE", "none")))
    _provider(runner).quarantine_snapshot()
    summary, status = runner.calls
    assert summary == ["/bin/summary_cli", "summary", "--ssd", "/ssd", "--limit", "20"]
    assert status == [
        "/bin/alert_cli",
        "status",
        "--ssd",
        "/ssd",
        "--store",
        "/alerts",
        "--limit",
        "20",
    ]


def test_the_default_runner_gives_the_cli_an_empty_environment(monkeypatch, tmp_path) -> None:
    """Neither CLI may fall back to an ambient ATP_SSD_DATA_DIR the composition did not choose."""

    from atp_dashboard import quarantine

    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, _summary(), "")

    binary = tmp_path / "summary_cli"
    binary.write_text("")
    monkeypatch.setattr(quarantine.subprocess, "run", fake_run)
    monkeypatch.setenv("ATP_SSD_DATA_DIR", "/somewhere/else")
    quarantine._default_runner([str(binary), "summary"], timeout=1.0)
    assert seen["env"] == {}


# --------------------------------------------------------------------------- #
# One read serves the poll interval (the O(history) read is not per request)
# --------------------------------------------------------------------------- #


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_one_read_serves_every_request_within_the_interval() -> None:
    runner = FakeRunner(GOOD, _status((RUN_A, "DUE", "none")))
    clock = Clock()
    provider = QuarantinePaneProvider(
        ssd_dir="/ssd",
        notification_store_dir="/alerts",
        runner=runner,
        min_interval_s=5.0,
        monotonic=clock,
    )
    first = provider.quarantine_snapshot()
    clock.now += 4.9
    assert provider.quarantine_snapshot() == first
    assert len(runner.calls) == 2, "one summary + one status read, shared"
    clock.now += 0.1
    provider.quarantine_snapshot()
    assert len(runner.calls) == 4, "the interval passed: a fresh read"


def test_a_served_body_cannot_edit_the_next_one() -> None:
    provider = QuarantinePaneProvider(
        ssd_dir="/ssd", notification_store_dir="/alerts", runner=FakeRunner(GOOD, _status())
    )
    provider.quarantine_snapshot()["runs"][0]["quarantined_total"] = 0
    assert provider.quarantine_snapshot()["runs"][0]["quarantined_total"] == 6


def test_concurrent_requests_share_one_read() -> None:
    import threading

    gate = threading.Event()

    class SlowRunner(FakeRunner):
        def __call__(self, argv, *, timeout):
            gate.wait(5)
            return super().__call__(argv, timeout=timeout)

    runner = SlowRunner(GOOD, _status())
    provider = QuarantinePaneProvider(
        ssd_dir="/ssd", notification_store_dir="/alerts", runner=runner, min_interval_s=60.0
    )
    bodies: list[dict] = []
    threads = [
        threading.Thread(target=lambda: bodies.append(provider.quarantine_snapshot()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(10)
    assert len(bodies) == 4
    assert len(runner.calls) == 2, "four requests, one read"


def test_the_worst_case_read_fits_inside_the_browser_poll() -> None:
    """Review r12: two reads back to back must land before the browser aborts the poll.

    If they cannot, every poll aborts, the next poll starts a fresh slow read, and the
    pane never recovers from "endpoint unreachable" even though every read succeeds.
    """
    import re

    from atp_dashboard import quarantine

    app_js = (PYTHON_ROOT / "atp_dashboard" / "assets" / "app.js").read_text()
    poll_ms = int(re.search(r"const POLL_MS = (\d+);", app_js).group(1))
    assert quarantine.BROWSER_POLL_BUDGET_S == poll_ms / 1000, "the provider's copy drifted"
    assert re.search(r"fetch\(QUARANTINE_ROUTE, \{[^}]*AbortSignal\.timeout\(POLL_MS\)", app_js), (
        "the pane's fetch is no longer bounded by POLL_MS"
    )
    worst = 2 * quarantine._DEFAULT_TIMEOUT_S
    assert worst <= 0.8 * quarantine.BROWSER_POLL_BUDGET_S, (
        f"two {quarantine._DEFAULT_TIMEOUT_S}s reads ({worst}s) leave no room inside "
        f"the {poll_ms}ms poll"
    )


def test_a_slow_read_still_serves_the_whole_interval() -> None:
    clock = Clock()

    class SlowRunner(FakeRunner):
        def __call__(self, argv, *, timeout):
            clock.now += 1.4  # each CLI takes most of its budget
            return super().__call__(argv, timeout=timeout)

    runner = SlowRunner(GOOD, _status())
    provider = QuarantinePaneProvider(
        ssd_dir="/ssd",
        notification_store_dir="/alerts",
        runner=runner,
        min_interval_s=5.0,
        monotonic=clock,
    )
    provider.quarantine_snapshot()
    clock.now += 4.0  # the next poll: 6.8 s after the read began, 4 s after it ended
    provider.quarantine_snapshot()
    assert len(runner.calls) == 2, "stamped at the start, the read had already expired"


def test_a_status_leg_returning_more_runs_than_it_has_is_refused() -> None:
    good = _status((RUN_A, "DUE", "none"))
    with pytest.raises(QuarantineUnavailable, match="exceeds total_runs"):
        parse_status(good.replace("total_runs:1", "total_runs:0"))
