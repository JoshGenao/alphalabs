"""L1 — Unit tests for the agent scheduler (tools/agent_pool.py).

Covers the pure scheduling/guard logic behind the self-claim model and the four
hardening fixes from the adversarial review:
  - guarded lease reclaim (owner PID-liveness, not just TTL)
  - the honesty guard (needs_serialized) used by `integrate --mode complete`
  - block dependency-id validation
  - ready-frontier / blocked computation, subsystem-avoidance, cycle detection
"""

import json
import os
import socket
import time

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


# --- dependency frontier ----------------------------------------------------
def test_compute_blocks_on_unmet_dependency():
    features = [_feat("A", passes=True), _feat("B")]
    deps = {"B": ["A"]}
    ready, blocked, active, held, _ = agent_pool.compute(features, deps, {"leases": {}})
    assert "B" in ready and not blocked  # A passes → B ready

    features = [_feat("A", passes=False), _feat("B")]
    ready, blocked, *_ = agent_pool.compute(features, deps, {"leases": {}})
    assert "B" in blocked and blocked["B"] == ["A"] and "B" not in ready


def test_compute_excludes_active_leases():
    features = [_feat("A"), _feat("B")]
    now = time.time()
    runtime = {"leases": {"A": {"owner": "x:1", "expiry": now + 9999, "port_index": 0}}}
    ready, _blocked, active, _held, _ = agent_pool.compute(features, {}, runtime)
    assert "A" in active and "A" not in ready and "B" in ready


def test_unknown_dependency_is_ignored_not_silently_blocking():
    # compute only counts deps that exist; block() is what must reject typos.
    features = [_feat("B")]
    ready, blocked, *_ = agent_pool.compute(features, {"B": ["NOPE"]}, {"leases": {}})
    assert "B" in ready and not blocked


# --- subsystem-avoidance ordering ------------------------------------------
def test_pick_order_prefers_unheld_subsystem():
    features = [_feat("D1", category="data"), _feat("M1", category="market_data")]
    by_id = {f["id"]: f for f in features}
    held = {"atp-data"}  # a sibling already holds the data crate
    order = agent_pool.pick_order(["D1", "M1"], by_id, held)
    assert order[0] == "M1"  # market_data preferred over the held data crate


# --- cycle detection --------------------------------------------------------
def test_reachable_detects_cycle():
    deps = {"B": ["A"]}  # adding A->B would close A->B->A
    assert agent_pool.reachable(deps, "B", "A") is True
    assert agent_pool.reachable(deps, "A", "B") is False


def test_dep_path_returns_the_hops_not_just_yes():
    """`block` has to PRINT the cycle, not merely refuse on it.

    Reporting "would create dependency cycle: ['SRS-MD-001']" left the operator to
    reconstruct the other hops by hand from a 4,000-line deps file; four features
    sat unrecordable behind that missing sentence.
    """
    deps = {"MD1": ["PERF1"], "PERF1": ["MD3"]}
    assert agent_pool.dep_path(deps, "MD1", "MD3") == ["MD1", "PERF1", "MD3"]
    assert agent_pool.dep_path(deps, "MD3", "MD1") is None


def test_dep_path_returns_the_shortest_route():
    """Breadth-first: the operator gets the cycle they have the best chance of
    recognising, not whichever one a DFS stack happened to surface."""
    deps = {"A": ["B", "LONG1"], "LONG1": ["LONG2"], "LONG2": ["Z"], "B": ["Z"]}
    assert agent_pool.dep_path(deps, "A", "Z") == ["A", "B", "Z"]


# --- honesty guard ----------------------------------------------------------
def test_needs_serialized_flags_ib_and_dashboard_but_not_pure_compute():
    ib = _feat(
        "X",
        description="capture live option-chain snapshots from IB",
        steps=["Run an integration test against IB Gateway"],
    )
    need, hits = agent_pool.needs_serialized(ib)
    assert need and hits

    pure = _feat(
        "Y",
        description="consolidate minute bars into 5-minute bars",
        steps=["Resample fixture OHLCV in-process and assert aggregation"],
    )
    need2, hits2 = agent_pool.needs_serialized(pure)
    assert need2 is False and hits2 == []


