#!/usr/bin/env bash
# Tag the held-out Diagnose harness harness-diagnose-freeze (D69 stage A), right before the
# first held-out Diagnose call: refuses unless dev is clean and in step with origin, the tag
# does not exist, the Diagnose freeze holds and the full test suite passes.
# Creates the tag locally and prints the push command; it never pushes.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-python}"
export PYTHONPATH=agents
"$PY" scenarios/eval_freeze.py check --tag harness-diagnose-freeze
"$PY" scenarios/eval_freeze.py lanes
# R1: hash every held-out Diagnose input and commit the list with the tag
"$PY" scenarios/heldout.py freeze-harness-inputs
git add scenarios/harness-diagnose-inputs.sha256
git commit -q -m "harness-diagnose-freeze: held-out Diagnose input hashes"
echo "running the full test suite..."
if ! "$PY" -m pytest -o addopts="" -q; then
  echo "refused: the test suite failed" >&2
  exit 1
fi
git tag -a harness-diagnose-freeze -m "harness-diagnose-freeze: held-out Diagnose harness frozen (D69)" HEAD
echo "tagged harness-diagnose-freeze at $(git rev-parse --short HEAD) (local only)"
echo "push them with: git push origin dev; git push origin refs/tags/harness-diagnose-freeze"
