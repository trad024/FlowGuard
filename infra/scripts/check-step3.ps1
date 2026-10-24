# FlowGuard step 3 acceptance check (about 15 to 30 minutes, mostly waiting for CI):
#   1. a commit under app/ runs the app pipeline to success and its image is pushed;
#   2. the laptop deployer picks it up and starts a staging canary with no manual step;
#   3. the canary completes under the L0 analysis (both analyses successful, image promoted);
#   4. the deployer logged the deployment, and a commit touching only docs/ starts no app
#      pipeline run.
# It makes and pushes two small marker commits (app/ci-trigger.txt, docs/ci-trigger.txt) to dev
# under your git identity, in two separate pushes: GitHub applies path filters to a whole push
# and runs the workflow once, for its last commit. It starts its own deployer unless -NoDeployer is given (use that when
# one already runs). Usage: .\infra\scripts\check-step3.ps1 [-NoDeployer] [-CiTimeoutMinutes 40]
param(
    [switch]$NoDeployer,
    [int]$CiTimeoutMinutes = 40,
    [int]$RolloutTimeoutMinutes = 25
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Namespace = "staging"
$ImageRepo = if ($env:FLOWGUARD_IMAGE) { $env:FLOWGUARD_IMAGE } else { "ghcr.io/trad024/flowguard-podinfo" }
$failed = 0
function Check($name, [scriptblock]$test) {
    try {
        if (& $test) { Write-Host "PASS  $name" } else { Write-Host "FAIL  $name"; $script:failed++ }
    } catch { Write-Host "FAIL  $name ($($_.Exception.Message))"; $script:failed++ }
}
function Rollout { kubectl -n $Namespace get rollout podinfo -o json | ConvertFrom-Json }
function WaitUntil([scriptblock]$condition, [int]$seconds, [int]$every = 10) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $condition) { return $true }
        Start-Sleep -Seconds $every
    }
    return $false
}
function Invoke-Git {
    # git writes progress and line-ending warnings to stderr; under "Stop", Windows PowerShell
    # would turn them into terminating errors whenever output is redirected
    $ErrorActionPreference = "Continue"
    git -C $Root @args 2>&1 | ForEach-Object { "$_" } | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "git $args failed" }
}

$launcher = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$python = & $launcher -c "import sys; print(sys.executable)"
$deployerPy = Join-Path $Root "ci\deployer.py"
function RunStatus($sha) { & $python $deployerPy status --sha $sha | ConvertFrom-Json }

# Preconditions: on dev, clean, in sync with origin; staging healthy
Invoke-Git fetch -q origin dev
if ((git -C $Root rev-parse --abbrev-ref HEAD) -ne "dev") { Write-Host "Check out dev first."; exit 1 }
if (git -C $Root status --porcelain) { Write-Host "Working tree not clean; commit or stash first."; exit 1 }
if ((git -C $Root rev-parse HEAD) -ne (git -C $Root rev-parse origin/dev)) {
    Write-Host "dev is not in sync with origin/dev; pull or push first."; exit 1
}
if ((Rollout).status.phase -ne "Healthy") { Write-Host "Staging Rollout is not Healthy."; exit 1 }

