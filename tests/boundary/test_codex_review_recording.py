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
from pathlib import Path

import pytest

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
    d = ROOT / ".harness" / "runs"
    made = []
    yield d
    for fid in made:
        shutil.rmtree(d / fid, ignore_errors=True)


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
        assert len(rounds) == 1, "an unreadable round is still a round"
        rec = json.loads(rounds[0])
        assert rec["verdict"] == "block", "fail closed, never approve"
        assert "no JSON found in reply" in rec["summary"], "carry the reviewer's own reason"
    finally:
        shutil.rmtree(runs_dir / fid, ignore_errors=True)
