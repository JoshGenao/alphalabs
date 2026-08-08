#!/usr/bin/env bash
# The one contract-check runner. Reads tools/gates.json — nothing else decides
# what runs.
#
# Before this existed, init.sh, .github/workflows/ci.yml and tools/run_ci_locally.sh
# each carried a hand-maintained list (62 / 32 / 25 checks), so "did my work pass?"
# had three different answers and 21 checks ran nowhere but a bootstrap script.
#
#   tools/verify_contracts.sh --scope env    # init.sh: the fast coherence subset
#   tools/verify_contracts.sh --scope ci     # ci.yml + run_ci_locally.sh: everything
#   tools/verify_contracts.sh --scope ci --list
#   tools/verify_contracts.sh --scope ci --keep-going
#
# Exit 0 only if every check in the scope ran and passed. A check that cannot be
# executed is a failure, never a skip (CLAUDE.md rule 3: unknown is not empty).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

SCOPE=""
LIST=0
KEEP_GOING=0

die() { echo "✗ verify_contracts: $*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scope)      SCOPE="${2:-}"; shift 2 ;;
    --scope=*)    SCOPE="${1#*=}"; shift ;;
    --list)       LIST=1; shift ;;
    --keep-going) KEEP_GOING=1; shift ;;
    -h|--help)    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            die "unknown option: $1" ;;
  esac
done

[[ -n "${SCOPE}" ]] || die "--scope is required (env | ci | all)"

PY="${ATP_PYTHON:-python3}"
command -v "${PY}" >/dev/null 2>&1 || die "${PY} not found; cannot read the gate registry"

# gates_registry_check.py is the ONLY scope resolver, so the argv rules live in one
# place. `all` used to have its own inline resolver here that took the first scope's
# argv — which for every cargo-strict check is `"ci": []`, silently dropping
# --require-cargo and restoring the "cargo absent -> skipped, still passes" hole the
# per-scope argv exists to close. A second implementation of the registry's contract
# is a second place for it to be wrong; `all` now just runs each real scope in turn.
if [[ "${SCOPE}" == "all" ]]; then
  # Forward the caller's own flags. `${VAR:+...}` expands whenever VAR is non-empty,
  # which "0" is — so build the list from the actual values, not from emptiness.
  passthru=()
  [[ "${KEEP_GOING}" -eq 1 ]] && passthru+=(--keep-going)
  [[ "${LIST}" -eq 1 ]] && passthru+=(--list)
  rc=0
  for s in $("${PY}" tools/gates_registry_check.py --list-scopes); do
    "${BASH_SOURCE[0]}" --scope "${s}" ${passthru[@]+"${passthru[@]}"} || rc=1
  done
  exit "${rc}"
fi

NAMES="$("${PY}" tools/gates_registry_check.py --scope "${SCOPE}")" \
  || die "could not resolve scope '${SCOPE}' from tools/gates.json"

[[ -n "${NAMES}" ]] || die "scope '${SCOPE}' resolved to zero checks"

TOTAL=$(printf '%s\n' "${NAMES}" | grep -c .)

if [[ "${LIST}" -eq 1 ]]; then
  printf '%s scope — %d check(s):\n' "${SCOPE}" "${TOTAL}"
  printf '%s\n' "${NAMES}" | sed 's/^/  · /'
  exit 0
fi

printf '→ contract checks (scope=%s, %d check(s))\n' "${SCOPE}" "${TOTAL}"

FAILED=""
FAIL_COUNT=0
RAN=0

while IFS= read -r line; do
  [[ -n "${line}" ]] || continue
  # "name --flag …" — the registry forbids whitespace inside an arg, so plain
  # word-splitting here is safe and is validated by gates_registry_check.py.
  # shellcheck disable=SC2206
  parts=(${line})
  check="${parts[0]}"
  check_args=("${parts[@]:1}")
  script="tools/${check}.py"
  if [[ ! -f "${script}" ]]; then
    # gates_registry_check.py exists to stop this reaching here; if it does, it is
    # a hard failure and never a silent skip.
    echo "✗ ${script} is registered but missing — run tools/gates_registry_check.py" >&2
    FAILED="${FAILED}    · ${check} (missing file)
"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    [[ "${KEEP_GOING}" -eq 1 ]] || exit 1
    continue
  fi

  printf '  · %s\n' "${line}"
  RAN=$((RAN + 1))
  # ${arr[@]+"${arr[@]}"} — the portable expansion for a possibly-empty array;
  # a bare "${arr[@]}" is an unbound-variable error under `set -u` in bash 3.2.
  if ! "${PY}" "${script}" ${check_args[@]+"${check_args[@]}"} >/dev/null; then
    echo "✗ ${check} failed; run '${PY} ${script} ${check_args[@]+${check_args[@]}}' for detail." >&2
    FAILED="${FAILED}    · ${line}
"
    FAIL_COUNT=$((FAIL_COUNT + 1))
    [[ "${KEEP_GOING}" -eq 1 ]] || exit 1
  fi
done <<EOF
${NAMES}
EOF

if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  printf '\n✗ %d of %d contract check(s) failed:\n' "${FAIL_COUNT}" "${TOTAL}"
  printf '%s' "${FAILED}"
  exit 1
fi

printf '✓ %d/%d contract check(s) passed (scope=%s)\n' "${RAN}" "${TOTAL}" "${SCOPE}"
