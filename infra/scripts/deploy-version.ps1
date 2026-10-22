# Starts a canary of podinfo with a good or a bad version (step 2 test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
# A message equal to the stable version's goes straight back to stable, without a canary.
# Usage: .\infra\scripts\deploy-version.ps1 good|bad [-Namespace staging] [-Message "text"]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet("good", "bad")][string]$Version,
    [ValidateSet("staging", "prod")][string]$Namespace = "staging",
    [string]$Message = ""
)
$ErrorActionPreference = "Stop"

if (-not $Message) { $Message = "FlowGuard $Version build $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" }
$command = @("./podinfo", "--port=9898", "--level=info", "--port-metrics=9797", "--ui-message=$Message")
if ($Version -eq "bad") { $command += "--random-error" }

# JSON patch through a file: Windows PowerShell mangles quotes in native arguments
$patch = ConvertTo-Json -Compress -Depth 5 -InputObject @(
    @{ op = "replace"; path = "/spec/template/spec/containers/0/command"; value = $command }
)
$file = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllText($file, $patch)
    kubectl -n $Namespace patch rollout podinfo --type json --patch-file $file
    if ($LASTEXITCODE -ne 0) { throw "kubectl patch failed." }
} finally {
    Remove-Item $file -ErrorAction SilentlyContinue
}
Write-Host "Deployed $Version version to $Namespace ('$Message')."
Write-Host "Follow it: kubectl -n $Namespace get rollout podinfo -w"
