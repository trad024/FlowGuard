# Method: Monitor (Step 6) and Release (Step 8)

Draft for the report. Every number below is from the dev recordings and is labelled
"dev, in-sample" where settings were chosen on the same data. Held-out results come at Step 11.
Live runs on the cluster are not described here.

## Shared setting

The agent service polls Prometheus every 30 s (D1). Evaluation replays recorded scenarios
offline (D15): each recording holds the raw Prometheus series of its window, and every ablation
level decides on the same recording. Ground truth sits in each recording's
`ground_truth.json`, which only the scoring side reads (D25). No agent query can name a metric
of FlowGuard's own exporters: every Prometheus selector an agent uses is checked to name one
metric literally and never a `flowguard_*` one.

Rates are reported with their count and a 95% Wilson interval. False alerts per hour use an
exact Poisson interval. Paired comparisons between levels use the exact McNemar test on the
same recordings, and a bootstrap interval for time differences.

## Monitor (Step 6, DSO1)

### What it does

The Monitor watches each namespace (staging, prod) and each pod. It proposes no action. When
it sees an incident, it reports it, and the orchestrator opens an incident record with the
Monitor's detector and score (D49). The Monitor posts evidence that refers to the series
behind it. If the service is mid-canary, the incident goes to Release; otherwise it goes to
Diagnose. The Monitor also answers Release's P1 question: is the node or the cluster under
stress?

### Levels

- **L0:** the five alert rules frozen before any measurement (D33). They fire on:
  - a 5xx share above 5% for 1 minute;
  - p99 above 1 s for 1 minute;
  - an OOM-killed restart;
  - CrashLoopBackOff for 2 minutes;
  - working set above 80% of the memory limit for 2 minutes.
- **L1 and above:** L0 plus two detectors. The ablation ladder is cumulative.

**Runtime detector.** It reads four features per namespace and per pod: 5xx ratio, p99
latency, memory against the limit, and restarts.

1. Each feature gets a rolling z-score against the same series' last 10 minutes. The baseline
   freezes while the entity looks abnormal. A series with too little history uses the
   reference profile fitted on normal traffic.
2. When a z-score passes the z threshold, an Isolation Forest scores the entity's windowed
   z-scores: the current values, their mean over the last minute, and the largest of each.
3. The entity is flagged once that score passes the forest threshold for the persistence
   number of ticks in a row.
4. One episode per namespace gives one incident.

**CI detector.** It flags two things:
- a job slower than its bound, which is its median plus k times the robust spread of its
  past successful durations;
- a failure streak on one branch.

A failed CI run is an incident at every level (D49).

### Frozen settings

- **Runtime model.** `agents/flowguard/models/monitor.json`, fitted on the first half of the
  4-hour normal-traffic window r-24e489bc:
  - z threshold 5.0, forest quantile 0.99 (score threshold 0.672), persistence 1;
  - history 10 minutes, window 4 ticks, seed 0.

  The three tuned settings were chosen by a 27-point grid on the 18 dev runtime incidents (in
  sample). The grid kept the detector's own false alerts on the fitting half under 0.5 per
  hour. A test pins the three settings and the file's SHA-256.
- **CI.** L0 is the rule frozen on 2026-10-28: a job longer than 3 times its dev median, or 3
  failed runs in a row. L1's duration detector uses k = 3 and needs at least 5 runs of history.
- **P1 thresholds:**
  - **CPU 0.507** (ratio against allocatable). It was set by a rule fixed before any node
    series was seen: per recording, take the median, then the midpoint between the highest
    unstressed and the lowest stressed recording. Unstressed medians were 0.022 and 0.023
    (2 recordings) and stressed medians 0.990 to 0.992 (7), so the groups separate fully.
  - **Memory 0.90**, a fixed choice, not set from data. No recording was memory-stressed,
    because every recorded node stress was CPU stress. This threshold is untested on real
    memory stress, a stated limitation.
  - **Restarts:** any restart of a non-canary pod in the last 5 minutes. No recording had one.

### Dev results (dev, in-sample)

**Data:**
- 18 runtime incidents, from the first dev delivery. The 4 recordings made before the L0 alert
  states were recorded are not scored (D50).
- False alerts are scored on the second half of r-24e489bc: 2 hours the model never saw.
- r-57130916 is excluded (invalid, host suspended).

| Runtime | L0 | L1 |
|---|---|---|
| Recall | 10/18, 55.6% (33.7–75.4) | 17/18, 94.4% (74.2–99.0) |
| above band | 6/6 | 6/6 |
| near band | 3/6 | 6/6 |
| below band | 1/6 | 5/6 |
| Median time to detect | 73.8 s | 15.0 s |
| False alerts per hour | 0 in 2.0 h (0–1.85) | 0 in 2.0 h (0–1.85) |

