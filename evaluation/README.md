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

`monitor_replay.py` runs the Monitor on dev recordings (folders or zips, read in place and
never copied; held-out paths are refused) at L0 (the frozen alert rules as Prometheus evaluated
them, read exactly as `l0_baseline.py` reads them) and L1 (L0 plus the detectors):

```powershell
py evaluation\monitor_replay.py fit <dev recordings> <normal traffic>     # writes agents\flowguard\models\monitor.json
py evaluation\monitor_replay.py score <dev recordings> <normal traffic>   # writes runs\dso1-replay.json
```

Runtime recordings without an ALERTS series (made before D33) are listed and not scored.
Incidents with an L0 alert already firing before the fault (left over from the previous run)
are kept in the primary result, left out of a sensitivity result and of the tuning. CI: L0 is
the rule frozen 2026-10-28 (`CI_L0`: a job longer than 3x its dev median, or 3 failed runs in a
row on a branch); L1 adds the duration detector.

`fit` uses the first half of each normal-traffic recording for the reference profile and the
Isolation Forest, then tunes a small grid (z threshold, forest quantile, persistence) for L1
recall on the dev runtime incidents with the detector's own false alerts on that half under 0.5
per hour; CI bounds come from the successful jobs of non-pipeline runs. `score` gives recall
(Wilson), median MTTD and false alerts per hour (exact Poisson) per level and per band, scored
on the normal-traffic half the model never saw, plus the paired L0 vs L1 comparison (exact
McNemar, bootstrap interval of the MTTD gain). Dev recall is in-sample (the grid was tuned on
those incidents); H1 is tested on the held-out set at Step 11.

### DSO1 on the dev recordings (dev, in-sample, 2026-10-29)

The model in `agents/flowguard/models/monitor.json` (frozen, its SHA-256 pinned in
`agents/tests/test_monitor.py`) was fitted on the first half of the 4-hour normal-traffic
window r-24e489bc; the grid chose z threshold 5.0, forest quantile 0.99, persistence 1 **on the
same 18 runtime incidents it is scored on**. False alerts are scored on the other half only:
**2 hours the model never saw**. Recordings: the first dev delivery (runtime incidents, CI),
recordings-2 (8 releases, no runtime incident) and r-24e489bc; r-57130916 is invalid (host
suspended) and the 4 runtime recordings made before the alert states were recorded are not
scored (D50).

| Runtime | L0 (frozen alert rules) | L1 (L0 + detectors) |
|---|---|---|
| Recall | 10/18, 55.6% (33.7 to 75.4) | 17/18, 94.4% (74.2 to 99.0) |
| above | 6/6 | 6/6 |
| near | 3/6 | 6/6 |
| below | 1/6 | 5/6 |
| Median MTTD | 73.8 s | 15.0 s |
| False alerts per hour (exact Poisson 95%) | 0 in 2.0 h: 0 (0 to 1.85) | 0 in 2.0 h: 0 (0 to 1.85) |

Paired: 7 incidents only L1 detects, 0 only L0, exact McNemar p = 0.016; median MTTD gain on
the 10 both detect 54.3 s (bootstrap 95%: 9.3 to 65.8 s).

| Sensitivity: without the 3 incidents that started under a leftover L0 alert | L0 | L1 |
|---|---|---|
| Recall | 7/15, 46.7% (24.8 to 69.9) | 14/15, 93.3% (70.2 to 98.8) |
| Paired | McNemar 7 vs 0, p = 0.016; MTTD gain 58.3 s (52.3 to 73.3) | |

CI: the one slow pipeline run is caught at L0 and L1 alike (MTTD 499 s from the run's first
job), 0 false flags in 2 runs; the CI detector needs 5 runs of history and has 2, so it cannot
fire yet.

Caveats: recall and MTTD are in-sample (the grid was chosen on these 18 incidents); the false
alert bound (1.85 per hour) is above DSO1's target of 1 per hour, which 2 unseen hours cannot
show either way (0 alerts over about 3.7 unseen hours would); every number is per dev data, H1
is tested on the held-out set.

P1 stress thresholds (`agents/flowguard/monitor.py`, frozen), set by the rule fixed before the
node series were seen (per recording, the median ratio against allocatable; the midpoint
between the highest unstressed and the lowest stressed recording when they separate) on the
recordings with node series (`monitor_replay.py p1`): CPU 0.507 (unstressed medians 0.022 and
0.023, stressed 0.990 to 0.992, separated); memory 0.90, final: a fixed choice, not set from
data, since every recorded node stress is CPU stress and no recording is memory-stressed
(memory medians 0.154 to 0.165 throughout); it is untested on real memory stress (none was
recorded), which the report states as a limitation; restarts: any restart of a non-canary pod
in the last 5 minutes (none in any recording). One unstressed release (r-68579c3d) peaks at 0.992 CPU for a moment: P1 answers from
the latest poll, so such a peak reads as stress.

## DSO3 by replay (D15, step 8)

`release_replay.py` runs Release on the dev release recordings (folders or zips; held-out
paths are refused) inside the real orchestrator, at L3 (Release alone) and L4 (with P1), and
L0 on the same recording (D54): `l0_canary.py` replays the frozen `analysis-l0.yaml` (its
settings read from the file, its two queries evaluated as Prometheus 3 does) after each step's
30 s pause, and is checked against L0's live outcome where one was recorded:

```powershell
py evaluation\release_replay.py score evaluation\recordings       # writes runs\dso3-replay.json
py evaluation\release_replay.py calibrate evaluation\recordings   # also writes agents\flowguard\models\release-calibration.json
```

The canary's steps come from the Rollout's own step changes when they were logged
(`state/rollout-steps.jsonl`, every 5 s), otherwise from the pods up per pod-template hash
(moved half a scrape earlier: a pod is Ready before its first `up` sample); `score` lists the
recordings on that fallback (`step_source_fallback`). Release looks where L0 measures (30 to 120 s
into each step) on 30 s windows of per-hash counts since the 20% step began. Release
scenarios are recorded under the agents-mode Rollout (D54), so each canary runs its full
window; on a recording made under the L0 Rollout a level that has not decided where the
canary stopped is "truncated" (not caught), and `needs_rerecording` lists those recordings
with their spec files. `test_l0_canary.py` checks the offline L0 against the live outcome on
the dev recordings when `FLOWGUARD_DEV_RECORDINGS` points at them (skipped otherwise). `score` gives bad releases caught before 100% and false
rollbacks (Wilson), escalations, wrong decisions and the median time to rollback, per level
and per band; L0 vs L3 and L3 vs L4 paired (exact McNemar, bootstrap interval of the rollback
time gain); and L3's wrong-decision rate on the node-stress recordings (H4's count, D44).
`calibrate` fits the conformal calibrator of Release's proposals (D9) on the L3 promote and
rollback decisions, labelled by the recording, and reports coverage leave-one-recording-out.
Everything is labelled "dev, in-sample".

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
