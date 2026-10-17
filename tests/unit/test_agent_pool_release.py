"""L1 — Release scoping in the scheduler (docs/SRS.md §3.1).

The board deadlocked because MVP work waited on features nobody needed for the
MVP. These pin the contract that fixed it:
  - a deferred (R2) feature is never offered, and never blocks an MVP feature
  - an untagged feature fails closed: not offered, still blocking, still in scope
  - "done" means the active release is done, not the whole backlog
  - `claim --id` will not take a feature outside the active release
"""

import argparse
import contextlib

import agent_pool
import pytest
import verify_queue

pytestmark = pytest.mark.unit

RT = {"leases": {}}


def _feat(fid, release="MVP", passes=False, **kw):
    f = {
        "id": fid,
        "category": "data",
        "priority": "P1",
        "passes": passes,
        "needs_clarification": False,
        "description": f"{fid} description",
        "steps": ["Step 1", "Step 2", "Step 3: Verify acceptance criteria: x", "Step 4"],
    }
    if release is not None:
        f["release"] = release
    f.update(kw)
    return f


# --- compute ----------------------------------------------------------------
def test_deferred_feature_is_never_ready():
    ready, blocked, *_ = agent_pool.compute([_feat("R", release="R2")], {}, RT)
    assert "R" not in ready and "R" not in blocked


def test_mvp_feature_does_not_wait_on_a_deferred_prerequisite():
    features = [_feat("M"), _feat("R", release="R2")]
    ready, blocked, *_ = agent_pool.compute(features, {"M": ["R"]}, RT)
    assert "M" in ready and "M" not in blocked


def test_mvp_feature_still_waits_on_an_mvp_prerequisite():
    features = [_feat("M"), _feat("P")]
    ready, blocked, *_ = agent_pool.compute(features, {"M": ["P"]}, RT)
    assert blocked == {"M": ["P"]}


@pytest.mark.parametrize("tag", [None, "", "  ", "mvp", "R3"])
def test_untagged_feature_fails_closed(tag):
    # Not offered, and it still blocks its dependents: unknown scope is not "R2".
    features = [_feat("U", release=tag), _feat("M")]
    ready, blocked, *_ = agent_pool.compute(features, {"M": ["U"]}, RT)
    assert "U" not in ready
    assert blocked == {"M": ["U"]}
    assert agent_pool.unknown_release(features) == ["U"]


def test_deferred_feature_edges_are_kept_on_disk():
    # live_deps filters a copy; the caller's graph is untouched, so moving a
    # feature back into the MVP restores its edges.
    deps = {"M": ["R"]}
    agent_pool.compute([_feat("M"), _feat("R", release="R2")], deps, RT)
    assert deps == {"M": ["R"]}


# --- impact -----------------------------------------------------------------
def test_impact_ignores_deferred_dependents():
    by_id = {f["id"]: f for f in (_feat("K"), _feat("M"), _feat("R", release="R2"))}
    impact = agent_pool.impact_scores({"M": ["K"], "R": ["K"]}, by_id)
    assert impact["K"] == 1  # only M; R2 work is not unlocked work


# --- assess_frontier --------------------------------------------------------
def test_done_when_every_active_release_feature_passes():
    features = [_feat("A", passes=True), _feat("R", release="R2")]
    a = agent_pool.assess_frontier(features, {}, RT)
    assert a["state"] == "done"
    assert (a["passed"], a["total"]) == (1, 1)
    assert a["deferred"] == ["R"]


def test_not_done_while_any_feature_is_untagged():
    features = [_feat("A", passes=True), _feat("U", release=None)]
    a = agent_pool.assess_frontier(features, {}, RT)
    assert a["state"] == "deadlock"
    assert a["total"] == 2 and a["unknown_release"] == ["U"]
    assert any("U" in line for line in agent_pool.deadlock_advice(a))


def test_deferred_external_blocker_is_not_operator_work():
    features = [
        _feat("R", release="R2", external_blocker="a PTP clock"),
        _feat("M", external_blocker="a real restart"),
    ]
    assert agent_pool.externally_blocked(features) == {"M": "a real restart"}


# --- claim --id -------------------------------------------------------------
def _claim(monkeypatch, features, fid):
    monkeypatch.setattr(agent_pool, "Lock", contextlib.nullcontext)
    monkeypatch.setattr(agent_pool, "_sync_primary_checkout", lambda *a, **k: None)
    monkeypatch.setattr(agent_pool, "load_features", lambda *a, **k: features)
    monkeypatch.setattr(agent_pool, "load_deps", lambda: {})
    monkeypatch.setattr(agent_pool, "load_runtime", lambda: {"leases": {}})
    finished = []
    monkeypatch.setattr(agent_pool, "_finish_claim", lambda *a, **k: finished.append(a) or 0)
    args = argparse.Namespace(id=fid, branch=None, reclaim=False, include_awaiting=False)
    return agent_pool.cmd_claim(args), finished


@pytest.mark.parametrize("tag", ["R2", None])
def test_claim_by_id_refuses_outside_active_release(monkeypatch, capsys, tag):
    rc, finished = _claim(monkeypatch, [_feat("X", release=tag)], "X")
    assert rc == 1 and not finished
    assert "outside the active MVP scope" in capsys.readouterr().err


def test_claim_by_id_takes_an_mvp_feature(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_pool, "ROOT", tmp_path / "primary")
    rc, finished = _claim(monkeypatch, [_feat("X")], "X")
    assert rc == 0 and len(finished) == 1


# --- block --on -------------------------------------------------------------
def _block(monkeypatch, features, fid, on):
    saved = []
    monkeypatch.setattr(agent_pool, "Lock", contextlib.nullcontext)
    monkeypatch.setattr(agent_pool, "load_features", lambda *a, **k: features)
    monkeypatch.setattr(agent_pool, "load_deps", lambda: {})
    monkeypatch.setattr(agent_pool, "save_deps", saved.append)
    args = argparse.Namespace(id=fid, on=on, reason=None)
    return agent_pool.cmd_block(args), saved


def test_block_refuses_an_mvp_edge_onto_deferred_work(monkeypatch, capsys):
    features = [_feat("M"), _feat("P"), _feat("R", release="R2")]
    rc, saved = _block(monkeypatch, features, "M", ["P", "R"])
    assert rc == 1 and not saved  # all-or-nothing: P is not recorded either
    assert "deferred to a later release" in capsys.readouterr().err


def test_block_records_an_mvp_edge_onto_mvp_work(monkeypatch):
    rc, saved = _block(monkeypatch, [_feat("M"), _feat("P")], "M", ["P"])
    assert rc == 0 and saved == [{"M": ["P"]}]


# --- verify_queue -----------------------------------------------------------
def test_verify_queue_omits_deferred_and_flags_untagged(monkeypatch):
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: set())
    features = [_feat("M"), _feat("R", release="R2"), _feat("U", release=None)]
    q = verify_queue.build_queue(features=features, deps={}, runtime=RT)
    assert {r["id"] for r in q["rows"]} == {"M", "U"}
    drift = [d for d in verify_queue.find_drift(q) if d["kind"] == "untagged-release"]
    assert [d["id"] for d in drift] == ["U"]
