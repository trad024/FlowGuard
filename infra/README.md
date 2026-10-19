# infra

The laptop platform: a k3d cluster with `staging` and `prod` namespaces running podinfo, and a
`monitoring` namespace with Prometheus, Alertmanager, kube-state-metrics and Grafana. Plain
manifests applied with `kubectl apply -k`, no Helm.

## Prerequisites

| Tool | Windows install |
| --- | --- |
| Docker Desktop (running, WSL2 backend) | [docker.com](https://www.docker.com/products/docker-desktop/) |
| k3d v5.8+ | `winget install k3d` or `choco install k3d` |
| kubectl v1.31+ | `winget install Kubernetes.kubectl` |

In Docker Desktop, set Settings > Resources > Memory to about 4 GB (WSL2: `memory=4GB` in
`%UserProfile%\.wslconfig`).

## Run it (from the repo root)

```powershell
.\infra\scripts\setup.ps1             # create the cluster and deploy everything
.\infra\scripts\smoke.ps1             # acceptance checks
.\infra\scripts\check-resources.ps1   # measured RAM against the 4 GB budget
.\infra\scripts\teardown.ps1          # delete it all
```

On Linux or macOS use the `.sh` versions. `GRAFANA_ADMIN_PASSWORD` overrides the default
Grafana password (`flowguard`), which is for local use only.

## What runs where

| Component | Namespace | URL on the laptop | Memory limit |
| --- | --- | --- | --- |
| podinfo | staging | http://localhost:9898 | 64 Mi |
| podinfo | prod | http://localhost:9899 | 64 Mi |
| Prometheus (1-day retention) | monitoring | http://localhost:9090 | 400 Mi |
| Alertmanager | monitoring | http://localhost:9093 | 64 Mi |
| kube-state-metrics | monitoring | internal only | 96 Mi |
| Grafana | monitoring | http://localhost:3000 | 200 Mi |

k3s itself runs with Traefik, metrics-server and the service load balancer disabled. Grafana
opens on the **FlowGuard / podinfo** dashboard: request rate, 5xx error rate, latency p95, CPU,
memory and restarts, filterable by environment.

## Layout

```
infra/
  k3d/cluster.yaml          cluster definition and port map
  base/                     namespaces
  apps/podinfo/             base Deployment + Service, staging and prod overlays
  monitoring/               Prometheus, Alertmanager, kube-state-metrics, Grafana
  scripts/                  setup, smoke, check-resources, teardown (.ps1 and .sh)
  kustomization.yaml        everything above in one apply
```

## Step 1 is done when

- [ ] `setup.ps1` builds the cluster from zero without manual steps
- [ ] `smoke.ps1` passes every check
- [ ] podinfo metrics show in the Grafana dashboard
- [ ] `check-resources.ps1` reports the cluster within the 4 GB budget (record the number in
      `docs/README.md`)
