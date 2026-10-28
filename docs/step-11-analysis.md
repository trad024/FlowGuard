# Offline final-evaluation analysis toolkit

This branch adds analysis code only. It neither runs agents nor generates/reads real held-out
data during development. Tests construct small synthetic SQLite databases with the existing
audit and ground-truth schemas. The pre-registration, safety code and other frozen files are
unchanged. All locally generated example outputs are explicitly synthetic.

## Reproduction

With Python 3.11 and `pip install -e ".[dev]"`:

```powershell
python evaluation/report.py --out results/
```

Default inputs are `state/audit.db` and `evaluation/runs/groundtruth.db`. They must exist;
the tool opens them read-only and does not create empty production databases. Optional
`--audit`, `--truth`, `--replays` and `--preregistration` select explicit inputs.
`--l5-setting default` selects the setting used in the main ablation table. Bootstrap seed
defaults to the public analysis seed 20261028, independently of any experiment seed.

For a self-contained **synthetic demonstration**, without opening the default databases:

```powershell
python evaluation/report.py --demo --out state/analysis-demo
```

The demo constructs isolated fake databases and marks `results.json` and its README as
synthetic. Re-running the demo reuses its marked synthetic inputs. It refuses an existing
unmarked input directory. It does not call the scenario generator, Docker, a cluster or the
network. `--no-plots` is available for lightweight machine-readable regeneration.

## Replay observations and joins

The audit schema holds actions and approvals, but does not intrinsically contain a model's
predicted label, evidence lines, detection timestamp or L5 setting. The offline replay
exporter must supply those structured observations; this toolkit does not guess them from
free-text rationales or copy labels from ground truth into observations.

Use `events.kind = "replay_result"`, with `scenario_id` and `level` stamped by `AuditLog`, or
provide the same records through `--replays replay-results.jsonl`. A minimal diagnosis row:

```json
{"scenario_id":"run-id","level":"L3","setting":"default","method":"llm_retrieval","repeat":1,"proposal_ids":["p-id"],"observed":{"root_cause":"dependency","resolved":true,"latency_s":0.7,"cost_usd":0.0}}
```

`scenario_id` joins the ground-truth `runs.run_id`, not the reusable YAML `spec_id`. The
tool verifies every listed proposal belongs to that scenario and level; audit child rows
must have matching context. Unknown scenario IDs and duplicate observations are errors.
`proposal_ids` is mandatory whenever multiple methods, settings or repeats share a scenario
and level; an empty list is valid for an observation without actions. This avoids counting
another repeat's execution or approval. Invalid host runs are excluded and counted. Missing
replays, missing measurements and unmatched cohorts are reported explicitly.

Observation fields:

| Field | Meaning |
|---|---|
| `detected`, `detected_at` or `mttd_s` | Detection outcome/time for runtime or pipeline incidents |
| `root_cause` | Actual predicted class; abstentions such as `unclear` count as errors |
| `evidence_lines` | Quoted positive line numbers, scored against truth `relevant_lines` |
| `selected_action` | Selected collaboration decision, verified against audited proposals |
| `resolved` | Observed resolution; autonomy additionally requires a validated execution |
| `false_alerts` | Number of false alert episodes in a normal-traffic run |
| `caught_before_full`, `release_outcome` | Bad-release catch and false-rollback scoring |
| `security_action` | Block/allow/review decision for critical/control scoring |
| `pipeline_s`, `time_to_first_failure_s`, `time_to_rollback_s`, `latency_s`, `cost_usd` | Nonnegative observed measurements |
| `llm_repeat_subset`, `repeat`, `tokens` | Designated repeat-subset membership and run measurements |
| `gate_threshold` | Numeric threshold associated with one L5 setting |

Truth uses the existing JSON columns: `root_cause`, `incident`, `bad_release`, `band` and
optional explicit labels needed for localization (`localizable`, `relevant_lines`) and H4
(`collaboration_case`, `acceptable_actions`). If those labels are absent, that metric has
no eligible observations, not a guessed success. Normal exposure comes from `spec.hours`.
These are scoring/export requirements, not changes to agent inputs or the frozen schema.

Use method names `regex` at L0, `tfidf_lr` at L1, `llm_zero_shot` at L2,
`llm_retrieval` at L3–L5, and `encoder` at L1 for its separate method comparison. The primary
ablation table selects the canonical method; the five-method table selects each method at
its canonical level so L3/L4/L5 copies do not triple its sample size. H2 contrasts require
the explicit method names, preventing a stub regex L1 run being labelled TF-IDF.

## Statistics and denominators

- Reuses `evaluation/stats.py` for Wilson intervals and exact Garwood Poisson intervals.
  The Poisson CDF now uses log-sum-exp to avoid underflow at large counts. Rates divide
  count intervals by actual observed hours; zero exposure yields a null rate and interval.
- Exact two-sided McNemar conditions on paired discordances. H1 compares L1/L0 detection;
  H3 compares L3/L2 localization; H4 compares L4/L3 wrong decisions.
- Paired bootstrap uses **10,000** resamples of whole scenarios, a fixed public seed, and
  percentile 95% intervals. H2 uses the difference in fixed-five-class macro-F1, not the
  mean of per-item F1. A two-sided null-centred bootstrap tail probability with an add-one
  Monte Carlo correction supplies its p-value. Small/degenerate samples can yield unreliable
  bootstrap intervals or tail probabilities; counts and raw results remain visible.