Paired comparison:
- 7 incidents are detected only at L1 and none only at L0 (exact McNemar p = 0.016).
- On the 10 detected by both, the median gain in time to detect is 54.3 s (bootstrap 95%:
  9.3 to 65.8 s).

A sensitivity analysis leaves out the 3 incidents that started while an L0 alert from the
previous run was still firing. L0 detects 7/15 (46.7%, 24.8–69.9) and L1 detects 14/15 (93.3%,
70.2–98.8), with McNemar 7 vs 0.

For CI, the one slow pipeline run is detected at L0 and L1 alike, 499 s after its first job
started. There were 0 false flags in 2 runs. The CI duration detector cannot fire yet: it has
2 runs of history and needs 5.

**Caveats:**
- Recall and time to detect are in-sample: the grid was chosen on the same 18 incidents.
- The false-alert bound, 1.85 per hour, is above DSO1's target of 1 per hour. Two unseen
  hours cannot show the target is met or missed: 0 alerts over about 3.7 hours would.
- There are 6 incidents per band, so the per-band intervals are wide.

## Release (Step 8, DSO3)

### What it does

At each canary step, Release compares the canary's pods with the stable pods. Its inputs
(D21, D55) are counts per pod-template hash from Prometheus counters, cut into 30-second
windows from the start of the 20% step. Each window gives the requests to `/`, the 5xx among
them, and the requests slower than 500 ms; the slow count is the `+Inf` bucket minus the
`le="0.5"` bucket of the latency histogram.

**Sequential test (D4).** There is one always-valid test per metric: 5xx, and slow requests.

1. Given a window's total number of events, the canary's share of them is binomial with the
   canary's share of that window's requests, as long as the canary is no worse than stable.
   (The exact distribution is hypergeometric, which only makes the test more conservative.)
2. The test is a mixture SPRT on the odds ratio ρ of that share. Its e-value is the average,
   over ρ = 2, 4, …, 1024 with equal weights, of the product over windows of
   ρ^x / (ρπ + 1 − π)^k. Here x is the canary's events, k the window's events, and π the
   canary's share of the window's requests.
