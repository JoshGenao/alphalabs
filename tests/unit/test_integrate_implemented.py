"""L1 - `agent_pool.py integrate --mode implemented`, end to end, in scratch git repos.

The independent review found that the first version of this path could never
succeed: the integrator appended its review round to the worktree's TRACKED
review.jsonl, the dirty-tree check then refused (exit 7), and the auto-park on a
failed gate was unreachable. Unit tests had mocked every seam that failed. This
drives the real cmd_integrate - rebase, close_feature subprocess, staging, push -
against a bare origin, a primary checkout, and an agent worktree. Only the LLM
reviewer itself is replaced.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import adversarial_review
import agent_pool
import contracts
import evidence
import pytest

pytestmark = pytest.mark.unit

FID = "SRS-TST-001"
AC = "Verify acceptance criteria: Given X, the probe reports Y."
TEST_CMD = "python3 -m pytest t_c1.py -q -p no:cacheprovider"
TOOLS = Path(agent_pool.__file__).resolve().parent
COPY = (
    "agent_pool.py",
    "evidence.py",
    "close_feature.py",
    "adversarial_review.py",
    "dependency_model.py",
    "contracts.py",
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    env_path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    monkeypatch.setenv("PATH", env_path)
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }.items():
        monkeypatch.setenv(k, v)

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    primary = tmp_path / "primary"
    subprocess.run(["git", "clone", "-q", str(origin), str(primary)], check=True)
    git(primary, "checkout", "-q", "-b", "main")

    (primary / "tools").mkdir()
    for name in COPY:
        shutil.copy(TOOLS / name, primary / "tools" / name)
    feature = {
        "id": FID,
        "category": "market_data",
        "description": "a probe",
        "priority": "P1",
        "release": "MVP",
        "steps": ["Step 1: init", "Step 2: run", f"Step 3: {AC}", "Step 4: record"],
        "passes": False,
        "needs_clarification": False,
        "verification_method": "solo",
    }
    (primary / "feature_list.json").write_text(json.dumps([feature], indent=2))
    (primary / "tools" / "feature_deps.json").write_text("{}\n")
    (primary / "tools" / "dependency_types.json").write_text('{"edges": {}}\n')
    (primary / "docs" / "contracts").mkdir(parents=True)
    (primary / "docs" / "SRS.md").write_text(
        f"| {FID} | stmt | ac | Test | P1 | SyRS: SYS-1; StRS: SN-1.01 |\n"
    )
    contract = {
        "feature": FID,
        "requirements": ["SYS-1"],
        "scope": "a probe",
        "implementation_paths": ["probe.txt"],
        "interfaces": {"consumes": [], "provides": []},
        "prerequisites": [],
        "obligations": [
            {
                "id": "C1",
                "level": "component",
                "criterion": "Given X, the probe returns Y.",
                "tests": [TEST_CMD],
            },
            {"id": "S1", "level": "system", "criterion": AC},
        ],
    }
    (primary / "docs" / "contracts" / f"{FID}.json").write_text(json.dumps(contract, indent=2))
    (primary / "t_c1.py").write_text("def test_c1():\n    assert True\n")
    (primary / ".gitignore").write_text(  # mirrors the repo's own ignore rules
        ".harness/*.jsonl\n.harness/tmp/\ntools/.agent_*\n__pycache__/\n"
    )
    git(primary, "add", "-A")
    git(primary, "commit", "-q", "-m", "base")
    git(primary, "push", "-q", "origin", "main")

    wt = tmp_path / f"alphalabs-wt-{FID}"
    git(primary, "worktree", "add", "-q", "-b", f"agent/{FID}", str(wt), "origin/main")
    (wt / "probe.txt").write_text("the implementation\n")
    git(wt, "add", "probe.txt")
    git(wt, "commit", "-q", "-m", "feat: probe")
    run = subprocess.run(
        [
            sys.executable,
            str(wt / "tools" / "evidence.py"),
            "run",
            FID,
            "--obligation",
            "C1",
            "--",
            *TEST_CMD.split(),
        ],
        cwd=wt,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    git(wt, "add", ".harness")
    git(wt, "commit", "-q", "-m", "chore: evidence")

    # Point the in-process modules at the scratch repos.
    for name, value in {
        "ROOT": primary,
        "FEATURE_FILE": primary / "feature_list.json",
        "DEPS_FILE": primary / "tools" / "feature_deps.json",
        "TYPES_FILE": primary / "tools" / "dependency_types.json",
        "RUNTIME_FILE": primary / "tools" / ".agent_runtime.json",
        "LOCK_FILE": primary / "tools" / ".agent_pool.lock",
    }.items():
        monkeypatch.setattr(agent_pool, name, value)
    for name, value in {
        "ROOT": wt,
        "FEATURE_FILE": wt / "feature_list.json",
        "HARNESS_DIR": wt / ".harness",
        "RUNS_DIR": wt / ".harness" / "runs",
        "LEDGER_DIR": primary / ".harness",
        "CLOSES_LOG": primary / ".harness" / "closes.jsonl",
    }.items():
        monkeypatch.setattr(evidence, name, value)
    monkeypatch.setattr(contracts, "CONTRACTS_DIR", wt / "docs" / "contracts")
    monkeypatch.setattr(contracts, "SRS_FILE", wt / "docs" / "SRS.md")
    # The reviewer module writes relative to its REPO_ROOT; point it at the scratch
    # worktree so any write it makes lands where the integrate checks can see it.
    monkeypatch.setattr(adversarial_review, "REPO_ROOT", wt)
    monkeypatch.setattr(adversarial_review, "current_head", lambda: git(wt, "rev-parse", "HEAD"))
    monkeypatch.setattr(adversarial_review, "check_budget", lambda *a, **k: None)
    monkeypatch.chdir(primary)
    return SimpleNamespace(origin=origin, primary=primary, wt=wt)


def origin_feature(repos) -> dict:
    git(repos.primary, "fetch", "-q", "origin")
    return json.loads(git(repos.primary, "show", "origin/main:feature_list.json"))[0]


def integrate(monkeypatch, verdict: str) -> int:
    reviewed = []

    def reviewer(base):
        reviewed.append(os.environ.get("ATP_FEATURE_ID"))
        return {"verdict": verdict, "reviewer": "codex", "findings": [], "summary": "fake"}

    monkeypatch.setattr(adversarial_review, "review", reviewer)
    rc = agent_pool.cmd_integrate(
        SimpleNamespace(id=FID, mode="implemented", force_complete=False, reason="", dry_run=False)
    )
    assert reviewed == [FID], "the integrator must run the reviewer itself, exactly once"
    return rc


def test_approved_feature_lands_implementation_complete_and_passes_stays_false(repos, monkeypatch):
    assert integrate(monkeypatch, "approve") == 0
    feat = origin_feature(repos)
    assert feat["implementation"]["status"] == "complete"
    assert feat["implementation"]["reviewer"] == "codex"
    assert feat["passes"] is False
    # The integrator's round went to the primary's gitignored ledger, not the worktree.
    assert (repos.primary / ".harness" / "integrator-reviews.jsonl").is_file()
    assert not (repos.wt / ".harness" / "runs" / FID / "review.jsonl").exists()
    assert git(repos.primary, "status", "--porcelain") == ""


def test_reviewer_block_lands_the_code_serialized_and_parks_the_feature(repos, monkeypatch):
    assert integrate(monkeypatch, "block") == 0
    feat = origin_feature(repos)
    assert "implementation" not in feat and feat["passes"] is False
    assert "probe.txt" in git(repos.primary, "ls-tree", "--name-only", "origin/main")  # code landed
    runtime = json.loads((repos.primary / "tools" / ".agent_runtime.json").read_text())
    park = runtime["parked"][FID]
    assert park["kind"] == "test-failure" and "block" in park["reason"]


def test_a_branch_that_edits_the_gate_is_left_to_the_operator(repos, monkeypatch):
    (repos.wt / "tools" / "evidence.py").write_text("# tampered\n")
    git(repos.wt, "commit", "-qam", "tamper with the judge")
    monkeypatch.setattr(
        adversarial_review, "review", lambda base: pytest.fail("reviewer must not run")
    )
    rc = agent_pool.cmd_integrate(
        SimpleNamespace(id=FID, mode="implemented", force_complete=False, reason="", dry_run=False)
    )
    assert rc == 0
    assert "implementation" not in origin_feature(repos)
    runtime = json.loads((repos.primary / "tools" / ".agent_runtime.json").read_text())
    assert "verification harness" in runtime["parked"][FID]["reason"]


def test_main_moving_during_review_lands_the_code_and_parks_instead_of_stranding(
    repos, monkeypatch
):
    """Reviewer finding (r2 #3): the review takes minutes; if main moves meanwhile the
    rebase rewrites the branch and the pre-rebase evidence no longer places. That must
    land the code as `serialized` and park, never strand it with exit 11."""

    def reviewer_while_main_moves(base):
        (repos.primary / "sibling.txt").write_text("a sibling landed meanwhile\n")
        git(repos.primary, "add", "sibling.txt")
        git(repos.primary, "commit", "-q", "-m", "sibling")
        git(repos.primary, "push", "-q", "origin", "main")
        return {"verdict": "approve", "reviewer": "codex", "findings": [], "summary": "ok"}

    monkeypatch.setattr(adversarial_review, "review", reviewer_while_main_moves)
    rc = agent_pool.cmd_integrate(
        SimpleNamespace(id=FID, mode="implemented", force_complete=False, reason="", dry_run=False)
    )
    assert rc == 0
    feat = origin_feature(repos)
    assert "implementation" not in feat
    assert "probe.txt" in git(repos.primary, "ls-tree", "--name-only", "origin/main")
    runtime = json.loads((repos.primary / "tools" / ".agent_runtime.json").read_text())
    assert "post-rebase" in runtime["parked"][FID]["reason"]
