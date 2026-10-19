#!/usr/bin/env bash
# FlowGuard step 1: build the laptop cluster from zero.
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

PASSWORD="${GRAFANA_ADMIN_PASSWORD:-flowguard}"
kubectl -n monitoring create secret generic grafana-admin \
  --from-literal=password="$PASSWORD" --dry-run=client -o yaml | kubectl apply -f -

echo "Deploying podinfo and monitoring..."
kubectl apply -k "$ROOT/infra"

echo "Waiting for deployments to be ready..."
for ns in staging prod monitoring; do
  kubectl -n "$ns" rollout status deployment --timeout=300s
done

cat <<MSG

Ready:
  podinfo staging  http://localhost:9898
  podinfo prod     http://localhost:9899
  Prometheus       http://localhost:9090
  Alertmanager     http://localhost:9093
  Grafana          http://localhost:3000  (admin / $PASSWORD)

Next: ./infra/scripts/smoke.sh, then ./infra/scripts/check-resources.sh
MSG
