# ci

The app pipeline's scripts and the laptop-side deployer (step 3). The workflow itself is
`.github/workflows/app-ci.yml`; it runs only for changes under `app/` and `ci/` (and the
workflow file), so agent, infra and docs commits never build the app. It runs on `dev`, `main`
and `scenario/**`.

## Scenario branches

Check and evaluation commits (marker changes, injected faults) never go to `dev`. They go to a
throwaway branch `scenario/<id>`, which app-ci builds like `dev`; a deployer started with
`--branch scenario/<id>` deploys it, and the branch is deleted afterwards. Only real code
changes are pushed to `dev`.

## Pipeline

```
plan -> build -> unit -> optional jobs (planned) -> scans -> push ghcr.io/<owner>/flowguard-podinfo:<sha>
```

| Job | Always runs | What it does |
| --- | --- | --- |
| plan | yes | `plan.py` picks the optional jobs and their order |
| build | yes | Docker image of `app/podinfo` (linux/amd64), handed to later jobs as an artifact |
| unit | yes | `go test ./...` |
| optional: lint | planned | `go vet`, `gofmt` |
| optional: integration | planned | `integration-test.sh`: runs the image, checks the API and the metrics port |
| optional: sbom | planned | SPDX SBOM of the image (syft v1.54.1), uploaded as an artifact |
| optional: multiarch | planned | Cross-build for amd64 and arm64 with `Dockerfile.xx` (no push) |
| scans | yes | Trivy v0.75.0 (image, HIGH and CRITICAL), Gitleaks v8.30.1 (repository files), SonarCloud if a `SONAR_TOKEN` secret exists, otherwise skipped with a notice |
| push | yes | The scanned image, tagged with the full commit SHA |

Optional jobs run one at a time in the planned order. Scans **report, they do not block**:
reports go to the run summary and the `scan-reports` artifact, and the allow / review / block
decision belongs to the Security agent (DSO4, Step 9) and its rule baseline (Step 4).

## Planning (D1, D2)

- `optimizer` mode (default): reads `optimizer/model.json`, the committed Optimizer model. Until
  Step 10 it is a stub that runs every optional job in the fixed order. A missing, broken or
  unknown model also runs everything: planning can never skip a check.
- `baseline` mode (DSO5 baseline): the fixed order from `optional-jobs.json`, each job filtered
  by its path rules. Select it with the repo variable `FLOWGUARD_CI_MODE=baseline` or the
  manual-run input.

Build, unit and scans are not in `optional-jobs.json`, so no plan can drop them.

## Laptop deployer (D1, D23, DSO4 baseline)

```powershell
py ci\deployer.py run            # poll and deploy; Ctrl+C to stop
py ci\deployer.py run --once     # one poll
py ci\deployer.py status --sha <commit>
py ci\deployer.py gate --sha <commit>           # scan counts and the L0 security verdict
py ci\deployer.py --branch scenario/<id> --no-prod run   # a scenario branch, staging only
```

It polls the GitHub API for the newest successful `app-ci` run on `dev`, then:

1. **L0 security gate (DSO4 baseline).** It reads the run's `scan-counts` annotation
   (`trivy_high=… trivy_critical=… gitleaks=…`). Any finding, or no counts at all, blocks the
   image: a `security_decision` event, no deployment.
2. **Staging canary.** It starts the load generator, deploys the image with
   `infra/scripts/deploy-version` (canary 20/50/100 under L0), waits for promotion or abort and
   stops the load.
3. **Prod canary (D23, L0).** After a promoted staging canary the same image goes through the
   prod canary the same way (`--no-prod` stops after staging).

Each step goes to `evaluation/runs/deployer-events.jsonl` and to the audit log
(`agents/flowguard/audit.py`, `state/audit.db`). After each release it deletes release Secrets no
ReplicaSet uses any more (D25). The deployed commit is read from each Rollout, so a restart never
deploys twice and still releases a staging-promoted commit to prod.

Configuration: `.env` at the repo root (see `.env.example`). `GITHUB_TOKEN_READ` is optional; a
fine-grained token with read access to this repository's Actions raises the limit from 60 to
5,000 API requests per hour. Unchanged answers are revalidated with ETags.

Tests: `pytest ci/tests` (the GitHub API is a local fake server; kubectl is faked).
