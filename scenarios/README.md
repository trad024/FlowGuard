# scenarios

Scenario specs and the runner that executes, records and cleans them up (Step 4a).

```powershell
py scenarios\runner.py --validate scenarios\dev\*.yaml     # check every spec
py scenarios\runner.py scenarios\dev\release-errors-05.yaml # run one scenario
```

Each run gets a run id (`<spec id>-<UTC time>`), writes audit events under it, and is recorded
into `evaluation/recordings/<run id>/` (see `evaluation/README.md`) with the ground truth and
what L0 did. Needs the cluster, Python with PyYAML, and for commit scenarios a clean `dev`
checkout and push access.

- `dev/`: the development scenarios. Developers tune on these only.
- `heldout/`: gitignored. Reserved for the team member who writes the held-out set (D12); its
  specs never reach `dev` before Step 11.

## Spec

```yaml
id: release-errors-05            # = file name; lowercase, digits, dashes
family: release                  # release (DSO3) | security (DSO4) | ci (DSO2)
description: 5% of user requests answer 500
environment: staging             # scenarios never touch prod
fault:
  kind: http_errors              # see the table below
  delivery: config               # config | commit
  error_rate: 0.05               # 0..1, share of faulty requests answered 500
  latency_ms: 0                  # added to faulty requests
  jitter_ms: 0                   # plus a uniform 0..jitter_ms
  fraction: 1.0                  # share of requests that are faulty
intensity: 0.05                  # the graded knob, for the analysis
timing: {warmup_s: 30, observe_s: 600, cooldown_s: 20}
load: {rate: 10}                 # requests per second during the scenario
ground_truth:
  bad_release: true
  expected_l0: abort             # promote | abort | block | ci_failure (what L0 should do)
  root_cause: null               # ci family: the DSO2 class
```

| Family | Fault kinds | Delivery |
| --- | --- | --- |
| `release` | `http_errors`, `latency`, `none` (good control) | `config`: the same image with fault flags in the Rollout's pod template, which starts a canary (D19 amended). `commit`: the fault turned on by default in podinfo's code, pushed on `scenario/<run id>`, through CI, the L0 security gate and the staging canary |
| `security` | `leaked_secret` | `commit`: a hard-coded API key; CI's Gitleaks finds it and the L0 gate must block the image |
| `ci` | `code`, `dependency`, `environment`, `flaky_test`, `infrastructure` | `commit`: one change per DSO2 class (`ci_failures.py`); the failed CI run is the scenario |
| `runtime` (DSO1) | reserved | rejected until the Monitor step |

Expected L0 outcomes are not always "catch it": `release-errors-0005` (0.5% 5xx, below the
frozen 1% threshold) is a real regression L0 is expected to promote, which the agents should
catch.

## Clean-up

Config scenarios restore the pre-scenario pod template (a promoted fault goes through a
canary back, with load). Commit scenarios always switch back to `dev` and delete the scenario
branch, also after an error. Nothing is pushed to `dev`.

## Live results, 2026-10-26 (all 12 dev specs, L0)

| Scenario | Delivery | Expected | L0 did |
| --- | --- | --- | --- |
| release-good-control | config | promote | promoted |
| release-errors-05 | config | abort | aborted |
| release-errors-0005 | config | promote (known L0 miss) | promoted |
| release-latency-800 | config | abort | aborted |
| release-latency-300-tail | config | abort | aborted |
| release-commit-errors-10 | commit | abort | aborted (82 s) |
| security-leaked-secret | commit | block | blocked (gitleaks=1) |
| ci-code, -dependency, -environment, -flaky-test, -infrastructure | commit | ci_failure | failed in the expected job (unit, build, build, unit, optional integration) |
