#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — run this before pushing.
#
# The CI workflow shells out to this script (or runs the same step list)
# so local and remote results cannot diverge silently.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# A gate that reports success for a step it never ran is worse than no gate: it is
# how an unformatted file reached `main` and left `ruff format --check` red. A skip
# is an UNKNOWN result, and unknown is never "pass" (CLAUDE.md rule 3). Every skip is
# recorded and turns the whole run non-zero unless the caller opts in explicitly.
#
# (A newline-delimited string, not an array: `${arr[@]}` on an empty array is an
# unbound-variable error under `set -u` in the bash 3.2 macOS ships.)
SKIPPED=""
SKIP_COUNT=0
# Steps ci.yml marks continue-on-error. Counted and reported, never silent.
ADVISORY_FAILED=0

step() { printf '\n→ %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
skip() {
  SKIPPED="${SKIPPED}    · $1 ($2)
"
  SKIP_COUNT=$((SKIP_COUNT + 1))
  printf '· %s (skipped: %s)\n' "$1" "$2"
}

report_skips() {
  [ "${SKIP_COUNT}" -eq 0 ] && return 0
  printf '\n✗ mirror INCOMPLETE — %d step(s) skipped:\n' "${SKIP_COUNT}"
  printf '%s' "${SKIPPED}"
  if [[ "${ATP_ALLOW_SKIP:-0}" == "1" ]]; then
    printf '  ATP_ALLOW_SKIP=1 — treating the run as advisory, NOT as a passing gate.\n'
    return 0
  fi
  printf '  Install the dev requirements and re-run:\n'
  printf '    source .venv/bin/activate && pip install -r requirements-dev.txt\n'
  printf '  A skipped step is an UNKNOWN result, not a pass. Set ATP_ALLOW_SKIP=1 only\n'
  printf '  when you have another way to cover it, and say so in the session note.\n'
  return 1
}

# 1 — lint / format
if command -v ruff >/dev/null 2>&1; then
  step "ruff check ."
  ruff check .
  step "ruff format --check ."
  ruff format --check .
else
  skip "ruff" "not installed (pip install -r requirements-dev.txt)"
fi

# 2 — typecheck. ADVISORY, matching ci.yml, which carries
# `continue-on-error: true  # remove once strict typing lands across python/`
# on this step. It was blocking here (`set -e`) and advisory there, so the
# "mirror" was stricter than the thing it mirrors — the same divergence class as
# the three different check lists, in a third dimension: same step, different
# blocking semantics. Reported loudly rather than hidden, so that "advisory" is a
# visible decision instead of a silent pass.
if command -v mypy >/dev/null 2>&1; then
  step "mypy python/  (advisory — matches ci.yml continue-on-error)"
  if ! mypy python/; then
    ADVISORY_FAILED=$((ADVISORY_FAILED + 1))
    printf '⚠ mypy reported errors. ci.yml does not block on this step either, so\n'
    printf '  this run is NOT failed by it — but the errors above are real and\n'
    printf '  someone chose to defer them. Do not add to them.\n'
  fi
else
  skip "mypy" "not installed"
fi

# 3 — Rust gates
if command -v cargo >/dev/null 2>&1; then
  step "cargo fmt --check"
  cargo fmt --check
  step "cargo clippy --workspace -- -D warnings"
  cargo clippy --workspace -- -D warnings
else
  skip "cargo gates" "cargo not installed"
fi

# 4 — Python tests (L1+L2+L3+L4+L7; integration & e2e are gated)
if command -v pytest >/dev/null 2>&1; then
  step "pytest -m \"not integration and not e2e\""
  pytest -m "not integration and not e2e"
else
  skip "pytest" "not installed"
fi

# 5 — Rust tests. The `else skip` is not decoration: without it a missing cargo made
# the entire Rust test suite vanish from the run without entering the skip ledger,
# so the mirror could report a clean pass having compiled and tested no Rust at all.
if command -v cargo >/dev/null 2>&1; then
  step "cargo test --workspace"
  cargo test --workspace
else
  skip "cargo test --workspace" "cargo not installed"
fi

# 6 — Critic against the PR diff (vs origin/main fallback to HEAD~1)
step "critic_check.py --range"
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  python3 tools/critic_check.py --range origin/main..HEAD --format text
else
  python3 tools/critic_check.py --range HEAD~1..HEAD --format text
fi

# 7 — contract checks, from tools/gates.json (the one registry ci.yml also reads)
step "contract checks (scope=ci)"
tools/verify_contracts.sh --scope ci

# 7b — the cargo-strict variants. --require-cargo turns "cargo not on PATH ->
# skipped, still passes" into a failure; ci.yml runs this scope in its Rust job.
if command -v cargo >/dev/null 2>&1; then
  step "contract checks (scope=ci-rust, --require-cargo)"
  tools/verify_contracts.sh --scope ci-rust
else
  skip "contract checks (ci-rust)" "cargo not installed"
fi

report_skips || exit 1

if [[ "${ADVISORY_FAILED}" -gt 0 ]]; then
  printf '\n⚠ %d advisory step(s) reported errors (not blocking, same as ci.yml).\n' "${ADVISORY_FAILED}"
fi

# "every step ran" is a claim about this run, so it may only be printed when it is
# true. ATP_ALLOW_SKIP=1 downgrades the EXIT CODE, never the description: a run that
# skipped steps and then announced it had run them all would be the exact false green
# this script was changed to stop producing.
if [[ "${SKIP_COUNT}" -gt 0 ]]; then
  printf '\n· local CI mirror finished with %d step(s) UNRUN (ATP_ALLOW_SKIP=1).\n' "${SKIP_COUNT}"
  printf '  This is NOT a passing gate. Name the unrun steps in your session note.\n'
  exit 0
fi

ok "local CI mirror complete — every step ran"
