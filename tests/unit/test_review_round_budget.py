"""L1 — The review round budget hands a non-converging loop to the operator.

Contract:
  - only BLOCK rounds spend the budget; attempts, warns, and approves do not
  - at the budget, the next review does not call a reviewer: it escalates (exit 3)
  - escalation never approves, and an operator authorization grants one more budget
  - an unreadable ledger escalates (fail closed); a missing one is a fresh feature
  - authorizations and escalations are not counted as review rounds anywhere
"""

import json

import adversarial_review as ar
import pytest

pytestmark = pytest.mark.unit


def _round(verdict="block", rules=("r",)):
    return {"kind": "round", "verdict": verdict, "blocking_rules": list(rules), "ts": "t"}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    # Never reach a live reviewer from a unit test, even when the budget code under
    # test is broken: a mutant that skipped the check once shelled `claude -p`.
    monkeypatch.setattr(
        ar, "review", lambda *a, **k: {"verdict": "block", "reviewer": "stub", "findings": []}
    )
    monkeypatch.setenv("ATP_FEATURE_ID", "F-B")
    monkeypatch.delenv("ATP_REVIEW_ROUND_BUDGET", raising=False)
    monkeypatch.setattr(ar, "_diffstat", lambda base: "3 files changed")
    path = tmp_path / ".harness" / "runs" / "F-B" / "review.jsonl"
    path.parent.mkdir(parents=True)

    def write(recs):
        path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    return write


# --- budget arithmetic --------------------------------------------------------
def test_only_block_rounds_spend_the_budget():
    recs = [_round("block")] * 3 + [_round("warn"), _round("approve")]
    recs += [{"kind": "attempt", "verdict": "none"}]
    assert ar.budget_state(recs, 8)["block_rounds"] == 3


def test_the_budget_is_exhausted_at_exactly_n_blocks():
    assert not ar.budget_state([_round()] * 7, 8)["exhausted"]
    assert ar.budget_state([_round()] * 8, 8)["exhausted"]


def test_an_authorization_grants_one_more_budget():
    recs = [_round()] * 8 + [{"kind": "authorization"}]
    state = ar.budget_state(recs, 8)
    assert state["allowed"] == 16 and not state["exhausted"]
    assert ar.budget_state(recs + [_round()] * 8, 8)["exhausted"]


@pytest.mark.parametrize("raw,expected", [("", 8), ("5", 5), ("0", 8), ("-2", 8), ("x", 8)])
def test_a_malformed_budget_falls_back_to_the_default_never_to_unlimited(
    monkeypatch, raw, expected
):
    monkeypatch.setenv("ATP_REVIEW_ROUND_BUDGET", raw)
    assert ar.round_budget() == expected


def test_operator_records_are_not_review_rounds():
    for kind in ("authorization", "escalation", "attempt"):
        assert ar.is_round({"kind": kind}) is False
    assert ar.is_round({"kind": "round"}) and ar.is_round({})  # pre-`kind` records count


# --- check_budget -------------------------------------------------------------
def test_under_budget_proceeds(ledger):
    ledger([_round()] * 7)
    assert ar.check_budget("F-B", "origin/main") is None


def test_a_never_reviewed_feature_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "REPO_ROOT", tmp_path)
    assert ar.check_budget("F-NEW", "origin/main") is None


def test_no_feature_id_means_no_budget():
    assert ar.check_budget("", "origin/main") is None


def test_an_exhausted_budget_escalates_with_the_recurring_classes(ledger):
    ledger([_round(rules=("drift-a",))] * 5 + [_round(rules=("race-b",))] * 3)
    esc = ar.check_budget("F-B", "origin/main")
    assert esc["verdict"] == "escalate"
    assert "5x  drift-a" in esc["report"] and "3x  race-b" in esc["report"]
    assert "3 files changed" in esc["report"]
    assert "--authorize-continue" in esc["report"]


def test_an_unreadable_ledger_escalates(ledger, tmp_path):
    (tmp_path / ".harness/runs/F-B/review.jsonl").write_text('{"kind": "round"\n')
    esc = ar.check_budget("F-B", "origin/main")
    assert esc["verdict"] == "escalate" and "unreadable" in esc["summary"]


# --- the command line -----------------------------------------------------------
def test_an_exhausted_budget_never_calls_a_reviewer(ledger, monkeypatch, capsys):
    ledger([_round()] * 8)
    called = []
    monkeypatch.setattr(ar, "review", lambda *a, **k: called.append(1) or {})
    monkeypatch.setattr("sys.argv", ["adversarial_review.py", "origin/main"])
    assert ar.main() == ar.EXIT_ESCALATE
    assert not called
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "escalate"  # never approve


def test_the_escalation_is_recorded_and_is_not_a_round(ledger, monkeypatch, tmp_path):
    ledger([_round()] * 8)
    monkeypatch.setattr("sys.argv", ["adversarial_review.py", "origin/main"])
    ar.main()
    recs = ar.read_records(tmp_path / ".harness/runs/F-B/review.jsonl")
    assert recs[-1]["kind"] == "escalation"
    assert sum(1 for r in recs if ar.is_round(r)) == 8


def test_authorize_continue_reopens_the_budget(ledger, monkeypatch):
    ledger([_round()] * 8)
    monkeypatch.setattr("sys.argv", ["x", "--authorize-continue", "split is worse here"])
    assert ar.main() == 0
    assert ar.check_budget("F-B", "origin/main") is None
    recs = ar.ledger_records("F-B")
    assert recs[-1]["kind"] == "authorization" and recs[-1]["reason"] == "split is worse here"


@pytest.mark.parametrize("reason", ["", "   "])
def test_authorize_continue_requires_a_reason(ledger, monkeypatch, reason):
    monkeypatch.setattr("sys.argv", ["x", "--authorize-continue", reason])
    assert ar.main() == 2


def test_each_round_records_the_size_of_what_was_reviewed(ledger, monkeypatch):
    monkeypatch.setattr("sys.argv", ["adversarial_review.py", "origin/main"])
    ar.main()
    rec = ar.ledger_records("F-B")[-1]
    assert rec["kind"] == "round" and rec["diffstat"] == "3 files changed"


def test_the_budget_spending_round_warns_ahead(ledger, monkeypatch, capsys):
    ledger([_round()] * 7)
    ar.emit({"verdict": "block", "reviewer": "codex", "findings": [{"severity": "high"}]})
    assert "next review will escalate" in capsys.readouterr().err
