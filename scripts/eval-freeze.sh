#!/usr/bin/env bash
# Tag the agent code eval-freeze (D39, D65): refuses unless dev is clean and in step with
# origin, the tag does not exist, the Diagnose freeze holds and the full test suite passes.
# Creates the tag locally and prints the push command; it never pushes.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
export PYTHONPATH=agents
"$PY" scenarios/eval_freeze.py check
"$PY" scenarios/eval_freeze.py lanes
echo "running the full test suite..."
if ! "$PY" -m pytest -o addopts="" -q; then
  echo "refused: the test suite failed" >&2
  exit 1
fi
git tag -a eval-freeze -m "eval-freeze: agent code frozen for Step 11" HEAD
echo "tagged eval-freeze at $(git rev-parse --short HEAD) (local only)"
echo "push it with: git push origin refs/tags/eval-freeze"
