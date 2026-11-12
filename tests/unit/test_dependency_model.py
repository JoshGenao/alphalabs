"""L1 - Implementation and requirement verification are separate lifecycle events.

The defect: the scheduler had one signal, ``passes`` (requirement verified), and
blocked a feature until every prerequisite had it. One unverified upstream froze
every downstream implementation: 37 of 50 open MVP features had merged code while
41 showed as "blocked" and 4 were claimable.

Scenarios A-G are the acceptance tests for the split. Each drives the real
scheduler (agent_pool.classify / assess_frontier), the real implementation gate
(evidence.implementation_verify), or the real closer (close_feature), against
scratch data. No test reads the live board.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import agent_pool
import close_feature
import contracts
import dependency_check
import dependency_model as dm
import evidence
import migrate_dependency_model as migrate
import pytest

pytestmark = pytest.mark.unit

AC = "Verify acceptance criteria: Stale data returns MARKET_DATA_STALE; fresh data resumes."
# Component tests are real commands the integrator would re-run. The sandbox writes
# these files and puts this interpreter's directory first on PATH, so `python3`
# resolves to an interpreter that has pytest.
PASS_TEST = "python3 -m pytest t_pass.py -q -p no:cacheprovider"
FAIL_TEST = "python3 -m pytest t_fail.py -q -p no:cacheprovider"


def feat(fid, *, passes=False, method="solo", **extra):
    return {
        "id": fid,
        "category": "market_data",
        "description": f"feature {fid}",
        "priority": "P1",
        "release": "MVP",
        "steps": ["Step 1: init", "Step 2: exercise", f"Step 3: {AC}", "Step 4: record"],
        "passes": passes,
        "needs_clarification": False,
        "verification_method": method,
        **extra,
    }


def edge(kind, **extra):
    return {"type": kind, "reason": "test edge", "source": "tests/unit", **extra}


def contract_for(fid, *, prerequisites=(), integration_needs=(), tests=(PASS_TEST,)):
    obligations = [
        {
            "id": "C1",
            "level": "component",
            "criterion": "Given a stale line, the probe returns MARKET_DATA_STALE.",
            "tests": list(tests),
        }
    ]
    if integration_needs:
        obligations.append(
            {
                "id": "I1",
                "level": "integration",
                "criterion": "With the real peer running, a stale line blocks orders.",
                "needs": list(integration_needs),
                "tests": [],
            }
        )
    obligations.append({"id": "S1", "level": "system", "criterion": AC})
    return {
        "feature": fid,
        "requirements": ["SYS-87"],
        "scope": "a freshness probe",
        "implementation_paths": ["t_pass.py"],
        "interfaces": {"consumes": [], "provides": []},
        "prerequisites": list(prerequisites),
        "obligations": obligations,
    }


TRACE = {"A": {"syrs": ["SYS-87"], "strs": []}, "B": {"syrs": ["SYS-87"], "strs": []}}


@pytest.fixture
def no_notes(monkeypatch):
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: set())


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """evidence.py and close_feature.py pointed at a scratch tree."""

    def write_features(features):
        (tmp_path / "feature_list.json").write_text(
            json.dumps(features, indent=2), encoding="utf-8"
        )

    def write_deps(deps):
        (tmp_path / "tools").mkdir(exist_ok=True)
        (tmp_path / "tools" / "feature_deps.json").write_text(json.dumps(deps), encoding="utf-8")

    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    monkeypatch.setattr(evidence, "FEATURE_FILE", tmp_path / "feature_list.json")
    monkeypatch.setattr(evidence, "HARNESS_DIR", tmp_path / ".harness")
    monkeypatch.setattr(evidence, "RUNS_DIR", tmp_path / ".harness" / "runs")
    monkeypatch.setattr(evidence, "CLOSES_LOG", tmp_path / ".harness" / "closes.jsonl")
    monkeypatch.setattr(evidence, "code_changed_since", lambda head: [])
    monkeypatch.setattr(close_feature, "ROOT", tmp_path)
    monkeypatch.setattr(close_feature, "FEATURE_FILE", tmp_path / "feature_list.json")
    (tmp_path / "t_pass.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "t_fail.py").write_text("def test_bad():\n    assert False\n")
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    return SimpleNamespace(root=tmp_path, write_features=write_features, write_deps=write_deps)


def run(fid, oid, test, contract):
    return evidence.run_obligation(fid, oid, shlex.split(test), contract)


def approve(head="abc123", reviewer="claude-fallback", verdict="approve", **extra):
    return [{"kind": "round", "reviewer": reviewer, "verdict": verdict, "head": head, **extra}]


def gate(fid, features, contract, deps, types, ledger):
    return evidence.implementation_verify(
        fid,
        features=features,
        contract=contract,
        deps=deps,
        types=types,
        trace=TRACE,
        ledger=ledger,
    )


# =============================================================================
# Scenario A - independent implementation
# =============================================================================
def test_scenario_a_integration_dependency_does_not_block_implementation(sandbox, no_notes):
    """A depends on B only for integration. B is not even started. A can be claimed,
    implemented, component-verified, independently reviewed, and recorded
    implementation-complete - while its integration verification stays pending and
    its requirement close still waits on B."""
    features = [feat("A"), feat("B")]
    deps = {"A": ["B"]}
    types = {"edges": {"A -> B": edge("integration")}}
    sandbox.write_features(features)
    sandbox.write_deps(deps)

    a = agent_pool.assess_frontier(features, deps, {"leases": {}}, types=types, root=sandbox.root)
    assert "A" in a["ready"] and a["phases"]["A"] == "implement"

    contract = contract_for("A", integration_needs=["B"])
    run("A", "C1", PASS_TEST, contract)
    ok, problems, _ = gate("A", features, contract, deps, types, approve())
    assert ok, problems

    rec = {
        "status": "complete",
        "commit": "abc123",
        "ts": "2026-11-10",
        "steps_digest": dm.steps_digest(feat("A")["steps"]),
    }
    assert close_feature.mark_implemented("A", rec, dry_run=False)
    features = json.loads((sandbox.root / "feature_list.json").read_text())
    a_feat = next(f for f in features if f["id"] == "A")
    assert dm.implementation_complete(a_feat) and a_feat["passes"] is False

    record = evidence.load_record("A")
    integ = dm.level_status(
        record,
        contract,
        "integration",
        blockers=dm.integration_blockers("A", deps, types, {f["id"]: f for f in features}),
    )
    assert integ["status"] == "blocked"  # B has nothing to integrate against yet
    assert close_feature.unverified_prerequisites("A") == ["B"]  # requirement close still waits


# =============================================================================
# Scenario B - hard interface dependency on an undefined contract
# =============================================================================
def test_scenario_b_undefined_interface_blocks_until_the_contract_exists(tmp_path, no_notes):
    features = [feat("A"), feat("B")]
    deps = {"A": ["B"]}
    types = {
        "edges": {
            "A -> B": edge("interface", contract={"path": "src/port.rs", "symbol": "trait Port"})
        }
    }

    st, *_ = agent_pool.classify(features, deps, {"leases": {}}, types=types, root=tmp_path)
    assert st["A"]["phase"] == "implement-blocked"
    assert "does not exist yet" in st["A"]["impl_blockers"][0]["reason"]

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "port.rs").write_text("// placeholder\n")
    st, *_ = agent_pool.classify(features, deps, {"leases": {}}, types=types, root=tmp_path)
    assert st["A"]["phase"] == "implement-blocked"  # file exists, contract not defined in it
    assert "does not define" in st["A"]["impl_blockers"][0]["reason"]

    (tmp_path / "src" / "port.rs").write_text("pub trait Port { fn read(&self); }\n")
    st, *_ = agent_pool.classify(features, deps, {"leases": {}}, types=types, root=tmp_path)
    assert st["A"]["phase"] == "implement"  # B itself is still unimplemented: irrelevant


def test_interface_pattern_pins_the_exact_shape(tmp_path):
    """A name mentioned in a docstring is not a field in the schema (UI-4's real case)."""
    (tmp_path / "audit.py").write_text('"""activation_id"""\nFIELDS = (\n    "order_id",\n)\n')
    meta = {"contract": {"path": "audit.py", "pattern": r'FIELDS\s*=\s*\([^)]*"activation_id"'}}
    assert dm.interface_satisfied(meta, tmp_path)[0] is False
    (tmp_path / "audit.py").write_text('FIELDS = (\n    "order_id",\n    "activation_id",\n)\n')
    assert dm.interface_satisfied(meta, tmp_path)[0] is True


# =============================================================================
# Scenario C - component passes, system verification pending
# =============================================================================
def test_scenario_c_component_passes_but_requirement_stays_open(sandbox, no_notes):
    features = [feat("A"), feat("B")]
    deps = {"A": ["B"]}
    types = {"edges": {"A -> B": edge("verification")}}
    sandbox.write_features(features)
    sandbox.write_deps(deps)
    contract = contract_for("A")

    run("A", "C1", PASS_TEST, contract)
    ok, problems, summary = gate("A", features, contract, deps, types, approve())
    assert ok, problems
    assert summary["component"] == {"C1": "passed"}

    record = evidence.load_record("A")
    assert dm.level_status(record, contract, "component")["status"] == "passed"
    by_id = {f["id"]: f for f in features}
    system = dm.system_status(
        by_id["A"], record, contract, close_blockers=dm.close_gate("A", deps, by_id)
    )
    assert system["status"] == "blocked" and "B" in system["detail"]
    # and the requirement close itself refuses: no step evidence, prerequisite open
    assert evidence.verify("A", features)[0] is False
    assert close_feature.unverified_prerequisites("A") == ["B"]


# =============================================================================
# Scenario D - a failing component test
# =============================================================================
def test_scenario_d_failing_component_test_cannot_claim_success(sandbox, no_notes):
    features = [feat("A")]
    sandbox.write_features(features)
    test = "python3 -m pytest t_c1.py -q -p no:cacheprovider"
    contract = contract_for("A", tests=[test])
    (sandbox.root / "t_c1.py").write_text("def test_c1():\n    assert False\n")

    run("A", "C1", test, contract)
    ok, problems, summary = gate("A", features, contract, {}, {"edges": {}}, approve())
    assert not ok and summary["component"]["C1"] == "failed"
    assert any("FAILED" in p for p in problems)
    record = evidence.load_record("A")
    assert dm.level_status(record, contract, "component")["status"] == "failed"
    assert dm.system_status(features[0], record, contract, close_blockers=[])["status"] == "failed"

    # Fixing the code and re-running decides the obligation; the failure stays on record.
    (sandbox.root / "t_c1.py").write_text("def test_c1():\n    assert True\n")
    run("A", "C1", test, contract)
    runs = evidence.load_record("A")["obligations"]["C1"]["runs"]
    assert [r["exit_code"] for r in runs] == [1, 0]
    assert gate("A", features, contract, {}, {"edges": {}}, approve())[0] is True


def test_a_passing_run_of_an_undeclared_command_does_not_satisfy_the_obligation(sandbox):
    """Reviewer finding 2: an obligation id is a label; the run must be a DECLARED test."""
    features = [feat("A")]
    sandbox.write_features(features)
    contract = contract_for("A", tests=[FAIL_TEST])
    run("A", "C1", PASS_TEST, contract)  # passes, but it is not C1's test
    ok, problems, summary = gate("A", features, contract, {}, {"edges": {}}, approve())
    assert not ok and summary["component"]["C1"] == "not_tested"
    assert summary["ignored_runs"]["C1"] == [shlex.join(shlex.split(PASS_TEST))]


def test_integrator_reexecutes_the_declared_test_not_the_recorded_argv(sandbox):
    features = [feat("A")]
    sandbox.write_features(features)
    test = "python3 -m pytest t_c1.py -q -p no:cacheprovider"
    contract = contract_for("A", tests=[test])
    (sandbox.root / "t_c1.py").write_text("def test_c1():\n    assert True\n")
    run("A", "C1", test, contract)
    (sandbox.root / "t_c1.py").write_text("def test_c1():\n    assert False\n")  # regressed
    ok, problems, _ = evidence.implementation_verify(
        "A",
        features=features,
        contract=contract,
        deps={},
        types={"edges": {}},
        trace=TRACE,
        ledger=approve(),
        reexecute_in=sandbox.root,
    )
    assert not ok and any("integrator re-ran" in p for p in problems)


def test_failing_close_refuses_to_mark_implemented(sandbox, monkeypatch):
    sandbox.write_features([feat("A")])
    monkeypatch.setattr(sys, "argv", ["close_feature.py", "A", "--implemented"])
    monkeypatch.setattr(
        evidence, "implementation_verify", lambda fid, **k: (False, ["C1: latest run FAILED"], {})
    )
    assert close_feature.main() == 3
    assert "implementation" not in json.loads((sandbox.root / "feature_list.json").read_text())[0]


# =============================================================================
# Scenario E - circular hard implementation dependency
# =============================================================================
def test_scenario_e_implementation_cycle_is_detected_and_never_offered(tmp_path, no_notes):
    features = [feat("A"), feat("B"), feat("C")]
    deps = {"A": ["B"], "B": ["C"], "C": ["A"]}
    types = {"edges": {k: edge("implementation") for k in ("A -> B", "B -> C", "C -> A")}}

    a = agent_pool.assess_frontier(features, deps, {"leases": {}}, types=types, root=tmp_path)
    assert a["cycles"]["implementation"] == [["A", "B", "C"]]
    assert a["ready"] == [] and a["state"] == "deadlock"
    advice = "\n".join(agent_pool.deadlock_advice(a))
    assert "IMPLEMENTATION CYCLE" in advice and "A -> B -> C" in advice


def test_cycle_through_a_verification_edge_is_deferred_not_a_deadlock(tmp_path, no_notes):
    features = [feat("A"), feat("B"), feat("C")]
    deps = {"A": ["B"], "B": ["C"], "C": ["A"]}
    types = {
        "edges": {
            "A -> B": edge("implementation"),
            "B -> C": edge("implementation"),
            "C -> A": edge("verification"),
        }
    }
    a = agent_pool.assess_frontier(features, deps, {"leases": {}}, types=types, root=tmp_path)
    assert a["cycles"]["implementation"] == []
    assert a["cycles"]["deferred"] == [["A", "B", "C"]]
    assert a["ready"] == ["C"]  # C's only edge is a verification edge


def test_dependency_check_fails_on_an_implementation_cycle(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "feature_list.json").write_text(json.dumps([feat("A"), feat("B")]))
    (tmp_path / "tools" / "feature_deps.json").write_text(json.dumps({"A": ["B"], "B": ["A"]}))
    failures, _ = dependency_check.run(tmp_path)
    assert any("implementation cycle" in f for f in failures)


# =============================================================================
# Scenario F - the independent reviewer finds a failure
# =============================================================================
def test_scenario_f_reviewer_block_overrides_the_implementers_green(sandbox):
    features = [feat("A")]
    sandbox.write_features(features)
    contract = contract_for("A")
    run("A", "C1", PASS_TEST, contract)  # implementer: all green

    ledger = approve(
        verdict="block", n_findings=1, summary="C1's test never exercises the stale path"
    )
    ok, problems, summary = gate("A", features, contract, {}, {"edges": {}}, ledger)
    assert not ok
    assert summary["review"]["verdict"] == "block"
    assert any("returned 'block'" in p and "never exercises" in p for p in problems)


@pytest.mark.parametrize(
    "ledger, needle",
    [
        ([], "no independent review round"),
        (approve(reviewer="self"), "no independent reviewer"),
        (approve(head=""), "records no reviewed commit"),
        (
            [{"kind": "attempt", "reviewer": "codex", "verdict": "approve"}],
            "no independent review round",
        ),
    ],
)
def test_review_must_be_a_real_independent_round_at_a_placeable_commit(sandbox, ledger, needle):
    features = [feat("A")]
    sandbox.write_features(features)
    contract = contract_for("A")
    run("A", "C1", PASS_TEST, contract)
    ok, problems, _ = gate("A", features, contract, {}, {"edges": {}}, ledger)
    assert not ok and any(needle in p for p in problems), problems


def test_an_approval_of_older_code_does_not_count(sandbox, monkeypatch):
    features = [feat("A")]
    sandbox.write_features(features)
    contract = contract_for("A")
    run("A", "C1", PASS_TEST, contract)
    monkeypatch.setattr(
        evidence, "code_changed_since", lambda head: ["src/x.rs"] if head == "old" else []
    )
    ok, problems, _ = gate("A", features, contract, {}, {"edges": {}}, approve(head="old"))
    assert not ok and any("moved since the approving review" in p for p in problems)


def test_review_rounds_record_the_commit_they_judged(monkeypatch):
    import adversarial_review

    monkeypatch.setattr(adversarial_review, "current_head", lambda: "deadbeef")
    rec = adversarial_review.round_record(
        {"reviewer": "codex", "verdict": "approve", "findings": []}
    )
    assert rec["head"] == "deadbeef"


# =============================================================================
# Scenario G - several features carry one system requirement
# =============================================================================
def test_scenario_g_requirement_unverified_until_every_contributor_is():
    trace = {"F1": {"syrs": ["SYS-1"]}, "F2": {"syrs": ["SYS-1", "SYS-2"]}}
    f1, f2 = feat("F1", passes=True), feat("F2")
    status = contracts.requirement_status(trace, [f1, f2], active_release="MVP")
    assert status["SYS-1"]["status"] == "partially_verified"
    assert status["SYS-1"]["open"] == ["F2"]
    assert status["SYS-2"]["status"] == "not_verified"

    f2["passes"] = True
    assert (
        contracts.requirement_status(trace, [f1, f2], active_release="MVP")["SYS-1"]["status"]
        == "verified"
    )

    f1["evidence"] = dm.PRE_GATE  # closed before the gate: never fresh proof
    got = contracts.requirement_status(trace, [f1, f2], active_release="MVP")["SYS-1"]
    assert got["status"] == "verified (legacy)" and got["legacy_pre_gate"] == ["F1"]


def test_srs_trace_resolves_section_8_rows_through_their_srs_row():
    srs = (
        "| SRS-MD-004 | stmt | ac | Test | P1 | SyRS: SYS-39a, SYS-87; StRS: SN-2.04 |\n"
        "| ERR-3 | Market data stale | Reject ... | SRS-MD-004 |\n"
    )
    trace = contracts.parse_srs(srs)
    assert trace["ERR-3"]["syrs"] == ["SYS-39a", "SYS-87"]
    assert trace["ERR-3"]["via"] == ["SRS-MD-004"]


# =============================================================================
# Supporting rules
# =============================================================================
def test_unclassified_edge_gates_implementation_exactly_as_before(tmp_path, no_notes):
    """Fail closed: with nothing classified, the board behaves as it always did."""
    features = [feat("A"), feat("B")]
    st, *_ = agent_pool.classify(
        features, {"A": ["B"]}, {"leases": {}}, types={"edges": {}}, root=tmp_path
    )
    assert st["A"]["phase"] == "implement-blocked"
    assert st["A"]["impl_blockers"][0]["type"] == dm.UNCLASSIFIED


def test_requirement_close_still_needs_every_prerequisite_of_every_type(sandbox):
    features = [feat("A"), feat("B"), feat("C"), feat("D", passes=True)]
    sandbox.write_features(features)
    sandbox.write_deps({"A": ["B", "C", "D"]})
    assert close_feature.unverified_prerequisites("A") == ["B", "C"]


def test_parked_feature_returns_only_when_something_it_depends_on_changes(tmp_path, no_notes):
    features = [feat("A")]
    types = {"edges": {}}
    by_id = {"A": features[0]}
    fp = agent_pool.current_fingerprint("A", by_id, {}, types, tmp_path)
    runtime = {
        "leases": {},
        "parked": {"A": {"kind": "decision", "reason": "crate choice", "fingerprint": fp}},
    }

    st, *_ = agent_pool.classify(features, {}, runtime, types=types, root=tmp_path)
    assert st["A"]["phase"] == "parked"
    assert "A" not in agent_pool.compute(features, {}, runtime, types=types, root=tmp_path)[0]

    (tmp_path / "docs" / "contracts").mkdir(parents=True)
    (tmp_path / "docs" / "contracts" / "A.json").write_text("{}")
    st, *_ = agent_pool.classify(features, {}, runtime, types=types, root=tmp_path)
    assert st["A"]["phase"] == "implement"


def test_implemented_solo_feature_is_offered_for_verification_and_others_wait_for_an_operator(
    tmp_path, no_notes
):
    impl = {"status": "complete", "steps_digest": dm.steps_digest(feat("S")["steps"])}
    features = [feat("S", implementation=impl), feat("L", method="live-ib", implementation=impl)]
    st, *_ = agent_pool.classify(features, {}, {"leases": {}}, types={"edges": {}}, root=tmp_path)
    assert st["S"]["phase"] == "verify" and st["L"]["phase"] == "awaiting"


def test_validate_types_rejects_unsupported_classifications():
    deps = {"A": ["B"]}
    types = {
        "edges": {
            "A -> B": {"type": "interface", "reason": "r", "source": "s"},
            "A -> C": edge("implementation"),
            "A -> D": {"type": "soon", "reason": "", "source": "s"},
        }
    }
    problems = "\n".join(dm.validate_types(types, deps))
    assert "needs contract.path" in problems
    assert "absent from tools/feature_deps.json" in problems
    assert "is not one of" in problems and "missing 'reason'" in problems


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda c: c["obligations"][-1].update(criterion="Stale data is handled."), "verbatim"),
        (
            lambda c: c["obligations"][0].update(criterion="The probe works correctly."),
            "not objective",
        ),
        (lambda c: c["obligations"][0].update(tests=[]), "at least one test"),
        (lambda c: c["obligations"][0].update(tests=["bash run.sh"]), "not re-executable"),
        (lambda c: c.update(prerequisites=["B"]), "implementation-gating"),
        (lambda c: c.update(draft=True), "draft"),
        (lambda c: c.update(implementation_paths=[]), "implementation_paths"),
        (lambda c: c.update(implementation_paths=["../etc/passwd"]), "repo-relative"),
    ],
)
def test_contract_validation_refuses_what_cannot_judge_an_implementation(mutate, needle):
    contract = contract_for("A")
    mutate(contract)
    problems = contracts.validate(
        contract,
        feat("A"),
        deps={},
        types={"edges": {}},
        trace=TRACE,
        reexecutable=evidence.reexecutable,
    )
    assert any(needle in p for p in problems), problems


