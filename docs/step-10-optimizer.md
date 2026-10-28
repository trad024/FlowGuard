# Step 10: CI Optimizer

The CI `plan` job now reads two trained LightGBM models from `ci/optimizer/model.json`:
a duration regressor and a binary job-failure classifier. Each prediction uses commit paths,
file types, added/deleted lines, dependency changes, change kind and job identity. Branch
names, commit messages, run identifiers and outcomes are never prediction features.

Training uses LightGBM 4.6.0, scikit-learn 1.7.2 and SHAP 0.49.1. CI inference still uses
**only the standard library**: numeric trees are exported to a validated JSON format, never
pickle or executable model code. Every job prediction includes exact interventional tree
SHAP values, expected value and the top three features. Duration explanations use seconds;
failure explanations use raw log-odds. Depth-two trees make exact feature enumeration cheap.
The training script checks portable predictions against native LightGBM on every validation
observation, and checks explanations against `shap.TreeExplainer` on three validation rows
per model. Background rows come only from training.

## Safety and integration

- Build, unit and scans remain outside the optional catalog and cannot be skipped.
- P2 forces SBOM after dependency changes or a previous security flag; scans always run.
- The workflow reads prior completed scan-count annotations with read-only Actions/checks
  permissions. Missing history, denied API access, failed scans or unknown status run all
  optional jobs. A first run on a new branch therefore runs everything.
- A job can be predictively skipped only below the fixed 0.01 failure threshold, with at least
  eight observed training outcomes, no observed training failures for that job, known change
  size and all features within observed training ranges. These are conservative heuristics,
  not a statistical guarantee about future failures.
- The existing path filters may omit unrelated optional jobs. Retained optional jobs are
  ordered by predicted failure probability divided by predicted duration. Baseline mode
  retains its fixed order and original path filters.
- Missing, unreadable, corrupt, nonfinite, overly deep or incompatible models run every
  optional job. Legacy `run-all` models remain supported.

The workflow passes `git diff --numstat` for change size. Missing stats disable predictive
skipping. Runtime agent stubs, the gate, executor, orchestrator and frozen files are unchanged.
This task implements the existing CI-side inference path specified by D1.

## Development data and measured results

`ci/optimizer/dev-history.jsonl` records metadata from 30 existing app-ci development runs,
collected read-only from `trad024/FlowGuard` on 2026-10-28. It contains commit file metadata,
job timings/conclusions and prior scan status, not log contents or artifact payloads.
The collector accepts `dev` and explicit development branch prefixes; it does not create
branches, commits or workflow runs. No extra CI history was generated.

The first 60% of distinct commit SHAs are training; the last 40% are validation, in timestamp
order. No commit occurs on both sides. The JSON model records the source checksum, exact run
IDs, split timestamps, seed and package versions. Skipped/cancelled jobs are unobserved and
are never labelled successful. The model was not refit on validation after these measurements.

| Measurement | Result |
|---|---:|
| Training commits / observed jobs / failures | 18 / 108 / 4 |
| Validation commits / observed jobs / failures | 12 / 72 / 3 |
| Validation duration MAE | 8.5664 s |
| Validation failure ROC AUC (pooled jobs) | 0.7705 |
| Observed validation failures skipped | 0/3 (Wilson 95% CI 0–56.15%) |

Per-job validation results:

| Job | Observed jobs | Failures | MAE, seconds | AUC |
|---|---:|---:|---:|---:|
| build | 12 | 1 | 15.2087 | 1.0 |
| unit | 11 | 1 | 11.0967 | 0.9 |
| scans | 9 | 0 | 4.6179 | undefined |
| lint | 10 | 0 | 5.6283 | undefined |
| integration | 10 | 1 | 6.1246 | 0.0 |
| sbom | 10 | 0 | 2.4768 | undefined |
| multiarch | 10 | 0 | 12.8356 | undefined |

An AUC is undefined with only one class. The integration ranking is poor on its single
validation failure; its historical training failure disables predictive skipping for that
job. The pooled AUC is not a substitute for these per-job results.

**Projected saving on validation `dev` commits:** 8/8 have complete observed durations for
both plans. The fixed-order/path-filter baseline totals 1,038 job-seconds; the Optimizer
totals 1,036: **2 seconds, or 0.1927% saved**. None of these eight commits failed, so this
subset cannot estimate failure recall. Mandatory jobs account for 975 of those 1,038 baseline
seconds, leaving 63 optional seconds. Even removing every optional job would save at most
**6.0694%** on this sample, so it cannot demonstrate the 20% target under the safety rules.
Across the broader validation set, 9/12 runs have
complete timings for both plans: baseline 1,248 job-seconds versus Optimizer 1,380,
**10.5769% more time**, mainly because unknown prior scan status runs everything.
The three incomplete runs are excluded from duration comparison but included in the
observed-failures-skipped count; no durations are imputed. The machine-readable results
are `ci/optimizer/dev-results.json`.

These are sums of observed job execution seconds under alternative plans, not measured
pipeline wall-clock changes: queue delays, matrix scheduling, setup and cross-run cache
effects can differ. The **20% duration reduction target is not met**. No reliable time-to-
first-failure improvement is established. This is a small development experiment, not
final evaluation or evidence of zero future failures. More diverse dev runs and repeated
paired baseline/optimizer runs are required before a stronger performance claim.

## Reproduce

Use Python 3.11. Training extras are separate from runtime and unit-test dependencies:

```powershell
pip install -e ".[dev]"
pip install -r ci/optimizer/requirements.txt
# Optional fresh read-only collection (requires an authenticated GitHub CLI):
python ci/collect_optimizer.py --repo trad024/FlowGuard
python ci/train_optimizer.py
python ci/plan.py --all-changed
ruff check .
ruff format --check .
pytest
```

To inspect a known commit, supply `--changed-files`, `--diff-stats` and
`--security-status clean|flagged|unknown`; the default status is deliberately `unknown`.
Ruff follows the approved D38 pin, 0.17.1, for consistent CI/local formatting. The committed history snapshot
reproduces the measured model and metrics; fresh collection can change the results.

Verified: real read-only history collection; actual model training; native/portable prediction
parity; reference SHAP parity; focused tests for features, P2, protected jobs, schema corruption,
fallbacks, chronology, skip support, ordering and annotation handling. The PR records final
full-suite results. A CLI smoke run under Python `-S` (no site packages) produced seven explained
job predictions with P2 enforced in 0.203 seconds on this machine. This is a smoke timing,
not a latency benchmark. Not verified: live execution of the changed workflow,
paired live pipeline savings, final evaluation or integration tests (skipped by default).
No frozen-file changes or proposals are needed.
