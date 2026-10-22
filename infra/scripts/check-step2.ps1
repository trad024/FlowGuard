# FlowGuard step 2 acceptance check, run against the live cluster (about 8 minutes):
#   1. a good version goes through 20% -> 50% -> 100% and becomes the stable version;
#   2. during that canary the per-request log shows both stable and canary pods;
#   3. a bad version is aborted by the L0 analysis on its own and stable stays the same;
#   4. after the abort, every request is served by stable pods without errors;
#   5. RAM stays within the 4 GB budget at the canary peak and at the end.
# Usage: .\infra\scripts\check-step2.ps1 [-Namespace staging] [-Rate 10]
param(
    [ValidateSet("staging", "prod")][string]$Namespace = "staging",
    [double]$Rate = 10
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Port = if ($Namespace -eq "staging") { 9898 } else { 9899 }
$failed = 0
function Check($name, [scriptblock]$test) {
    try {
        if (& $test) { Write-Host "PASS  $name" } else { Write-Host "FAIL  $name"; $script:failed++ }
    } catch { Write-Host "FAIL  $name ($($_.Exception.Message))"; $script:failed++ }
}
function Field($path) { kubectl -n $Namespace get rollout podinfo -o "jsonpath={$path}" }
function UtcNow { (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ") }
function WaitUntil([scriptblock]$condition, [int]$seconds) {
    $deadline = (Get-Date).AddSeconds($seconds)
    while ((Get-Date) -lt $deadline) {
        if (& $condition) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

function Resources {
    # check-resources.ps1 sends a failing "kubectl top" to $null; under "Stop", Windows
    # PowerShell would turn that into a terminating error, so run it with "Continue".
    $ErrorActionPreference = "Continue"
    & (Join-Path $PSScriptRoot "check-resources.ps1") | Out-Host
    return $LASTEXITCODE
}

# The real interpreter, not the py launcher, so stopping the process stops the load
$launcher = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$python = & $launcher -c "import sys; print(sys.executable)"
$loadgen = Join-Path $Root "evaluation\loadgen.py"
function Summary($since, $until) {
    & $python $loadgen summary $log --namespace $Namespace --since $since --until $until | ConvertFrom-Json
}
function Show($label, $summary) {
    Write-Host "      $label`: $($summary.requests) requests, $($summary.transport_errors) transport errors"
    foreach ($p in $summary.by_hash.PSObject.Properties) {
        Write-Host ("        {0,-12} {1,6} requests  {2,5} 5xx  {3,5} 4xx" -f `
            $p.Name, $p.Value.requests, $p.Value.status_5xx, $p.Value.other)
    }
}

if (-not (WaitUntil { (Field ".status.phase") -eq "Healthy" } 120)) {
    Write-Host "podinfo Rollout in $Namespace is not Healthy. Run .\infra\scripts\setup.ps1 first."
    exit 1
}
$stable0 = Field ".status.stableRS"
$runId = Get-Date -Format "yyyyMMdd-HHmmss"
$goodMessage = "check-step2 $runId"
$log = Join-Path $Root "evaluation\runs\step2-$Namespace-$runId.jsonl"

Write-Host "Load: $Rate requests/s on localhost:$Port, logged to $log"
$lgArgs = "`"$loadgen`" run --rate $Rate --target $Namespace=http://localhost:$Port/ --out `"$log`""
$lg = Start-Process -FilePath $python -ArgumentList $lgArgs -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 15

    # 1-2. Good version
    Write-Host "Deploying the good version (stable is $stable0)..."
    $t0 = UtcNow
    & (Join-Path $PSScriptRoot "deploy-version.ps1") good -Namespace $Namespace -Message $goodMessage | Out-Null
    $null = WaitUntil { (Field ".status.currentPodHash") -ne $stable0 } 60
    $goodHash = Field ".status.currentPodHash"
    # Peak pod count is at the 50% step
    $peakExit = 1
    if (WaitUntil { [int](Field ".status.currentStepIndex") -ge 4 } 400) {
        Write-Host "RAM at the 50% canary step:"
        $peakExit = Resources
    }
    $promoted = WaitUntil {
        (Field ".status.phase") -eq "Healthy" -and (Field ".status.stableRS") -eq $goodHash
    } 600
    $t1 = UtcNow
    $s1 = Summary $t0 $t1
    Check "good version $goodHash promoted to 100% (Healthy, now stable)" { $promoted }
    Check "per-request log shows stable and canary pods during the canary" {
        $s1.by_hash.$stable0.requests -gt 0 -and $s1.by_hash.$goodHash.requests -gt 0
    }
    Show "good canary" $s1

    # 3-4. Bad version
    Write-Host "Deploying the bad version..."
    $t2 = UtcNow
    & (Join-Path $PSScriptRoot "deploy-version.ps1") bad -Namespace $Namespace | Out-Null
    $null = WaitUntil { (Field ".status.currentPodHash") -ne $goodHash } 60
    $badHash = Field ".status.currentPodHash"
    $aborted = WaitUntil { (Field ".status.abort") -eq "true" } 600
    $t3 = UtcNow
    $runs = kubectl -n $Namespace get analysisrun -l "rollouts-pod-template-hash=$badHash" `
        -o "jsonpath={.items[*].status.phase}"
    Write-Host "      $(Field '.status.message')"
    Check "bad version $badHash aborted by the L0 analysis (AnalysisRun $runs)" {
        $aborted -and ($runs -match "Failed")
    }
    Check "stable version unchanged ($goodHash)" { (Field ".status.stableRS") -eq $goodHash }
    Show "bad canary" (Summary $t2 $t3)
    Write-Host "      (unknown = error responses: podinfo's injected errors have no body to name the pod)"

    $null = WaitUntil { -not (kubectl -n $Namespace get pods -l "rollouts-pod-template-hash=$badHash" -o name) } 120
    Start-Sleep -Seconds 5
    $t4 = UtcNow
    Start-Sleep -Seconds 20
    $s4 = Summary $t4 (UtcNow)
    Check "stable keeps serving after the abort (all requests on $goodHash, no errors)" {
        $names = @($s4.by_hash.PSObject.Properties | ForEach-Object { $_.Name })
        $s4.requests -gt 0 -and $s4.transport_errors -eq 0 -and $names.Count -eq 1 -and
            $names[0] -eq $goodHash -and $s4.by_hash.$goodHash.status_5xx -eq 0 -and
            $s4.by_hash.$goodHash.other -eq 0
    }
    Show "after abort" $s4

    # Back to the stable spec: same template as stable, so no new canary
    & (Join-Path $PSScriptRoot "deploy-version.ps1") good -Namespace $Namespace -Message $goodMessage | Out-Null
    $null = WaitUntil { (Field ".status.phase") -eq "Healthy" } 120
} finally {
    Stop-Process -Id $lg.Id -ErrorAction SilentlyContinue
}

Write-Host "RAM at the end:"
$endExit = Resources
Check "RAM within budget at the canary peak and at the end" { $peakExit -eq 0 -and $endExit -eq 0 }

Write-Host ""
Write-Host "Per-request log: $log"
if ($failed -eq 0) { Write-Host "All step 2 checks passed."; exit 0 }
else { Write-Host "$failed check(s) failed."; exit 1 }
