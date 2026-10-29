# Evaluation design

Draft for the report. This section describes the design only. The only results quoted are the
dev L0 baselines the design was sized from. Decision numbers (Dn) refer to the decision log in
`docs/blueprint-v2.md`. The pre-registration is `docs/preregistration.yaml`.

## Research question and hypotheses

How much does each AI component add to a DevOps pipeline's decisions, and at what cost in
human interventions and safety? Every comparison is between ablation levels (L0 to L5, D10)
on the same scenarios.

| Hypothesis | Comparison | Primary metric | Role |
|---|---|---|---|
| H1 | L0 vs L1 | DSO1 recall (runtime and CI incidents detected) | Confirmatory |
| H2 | Regex, TF-IDF + logistic regression, LLM zero-shot, LLM + retrieval, fine-tuned encoder | DSO2 macro-F1 on the generated CI-failure corpus | Descriptive (D42): n and a Wilson interval, no confirmatory test |
| H3 | L2 vs L3 | Evidence-localization hit rate on LogChunks real logs (D32) | Confirmatory |
| H4 | L3 vs L4 | Wrong decisions (false rollbacks and wrong fixes) in collaboration scenarios | Descriptive (D56): n and a Wilson interval per level, paired counts shown |
| H5 | L5 gate settings | Share of incidents resolved without a human, with 0 unapproved high-risk actions | Descriptive: a Wilson interval per setting |

The confirmatory family is H1 and H3, with Holm's correction across the two at a family-wise
α = 0.05 (D31, D42, D56). Secondary and per-band results are reported uncorrected and labelled
as such. Analyses not in the pre-registration are labelled exploratory.

## Pre-registration and staged freezes

`docs/preregistration.yaml` fixes, before the held-out set exists:
- the scenario families, kinds, bands, weights and parameter ranges;
- the counts, metrics, tests and the smallest effects of interest;
- the exclusion rules.

Once frozen, it is never edited. A later change is a dated amendment with its reason, and the
report lists every amendment.

Two freezes protect the agent code from the test data:
- **`diagnose-freeze` (D39).** Once the pre-registration is frozen and Step 7 is done, the
  Diagnose code is tagged `diagnose-freeze`. Its runs on the test templates and on LogChunks
  may then start while other agents are still being built. The reason is the LLM quota, which
  sets the schedule.
- **`eval-freeze`.** All agent code is tagged `eval-freeze` before the held-out scenarios are
  generated and the rest of the evaluation runs. The generator refuses to run without both
  tags, or when `agents/` differs from the tagged code.

After any held-out result has been seen, nothing under `agents/` changes: no threshold, no
prompt. A fix found then is reported as a limitation and not applied to the scored run.

## Held-out generation

The held-out scenarios do not exist until Step 11. With a single developer (D26), the test set
cannot be written blind, so it is fixed by rules and a committed seed instead:
- `scenarios/heldout.py` draws the scenarios from the pre-registered families and ranges with
  a secret seed;
- only the seed's SHA-256 is committed (`scenarios/heldout-seed.sha256`), and the seed itself
  stays in `.env`;
- the generator also draws a seeded run order, and every level replays the recordings in that
  order.

Development uses only the dev scenarios. A failure corpus is split by fault template, never by
run, so no variant of a test template is seen in training (D6, D34).

## Record and replay (D15)

Every scenario is run live once on the laptop cluster, and its raw inputs are recorded:
- Prometheus series;
- load-generator logs;
- CI runs and logs;
- deployer events;
- the Rollout's step changes.

Every ablation level is then evaluated offline on the identical recording, so the levels are
compared exactly paired and the evaluation is reproducible. DSO2 classification is evaluated
fully offline on the corpus and on LogChunks.

A smaller set of live end-to-end runs shows the whole loop working: detection, gate,
approval, execution and recovery. Live and replayed results are reported separately.

**Conditions that keep runs comparable:**
- **Quiet start (D50).** No fault is injected while an L0 alert is firing: the runner waits
  for 2 quiet minutes.
- **Release scenarios (D53, D54).** They are recorded under the agents-mode canary, so every
  canary runs its full window. L0's canary decision is replayed offline from its frozen
  analysis queries, on the same recording as the other levels.
