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
and is NOT run by this suite (a debug-build timing assertion does not belong in
the default gate). It runs through the evidence recorder instead:
``python3 tools/evidence.py run SRS-MD-001 --step 2 -- cargo test -p
atp-market-data --test srs_md_001_fanout_runtime -- --include-ignored``. This
suite pins, statically, that it stays gated and honestly labelled.
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
        # Deterministic overlap: a drop while ingest holds the lock fires both
        # release paths for one subscriber; the line is released exactly once.
        "srs_md_001_receiver_dropped_while_ingest_holds_the_lock_is_released_once",
        # Receivers dropped on other threads mid-ingest: each line closes once.
        "srs_md_001_receivers_dropped_during_ingest_release_every_line_once",
    ],
)
def test_fanout_runtime_post_condition(test_name: str) -> None:
    _assert_passed(test_name)


def test_latency_probe_stays_gated_and_honestly_labelled() -> None:
    # The probe is wall-clock: it must stay #[ignore]-gated so the default
    # gate never runs a timing assertion, and its report must say it is NOT
    # the AC's verification artifact (no IB feed, no PTP clock).
    source = (REPO_ROOT / "crates" / "atp-market-data" / "tests" / f"{TEST_BINARY}.rs").read_text(
        encoding="utf-8"
    )
    probe = "fn srs_md_001_fanout_latency_probe_on_fixture_feed()"
    assert source.count(probe) == 1
    head = source[: source.index(probe)].rstrip().splitlines()
    assert head[-1].startswith("#[ignore"), "the wall-clock probe must stay #[ignore]-gated"
    assert head[-2] == "#[test]"
    body = source[source.index(probe) :]
    for label in (
        "not an SRS-MD-001 verification artifact",
        "PTP not verified",
        "fixture (in-process; no IB)",
        "SUBSCRIPTION_FANOUT_LATENCY_MS",
        "LatencyPercentiles::from_samples",
    ):
        assert label in body, label


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
