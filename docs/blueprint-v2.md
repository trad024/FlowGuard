# FlowGuard: project context (blueprint v2)

Single source of truth for FlowGuard. Every approved decision lives here. Anything not written here is not approved.

- Version: v2, 2026-10-21. Supersedes the v1 PDF blueprint where they differ; v1 stays valid for everything not changed below.
- Status: v2 decisions D1 to D33 accepted. Build position: Step 0 done; Step 1 done and verified on a team laptop (2026-10-22, commit `a0abaf2`): setup from zero, 9/9 smoke checks pass, cluster about 930 to 980 MiB of the 4096 MiB budget (Docker was not yet capped at 4 GB). Step 2 implemented and verified on the same laptop (2026-10-22): `check-step2.ps1` 6/6 (good version promoted 20/50/100, bad version aborted by L0 at 20%, stable kept serving, canary and stable pods in the per-request log), cluster about 1.73 GB at the canary peak. Step 2 signed off 2026-10-22 after Q7 and Q8 were closed by D21 and D22. Step 3 implemented and verified 2026-10-22: `check-step3.ps1` passed (an `app/` commit ran app-ci to success, the laptop deployer rolled its image out to the staging canary, promoted under L0 with both analyses successful; a docs-only commit started no app-ci run). Step 3 done 2026-10-25: `check-step3.ps1` 6/6 on a throwaway `scenario/` branch (dev untouched), baseline plan mode proven on a `scenario/baseline/` branch, and the demo app's vulnerabilities fixed (D24: 0 HIGH, 0 CRITICAL, 0 Gitleaks findings). Step 4 is split (section 7): 4a approved 2026-10-26 and signed off 2026-10-27; D25 (information firewall) implemented before 4b; 4b not approved yet.

---

## 1. The assignment

The professor's brief, "Intelligent Multi-Agent System for Autonomous DevOps / MLOps Pipelines", asks:

> How can intelligent agents be used to make a DevOps or MLOps pipeline more automated, adaptive, efficient, and autonomous?

Expected outcomes:
- a functional prototype;
- a multi-agent system whose agents analyze, decide, collaborate and act;
- experiments under different scenarios;
- a comparison with a traditional pipeline or baseline;
- measured performance, reliability and limitations;
- an analysis of the impact of AI on automation and autonomy.

The brief also asks explicitly about human intervention, LLMs and other AI approaches, and fine-tuning.

Grading principle, quoted from the brief: "Innovation, experimentation, reasoning, autonomy, and evidence-based results are more important than technological complexity."

The team chose the DevOps pipeline. The other team chose MLOps.

---

## 2. Research question and hypotheses (new in v2)

**Research question.** Which AI components (statistical detection, learned models, LLM reasoning, agent collaboration) improve detection, diagnosis and release safety over a strong rule-based pipeline, and at what cost in autonomy risk?

| ID | Hypothesis | Measured by | Test set |
|---|---|---|---|
| H1 | Statistical and ML detectors (L1) catch more injected incidents than static thresholds (L0), at an equal or lower false-alert rate | DSO1 recall, false alerts per hour, MTTD | Held-out runtime and CI faults |
| H2 | LLM with retrieval (L3) classifies CI failure root causes better than regex rules and classical ML. A fine-tuned small encoder is compared against both. | DSO2 macro-F1, accuracy, cost and latency per incident | Held-out failure corpus |
| H3 | Evidence grounding (L3) quotes log lines that locate the real failure reason more often than the LLM alone (L2), and lowers the hallucination rate without a significant accuracy loss | Primary: evidence localization hit rate on LogChunks (D32); secondary: hallucination rate, line-level precision and recall, macro-F1 on the generated corpus | LogChunks sample (localization); held-out failure corpus (hallucination, macro-F1) |
| H4 | Collaboration (L4) reduces wrong decisions in cases where one agent's view is misleading | False rollbacks and wrong fixes, L3 vs L4 | Held-out collaboration scenarios |
| H5 | Some gate setting reaches ≥ 60% of incidents resolved without a human with 0 unapproved high-risk actions | Autonomy vs safety curve (L5) | All held-out scenarios |

A hypothesis that is rejected is a valid result. It is reported, not hidden.

---

## 3. Business objectives (unchanged from v1)

| ID | Objective | Priority |
|---|---|---|
| BO1 | Reduce service interruptions: detect and resolve delivery problems quickly | Main |
| BO2 | Reduce operations workload: free engineers from repetitive troubleshooting | Main |
| BO3 | Deliver new versions more reliably: faulty releases rarely reach customers and are rolled back quickly | Main |
| BO6 | Keep humans in control: automation never takes a high-risk decision without human approval | Main |
| BO4 | Protect against security and quality risks | Secondary |
| BO5 | Lower operational costs: less time and fewer resources spent delivering software | Secondary |

---

## 4. Data science objectives (v2)

Metrics and definitions are fixed. Target values stay provisional until the L0 baseline is measured in Step 4. Every percentage is reported with its count (n) and a 95% Wilson interval.

