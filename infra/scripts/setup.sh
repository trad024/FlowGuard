#!/usr/bin/env bash
# FlowGuard: build the laptop cluster from zero (steps 1 and 2). Safe to rerun.
# Requires Docker (running), k3d and kubectl. Usage: ./infra/scripts/setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CLUSTER=flowguard

for tool in docker k3d kubectl; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed. See infra/README.md." >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }

if k3d cluster list "$CLUSTER" >/dev/null 2>&1; then
  echo "Cluster '$CLUSTER' already exists; reusing it."
else
  echo "Creating cluster '$CLUSTER'..."
  k3d cluster create --config "$ROOT/infra/k3d/cluster.yaml"
fi

echo "Creating namespaces..."
kubectl apply -k "$ROOT/infra/base"

echo "Installing Argo Rollouts..."
# Server-side: the CRDs are too large for client-side apply's annotation
kubectl apply --server-side -k "$ROOT/infra/argo-rollouts"
kubectl wait --for=condition=Established --timeout=60s \
  crd/rollouts.argoproj.io crd/analysistemplates.argoproj.io crd/analysisruns.argoproj.io
kubectl -n argo-rollouts rollout status deployment/argo-rollouts --timeout=180s

# A step 1 cluster ran podinfo as a Deployment; the Rollout replaces it.
for ns in staging prod; do
  kubectl -n "$ns" delete deployment podinfo --ignore-not-found
done

PASSWORD="${GRAFANA_ADMIN_PASSWORD:-flowguard}"
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=password="$PASSWORD" --dry-run=client -o yaml | kubectl apply -f -

echo "Deploying podinfo and monitoring..."
kubectl apply -k "$ROOT/infra"

echo "Waiting for workloads to be ready..."
kubectl -n monitoring rollout status deployment --timeout=300s
for ns in staging prod; do
  kubectl -n "$ns" wait rollout/podinfo --for=jsonpath='{.status.phase}'=Healthy --timeout=300s
done

cat <<MSG

Ready:
  podinfo staging  http://localhost:9898
  podinfo prod     http://localhost:9899
  Prometheus       http://localhost:9090
  Alertmanager     http://localhost:9093
  Grafana          http://localhost:3000  (admin / $PASSWORD)

Next: ./infra/scripts/smoke.sh, ./infra/scripts/check-step2.sh, ./infra/scripts/check-resources.sh
MSG
