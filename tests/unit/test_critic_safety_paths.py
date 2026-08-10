"""L1 — SAFETY_PATH_RE coverage (tools/critic_check.py).

Heartbeat freshness is the INPUT to stale-data blocking. If the heartbeat is wrong,
the `stale[_-]?data` guard never fires and a strategy trades on data nobody is
checking — yet no heartbeat path matched SAFETY_PATH_RE while `kill_switch` did. So
SRS-MD-003 could retune the live heartbeat producer with no paired tests/domain/
test demanded. That session shipped one voluntarily and recorded the gap for the
operator; this closes it.

The first proposed fix was `/heartbeat\\.py`, which would have missed
crates/atp-market-data/src/bin/md003_heartbeat_cli.rs — the Rust producer the
session actually changed.
"""

from __future__ import annotations

import critic_check as c
import pytest

pytestmark = pytest.mark.unit


def _blocks(files):
    diff = c.DiffSlice(
        files_changed=tuple(files),
        files_added=(),
        files_deleted=(),
        unified_diff="",
        commit_message="feat: x",
    )
    report = c.Report()
    c.check_safety_critical_paired(diff, report)
    return [f for f in report.findings if f.severity == "block"]


@pytest.mark.parametrize(
    "path",
    [
        "python/atp_dashboard/heartbeat.py",
        "crates/atp-market-data/src/bin/md003_heartbeat_cli.rs",  # the .py-only fix missed this
        "crates/atp-market-data/tests/srs_md_003_heartbeat_freshness.rs",
        "tests/unit/test_heartbeat_source.py",
    ],
)
def test_heartbeat_paths_are_safety_paths(path):
    assert c.SAFETY_PATH_RE.search(path), f"{path} must be a safety path"


def test_kill_switch_coverage_is_not_regressed():
    """The token that already worked must keep working."""
    assert c.SAFETY_PATH_RE.search("crates/atp-execution/src/kill_switch.rs")


def test_a_heartbeat_change_without_a_domain_test_is_blocked():
    blocks = _blocks(["python/atp_dashboard/heartbeat.py"])
    assert blocks and blocks[0].rule == "safety:paired-test-required"


def test_a_heartbeat_change_with_a_domain_test_passes():
    assert (
        _blocks(
            [
                "python/atp_dashboard/heartbeat.py",
                "tests/domain/test_heartbeat_staleness.py",
            ]
        )
        == []
    )


def test_unrelated_paths_are_still_unaffected():
    assert _blocks(["python/atp_strategy/indicators.py"]) == []


def test_documentation_carve_outs_still_apply():
    """A note or playbook NAMED for a safety subject carries no behavior to test."""
    assert _blocks(["docs/playbooks/safety-paths.md"]) == []
    assert _blocks(["progress.d/session-SRS-SAFE-001.md"]) == []