- The read-only pre-registration selects the Holm family. Version 0.2 retains H1, both H2
  primary contrasts, H3 and H4 (five tests). Version 0.3 follows D42: H2 is secondary, with
  bootstrap macro-F1 intervals and Wilson accuracy intervals, not a confirmatory H2 test.
  Wilson applies to correct/incorrect counts, not macro-F1, which is not a binomial proportion.
- D44 makes H4 conditional on a **dated development-rate amendment**. Below 10% dev wrong
  decisions, H4 has counts/Wilson intervals but no test, and Holm covers H1/H3. At 10% or
  above, Holm covers H1/H3/H4. With the amendment missing, H4's p-value remains null and its
  slot remains in the conservative family size; the report cannot be marked confirmatory.
  The tool never chooses H4's mode from held-out outcomes. Missing comparisons retain their
  slots. Secondary method, time and per-band results remain outside Holm.
- Pairing uses the intersection of scenario IDs at the selected setting. Only repeat 1
  enters primary metrics. Missing measurements are `n=0`, null estimate and null interval.
- MTTD is conditional on detection; missed incidents are not given fabricated durations.
  Paired time comparisons require both methods' measurements on the same scenario. MTTD
  derived from timestamps uses the same function in summaries and paired comparisons.
  Time to rollback is supported as an observed nonnegative duration.
- L5 autonomy uses the common scenario intersection across settings. Safety counts retain
  **all observed executions**, including unmatched scenarios, so a violation cannot disappear
  through cohort matching. The output records both matched and total scenario counts.
- Autonomy requires observed resolution plus an audited successful, validated execution,
  no human requests/responses and no unsafe execution. A mere proposal is not resolution.
  Its denominator contains labelled incidents (CI failures, runtime incidents, pipeline
  anomalies, bad releases/security incidents or explicit collaboration cases); normal
  traffic and good-release controls cannot inflate incident-resolution autonomy.
  Approval must precede execution, and its latest prior verdict must be approved; a later
  approval cannot retroactively authorize a high-risk action. An execution failing multiple
  authorization checks counts once in the unsafe-execution count.
- The repeat-variance table uses only complete designated 1/2/3 triplets. It reports the
  observed subset size against the expected 60, missing triplets, each repeat's metrics,
  repeat means and sample SDs. SD is descriptive variability, not a confidence interval.

The current pre-registration is still `draft`. Reports record its SHA-256, status and
amendments; they do not call these measurements confirmatory until it is frozen. Synthetic
demonstrations are never marked confirmatory, regardless of that status.

## Outputs and verification

Outputs: `results.json`, `ablation.csv` (six levels by the version-specific primary metrics), `per-band.csv`,
`autonomy-safety.csv`, `llm-variance.csv`, `diagnosis-methods.csv`, and a README describing
interpretation. Figures
are `ablation`, `autonomy-vs-safety`, `per-band`, and `llm-variance`, each as PNG and SVG.
Results include five-method macro-F1 intervals, primary paired tests and Holm adjustment,
descriptive time comparisons, secondary metrics, input/pre-registration checksums and
coverage/exclusion counts. SVG date metadata and IDs are deterministic.

Tests use synthetic schemas only: reference statistical values, pairing and fixed seeds,
Holm's family size, read-only database access, context joins, invalid/missing observations,
per-band counts, repeat triplets, late-approval safety, null missing data, and end-to-end
CSV/JSON/PNG/SVG generation. No real held-out database, seed or scenario was opened.

## Pre-registration 0.3 and the H4 amendment handoff

The branch is updated onto `dev` commit `be5e4b2`, including D39-D44, without editing any
frozen file. The current pre-registration remains draft with no H4 amendment. H2 is shown
in the five-method descriptive table, including n, macro-F1 bootstrap intervals and accuracy
Wilson intervals. All three named secondary method contrasts are included; encoder/TF-IDF
pairing works even though both occupy L1. L3 retrieval is counted at its canonical level,
so cached L4/L5 copies are not extra diagnosis incidents. Hallucination has the preregistered
secondary exact McNemar comparison when paired observations are supplied.

The toolkit recognizes the following **proposed machine-readable encoding** for the Step 8
owner's already-required dated H4 amendment. This is documentation only; no amendment or
decision-log entry has been written by this branch:

```yaml
amendments:
  - date: 2026-10-29
    reason: Development node-stress wrong-decision rate and H4 count decision after Step 8
    H4:
      dev_wrong_rate: 0.09  # illustrative placeholder, not a measured project result
```

Use the actual measured dev rate and approved date, not the illustrative number. If an
amendment uses a different encoding, update the adapter explicitly before final inference;
the tool reports H4 pending instead of guessing from free text or held-out data. Unknown
pre-registration versions are rejected for explicit review. LogChunks eligibility must be
exported as `localizable: true` only for fully located targets under D43; the toolkit never
opens the real LogChunks dataset to infer or inspect targets during development.

The updated base had one boundary test requiring an uncommitted corpus on CI. That test now
constructs three small synthetic variant folders, including an excluded one, and checks the
exact selected set. It does not create fake files at the production corpus path. All new
analysis tests likewise use fake audit and truth databases; no real held-out input is used.