# --- honesty guard: the DECLARED method outranks the prose -------------------
# The keyword scan above matched templated boilerplate — "dashboard" appeared in 47
# of 120 features and " ib " in 32 — so the guard fired on 90 of 120 and
# --force-complete became the normal path. verification_method replaces it; the
# keyword scan survives only as a fallback for an unclassified feature.
def test_declared_method_beats_the_keyword_scan():
    # Prose screams IB and dashboard; the declared method says it is solo-verifiable.
    feat = _feat(
        "X",
        description="expose live designation state on the dashboard for IB accounts",
        steps=["Exercise the REST contract with fixture data and provider mocks"],
    )
    assert agent_pool.needs_serialized(feat)[0] is True  # fallback would serialize it

    feat["verification_method"] = "solo"
    need, hits = agent_pool.needs_serialized(feat)
    assert need is False and hits == []


# ids= is load-bearing, not cosmetic: pytest puts each parametrize id into
# item.keywords, and tests/conftest.py auto-skips any item whose keywords contain
# "integration" or "e2e". With the raw method names as ids, two of these three cases
# were silently SKIPPED — the guard for the two most consequential methods was never
# executed, in a suite whose own rule 6 says a test that cannot fail is not evidence.
@pytest.mark.parametrize(
    "method",
    ["integration", "live-ib", "e2e"],
    ids=["m_integration", "m_live_ib", "m_e2e"],
)
def test_declared_non_solo_methods_serialize(method):
    feat = _feat("X", description="pure in-process resampling", steps=["assert aggregation"])
    assert agent_pool.needs_serialized(feat)[0] is False  # fallback would allow complete
    feat["verification_method"] = method
    need, hits = agent_pool.needs_serialized(feat)
    assert need is True and hits == [method]


def test_unknown_method_fails_closed():
    """Unrecognised is not 'solo'. Permissive-on-unknown is how a false green ships."""
    feat = _feat("X", description="pure compute", steps=["assert"])
    feat["verification_method"] = "probably-fine"
    need, hits = agent_pool.needs_serialized(feat)
    assert need is True
    assert "unknown verification_method" in hits[0]


def test_blank_method_falls_back_rather_than_passing():
    feat = _feat("X", description="uses IB Gateway", steps=["integration test"])
    feat["verification_method"] = "   "
    assert agent_pool.needs_serialized(feat)[0] is True


def test_unclassified_lists_features_still_on_the_fallback():
    feats = [
        _feat("A") | {"verification_method": "solo"},
        _feat("B"),
        _feat("C") | {"verification_method": ""},
    ]
    assert agent_pool.unclassified(feats) == ["B", "C"]


# --- the audit trail is not writable by the audited party --------------------
# The ledgers live in the primary checkout, outside any worktree, so a branch cannot
# reach them at all. This guard is the second line of defence: it also stops a branch
# committing ANOTHER feature's evidence, which the path layout alone does not.
def test_branch_may_commit_its_own_evidence_but_not_the_ledgers():
    assert agent_pool.shared_state_violations([".harness/runs/X/evidence.json"], "X") == []
    for forbidden in (
        ".harness/closes.jsonl",
        ".harness/overrides.jsonl",
        ".harness/runs/OTHER/evidence.json",
    ):
        assert agent_pool.shared_state_violations([forbidden], "X") == [forbidden], forbidden


def test_integrator_owned_files_are_reset_not_trusted():
    """The trust boundary, as a set: what the agent may author vs what it may not.

    shared_state_violations only sees COMMITTED paths. Uncommitted edits to
    feature_list.json passed every check — the path is inside INTEGRATE_ALLOWLIST, so
    _uncommitted_outside_allowlist ignored it and `git add -A` staged it. That made
    the human gate self-grantable: hand-write evidence, run `close_feature.py
    --verified --attested-by operator` in the worktree, then `integrate --mode
    serialized` and the mutated feature_list.json reaches main with passes:true.
    `integrate` now hard-resets these from the base ref before writing them.
    """
    assert set(agent_pool.INTEGRATOR_OWNED) == {"feature_list.json", "progress.txt"}
    # progress.d must NOT be reset — an agent legitimately authors its own note.
    assert "progress.d" not in agent_pool.INTEGRATOR_OWNED
    assert "progress.d" in agent_pool.INTEGRATE_ALLOWLIST


