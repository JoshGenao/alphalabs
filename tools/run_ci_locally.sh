#!/usr/bin/env bash
# Local mirror of .github/workflows/ci.yml — run this before pushing.
#
# The CI workflow shells out to this script (or runs the same step list)
# so local and remote results cannot diverge silently.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

step() { printf '\n→ %s\n' "$*"; }
ok()   { printf '✓ %s\n' "$*"; }
skip() { printf '· %s (skipped: %s)\n' "$1" "$2"; }

# 1 — lint / format
if command -v ruff >/dev/null 2>&1; then
  step "ruff check ."
  ruff check .
  step "ruff format --check ."
  ruff format --check .
else
  skip "ruff" "not installed (pip install -r requirements-dev.txt)"
fi

# 2 — typecheck
if command -v mypy >/dev/null 2>&1; then
  step "mypy python/"
  mypy python/
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

# 5 — Rust tests
if command -v cargo >/dev/null 2>&1; then
  step "cargo test --workspace"
  cargo test --workspace
fi

# 6 — Critic against the PR diff (vs origin/main fallback to HEAD~1)
step "critic_check.py --range"
if git rev-parse --verify origin/main >/dev/null 2>&1; then
  python3 tools/critic_check.py --range origin/main..HEAD --format text
else
  python3 tools/critic_check.py --range HEAD~1..HEAD --format text
fi

# 7 — existing architecture & contract checks (the legacy gates)
step "architecture / contract checks"
for check in \
    architecture_check \
    dependency_boundary_check \
    adapter_isolation_check \
    deployment_check \
    config_check \
    startup_readiness_gate_check \
    rest_api_check \
    websocket_api_check \
    cli_check \
    operator_workflow_surface_check \
    operator_interface_runtime_check \
    credential_security_check \
    log_record_check \
    log_persistence_check \
    adapter_check \
    ib_adapter_check \
    data_provider_check \
    historical_data_check \
    strategy_api_check; do
  printf '  · %s.py\n' "${check}"
  python3 "tools/${check}.py" >/dev/null
done

ok "local CI mirror complete"
