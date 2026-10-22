# docs

The project blueprint is the reference for every decision. Key points for quick reading:

- Six business objectives: BO1, BO2, BO3, BO6 main; BO4, BO5 secondary.
- Six DSOs, one agent each for DSO1–DSO5 and a risk gate for DSO6.
- Every agent action passes the risk gate; tiers are fixed in `agents/flowguard/catalog.py`.
- Both modes (`agents`, `baseline`) run the same scenarios and write the same audit format.

## Decisions taken during the build

| Date | Step | Decision | Reason |
| --- | --- | --- | --- |
| 2026-10-19 | 0 | Workflows in `.github/workflows/`, scripts in `ci/` | GitHub only runs workflows from that folder |
| 2026-10-19 | 0 | Environments limited to `staging` and `prod` in the contracts | Matches the two namespaces |
| 2026-10-19 | 0 | Draft gate, audit, executor and agent stubs held back until step 5 | Not part of step 0 |
| 2026-10-19 | 1 | Plain manifests with kustomize instead of Helm charts | One less tool on Windows, every setting visible and versioned |
| 2026-10-19 | 1 | k3s Traefik, metrics-server and service load balancer disabled; NodePorts mapped to localhost | Saves RAM; no port-forward needed |
| 2026-10-22 | 1 | Measured cluster RAM on the team laptop: 982 MiB of 4096 MiB (`check-resources.ps1`, Docker not yet capped at 4 GB) | Step 1 acceptance |
| 2026-10-22 | 2 | Argo Rollouts v1.10.1 from the official `install.yaml` (kustomize remote resource pinned by tag, sha256 in the kustomization), applied first and server-side | Its CRDs exceed the 256 KiB annotation of client-side apply |
| 2026-10-22 | 2 | Canary without a traffic router: weight = share of the 10 replicas behind the one Service; `maxSurge` 20%, `maxUnavailable` 0 | No mesh or ingress (excluded by the blueprint); stable capacity never drops |
| 2026-10-22 | 2 | Bad version = podinfo `--random-error` only, not `--random-delay` | podinfo applies both to its probes; a 0–5 s delay fails the 1 s readiness probe, so the canary never becomes ready and L0 would never run |
| 2026-10-22 | 2 | podinfo metrics scraped on its separate metrics port (9797) and liveness on that port's `/healthz` | `--random-error` also hits `/metrics` and `/healthz` on the main port: in the first run 1 scrape in 3 of the bad pods failed, their 5xx counters had too few samples for `rate()`, and the bad version passed the 20% analysis |
| 2026-10-22 | 2 | L0 metrics read `http_request_duration_seconds` with `path="root"`, not `http_requests_total` | The counter has no path label, so probes and scrapes would dilute the canary's ratio |
| 2026-10-22 | 2 | Static memory-limit check raised from 1536 to 2560 MiB of summed limits at peak pod count (proposal, awaiting approval) | Option A's 20 podinfo pods (+4 during a canary) at 64 Mi cannot fit 1536; 4 GB minus about 1 GB for k3s/Docker minus 0.5 GB margin; real use is measured by `check-resources` |
| 2026-10-22 | 3 | podinfo 6.7.1 vendored in `app/podinfo` (cmd, pkg, ui, Go module, Dockerfiles, Makefile, LICENSE; NOTICE records source and sha256); charts, deploy, timoni and upstream CI left out | The pipeline needs source it can build; the rest is upstream packaging |
| 2026-10-22 | 3 | Scans report and never block in CI: Trivy found 78 HIGH and 4 CRITICAL in the image, Gitleaks 2 findings in upstream podinfo (`cmd/podinfo/main.go`, a test file) | Blocking would stop every run; the allow / review / block decision is the Security agent's (DSO4) with its rule baseline in Step 4 |
| 2026-10-22 | 3 | Optional jobs run as one matrix job with `max-parallel: 1` in the planned order | GitHub cannot reorder jobs at run time; a sequential matrix applies the plan's order, which DSO5 measures |
| 2026-10-22 | 3 | Tools pinned to releases at least two weeks old: Trivy 0.75.0, syft 1.54.1, Gitleaks 8.30.1 | Newest releases were 1 to 3 days old; supply-chain caution |
| 2026-10-22 | 3 | `app-ci` path filter also includes its own workflow file | Otherwise a workflow fix could not be tested without touching `app/` |
| 2026-10-22 | 3 | Deployer lives in `ci/` (per `ci/README.md` from step 0), stateless: the deployed commit is read from the Rollout's image tag | A restart never deploys twice; an aborted commit is not retried until a newer one succeeds |
| 2026-10-22 | 3 | CI manifest validation skips the upstream Argo Rollouts CRDs | kubeconform has no schema for `CustomResourceDefinition`; this broke the `checks` workflow from `eb62ae3` |
