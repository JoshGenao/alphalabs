#!/usr/bin/env bash
#
# codex_review.sh — autonomous adversarial review (the judgment critic pass).
#
# `/codex:adversarial-review` is flagged `disable-model-invocation: true`, so an
# agent cannot self-trigger the slash command. Under the hood it is just a call
# to the codex companion, which an agent CAN run in Bash. This script is that
# call, with the repo's judgment criteria (prompts/critic_prompt.md) as focus.
#
# Run it from inside your worktree so it reviews that branch's diff vs main.
#
# Usage:
#   tools/codex_review.sh [BASE_REF]          # default BASE_REF = origin/main
#
# Emits the same JSON verdict schema as tools/critic_check.py. If Codex is not
# installed/ready it prints a JSON error (run /codex:setup) and exits 0 so the
# caller can fall back to a manual fresh-context review.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_REF="${1:-origin/main}"
PROMPT_FILE="${ROOT_DIR}/prompts/critic_prompt.md"

[[ -f "$PROMPT_FILE" ]] || { echo "✗ missing $PROMPT_FILE" >&2; exit 1; }
command -v node >/dev/null 2>&1 || { echo '{"verdict":"error","reason":"node not on PATH"}'; exit 0; }

# Locate the codex companion: plugin env first, then the installed cache.
companion="${CLAUDE_PLUGIN_ROOT:-}/scripts/codex-companion.mjs"
if [[ ! -f "$companion" ]]; then
  companion="$(ls -t "$HOME"/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs 2>/dev/null | head -1 || true)"
fi
if [[ -z "$companion" || ! -f "$companion" ]]; then
  echo '{"verdict":"error","reason":"codex-companion.mjs not found; run /codex:setup or use the manual fallback in prompts/critic_prompt.md"}'
  exit 0
fi

# --wait → foreground JSON verdict; --base → branch review vs the integrated main.
exec node "$companion" adversarial-review --wait --base "$BASE_REF" "$(cat "$PROMPT_FILE")"