$dep = $null
if (-not $NoDeployer) {
    Write-Host "Starting the deployer (events: evaluation\runs\deployer-events.jsonl)..."
    $dep = Start-Process -FilePath $python -ArgumentList "`"$deployerPy`" run" -PassThru -WindowStyle Hidden
}
try {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $note = "Touched by infra/scripts/check-step3 to test the CI path filters: $stamp`n"

    [IO.File]::WriteAllText((Join-Path $Root "app\ci-trigger.txt"), $note)
    Invoke-Git add app/ci-trigger.txt
    Invoke-Git commit -q -m "Step 3 check: app change ($stamp)"
    $appSha = git -C $Root rev-parse HEAD
    Invoke-Git push -q origin dev
    Write-Host "Pushed app commit $($appSha.Substring(0,7))."

    # 1. CI for the app commit
    Write-Host "Waiting for app-ci on $($appSha.Substring(0,7))..."
    $run = $null
    $done = WaitUntil {
        $script:run = RunStatus $appSha
        $script:run -and $script:run.status -eq "completed"
    } ($CiTimeoutMinutes * 60) 60
    Write-Host "      run: $($run.html_url)"
    Check "app commit ran app-ci to success (image pushed)" { $done -and $run.conclusion -eq "success" }

    # Docs-only commit, in its own push; checked at the end, minutes later
    [IO.File]::WriteAllText((Join-Path $Root "docs\ci-trigger.txt"), $note)
    Invoke-Git add docs/ci-trigger.txt
    Invoke-Git commit -q -m "Step 3 check: docs-only change ($stamp)"
    $docsSha = git -C $Root rev-parse HEAD
    Invoke-Git push -q origin dev
    Write-Host "Pushed docs-only commit $($docsSha.Substring(0,7))."

    # 2-3. Deployer -> canary -> promoted under L0
    $image = "$ImageRepo`:$appSha"
    $started = WaitUntil { (Rollout).spec.template.spec.containers[0].image -eq $image } 600
    Check "deployer started a staging canary of $($appSha.Substring(0,7)) with no manual step" { $started }
    $promoted = $false
    if ($started) {
        $promoted = WaitUntil {
            $r = Rollout
            $r.status.abort -ne $true -and $r.status.phase -eq "Healthy" -and
                "$($r.status.observedGeneration)" -eq "$($r.metadata.generation)" -and
                $r.status.stableRS -eq $r.status.currentPodHash
        } ($RolloutTimeoutMinutes * 60)
    }
    $r = Rollout
    $hash = $r.status.currentPodHash
    $runs = kubectl -n $Namespace get analysisrun -l "rollouts-pod-template-hash=$hash" `
        -o "jsonpath={.items[*].status.phase}"
    $ok = @(($runs -split " ") | Where-Object { $_ -eq "Successful" }).Count
    Write-Host "      rollout: $($r.status.phase), stable $($r.status.stableRS), analyses: $runs"
    if (-not $promoted) {
        $pull = kubectl -n $Namespace get pods -l "rollouts-pod-template-hash=$hash" `
            -o "jsonpath={.items[*].status.containerStatuses[*].state.waiting.reason}"
        if ($pull -match "ImagePull|ErrImage") {
            Write-Host "      canary pods cannot pull $image ($pull): is the GHCR package public?"
        }
    }
    Check "canary completed under L0 (promoted, 2 successful analyses)" { $promoted -and $ok -ge 2 }

    # Let the deployer finish its own wait, log the outcome and stop its load generator
    $eventsFile = Join-Path $Root "evaluation\runs\deployer-events.jsonl"
    $pattern = '"event": "deploy_finished".*"outcome": "promoted".*"sha": "' + $appSha + '"'
    $logged = WaitUntil {
        (Test-Path $eventsFile) -and (Select-String -Path $eventsFile -Pattern $pattern -Quiet)
    } 120 5
    Check "deployer logged the deployment of $($appSha.Substring(0,7)) as promoted" { $logged }

    # 4. Docs-only commit: no app-ci run (GitHub creates runs within seconds of a push)
    Check "docs-only commit $($docsSha.Substring(0,7)) started no app-ci run" { $null -eq (RunStatus $docsSha) }
} finally {
    if ($dep) { Stop-Process -Id $dep.Id -ErrorAction SilentlyContinue }
}

Write-Host ""
Write-Host "Deployer events: $(Join-Path $Root 'evaluation\runs\deployer-events.jsonl')"
if ($failed -eq 0) { Write-Host "All step 3 checks passed."; exit 0 }
else { Write-Host "$failed check(s) failed."; exit 1 }