- **Invalid runs.** A run with a slept host, a canary that never completed, or alerts that
  never went quiet is invalid. It is re-run, never dropped silently.

## Human approval in batch runs (D17)

Batch evaluation uses a simulated approver: it approves when the scenario's ground truth says
the action is right. Its error rate is 0 by default and 10% in a sensitivity run. A real team
member approves only in the live runs and the demo. Approvals per incident are counted either
way.

## Calibrated confidence (D9, D45)

The gate's confidence comes from split conformal prediction:
- **Calibration set.** On held-apart data, each example's nonconformity is 1 minus the score of
  its true label.
- **Prediction.** For a new prediction, the confidence of the predicted label is 1 minus the
  second-largest label p-value.
- **Out of distribution.** If the best label's own p-value is below 0.05, the example looks
  unlike the calibration data and the confidence is 0.
- **Too little data.** With fewer than 20 calibration examples the confidence is also 0, so a
  medium-tier action always goes to a human.

The gate never reads a confidence an agent or an LLM reports about itself.

**Diagnose (D45)** is calibrated leave-one-template-out on the 25 training and validation
templates:
- every template's scores come from a model that never saw it: L1 is refit without it, L3's
  retrieval excludes cases of the same template, and L2 has no training;
- coverage at a 90% target is reported on each template held apart.

**Release** is calibrated on its dev promote and rollback decisions, labelled by the
recording, with coverage reported leave-one-recording-out.

## Statistics

| Quantity | Method |
|---|---|
| Rates | Count and a 95% Wilson interval |
| Event rates per hour (false alerts) | Exact Poisson (Garwood) 95% interval on the count, divided by the hours observed |
| Paired correct/incorrect between levels on the same items | Exact McNemar test |
| Time metrics (time to detect, time to rollback, durations) | Paired bootstrap interval, 10,000 resamples |
| H2's macro-F1 differences | Paired bootstrap, 10,000 resamples |

- **Per band.** Every release and runtime result is also reported per intensity band (below,
  near or above the frozen L0 thresholds), descriptively.
- **LLM variability (D18, D27, D28).** The model id, temperature (0) and reasoning effort are
  pinned and logged per call. Responses are cached by a hash of the model, the settings and the
  prompt. Repeats: set at freeze (the draft has 3 runs on a seeded 60-item subset per LLM
  level, with the repeat index in the cache key).

## Sample sizes (D16)

Counts come from a power analysis at 80% power and α = 0.05, paired, for the smallest effect
of interest (D43). It starts from the dev L0 baselines measured in Step 4b:

| Baseline (dev L0) | Value |
|---|---|
| DSO1 recall | 10/18 = 55.6% after the D50 exclusions; 7/15 = 46.7% without incidents that started under leftover alerts |
| DSO2 (regex) | 61/74 = 82% accuracy on the generated failures |
| DSO3 (frozen canary analysis, live, 28 dev releases) | 15/22 bad releases caught before 100%, 0/6 false rollbacks |

| Set | Count | Basis |
|---|---|---|
| H1: runtime incidents | 147 | 220 incidents for +15 points from 55.6%, sized at Holm α/3; two thirds runtime |
| H1: pipeline (CI) incidents | 74 | The CI third of the 220 |
| H3: LogChunks logs | 230 | Seeded (`logchunks-h3-20261028`) from the 514 fully located logs, for +15 points at Holm α/3 (D43, D47); 26 not visible after log reduction were replaced and are reported (D48) |
| Normal traffic for false alerts | At least 4 hours | |
| DSO2 live end-to-end CI runs | 12 | One per test-split variant |
| DSO4 security scenarios | 14 | |
| Release scenarios (DSO3, H4, H5) | Set at freeze | No confirmatory test rests on this family since D56; the count follows DSO3's precision once the agents-mode dev recordings are scored |
| DSO5 commit sequence | Set at freeze | |
| DSO6 adversarial and conflict set | Set at freeze | |
| LLM repeats | Set at freeze | |

**Margin from Holm.** Holm now runs across H1 and H3 only. The counts sized at α/3 are kept,
so their power is somewhat above 80%.