| ID | Objective | Method | L0 baseline | Success criteria | Changed in v2 |
|---|---|---|---|---|---|
| DSO1 | Detect injected incidents early (runtime and CI) | Rolling z-score, then Isolation Forest (runtime); duration and failure-streak detector (CI) | Static alert thresholds | Recall ≥ 90%; ≤ 1 false alert per hour of normal traffic; median MTTD lower than L0 | Normal-traffic window ≥ 4 h (run overnight); CIs |
| DSO2 | Classify the root cause of CI failures into 5 classes and propose a validated fix | Log reduction (drain3) + retrieval (Chroma) + LLM with structured output and evidence grounding; compared against regex, TF-IDF + logistic regression, and a fine-tuned small encoder | Regex root-cause classifier + fixed class → action playbook | Accuracy ≥ 80%; macro-F1 ≥ 0.75; ≥ 60% resolved without a human (green rerun, gate-allowed actions only); hallucination rate and cost per incident reported; fix PRs counted as assisted | Stronger baseline (D3); generated corpus (D6); 5-way comparison (D7); grounding (D8); external real-log validation (D14) |
| DSO3 | Decide continue, hold, rollback or escalate during a canary | Sequential test with always-valid p-values on per-request data, plus an absolute error floor; collaboration with Monitor and Diagnose before rollback | Argo Rollouts AnalysisTemplate with fixed, realistic thresholds | ≥ 90% of bad releases caught before 100% traffic; ≤ 10% false rollbacks; lower time to rollback and change failure rate than L0 | Sequential test replaces Mann-Whitney on time series (D4); collaboration (D5) |
| DSO4 | Block or allow a deployment from scan, quality and test results | Hard rules for criticals; ML score for the grey zone, explained with SHAP | Block on any HIGH or CRITICAL finding + quality gate | 100% of injected criticals blocked; false-block rate ≤ 10% and lower than L0; allowing over a scanner finding always needs a human | SHAP explanations |
| DSO5 | Predict job duration and failure probability to reorder or skip optional jobs | LightGBM regressor + classifier, explained with SHAP; inference runs inside the CI `plan` job from a model file in the repo | Fixed job order + standard `paths:` filters | Pipeline duration −20%; 0 failures missed vs the full pipeline; shorter time to first failure; MAE and AUC reported | Real optional jobs and a fair baseline (D2); CI-side inference (D1) |
| DSO6 | Score every agent action and route high-risk ones to a human | Static catalog + deterministic gate + loop protection; calibrated confidence | No gate | 0 unapproved high-risk actions; 100% high-risk recall; escalation precision ≥ 85%; 100% of decisions audited; approvals per incident reported | Confidence no longer self-reported by the LLM (D9); conflict scenarios added (D5) |

The risk score is kept only to order the human approval queue. It never changes a gate outcome.

---

## 5. Architecture

### Three layers

**Cloud, on free tiers**
- GitHub and GitHub Actions: build, unit tests, optional jobs, SonarCloud, Trivy, Gitleaks, image push, and the Optimizer's `plan` job.
- A hosted LLM API.
- The human approval channel.

**Laptop, agent service** (one Python process)
- LangGraph orchestrator.
- Five agents with read-only credentials.
- Shared incident record.
- Risk gate.
- Executor: the only component with write credentials.
- SQLite audit log.
- Chroma, embedded.

**Laptop, k3d cluster**
- podinfo in `staging` and `prod`.
- Argo Rollouts.
- Prometheus, Alertmanager, kube-state-metrics, Grafana.
- A load generator that logs every request, tagged with the pod that served it.

### Communication

The laptop polls GitHub (Actions API) and Prometheus. There are no webhooks: GitHub cannot reach the laptop (D1).

### Workflows (v1, unchanged except where noted)

- **A. New commit.** The `plan` job in CI applies the Optimizer model → CI builds and scans → Security decides allow, review or block → if allowed, canary deploy, and workflow D starts.
- **B. CI failure.** Diagnose classifies and proposes a fix → gate → executor → rerun. Green resolves the incident. Red gives Diagnose one more attempt, then escalates.
- **C. Runtime anomaly.** Monitor opens an incident. If the service is mid-canary, it goes to Release (D); otherwise to Diagnose. The fix is validated by the metrics recovering.
- **D. Canary in progress.** Release compares canary and stable at each step. **v2:** before proposing a rollback, Release requests evidence from Monitor and Diagnose (D5).

### Collaboration protocols (new in v2, D5)

Agents read and post to a shared incident record, which is an `incidents` / `evidence` table in SQLite. Each post carries an agent, a claim, references to supporting data and a timestamp.

| Protocol | Rule | Scenario that tests it |
|---|---|---|
| P1 Rollback check | Before `rollout_undo` or `rollout_abort`, Release asks Monitor (is the node or cluster under stress?) and Diagnose (does the error look release-caused or infrastructure-caused?). If the evidence says infrastructure, Release holds and escalates instead of rolling back. | Good release while a node is under memory or CPU stress |
| P2 Forced scans | The Optimizer may not skip a dependency or security job when the commit changes dependency files, or when Security flagged the previous run | Dependency bump commits |
| P3 Conflicts | Two proposals for the same target within the same incident: the gate takes the more conservative one (lower blast radius, reversible) or escalates if they are incomparable | Diagnose proposes a restart while Release proposes a rollback |

Agents still act only by proposing one catalog action. Collaboration exchanges evidence, never actions.

### Action catalog and gate (unchanged)

The tiers and rules are as implemented in `agents/flowguard/catalog.py`, with v1 gate rules 1 to 7, first match wins.

- No agent or LLM ever sets or changes a risk tier.
- The 0.70 confidence threshold for the medium tier is applied to calibrated confidence (D9).

### Technology choices

v1 choices stand: k3d, GitHub Actions, SonarCloud, Trivy, Gitleaks, Argo Rollouts, Prometheus, Alertmanager, Grafana, podinfo, LangGraph, a hosted LLM (D27: Groq free tier, `openai/gpt-oss-120b`), Chroma, scikit-learn, LightGBM, MLflow in file mode, SQLite.

v2 additions:
- SHAP;
- a small encoder fine-tuned on a free GPU notebook (Kaggle or Colab), CPU inference only (D7);
- split conformal prediction, implemented in plain Python.

Still excluded:
- a local generative LLM;
- free-form shell commands from the LLM;
- Helm, a service mesh, real production traffic.

---

