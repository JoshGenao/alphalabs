"""SRS-MD-001 runtime / SyRS SYS-70 - the thread-safe fan-out runtime drives
exactly ONE upstream market-data line per security and delivers each tick to
every subscriber's own bounded queue, without letting a slow reader delay the
others.

L7 domain (safety) test. The Rust integration test at
``crates/atp-market-data/tests/srs_md_001_fanout_runtime.rs`` drives
``FanOutRuntime`` over a fake upstream feed; this Python test shells out to
``cargo test`` for each post-condition so the domain-test layer anchors them,
and pins the honest scope: the real IB feed + PTP-clock latency evidence is
serialized, so ``feature_list.json`` keeps SRS-MD-001 ``passes:false``.

The latency probe is a wall-clock test, so it is ``#[ignore]``-gated in Rust
and run here explicitly with ``--ignored``. Its budget is 100 ms against a
measured tail in the low hundreds of microseconds, so host load cannot plausibly
flip it; a red here is a real regression, not a flake to re-run.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.domain, pytest.mark.safety]


REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_BINARY = "srs_md_001_fanout_runtime"


def _run_cargo_test(test_name: str, *, ignored: bool = False) -> subprocess.CompletedProcess[str]:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip(reason="cargo not on PATH; cannot run Rust integration test")
    return subprocess.run(
        [
            cargo,
            "test",
            "-p",
            "atp-market-data",
            "--test",
            TEST_BINARY,
            test_name,
            "--",
            "--exact",
            *(["--ignored"] if ignored else []),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_passed(test_name: str, *, ignored: bool = False) -> None:
    result = _run_cargo_test(test_name, ignored=ignored)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"SRS-MD-001 runtime domain test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "1 passed" in combined, f"unexpected cargo test output:\n{combined}"


@pytest.mark.parametrize(
    "test_name",
    [
        # Core AC: many subscribers -> one upstream open; every one receives.
        "srs_md_001_many_subscribers_drive_one_upstream_open",
        # The upstream line closes only when the last subscriber leaves.
        "srs_md_001_close_runs_only_when_last_subscriber_leaves",
        # Fail closed: a refused upstream open registers and publishes nothing.
        "srs_md_001_failed_upstream_open_registers_nothing",
        # Fail closed: an over-limit request never opens-then-closes a line.
        "srs_md_001_over_limit_request_never_touches_upstream",
        # SRS-MD-005 / SYS-45: no upstream request while the window is closed
        # or the gateway is lost; a window closing mid-admission releases the line.
        "srs_md_001_suspended_window_sends_nothing_upstream",
        "srs_md_001_lost_gateway_sends_nothing_upstream",
        "srs_md_001_window_closing_mid_admission_releases_the_line",
        # Fail closed: no subscription without a delivery queue; options rejected.
        "srs_md_001_subscribe_requires_registered_queue",
        # Isolation + order: a subscriber gets only its securities, in order.
        "srs_md_001_delivery_is_isolated_and_ordered",
        # Operator policy: a slow reader drops only its own ticks + lag event.
        "srs_md_001_slow_reader_drops_only_its_own_ticks",
        # A gone receiver is deregistered and its sole-held lines closed.
        "srs_md_001_dropped_receiver_releases_its_lines",
        "srs_md_001_dropped_receiver_on_an_idle_security_frees_its_line",
        "srs_md_001_stale_departure_note_never_tears_down_a_re_registration",
        "srs_md_001_reap_departed_counts_each_dropped_subscriber_once",
        # A panic under the lock poisons it: every mutation refuses, the
        # SRS-MD-002 probe refuses capacity, reads report without reaping.
        "srs_md_001_poisoned_lock_fails_every_mutation_closed",
        "srs_md_001_deregister_releases_every_line",
        # The runtime is the live line counter for the SRS-MD-002 gate.
        "srs_md_001_runtime_is_the_md_002_line_counter",
        # Thread safety: concurrent subscribers still share one upstream open.
        "srs_md_001_concurrent_subscribers_share_one_line",
    ],
)
def test_fanout_runtime_post_condition(test_name: str) -> None:
    _assert_passed(test_name)


def test_fixture_feed_latency_probe_is_within_budget_and_honestly_labelled() -> None:
    # AC: <= 100 ms added fan-out latency. On the fixture feed the probe
    # asserts p99.9 within budget and writes the percentiles it measured.
    artifact = REPO_ROOT / "target" / "verification" / "srs_md_001_fanout_latency_probe.json"
    artifact.unlink(missing_ok=True)  # the report must come from THIS run
    _assert_passed("srs_md_001_fanout_latency_probe_on_fixture_feed", ignored=True)
    report = json.loads(artifact.read_text(encoding="utf-8"))
    latency = report["latency_ns"]
    for key in ("p50", "p95", "p99", "p99_9", "max"):
        assert isinstance(latency[key], int), key
    assert latency["p50"] <= latency["p95"] <= latency["p99"] <= latency["p99_9"] <= latency["max"]
    assert report["budget_ns"] == 100 * 1_000_000
    assert latency["p99_9"] <= report["budget_ns"]
    assert report["p99_9_within_budget"] is True
    assert report["dropped"] == 0
    assert report["samples"] == report["subscribers"] * report["ticks"]
    # Scope honesty: the probe must not pass itself off as the AC's artifact.
    assert "not an SRS-MD-001 verification artifact" in report["kind"]
    assert "PTP not verified" in report["clock"]
    assert "no IB" in report["feed"]


def test_scope_honesty_feature_stays_unflipped() -> None:
    # The real IB feed + PTP-clock evidence is serialized to the operator,
    # so SRS-MD-001 must not be marked passing by this work.
    features = json.loads((REPO_ROOT / "feature_list.json").read_text(encoding="utf-8"))
    feature = next(f for f in features if f["id"] == "SRS-MD-001")
    assert feature["passes"] is False
    services = json.loads(
        (REPO_ROOT / "architecture" / "runtime_services.json").read_text(encoding="utf-8")
    )
    deferred = services["subscription_fanout_contract"]["deferred"]
    runtime_item = next(d for d in deferred if d["feature"] == "SRS-MD-001-runtime")
    assert "SERIALIZED" in runtime_item["what"]
