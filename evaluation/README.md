# evaluation

Data collection and, later, the comparison reports (agents vs baseline, Steps 4b and 11).

## Load generator (step 2)

`loadgen.py`: fixed-rate load on podinfo with one JSON line per request (time, namespace,
status, latency, pod) in the gitignored `runs/` folder, and a per-pod-template-hash summary.
Injected faults (D19) answer with the pod's hostname, so failed requests are attributed too.

## Recorder (D15, step 4a)

`recorder.py` saves the raw inputs of a time window into `recordings/<run id>/` (gitignored):

| File | Content |
| --- | --- |
| `manifest.json` | window, git commit, Prometheus selectors, per-source counts, and `valid` (false when the host slept during the run; such runs are not scored). Nothing about what was injected |
| `ground_truth.json` | the only file with the ground truth (D25): `ground_truth`, the spec, what L0 did (`observed`, `l0_correct`), `valid` |
| `prometheus/<name>.json` | raw samples (a `selector[window]` query, not resampled) for request counters, latency buckets and sums, `up`, CPU, memory, restarts |
| `loadgen.jsonl` | per-request lines inside the window |
| `deployer-events.jsonl`, `audit-events.jsonl` | deployer and audit-log events inside the window |
| `ci/<run>/` | run, jobs with step results, annotations; job logs when the token may read them |

The scenario runner calls it for every scenario; it also runs alone:

```powershell
py evaluation\recorder.py --scenario manual-1 --start 2026-10-26T18:00:00Z --end 2026-10-26T18:10:00Z --loadgen evaluation\runs\<file>.jsonl
```

CI job logs need a GitHub token with read access to the repository's Actions (a fine-grained
token with *Actions: Read*). Without it the recording marks them unavailable and keeps the
jobs, steps and annotations.

## Ground truth and the information firewall (D25)

`groundtruth.py` keeps what only the experiment knows in `runs/groundtruth.db` (gitignored;
`FLOWGUARD_GROUNDTRUTH_DB` overrides): one row per run (spec, ground truth, what L0 did,
validity) and every injection event (scenario started, fault injected or cleared, node
stress, L0 outcome). The scenario runner and the recorder write it; nothing under `agents/`
opens it (`agents/tests/test_firewall.py`). The audit log, which agents will read, lives
apart in `state/audit.db` and holds only what a real system would see: deployments, gate and
security decisions, CI results.

## Audit metrics (step 4a)

`py -m flowguard.audit_metrics [--scenario <run id>]` (with `agents` on `PYTHONPATH`):
deployment outcomes and time to promotion per environment, security decisions (once per
distinct commit and decision), executions while the gate required a human without an approval,
whatever the tier (DSO6, must stay 0), executions after a reject, approvals per incident.

## DSO1 by replay (D15, step 6)

`monitor_replay.py` runs the Monitor on dev recordings (a folder, or the zip they arrive in;
held-out paths are refused) at L0 (the frozen alert rules as Prometheus evaluated them, read
exactly as `l0_baseline.py` reads them) and L1 (L0 plus the runtime detector):

```powershell
py evaluation\monitor_replay.py fit evaluation\recordings     # writes agents\flowguard\models\monitor.json
py evaluation\monitor_replay.py score evaluation\recordings   # writes runs\dso1-replay.json
```

`fit` uses the first half of each normal-traffic recording for the reference profile and the
Isolation Forest, then tunes a small grid (z threshold, forest quantile, persistence) for L1
recall on the dev runtime incidents with the detector's own false alerts on that half under 0.5
per hour; CI bounds come from the successful jobs of non-pipeline runs. `score` gives recall
(Wilson), median MTTD and false alerts per hour (exact Poisson) per level and per band, scored
on the normal-traffic half the model never saw, plus the paired L0 vs L1 comparison (exact
McNemar, bootstrap interval of the MTTD gain). Dev recall is in-sample (the grid was tuned on
those incidents); H1 is tested on the held-out set at Step 11.

## External datasets (D14, step 4a)

```powershell
py evaluation\datasets.py download                  # LogChunks, MD5-verified, extracted
py evaluation\datasets.py download --with-logsieve  # LogSieve Data.zip (1 GB): not used, see below
py evaluation\datasets.py sample                    # the 150-log sample (seed 20261026)
```

LogChunks is used for evidence localization only (D32): no manual labelling. The lines
Diagnose quotes are compared with each log's human-labelled chunk (see
`datasets/labelling/README.md`).

Raw files stay in the gitignored `datasets/raw/`. Committed: `datasets/logchunks-sample-150.csv`
(identifiers and SHA-256, no log text) and `datasets/labelling/README.md` (how LogChunks is used).
LogSieve's `Data.zip` holds raw GitHub Actions run logs (111,172 files, 9 GB unpacked, not
extracted); the line-labelled logs D14 refers to were not found in it, so it was deleted and
LogSieve is not used unless a small labels file is identified (D14).
