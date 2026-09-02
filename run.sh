#!/usr/bin/env bash
# One command, fresh clone to measured results.
#   ./run.sh              generate data, reconcile, report (LLM tier if a key is set)
#   ./run.sh --no-llm     deterministic tiers only
set -euo pipefail
cd "$(dirname "$0")"

# Find a working Python 3.11+. Existence is not enough: on Windows a
# `python3` App Execution Alias stub resolves on PATH but fails when run,
# so each candidate is probed by actually executing it.
PY=""
for cand in "${PYTHON:-}" python3 python py; do
  [ -n "$cand" ] || continue
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
      >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "error: no Python 3.11+ on PATH (tried \$PYTHON, python3, python, py)" >&2
  exit 1
fi
echo "==> Python: $("$PY" --version 2>&1) ($PY)"

if [ ! -d .venv ]; then
  echo "==> Creating .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
if [ -f .venv/bin/activate ]; then
  . .venv/bin/activate
elif [ -f .venv/Scripts/activate ]; then
  . .venv/Scripts/activate
else
  echo "error: .venv exists but has no activate script; remove it and retry" >&2
  exit 1
fi

echo "==> Installing dependencies"
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"

echo "==> Phase 1: generating synthetic data with ground truth"
python -m reconciler.generate_data --out data

echo
echo "==> Tests"
python -m unittest discover -s tests -q

echo
echo "==> Phases 2 and 3: reconciling the full batch and measuring"
python -m reconciler.report --out reports/results.md "$@"

echo
echo "==> Done. Full report written to reports/results.md"
