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

## External datasets (D14, step 4a)

```powershell
py evaluation\datasets.py download                  # LogChunks, MD5-verified, extracted
py evaluation\datasets.py download --with-logsieve  # LogSieve Data.zip (1 GB): not used, see below
py evaluation\datasets.py sample                    # the 150-log sample (seed 20261026)
py evaluation\datasets.py sheet                     # the blank labelling sheet
```

Labelling (D26a): one labeller fills a copy of the blank sheet for all 150 logs. At least 7
days after finishing, `py evaluation\relabel.py --first <first pass>.csv --finished <date>`
writes a blind sheet for a seeded random 50 (seed 20261027, shuffled, no first answers); the
intra-rater kappa is Step 4b.

Raw files stay in the gitignored `datasets/raw/`. Committed: `datasets/logchunks-sample-150.csv`
(identifiers and SHA-256, no log text) and `datasets/labelling/` (blank sheet and guide).
LogSieve's `Data.zip` holds raw GitHub Actions run logs (111,172 files, 9 GB unpacked, not
extracted); the line-labelled logs D14 refers to were not found in it, so it was deleted and
LogSieve is not used unless a small labels file is identified (D14).
