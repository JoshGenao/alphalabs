"""L1 — Unit tests for the verification queue (tools/verify_queue.py).

The tool exists because "awaiting human verification" was one label over four
unrelated situations, and telling them apart meant reading ~2,000 lines of session
notes every fresh-context session. These tests pin the part that must not drift:
the classification is DERIVED from the board, so a feature cannot be filed wrongly
by someone forgetting to file it.
"""

import agent_pool
import pytest
import verify_queue

pytestmark = pytest.mark.unit


def _feat(fid, **kw):
    base = {
        "id": fid,
        "category": "data",
        "priority": "P1",
        "passes": False,
        "needs_clarification": False,
        "description": f"{fid} description",
        "steps": ["Step 1", "Step 2", "Step 3: Verify acceptance criteria: something", "Step 4"],
    }
    base.update(kw)
    return base


# --- classification ---------------------------------------------------------
def test_class_c_outranks_a_dependency_edge():
    """A feature can be BOTH blocked on a feature and waiting on a purchase. The
    purchase wins: no amount of feature work moves it, so filing it as B would put
    it on a queue that implies someone can code their way out."""
    feat = _feat("X", external_blocker="an SMS provider account")
    assert verify_queue.classify("X", feat, {"X": ["Y"]}, set()) == "C"


def test_class_d_outranks_b_because_a_cycle_never_clears():
    feat = _feat("X")
    assert verify_queue.classify("X", feat, {"X": ["Y"]}, {"X"}) == "D"


def test_class_b_when_a_real_feature_is_unmet():
    assert verify_queue.classify("X", _feat("X"), {"X": ["Y"]}, set()) == "B"


def test_class_a_when_nothing_is_in_the_way():
    assert verify_queue.classify("X", _feat("X"), {}, set()) == "A"


def test_every_feature_lands_in_exactly_one_class():
    """The taxonomy must be total. A feature that matches no class is invisible,
    which is the failure the whole tool exists to remove."""
    cases = [
        (_feat("A1"), {}, set()),
        (_feat("B1"), {"B1": ["Z"]}, set()),
        (_feat("C1", external_blocker="a PTP host"), {}, set()),
        (_feat("D1"), {}, {"D1"}),
    ]
    got = {verify_queue.classify(f["id"], f, b, c) for f, b, c in cases}
    assert got == {"A", "B", "C", "D"}


# --- cycle detection over the whole graph -----------------------------------
def test_graph_cycles_finds_a_multi_hop_loop():
    """`block` refuses to create one, but SEED_DEPS and hand edits do not go
    through `block`, and a cycle is otherwise invisible: it presents as features
    permanently blocked for no stated reason."""
    deps = {"A": ["B"], "B": ["C"], "C": ["A"]}
    cycles = verify_queue.graph_cycles(deps)
    assert len(cycles) == 1
    assert set(cycles[0]) == {"A", "B", "C"}


def test_graph_cycles_reports_each_loop_once():
    """Three edges sit on the same 3-cycle; reporting it three times would make a
    single decision look like three."""
    deps = {"A": ["B"], "B": ["C"], "C": ["A"]}
    assert len(verify_queue.graph_cycles(deps)) == 1


def test_graph_cycles_is_silent_on_an_acyclic_graph():
    assert verify_queue.graph_cycles({"A": ["B"], "B": ["C"]}) == []


# --- method proposal from real artifacts ------------------------------------
def test_live_broker_marker_outranks_integration():
    """The single-live invariant is the binding scheduling constraint, so a file
    carrying both markers is a live-IB file."""
    disc = {"strong": ["t.py"], "weak": [], "markers": {"t.py": ["integration", "live_broker"]}}
    assert verify_queue.method_from_markers(disc)[0] == "live-ib"


def test_e2e_marker_outranks_integration():
    disc = {"strong": ["t.py"], "weak": [], "markers": {"t.py": ["e2e", "integration"]}}
    assert verify_queue.method_from_markers(disc)[0] == "e2e"


def test_only_in_process_markers_propose_solo():
    disc = {"strong": ["t.py"], "weak": [], "markers": {"t.py": ["unit", "domain"]}}
    method, why = verify_queue.method_from_markers(disc)
    assert method == "solo" and "in-process" in why


