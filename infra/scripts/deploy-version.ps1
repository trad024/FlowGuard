# Starts a canary of podinfo with a good, bad or fault version (test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
#   fault: graded fault (D19, needs a FlowGuard-built image): -ErrorRate share of requests
#         answered 500, -LatencyMs plus 0..-JitterMs added, on a -Fraction of requests. User
#         routes only; probes and the metrics port are untouched.
# -Image also switches the container image (the CI deployer passes the image built for a commit).
# A message equal to the stable version's goes straight back to stable, without a canary.
# Usage: .\infra\scripts\deploy-version.ps1 good|bad [-Namespace staging] [-Message "text"] [-Image ref]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet("good", "bad", "fault")][string]$Version,
    [ValidateSet("staging", "prod")][string]$Namespace = "staging",
    [string]$Message = "",
    [string]$Image = "",
    [double]$ErrorRate = 0,
    [int]$LatencyMs = 0,
    [int]$JitterMs = 0,
    [double]$Fraction = 1
)
$ErrorActionPreference = "Stop"

if (-not $Message) { $Message = "FlowGuard $Version build $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" }
$command = @("./podinfo", "--port=9898", "--level=info", "--port-metrics=9797", "--ui-message=$Message")
if ($Version -eq "bad") { $command += "--random-error" }
if ($Version -eq "fault") {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $command += "--fault-error-rate=$($ErrorRate.ToString($inv))", "--fault-latency-ms=$LatencyMs",
        "--fault-jitter-ms=$JitterMs", "--fault-fraction=$($Fraction.ToString($inv))"
}

# JSON patch through a file: Windows PowerShell mangles quotes in native arguments
$ops = @(@{ op = "replace"; path = "/spec/template/spec/containers/0/command"; value = $command })
if ($Image) { $ops += @{ op = "replace"; path = "/spec/template/spec/containers/0/image"; value = $Image } }
$patch = ConvertTo-Json -Compress -Depth 5 -InputObject $ops
$file = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllText($file, $patch)
    kubectl -n $Namespace patch rollout podinfo --type json --patch-file $file
    if ($LASTEXITCODE -ne 0) { throw "kubectl patch failed." }
} finally {
    Remove-Item $file -ErrorAction SilentlyContinue
}
$imageNote = if ($Image) { ", image $Image" } else { "" }
Write-Host "Deployed $Version version to $Namespace ('$Message'$imageNote)."
Write-Host "Follow it: kubectl -n $Namespace get rollout podinfo -w"
