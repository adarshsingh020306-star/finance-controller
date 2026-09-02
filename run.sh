#!/usr/bin/env bash
# One command, fresh clone to measured results.
#   ./run.sh              generate data, reconcile, report (LLM tier if a key is set)
#   ./run.sh --no-llm     deterministic tiers only
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

echo "==> Python: $($PY --version)"
$PY - <<'CHECK'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"needs Python 3.11+, found {sys.version.split()[0]}")
CHECK

if [ ! -d .venv ]; then
  echo "==> Creating .venv"
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; else . .venv/Scripts/activate; fi

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
