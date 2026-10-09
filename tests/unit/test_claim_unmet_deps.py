"""L1 — `claim --id` refuses a feature with unmet dependencies unless told why.

SRS-LOG-001 was hand-claimed while its AC depended on five unbuilt producers, and
spent 38 review rounds on a feature that could not close. Reporting unmet deps on
stderr did not stop that; refusing does, and an explicit reason keeps the operator
able to override deliberately.
"""

import argparse
import contextlib

import agent_pool
import pytest

pytestmark = pytest.mark.unit


def _feat(fid, passes=False):
    return {
        "id": fid,
        "category": "data",
        "priority": "P1",
        "release": "MVP",
        "passes": passes,
        "needs_clarification": False,
        "description": fid,
        "steps": [],
    }


def _claim(monkeypatch, tmp_path, deps, allow=None):
    features = [_feat("X"), _feat("P"), _feat("DONE", passes=True)]
    monkeypatch.setattr(agent_pool, "ROOT", tmp_path / "primary")
    monkeypatch.setattr(agent_pool, "Lock", contextlib.nullcontext)
    monkeypatch.setattr(agent_pool, "_sync_primary_checkout", lambda *a, **k: None)
    monkeypatch.setattr(agent_pool, "load_features", lambda *a, **k: features)
    monkeypatch.setattr(agent_pool, "load_deps", lambda: deps)
    monkeypatch.setattr(agent_pool, "load_runtime", lambda: {"leases": {}})
    finished = []
    monkeypatch.setattr(agent_pool, "_finish_claim", lambda *a, **k: finished.append(a) or 0)
    args = argparse.Namespace(
        id="X", branch=None, reclaim=False, include_awaiting=False, allow_unmet=allow
    )
    return agent_pool.cmd_claim(args), finished


def test_unmet_deps_refuse_the_claim(monkeypatch, tmp_path, capsys):
    rc, finished = _claim(monkeypatch, tmp_path, {"X": ["P"]})
    assert rc == 1 and not finished
    err = capsys.readouterr().err
    assert "unmet deps (P)" in err and "--allow-unmet" in err


@pytest.mark.parametrize("allow", ["", "   "])
def test_a_blank_reason_is_no_reason(monkeypatch, tmp_path, allow):
    rc, finished = _claim(monkeypatch, tmp_path, {"X": ["P"]}, allow=allow)
    assert rc == 1 and not finished


def test_a_stated_reason_allows_it_and_is_echoed(monkeypatch, tmp_path, capsys):
    rc, finished = _claim(monkeypatch, tmp_path, {"X": ["P"]}, allow="building the shared fixture")
    assert rc == 0 and len(finished) == 1
    assert "building the shared fixture" in capsys.readouterr().err


def test_met_deps_need_no_reason(monkeypatch, tmp_path):
    rc, finished = _claim(monkeypatch, tmp_path, {"X": ["DONE"]})
    assert rc == 0 and len(finished) == 1
