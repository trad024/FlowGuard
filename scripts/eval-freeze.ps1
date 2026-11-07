# Tag the agent code eval-freeze (D39, D65): refuses unless dev is clean and in step with
# origin, the tag does not exist, the Diagnose freeze holds and the full test suite passes.
# Creates the tag locally and prints the push command; it never pushes.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$py = if ($env:PYTHON) { $env:PYTHON } else { "py" }
$env:PYTHONPATH = "agents"
& $py scenarios/eval_freeze.py check
if ($LASTEXITCODE -ne 0) { exit 1 }
& $py scenarios/eval_freeze.py lanes
Write-Output "running the full test suite..."
& $py -m pytest -o addopts="" -q
if ($LASTEXITCODE -ne 0) { Write-Error "refused: the test suite failed"; exit 1 }
git tag -a eval-freeze -m "eval-freeze: agent code frozen for Step 11" HEAD
if ($LASTEXITCODE -ne 0) { exit 1 }
Write-Output "tagged eval-freeze at $(git rev-parse --short HEAD) (local only)"
Write-Output "push it with: git push origin refs/tags/eval-freeze"
