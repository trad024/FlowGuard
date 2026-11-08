# Tag the held-out Diagnose harness harness-diagnose-freeze (D69 stage A), right before the
# first held-out Diagnose call: refuses unless dev is clean and in step with origin, the tag
# does not exist, the Diagnose freeze holds and the full test suite passes.
# Creates the tag locally and prints the push command; it never pushes.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = if ($env:PYTHON) { $env:PYTHON } else { "py" }
$env:PYTHONPATH = "agents"
& $py scenarios/eval_freeze.py check --tag harness-diagnose-freeze
if ($LASTEXITCODE -ne 0) { exit 1 }
& $py scenarios/eval_freeze.py lanes
# R1: hash every held-out Diagnose input and commit the list with the tag
& $py scenarios/heldout.py freeze-harness-inputs
if ($LASTEXITCODE -ne 0) { exit 1 }
git add scenarios/harness-diagnose-inputs.sha256
git commit -q -m "harness-diagnose-freeze: held-out Diagnose input hashes"
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Output "running the full test suite..."
& $py -m pytest -o addopts="" -q
if ($LASTEXITCODE -ne 0) { Write-Error "refused: the test suite failed"; exit 1 }
git tag -a harness-diagnose-freeze -m "harness-diagnose-freeze: held-out Diagnose harness frozen (D69)" HEAD
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Output "tagged harness-diagnose-freeze at $(git rev-parse --short HEAD) (local only)"
Write-Output "push them with: git push origin dev; git push origin refs/tags/harness-diagnose-freeze"