def test_a_valid_contract_has_no_problems():
    assert (
        contracts.validate(
            contract_for("A"),
            feat("A"),
            deps={},
            types={"edges": {}},
            trace=TRACE,
            reexecutable=evidence.reexecutable,
        )
        == []
    )


def test_migration_never_removes_an_edge_and_is_idempotent(tmp_path):
    deps: dict = {}
    for a, b, *_ in migrate.CLASSIFICATIONS[:3]:
        deps.setdefault(a, []).append(b)
    ids = sorted({x for a, bs in deps.items() for x in (a, *bs)})
    (tmp_path / "tools").mkdir()
    (tmp_path / "feature_list.json").write_text(json.dumps([feat(x) for x in ids]))
    (tmp_path / "tools" / "feature_deps.json").write_text(json.dumps(deps))
    before = (tmp_path / "tools" / "feature_deps.json").read_text()

    # Audited edges this graph no longer has are skipped, never re-added or refused.
    assert migrate.main(["--apply", "--root", str(tmp_path)]) == 0
    written = json.loads((tmp_path / dm.TYPES_REL).read_text())["edges"]
    assert set(written) == {dm.edge_key(a, b) for a, bs in deps.items() for b in bs}
    assert (tmp_path / "tools" / "feature_deps.json").read_text() == before

    # Idempotent, and an operator's later correction survives a re-run.
    types = json.loads((tmp_path / dm.TYPES_REL).read_text())
    key = next(iter(types["edges"]))
    types["edges"][key]["type"] = "implementation"
    types["edges"][key]["reason"] = "operator correction"
    (tmp_path / dm.TYPES_REL).write_text(json.dumps(types))
    assert migrate.main(["--apply", "--root", str(tmp_path)]) == 0
    assert (
        json.loads((tmp_path / dm.TYPES_REL).read_text())["edges"][key]["reason"]
        == "operator correction"
    )


