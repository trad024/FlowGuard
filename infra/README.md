# infra

The laptop platform: a k3d cluster with `staging` and `prod` namespaces running podinfo as an
Argo Rollout (canary 20% → 50% → 100% under the frozen L0 analysis), an `argo-rollouts`
namespace with the controller, and a `monitoring` namespace with Prometheus, Alertmanager,
kube-state-metrics and Grafana. Plain manifests applied with `kubectl apply -k`, no Helm.

## Prerequisites

| Tool | Windows install |
| --- | --- |
| Docker Desktop (running, WSL2 backend) | [docker.com](https://www.docker.com/products/docker-desktop/) |
| k3d v5.8+ | `winget install k3d` or `choco install k3d` |
| kubectl v1.31+ | `winget install Kubernetes.kubectl` |
| Python 3.11+ (load generator, standard library only) | `winget install Python.Python.3.11` |

In Docker Desktop, set Settings > Resources > Memory to about 4 GB (WSL2: `memory=4GB` in
`%UserProfile%\.wslconfig`).

## Run it (from the repo root)

```powershell
.\infra\scripts\setup.ps1             # create the cluster and deploy everything (safe to rerun)
.\infra\scripts\smoke.ps1             # step 1 checks: services and metrics
.\infra\scripts\check-step2.ps1       # step 2 checks: good canary promotes, bad one aborts (~8 min)
.\infra\scripts\check-step3.ps1       # step 3 checks: app commit -> CI -> staging canary (~15-30 min)
.\infra\scripts\check-resources.ps1   # measured RAM against the 4 GB budget
.\infra\scripts\teardown.ps1          # delete it all
```

On Linux or macOS use the `.sh` versions. `GRAFANA_ADMIN_PASSWORD` overrides the default
Grafana password (`flowguard`), which is for local use only.

## Canary releases (step 2)

```powershell
py evaluation\loadgen.py run --rate 10                 # traffic + per-request log, Ctrl+C to stop
.\infra\scripts\deploy-version.ps1 good                # harmless change: new UI message
.\infra\scripts\deploy-version.ps1 bad                 # podinfo --random-error: about 11% 5xx
.\infra\scripts\deploy-version.ps1 fault -ErrorRate 0.05 -LatencyMs 200   # graded fault (D19)
kubectl -n staging get rollout podinfo -w              # follow the steps
kubectl -n staging get analysisrun                     # L0 verdicts
py evaluation\loadgen.py summary evaluation\runs\<file>.jsonl --namespace staging
```

Each Rollout has 10 replicas. With no traffic router, the canary weight is the canary's share of
the pods behind the one Service: 2 pods at 20%, 5 at 50%. At 20% and 50% the rollout waits 30 s,
then runs the L0 AnalysisTemplate (`apps/podinfo/base/analysis-l0.yaml`): the canary's 5xx ratio
must stay ≤ 1% and its p99 latency ≤ 500 ms, measured every 30 s, four times; a second failed
measurement aborts the rollout and the stable version keeps serving. The thresholds are frozen;
their justification is in `docs/blueprint-v2.md` section 6.

The load generator (`evaluation/loadgen.py`) sends a fixed rate of requests over new connections
and writes one JSON line per request (time, namespace, status, latency, pod) to the gitignored
`evaluation/runs/`. The pod name comes from podinfo's response body. podinfo's injected errors
have no body, so an error response is logged with `"pod": null`.

Prometheus scrapes podinfo on its separate metrics port (9797), and the liveness probe uses
that port's `/healthz`. That port has none of podinfo's fault middleware, so a bad version's
errors reach only user traffic and readiness: they cannot hide its own metrics or get it
restarted.

A `deploy-version` message equal to the stable version's returns straight to stable without a
canary: that is how `check-step2` clears the aborted bad version. An aborted rollout stays
`Degraded` until the next deploy.

Any change to the podinfo template on a running cluster (including rerunning `setup` after the
template changed) goes through the canary, and **the canary needs traffic**: with no requests,
L0 has no data and aborts. Run `py evaluation\loadgen.py run` while it rolls out. A new
cluster's first rollout goes straight to 100% without analysis.

## CI to staging (step 3)

```powershell
py ci\deployer.py run      # leave running: deploys every successful app-ci run on dev to staging
```

A commit under `app/` runs `.github/workflows/app-ci.yml` (see `ci/README.md`), which pushes
`ghcr.io/trad024/flowguard-podinfo:<sha>`. The deployer polls GitHub, then deploys that image
with `deploy-version.ps1 good -Image ...` while it runs the load generator, so the canary goes
20/50/100 under L0 with no manual step. `check-step3` proves it end to end on a throwaway branch
`scenario/step3-<time>`: it pushes one marker commit under `app/` and, separately, one under
`docs/` (which must not start the pipeline), waits for CI and the canary, then deletes the
branch, so nothing reaches `dev`. The cluster pulls the image anonymously, so the GHCR package must be public.

Rerunning `setup` resets the Rollout to the upstream podinfo image; the deployer then rolls the
newest CI image out again.

## What runs where

| Component | Namespace | URL on the laptop | Memory limit |
| --- | --- | --- | --- |
| podinfo Rollout, 10 replicas (+2 during a canary) | staging | http://localhost:9898 | 64 Mi each |
| podinfo Rollout, 10 replicas (+2 during a canary) | prod | http://localhost:9899 | 64 Mi each |
| Argo Rollouts controller v1.10.1 | argo-rollouts | internal only | 128 Mi |
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
  argo-rollouts/            controller: pinned upstream install.yaml + memory limit patch
                            (applied first and server-side: its CRDs are too large otherwise)
  apps/podinfo/             base Rollout + Service + L0 AnalysisTemplate, staging and prod overlays
  monitoring/               Prometheus, Alertmanager, kube-state-metrics, Grafana
  scripts/                  setup, smoke, check-step2, deploy-version, check-resources, teardown
                            (.ps1 and .sh)
  kustomization.yaml        everything except argo-rollouts in one apply
```

## Step 1 is done when

- [ ] `setup.ps1` builds the cluster from zero without manual steps
- [ ] `smoke.ps1` passes every check
- [ ] podinfo metrics show in the Grafana dashboard
- [ ] `check-resources.ps1` reports the cluster within the 4 GB budget (record the number in
      `docs/README.md`)

## Step 2 is done when

- [ ] `check-step2.ps1` passes: a good version promotes to 100%, a bad one is aborted by L0 and
      stable keeps serving, the per-request log shows canary and stable pods
- [ ] RAM stays within the 4 GB budget at the canary peak
- [ ] The per-request log is usable for DSO3 (open: error responses are not yet tied to a pod)