def test_only_the_integrating_features_evidence_may_be_staged():
    """close_feature's retirement has to reach main, but scoped to one feature."""
    staged = [
        "feature_list.json",
        ".harness/runs/X/evidence.json",
        ".harness/runs/X/closed-20260808T000000+0000.json",
        ".harness/runs/OTHER/evidence.json",
        ".harness/closes.jsonl",
    ]
    outside = agent_pool.staged_outside_allowlist(staged, "X")
    assert outside == [".harness/runs/OTHER/evidence.json", ".harness/closes.jsonl"]
    # With no feature id, no evidence path is stageable at all.
    assert ".harness/runs/X/evidence.json" in agent_pool.staged_outside_allowlist(staged, None)


# --- block id validation ----------------------------------------------------
def test_validate_block_splits_known_and_unknown():
    ids = {"A", "B", "C"}
    known, unknown = agent_pool.validate_block(ids, "A", ["B", "ZZZ"])
    assert known == ["B"] and unknown == ["ZZZ"]


# --- guarded lease reclaim (PID liveness) ----------------------------------
def test_lease_active_honors_live_owner_pid_even_when_expired():
    me = f"{socket.gethostname()}:{os.getpid()}"
    expired_but_alive = {"owner": me, "expiry": time.time() - 10_000}
    assert agent_pool.lease_active(expired_but_alive, time.time()) is True

    dead = {"owner": f"{socket.gethostname()}:2147483646", "expiry": time.time() - 10_000}
    assert agent_pool.lease_active(dead, time.time()) is False

    future = {"owner": "whatever:1", "expiry": time.time() + 10_000}
    assert agent_pool.lease_active(future, time.time()) is True


def test_owner_is_live_rejects_malformed_and_remote():
    assert agent_pool.owner_is_live("") is False
    assert agent_pool.owner_is_live("no-colon") is False
    assert agent_pool.owner_is_live("some-other-host:1") is False  # not this host
    assert agent_pool.owner_is_live(f"{socket.gethostname()}:{os.getpid()}") is True


# --- foreign-host lease stickiness (single-host contract) -------------------
def test_lease_active_foreign_host_is_sticky_unless_reclaiming():
    foreign_expired = {"owner": "other-host:1", "expiry": time.time() - 10_000}
    # default: never auto-reclaim a remote owner on TTL alone (can't probe its pid)
    assert agent_pool.lease_active(foreign_expired, time.time()) is True
    # explicit --reclaim releases it
    assert (
        agent_pool.lease_active(foreign_expired, time.time(), allow_foreign_reclaim=True) is False
    )


# --- integrate staging allowlist (no feature work in the marker commit) -----
def test_path_in_allowlist():
    assert agent_pool.path_in_allowlist("feature_list.json") is True
    assert agent_pool.path_in_allowlist("progress.txt") is True
    assert agent_pool.path_in_allowlist("progress.d/session-SRS-DATA-008.md") is True
    assert agent_pool.path_in_allowlist("tools/feature_deps.json") is True
    # feature / tooling / test work must NOT be stage-able by integrate
    assert agent_pool.path_in_allowlist("tools/agent_pool.py") is False
    assert agent_pool.path_in_allowlist("crates/atp-data/src/store.rs") is False
    assert agent_pool.path_in_allowlist("tests/unit/test_agent_pool.py") is False


