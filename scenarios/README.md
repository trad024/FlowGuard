# scenarios

Scenario specs and the runner that executes, records and cleans them up (Step 4a).

```powershell
py scenarios\runner.py --validate scenarios\dev\*.yaml     # check every spec
py scenarios\runner.py scenarios\dev\release-errors-05.yaml # run one scenario
py scenarios\verify_templates.py                           # check the CI failure templates locally
```

Each run gets an opaque run id (`r-<8 hex>`), writes audit events under it, and is recorded
into `evaluation/recordings/<run id>/` (see `evaluation/README.md`) with the ground truth and
what L0 did. Needs the cluster, Python with PyYAML, and for commit scenarios a clean `dev`
checkout and push access.

- `dev/`: the development scenarios. Developers tune on these only.
- `heldout/`: gitignored. Reserved for the team member who writes the held-out set (D12); its
  specs never reach `dev` before Step 11.

## Spec

```yaml
id: release-errors-05            # = file name; lowercase, digits, dashes
family: release                  # release | security | ci | runtime | pipeline
description: 5% of user requests answer 500
environment: staging             # scenarios never touch prod
fault:
  kind: http_errors              # see the table below
  delivery: config               # config | commit | runtime
  error_rate: 0.05               # 0..1, share of faulty requests answered 500
  latency_ms: 0                  # added to faulty requests
  jitter_ms: 0                   # plus a uniform 0..jitter_ms
  fraction: 1.0                  # share of requests that are faulty
node_stress: {cpu_workers: 12, cpu_request: '5.5'}  # optional: busy-loop batch pod; its CPU request sets its weight (D44)
intensity: 0.05                  # the graded knob, for the analysis
timing: {warmup_s: 30, observe_s: 600, cooldown_s: 20}
load: {rate: 10}                 # requests per second during the scenario
ground_truth:
  bad_release: true
  expected_l0: abort             # promote | abort | block | ci_failure | detect
  root_cause: null               # ci family: the DSO2 class
```

| Family | Fault kinds | Delivery |
| --- | --- | --- |
| `release` (DSO3) | `http_errors`, `latency`, `none` (good control, optionally under node CPU stress, H4) | `config`: the same image and command with a new release Secret (`podinfo-release-<random>`, envFrom) holding the fault settings, which starts a canary (D19 amended, D25); good releases get one with empty values, so the pod spec never tells them apart. `commit` (`http_errors` only): an ordinary code defect (D30), a periodic refresh in `info.go` that parses the wrong `/proc/self/stat` field, so one request per pod per interval answers 500; the interval comes from the target error rate and the load and is at least 5 s, so podinfo's unit tests never trigger it. Pushed on `scenario/<run id>`, through CI, the L0 security gate and the staging canary |
| `security` (DSO4) | `leaked_secret`, `vulnerable_dependency` (must block), `low_severity_finding` (must allow) | `commit`: a hard-coded API key; `golang.org/x/text` pinned back to v0.19.0 (HIGH CVEs); `gorilla/websocket` v1.5.3 -> v1.5.0 (no HIGH or CRITICAL finding) |
| `ci` (DSO2) | `code`, `dependency`, `environment`, `flaky_test`, `infrastructure` | `commit`: one template per run from `ci_failures.py`; the failed CI run is the scenario |
| `runtime` (DSO1) | `error_burst`, `latency_spike`, `memory_leak` | `runtime`: settings PUT to the staging-only `/fault` admin endpoint (metrics port) of a share of the running pods, cleared after `incident_s`; no deployment |
| `pipeline` (DSO1, CI detector) | `slow_step` | `commit`: a test that waits `added_s`, so one CI job is slow but passes |

`expected_l0: detect` means L0 has no check for it (runtime incidents, a slow CI step): the
outcome is judged against the agents in Step 4b, so `l0_correct` is null. Expected L0 outcomes
are not always "catch it": `release-errors-0005` (0.5% 5xx, below the frozen 1% threshold) is
a real regression L0 is expected to promote, which the agents should catch.

## Information firewall (D25)

The runner writes the spec, the ground truth and every injection event to the ground-truth DB
(`evaluation/groundtruth.py`), never to the audit log. The audit log gets only what a real
system would see: for a config release, the same `deploy_started` and `deploy_finished` the
deployer writes (also for the restore); for commit releases, the deployer's own events and the
security gate's decision; for runtime incidents, nothing. The `/fault` admin endpoint is for
runtime incidents only: it is on the metrics port, which no Service exposes, and the only way
in, `kubectl exec`, is a prohibited catalog action, so agent tools have no route to it.

## Held-out set (D26b)

