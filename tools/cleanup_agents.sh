#!/usr/bin/env bash
#
# cleanup_agents.sh — tear down spawned agent worktrees + branches.
#
# Counterpart to tools/spawn_agents.sh. Removes the worktree and branch for any
# feature that has been closed on main (its feature_list.json "passes" is now
# true), then prunes stale worktree metadata.
#
# "Closed on main" (passes:true) is the cleanup signal rather than `git branch
# --merged`, because GitHub squash-merges do not register the agent branch as
# merged. Run this from the primary checkout, on an up-to-date main.
#
# Usage:
#   tools/cleanup_agents.sh [options] [FEATURE_ID ...]
#
# Options:
#   --dry-run    Show what would be removed; change nothing.
#   --force      Remove the worktree even if it has uncommitted changes, and
#                remove worktrees whose feature is NOT yet closed (use with care).
#   -h, --help   Show this help.
#
# With no FEATURE_IDs, every alphalabs-wt-* worktree whose feature is closed is
# removed. With FEATURE_IDs, only those are considered.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT_DIR="$(dirname "$ROOT_DIR")"
FEATURE_FILE="${ROOT_DIR}/feature_list.json"

DRY_RUN=0
FORCE=0
EXPLICIT_IDS=()

die() { echo "✗ $*" >&2; exit 1; }
usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help) usage ;;
    -*)        die "unknown option: $1" ;;
    *)         EXPLICIT_IDS+=("$1"); shift ;;
  esac
done

command -v git >/dev/null 2>&1 || die "git is required"
[[ -f "$FEATURE_FILE" ]] || die "feature_list.json not found at $FEATURE_FILE"

feature_passes() {
  python3 - "$FEATURE_FILE" "$1" <<'PY'
import json, sys
features = json.load(open(sys.argv[1]))
f = next((f for f in features if f.get("id") == sys.argv[2]), None)
sys.exit(0 if (f is not None and f.get("passes") is True) else 1)
PY
}

# Collect agent worktrees from git's own registry (porcelain), so we only touch
# worktrees git actually knows about. (while-read, not mapfile: portable to the
# bash 3.2 shipped on macOS.)
WT_DIRS=()
while IFS= read -r wtdir; do
  [[ -n "$wtdir" ]] && WT_DIRS+=("$wtdir")
done < <(
  cd "$ROOT_DIR"
  git worktree list --porcelain \
    | awk '/^worktree /{print substr($0,10)}' \
    | grep "/alphalabs-wt-" || true
)

if [[ ${#WT_DIRS[@]} -eq 0 ]]; then
  echo "No alphalabs-wt-* worktrees registered."
  [[ "$DRY_RUN" -eq 0 ]] && git -C "$ROOT_DIR" worktree prune
  exit 0
fi

removed=0
for wt in "${WT_DIRS[@]}"; do
  id="${wt##*/alphalabs-wt-}"
  branch="agent/${id}"

  # Honor an explicit id filter.
  if [[ ${#EXPLICIT_IDS[@]} -gt 0 ]]; then
    keep=1
    for want in "${EXPLICIT_IDS[@]}"; do [[ "$want" == "$id" ]] && keep=0; done
    [[ "$keep" -eq 1 ]] && continue
  fi

  if feature_passes "$id"; then
    closed="closed"
  elif [[ "$FORCE" -eq 1 ]]; then
    closed="NOT-closed (forced)"
  else
    echo "  • keep ${id}: feature not closed on main (passes:false); skipping"
    continue
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would remove worktree ${wt} and branch ${branch} (${closed})"
    continue
  fi

  echo "→ removing ${id} (${closed})"
  if [[ "$FORCE" -eq 1 ]]; then
    remove_ok=0
    git -C "$ROOT_DIR" worktree remove --force "$wt" && remove_ok=1
  else
    remove_ok=0
    git -C "$ROOT_DIR" worktree remove "$wt" && remove_ok=1
  fi
  if [[ "$remove_ok" -eq 1 ]]; then
    echo "  ✓ worktree removed: alphalabs-wt-${id}"
  else
    echo "  ✗ worktree busy/dirty (re-run with --force): ${wt}" >&2
    continue
  fi

  if git -C "$ROOT_DIR" show-ref --verify --quiet "refs/heads/${branch}"; then
    git -C "$ROOT_DIR" branch -D "$branch" >/dev/null \
      && echo "  ✓ branch deleted: ${branch}"
  fi
  removed=$((removed + 1))
done

if [[ "$DRY_RUN" -eq 0 ]]; then
  git -C "$ROOT_DIR" worktree prune
  echo
  echo "✓ removed ${removed} worktree(s). Remaining:"
  git -C "$ROOT_DIR" worktree list
fi
