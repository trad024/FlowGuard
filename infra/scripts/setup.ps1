# FlowGuard step 1: build the laptop cluster from zero.
# Requires Docker Desktop (running), k3d and kubectl on PATH.
# Usage (from the repo root):  .\infra\scripts\setup.ps1
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$Cluster = "flowguard"

foreach ($tool in "docker", "k3d", "kubectl") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool is not installed or not on PATH. See infra/README.md."
    }
}
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop first." }

$existing = k3d cluster list -o json | ConvertFrom-Json | Where-Object { $_.name -eq $Cluster }
if ($existing) {
    Write-Host "Cluster '$Cluster' already exists; reusing it."
} else {
    Write-Host "Creating cluster '$Cluster'..."
    k3d cluster create --config (Join-Path $Root "infra\k3d\cluster.yaml")
    if ($LASTEXITCODE -ne 0) { throw "k3d cluster create failed." }
}

Write-Host "Creating namespaces..."
kubectl apply -k (Join-Path $Root "infra\base")

$password = if ($env:GRAFANA_ADMIN_PASSWORD) { $env:GRAFANA_ADMIN_PASSWORD } else { "flowguard" }
kubectl -n monitoring create secret generic grafana-admin `
    --from-literal=password=$password --dry-run=client -o yaml | kubectl apply -f -

Write-Host "Deploying podinfo and monitoring..."
kubectl apply -k (Join-Path $Root "infra")
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed." }

Write-Host "Waiting for deployments to be ready..."
foreach ($ns in "staging", "prod", "monitoring") {
    kubectl -n $ns rollout status deployment --timeout=300s
    if ($LASTEXITCODE -ne 0) { throw "Deployments in $ns did not become ready." }
}

Write-Host ""
Write-Host "Ready:"
Write-Host "  podinfo staging  http://localhost:9898"
Write-Host "  podinfo prod     http://localhost:9899"
Write-Host "  Prometheus       http://localhost:9090"
Write-Host "  Alertmanager     http://localhost:9093"
Write-Host "  Grafana          http://localhost:3000  (admin / $password)"
Write-Host ""
Write-Host "Next: .\infra\scripts\smoke.ps1, then .\infra\scripts\check-resources.ps1"