## 6. Evaluation protocol (v2)

### Ablation ladder (D10)

Every level runs on the same held-out scenarios, with the same seeds and the same order. Each level is a configuration flag of the agent service, so no level needs separate code.

| Level | Adds | Answers |
|---|---|---|
| L0 | Strong rule baseline (DSO baselines above) | Reference |
| L1 | Statistical and ML detectors and classifiers, no LLM | Value of classical AI (H1, part of H2) |
| L2 | LLM diagnosis, zero-shot | Value of the LLM alone (H2) |
| L3 | Retrieval + evidence grounding | Value of memory and grounding (H2, H3) |
| L4 | Collaboration protocols P1 to P3 | Value of multi-agent design (H4) |
| L5 | Sweep of the gate thresholds (confidence threshold and tier routing) | Autonomy vs safety trade-off (H5) |

### Autonomy metrics (D11)

- Share of incidents resolved with no human.
- Human interventions per incident.
- Wrong autonomous actions.
- Unapproved high-risk actions, which must stay at 0.

These are plotted against the L5 gate settings.

### Data and test sets

- **External real-world data (D14).** Used only for evaluation and retrieval seeding, never for tuning, and never mixed into the held-out set. Raw files are not committed: a download script fetches them and checks their checksums.
  - **LogChunks** (Brandt et al., MSR 2020; Zenodo record 3632351; CC BY 4.0): 797 real failed Travis CI build logs from 80 GitHub projects, each with a human-labelled chunk that explains the failure. Used for **evidence localization only** (D32): the log lines Diagnose quotes are compared with the chunk of each log in a seeded sample of 150; nothing is labelled by hand, and the 5-class accuracy is measured only on the generated corpus. Chunks are given as text: they are located in their log by line matching (whitespace collapsed, ANSI codes and carriage-return overwrites removed); on the sample, 96 of 150 chunks are located fully, 34 partly and 20 not at all (excluded and reported). Logs outside the sample may seed the retrieval memory.
  - **LogSieve replication package** (2026; Figshare 10.6084/m9.figshare.30811382): 20 failed GitHub Actions logs with each line labelled relevant or irrelevant to the root cause. Used to evaluate log reduction (drain3) and grounding on GitHub Actions logs. **Status 2026-10-27: labels not found, not used unless a small labels file is identified.** Data.zip (1 GB, 111,172 files) holds raw run logs without line-level labels; it was deleted to free disk; Study_Steps.zip (1.5 GB) is not downloaded.
  - **GADFPD** (Mendeley Data 10.17632/mggwn7rj9f.1, CC BY 4.0): about 300,000 GitHub Actions and Travis runs. Optional for DSO5 pre-training or external validation, only after a quality check (fields present, duplicates, label balance).
  - Not used: Loghub (system logs; the Monitor agent reads metrics, so it would serve no hypothesis).
  - Stated limitations: LogChunks is Travis CI from 2019 to 2020 and from other projects; public logs may appear in a hosted LLM's training data, which can inflate its score on them.
- **Failure corpus (D6).** A scripted generator injects faults of the 5 DSO2 classes into copies of the demo repo and runs build and tests unattended in containers. It aims for several hundred labelled logs. Exact volume will be set after measuring build time, so this number is not yet a commitment.
- **Splits.** The corpus is split train / validation / test **by fault template, not by run**, so near-duplicate logs never leak into the test set.
- **Held-out protocol (D12, changed by D26b).** A pre-registration file (`docs/preregistration.yaml`: families, parameter ranges, counts, metrics, tests) is frozen and committed before any agent code, with the SHA-256 of a secret seed (the seed itself stays in `.env`). The held-out scenarios are drawn from them by a seeded generator (`scenarios/heldout.py`) only at Step 11. Development uses only the dev scenarios.
- **Information firewall (D25).** Ground truth and injection events live in an evaluation-only store that no agent code can reach; the shared audit log holds only what a real system would see.
- **Fairness (v1, kept).** L0 thresholds are set to industry-style values and frozen before any agent runs. All levels see the same data. Counts are reported, not only percentages.

### L0 canary baseline (DSO3), frozen 2026-10-22

Set in Step 2, before any agent runs, and never tuned afterwards (fairness rule). It lives in `infra/apps/podinfo/base/analysis-l0.yaml`; a test fails if a value changes.

| Setting | Value | Justification |
|---|---|---|
| Canary steps | 20% → 50% → 100% of 10 replicas; at 20% and 50%, a 30 s warm-up pause, then one analysis | No traffic router: the weight is the share of pods behind one Service, so 10 replicas make 20% and 50% whole pods |
| 5xx ratio of the canary's user traffic, 1-minute rate | ≤ 1% | The 99% request success rate used in Flagger's documentation for its built-in canary check; 99% is also the most common entry-level availability target |
| p99 latency of the canary's user traffic, 1-minute window | ≤ 500 ms | The 500 ms p99 request duration used in Flagger's documentation for its built-in canary check; 0.5 s is also a bucket boundary of podinfo's histogram |
| Measurements | Every 30 s, 4 per analysis step; the second failed measurement in a step aborts the rollout (`failureLimit: 1`) | One failed measurement can be noise from a 1-minute rate over a few pods; two out of four is treated as a real regression |
| No data | Counts as a failure | A canary that cannot be measured is not promoted |
| Scope | Canary pods only (`rollouts_pod_template_hash`), path `/` only | kubelet probes and Prometheus scrapes would dilute both metrics |

Known weaknesses of L0, which Release (Step 8) must beat: fixed thresholds ignore the stable version's own error rate; 1-minute rates over 2 to 5 canary pods are noisy; repeated looks at autocorrelated samples are the reason for D4.

### DSO1 L0 alert rules, frozen 2026-10-27

