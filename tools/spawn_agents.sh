#!/usr/bin/env bash
#
# spawn_agents.sh — orchestrator launcher for parallel coding agents.
#
# Creates one git worktree + branch per assigned feature so that several agents
# can run concurrently without file/branch/port collisions. Each agent is given
# exactly one feature via ATP_FEATURE_ID and a private dev-server port block.
#
# See prompts/coding_prompt.md (Step 0) for the agent-side contract.
#
# Usage:
#   tools/spawn_agents.sh [options] [FEATURE_ID ...]
#
# Options:
#   -n N            Number of features to assign when none are listed (default 3).
#   --dry-run       Print the plan; create nothing, fetch nothing.
#   --base REF      Base ref for new worktrees (default: origin/main, then main).
#   --launch TMPL   Shell template to start each agent. Run with cwd = the
#                   worktree and ATP_FEATURE_ID / ATP_DEV_PORT / ATP_IB_LIVE_PORT /
#                   ATP_IB_PAPER_PORT exported. Example:
#                     --launch 'claude -p "$(cat prompts/coding_prompt.md)"'
#                   If omitted, the script only prints a ready-to-run invocation.
#   -h, --help      Show this help.
#
# Positional FEATURE_IDs override automatic selection (still validated as
# failing + unclaimed). Otherwise the N highest-priority unclaimed failing
# features are chosen (same order the single-agent flow used: priority, then id).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT_DIR="$(dirname "$ROOT_DIR")"
FEATURE_FILE="${ROOT_DIR}/feature_list.json"

N=3
DRY_RUN=0
BASE_REF=""
LAUNCH_TMPL=""
EXPLICIT_IDS=()

die() { echo "✗ $*" >&2; exit 1; }

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n)         N="${2:?-n needs a number}"; shift 2 ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --base)     BASE_REF="${2:?--base needs a ref}"; shift 2 ;;
    --launch)   LAUNCH_TMPL="${2:?--launch needs a template}"; shift 2 ;;
    -h|--help)  usage ;;
    --)         shift; while [[ $# -gt 0 ]]; do EXPLICIT_IDS+=("$1"); shift; done ;;
    -*)         die "unknown option: $1" ;;
    *)          EXPLICIT_IDS+=("$1"); shift ;;
  esac
done

[[ -f "$FEATURE_FILE" ]] || die "feature_list.json not found at $FEATURE_FILE"
command -v git >/dev/null 2>&1 || die "git is required"

# Must run from the primary worktree, not from a spawned one.
if git -C "$ROOT_DIR" rev-parse --git-common-dir >/dev/null 2>&1; then
  common="$(cd "$ROOT_DIR" && git rev-parse --git-common-dir)"
  gitdir="$(cd "$ROOT_DIR" && git rev-parse --git-dir)"
  [[ "$(cd "$ROOT_DIR" && cd "$common" && pwd)" == "$(cd "$ROOT_DIR" && cd "$gitdir" && pwd)" ]] \
    || die "run this from the primary checkout, not a spawned worktree"
fi

# Resolve the base ref. Fetch first (skipped in --dry-run to stay side-effect-free).
if [[ -z "$BASE_REF" ]]; then
  if [[ "$DRY_RUN" -eq 0 ]]; then
    echo "→ Fetching origin..."
    git -C "$ROOT_DIR" fetch --quiet origin || echo "  (fetch failed; using local refs)"
  fi
  if git -C "$ROOT_DIR" rev-parse --verify --quiet origin/main >/dev/null; then
    BASE_REF="origin/main"
  elif git -C "$ROOT_DIR" rev-parse --verify --quiet main >/dev/null; then
    BASE_REF="main"
    echo "  (no origin/main; basing worktrees on local main)"
  else
    die "no origin/main or main to base worktrees on (pass --base REF)"
  fi
fi

# A feature is "claimed" if its branch or its worktree dir already exists.
is_claimed() {
  local id="$1"
  git -C "$ROOT_DIR" show-ref --verify --quiet "refs/heads/agent/${id}" && return 0
  [[ -e "${PARENT_DIR}/alphalabs-wt-${id}" ]] && return 0
  return 1
}

