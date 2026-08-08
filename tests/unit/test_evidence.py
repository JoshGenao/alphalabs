"""L1 — Unit tests for the verification evidence gate (tools/evidence.py).

The defect these cover: `close_feature.py --verified` refuses to flip a feature
without an attestation, and `agent_pool.py integrate --mode complete` used to
supply that attestation on the agent's own behalf. `passes: true` therefore meant
"the agent says so". These tests pin the properties that make it mean "the record
says so" instead — above all that every *absent* or *unreadable* state fails
closed rather than reading as a pass (CLAUDE.md rule 3).
"""

from __future__ import annotations

import json

import evidence
import pytest

pytestmark = pytest.mark.unit


FEATURE = "TEST-EV-001"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point evidence.py at a scratch tree with one 3-step feature."""
    feature_file = tmp_path / "feature_list.json"
    feature_file.write_text(
        json.dumps(
            [
                {
                    "id": FEATURE,
                    "passes": False,
                    "steps": ["Step 1: one", "Step 2: two", "Step 3: three"],
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(evidence, "ROOT", tmp_path)
    monkeypatch.setattr(evidence, "FEATURE_FILE", feature_file)
    monkeypatch.setattr(evidence, "HARNESS_DIR", tmp_path / ".harness")
    monkeypatch.setattr(evidence, "RUNS_DIR", tmp_path / ".harness" / "runs")
    monkeypatch.setattr(evidence, "CLOSES_LOG", tmp_path / ".harness" / "closes.jsonl")
    monkeypatch.setattr(evidence, "OVERRIDES_LOG", tmp_path / ".harness" / "overrides.jsonl")
    return tmp_path


def _record_all_steps(n: int = 3, *, executed: bool = True) -> None:
    for i in range(1, n + 1):
        rec = evidence.load_record(FEATURE)
        entry = {
            "n": i,
            "step_text": f"Step {i}",
            "command": f"cmd{i}",
            "observed": f"out{i}",
            "status": "pass",
            "executed": executed,
            "ts": "2026-08-07T00:00:00+00:00",
        }
        if executed:
            entry["exit_code"] = 0
        rec["steps"].append(entry)
        # Same binding the real write path (_store_step) applies.
        rec["steps_digest"] = evidence.steps_digest(evidence.feature_steps(FEATURE))
        evidence.save_record(FEATURE, rec)


def _approve_both() -> None:
    rec = evidence.load_record(FEATURE)
    rec["critic"] = {"deterministic": {"verdict": "approve"}, "judgment": {"verdict": "approve"}}
    evidence.save_record(FEATURE, rec)


# --- fail-closed: the whole point of the module --------------------------------
def test_missing_record_is_not_a_pass(sandbox):
    ok, problems, summary = evidence.verify(FEATURE)
    assert ok is False
    assert summary["steps_evidenced"] == 0
    # The denominator must be the feature's real step count, not 0. "0/0 steps"
    # reads as "nothing was required" — the same absent-is-not-empty error the
    # rest of this module exists to prevent.
    assert summary["steps_total"] == 3
    assert any("no evidence record" in p for p in problems)


def test_corrupt_record_raises_rather_than_reading_as_empty(sandbox):
    path = evidence.record_path(FEATURE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    # An unreadable record must never present as "nothing recorded yet".
    with pytest.raises(evidence.EvidenceError):
        evidence.verify(FEATURE)


def test_partial_step_coverage_fails_and_names_the_gap(sandbox):
    _record_all_steps(2)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("step 3 has no evidence" in p for p in problems)


def test_hand_recorded_step_without_observed_output_fails(sandbox):
    """For a step nobody executed, the text IS the evidence — blank means none."""
    _record_all_steps(3, executed=False)
    rec = evidence.load_record(FEATURE)
    rec["steps"][1]["observed"] = "   "
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE, allow_attested=True)
    assert ok is False
    assert any("step 2 records no observed output" in p for p in problems)


def test_executed_step_may_produce_no_output(sandbox):
    """`cargo fmt --check` and `git diff --exit-code` pass silently.

    For an executed step the exit code is the observation. Requiring non-empty text
    would push exactly those commands onto the hand-recorded path, where they would
    then demand a human attestation to close.
    """
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["steps"][1]["observed"] = ""
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is True, problems


def test_failing_step_is_not_a_pass(sandbox):
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["steps"][0]["status"] = "fail"
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("status is 'fail'" in p for p in problems)


# --- both critic layers are required -------------------------------------------
@pytest.mark.parametrize(
    "present,missing", [("deterministic", "judgment"), ("judgment", "deterministic")]
)
def test_one_critic_layer_is_not_enough(sandbox, present, missing):
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["critic"] = {present: {"verdict": "approve"}}
    evidence.save_record(FEATURE, rec)
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any(f"no {missing} critic verdict" in p for p in problems)


def test_block_verdict_is_not_an_approval(sandbox):
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["critic"] = {
        "deterministic": {"verdict": "approve"},
        "judgment": {"verdict": "block"},
    }
    evidence.save_record(FEATURE, rec)
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("judgment critic verdict is 'block'" in p for p in problems)


# --- the one path that should succeed ------------------------------------------
def test_complete_record_verifies(sandbox):
    _record_all_steps(3)
    _approve_both()
    ok, problems, summary = evidence.verify(FEATURE)
    assert ok is True, problems
    assert summary == {
        "steps_total": 3,
        "steps_evidenced": 3,
        "critic": {"deterministic": "approve", "judgment": "approve"},
    }


def test_extra_step_beyond_the_feature_is_flagged(sandbox):
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["steps"].append(
        {"n": 9, "step_text": "?", "command": "c", "observed": "o", "status": "pass", "ts": "t"}
    )
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("step 9 is recorded but the feature has only 3 steps" in p for p in problems)


# --- auditability ---------------------------------------------------------------
def test_closes_and_overrides_are_appended_not_overwritten(sandbox):
    evidence.log_close(FEATURE, mode="complete", attestation="evidence-record")
    evidence.log_close("OTHER-1", mode="complete", attestation="verified-e2e-label")
    evidence.log_override(FEATURE, "force-complete", "operator ran the live window by hand")

    closes = [json.loads(x) for x in evidence.CLOSES_LOG.read_text().splitlines()]
    assert [c["feature"] for c in closes] == [FEATURE, "OTHER-1"]
    assert closes[1]["attestation"] == "verified-e2e-label"

    overrides = [json.loads(x) for x in evidence.OVERRIDES_LOG.read_text().splitlines()]
    assert overrides[0]["kind"] == "force-complete"
    assert "live window" in overrides[0]["reason"]


def test_unknown_feature_id_raises(sandbox):
    with pytest.raises(evidence.EvidenceError):
        evidence.feature_steps("NOPE-1")


# --- retirement: the record is consumed by the close it justifies -----------------
# An earlier design REJECTED any record that had reached main. That was unsatisfiable:
# the record arrives on main as part of the very close it justifies, and
# close-feature.yml runs ON main after the merge, so the human-attested path — the
# only documented route to passes:true for live-IB/e2e work — could never pass.
def test_retire_archives_the_live_record(sandbox):
    _record_all_steps(3)
    _approve_both()
    assert evidence.verify(FEATURE)[0] is True

    archived = evidence.retire(FEATURE)
    assert archived is not None
    assert archived.exists() and archived.name.startswith("closed-")
    assert not evidence.record_path(FEATURE).exists()


def test_a_reopened_feature_cannot_inherit_the_retired_record(sandbox):
    _record_all_steps(3)
    _approve_both()
    evidence.retire(FEATURE)

    ok, problems, summary = evidence.verify(FEATURE)
    assert ok is False
    assert summary["steps_total"] == 3  # the real denominator, not 0
    assert any("no evidence record" in p for p in problems)


def test_retire_is_a_no_op_when_there_is_nothing_to_retire(sandbox):
    assert evidence.retire(FEATURE) is None


def test_retire_dry_run_leaves_the_record_in_place(sandbox):
    _record_all_steps(1)
    assert evidence.retire(FEATURE, dry_run=True) is not None
    assert evidence.record_path(FEATURE).exists()


def test_a_record_that_reached_main_still_verifies(sandbox):
    """The attested close runs ON main, after the merge. Rejecting merged records
    broke the only documented path to passes:true for live-IB/e2e features."""
    _record_all_steps(3, executed=False)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE, allow_attested=True)
    assert ok is True, problems


# --- executed vs merely described ------------------------------------------------
# The gate's whole claim is "evidence, not assertion". `record` takes the caller's
# strings and runs nothing, so accepting it unattested would leave the gate
# satisfiable by description — the self-granted --verified defect one layer up.
def test_hand_recorded_steps_do_not_satisfy_the_gate(sandbox):
    _record_all_steps(3, executed=False)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert sum("was hand-recorded, not executed" in p for p in problems) == 3


def test_hand_recorded_steps_pass_only_with_explicit_attestation(sandbox):
    _record_all_steps(3, executed=False)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE, allow_attested=True)
    assert ok is True, problems


def test_executed_claim_without_an_exit_code_is_not_a_pass(sandbox):
    """`executed: true` with no exit_code is the cheapest possible forgery.

    The check was `exit_code not in (0, None)`, so an absent code read as success —
    a hand-written record claiming execution satisfied the gate having run nothing.
    Absent is unknown, and unknown is not zero.
    """
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    del rec["steps"][0]["exit_code"]
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("records no exit_code" in p for p in problems)


def test_attestation_relaxes_which_steps_count_not_whether_a_record_exists(sandbox):
    """A human vouching for the work still has to say what the work was."""
    ok, problems, _ = evidence.verify(FEATURE, allow_attested=True)
    assert ok is False
    assert any("no evidence record" in p for p in problems)

    # …and an attested close cannot pass on steps recorded as failures.
    _record_all_steps(3, executed=False)
    rec = evidence.load_record(FEATURE)
    rec["steps"][0]["status"] = "fail"
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE, allow_attested=True)
    assert ok is False
    assert any("status is 'fail'" in p for p in problems)


def test_executed_step_with_nonzero_exit_is_not_a_pass(sandbox):
    _record_all_steps(3)
    rec = evidence.load_record(FEATURE)
    rec["steps"][2]["exit_code"] = 1
    evidence.save_record(FEATURE, rec)
    _approve_both()
    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("step 3 exited 1, not 0" in p for p in problems)


def test_run_executes_the_command_and_records_the_real_result(sandbox, monkeypatch):
    """`run` must capture what happened, not what it was told happened."""

    class Args:
        id = FEATURE
        step = 1
        command = ["sh", "-c", "echo hello-from-the-actual-process; exit 0"]

    assert evidence.cmd_run(Args()) == 0
    entry = evidence.load_record(FEATURE)["steps"][0]
    assert entry["executed"] is True
    assert entry["exit_code"] == 0
    assert "hello-from-the-actual-process" in entry["observed"]
    assert entry["status"] == "pass"


def test_respecified_feature_invalidates_the_record(sandbox):
    """Matching step INDICES is not matching step CRITERIA.

    Editing what step 3 demands used to leave the old record satisfying the new
    acceptance criteria — the record attested a specification that no longer exists.
    """
    _record_all_steps(3)
    _approve_both()
    assert evidence.verify(FEATURE)[0] is True

    feats = json.loads(evidence.FEATURE_FILE.read_text())
    feats[0]["steps"][2] = "Step 3: now demands something entirely different"
    evidence.FEATURE_FILE.write_text(json.dumps(feats), encoding="utf-8")

    ok, problems, _ = evidence.verify(FEATURE)
    assert ok is False
    assert any("attests a different specification" in p for p in problems)


def test_run_records_a_failure_as_a_failure(sandbox):
    class Args:
        id = FEATURE
        step = 2
        command = ["sh", "-c", "echo nope >&2; exit 3"]

    assert evidence.cmd_run(Args()) == 1
    entry = next(s for s in evidence.load_record(FEATURE)["steps"] if s["n"] == 2)
    assert entry["status"] == "fail"
    assert entry["exit_code"] == 3
    assert "nope" in entry["observed"]
