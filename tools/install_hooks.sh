#!/usr/bin/env bash
# Idempotent installer for the critic pre-commit hook AND the pre-push CI gate.
#
# Hooks live in .git/hooks/ which is not tracked, so every fresh clone needs
# this script run once. init.sh calls it on every session (idempotent).
#
# Bypass with: ATP_CRITIC_BYPASS=1 git commit ...
#              ATP_PREPUSH_BYPASS=1 git push ...
# Do NOT use --no-verify (forbidden by harness rules).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOKS_DIR="${ROOT_DIR}/.git/hooks"
HOOK_PATH="${HOOKS_DIR}/pre-commit"

if [[ ! -d "${ROOT_DIR}/.git" ]]; then
  echo "install_hooks: not a git repo (${ROOT_DIR}/.git missing); skipping."
  exit 0
fi

mkdir -p "${HOOKS_DIR}"

read -r -d '' HOOK_BODY <<'HOOK' || true
#!/usr/bin/env bash
# Auto-installed by tools/install_hooks.sh — do not edit by hand.
# Bypass: ATP_CRITIC_BYPASS=1 git commit ...

if [[ "${ATP_CRITIC_BYPASS:-0}" == "1" ]]; then
  echo "critic: bypassed via ATP_CRITIC_BYPASS=1 (visible in shell history)" >&2
  exit 0
fi

ROOT="$(git rev-parse --show-toplevel)"
exec python3 "${ROOT}/tools/critic_check.py" --staged --format text
HOOK

# `exit 0` here would be a bug, not a shortcut: an up-to-date pre-commit hook must
# not stop the pre-push hook below from being installed. That is exactly how this
# script silently installed nothing on every machine that already had the critic
# hook — which is every machine that has ever run init.sh.
PRECOMMIT_CURRENT=0
if [[ -f "${HOOK_PATH}" ]]; then
  EXISTING_SUM="$(shasum -a 256 "${HOOK_PATH}" | awk '{print $1}')"
  NEW_SUM="$(printf '%s\n' "${HOOK_BODY}" | shasum -a 256 | awk '{print $1}')"
  if [[ "${EXISTING_SUM}" == "${NEW_SUM}" ]]; then
    echo "install_hooks: pre-commit hook already up to date."
    PRECOMMIT_CURRENT=1
  fi
fi

if [[ "${PRECOMMIT_CURRENT}" == "0" ]]; then
  printf '%s\n' "${HOOK_BODY}" > "${HOOK_PATH}"
  chmod +x "${HOOK_PATH}"
  echo "install_hooks: pre-commit hook installed at ${HOOK_PATH}"
fi

# ---------------------------------------------------------------------------
# pre-push — run the CI mirror's fast subset before anything reaches GitHub.
#
# On 2026-08-14 `ci`, `security` and `integration` were red on main SIMULTANEOUSLY,
# each for a different reason, and every one of them had been pushed by someone who
# had run their own tests and reasonably believed they were done. The mirror
# (`run_ci_locally.sh`) already existed and already said "run this before pushing";
# what did not exist was anything that made it happen.
#
# --fast on purpose: the full mirror takes over eight minutes, and an eight-minute
# push is one people learn to pass with --no-verify. This runs the cheap high-yield
# half (~1 min) and tells you to watch the rest.
# ---------------------------------------------------------------------------
PUSH_HOOK_PATH="${HOOKS_DIR}/pre-push"

read -r -d '' PUSH_HOOK_BODY <<'PUSHHOOK' || true
#!/usr/bin/env bash
# Auto-installed by tools/install_hooks.sh — do not edit by hand.
# Bypass: ATP_PREPUSH_BYPASS=1 git push ...

if [[ "${ATP_PREPUSH_BYPASS:-0}" == "1" ]]; then
  echo "pre-push: bypassed via ATP_PREPUSH_BYPASS=1 (visible in shell history)" >&2
  exit 0
fi

ROOT="$(git rev-parse --show-toplevel)"
echo "pre-push: running the CI mirror's fast subset (ATP_PREPUSH_BYPASS=1 to skip)…" >&2
if ! "${ROOT}/tools/run_ci_locally.sh" --fast; then
  cat >&2 <<'MSG'

✗ pre-push REFUSED: the CI mirror's fast subset failed.
  Push blocked because these same checks run in CI and would turn main red.
  Fix them, or re-run with ATP_PREPUSH_BYPASS=1 if you have a reason worth
  recording in the session note.
MSG
  exit 1
fi

echo "pre-push: fast subset passed. The Rust suite and ci-rust scope were NOT run —" >&2
echo "          confirm the real answer after pushing:  tools/ci_watch.sh" >&2
PUSHHOOK

if [[ -f "${PUSH_HOOK_PATH}" ]]; then
  EXISTING_PUSH_SUM="$(shasum -a 256 "${PUSH_HOOK_PATH}" | awk '{print $1}')"
  NEW_PUSH_SUM="$(printf '%s\n' "${PUSH_HOOK_BODY}" | shasum -a 256 | awk '{print $1}')"
  if [[ "${EXISTING_PUSH_SUM}" == "${NEW_PUSH_SUM}" ]]; then
    echo "install_hooks: pre-push hook already up to date."
    exit 0
  fi
  :
fi

printf '%s\n' "${PUSH_HOOK_BODY}" > "${PUSH_HOOK_PATH}"
chmod +x "${PUSH_HOOK_PATH}"
echo "install_hooks: pre-push hook installed at ${PUSH_HOOK_PATH}"
