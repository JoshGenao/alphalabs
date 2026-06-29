#!/usr/bin/env bash
#
# claim_and_work.sh — open an interactive coding agent on a self-claimed feature.
#
# Run this in each terminal you want an agent in. It atomically claims the best
# ready, unclaimed, dependency-satisfied feature (tools/agent_pool.py claim),
# creates that feature's worktree + branch + private ports, then opens an
# INTERACTIVE Claude session inside the worktree seeded with the coding prompt —
# so you can watch the agent work and intervene. Open N terminals → N agents on
# N different features, no file/branch/port collisions.
#
# Usage:
#   tools/claim_and_work.sh
#
# Nothing to claim? It prints the board and exits. Tear down finished worktrees
# with tools/cleanup_agents.sh after their features integrate (passes:true).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

command -v claude >/dev/null 2>&1 || { echo "✗ 'claude' not on PATH" >&2; exit 1; }

# Claim under the pool lock; `claim` prints shell-assignable FEATURE=/WORKTREE=/ports.
claim_out="$(python3 tools/agent_pool.py claim)"
eval "$claim_out"

if [[ "${FEATURE:-EMPTY}" == "EMPTY" ]]; then
  echo "No ready feature to claim right now. Current board:"
  python3 tools/agent_pool.py status --no-fetch
  exit 0
fi

echo "→ Claimed ${FEATURE}"
echo "  worktree: ${WORKTREE}"
echo "  branch:   ${BRANCH}"
echo "  ports:    dev=${ATP_DEV_PORT}  ib-live=${ATP_IB_LIVE_PORT}  ib-paper=${ATP_IB_PAPER_PORT}"
echo

cd "$WORKTREE"
export ATP_FEATURE_ID="$FEATURE" ATP_DEV_PORT ATP_IB_LIVE_PORT ATP_IB_PAPER_PORT

# Interactive session (not -p/headless) so you can watch + intervene.
exec claude "$(cat "${WORKTREE}/prompts/coding_prompt.md")"