def test_porcelain_outside_allowlist_checks_both_rename_sides():
    porcelain = (
        " M feature_list.json\n"  # allowlisted — fine
        "?? tools/agent_pool.py\n"  # outside
        "R  crates/x.rs -> progress.d/x.rs\n"  # rename source is outside
    )
    bad = agent_pool.porcelain_outside_allowlist(porcelain)
    assert "tools/agent_pool.py" in bad
    assert "crates/x.rs" in bad  # source side caught
    assert "feature_list.json" not in bad
    assert "progress.d/x.rs" not in bad  # destination is allowlisted


def test_staged_outside_allowlist():
    names = ["feature_list.json", "progress.d/session-X.md", "crates/x.rs", "tools/agent_pool.py"]
    assert agent_pool.staged_outside_allowlist(names) == ["crates/x.rs", "tools/agent_pool.py"]


def test_shared_state_violations_allows_only_own_note():
    fid = "SRS-DATA-008"
    committed = [
        f"progress.d/session-{fid}.md",  # the agent's own resume note — allowed
        "crates/atp-data/src/store.rs",  # feature work — allowed (not shared state)
        "feature_list.json",  # only the integrator may write — violation
        "progress.txt",  # violation
        "tools/feature_deps.json",  # violation
        "progress.d/session-OTHER.md",  # someone else's note — violation
    ]
    bad = agent_pool.shared_state_violations(committed, fid)
    assert bad == [
        "feature_list.json",
        "progress.txt",
        "tools/feature_deps.json",
        "progress.d/session-OTHER.md",
    ]


# --- integrate ownership (no double-assign / cross-session integrate) --------
def test_lease_blocks_owner():
    now = time.time()
    mine = f"{socket.gethostname()}:{os.getpid()}"
    # same-host dead pid + expired → free (does not block us)
    dead = {"owner": f"{socket.gethostname()}:2147483646", "expiry": now - 1}
    assert agent_pool.lease_blocks_owner(dead, mine, now) is False
    # remote owner is sticky-active → blocks us
    foreign = {"owner": "other-host:1", "expiry": now - 1}
    assert agent_pool.lease_blocks_owner(foreign, mine, now) is True
    # our own lease never blocks us; no lease never blocks
    assert agent_pool.lease_blocks_owner({"owner": mine, "expiry": now + 9999}, mine, now) is False
    assert agent_pool.lease_blocks_owner(None, mine, now) is False


def test_should_refuse_release():
    now = time.time()
    mine = f"{socket.gethostname()}:{os.getpid()}"
    foreign = {"owner": "other-host:1", "expiry": now - 1}  # active sibling
    # refuse a live sibling's lease without --force; allow with --force
    assert agent_pool.should_refuse_release(foreign, mine, force=False, now=now) is True
    assert agent_pool.should_refuse_release(foreign, mine, force=True, now=now) is False
    # our own lease is always releasable
    own = {"owner": mine, "expiry": now + 9999}
    assert agent_pool.should_refuse_release(own, mine, force=False, now=now) is False
    # a stale same-host dead-pid lease is releasable
    dead = {"owner": f"{socket.gethostname()}:2147483646", "expiry": now - 1}
    assert agent_pool.should_refuse_release(dead, mine, force=False, now=now) is False
    # our own lease never blocks us
    own = {"owner": mine, "expiry": now + 9999}
    assert agent_pool.lease_blocks_owner(own, mine, now) is False
    # no lease → not blocked
    assert agent_pool.lease_blocks_owner(None, mine, now) is False


# --- review-round accounting (P1-1) ------------------------------------------
# `Adversarial rounds:` appeared in 1 of 38 notes because it was prose an agent had
# to remember. adversarial_review.py now records each round, so the note can be
# checked against it — but absent telemetry must stay silent, since a review can
# legitimately predate this or run outside the worktree.
def _wt(tmp_path, fid, *, note=None, rounds=None):
    (tmp_path / "progress.d").mkdir(parents=True, exist_ok=True)
    if note is not None:
        (tmp_path / "progress.d" / f"session-{fid}.md").write_text(note, encoding="utf-8")
    if rounds is not None:
        d = tmp_path / ".harness" / "runs" / fid
        d.mkdir(parents=True, exist_ok=True)
        (d / "review.jsonl").write_text('{"verdict":"block"}\n' * rounds, encoding="utf-8")
    return tmp_path


