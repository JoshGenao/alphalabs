"""L4 — tools/codex_review.sh drives the real script against a stub companion.

Two defects live here that no unit test could reach, because both are about the
script's shell semantics rather than any Python:

1. The script used to `exec node`, so a session running it directly recorded none of
   its judgment rounds. SRS-MD-003 ran 7 that way; the ledger held nothing, while the
   note claimed an APPROVE the ledger had no trace of.
2. The first fix replaced `exec` with a bare `out="$(node ...)"` — which under
   `set -e` aborts the script the moment Codex exits nonzero, skipping the recording
   AND the output replay. That is the case that matters most: a rate-limited Codex is
   the one whose stdout the dispatcher must read to fail over.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import adversarial_review  # noqa: E402  (sibling tool under test)

pytestmark = pytest.mark.boundary

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "codex_review.sh"


def _stub(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "codex-companion.mjs").write_text(body, encoding="utf-8")
    return tmp_path


def _run(stub: Path, fid: str, runs_dir: Path):
    env = {
        "PATH": subprocess.os.environ.get("PATH", ""),
        "HOME": subprocess.os.environ.get("HOME", ""),
        "CLAUDE_PLUGIN_ROOT": str(stub),
        "ATP_FEATURE_ID": fid,
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)], cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    jsonl = runs_dir / fid / "review.jsonl"
    rounds = jsonl.read_text().splitlines() if jsonl.is_file() else []
    return proc, rounds


@pytest.fixture
def runs_dir():
    """The REAL .harness/runs — codex_review.sh resolves it from its own location.

    These tests drive the actual script, so they cannot be pointed at tmp_path. The
    cleanup therefore has to be real: the previous version built an empty `made` list,
    never appended to it, and looped over nothing — cleanup that looked present in
    review and removed nothing, leaving TEST-CR-* dirs in the developer's live ledger
    whenever a test failed before its own `finally`.

    Snapshot what exists, and remove anything this test created, pass or fail.
    """
    d = ROOT / ".harness" / "runs"
    before = {p.name for p in d.iterdir()} if d.is_dir() else set()
    yield d
    if d.is_dir():
        for p in d.iterdir():
            # Only ever our own fixtures: a real SRS-* ledger must survive untouched.
            if p.name not in before and p.name.startswith("TEST-CR-"):
                shutil.rmtree(p, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_failing_codex_still_replays_output_and_records_the_round(tmp_path, runs_dir):
    """The regression: `set -e` + bare command substitution swallowed both."""
    fid = "TEST-CR-FAIL"
    shutil.rmtree(runs_dir / fid, ignore_errors=True)
    stub = _stub(
        tmp_path,
        'console.log(JSON.stringify({verdict:"error",reason:"usage limit"}));\nprocess.exit(1);\n',
    )
    proc, rounds = _run(stub, fid, runs_dir)
    try:
        assert proc.returncode == 1, "the companion's exit status must propagate"
        # stdout must survive: the dispatcher parses it to detect the limit and fail over
        assert "usage limit" in proc.stdout
        assert len(rounds) == 1, "a failed round is exactly the one worth recording"
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_direct_run_records_and_a_dispatched_run_does_not(tmp_path, runs_dir):
    """The dispatcher records its own rounds; recording twice would inflate every count."""
    fid = "TEST-CR-GUARD"
    shutil.rmtree(runs_dir / fid, ignore_errors=True)
    stub = _stub(
        tmp_path,
        'console.log(JSON.stringify({verdict:"approve",findings:[]}));\n',
    )
    try:
        _run(stub, fid, runs_dir)
        first = (runs_dir / fid / "review.jsonl").read_text().splitlines()
        assert len(first) == 1

        env = {
            "PATH": subprocess.os.environ.get("PATH", ""),
            "HOME": subprocess.os.environ.get("HOME", ""),
            "CLAUDE_PLUGIN_ROOT": str(stub),
            "ATP_FEATURE_ID": fid,
            "ATP_REVIEW_DISPATCHED": "1",
        }
        subprocess.run(["bash", str(SCRIPT)], cwd=str(ROOT), env=env, capture_output=True)
        after = (runs_dir / fid / "review.jsonl").read_text().splitlines()
        assert len(after) == 1, "dispatched run must not double-record"
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_codex_finding_is_recorded_with_its_title_not_a_question_mark(tmp_path, runs_dir):
    fid = "TEST-CR-SCHEMA"
    shutil.rmtree(runs_dir / fid, ignore_errors=True)
    stub = _stub(
        tmp_path,
        "console.log(JSON.stringify({verdict:'needs-attention',"
        "findings:[{title:'Some Real Class',severity:'high'}]}));\n",
    )
    try:
        _run(stub, fid, runs_dir)
        rec = json.loads((runs_dir / fid / "review.jsonl").read_text().splitlines()[0])
        assert rec["rules"] == ["some real class"]
        assert rec["blocking_rules"] == ["some real class"]
        assert rec["verdict"] == "block"  # needs-attention + high normalizes to block
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_real_usage_limit_envelope_is_still_recorded(tmp_path, runs_dir):
    """The shape three consecutive fixes missed.

    A rate-limited Codex does not emit `{"verdict": ...}`. It emits an envelope with
    `result: null`, an empty `rawOutput`, and the cause in parseError/codex.stderr —
    so extract_json finds nothing verdict-bearing. Dropping the round there kept
    losing exactly the failure the ledger most needs. Unreadable is not empty.

    It is recorded as an ATTEMPT, not a round: the reviewer never judged the diff,
    and CLAUDE.md rule 7 is explicit that an availability failure is not a verdict.
    The fail-closed contract lives in the EXIT CODE (still 1, so the agent halts) —
    the record's `verdict` field is telemetry, and calling an outage "block" would
    claim a rejection that never happened.
    """
    fid = "TEST-CR-ENVELOPE"
    shutil.rmtree(runs_dir / fid, ignore_errors=True)
    stub = _stub(
        tmp_path,
        "console.log(JSON.stringify({review:'adversarial-review',result:null,"
        "rawOutput:'',parseError:'no JSON found in reply',"
        "codex:{stderr:\"you've hit your usage limit, try again at 3:00 PM\",stdout:''}}));\n"
        "process.exit(1);\n",
    )
    try:
        proc, rounds = _run(stub, fid, runs_dir)
        assert proc.returncode == 1
        assert "usage limit" in proc.stdout, "the dispatcher parses this to fail over"
        assert len(rounds) == 1, "an unreadable envelope must still leave a trace"
        rec = json.loads(rounds[0])
        assert rec["verdict"] != "approve", "fail closed, never approve"
        assert rec["kind"] == "attempt", "no verdict was rendered — not a round"
        assert rec["n_findings"] == 0 and rec["rules"] == []
        assert "no JSON found in reply" in rec["summary"], "carry the reviewer's own reason"
        # The number the session note has to match: an outage is not a review round.
        jsonl = runs_dir / fid / "review.jsonl"
        assert adversarial_review.count_rounds(jsonl) == 0
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)


# ---------------------------------------------------------------------------
# The DISPATCHED path: adversarial_review.review() -> the REAL codex_review.sh ->
# a stub companion. Every earlier fix hardened the shell path and left this one
# dropping the attempt, because the unit tests monkeypatch run_codex — the exact
# function at fault. Here run_codex is real; only the two availability predicates
# and the paid fallback are stubbed.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dispatched_rate_limit_records_the_attempt_not_just_the_fallback(
    tmp_path, runs_dir, monkeypatch
):
    """A rate-limited Codex must leave a trace on the path the docs tell agents to use.

    run_codex sets ATP_REVIEW_DISPATCHED=1 so codex_review.sh does NOT record (else
    the round would land twice). review() then failed over and emitted only the
    FALLBACK — so the failed Codex attempt vanished from the ledger entirely.

    `codex_cooldown_until` is forced to None because a real cooldown file in the
    working tree would short-circuit review() before run_codex ever runs, quietly
    turning this into a test of nothing. `record_cooldown` is stubbed so the test
    cannot write to the developer's real tools/.codex_cooldown.json.
    """
    fid = "TEST-CR-DISPATCH"
    shutil.rmtree(runs_dir / fid, ignore_errors=True)
    stub = _stub(
        tmp_path,
        "console.log(JSON.stringify({review:'adversarial-review',result:null,"
        "rawOutput:'',parseError:'no JSON found in reply',"
        "codex:{stderr:\"you've hit your usage limit, try again at 3:00 PM\",stdout:''}}));\n"
        "process.exit(1);\n",
    )
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(stub))
    monkeypatch.setenv("ATP_FEATURE_ID", fid)
    monkeypatch.setattr(adversarial_review, "codex_cooldown_until", lambda: None)
    monkeypatch.setattr(adversarial_review, "record_cooldown", lambda *_a, **_k: None)

    def _no_claude(_base, timeout=900):
        raise FileNotFoundError("claude")  # keep it offline; no paid call from a test

    monkeypatch.setattr(adversarial_review, "run_claude_fallback", _no_claude)

    try:
        result = adversarial_review.review("origin/main")
        assert result["verdict"] == "block", "no verdict was rendered — must fail closed"
        adversarial_review.append_round(result)  # what emit() does

        jsonl = runs_dir / fid / "review.jsonl"
        assert jsonl.is_file(), "the dispatched attempt must be recorded somewhere"
        recs = adversarial_review.read_records(jsonl)
        assert recs, "unreadable/empty telemetry means the fix did not take"

        codex = [r for r in recs if r["reviewer"] == "codex"]
        assert codex, "THE DEFECT: the rate-limited Codex left no trace on this path"
        assert codex[0]["kind"] == "attempt"
        # Why it failed must be legible to whoever reads the ledger. The dispatcher
        # names the cause it detected; the summary carries the reviewer's own words.
        assert "usage limit" in codex[0]["reviewer_note"]
        assert "no JSON found in reply" in codex[0]["summary"]

        # Nobody judged the diff, so no round may be claimed.
        assert adversarial_review.count_rounds(jsonl) == 0
        assert all(r["verdict"] != "approve" for r in recs)
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)
