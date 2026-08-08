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
#   --json       Machine-readable counts (implies --dry-run). Used by the weekly
#                harness-garden workflow.
#   --scratch    Also clear leaked $TMPDIR/atp-* test scratch dirs. Off by default
#                because they are outside the repo.
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
JSON=0
SCRATCH=0
EXPLICIT_IDS=()

die() { echo "✗ $*" >&2; exit 1; }
# In --json mode stdout must carry ONLY the JSON object, so the human narration
# goes to stderr. A machine mode whose stdout needs filtering is not a machine mode.
say() { if [[ "${JSON:-0}" -eq 1 ]]; then echo "$@" >&2; else echo "$@"; fi; }
usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --force)   FORCE=1; shift ;;
    --json)    JSON=1; DRY_RUN=1; shift ;;
    --scratch) SCRATCH=1; shift ;;
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

# NOTE: no early exit when there are no worktrees. There used to be one, and the
# fresh CI checkout that harness-garden.yml runs on has no worktrees BY DEFINITION —
# so on its only automated trigger the collector printed one line of prose and
# reported none of the orphan plans, stale branches or leaked scratch it exists to
# surface. A collector that reports nothing on its own trigger is the thing this
# whole change set was fixing.
if [[ ${#WT_DIRS[@]} -eq 0 ]]; then
  say "No alphalabs-wt-* worktrees registered; checking the rest."
  [[ "$DRY_RUN" -eq 0 ]] && git -C "$ROOT_DIR" worktree prune
fi

removed=0
removable=0
for wt in ${WT_DIRS[@]+"${WT_DIRS[@]}"}; do
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
    say "  • keep ${id}: feature not closed on main (passes:false); skipping"
    continue
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    say "  [dry-run] would remove worktree ${wt} and branch ${branch} (${closed})"
    removable=$((removable + 1))
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

# ---------------------------------------------------------------------------
# The rest of the entropy. Worktrees were the visible part; these accumulate at
# the same rate and nothing has ever collected them.
# ---------------------------------------------------------------------------

# 1. Plan files for features that already closed. close_feature.py folds and removes
#    session-<id>.md and has never touched plan-<id>.md, so an approved plan for
#    finished work reads as current intent to the next session that opens the dir.
orphan_plans=()
while IFS= read -r plan; do
  [[ -n "$plan" ]] || continue
  pid="$(basename "$plan" .md)"; pid="${pid#plan-}"
  if feature_passes "$pid"; then orphan_plans+=("$plan"); fi
done < <(ls "${ROOT_DIR}"/progress.d/plan-*.md 2>/dev/null || true)

# 2. agent/* branches fully merged into main with no worktree left. The removal loop
#    above only reaches branches that still HAVE a worktree; a branch whose worktree
#    was already removed is invisible to it.
stale_branches=()
while IFS= read -r br; do
  [[ -n "$br" ]] || continue
  [[ "$br" == agent/* ]] || continue
  if ! git -C "$ROOT_DIR" worktree list --porcelain | grep -q "^branch refs/heads/${br}$"; then
    stale_branches+=("$br")
  fi
done < <(git -C "$ROOT_DIR" branch --merged main --format='%(refname:short)' 2>/dev/null || true)

# 3. Leaked test scratch. Rust helpers build $TMPDIR/atp-<name>-<pid>-<seq> and never
#    remove it; macOS recycles PIDs, so a fresh process inherits a POPULATED dir and
#    tests that assert absence fail in crates the diff never touched. 57,131 of these
#    (1.1 GB) were found on this machine, and they did exactly that.
#    See docs/playbooks/test-integrity.md rule 20.
scratch_dir="${TMPDIR:-/tmp}"
scratch_count=$(find "$scratch_dir" -maxdepth 1 -name 'atp-*' -type d 2>/dev/null | wc -l | tr -d ' ')

if [[ "$JSON" -eq 1 ]]; then
  printf '{"worktrees_removable":%d,"worktrees_removed":%d,"orphan_plans":%d,"stale_branches":%d,"leaked_scratch":%d}\n' \
    "$removable" "$removed" "${#orphan_plans[@]}" "${#stale_branches[@]}" "$scratch_count"
  exit 0
fi

echo
echo "== other accumulated entropy =="
if [[ ${#orphan_plans[@]} -gt 0 ]]; then
  echo "  ${#orphan_plans[@]} plan file(s) for CLOSED features:"
  for p in "${orphan_plans[@]}"; do
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] would remove $(basename "$p")"
    else
      rm -f "$p" && echo "    ✓ removed $(basename "$p")"
    fi
  done
else
  echo "  · no orphan plan files"
fi

if [[ ${#stale_branches[@]} -gt 0 ]]; then
  echo "  ${#stale_branches[@]} merged agent/* branch(es) with no worktree:"
  for br in "${stale_branches[@]}"; do
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "    [dry-run] would delete ${br}"
    else
      git -C "$ROOT_DIR" branch -d "$br" >/dev/null 2>&1 \
        && echo "    ✓ deleted ${br}" || echo "    · kept ${br} (not safely mergeable)"
    fi
  done
else
  echo "  · no stale merged branches"
fi

if [[ "$scratch_count" -gt 0 ]]; then
  echo "  ${scratch_count} leaked atp-* scratch dir(s) in ${scratch_dir}"
  if [[ "$SCRATCH" -eq 1 && "$DRY_RUN" -eq 0 ]]; then
    find "$scratch_dir" -maxdepth 1 -name 'atp-*' -type d -exec rm -rf {} + 2>/dev/null || true
    echo "    ✓ cleared (they cause phantom test failures — test-integrity.md rule 20)"
  else
    echo "    re-run with --scratch to clear; they cause phantom test failures"
  fi
else
  echo "  · no leaked test scratch"
fi