# =============================================================================
# Fixes from the independent review (2026-11-11)
# =============================================================================
def test_close_uses_the_integrators_review_not_the_branch_ledger(sandbox, monkeypatch, tmp_path):
    """Finding 1: review.jsonl is committed by the branch, so a forged approval there
    must not satisfy the integrator. The integrator passes its OWN round."""
    features = [feat("A")]
    sandbox.write_features(features)
    seen = {}

    def fake_verify(fid, *, ledger=None, **k):
        seen["ledger"] = ledger
        ok = bool(ledger) and ledger[0]["verdict"] == "approve"
        return (
            ok,
            ([] if ok else ["independent reviewer returned 'block'"]),
            {"review": ledger[0] if ledger else None},
        )

    monkeypatch.setattr(evidence, "implementation_verify", fake_verify)
    review_file = tmp_path / "integrator-round.json"
    review_file.write_text(
        json.dumps({"kind": "round", "verdict": "block", "reviewer": "codex", "head": "h"})
    )
    monkeypatch.setattr(
        sys, "argv", ["close_feature.py", "A", "--implemented", "--review-file", str(review_file)]
    )
    assert close_feature.main() == 3
    assert seen["ledger"][0]["verdict"] == "block"
    assert "implementation" not in json.loads((sandbox.root / "feature_list.json").read_text())[0]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """Never let a test write the operator's real ledger in the primary checkout."""
    monkeypatch.setattr(evidence, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(agent_pool, "base_ref", lambda: "origin/main")
    return tmp_path / "ledger"


def test_integrator_review_runs_the_reviewer_itself_and_ignores_no_verdict(monkeypatch, ledger):
    heads = iter(["before-review", "after-review"])
    monkeypatch.setattr(agent_pool.adversarial_review, "current_head", lambda: next(heads))
    calls = []

    def reviewer(base):
        calls.append((base, os.environ.get("ATP_FEATURE_ID")))
        return {"verdict": "approve", "reviewer": "codex", "findings": []}

    rnd = agent_pool.integrator_review("SRS-X-1", review_fn=reviewer)
    assert calls == [("origin/main", "SRS-X-1")]  # full diff, feature context on
    assert rnd["verdict"] == "approve" and rnd["kind"] == "round"
    # Stamped with the commit captured BEFORE the review, not whatever HEAD is after.
    assert rnd["head"] == "before-review"
    monkeypatch.setattr(agent_pool.adversarial_review, "current_head", lambda: "h")
    timeout = {
        "verdict": "block",
        "reviewer": "claude-fallback",
        "no_verdict": True,
        "findings": [],
    }
    assert agent_pool.integrator_review("SRS-X-1", review_fn=lambda b: timeout) is None
    assert (ledger / agent_pool.INTEGRATOR_REVIEWS).is_file()


def test_integrator_review_budget_counts_its_own_rounds_and_operator_can_reset(monkeypatch, ledger):
    monkeypatch.setattr(agent_pool.adversarial_review, "current_head", lambda: "h")
    monkeypatch.setattr(agent_pool.adversarial_review, "round_budget", lambda: 2)
    approve_fn = lambda b: {"verdict": "block", "reviewer": "codex", "findings": []}  # noqa: E731
    assert agent_pool.integrator_review("F", review_fn=approve_fn) is not None
    assert agent_pool.integrator_review("F", review_fn=approve_fn) is not None
    spent = agent_pool.integrator_review("F", review_fn=lambda b: pytest.fail("budget spent"))
    assert spent is None
    monkeypatch.setattr(agent_pool, "LOCK_FILE", ledger / ".lock")
    monkeypatch.setattr(agent_pool, "load_runtime", lambda: {"leases": {}})
    monkeypatch.setattr(agent_pool, "save_runtime", lambda rt: None)
    agent_pool.cmd_unpark(SimpleNamespace(id="F", reset_review_budget=True))
    assert agent_pool.integrator_review("F", review_fn=approve_fn) is not None


def test_reviewer_is_shown_the_implementation_files_the_contract_names(tmp_path, monkeypatch):
    import adversarial_review

    (tmp_path / "docs" / "contracts").mkdir(parents=True)
    (tmp_path / "src.rs").write_text("fn probe() { /* THE IMPLEMENTATION */ }\n")
    contract = contract_for("A")
    contract["implementation_paths"] = ["src.rs", "gone.rs"]
    (tmp_path / "docs" / "contracts" / "A.json").write_text(json.dumps(contract))
    monkeypatch.setattr(adversarial_review, "REPO_ROOT", tmp_path)
    ctx = adversarial_review.feature_context("A")
    assert "THE IMPLEMENTATION" in ctx and "gone.rs: MISSING" in ctx


def test_verify_queue_counts_every_unverified_prerequisite(monkeypatch, no_notes):
    """Finding 3: a feature whose implementation is free but whose close waits on a
    prerequisite is class B, not class A ("only evidence missing")."""
    import verify_queue

    features = [feat("A"), feat("B")]
    monkeypatch.setattr(
        agent_pool, "load_types", lambda: {"edges": {"A -> B": edge("verification")}}
    )
    q = verify_queue.build_queue(features, {"A": ["B"]}, {"leases": {}})
    row = next(r for r in q["rows"] if r["id"] == "A")
    assert row["class"] == "B" and row["unmet"] == ["B"]


def test_merged_but_unrecorded_features_stay_visible_to_the_operator(tmp_path, monkeypatch):
    """Finding 5: claimable, but listed, so one that cannot be component-tested
    does not silently disappear."""
    monkeypatch.setattr(agent_pool, "serialized_notes", lambda *a, **k: {"L"})
    features = [feat("L", method="live-ib")]
    a = agent_pool.assess_frontier(features, {}, {"leases": {}}, types={"edges": {}}, root=tmp_path)
    assert a["merged_unrecorded"] == ["L"] and a["phases"]["L"] == "implement"


@pytest.fixture
def pool_files(tmp_path, monkeypatch):
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(agent_pool, "DEPS_FILE", tmp_path / "tools" / "feature_deps.json")
    monkeypatch.setattr(agent_pool, "TYPES_FILE", tmp_path / dm.TYPES_REL)
    monkeypatch.setattr(agent_pool, "LOCK_FILE", tmp_path / "tools" / ".lock")
    monkeypatch.setattr(agent_pool, "load_features", lambda fetch=False: [feat("A"), feat("B")])
    return tmp_path


def _block_args(**kw):
    base = dict(
        id="A",
        on=["B"],
        reason="r",
        type=None,
        source=None,
        contract=None,
        symbol=None,
        retype=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_block_never_silently_retypes_an_existing_classification(pool_files, monkeypatch):
    """Finding 6: re-typing an audited implementation edge would free the feature, and a
    refusal must write NOTHING (not even the new edge riding along with it)."""
    monkeypatch.setattr(
        agent_pool, "load_features", lambda fetch=False: [feat("A"), feat("B"), feat("C")]
    )
    agent_pool.save_deps({"A": ["B"]})
    agent_pool.save_types({"edges": {"A -> B": edge("implementation")}})
    assert agent_pool.cmd_block(_block_args(on=["B", "C"], type="verification")) == 14
    assert agent_pool.load_deps() == {"A": ["B"]}  # C was not written either
    assert agent_pool.load_types()["edges"]["A -> B"]["type"] == "implementation"


def test_an_agent_retype_that_relaxes_gating_keeps_gating_until_an_operator_confirms(
    pool_files, no_notes, tmp_path
):
    agent_pool.save_deps({"A": ["B"]})
    agent_pool.save_types({"edges": {"A -> B": edge("implementation")}})
    assert agent_pool.cmd_block(_block_args(type="verification", retype=True)) == 0
    meta = agent_pool.load_types()["edges"]["A -> B"]
    assert meta["type"] == "verification" and meta["confidence"] == "low"
    st, *_ = agent_pool.classify(
        [feat("A"), feat("B")],
        {"A": ["B"]},
        {"leases": {}},
        types=agent_pool.load_types(),
        root=tmp_path,
    )
    assert st["A"]["phase"] == "implement-blocked"  # still gated
    # Tightening applies at once.
    assert agent_pool.cmd_block(_block_args(type="implementation", retype=True)) == 0
    assert agent_pool.load_types()["edges"]["A -> B"]["confidence"] == "medium"
    # implementation -> interface is a relaxation too: any named file would clear it.
    (tmp_path / "README.md").write_text("unrelated\n")
    args = _block_args(type="interface", contract="README.md", retype=True)
    assert agent_pool.cmd_block(args) == 0
    assert agent_pool.load_types()["edges"]["A -> B"]["confidence"] == "low"


def test_interface_block_requires_a_contract(pool_files):
    assert agent_pool.cmd_block(_block_args(type="interface")) == 1


def test_primary_sync_merge_is_three_way():
    """Finding 7: a stale local copy must not undo a re-type or removal from main."""
    head = {"edges": {"A -> B": {"type": "integration"}, "C -> D": {"type": "verification"}}}
    base = {"edges": {"A -> B": {"type": "implementation"}, "C -> D": {"type": "verification"}}}
    local = {"edges": {**head["edges"], "E -> F": {"type": "interface"}}}  # local ADDED E->F only
    merged = agent_pool.merge_types(head, local, base)
    assert merged["edges"]["A -> B"]["type"] == "implementation"  # main's re-type kept
    assert merged["edges"]["E -> F"]["type"] == "interface"  # local addition kept
    local_removed = {"edges": {"A -> B": {"type": "integration"}}}  # local removed C->D
    assert "C -> D" not in agent_pool.merge_types(head, local_removed, base)["edges"]


def test_low_confidence_classification_fails_closed(tmp_path, no_notes):
    """Finding 8: a guess nobody confirmed gates like no classification at all."""
    features = [feat("A"), feat("B")]
    types = {"edges": {"A -> B": edge("integration", confidence="low")}}
    st, *_ = agent_pool.classify(features, {"A": ["B"]}, {"leases": {}}, types=types, root=tmp_path)
    assert st["A"]["phase"] == "implement-blocked"
    assert "LOW confidence" in st["A"]["impl_blockers"][0]["reason"]


def test_respecifying_a_feature_voids_its_implementation_record():
    f = feat("A")
    f["implementation"] = {"status": "complete", "steps_digest": dm.steps_digest(f["steps"])}
    assert dm.implementation_complete(f)
    f["steps"][2] = "Step 3: Verify acceptance criteria: something stricter"
    assert not dm.implementation_complete(f)
    f["implementation"] = {"status": "complete"}  # no digest: unknown is not complete
    assert not dm.implementation_complete(f)


def test_prerequisite_closing_in_the_same_batch_counts(sandbox):
    sandbox.write_features([feat("A"), feat("B")])
    sandbox.write_deps({"A": ["B"]})
    assert close_feature.unverified_prerequisites("A", ("A", "B")) == []
    assert close_feature.unverified_prerequisites("A", ("A",)) == ["B"]


def test_untagged_feature_keeps_its_requirement_unverified():
    trace = {"F1": {"syrs": ["SYS-1"]}, "F2": {"syrs": ["SYS-1"]}}
    f1, f2 = feat("F1", passes=True), feat("F2")
    f2.pop("release")
    assert contracts.requirement_status(trace, [f1, f2], active_release="MVP")["SYS-1"][
        "status"
    ] == ("partially_verified")
    assert contracts.KNOWN_RELEASES == agent_pool.KNOWN_RELEASES


def test_blocker_report_counts_only_fully_freed_and_names_verification_roots():
    blocked = {
        "X": [{"type": "implementation", "dep": "P", "reason": "r"}],
        "Y": [
            {"type": "implementation", "dep": "P", "reason": "r"},
            {"type": "interface", "dep": "Q", "reason": "contract q missing"},
        ],
    }
    report = dm.blocker_report(blocked, {}, {}, close_blocked={"Z": ["P"], "W": ["P", "R"]})
    first = report["cover"][0]
    assert (
        first["prerequisite"] == "P"
        and first["frees"] == ["X", "Y"]
        and first["fully_freed"] == ["X"]
    )
    assert report["verify_first"][0] == {"prerequisite": "P", "holds_back": ["W", "Z"]}


def test_gate_refuses_an_implementation_path_that_does_not_exist(sandbox):
    features = [feat("A")]
    sandbox.write_features(features)
    contract = contract_for("A")
    contract["implementation_paths"] = ["t_pass.py", "src/never_written.rs"]
    run("A", "C1", PASS_TEST, contract)
    ok, problems, _ = gate("A", features, contract, {}, {"edges": {}}, approve())
    assert not ok and any("never_written.rs does not exist" in p for p in problems)


def test_reissuing_a_low_confidence_retype_does_not_launder_it(pool_files, no_notes, tmp_path):
    """Round-4 finding 1: the same `block` twice must not raise low to medium."""
    agent_pool.save_deps({"A": ["B"]})
    agent_pool.save_types({"edges": {"A -> B": edge("implementation", confidence="high")}})
    (tmp_path / "README.md").write_text("unrelated\n")
    args = _block_args(type="interface", contract="README.md", retype=True)
    assert agent_pool.cmd_block(args) == 0
    assert agent_pool.cmd_block(args) == 0  # again, same type
    assert agent_pool.load_types()["edges"]["A -> B"]["confidence"] == "low"
    st, *_ = agent_pool.classify(
        [feat("A"), feat("B")],
        {"A": ["B"]},
        {"leases": {}},
        types=agent_pool.load_types(),
        root=tmp_path,
    )
    assert st["A"]["phase"] == "implement-blocked"


def test_truncated_implementation_files_are_marked(tmp_path, monkeypatch):
    import adversarial_review

    (tmp_path / "docs" / "contracts").mkdir(parents=True)
    (tmp_path / "big.rs").write_text("x" * (adversarial_review.PER_FILE_CHARS + 50))
    contract = contract_for("A")
    contract["implementation_paths"] = ["big.rs"]
    (tmp_path / "docs" / "contracts" / "A.json").write_text(json.dumps(contract))
    monkeypatch.setattr(adversarial_review, "REPO_ROOT", tmp_path)
    assert "[TRUNCATED: showed the first" in adversarial_review.feature_context("A")


def test_codex_context_fits_one_command_line_argument():
    import adversarial_review

    capped = adversarial_review.cap_context("é" * 200_000, limit=1000)
    assert len(capped.encode("utf-8")) <= 1000 and "CONTEXT TRUNCATED" in capped
    assert adversarial_review.cap_context("short") == "short"


def test_unreadable_integrator_ledger_reads_as_budget_spent(ledger, monkeypatch):
    ledger.mkdir(parents=True)
    (ledger / agent_pool.INTEGRATOR_REVIEWS).write_text('{"feature": "F", "round": {}}\nnot json\n')
    monkeypatch.setattr(agent_pool.adversarial_review, "current_head", lambda: "h")
    assert agent_pool.integrator_rounds_since_reset("F") >= agent_pool.UNREADABLE_LEDGER
    assert (
        agent_pool.integrator_review("F", review_fn=lambda b: pytest.fail("must not review"))
        is None
    )
