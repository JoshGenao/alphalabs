#!/usr/bin/env bash
#
# work_on.sh — open an interactive coding agent on an OPERATOR-SELECTED feature.
#
# The operator-select twin of tools/claim_and_work.sh. Where claim_and_work.sh
# auto-picks the best ready feature (and deliberately skips the ones that are
# code-done but awaiting human e2e verification), this takes the feature id from
# you. That is the closing path: those awaiting-verification features are exactly
# the ones a human drives to green, and the anti-churn guard must not block it.
#
# Everything else is identical to claim_and_work.sh — same fcntl pool lock, same
# lease + private port-slot allocation, same worktree/branch materialisation, and
# the session still starts in PLAN MODE (read-only) so the agent must present a
# plan and get your approval before it can edit anything. Open N terminals → N
# agents on N different features, no file/branch/port collisions.
#
# Usage:
#   tools/work_on.sh <FEATURE_ID> [--branch <BRANCH>] [--reclaim]
#
# Examples:
#   tools/work_on.sh SRS-LOG-001
#   tools/work_on.sh SRS-MD-003 --branch agent/SRS-MD-003-stream
#
# --branch pins the session to an existing branch instead of agent/<FEATURE_ID>.
# Use it when the work already lives somewhere else — e.g. SRS-MD-003's live feed
# loop sits on agent/SRS-MD-003-stream, while agent/SRS-MD-003 is the older,
# already-integrated branch. Without it you would open a session on the wrong tree.
#
# See the board with: python3 tools/agent_pool.py status
# Tear down finished worktrees with tools/cleanup_agents.sh after they integrate.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -lt 1 || "$1" == -* ]]; then
  echo "usage: tools/work_on.sh <FEATURE_ID> [--branch <BRANCH>] [--reclaim]" >&2
  echo >&2
  python3 tools/agent_pool.py status --no-fetch >&2
  exit 2
fi

FEATURE_ID="$1"; shift

command -v claude >/dev/null 2>&1 || { echo "✗ 'claude' not on PATH" >&2; exit 1; }

# Owner = this shell's PID. `exec claude` below preserves the PID, so the lease's
# owner stays the live session; in-session re-claims inherit it. The scheduler
# uses this to never reclaim a feature whose process is still alive, which
# prevents two terminals landing on the same feature.
export ATP_AGENT_OWNER="$(hostname):$$"

# Claim under the pool lock; `claim` prints shell-assignable FEATURE=/WORKTREE=/ports.
claim_out="$(python3 tools/agent_pool.py claim --id "$FEATURE_ID" "$@")"
eval "$claim_out"

if [[ "${FEATURE:-EMPTY}" == "EMPTY" ]]; then
  echo "✗ could not claim ${FEATURE_ID}." >&2
  exit 1
fi

echo "→ Claimed ${FEATURE} (operator-selected)"
echo "  worktree: ${WORKTREE}"
echo "  branch:   ${BRANCH}"
echo "  ports:    dev=${ATP_DEV_PORT}  ib-live=${ATP_IB_LIVE_PORT}  ib-paper=${ATP_IB_PAPER_PORT}"
echo

cd "$WORKTREE"
export ATP_FEATURE_ID="$FEATURE" ATP_DEV_PORT ATP_IB_LIVE_PORT ATP_IB_PAPER_PORT

# Interactive session (not -p/headless) so you can watch + intervene.
# --permission-mode plan opens READ-ONLY: the agent must present a plan and get
# your approval (Step 4.6 of the coding prompt) before it can touch any file.
exec claude --permission-mode plan "$(cat "${WORKTREE}/prompts/coding_prompt.md")"
