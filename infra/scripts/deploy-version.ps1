# Starts a canary of podinfo with a good, bad or fault version (test releases).
#   good: podinfo with a new UI message only, a harmless change.
#   bad:  the same plus --random-error, podinfo's own fault flag (1 request in 3 fails with
#         500, 400 or 409, so about 11% are 5xx). --random-delay is not used: podinfo applies it
#         to the probes too, so the canary would never become ready and analysis would not run.
#   fault: graded fault (D19, needs a FlowGuard-built image): -ErrorRate share of requests
#         answered 500, -LatencyMs plus 0..-JitterMs added, on a -Fraction of requests. User
#         routes only; probes and the metrics port are untouched.
# Every version gets a new release Secret (D25), podinfo-release-<random>, mounted with envFrom:
# the fault settings for "fault", empty values otherwise. The pod spec shows only its name.
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
$settings = [ordered]@{
    PODINFO_FAULT_ERROR_RATE = ""; PODINFO_FAULT_LATENCY_MS = ""
    PODINFO_FAULT_JITTER_MS = ""; PODINFO_FAULT_FRACTION = ""
}
if ($Version -eq "fault") {
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $settings.PODINFO_FAULT_ERROR_RATE = $ErrorRate.ToString($inv)
    $settings.PODINFO_FAULT_LATENCY_MS = "$LatencyMs"
    $settings.PODINFO_FAULT_JITTER_MS = "$JitterMs"
    $settings.PODINFO_FAULT_FRACTION = $Fraction.ToString($inv)
}
$release = "podinfo-release-" + (-join (1..8 | ForEach-Object { "{0:x}" -f (Get-Random -Maximum 16) }))
$secret = @{
    apiVersion = "v1"; kind = "Secret"; type = "Opaque"
    metadata = @{ name = $release; namespace = $Namespace; labels = @{ "release-settings" = "podinfo" } }
    stringData = $settings
}

# JSON through files: Windows PowerShell mangles quotes in native arguments
$ops = @(
    @{ op = "replace"; path = "/spec/template/spec/containers/0/command"; value = $command },
    @{ op = "add"; path = "/spec/template/spec/containers/0/envFrom"; value = @(@{ secretRef = @{ name = $release } }) }
)
if ($Image) { $ops += @{ op = "replace"; path = "/spec/template/spec/containers/0/image"; value = $Image } }
$patch = ConvertTo-Json -Compress -Depth 6 -InputObject $ops
$file = [IO.Path]::GetTempFileName()
try {
    [IO.File]::WriteAllText($file, (ConvertTo-Json -Compress -Depth 5 -InputObject $secret))
    kubectl apply -f $file | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "kubectl apply of the release Secret failed." }
    [IO.File]::WriteAllText($file, $patch)
    kubectl -n $Namespace patch rollout podinfo --type json --patch-file $file
    if ($LASTEXITCODE -ne 0) { throw "kubectl patch failed." }
} finally {
    Remove-Item $file -ErrorAction SilentlyContinue
}
$imageNote = if ($Image) { ", image $Image" } else { "" }
Write-Host "Deployed $Version version to $Namespace ('$Message'$imageNote, settings $release)."
Write-Host "Follow it: kubectl -n $Namespace get rollout podinfo -w"
