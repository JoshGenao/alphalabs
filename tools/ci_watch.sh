#!/usr/bin/env bash
# Watch the CI workflows for a pushed commit, and say plainly whether they went green.
#
# The pre-push hook runs a SUBSET — it cannot know what CI will do. This is how you
# find out. "I pushed and moved on" is how main stayed red for three days across
# `ci`, `security` and `integration` simultaneously, each for a different reason,
# none of which any local run would have surfaced: a link check that resolved
# against a file only developer machines have, a Rust job with no Python
# interpreter, a CVE whose fix its own version ceiling excluded, and a Compose
# project name that was invalid only on unlucky random draws.
#
#   tools/ci_watch.sh              # watch HEAD's runs to completion
#   tools/ci_watch.sh <sha>        # a specific commit
#   tools/ci_watch.sh --no-wait    # report current state and exit
#
# Exit 0 only when every workflow for that commit concluded successfully. A run
# still in progress is NOT success (rule 3: unknown is never a pass), so a
# --no-wait call on an in-flight commit exits non-zero by design.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

WAIT=1
SHA=""
for arg in "$@"; do
  case "$arg" in
    --no-wait) WAIT=0 ;;
    -*) printf 'ci_watch: unknown argument %s\n' "$arg" >&2; exit 2 ;;
    *) SHA="$arg" ;;
  esac
done
[ -n "$SHA" ] || SHA="$(git rev-parse HEAD)"

command -v gh >/dev/null 2>&1 || {
  echo "✗ ci_watch: the gh CLI is not installed — cannot read workflow results." >&2
  echo "  Without it there is no way to know whether CI passed; do not assume it did." >&2
  exit 2
}

REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
[ -n "$REPO" ] || { echo "✗ ci_watch: could not resolve the GitHub repo." >&2; exit 2; }

printf '→ watching %s for %s\n' "$REPO" "${SHA:0:8}"

# Give GitHub a moment to register runs for a just-pushed commit; a commit with no
# runs YET is not a commit with no runs.
for _ in $(seq 1 10); do
  COUNT="$(gh run list --repo "$REPO" --commit "$SHA" --json databaseId --jq 'length' 2>/dev/null || echo 0)"
  [ "${COUNT:-0}" -gt 0 ] && break
  sleep 6
done

if [ "${COUNT:-0}" -eq 0 ]; then
  echo "✗ no workflow runs found for ${SHA:0:8} after 60s." >&2
  echo "  Either the push has not landed or no workflow matches it. Not a pass." >&2
  exit 1
fi

if [ "$WAIT" = "1" ]; then
  # `gh run watch` follows ONE run; loop until every run for this commit concludes.
  while :; do
    RUNNING="$(gh run list --repo "$REPO" --commit "$SHA" --json status \
                 --jq '[.[] | select(.status != "completed")] | length')"
    [ "${RUNNING:-0}" -eq 0 ] && break
    printf '  %s run(s) still going…\n' "$RUNNING"
    sleep 20
  done
fi

echo
# GitHub returns conclusion as "" (not null) while a run is in flight, so jq's `//`
# does NOT fall through to .status — that rendered every in-progress run as a blank
# status column. Choose explicitly.
gh run list --repo "$REPO" --commit "$SHA" \
  --json workflowName,status,conclusion \
  --jq '.[] | "  \(if (.conclusion // "") == "" then .status else .conclusion end)\t\(.workflowName)"'

RUNNING="$(gh run list --repo "$REPO" --commit "$SHA" --json status \
            --jq '[.[] | select(.status != "completed")] | length')"
FAILED="$(gh run list --repo "$REPO" --commit "$SHA" --json status,conclusion \
            --jq '[.[] | select(.status == "completed" and .conclusion != "success" and .conclusion != "skipped")] | length')"

if [ "${RUNNING:-0}" -eq 0 ] && [ "${FAILED:-1}" -eq 0 ]; then
  echo
  echo "✓ every workflow for ${SHA:0:8} concluded successfully."
  exit 0
fi

# Still running and actually failed are different facts, and calling the first one
# "did not succeed" would train the reader to ignore the message that matters.
if [ "${FAILED:-0}" -gt 0 ]; then
  cat >&2 <<EOF

✗ ${FAILED} workflow(s) for ${SHA:0:8} FAILED.
  This is now the highest-priority work: a red main is the state in which nobody
  else's green means anything. Read the failure, do not re-run it hoping — a
  CI-only failure is usually something the runner does not have:
    gh run list --repo $REPO --commit $SHA
    gh run view <run-id> --repo $REPO --log-failed
EOF
fi
if [ "${RUNNING:-0}" -gt 0 ]; then
  cat >&2 <<EOF

· ${RUNNING} workflow(s) for ${SHA:0:8} are STILL RUNNING — not a pass yet
  (rule 3: unknown is never success). Re-run without --no-wait to wait for them.
EOF
fi
exit 1
