"""L1 — Unit tests for the documentation reference gate (tools/docs_link_check.py).

Four defects the audit found by reading, one at a time: README.md pointed at
docs/StRS.md and docs/SyRS.md (neither ever existed) while init.sh curled that file
as its smoke test; critic_prompt.md sent reviewers to a renumbered "Step 6.5". None
broke anything loudly; all sent an agent somewhere that was not there.

The hard requirement is a LOW false-positive rate. The first draft flagged 66
references of which ~64 were prose, and a checker like that gets switched off the
first day — the same "a guard that always fires is one everybody ignores" lesson
docs/playbooks/pipeline-and-integrate.md rule 28 records.
"""

from __future__ import annotations

import docs_link_check as dlc
import pytest

pytestmark = pytest.mark.unit

TOP = frozenset({"tools", "docs", "prompts", "tests", "progress.d", "AGENTS.md"})


# --- the false positives that made the first draft useless -------------------
@pytest.mark.parametrize(
    "text",
    [
        "close_feature.py",  # bare filename: prose, not navigation
        "origin/main",  # git ref
        "agent/SRS-LOG-001",  # branch
        "refs/heads/main",  # ref path
        "/codex:adversarial-review",  # slash command
        "~/.codex/skills/",  # home path
        ".py",  # bare extension
        "https://example.com/x",  # URL
        "cargo test --workspace",  # command
        "tools/*.py",  # glob
        "progress.d/plan-<id>.md",  # placeholder
    ],
)
def test_prose_is_not_treated_as_a_path(text):
    assert dlc._looks_like_path(text, TOP) is False


@pytest.mark.parametrize(
    "text", ["tools/evidence.py", "docs/playbooks/INDEX.md", "progress.d/README.md", "docs/SRS.md"]
)
def test_real_repo_relative_paths_are_checked(text):
    assert dlc._looks_like_path(text, TOP) is True


# --- and it must still catch the real thing ----------------------------------
def test_a_dead_path_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(dlc, "ROOT", tmp_path)
    (tmp_path / "tools").mkdir()
    doc = tmp_path / "AGENTS.md"
    doc.write_text("See `tools/gone.py` for detail.\n", encoding="utf-8")
    problems = dlc.check_file(doc, set(), frozenset({"tools", "AGENTS.md"}))
    assert any("tools/gone.py" in p for p in problems)


def test_a_broken_markdown_link_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(dlc, "ROOT", tmp_path)
    doc = tmp_path / "AGENTS.md"
    doc.write_text("[the playbooks](docs/playbooks/NOPE.md)\n", encoding="utf-8")
    problems = dlc.check_file(doc, set(), frozenset())
    assert any("does not resolve" in p for p in problems)


def test_a_renumbered_step_is_reported(tmp_path, monkeypatch):
    """The exact critic_prompt.md 'Step 6.5' defect, after it became 6.6."""
    monkeypatch.setattr(dlc, "ROOT", tmp_path)
    doc = tmp_path / "critic_prompt.md"
    doc.write_text("see coding_prompt.md Step 6.5 Pass 2\n", encoding="utf-8")
    problems = dlc.check_file(doc, {"6.6", "7"}, frozenset())
    assert any("cites 'Step 6.5'" in p for p in problems)


def test_a_file_that_defines_its_own_steps_owns_them(tmp_path, monkeypatch):
    """initializer_prompt.md has nine `## Step N` headings of its own."""
    monkeypatch.setattr(dlc, "ROOT", tmp_path)
    doc = tmp_path / "initializer_prompt.md"
    doc.write_text("## Step 3.5 — Scaffold\n\nGo back to Step 3.5 when done.\n", encoding="utf-8")
    assert dlc.check_file(doc, {"6.6"}, frozenset()) == []


def test_the_shipped_tree_has_no_unresolved_references():
    """The gate must be green on the repository it ships in."""
    assert dlc.main([]) == 0


# --- the worktree class: a guard that fires everywhere fires for no one -------
#
# Every parallel agent session runs in a linked `alphalabs-wt-<id>` worktree, where `.git` is a
# FILE (a `gitdir:` pointer) rather than a directory, and where the scheduler's lease file lives
# in the PRIMARY checkout. Both made this gate red in every worktree, for references that are
# perfectly correct — and a red gate nobody caused is one everybody learns to skip.


def test_a_dot_git_reference_resolves_through_git_not_the_literal_directory():
    # Correct in the primary checkout AND in a worktree; before the fix, only the former.
    assert dlc._resolve(".git/hooks/pre-commit", dlc.ROOT / "AGENTS.md")


def test_a_missing_dot_git_reference_is_still_reported():
    # Non-vacuity: the `.git/` branch must resolve real paths, not wave the prefix through.
    assert not dlc._resolve(".git/hooks/no-such-hook", dlc.ROOT / "AGENTS.md")


def test_a_gitignored_runtime_artifact_is_a_valid_reference():
    # `tools/.agent_runtime.json` is written by the scheduler at claim time, in the primary
    # checkout. A document naming it is telling the reader where the state lives; that pointer
    # is not broken merely because no agent is running.
    assert "tools/.agent_runtime.json" in dlc.RUNTIME_ARTIFACTS
    assert dlc._resolve("tools/.agent_runtime.json", dlc.ROOT / "AGENTS.md")


def test_an_unlisted_missing_path_under_tools_is_still_reported():
    # The runtime-artifact allowance is an explicit allowlist, not a blanket exemption for
    # anything under tools/.
    assert not dlc._resolve("tools/.no_such_runtime_file.json", dlc.ROOT / "AGENTS.md")
