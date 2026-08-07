"""L1 — the anti-churn note reader must see notes ROOT's working tree cannot.

`serialized_notes()` used to read ONLY `ROOT/progress.d` — the primary checkout's
working tree. That tree advances only when `_sync_primary_checkout()` can
fast-forward it, and that is fast-forward-ONLY, so it correctly refuses exactly
when the operator has their own work in ROOT (a WIP branch, a dirty tree) — the
normal case. On 2026-07-27 that left the four newest session notes invisible to the
scheduler and two agents were simultaneously re-offered already-serialized
features; by the end of that day the ready frontier was 100% churn.

`serialized_notes()` now reads `origin/main` UNION the working tree, so a note that
reached `main` via `integrate` de-churns its feature regardless of where ROOT sits.

Driven against a HERMETIC temp origin + clone; `agent_pool.ROOT` is monkeypatched so
nothing touches the real primary checkout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import agent_pool  # noqa: E402

pytestmark = pytest.mark.unit


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _note(outcome: str) -> str:
    return f"=== SESSION X ===\nDate: 2026-08-07\nOutcome: {outcome}\nWhat I did: ...\n"


def _origin_and_root(tmp_path: Path) -> tuple[Path, Path]:
    origin, root = tmp_path / "origin.git", tmp_path / "root"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "t@t.io")
    _git(root, "config", "user.name", "tester")
    _git(root, "remote", "add", "origin", str(origin))
    (root / "progress.d").mkdir()
    (root / "progress.d" / "session-OLD.md").write_text(_note("serialized"), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    _git(root, "push", "-u", "origin", "main")
    return origin, root


def _sibling_integrate(tmp_path: Path, origin: Path, fid: str, outcome: str, tag: str) -> None:
    """Another worktree lands a note on origin/main — ROOT never sees the file."""
    other = tmp_path / f"other-{tag}"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t.io")
    _git(other, "config", "user.name", "tester")
    (other / "progress.d" / f"session-{fid}.md").write_text(_note(outcome), encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", f"integrate {fid}")
    _git(other, "push", "origin", "main")


def _strand_root(root: Path) -> None:
    """Put ROOT exactly where `_sync_primary_checkout` refuses to help it: off `main`,
    behind origin/main, with a dirty working tree (the 2026-07-27 state)."""
    _git(root, "checkout", "-b", "chore/operator-wip")
    (root / "scratch.txt").write_text("in-flight operator work\n", encoding="utf-8")


@pytest.fixture
def pool_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    origin, root = _origin_and_root(tmp_path)
    monkeypatch.setattr(agent_pool, "ROOT", root)
    return origin, root


def test_a_note_on_origin_main_is_seen_while_root_lags(tmp_path: Path, pool_root) -> None:
    origin, root = pool_root
    _sibling_integrate(tmp_path, origin, "SRS-NEW-001", "serialized", "new")
    _strand_root(root)
    _git(root, "fetch", "origin", "main")

    # The working tree genuinely cannot see it -- this is the bug, reproduced.
    assert not (root / "progress.d" / "session-SRS-NEW-001.md").exists()
    assert agent_pool._serialized_from_worktree(root / "progress.d") == {"OLD"}

    # ...and the fix: origin/main carries it, so the scheduler does see it.
    assert agent_pool.serialized_notes() == {"OLD", "SRS-NEW-001"}


def test_the_union_never_drops_a_working_tree_only_note(tmp_path: Path, pool_root) -> None:
    origin, root = pool_root
    _sibling_integrate(tmp_path, origin, "SRS-NEW-001", "serialized", "new")
    _git(root, "fetch", "origin", "main")
    # A note that exists only on disk (not yet integrated) must still de-churn.
    (root / "progress.d" / "session-SRS-LOCAL-002.md").write_text(
        _note("serialized"), encoding="utf-8"
    )
    assert agent_pool.serialized_notes() == {"OLD", "SRS-NEW-001", "SRS-LOCAL-002"}


def test_only_a_real_serialized_outcome_counts_on_the_ref(tmp_path: Path, pool_root) -> None:
    origin, root = pool_root
    _sibling_integrate(tmp_path, origin, "SRS-DONE-003", "complete", "done")
    _sibling_integrate(tmp_path, origin, "SRS-PART-004", "partial(blocked-on Y)", "part")
    # The template's own menu line must not false-positive.
    other = tmp_path / "other-menu"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t.io")
    _git(other, "config", "user.name", "tester")
    (other / "progress.d" / "session-SRS-MENU-005.md").write_text(
        "Outcome: complete | serialized | partial(blocked-on X)\n", encoding="utf-8"
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "menu")
    _git(other, "push", "origin", "main")
    _strand_root(root)
    _git(root, "fetch", "origin", "main")

    assert agent_pool.serialized_notes() == {"OLD"}


def test_the_first_outcome_line_wins_on_the_ref(tmp_path: Path, pool_root) -> None:
    """A later session block in the same note must not override the outcome line —
    the same first-line-wins rule the working-tree reader has always used."""
    origin, root = pool_root
    other = tmp_path / "other-two-blocks"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t.io")
    _git(other, "config", "user.name", "tester")
    (other / "progress.d" / "session-SRS-TWO-006.md").write_text(
        "=== SESSION X ===\nOutcome: complete\nbody\n\n=== SESSION X (2) ===\nOutcome: serialized\n",
        encoding="utf-8",
    )
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "two blocks")
    _git(other, "push", "origin", "main")
    _strand_root(root)
    _git(root, "fetch", "origin", "main")

    assert "SRS-TWO-006" not in agent_pool.serialized_notes()


def test_an_unreadable_ref_falls_back_to_the_working_tree(tmp_path: Path, pool_root) -> None:
    """Offline / no remote must NOT read as "no serialized notes" — that would
    re-offer every already-done feature."""
    _origin, root = pool_root
    assert agent_pool._serialized_from_ref("no/such/ref") is None
    assert agent_pool.serialized_notes() == {"OLD"}  # the on-disk note still counts


def test_an_explicit_dir_still_overrides_both_sources(tmp_path: Path, pool_root) -> None:
    _origin, root = pool_root
    d = tmp_path / "elsewhere"
    d.mkdir()
    (d / "session-SRS-ONLY-007.md").write_text(_note("serialized"), encoding="utf-8")
    assert agent_pool.serialized_notes(d) == {"SRS-ONLY-007"}
