#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — run this before pushing.
#
# The CI workflow shells out to this script (or runs the same step list)
# so local and remote results cannot diverge silently.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

# --fast keeps only the SUB-SECOND checks. Chosen by measuring the mirror, and the
# measurement killed the original design: `ruff check`, `ruff format`, `cargo fmt`,
# gates_registry_check and docs_link_check are each under a second, but every other
# step is minutes — `cargo clippy --workspace`, `cargo test --workspace`, both
# contract scopes, and the pytest run too (5,093 tests, ~7 min; the ~35 s figure was
# tests/unit alone).
#
# So there is no fast subset that meaningfully PREDICTS CI, and pretending otherwise
# would be the more comfortable lie. What --fast is for is narrower and still worth
# having: the cheap structural checks that cause a red main out of proportion to
# their cost. On 2026-08-14 the `ci` workflow went red on docs_link_check — a
# sub-second check, invisible to any test run, failing on a reference that resolved
# only where somebody had run tools/install_hooks.sh.
#
# A gate that takes eight minutes is a gate people learn to pass with --no-verify,
# and a bypassed gate catches nothing at all. This one takes about two seconds.
# The real answer arrives after the push: tools/ci_watch.sh.
#
# It is NOT a passing mirror, and does not pretend to be. The dropped steps go
# through the SAME skip ledger as a missing toolchain, so the run still reports
# itself incomplete — `--fast` merely says the incompleteness was chosen. The full
# answer is CI; `tools/ci_watch.sh` is how you get it after the push.
# Use the project venv when there is one. The mirror shells `python3`, `ruff` and
# `pytest` by name, so without this it runs against whatever the shell happens to
# have: on a machine where the venv is not activated, `python3` has no numpy and
# architecture_check dies importing atp_strategy — the identical coupling that had
# the Rust CI job reporting "No module named 'numpy'" as a broken contract.
#
# It matters more now that a pre-push hook calls this: a gate that fails on every
# clean tree is a gate everyone learns to bypass, and then it catches nothing.
if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PATH="${ROOT_DIR}/.venv/bin:${PATH}"
  export PATH
fi

FAST=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    *) printf 'run_ci_locally.sh: unknown argument %s\n' "$arg" >&2; exit 2 ;;
  esac
done

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
  if [[ "${FAST}" == "1" ]]; then
    skip "cargo clippy --workspace" "--fast: minutes on a cold cache; CI runs it"
  else
    step "cargo clippy --workspace -- -D warnings"
    cargo clippy --workspace -- -D warnings
  fi
else
  skip "cargo gates" "cargo not installed"
fi

# 4 — Python tests (L1+L2+L3+L4+L7; integration & e2e are gated)
if [[ "${FAST}" == "1" ]]; then
  skip "pytest" "--fast: 5,093 tests, ~7 min; CI runs it"
elif command -v pytest >/dev/null 2>&1; then
  step "pytest -m \"not integration and not e2e\""
  pytest -m "not integration and not e2e"
else
  skip "pytest" "not installed"
fi

# 5 — Rust tests. The `else skip` is not decoration: without it a missing cargo made
# the entire Rust test suite vanish from the run without entering the skip ledger,
# so the mirror could report a clean pass having compiled and tested no Rust at all.
if [[ "${FAST}" == "1" ]]; then
  skip "cargo test --workspace" "--fast: too slow for a pre-push gate; CI runs it"
elif command -v cargo >/dev/null 2>&1; then
  step "cargo test --workspace"
  cargo test --workspace
else
  skip "cargo test --workspace" "cargo not installed"
fi

# 6 — Critic against the PR diff (vs origin/main fallback to HEAD~1)
if [[ "${FAST}" == "1" ]]; then
  skip "critic_check.py --range" "--fast: reviews the whole diff; run it at Step 6.1"
else
step "critic_check.py --range"
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  python3 tools/critic_check.py --range origin/main..HEAD --format text
else
  python3 tools/critic_check.py --range HEAD~1..HEAD --format text
fi
fi

# 6b — the registry's own validator. ci.yml runs this as a BLOCKING step before the
# contract checks; omitting it here left a gates.json edit passing the "mirror" and
# failing CI — the step-list divergence this repo's own playbook rule 27 records.
step "gates_registry_check.py"
python3 tools/gates_registry_check.py

# Sub-second, and it is what turned `ci` red on 2026-08-14 for a reference that
# resolved only on machines where somebody had run tools/install_hooks.sh. Exactly
# the shape a pre-push gate should catch: cheap, and invisible to a normal test run.
step "docs_link_check.py"
python3 tools/docs_link_check.py

# 7 — contract checks, from tools/gates.json (the one registry ci.yml also reads)
if [[ "${FAST}" == "1" ]]; then
  skip "contract checks (scope=ci)" "--fast: >7 min; CI runs it"
else
  step "contract checks (scope=ci)"
  tools/verify_contracts.sh --scope ci
fi

# 7b — the cargo-strict variants. --require-cargo turns "cargo not on PATH ->
# skipped, still passes" into a failure; ci.yml runs this scope in its Rust job.
if [[ "${FAST}" == "1" ]]; then
  skip "contract checks (ci-rust)" "--fast: too slow for a pre-push gate; CI runs it"
elif command -v cargo >/dev/null 2>&1; then
  step "contract checks (scope=ci-rust, --require-cargo)"
  tools/verify_contracts.sh --scope ci-rust
else
  skip "contract checks (ci-rust)" "cargo not installed"
fi

if [[ "${FAST}" == "1" ]]; then
  # The skips are the ones --fast chose, so they do not fail the run — but the
  # ledger is still printed, and the run is still not a mirror pass.
  ATP_ALLOW_SKIP=1 report_skips || true
  printf '\n→ --fast ran the sub-second checks ONLY. The test suites, clippy and the\n'
  printf '  contract scopes were NOT run — this is not a green CI and cannot predict\n'
  printf '  one. Get the real answer after pushing:\n'
  printf '    tools/ci_watch.sh\n'
else
  report_skips || exit 1
fi

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
