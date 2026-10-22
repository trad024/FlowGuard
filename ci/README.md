# ci

The app pipeline's scripts and the laptop-side deployer (step 3). The workflow itself is
`.github/workflows/app-ci.yml`; it runs only for changes under `app/` and `ci/` (and the
workflow file), so agent, infra and docs commits never build the app.

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

## Laptop deployer (D1)

```powershell
py ci\deployer.py run            # poll and deploy; Ctrl+C to stop
py ci\deployer.py run --once     # one poll
py ci\deployer.py status --sha <commit>
```

It polls the GitHub API for the newest successful `app-ci` run on `dev`. When staging runs a
different commit, it starts the load generator, deploys the image with
`infra/scripts/deploy-version` (canary 20/50/100 under L0), waits for promotion or abort, stops
the load and logs each step to `evaluation/runs/deployer-events.jsonl`. The deployed commit is
read from the Rollout itself, so a restart never deploys twice.

Configuration: `.env` at the repo root (see `.env.example`). `GITHUB_TOKEN_READ` is optional; a
fine-grained token with read access to this repository's Actions raises the limit from 60 to
5,000 API requests per hour. Unchanged answers are revalidated with ETags.

Tests: `pytest ci/tests` (the GitHub API is a local fake server; kubectl is faked).
