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
| 2026-10-22 | 3 | Integration checks never pipe into `grep -q` under `pipefail`, and report failures as workflow annotations | `grep -q` exits at the first match, curl then fails with SIGPIPE on large responses (`/metrics`): the first app-ci run failed this way; job logs need a signed-in account, annotations do not |
| 2026-10-22 | 3 | `check-step3` pushes the app commit and the docs-only commit separately and waits for the deployer's own `deploy_finished` event | GitHub runs a workflow once per push, for its last commit; stopping the deployer early left its load generator running |
| 2026-10-24 | 3 | Check and scenario commits go to throwaway `scenario/<id>` branches (app-ci also runs there; the deployer takes `--branch`); the branch is deleted afterwards | Test commits polluted `dev`; only code changes belong there |
| 2026-10-24 | 3 | The plan job also writes its mode and plan as a workflow annotation | Annotations are public; job logs and step summaries need a signed-in account |
| 2026-10-25 | 3 | Plan mode also from the branch name: `scenario/baseline/*`, `scenario/optimizer/*`; manual input and repo variable keep priority | GitHub offers manual runs only for workflows on the default branch (`main`), which is not merged |
| 2026-10-25 | 3 | D24 applied with Go 1.26.10 instead of 1.25.x (approved amendment) | Go 1.25 is end-of-life: x/net 0.60.0 needs Go 1.26 and four stdlib CVEs have no 1.25 fix |
| 2026-10-25 | 3 | podinfo's own `replace golang.org/x/text => v0.19.0` raised to v0.42.0 | The upstream security floor pinned x/text below the current fixes, whatever `require` said |
| 2026-10-26 | 4a | Audit log in `agents/flowguard/audit.py`, standard library only (SQLite), every row stamped with scenario id and ablation level; L0 rule decisions are events, the proposal/gate/approval/execution tables wait for Step 5 | The stdlib-only laptop scripts must write to it; L0 has no agent proposals |
| 2026-10-26 | 4a | CI runners pinned to `ubuntu-24.04`; runtime image `alpine:3.24` (branch tag, so rebuilds take Alpine security patches) | `ubuntu-latest` moves under us; Alpine 3.20 is end-of-life |
| 2026-10-26 | 4a | The L0 security gate fails closed: a run without a `scan-counts` annotation is blocked like one with findings | A missing scan must never look like a clean one |
| 2026-10-26 | 4a | The deployer releases to prod by reading both Rollouts (stateless): a staging-promoted commit not yet in prod is released on the next poll; `--no-prod` for staging-only scenario checks | Restarts must neither lose nor repeat a prod release |
| 2026-10-26 | 4a | Fault settings go in the pod template as podinfo flags (`deploy-version fault -ErrorRate ...`), which podinfo also accepts as `PODINFO_FAULT_*` env | deploy-version replaces the whole command on every deploy, so a good deploy always clears them; an env list could leave an empty entry that changes the template hash |
| 2026-10-26 | 4a | Injected 500s carry a JSON body with the pod's hostname | The load generator can attribute injected errors to canary or stable pods (gap noted under D21) |
| 2026-10-26 | 4a | Recorder saves raw Prometheus samples (a `selector[window]` instant query), not resampled ranges | Replays can compute any rate, ratio or quantile later, exactly as live |
| 2026-10-26 | 4a | Scenario families: release (config or commit delivery), security (commit), ci (commit); runtime rejected until the Monitor step; only staging | Each family maps to one DSO; prod stays out of experiments |
| 2026-10-26 | 4a | CI failure classes = `contracts.RootCause`: code (broken handler), dependency (missing module), environment (Go 1.22 build image), flaky_test (random test, p=0.8), infrastructure (unreachable registry) | One ground-truth vocabulary from generator to Diagnose |
| 2026-10-26 | 4a | LogChunks sample: 150 of the 797 labelled failed logs, seeded (20261026), round-robin over the 80 repositories (29 languages, at most 2 per repository); the committed CSV holds ids and SHA-256 only | Reproducible, no single project dominates, no third-party log text in git |
| 2026-10-26 | 4a | LogSieve is downloaded whole (1 GB, MD5 verified) and only on request (`--with-logsieve`) | Its host's signed URLs refused range requests, so the labelled subset cannot be fetched alone |