`heldout.py` draws the held-out scenarios from the frozen pre-registration
(`docs/preregistration.yaml`) with a secret seed: `commit-seed` stores the seed in `.env` and
writes only its SHA-256 to `heldout-seed.sha256` (committed); `generate --confirm-step-11`
refuses to run before the pre-registration is frozen, the agent code is tagged `eval-freeze`
(with `agents/` unchanged since) and Step 11 is confirmed. It writes the specs and a seeded
`run-order.txt` to the gitignored `heldout/`. Release and runtime faults are drawn within an
intensity band (below, near or above the L0 thresholds), recorded as `band` in the ground
truth. Nothing has been generated.

## No label leakage

Diagnose reads CI logs, so nothing a generated failure adds may name its class or give the
experiment away. Branches are `scenario/r-<random>`, commit messages are neutral ("Bump
websocket", "Return 200 from echo"), and test, file and host names are realistic.
`leakage.py` checks, and `tests/test_leakage.py` runs it on every template and commit
scenario and on every log `verify_templates.py` captured:

- class words (`code`, `dependency`, `environment`, `flaky`, `infrastructure`) and experiment
  words (`scenario`, `flowguard`, `simulated`, `injected`, `root`, `cause`, `label`, ...) in
  any spelling (`TestFlakyCache`, `flaky_test`, `FlowGuard`);
- in logs, only on lines a normal run's log (baseline) does not have, after masking tool
  phrases every class prints (`exit code`, `status code`).

## CI failure templates

31 templates, 6 or 7 per class, each recorded as `template_id` with its `failing_job` in the
ground truth. `verify_templates.py` applies each one to an export of `HEAD` and runs the check
behind its failing job with Docker (build, `go test`, `go vet`, build plus the integration
test), with opaque image tags. Results go to the gitignored `evaluation/runs/template-checks/`.

## Clean-up and validity

Config scenarios restore the pre-scenario pod template (a promoted fault goes through a canary
back, with load). An aborted or timed-out restore is retried once, with a new load generator
if the old one died. Commit scenarios always switch back to `dev` and delete the scenario
branch, also after an error. Node-stress pods are deleted. Nothing is pushed to `dev`.

If the host sleeps during a run, load and metrics stop and L0 sees no data. The runner finds
such episodes in the load log (no request, or a request stalled, for 60 s or more), records
them as `observed.host_suspended`, sets `valid: false` and `l0_correct: null`. Invalid runs
are excluded from scoring.

## Live results (L0)

| Scenario | Delivery | Expected | L0 did |
| --- | --- | --- | --- |
| release-good-control | config | promote | promoted |
| release-errors-05 | config | abort | aborted |
| release-errors-0005 | config | promote (known L0 miss) | promoted |
| release-latency-800 | config | abort | aborted |
| release-latency-300-tail | config | abort | aborted |
| release-commit-errors-10 | commit | abort | aborted (82 s); rerun 2026-10-27 as the D30 code defect: CI green, gate allow, aborted in 102 s, canary 10/128 5xx (7.8%), stable 0/891 |
| release-good-node-stress | config, 6 CPU workers | promote | promoted (see note) |
| security-leaked-secret | commit | block | blocked (gitleaks=1) |
| security-vulnerable-dependency | commit | block | blocked (trivy_high=4) |
| security-low-severity-finding | commit | promote | allowed (trivy_high=0), promoted |
| ci-code, -dependency, -environment, -flaky-test, -infrastructure | commit | ci_failure | rerun 2026-10-27 with templates code-06, dependency-06, environment-04, flaky_test-01, infrastructure-01: all failed, with the expected error in the failing job's log; code-06 failed in unit instead of lint (go test runs vet's printf check), fixed and rerun: fails in lint. 33 downloaded job logs scan clean |
| runtime-error-burst | runtime, 30% of pods | detect | 28.6% 5xx on the 3 target pods, 0% on the others |
| runtime-latency-spike | runtime | detect | mean 436 ms during the incident, 29 ms outside |
| runtime-memory-leak | runtime, 512 KB/s on 2 pods | detect | both pods OOMKilled (exit 137) and restarted twice: a crash loop |
| pipeline-slow-step | commit, +120 s | detect | unit job 182 s (normal 18 to 46 s), then promoted |

Note on `release-good-node-stress` (2026-10-27): L0 promoted correctly, but the client-side
p99 reached 5 s (load-generator timeouts) while L0's server-side p99 stayed within 500 ms:
evidence for H4. The host then slept for an hour during the restore canary, which L0 aborted
for lack of data and left staging Degraded; it was recovered by retrying the canary with load.
That run predates the suspend detection, and the restore retry was added because of it.