3. When the canary is no worse, the expected e-value is at most 1 at every look. Stopping at
   the first look where it reaches 2/α therefore keeps false rollbacks under α over both
   metrics and every look (Ville's inequality), with α = 0.05.
4. The test conditions on the counts, so it needs no normal approximation.

**Absolute floor (D4).** A rollback also needs the canary's share over the whole canary to
exceed stable's by at least 0.1 percentage point.

**Decisions.** Release looks 30, 60, 90 and 120 s into each step:

| Release finds | It proposes |
|---|---|
| A rejection that clears the floor | `rollout_abort`; at L4 it first asks Monitor and Diagnose (P1), and if either says the cause is infrastructure, it holds and proposes `escalate_to_human` instead |
| No rejection at the fourth look | `rollout_promote` |
| No rejection before the fourth look | Nothing (a hold) |
| No canary requests at the fourth look | `escalate_to_human` |
| An aborted Rollout | `rollout_undo` (high tier: always a human) |

Release never proposes `rollout_set_weight`: the steps set the weight.

**Canary under agents mode (D53).** The agents-mode canary keeps L0's steps and pauses, but
each inline L0 analysis is replaced by a pause where Release decides through the gate.

### Levels

- **L0:** the frozen AnalysisTemplate (`analysis-l0.yaml`), replayed offline on the recorded
  series (D54). It uses the same two queries (the 5xx ratio and p99 over 1 minute),
  4 measurements 30 s apart after each step's 30 s pause, and aborts on the second failure.
  Missing data counts as a failure.
- **L1 to L3:** identical for Release, which uses no LLM.
- **L4:** adds the P1 questions before any rollback.

### Frozen settings and calibration

- α = 0.05 over the two metrics; the ρ grid 2 to 1024; the floor 0.1 percentage point.
- 30 s windows; 4 looks per step; the slow threshold 500 ms.
- **Gate confidence (D9).** It comes from a split conformal calibrator fitted on Release's
  dev promote and rollback decisions, labelled by the recording.
  - On the 28 first dev recordings: 38 decisions, leave-one-recording-out coverage 38/38.
    Every prediction set held both labels (mean set size 2.0), and no rollback reached
    confidence 0.70, so every staging rollback or promotion would go to a human.
  - On those 28 plus 8 more: 53 decisions; coverage 53/53, both labels in every set.
  - The final refit comes after the agents-mode recordings are scored.

### Behaviour of the test in simulation

**Setup:** simulated canaries on L0's schedule (4 looks at 20%, then 4 at 50%, of 10 requests/s).

**False rollbacks with no difference:** 4 to 19 in 2,000 runs at each of four error levels
(0.2%, 1%, 3% and 10% for both canary and stable), at most 1%, against α = 5%.

**Rollbacks with a real difference:**

| Canary vs stable | Caught |
|---|---|
| 5% 5xx vs 0 | 500/500 (median look: the first) |
| 3% vs 0 | 500/500 |
| 1% vs 0 | 468/500 |
| 5% vs 1% | 491/500 |

### Dev results (dev, in-sample)

**Data:** 28 release recordings from the first dev delivery (22 bad, 6 good), all made under
the L0 Rollout. Times run from the start of the 20% step.

**How the step times were found.** These recordings hold no Rollout step log, so each step's
start is the first `up` sample of its pods, moved 7.5 s earlier. That is half of
Prometheus's 15 s scrape interval, derived and not tuned. Checked against L0's live outcomes:

| Shift | Offline L0 matches live L0 |
|---|---|
| 0 s | 23/28 |
| 5 s | 26/28 |
| 7.5 s | 27/28 |
| 10 s | 27/28 |
| 15 s | 26/28 |

Every disagreement is a recording that ends before the deciding measurement; the offline L0
never contradicts a live decision.

| | L0 (offline) | L3 | L4 |
|---|---|---|---|
| Bad releases caught before 100% | 14/22, 64% (43–80) | 12/22, 55% (35–73) | 12/22 |
| False rollbacks | 0/6 (0–39) | 0/6 | 0/6 |
| Median time to rollback | 60 s (n 14) | 60 s (n 12) | 60 s |

Caught by band:

| Band | L0 | L3 | L4 |
|---|---|---|---|
| above | 4/4 | 4/4 | 4/4 |
| above with node stress | 2/2 | 2/2 | 2/2 |
| near | 2/4 | 1/4 | 1/4 |
| below | 0/4 | 0/4 | 0/4 |
| no band (early dev specs) | 6/8 | 5/8 | 5/8 |

Paired comparisons:
- **L0 vs L3:** 2 right only at L0, 0 only at L3 (exact McNemar p = 0.5). On the 12 bad
  releases both rolled back, L3 decided a median 15 s earlier (bootstrap 95%: 0 to 30 s).
- **L3 vs L4:** identical.

**Node-stress recordings (recordings-2):** 8 releases with node series. Seven ran under the
D44 node stress; one did not.

| Level | Bad caught (2 near-band bad releases) | False rollbacks (6 good releases) |
|---|---|---|
| L0 | 1 | 0 |
| L3 | 0 | 0 |
| L4 | 0 | 0 |

**P1 never ran:** Release proposed no rollback on any of the 8.

**Wrong decisions on node-stress cases** (the 7 new ones plus 5 earlier ones):

| Level | Wrong decisions |
|---|---|
| L3 | 2/12 = 16.7% (4.7–44.8) |
| L4 | 2/12 (same) |

- Neither wrong decision is a false rollback. One is a near-band bad release that every
  level, L0 included, promoted. The other is a recording that ends before Release's deciding
  look.
- Under the pre-registered H4 metric (false rollbacks and wrong fixes), L3 has 0 false
  rollbacks in 8 good releases under stress.
- H4 is reported descriptively (D56).

**All 36 dev release recordings:**

| | L0 | L3 | L4 |
|---|---|---|---|
| Bad releases caught | 15/24 | 12/24 | 12/24 |
| False rollbacks | 0/12 | 0/12 | 0/12 |

**Caveats:**
- **Truncation.** The L0 Rollout aborts a bad canary live, so a recording stops where L0
  aborted. A level that has not decided by then is counted as not caught ("truncated": 3 of
  L3's misses on the 28, 4 on the 36). The agents-mode re-recordings (D54) remove this. Until
  they are scored, L0 has more data than the other levels on those recordings.
- **Below band.** At 10 requests/s, below-band releases are invisible to every level: 0.29% to
  0.42% errors give 1 to 3 errors per canary, and delays of 196 to 318 ms never cross 500 ms.
- **Near band.** The configured error rate is not what the canary shows. One near-band
  release configured at 1.09% showed 3 errors in 812 canary requests.
- **P1 is unexercised.** It never ran, because Release never proposed a rollback under node
  stress. L3 and L4 are therefore identical on dev, and P1's answers on real node series are
  unobserved.
- **Calibration.** It is uninformative on dev and is refitted once more after the agents-mode
  recordings are scored.
