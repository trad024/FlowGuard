# Measures the cluster's real memory use against the 4 GB laptop budget (blueprint section 10).
# Usage: .\infra\scripts\check-resources.ps1
$BudgetMiB = 4096
$Server = "k3d-flowguard-server-0"
$stats = docker stats --no-stream --format "{{.Name}};{{.MemUsage}}" | Where-Object { $_ -like "k3d-flowguard*" }
# The server node must be running; a leftover tools container alone is not a cluster.
if (-not ($stats | Where-Object { $_ -like "$Server;*" })) {
    Write-Host "$Server is not running. Run .\infra\scripts\setup.ps1 first."
    exit 1
}

function ToMiB($s) {
    if ($s -match "([\d\.]+)\s*([KMG]i?B)") {
        $v = [double]$Matches[1]
        switch -regex ($Matches[2]) { "^K" { $v / 1024 } "^M" { $v } "^G" { $v * 1024 } }
    } else { 0 }
}
$total = 0
foreach ($line in $stats) {
    $name, $usage = $line -split ";"
    $mib = ToMiB(($usage -split "/")[0])
    $total += $mib
    Write-Host ("{0,-28} {1,8:N0} MiB" -f $name, $mib)
}
Write-Host ("{0,-28} {1,8:N0} MiB  (budget {2} MiB)" -f "TOTAL cluster", $total, $BudgetMiB)
Write-Host ""
Write-Host "Per pod (from inside the cluster):"
kubectl top pods -A 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "  (kubectl top needs metrics-server, disabled to save RAM; use the totals above or Grafana)" }
if ($total -gt $BudgetMiB) { Write-Host "OVER BUDGET"; exit 1 } else { Write-Host "Within budget."; exit 0 }
