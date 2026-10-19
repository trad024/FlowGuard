# FlowGuard step 1 acceptance check: services answer and podinfo metrics reach Prometheus.
# Usage: .\infra\scripts\smoke.ps1
$ErrorActionPreference = "Stop"
$failed = 0
function Check($name, [scriptblock]$test) {
    try {
        if (& $test) { Write-Host "PASS  $name" } else { Write-Host "FAIL  $name"; $script:failed++ }
    } catch { Write-Host "FAIL  $name ($($_.Exception.Message))"; $script:failed++ }
}

Write-Host "Sending 60 requests to each podinfo so metrics exist..."
foreach ($port in 9898, 9899) {
    1..60 | ForEach-Object { Invoke-WebRequest "http://localhost:$port/" -UseBasicParsing | Out-Null }
}
Start-Sleep -Seconds 35   # two scrape intervals

function PromQuery($q) {
    $u = "http://localhost:9090/api/v1/query?query=" + [uri]::EscapeDataString($q)
    (Invoke-RestMethod $u).data.result
}

Check "podinfo staging answers"  { (Invoke-WebRequest "http://localhost:9898/healthz" -UseBasicParsing).StatusCode -eq 200 }
Check "podinfo prod answers"     { (Invoke-WebRequest "http://localhost:9899/healthz" -UseBasicParsing).StatusCode -eq 200 }
Check "Prometheus ready"         { (Invoke-WebRequest "http://localhost:9090/-/ready" -UseBasicParsing).StatusCode -eq 200 }
Check "Alertmanager ready"       { (Invoke-WebRequest "http://localhost:9093/-/ready" -UseBasicParsing).StatusCode -eq 200 }
Check "Grafana healthy"          { (Invoke-RestMethod "http://localhost:3000/api/health").database -eq "ok" }
Check "podinfo scraped in both namespaces" {
    (PromQuery 'count(up{app="podinfo"} == 1)')[0].value[1] -eq "2"
}
Check "podinfo request metrics present" {
    (PromQuery 'count(sum by (namespace) (http_requests_total{app="podinfo"}))')[0].value[1] -eq "2"
}
Check "kube-state-metrics scraped" { (PromQuery 'up{job="kube-state-metrics"}')[0].value[1] -eq "1" }
Check "container memory metrics present" {
    (PromQuery 'count(container_memory_working_set_bytes{container="podinfo"})').Count -gt 0
}

Write-Host ""
if ($failed -eq 0) { Write-Host "All checks passed. Open Grafana > FlowGuard > podinfo." }
else { Write-Host "$failed check(s) failed."; exit 1 }
