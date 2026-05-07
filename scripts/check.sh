#!/usr/bin/env bash
# Run every quality gate in one shot. Exits non-zero on any failure.
#
# Usage:
#   bash scripts/check.sh
#
# Equivalent to running each phase manually:
#   cd react-app && npm run typecheck && npm run test && npm run build
#   cd .. && python3 -m pytest tests/ -q --no-header --ignore=tests/manual
#
# Reports all four gates' results at the end so a single failure doesn't
# hide later ones. Compatible with macOS bash 3.2 (no associative arrays).

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 99

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# Parallel arrays — bash-3 friendly.
LABELS=()
RESULTS=()

record() {
  LABELS+=("$1")
  RESULTS+=("$2")
}

run_gate() {
  local label="$1"; shift
  bold "→ $label"
  if "$@"; then
    record "$label" "PASS"
    green "  ✓ $label passed"
  else
    record "$label" "FAIL"
    red "  ✗ $label failed"
  fi
  echo
}

run_gate "typecheck"   bash -c "cd '$ROOT/react-app' && npm run typecheck"
run_gate "vitest"      bash -c "cd '$ROOT/react-app' && npm run test"
run_gate "vite build"  bash -c "cd '$ROOT/react-app' && npm run build"
run_gate "pytest"      bash -c "cd '$ROOT' && python3 -m pytest tests/ -q --no-header --ignore=tests/manual"

bold "================================================================"
bold "Gate summary"
bold "================================================================"
exit_code=0
for i in "${!LABELS[@]}"; do
  label="${LABELS[$i]}"
  status="${RESULTS[$i]}"
  if [ "$status" = "PASS" ]; then
    green "  ✓ $label"
  else
    red   "  ✗ $label"
    exit_code=1
  fi
done
echo
exit $exit_code
