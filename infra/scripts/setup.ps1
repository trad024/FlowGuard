# FlowGuard: build the laptop cluster from zero (steps 1 and 2). Safe to rerun.
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

Write-Host "Installing Argo Rollouts..."
# Server-side: the CRDs are too large for client-side apply's annotation
kubectl apply --server-side -k (Join-Path $Root "infra\argo-rollouts")
if ($LASTEXITCODE -ne 0) { throw "Argo Rollouts install failed." }
kubectl wait --for=condition=Established --timeout=60s `
    crd/rollouts.argoproj.io crd/analysistemplates.argoproj.io crd/analysisruns.argoproj.io
kubectl -n argo-rollouts rollout status deployment/argo-rollouts --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Argo Rollouts controller did not become ready." }

# A step 1 cluster ran podinfo as a Deployment; the Rollout replaces it.
foreach ($ns in "staging", "prod") {
    kubectl -n $ns delete deployment podinfo --ignore-not-found
}

$password = if ($env:GRAFANA_ADMIN_PASSWORD) { $env:GRAFANA_ADMIN_PASSWORD } else { "flowguard" }
kubectl -n monitoring create secret generic grafana-admin `
    --from-literal=password=$password --dry-run=client -o yaml | kubectl apply -f -

Write-Host "Deploying podinfo and monitoring..."
kubectl apply -k (Join-Path $Root "infra")
if ($LASTEXITCODE -ne 0) { throw "kubectl apply failed." }

Write-Host "Waiting for workloads to be ready..."
kubectl -n monitoring rollout status deployment --timeout=300s
if ($LASTEXITCODE -ne 0) { throw "Deployments in monitoring did not become ready." }
foreach ($ns in "staging", "prod") {
    kubectl -n $ns wait rollout/podinfo --for=jsonpath='{.status.phase}'=Healthy --timeout=300s
    if ($LASTEXITCODE -ne 0) { throw "podinfo Rollout in $ns did not become Healthy." }
}

Write-Host ""
Write-Host "Ready:"
Write-Host "  podinfo staging  http://localhost:9898"
Write-Host "  podinfo prod     http://localhost:9899"
Write-Host "  Prometheus       http://localhost:9090"
Write-Host "  Alertmanager     http://localhost:9093"
Write-Host "  Grafana          http://localhost:3000  (admin / $password)"
Write-Host ""
Write-Host "Next: .\infra\scripts\smoke.ps1, .\infra\scripts\check-step2.ps1, .\infra\scripts\check-resources.ps1"