def test_no_discovered_tests_proposes_nothing_rather_than_solo():
    """CLAUDE.md rule 3: absent is not empty. A feature whose tests this tool
    cannot find is UNDETERMINED — reading a miss as `solo` is how the permissive
    answer gets granted by accident, and `solo` is the one that lets
    `integrate --mode complete` flip passes."""
    method, why = verify_queue.method_from_markers({"strong": [], "weak": [], "markers": {}})
    assert method is None
    assert "no tests discovered" in why


def test_weak_matches_are_excluded_by_default():
    """A file that merely mentions the id somewhere is not the feature's test."""
    disc = {"strong": [], "weak": ["other.py"], "markers": {"other.py": ["e2e"]}}
    assert verify_queue.method_from_markers(disc)[0] is None
    assert verify_queue.method_from_markers(disc, strong_only=False)[0] == "e2e"


# --- drift ------------------------------------------------------------------
def _queue(features, deps=None):
    return verify_queue.build_queue(features, deps or {}, {"leases": {}})


def test_drift_is_silent_on_a_steady_board(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: set())
    monkeypatch.setattr(verify_queue.evidence, "RUNS_DIR", tmp_path / "runs")
    q = _queue([_feat("A", passes=True), _feat("B")])
    assert [f for f in verify_queue.find_drift(q) if f["severity"] != "low"] == []


def test_drift_flags_a_cycle_with_the_retraction_that_breaks_it(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: set())
    monkeypatch.setattr(verify_queue.evidence, "RUNS_DIR", tmp_path / "runs")
    q = _queue([_feat("A"), _feat("B")], {"A": ["B"], "B": ["A"]})
    cycle = [f for f in verify_queue.find_drift(q) if f["kind"] == "cycle"]
    assert cycle and cycle[0]["severity"] == "high"
    assert "unblock" in cycle[0]["action"]


def test_drift_flags_a_feature_held_off_the_frontier_by_a_note_alone(monkeypatch, tmp_path):
    """The churn loop's signature: no dep edge, no external_blocker, and nothing
    anywhere recording why it is not being offered."""
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: {"B"})
    monkeypatch.setattr(verify_queue.evidence, "RUNS_DIR", tmp_path / "runs")
    q = _queue([_feat("B")])
    kinds = {f["kind"] for f in verify_queue.find_drift(q)}
    assert "untriaged" in kinds


def test_an_externally_blocked_feature_is_not_reported_as_untriaged(monkeypatch, tmp_path):
    """It IS triaged — the reason is declared. Re-reporting it every run is how a
    watcher's output stops being read."""
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: {"B"})
    monkeypatch.setattr(verify_queue.evidence, "RUNS_DIR", tmp_path / "runs")
    q = _queue([_feat("B", external_blocker="an SMS provider account")])
    assert [f for f in verify_queue.find_drift(q) if f["kind"] == "untriaged"] == []


def test_a_passing_feature_never_appears_in_the_queue(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: set())
    q = _queue([_feat("A", passes=True), _feat("B")])
    assert [r["id"] for r in q["rows"]] == ["B"]


# --- id spellings: the compact form must not match the whole tree -------------
def test_the_compact_form_drops_bare_digits_but_keeps_real_stems():
    """Both directions of the same boundary, in one test on purpose.

    `API-3` reduced to `"3"`, and a bare digit is a substring of very nearly every
    source file, so every short-id feature matched the whole tree. `run` EXECUTES
    the selection, so a false match files an unrelated passing suite as a feature's
    evidence — the thing evidence.reexecute says nothing mechanical can catch.

    The second half guards the over-correction: `md003` is only five characters,
    and a stricter threshold would silently stop finding
    `test_data013_ingestion_validation.py` by filename. Asserted here rather than
    in its own test because alone it cannot fail — it describes behaviour this
    change preserved, not behaviour it introduced (rule 6).
    """
    assert verify_queue._fid_forms("API-3") == ("API-3", "api_3", "")
    assert verify_queue._fid_forms("UI-1") == ("UI-1", "ui_1", "")
    assert verify_queue._fid_forms("SRS-DATA-013")[2] == "data013"
    assert verify_queue._fid_forms("SRS-MD-003")[2] == "md003"


def test_discovery_of_a_short_id_does_not_sweep_the_tree():
    strong = verify_queue.discover_tests("UI-1")["strong"]
    assert len(strong) < 20, f"UI-1 matched {len(strong)} files — the digit is matching everything"