is_failing() {
  python3 - "$FEATURE_FILE" "$1" <<'PY'
import json, sys
features = json.load(open(sys.argv[1]))
fid = sys.argv[2]
f = next((f for f in features if f.get("id") == fid), None)
sys.exit(0 if (f is not None and not f.get("passes", False)) else 1)
PY
}

# Build the selection list.
SELECTED=()
if [[ ${#EXPLICIT_IDS[@]} -gt 0 ]]; then
  for id in "${EXPLICIT_IDS[@]}"; do
    if ! is_failing "$id"; then
      echo "  skip ${id}: not a failing feature in feature_list.json" >&2; continue
    fi
    if is_claimed "$id"; then
      echo "  skip ${id}: already claimed (branch or worktree exists)" >&2; continue
    fi
    SELECTED+=("$id")
  done
else
  # Highest-priority failing features first (priority, then id) — same order the
  # single-agent flow used. Then drop already-claimed ones and take N.
  # (while-read, not mapfile: portable to the bash 3.2 shipped on macOS.)
  CANDIDATES=()
  while IFS= read -r cand; do
    [[ -n "$cand" ]] && CANDIDATES+=("$cand")
  done < <(python3 - "$FEATURE_FILE" <<'PY'
import json, sys
features = json.load(open(sys.argv[1]))
failing = [f for f in features if not f.get("passes", False)
           and not f.get("needs_clarification", False)]
for f in sorted(failing, key=lambda x: (x.get("priority", "P9"), x.get("id", ""))):
    print(f["id"])
PY
)
  for id in ${CANDIDATES[@]+"${CANDIDATES[@]}"}; do
    [[ ${#SELECTED[@]} -ge $N ]] && break
    is_claimed "$id" && continue
    SELECTED+=("$id")
  done
fi

[[ ${#SELECTED[@]} -gt 0 ]] || die "no unclaimed failing features to assign"

echo
echo "Base ref: $BASE_REF    Mode: $([[ $DRY_RUN -eq 1 ]] && echo DRY-RUN || echo LIVE)"
printf '%-22s %-26s %-34s %s\n' "FEATURE" "BRANCH" "WORKTREE" "PORTS(dev/ib-live/ib-paper)"
printf '%-22s %-26s %-34s %s\n' "-------" "------" "--------" "---------------------------"

i=0
for id in "${SELECTED[@]}"; do
  branch="agent/${id}"
  wt_dir="${PARENT_DIR}/alphalabs-wt-${id}"
  dev_port=$((3000 + i * 10))
  ib_live=$((4001 + i * 10))
  ib_paper=$((4002 + i * 10))
  printf '%-22s %-26s %-34s %s\n' "$id" "$branch" "alphalabs-wt-${id}" "${dev_port}/${ib_live}/${ib_paper}"

  if [[ "$DRY_RUN" -eq 0 ]]; then
    git -C "$ROOT_DIR" worktree add -b "$branch" "$wt_dir" "$BASE_REF" >/dev/null
  fi

  ref_cmd="cd '${wt_dir}' && ATP_FEATURE_ID='${id}' ATP_DEV_PORT=${dev_port} ATP_IB_LIVE_PORT=${ib_live} ATP_IB_PAPER_PORT=${ib_paper} <your-agent-runner>"

  if [[ -n "$LAUNCH_TMPL" && "$DRY_RUN" -eq 0 ]]; then
    echo "  launching agent for ${id}..."
    (
      cd "$wt_dir"
      export ATP_FEATURE_ID="$id" ATP_DEV_PORT="$dev_port" \
             ATP_IB_LIVE_PORT="$ib_live" ATP_IB_PAPER_PORT="$ib_paper"
      eval "$LAUNCH_TMPL"
    ) &
  else
    echo "  run: ${ref_cmd}"
  fi
  i=$((i + 1))
done

if [[ -n "$LAUNCH_TMPL" && "$DRY_RUN" -eq 0 ]]; then
  echo
  echo "→ ${#SELECTED[@]} agent(s) launched in background. Waiting..."
  wait
fi

echo
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "✓ dry-run: would prepare ${#SELECTED[@]} worktree(s). Re-run without --dry-run to create them."
else
  echo "✓ ${#SELECTED[@]} worktree(s) prepared. Inspect with: git worktree list"
  echo "  Tear down after merge with: tools/cleanup_agents.sh"
fi