def test_no_telemetry_is_not_a_mismatch(tmp_path):
    wt = _wt(tmp_path, "F-1", note="Adversarial rounds: 7\n")
    assert agent_pool.note_rounds_mismatch(wt, "F-1") is None


def test_agreeing_counts_pass(tmp_path):
    wt = _wt(tmp_path, "F-1", note="Outcome: complete\nAdversarial rounds: 3\n", rounds=3)
    assert agent_pool.note_rounds_mismatch(wt, "F-1") is None


def test_disagreeing_counts_are_reported(tmp_path):
    wt = _wt(tmp_path, "F-1", note="Adversarial rounds: 1\n", rounds=4)
    msg = agent_pool.note_rounds_mismatch(wt, "F-1")
    assert msg and "claims `Adversarial rounds: 1`" in msg and "recorded 4" in msg


def test_a_missing_rounds_line_is_reported_when_rounds_were_recorded(tmp_path):
    wt = _wt(tmp_path, "F-1", note="Outcome: complete\n", rounds=2)
    msg = agent_pool.note_rounds_mismatch(wt, "F-1")
    assert msg and "no `Adversarial rounds:` line" in msg


def _append_raw(wt, fid, line):
    d = wt / ".harness" / "runs" / fid
    d.mkdir(parents=True, exist_ok=True)
    with (d / "review.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_a_reviewer_outage_does_not_demand_a_higher_round_count(tmp_path):
    """3 real rounds + a rate-limited Codex attempt is still `Adversarial rounds: 3`.

    Counting lines here would refuse the integrate unless the note claimed 4 —
    forcing the agent to write a number that includes a reviewer outage nobody
    reviewed anything in.
    """
    wt = _wt(tmp_path, "F-1", note="Outcome: complete\nAdversarial rounds: 3\n", rounds=3)
    _append_raw(wt, "F-1", '{"kind":"attempt","verdict":"none","reviewer":"codex"}')
    assert agent_pool.note_rounds_mismatch(wt, "F-1") is None


def test_attempts_alone_are_not_rounds(tmp_path):
    """Every reviewer was down: nothing was reviewed, so nothing is claimed."""
    wt = _wt(tmp_path, "F-1", note="Outcome: complete\n")
    _append_raw(wt, "F-1", '{"kind":"attempt","verdict":"none","reviewer":"codex"}')
    assert agent_pool.note_rounds_mismatch(wt, "F-1") is None


def test_unreadable_telemetry_is_not_a_mismatch(tmp_path):
    """CLAUDE.md rule 3 — a truncated ledger must not accuse the note of lying."""
    wt = _wt(tmp_path, "F-1", note="Adversarial rounds: 3\n")
    _append_raw(wt, "F-1", '{"verdict": <-- truncated')
    assert agent_pool.note_rounds_mismatch(wt, "F-1") is None


# --- dependency-edge retraction (P1-8) ---------------------------------------
# `block --on` appends and nothing has ever removed an edge, so the graph only
# accretes constraints. A wrong edge parks a feature until its named prerequisite
# passes — which may be never.
@pytest.fixture
def deps_sandbox(tmp_path, monkeypatch):
    feats = tmp_path / "feature_list.json"
    feats.write_text(
        '[{"id":"A","passes":false,"steps":[]},{"id":"B","passes":false,"steps":[]},'
        '{"id":"C","passes":false,"steps":[]},{"id":"D","passes":false,"steps":[]}]',
        encoding="utf-8",
    )
    deps = tmp_path / "feature_deps.json"
    deps.write_text('{"A": ["B", "C"]}', encoding="utf-8")
    monkeypatch.setattr(agent_pool, "FEATURE_FILE", feats)
    monkeypatch.setattr(agent_pool, "DEPS_FILE", deps)
    monkeypatch.setattr(agent_pool, "LOCK_FILE", tmp_path / ".lock")
    monkeypatch.setattr(
        agent_pool, "load_features", lambda fetch=False: json.loads(feats.read_text())
    )
    return deps


class _UnblockArgs:
    def __init__(self, id, off, reason=""):
        self.id, self.off, self.reason = id, off, reason


def test_unblock_removes_only_the_named_edge(deps_sandbox):
    assert agent_pool.cmd_unblock(_UnblockArgs("A", ["B"])) == 0
    assert json.loads(deps_sandbox.read_text()) == {"A": ["C"]}


def test_unblock_drops_the_key_when_the_last_edge_goes(deps_sandbox):
    assert agent_pool.cmd_unblock(_UnblockArgs("A", ["B", "C"])) == 0
    assert json.loads(deps_sandbox.read_text()) == {}


def test_unblock_refuses_an_edge_that_does_not_exist(deps_sandbox):
    """Silently succeeding would let a typo read as a retraction that never happened."""
    assert agent_pool.cmd_unblock(_UnblockArgs("A", ["NOPE"])) == 1
    assert json.loads(deps_sandbox.read_text()) == {"A": ["B", "C"]}


def test_unblock_refuses_an_unknown_feature(deps_sandbox):
    assert agent_pool.cmd_unblock(_UnblockArgs("ZZZ", ["B"])) == 1


def test_this_branch_does_not_carry_integrator_owned_scheduler_state():
    """tools/feature_deps.json is integrator-owned; a branch must not commit it.

    shared_state_violations() has always said so, but nothing checked THIS repo's
    own harness branches — and a broad `git add tools/` swept a sibling session's six
    self-learned SRS-LOG-001 edges into a harness commit that had no reason to touch
    the scheduler. Those edges change which features the pool hands out.
    """
    assert agent_pool.shared_state_violations(["tools/feature_deps.json"], "ANY") == [
        "tools/feature_deps.json"
    ]


# --- block: a refused edge is a FAILURE, not a warning on the way to exit 0 ---
# `block` used to drop cycle-forming edges, print "✓ {fid} blocked-on [survivors]",
# and return 0 — and `if cur:` meant a feature with no prior edges whose every
# requested edge was dropped had NOTHING written while still printing ✓. The
# operator's blocker was therefore silently not recorded and the feature returned to
# the ready frontier every cycle: rule 24's churn loop with rule 24's remedy missing
# (pipeline-and-integrate rule 32).
class _BlockArgs:
    def __init__(self, id, on, reason=""):
        self.id, self.on, self.reason = id, on, reason


def test_block_unions_with_edges_already_recorded(deps_sandbox, capsys):
    """A re-run adding one edge to existing ones must report all of them, not just
    the new one — the message is what the operator checks the graph against."""
    assert agent_pool.cmd_block(_BlockArgs("D", ["B"])) == 0
    assert agent_pool.cmd_block(_BlockArgs("D", ["C"])) == 0
    assert json.loads(deps_sandbox.read_text())["D"] == ["B", "C"]
    assert "D blocked-on ['B', 'C']" in capsys.readouterr().out


def test_block_refuses_a_cycle_nonzero_and_writes_nothing(deps_sandbox, capsys):
    """The whole defect in one test: A already depends on B, so B->A closes a loop."""
    before = deps_sandbox.read_text()
    assert agent_pool.cmd_block(_BlockArgs("B", ["A"])) == 13
    assert deps_sandbox.read_text() == before  # untouched, not partially written
    err = capsys.readouterr().err
    assert "NOTHING was written" in err
    assert "B -> A -> B" in err  # the cycle is printed, not just named


def test_block_cycle_refusal_names_the_edge_to_retract(deps_sandbox, capsys):
    """The operator needs the one concrete `unblock` that breaks the loop.

    A depends on B and C; adding C -> A closes C->A->C, so the last hop on the
    existing path (A -> C) is what must be retracted.
    """
    assert agent_pool.cmd_block(_BlockArgs("C", ["A"])) == 13
    assert "agent_pool.py unblock A --off C" in capsys.readouterr().err


def test_block_is_all_or_nothing_when_one_edge_cycles(deps_sandbox, capsys):
    """A partial write behind a non-zero exit is the ambiguous state that produced
    the original confusion. B->C is fine; B->A is not; neither is recorded."""
    before = deps_sandbox.read_text()
    assert agent_pool.cmd_block(_BlockArgs("B", ["C", "A"])) == 13
    assert deps_sandbox.read_text() == before
    err = capsys.readouterr().err
    assert "1 of 2 requested" in err
    assert "agent_pool.py block B --on C" in err  # the salvageable subset, spelled out


def test_block_refuses_a_self_edge(deps_sandbox):
    before = deps_sandbox.read_text()
    assert agent_pool.cmd_block(_BlockArgs("B", ["B"])) == 13
    assert deps_sandbox.read_text() == before


# --- external_blocker: a blocker no FEATURE owns ------------------------------
# `block --on` cannot express "needs 30 real market-hours days" or "needs an SMS
# provider account" — the thing in the way is not a feature. With nowhere to put
# it, those features stayed `ready`, fell into the serialized-note bucket, and
# made assess_frontier report DEADLOCK over work no agent could ever have done.
def test_external_blocker_reads_the_declared_reason():
    assert agent_pool.external_blocker({"external_blocker": "a PTP host"}) == "a PTP host"
    assert agent_pool.external_blocker({}) == ""


def test_external_blocker_treats_whitespace_as_absent():
    """A field stubbed out with spaces must not park a feature forever."""
    assert agent_pool.external_blocker({"external_blocker": "   "}) == ""
    assert agent_pool.external_blocker({"external_blocker": None}) == ""


def test_compute_excludes_an_externally_blocked_feature_from_ready():
    features = [_feat("A"), _feat("B") | {"external_blocker": "an SMS provider account"}]
    ready, blocked, *_ = agent_pool.compute(features, {}, {"leases": {}})
    assert ready == ["A"]
    assert "B" not in blocked  # not blocked-on-deps either — it has no unmet dep


def test_externally_blocked_ignores_features_that_have_since_closed():
    """A stale reason on a green feature keeps no claim on the operator's attention."""
    features = [
        _feat("A") | {"external_blocker": "a PTP host"},
        _feat("B", passes=True) | {"external_blocker": "an SMS provider account"},
    ]
    assert agent_pool.externally_blocked(features) == {"A": "a PTP host"}


def test_assess_frontier_separates_external_roots_from_guarded_ones():
    """Both are "a human must act", but only one is answerable by verifying."""
    features = [
        _feat("EXT") | {"verification_method": "integration", "external_blocker": "an SMS account"},
        _feat("GUARD") | {"verification_method": "live-ib"},
        _feat("DOWN"),
    ]
    deps = {"DOWN": ["EXT", "GUARD"]}
    a = agent_pool.assess_frontier(features, deps, {"leases": {}}, skip_awaiting=False)
    assert a["external_root_blockers"] == ["EXT"]
    assert a["guarded_root_blockers"] == ["GUARD"]  # EXT is not double-counted here
    assert set(a["root_blockers"]) == {"EXT", "GUARD"}
    assert a["external_blocked"] == {"EXT": "an SMS account"}


def test_deadlock_advice_never_prescribes_force_complete():
    """--force-complete overrides the honesty guard and NOTHING ELSE; the evidence
    gate rewrites the mode straight back to `serialized`. Both `status` and `claim`
    told the operator to run it, which is why they looped."""
    advice = "\n".join(
        agent_pool.deadlock_advice(
            {
                "external_root_blockers": ["EXT"],
                "external_blocked": {"EXT": "an SMS provider account"},
                "guarded_root_blockers": ["GUARD"],
            }
        )
    )
    assert "an SMS provider account" in advice  # the reason, not just the id
    assert "--attested-by operator" in advice
    assert "does NOT skip it" in advice


def test_deadlock_advice_is_empty_when_nothing_is_stuck():
    assert agent_pool.deadlock_advice({}) == []