Set in Step 4b before any DSO1 measurement and never tuned afterwards (fairness rule). They live in `infra/monitoring/prometheus/l0-alerts.yml`, loaded by Prometheus (evaluation every 15 s); a test fails if a value changes, and `promtool` unit tests in CI check that each rule fires just above its threshold and stays silent below it. The values are copied from widely used community rules ([awesome-prometheus-alerts](https://github.com/samber/awesome-prometheus-alerts), [kubernetes-mixin](https://github.com/kubernetes-monitoring/kubernetes-mixin)), not tuned on FlowGuard data.

| Alert | Condition | For | Source |
|---|---|---|---|
| HighErrorRatio | 5xx share of requests to `/`, per environment, 1-minute rate, > 5% | 1m | awesome-prometheus-alerts, "high HTTP 5xx error rate" (nginx, Traefik, HAProxy, Caddy, Envoy): > 5%, for 1m |
| HighLatencyP99 | p99 of requests to `/`, per environment, 1-minute window, > 1 s | 1m | awesome-prometheus-alerts, "Istio latency 99 percentile": > 1000 ms, for 1m (Kubernetes API server rule: > 1 s) |
| ContainerOOMKilled | a podinfo container restarted in the last 10 minutes and its last termination was OOMKilled | — | awesome-prometheus-alerts, "Kubernetes Container oom killer" |
| PodCrashLooping | a podinfo container in CrashLoopBackOff within 5 minutes | 2m | awesome-prometheus-alerts, "Kubernetes pod crash looping" (kubernetes-mixin's KubePodCrashLooping waits 15m; the more sensitive one is kept, which favours L0) |
| ContainerHighMemory | working set > 80% of the memory limit (limit from kube-state-metrics), per pod | 2m | awesome-prometheus-alerts, "Container High Memory usage": > 80%, for 2m |

DSO1 at L0: an incident is detected when any of these alerts fires during it; MTTD is the time from the fault's start to the first firing; false alerts are firings on normal traffic, per hour, with an exact Poisson interval. These thresholds are deliberately looser than the canary's (1%, 500 ms): a canary compares a new version before it takes traffic, an alert pages on a running service. The runtime intensity bands are set against these thresholds.

### Record and replay (D15)

Every scenario is run live once and its raw inputs are recorded: Prometheus series, per-request load-generator logs, CI logs and events, with timestamps. Each ablation level's decision logic is then evaluated offline on the identical recording. This makes the comparison between levels exactly paired, reproducible and cheap enough for the sample sizes of D16. DSO2 classification is evaluated fully offline on the log corpus and the external sets (D14). A smaller set of live end-to-end runs, at L0 and at the full system, shows that the whole loop (detection, gate, approval, execution, recovery) works for real; live and replayed results are reported separately.

### Human approval during evaluation (D17)

Batch evaluation uses a simulated approver: it approves a proposal when the scenario's ground truth says the action is correct and denies it otherwise, with a configurable error rate (default 0%, and 10% in a sensitivity run) and a configurable delay. A real team member approves only in the live end-to-end runs and the demo. The report shows both, and the number of approvals per incident is counted either way.

### Statistics

- 95% Wilson intervals for every rate.
- Paired comparisons between levels on the same scenarios: McNemar's test for correct/incorrect outcomes; bootstrap intervals for time metrics.
- Canary decisions never use autocorrelated Prometheus samples as if they were independent observations. Error rates are compared as counts: per-pod request and 5xx counter increases over each window, summed per pod-template hash (canary vs stable), tested as proportions. Latency is compared on per-request data from the load generator (D21).
- **Sample sizes (D16).** Scenario counts are set by a power analysis done right after the L0 numbers exist (Step 4), for the smallest difference worth detecting per DSO, at 80% power and 5% significance. The counts in v1 (about 40 releases, 15 incidents) are placeholders until then.
- **LLM variability (D18, adjusted by D27).** The model id, temperature (0) and reasoning effort are pinned and written to the audit log for every call. Each LLM-based level runs once on every item and 3 times on a seeded random subset of 60 items; results report the mean and the spread on that subset. Responses are cached by a hash of (model, settings, prompt) and never re-sent; for the 60-item repeat subset the repeat index (1 to 3) is part of the cache key, so each repeat is one real call, cached for reproduction (D28).

### Failure analysis

Every wrong decision at L3 and above gets a short entry: scenario, what the agent saw, why it was wrong, and the fix or the stated limitation. This feeds the report's limitations section directly.

---

## 7. Build plan (v2)

Each step needs explicit approval before it starts. Each step ships with tests, ruff and pytest passing.

| Step | Content | Done when | Status |
|---|---|---|---|
| 0 | Repo layout, conventions, contracts, catalog | Tests pass | Committed `0e888e8` |
| 1 | k3d cluster, podinfo ×2, monitoring stack | `setup.ps1` from zero; `smoke.ps1` passes; RAM measured ≤ 4 GB | **Done** `a0abaf2`: 9/9 checks, about 930 to 980 MiB. Fixes: direct port mapping, API pinned to 127.0.0.1:6550, PowerShell 5.1-safe checks |
| 2 | Argo Rollouts; podinfo as a Rollout (20/50/100); L0 AnalysisTemplate; load generator with per-request log tagged by pod | A good version promotes, a bad one aborts under L0; per-request latency log plus per-pod error counts usable for DSO3 (D21) | **Done** `eb62ae3`: 6/6 checks, about 1.73 GB at the canary peak |
| 3 | CI: build, unit, optional jobs (lint, integration, SBOM, multi-arch), scans, push, `plan` job reading a model file; laptop deployer by polling | A commit reaches a staging canary with no manual step | **Done** `baa9734`: `check-step3` 6/6 on a scenario branch; app-ci about 5 min, canary about 6 min; D24 applied (0 HIGH/CRITICAL, 0 Gitleaks) |
| 4a | SQLite audit log (events, proposals, gate decisions, approvals, execution results; query module), written by the deployer, the L0 security gate and the scenario runner; CI runners pinned to `ubuntu-24.04`, scan counts as an annotation, supported Alpine runtime image; L0 security gate in the deployer (DSO4 baseline: any HIGH, CRITICAL or Gitleaks finding blocks); D23 at L0: prod canary after a promoted staging canary; fault middleware in podinfo with configurable error rate and added latency for graded bad releases (D19, amended); recorder for D15 (Prometheus series, load-generator logs, deployer events, CI logs, manifest and ground truth); scenario YAML format and runner (`scenarios/dev/`, `scenarios/heldout/` gitignored, D12); CI failure generator for the 5 DSO2 classes on `scenario/*` branches; external datasets downloaded and checksummed, a reproducible sample of 150 LogChunks logs and a blank labelling sheet (D14) | Each item tested and shown working on the laptop cluster or in CI | **Done**, signed off 2026-10-27: 19 dev scenarios in 5 families run live and recorded; L0 matched the ground truth in all 15 scenarios it is graded on (the 5 ci runs predate the template rework), and the 4 `detect` scenarios (3 runtime incidents, slow CI step) show the expected signal; 31 CI failure templates (6 or 7 per class) verified locally, 31 of 31; leakage scan clean on every template, commit scenario and captured log; audit metrics fixed (distinct security decisions, DSO6 on required approvals); runs during host sleep flagged invalid. Carried over to 4b: CI failure scenarios to rerun through real CI with the templates, and their CI logs scanned for leakage, once the Actions-read token is in `.env`; LogSieve not used (labels not found) |
| 4b | Carry-over from 4a: `ci-*` scenarios rerun through real CI and their CI logs scanned for leakage (needs the Actions-read token); failure corpus at scale (D6); L0 baseline measured for all DSOs; power analysis setting scenario counts (D16); LLM client for D27 (pinned settings, daily token budget, response cache); pre-registration frozen and held-out seed hash committed (D26b; the held-out scenarios themselves are generated at Step 11); offline replay harness (D15) | Every DSO has a measured L0 number on the held-out set; final targets set | **In progress** (approved 2026-10-27). Done: CI scenarios rerun through real CI, 33 job logs scanned clean, code-06 fixed (failed in unit, now in lint); DSO1 L0 alert rules frozen (D33) and live; base server time (0.1 ms) and memory headroom (23-26 of 64 MiB) measured; D30 defect through CI and canary (CI green, L0 abort, 7.8% canary 5xx); `/env` hides `PODINFO_FAULT_*` on deployed pods |
| 5 | Orchestrator with ablation flags L0 to L5; shared incident record; gate with calibrated confidence; executor; approval channel and simulated approver (D17); stub agents; agent-input leakage test (D25): every input an agent receives (logs, metrics, events, diffs, specs) is scanned for ground truth and experiment words | Workflows A to D run end to end with stubs; DSO6 adversarial, conflict and prompt-injection sets pass (D20); the agent-input leakage test passes | |
| 6 | Monitor agent | DSO1 measured at L0 and L1 | |
| 7 | Diagnose agent with the 5-way comparison and grounding | DSO2 measured at L0 to L3, plus the encoder, on the held-out corpus; evidence localization on LogChunks (D32) | |
| 8 | Release agent: sequential test + P1 | DSO3 measured at L0 to L4 | |
| 9 | Security agent (secondary) | DSO4 measured | |
| 10 | Optimizer agent (secondary) | DSO5 measured | |
| 11 | Full ablation, autonomy curve, statistics, failure analysis, report | One command reproduces every table and plot | |

**Cut order if time runs short:** Step 10, then Step 9, then the fine-tuned encoder in Step 7. The ablation ladder, collaboration (H4) and the autonomy curve (H5) are never cut, because they are what the brief grades.

**Contract changes for collaboration** (evidence posts, agent requests, ablation level) are made at the start of Step 5, not earlier.

---

## 8. Non-functional requirements (v1, kept)

- **RAM:** the whole laptop footprint ≤ 4 GB, Docker capped at 4 GB. The encoder (D7) is adopted only if its measured RAM fits this budget.
- **Latency:** Monitor detects within 2 polling cycles (60 s); Diagnose decides within 60 s; Release decides per canary step within 30 s.
- **Reliability:** an agent or LLM failure never blocks the pipeline. It falls back to escalation.
- **Reproducibility:** scenarios, seeds, L0 thresholds, model versions and the ablation config are versioned in the repo.
- **Cost:** LLM tokens and cost are logged per incident; a hard daily budget stops LLM calls; responses are cached for reruns.
- **Security boundaries:** agents have read-only credentials; only the executor writes; the LLM returns JSON from a fixed schema, never commands; secrets stay outside the repo; the approval channel accepts listed team members only.

---

## 9. Repo and working rules

- **Repo:** https://github.com/trad024/FlowGuard. `main` is stable, `dev` is work in progress; work happens on `dev`.
- **Team:** a single developer since D26 (2026-10-27); the labelling and held-out rules follow from that.
- **Authorship:** several team members share this project. Each commit is authored by the member who makes and pushes it, under their own git identity (`git config user.name` / `user.email` in their clone). Never commit under another member's name. No mention of Claude or AI tooling in commits, code, comments or docs.
- **Code:** Python 3.11; contracts in `agents/flowguard/contracts.py`; catalog in `agents/flowguard/catalog.py`.
- **Deliverables:** complete files, not diffs.
- **Commands:** PowerShell first, bash second.
- **Division of work:**
  - Code, tests and static checks are produced and run in the assistant's workspace.
  - Each team member builds with a local coding session on their own laptop: it runs the cluster, tests, commits and pushes under that member's identity.
  - After each step, an independent review (one that did not write the code) checks the pushed `dev` against this blueprint before the next step is approved.
  - Nothing counts as verified until its output has been seen.

---

## 10. Decision log

| Date | ID | Decision | Reason |
|---|---|---|---|
| 2026-10-27 | D33 | DSO1 L0 = five static Prometheus alert rules copied from community rules (5xx > 5% for 1m, p99 > 1 s for 1m, OOMKilled restart, CrashLoopBackOff for 2m, working set > 80% of limit for 2m), frozen before any measurement in `l0-alerts.yml` with a value test and promtool unit tests; runtime intensity bands set against them | A fair, industry-style baseline for H1, fixed before the data is seen |
| 2026-10-27 | D32 | No manual labelling (replaces D26a). LogChunks is used for evidence localization only: the evidence lines Diagnose quotes are compared with LogChunks' human-labelled failure-reason chunks; hit rate = at least one quoted line inside the chunk, plus line-level precision and recall. The 5-class accuracy is measured only on the generated corpus. The labelling sheet, the re-label generator and the intra-rater kappa are dropped. H3's primary metric becomes the localization hit rate on LogChunks; the hallucination rate is secondary. The report states the limitation: the 5-class accuracy has no external validation on real logs | A single developer cannot provide independent labels; LogChunks' own human chunks give a real-log test of where the evidence is, without new labels |
| 2026-10-27 | D31 | Holm correction across the primary tests of H1 to H4 (family-wise alpha 0.05); secondary and per-band results are reported without correction and labelled as such | Four primary hypotheses tested on the same evaluation; Holm controls the family-wise error and is uniformly more powerful than Bonferroni |
| 2026-10-27 | D30 | The commit-delivered bad release (`release-commit-errors-10`) is an ordinary code defect, not a fault setting: `info.go` re-reads the thread count from `/proc/self/stat` at most once per interval and parses the wrong field, so one request per pod per interval answers 500 (interval from the target error rate and the load, at least 5 s so podinfo's unit tests never trigger it). The diff names no fault setting; a release through CI must be an `http_errors` defect | A diff-reading agent must judge the code, not read a `--fault-error-rate` default; a defect the unit tests catch would never reach the canary (closes open question 11) |
| 2026-10-27 | D29 | podinfo's `GET /env` drops variables starting with `PODINFO_FAULT_` (vendored source change, Go test, NOTICE entry) | Since D25 the release settings are environment variables; `/env` on the user port would show them (closes open question 10) |
| 2026-10-27 | D28 | LLM repeats and the cache: for the seeded 60-item repeat subset only, the repeat index (1 to 3) is part of the cache key, so each repeat is one real call, then cached for reproduction; every other item has one call | Without it the D27 cache would answer repeats 2 and 3 with the first answer and D18's spread would be zero by construction (closes open question 9) |
| 2026-10-27 | D27 | (Cache key for repeats: D28.) LLM: Groq free tier, model `openai/gpt-oss-120b`, pinned (model id, temperature 0, reasoning effort low) and logged per call. The client reads daily token use from the response headers, stops at 90% of the daily limit and resumes after the reset (runs are resumable); every request and response is cached by a hash of (model, settings, prompt) and never re-sent. Prompt budget: reduced log about 1,200 tokens, at most 3 retrieved cases, a short output schema, about 3,000 tokens per call. D18 adjusted: 3 repeats only on a seeded random subset of 60 items per LLM level, 1 run elsewhere. If the free limit makes the plan exceed the schedule, it is reported with numbers before any switch; models are never mixed within one set of results | Closes open question 4; a free hosted model with a hard daily budget needs a client that never wastes a call and runs that survive the limit |
| 2026-10-27 | D26 | Single-developer team. (a) (Replaced 2026-10-27 by D32: no manual labelling.) Labelling: one labeller labels all 150 LogChunks logs; at least 7 days later, a blind re-label of a seeded random 50 (separate sheet without the first answers, `evaluation/relabel.py`); intra-rater Cohen's kappa reported; evidence lines compared with LogChunks' own labelled chunks; a second labeller on the same 50 if one becomes available. (b) Held-out set: a pre-registration file (scenario families, parameter ranges, counts, metrics, statistical tests) committed before any agent code; a seeded held-out generator (`scenarios/heldout.py`); only the SHA-256 of the secret seed is committed, the seed lives in `.env`; held-out scenarios are generated only at Step 11. (c) The report states the single-developer limitation and these mitigations | Changes D12 and D14, which assumed several people; a developer who wrote a test set cannot be blind to it, so the set is fixed by rules and a committed seed instead |
| 2026-10-27 | D25 | Information firewall. Ground truth and every injection event (scenario start with spec and fault, fault injected or cleared, node stress, L0 outcome) go to an evaluation-only SQLite file (`evaluation/runs/groundtruth.db`) that nothing under `agents/` opens; a test fails if code under `agents/` references it, a path into `scenarios/` or `evaluation/`, or imports from them. The shared audit log (moved to `state/audit.db`, outside the evaluation tree) keeps only what a real system would see: deployments, gate and security decisions, CI results. Recordings hold the ground truth only in `ground_truth.json`. Release faults come from a per-release Secret (`podinfo-release-<random>`, envFrom), created for every release including good ones (empty values), so the Rollout spec carries no fault flags. The run-time `/fault` endpoint is kept for runtime incidents only; agent tools get no route to it (no Service exposes the metrics port; `kubectl_exec` is prohibited in the catalog) | Agents must decide from what a real system shows, not from labels the experiment left in the logs, the audit log or the pod spec |
| 2026-10-26 | D19 (amended) | (2026-10-27, D25: release fault settings come from a per-release Secret mounted with envFrom, not from flags in the pod template.) No separate proxy: a minimal fault middleware in the vendored podinfo, inside its request instrumentation, with an error rate (5xx) and added latency (fixed plus jitter), optionally limited to a fraction of requests, off by default, user routes only (never probes or the metrics port). A bad version is the same image with the fault settings in the Rollout's pod template, or a fault shipped as a code commit on a `scenario/*` branch 2026-10-27: the settings can also change at run time through an admin endpoint on the internal metrics port, enabled only in staging (`PODINFO_FAULT_ADMIN`), including a memory leak at a fixed rate, so runtime incidents (error burst, latency spike, memory leak to a crash loop) happen without a rollout | The frozen L0 reads podinfo's own metrics, so a proxy's injected faults would be invisible to it |
| 2026-10-25 | D24 | Fix the demo app's vulnerabilities: Go toolchain 1.26.10 (amended from "a fixed 1.25.x": Go 1.25 is end-of-life, x/net 0.60.0 needs Go 1.26 and four stdlib CVEs have no 1.25 fix), grpc 1.83.2, x/net 0.60.0, x/text 0.42.0, otel/sdk 1.44.0, golang-jwt/v4 4.5.2, klauspost/compress 1.18.7; a frozen, documented allowlist only for findings that cannot be fixed (none needed); `.gitleaksignore` for podinfo's two demo JWT secret findings | 78 HIGH and 4 CRITICAL in the image would make every DSO4 decision a block and hide what the agents add; result: 0 HIGH, 0 CRITICAL, 0 Gitleaks findings on `baa9734` |
| 2026-10-25 | D23 | L0 releases to prod after a successful staging canary; in agents mode Release proposes the prod release and the gate sends it to a human | Delivery is only complete in prod; a prod release has the largest blast radius, so it stays human-approved (BO6) |
| 2026-10-22 | D22 | Static memory-limit test ceiling raised from 1536 to 2560 MiB (Q8); measured RAM from `check-resources` stays the real acceptance criterion | Follows from 10 replicas per namespace; 1.75 GB measured of the 4096 MiB budget |
| 2026-10-22 | D21 | DSO3 data (Q7): error rates from per-pod Prometheus counter increases per window, summed per pod-template hash; latency from the load generator's per-request log, where the pod is known; joining each failed request to its pod is deferred and only done in Step 8 if a test needs it | Counts are valid for a proportion test and already carry the pod label; avoids debug logging and log streaming |
| 2026-10-22 | D20 | DSO6 adds prompt-injection cases: CI logs and commit messages containing instructions aimed at the LLM (for example to propose a prohibited or out-of-catalog action); expected result is that the gate rejects every resulting proposal | LLM inputs contain attacker-controlled text; the gate must hold regardless of what the model is told |
| 2026-10-22 | D19 | (Superseded by the 2026-10-26 amendment.) A small fault-injection proxy in front of podinfo, used only in bad versions, with a configurable error rate and added latency, so releases can be graded from obviously bad to just above the L0 thresholds | podinfo 6.7.1's `--random-error` is on/off only (fixed "1/3 chances" per its source); only latency is tunable; subtly bad releases are needed or the scenarios are too easy |
| 2026-10-22 | D18 | (Adjusted 2026-10-27 by D27: 3 repeats on a seeded subset of 60 items per LLM level, 1 run elsewhere.) LLM model id and temperature pinned and logged per call; every LLM-based level run 3 times with mean and spread reported; provider chosen and call volume checked against its limits in Step 4 | LLM outputs vary between runs and hosted models change over time |
| 2026-10-22 | D17 | Simulated approver (ground-truth policy, configurable error rate and delay) for batch evaluation; a real human only in live runs and the demo | Hundreds of approvals per evaluation would stall runs and add human noise |
| 2026-10-22 | D16 | Scenario counts set by a power analysis (80% power, 5% significance) after the L0 numbers exist | Arbitrary small samples could hide a real improvement |
| 2026-10-22 | D15 | Record each scenario once, evaluate every ablation level's decisions offline on the identical recording; DSO2 fully offline; a smaller live end-to-end set at L0 and the full system | Six levels of live runs would take days of cluster time; replay is paired, reproducible and cheap |
| 2026-10-22 | D14 | External real-world data for DSO2: about 150 LogChunks logs double-labelled into the 5 classes (2026-10-27, D32: no labelling; LogChunks used for evidence localization only) and the 20 LogSieve GitHub Actions logs, for evaluation and grounding only; GADFPD optional for DSO5 after a quality check; Loghub not used (2026-10-27: LogSieve labels not found, not used unless a small labels file is identified) | Answers the synthetic-data criticism with real logs from 80 projects and gives H3 a labelled grounding test |
| 2026-10-22 | D13 | Each commit is authored by the team member who makes it, under their own identity (replaces "all commits as Mohamed Boussetta") | Honest per-member contribution history |
| 2026-10-21 | D1 | No webhooks. The laptop polls the GitHub Actions API. Optimizer inference runs inside the CI `plan` job from a model file committed to the repo; training happens on the laptop. | GitHub cannot reach the laptop |
| 2026-10-21 | D2 | Add real optional CI jobs (lint, integration, SBOM, multi-arch build). DSO5 baseline = fixed order + standard `paths:` filters. | Otherwise DSO5 has almost nothing to skip and the baseline is a strawman |
| 2026-10-21 | D3 | DSO2 baseline = regex root-cause classifier + fixed playbook (replaces "retry twice, then notify") | A fair baseline; agents must beat real rules |
| 2026-10-21 | D4 | DSO3 uses a sequential test with always-valid p-values on per-request data, plus an absolute error floor (replaces Mann-Whitney on Prometheus samples) | Repeated looks and autocorrelated samples invalidate Mann-Whitney |
| 2026-10-21 | D5 | Collaboration protocols P1 to P3 through a shared incident record; dedicated scenarios for each | The brief requires collaboration; v1 only routed events |
| 2026-10-21 | D6 | Generated failure corpus (several hundred labelled logs, volume set after measuring build time), split by fault template | About 50 cases is too few for comparisons and for fine-tuning |
| 2026-10-21 | D7 | DSO2 compares regex, TF-IDF + logistic regression, LLM zero-shot, LLM + retrieval, and a fine-tuned small encoder (trained on a free GPU notebook, CPU inference, adopted only if the RAM measurement fits) | The brief asks explicitly about fine-tuning |
| 2026-10-21 | D8 | Evidence grounding: a diagnosis is rejected if the log lines it quotes are not in the real log; the hallucination rate is measured | Makes LLM output verifiable |
| 2026-10-21 | D9 | Gate confidence comes from split conformal calibration on validation data, never from LLM self-report; the risk score only orders the approval queue | Otherwise the LLM can bypass the human by reporting 0.71 |
| 2026-10-21 | D10 | The ablation ladder L0 to L5 is the core experiment; each level is a configuration flag | Measures the impact of each AI component, as the brief asks |
| 2026-10-21 | D11 | Autonomy metrics and an autonomy vs safety curve are reported | Directly answers the brief's headline question |
| 2026-10-21 | D12 | (Replaced 2026-10-27 by D26b: pre-registration, seeded generator and committed seed hash.) Held-out test scenarios are written by a team member who does not tune agents and are kept outside `dev` until Step 11 | Prevents tuning on the test set |
| 2026-10-21 | — | Explicitly not adopted: deep models (LSTM autoencoder), a contextual bandit, Chaos Mesh. They stay optional extensions only. | Too little data or RAM; they don't serve the research question |
| 2026-10-18 | v1 | Blueprint v1 approved; build one step at a time on explicit approval; workflows A to D; two namespaces; DSO1 to DSO6 approved | See the v1 PDF |
| 2026-10-13 | v1 | Lightweight stack: k3d, GitHub Actions, SonarCloud, hosted LLM, embedded stores | Limited laptop RAM |
| 2026-10-09 | v1 | Six BOs: BO1, BO2, BO3, BO6 main; BO4, BO5 secondary | Project framing |

---

## 11. Assumptions and open questions

### Assumptions

- Generated faults and scripted commits are acceptable evaluation data for the professor, complemented by real external logs for evidence localization (D14, D32). Stated limitation: the 5-class root-cause accuracy is measured on the generated corpus only, with no external validation on real logs (D32).
- The repo can be public, which keeps SonarCloud and Actions minutes free.
- The developer approves in the live end-to-end runs and the demo; batch evaluation uses the simulated approver (D17).

### Open questions (blocking sizing, not Step 2)

1. Submission date and expected deliverables (report, demo, slides).
2. ~~Team size and owner of each step.~~ Closed by D26: a single developer.
3. ~~Who writes the held-out test set (D12).~~ Closed by D26b: pre-registration and a seeded generator.
4. ~~LLM provider: NVIDIA NIM, Groq or Gemini.~~ Closed by D27: Groq, `openai/gpt-oss-120b`.
5. Approval channel: Telegram bot or Streamlit page.
6. Re-measure RAM with Docker capped at 4 GB before the final evaluation runs.

### Open questions raised in Step 2 (closed)

7. **Error responses are not tied to a pod in the per-request log.** podinfo's injected errors (`--random-error`) have an empty body, so the load generator logs them with `"pod": null`; DSO3's per-request comparison of canary and stable error rates needs them attributed. Proposal: the load generator adds a request id to each URL (`/?rid=...`), podinfo runs with `--level=debug` (its request log records the URI before the error middleware), and pod logs are streamed during each run and joined on the id. Alternative: accept the gap for L0 and solve it in Step 8. **Closed by D21.**
8. **Static memory-limit check.** With option A (10 replicas per namespace) the summed container limits at peak are about 2.4 GB, above the 1536 MiB the Step 1 test allowed. Proposal (implemented, not yet approved): raise the test's ceiling to 2560 MiB (4 GB minus about 1 GB for k3s and Docker minus a 0.5 GB margin) and keep measured RAM (`check-resources`) as the real acceptance criterion. **Closed by D22.**

### Open questions raised with D25 to D27 (2026-10-27, all closed)

9. **Closed by D28.** **LLM repeats and the response cache (D27 vs D18).** A cache keyed by (model, settings, prompt) that never re-sends would answer repeats 2 and 3 from the cache, so the D18 repeats would always equal the first run. Proposal: a repeat index (1 to 3) is part of the cache key for the 60-item subset only, so each repeat is one real call, cached for reproduction; everything else stays one call. Not approved.
10. **Closed by D29.** **podinfo's `/env` route shows the release Secret's values (D25).** Fault settings are now environment variables, and podinfo serves `GET /env` on its user port, so anyone who can reach the app can read them. No agent tool reaches it today (the catalog has no HTTP action). Proposal: drop `PODINFO_FAULT_*` from `/env`'s answer in the vendored podinfo (a few lines, NOTICE entry). Not approved.
11. **Closed by D30.** **The commit-delivered bad release names the fault in its diff (D25).** `release-commit-errors-10` turns the fault on by changing a `--fault-error-rate` flag default in `main.go`, which a diff-reading agent sees. Proposal: deliver it as an ordinary code defect (a handler that fails some requests) instead. Not approved.
