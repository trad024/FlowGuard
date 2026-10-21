# FlowGuard step 1 acceptance check: services answer and podinfo metrics reach Prometheus.
# Usage: .\infra\scripts\smoke.ps1
$ErrorActionPreference = "Stop"
$failed = 0
function Check($name, [scriptblock]$test) {
    try {
        if (& $test) { Write-Host "PASS  $name" } else { Write-Host "FAIL  $name"; $script:failed++ }
    } catch { Write-Host "FAIL  $name ($($_.Exception.Message))"; $script:failed++ }
}

# Stop early with a clear message when the cluster is not up.
try {
    Invoke-WebRequest "http://localhost:9898/healthz" -UseBasicParsing -TimeoutSec 5 | Out-Null
} catch {
    Write-Host "Cluster not reachable on localhost:9898. Run .\infra\scripts\setup.ps1 first."
    exit 1
}

Write-Host "Sending 60 requests to each podinfo so metrics exist..."
foreach ($port in 9898, 9899) {
    1..60 | ForEach-Object { Invoke-WebRequest "http://localhost:$port/" -UseBasicParsing | Out-Null }
}
Start-Sleep -Seconds 35   # two scrape intervals

# Returns the first sample value as a string, or $null. Indexing with [0] works whether
# Prometheus returns one result or several (Windows PowerShell 5.1 has no .Count on a
# single object).
function PromValue($q) {
    $u = "http://localhost:9090/api/v1/query?query=" + [uri]::EscapeDataString($q)
    $r = @((Invoke-RestMethod $u).data.result)
    if ($r.Length -eq 0) { return $null }
    return $r[0].value[1]
}

Check "podinfo staging answers"  { (Invoke-WebRequest "http://localhost:9898/healthz" -UseBasicParsing).StatusCode -eq 200 }
Check "podinfo prod answers"     { (Invoke-WebRequest "http://localhost:9899/healthz" -UseBasicParsing).StatusCode -eq 200 }
Check "Prometheus ready"         { (Invoke-WebRequest "http://localhost:9090/-/ready" -UseBasicParsing).StatusCode -eq 200 }
Check "Alertmanager ready"       { (Invoke-WebRequest "http://localhost:9093/-/ready" -UseBasicParsing).StatusCode -eq 200 }
Check "Grafana healthy"          { (Invoke-RestMethod "http://localhost:3000/api/health").database -eq "ok" }
Check "podinfo scraped in both namespaces" {
    (PromValue 'count(up{app="podinfo"} == 1)') -eq "2"
}
Check "podinfo request metrics present" {
    (PromValue 'count(sum by (namespace) (http_requests_total{app="podinfo"}))') -eq "2"
}
Check "kube-state-metrics scraped" { (PromValue 'up{job="kube-state-metrics"}') -eq "1" }
Check "container memory metrics present in both namespaces" {
    (PromValue 'count(sum by (namespace) (container_memory_working_set_bytes{container="podinfo"}))') -eq "2"
}

Write-Host ""
if ($failed -eq 0) { Write-Host "All checks passed. Open Grafana > FlowGuard > podinfo." }
else { Write-Host "$failed check(s) failed."; exit 1 }
