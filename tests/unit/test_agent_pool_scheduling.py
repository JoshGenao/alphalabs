"""L1 — Unit tests for the impact-ranked scheduler + deadlock detector.

Covers the additions that steer the greedy self-claim scheduler toward keystones
and make its terminal state legible (done vs deadlock), plus the anti-churn
``awaiting_verification`` bucket:
  - impact_scores: transitive reverse-dependency closure
  - pick_order: keystone-first ordering, subsystem-conflict avoidance still primary
  - serialized_notes: read ``Outcome: serialized`` resume notes
  - assess_frontier: done / progressing / deadlock + guarded root blockers
"""

import agent_pool
import pytest

pytestmark = pytest.mark.unit


def _feat(fid, category="data", passes=False, priority="P1", description="", steps=None):
    return {
        "id": fid,
        "category": category,
        "priority": priority,
        "passes": passes,
        "needs_clarification": False,
        "description": description,
        "steps": steps or [],
    }


# --- impact scoring ---------------------------------------------------------
def test_impact_scores_counts_transitive_descendants():
    # K is the keystone: B and C depend on it directly, D depends on C.
    by_id = {f: _feat(f) for f in ("K", "B", "C", "D", "LEAF")}
    deps = {"B": ["K"], "C": ["K"], "D": ["C"]}
    impact = agent_pool.impact_scores(deps, by_id)
    assert impact["K"] == 3  # B, C, D all transitively unblocked by K
    assert impact["C"] == 1  # only D
    assert impact["LEAF"] == 0  # unblocks nothing


def test_impact_scores_handles_no_deps():
    by_id = {"A": _feat("A"), "B": _feat("B")}
    assert agent_pool.impact_scores({}, by_id) == {"A": 0, "B": 0}


# --- pick_order -------------------------------------------------------------
def test_pick_order_puts_keystone_before_alphabetical_leaf():
    # Without impact, "AAA" (leaf) would sort before "ZZZ" (keystone) by id.
    by_id = {"AAA": _feat("AAA"), "ZZZ": _feat("ZZZ"), "B": _feat("B"), "C": _feat("C")}
    deps = {"B": ["ZZZ"], "C": ["ZZZ"]}
    impact = agent_pool.impact_scores(deps, by_id)
    order = agent_pool.pick_order(["AAA", "ZZZ"], by_id, set(), impact)
    assert order[0] == "ZZZ"  # keystone first despite later id


def test_pick_order_subsystem_conflict_stays_primary():
    # A sibling holds atp-data; even a higher-impact data feature yields to a
    # conflict-free feature in another subsystem.
    by_id = {
        "DATA_KEYSTONE": _feat("DATA_KEYSTONE", category="data"),
        "EXE_LEAF": _feat("EXE_LEAF", category="execution"),
        "X": _feat("X"),
    }
    deps = {"X": ["DATA_KEYSTONE"]}
    impact = agent_pool.impact_scores(deps, by_id)  # DATA_KEYSTONE=1, EXE_LEAF=0
    held = {"atp-data"}
    order = agent_pool.pick_order(["DATA_KEYSTONE", "EXE_LEAF"], by_id, held, impact)
    assert order[0] == "EXE_LEAF"  # conflict-avoidance beats impact


def test_pick_order_defaults_to_legacy_without_impact():
    by_id = {"B": _feat("B"), "A": _feat("A")}
    assert agent_pool.pick_order(["B", "A"], by_id, set()) == ["A", "B"]  # id order


# --- serialized_notes (anti-churn) -----------------------------------------
def test_serialized_notes_reads_outcome(tmp_path):
    (tmp_path / "session-SRS-EXE-006.md").write_text(
        "=== SESSION SRS-EXE-006 ===\nOutcome: serialized\nWhat I did: ...\n"
    )
    # The template menu line starts with "complete" — must NOT false-positive.
    (tmp_path / "session-SRS-FOO-001.md").write_text(
        "Outcome: complete | serialized | partial(blocked-on X)\n"
    )
    (tmp_path / "session-SRS-BAR-002.md").write_text("Outcome: partial(blocked-on Y)\n")
    got = agent_pool.serialized_notes(tmp_path)
    assert got == {"SRS-EXE-006"}


def test_serialized_notes_missing_dir_is_empty(tmp_path):
    assert agent_pool.serialized_notes(tmp_path / "nope") == set()


# --- assess_frontier --------------------------------------------------------
def test_assess_frontier_done_when_all_pass():
    features = [_feat("A", passes=True), _feat("B", passes=True)]
    a = agent_pool.assess_frontier(features, {}, {"leases": {}})
    assert a["state"] == "done" and a["passed"] == a["total"] == 2


def test_assess_frontier_progressing_with_ready_work():
    features = [_feat("A", passes=True), _feat("B")]
    a = agent_pool.assess_frontier(features, {"B": ["A"]}, {"leases": {}}, skip_awaiting=False)
    assert a["state"] == "progressing" and "B" in a["ready"]


def test_assess_frontier_deadlock_names_guarded_blocker(monkeypatch):
    # R is ready but awaiting human verification (serialized note); everything
    # else depends on R → nothing claimable → deadlock, and R is guarded because
    # its text trips the IB/dashboard honesty guard.
    features = [
        _feat("R", category="ui_requirements", description="provide a web dashboard"),
        _feat("C1"),
        _feat("C2"),
    ]
    deps = {"C1": ["R"], "C2": ["R"]}
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: {"R"})
    a = agent_pool.assess_frontier(features, deps, {"leases": {}})
    assert a["state"] == "deadlock"
    assert a["awaiting_verification"] == ["R"]
    assert "R" in a["root_blockers"]
    assert "R" in a["guarded_root_blockers"]  # matches "dashboard" keyword
