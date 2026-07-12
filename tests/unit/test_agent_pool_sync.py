"""Unit + hermetic-git tests for the primary-checkout sync (ROOT-lag fix).

The scheduler reads the PRIMARY checkout's working tree to decide what is claimable
(`serialized_notes()` / `load_features()`), but nothing kept it current, so features
integrated from sibling worktrees (serialized notes, passes flips) were invisible and
already-de-churned features got re-offered. `agent_pool._sync_primary_checkout()` now
fast-forwards the primary checkout to origin/main under the pool lock at claim time,
preserving the canonical `tools/feature_deps.json` (which carries in-place `block`
edits) via a per-key union.

These tests cover the pure union (`_union_deps`) and drive `_sync_primary_checkout`
against a HERMETIC temp git origin+clone (passing `root=` so nothing touches the real
primary checkout).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from agent_pool import _sync_primary_checkout, _union_deps  # noqa: E402

# --------------------------------------------------------------------------- #
# _union_deps — pure per-key union
# --------------------------------------------------------------------------- #


def test_union_keeps_both_sides_deduped_and_sorted() -> None:
    base = {"A": ["B", "C"]}
    extra = {"A": ["D", "C"]}  # C overlaps; D is only in extra
    assert _union_deps(base, extra) == {"A": ["B", "C", "D"]}


def test_union_adds_extra_only_keys_and_preserves_base_only_keys() -> None:
    base = {"A": ["B"], "X": ["Y"]}
    extra = {"Z": ["W"]}
    assert _union_deps(base, extra) == {"A": ["B"], "X": ["Y"], "Z": ["W"]}


def test_union_idempotent_when_extra_is_a_subset() -> None:
    # The common case: our in-place edits are already committed on origin/main.
    base = {"A": ["B", "C"]}
    assert _union_deps(base, {"A": ["B"]}) == base
    assert _union_deps(base, {}) == base


def test_union_does_not_mutate_inputs() -> None:
    base = {"A": ["B"]}
    extra = {"A": ["C"]}
    _union_deps(base, extra)
    assert base == {"A": ["B"]} and extra == {"A": ["C"]}


# --------------------------------------------------------------------------- #
# _sync_primary_checkout — hermetic temp git repo
# --------------------------------------------------------------------------- #


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=check, capture_output=True, text=True
    )


def _write_deps(repo: Path, deps: dict) -> None:
    (repo / "tools" / "feature_deps.json").write_text(
        json.dumps(deps, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _read_deps(repo: Path) -> dict:
    return json.loads((repo / "tools" / "feature_deps.json").read_text(encoding="utf-8"))


def _make_origin_and_root(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin with an initial commit, and a `root` clone tracking origin/main."""
    origin = tmp_path / "origin.git"
    root = tmp_path / "root"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "t@t.io")
    _git(root, "config", "user.name", "tester")
    _git(root, "remote", "add", "origin", str(origin))
    (root / "tools").mkdir()
    (root / "progress.d").mkdir()
    _write_deps(root, {"A": ["B"]})
    (root / "feature_list.json").write_text("[]\n", encoding="utf-8")
    (root / "progress.d" / "session-OLD.md").write_text("Outcome: serialized\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    _git(root, "push", "-u", "origin", "main")
    return origin, root


def _advance_origin(tmp_path: Path, origin: Path) -> None:
    """A sibling integrate: add a new serialized note + a new deps edge on origin/main."""
    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.email", "t@t.io")
    _git(other, "config", "user.name", "tester")
    (other / "progress.d" / "session-NEW.md").write_text("Outcome: serialized\n", encoding="utf-8")
    _write_deps(other, {"A": ["B", "C"]})  # origin gains edge C
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "sibling integrate")
    _git(other, "push", "origin", "main")


def test_sync_fast_forwards_and_unions_in_flight_block_edits(tmp_path: Path) -> None:
    origin, root = _make_origin_and_root(tmp_path)
    _advance_origin(tmp_path, origin)
    # root is now behind origin/main AND carries an in-place `block` edit (edge D) that
    # is NOT yet on origin/main -- exactly the dirty-canonical-deps case.
    _write_deps(root, {"A": ["B", "D"]})
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()

    _sync_primary_checkout(root)

    # Fast-forwarded to origin/main...
    head_after = _git(root, "rev-parse", "HEAD").stdout.strip()
    origin_main = _git(root, "rev-parse", "origin/main").stdout.strip()
    assert head_after != head_before and head_after == origin_main
    # ...the newly-integrated serialized note is now visible in the working tree...
    assert (root / "progress.d" / "session-NEW.md").exists()
    # ...and NO deps edge was lost: origin's C ∪ our in-flight D (both on top of B).
    assert _read_deps(root) == {"A": ["B", "C", "D"]}
    # working tree clean except (possibly) the re-unioned canonical deps file
    dirty = [ln[3:] for ln in _git(root, "status", "--porcelain").stdout.splitlines() if ln.strip()]
    assert all(p == "tools/feature_deps.json" for p in dirty)


def test_sync_noop_when_primary_is_ahead_or_diverged(tmp_path: Path) -> None:
    origin, root = _make_origin_and_root(tmp_path)
    # root commits locally without pushing -> ahead of origin/main; origin not advanced.
    (root / "progress.d" / "session-LOCAL.md").write_text("Outcome: serialized\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "local-only")
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()

    _sync_primary_checkout(root)

    # Not fast-forwardable -> left untouched.
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before


def test_sync_noop_on_unexpected_dirty_file(tmp_path: Path) -> None:
    origin, root = _make_origin_and_root(tmp_path)
    _advance_origin(tmp_path, origin)
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    # A dirty file OTHER than the canonical deps -> the sync must refuse (don't stomp it).
    (root / "feature_list.json").write_text('[{"id": "LOCAL"}]\n', encoding="utf-8")

    _sync_primary_checkout(root)

    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before  # not advanced
    assert json.loads((root / "feature_list.json").read_text()) == [{"id": "LOCAL"}]  # preserved


def test_sync_noop_when_already_current(tmp_path: Path) -> None:
    _origin, root = _make_origin_and_root(tmp_path)
    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    deps_before = _read_deps(root)

    _sync_primary_checkout(root)

    assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _read_deps(root) == deps_before
