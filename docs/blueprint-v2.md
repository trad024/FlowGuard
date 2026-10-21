# FlowGuard: project context (blueprint v2)

Single source of truth for FlowGuard. Every approved decision lives here. Anything not written here is not approved.

- Version: v2, 2026-10-21. Supersedes the v1 PDF blueprint where they differ; v1 stays valid for everything not changed below.
- Status: v2 decisions D1 to D13 accepted. Build position: Step 0 done; Step 1 done and verified on a team laptop (2026-10-22, commit `db24d62`): setup from zero, 9/9 smoke checks pass, cluster about 930 to 980 MiB of the 4096 MiB budget (Docker was not yet capped at 4 GB). Step 2 is next and not started.

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
| H3 | Evidence grounding lowers the LLM hallucination rate without a significant accuracy loss | Hallucination rate, macro-F1, L2 vs L3 | Held-out failure corpus |
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
| DSO2 | Classify the root cause of CI failures into 5 classes and propose a validated fix | Log reduction (drain3) + retrieval (Chroma) + LLM with structured output and evidence grounding; compared against regex, TF-IDF + logistic regression, and a fine-tuned small encoder | Regex root-cause classifier + fixed class → action playbook | Accuracy ≥ 80%; macro-F1 ≥ 0.75; ≥ 60% resolved without a human (green rerun, gate-allowed actions only); hallucination rate and cost per incident reported; fix PRs counted as assisted | Stronger baseline (D3); generated corpus (D6); 5-way comparison (D7); grounding (D8) |
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

v1 choices stand: k3d, GitHub Actions, SonarCloud, Trivy, Gitleaks, Argo Rollouts, Prometheus, Alertmanager, Grafana, podinfo, LangGraph, a hosted LLM, Chroma, scikit-learn, LightGBM, MLflow in file mode, SQLite.

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

- **Failure corpus (D6).** A scripted generator injects faults of the 5 DSO2 classes into copies of the demo repo and runs build and tests unattended in containers. It aims for several hundred labelled logs. Exact volume will be set after measuring build time, so this number is not yet a commitment.
- **Splits.** The corpus is split train / validation / test **by fault template, not by run**, so near-duplicate logs never leak into the test set.
- **Held-out protocol (D12).** One team member who does not tune agents writes the final test scenarios and seeds. They are kept outside `dev` until Step 11. Developers tune only on the dev scenarios.
- **Fairness (v1, kept).** L0 thresholds are set to industry-style values and frozen before any agent runs. All levels see the same data. Counts are reported, not only percentages.

### Statistics

- 95% Wilson intervals for every rate.
- Paired comparisons between levels on the same scenarios: McNemar's test for correct/incorrect outcomes; bootstrap intervals for time metrics.
- Canary decisions use per-request data, never autocorrelated Prometheus samples.

### Failure analysis

Every wrong decision at L3 and above gets a short entry: scenario, what the agent saw, why it was wrong, and the fix or the stated limitation. This feeds the report's limitations section directly.

---

## 7. Build plan (v2)

Each step needs explicit approval before it starts. Each step ships with tests, ruff and pytest passing.

| Step | Content | Done when | Status |
|---|---|---|---|
| 0 | Repo layout, conventions, contracts, catalog | Tests pass | Committed `0e888e8` |
| 1 | k3d cluster, podinfo ×2, monitoring stack | `setup.ps1` from zero; `smoke.ps1` passes; RAM measured ≤ 4 GB | **Done** `db24d62`: 9/9 checks, about 930 to 980 MiB. Fixes: direct port mapping, API pinned to 127.0.0.1:6550, PowerShell 5.1-safe checks |
| 2 | Argo Rollouts; podinfo as a Rollout (20/50/100); L0 AnalysisTemplate; load generator with per-request log tagged by pod | A good version promotes, a bad one aborts under L0; per-request log is usable for DSO3 | Next |
| 3 | CI: build, unit, optional jobs (lint, integration, SBOM, multi-arch), scans, push, `plan` job reading a model file; laptop deployer by polling | A commit reaches a staging canary with no manual step | |
| 4 | Failure-corpus generator; scenario runner; L0 baseline for all DSOs; audit log; held-out set frozen | Every DSO has a measured L0 number on the held-out set; final targets set | |
| 5 | Orchestrator with ablation flags L0 to L5; shared incident record; gate with calibrated confidence; executor; approval channel; stub agents | Workflows A to D run end to end with stubs; DSO6 adversarial and conflict sets pass | |
| 6 | Monitor agent | DSO1 measured at L0 and L1 | |
| 7 | Diagnose agent with the 5-way comparison and grounding | DSO2 measured at L0 to L3, plus the encoder | |
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
| 2026-10-21 | D12 | Held-out test scenarios are written by a team member who does not tune agents and are kept outside `dev` until Step 11 | Prevents tuning on the test set |
| 2026-10-21 | — | Explicitly not adopted: deep models (LSTM autoencoder), a contextual bandit, Chaos Mesh. They stay optional extensions only. | Too little data or RAM; they don't serve the research question |
| 2026-10-18 | v1 | Blueprint v1 approved; build one step at a time on explicit approval; workflows A to D; two namespaces; DSO1 to DSO6 approved | See the v1 PDF |
| 2026-10-13 | v1 | Lightweight stack: k3d, GitHub Actions, SonarCloud, hosted LLM, embedded stores | Limited laptop RAM |
| 2026-10-09 | v1 | Six BOs: BO1, BO2, BO3, BO6 main; BO4, BO5 secondary | Project framing |

---

## 11. Assumptions and open questions

### Assumptions

- Generated faults and scripted commits are acceptable evaluation data for the professor.
- The repo can be public, which keeps SonarCloud and Actions minutes free.
- A team member is available to approve during evaluation runs.

### Open questions (blocking sizing, not Step 2)

1. Submission date and expected deliverables (report, demo, slides).
2. Team size and owner of each step.
3. Who writes the held-out test set (D12).
4. LLM provider: NVIDIA NIM, Groq or Gemini.
5. Approval channel: Telegram bot or Streamlit page.
6. Re-measure RAM with Docker capped at 4 GB before the final evaluation runs.
