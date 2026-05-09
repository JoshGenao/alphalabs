#!/usr/bin/env bash
# Idempotent installer for the critic pre-commit hook.
#
# Hooks live in .git/hooks/ which is not tracked, so every fresh clone needs
# this script run once. init.sh calls it on every session (idempotent).
#
# Bypass with: ATP_CRITIC_BYPASS=1 git commit ...
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

if [[ -f "${HOOK_PATH}" ]]; then
  EXISTING_SUM="$(shasum -a 256 "${HOOK_PATH}" | awk '{print $1}')"
  NEW_SUM="$(printf '%s\n' "${HOOK_BODY}" | shasum -a 256 | awk '{print $1}')"
  if [[ "${EXISTING_SUM}" == "${NEW_SUM}" ]]; then
    echo "install_hooks: pre-commit hook already up to date."
    exit 0
  fi
fi

printf '%s\n' "${HOOK_BODY}" > "${HOOK_PATH}"
chmod +x "${HOOK_PATH}"
echo "install_hooks: pre-commit hook installed at ${HOOK_PATH}"
