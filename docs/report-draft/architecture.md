# Architecture

Draft for the report. This section describes the design only; results are in the Method and
Results sections. Decision numbers (Dn) refer to the decision log in `docs/blueprint-v2.md`.

## Layers

- **Cloud.** GitHub and GitHub Actions run the build, the tests, the optional jobs, the
  scans, the image push and the Optimizer's `plan` job. Other cloud parts are a hosted LLM
  (Groq, `openai/gpt-oss-120b`, pinned; D27) and the human approval channel.
- **Laptop, agent service.** One Python process holds:
  - the orchestrator and the five agents;
  - the shared incident record and the risk gate;
  - the executor and the audit log.
- **Laptop, k3d cluster:**
  - podinfo in two namespaces, staging and prod, each an Argo Rollouts canary over 10 replicas;
  - Prometheus, Alertmanager, kube-state-metrics and Grafana;
  - a load generator.

**Communication.** The laptop polls; nothing pushes to it (D1). The agent service reads
Prometheus every 30 s and the GitHub Actions API for CI runs. Optimizer inference runs inside
CI, from a model file committed to the repo.

## Agents

Each agent receives only what a real system would show it: logs, metrics, scan counts,
changed files, and evidence from the incident record. Each returns proposals of catalog
actions, label scores for calibration, and evidence posts. **No agent sets a risk tier or
executes anything.** Which actions each agent may propose is fixed in `AGENT_ACTIONS`
(`agents/flowguard/catalog.py`).

| Agent | Objective | May propose |
|---|---|---|
| Monitor | DSO1, detection | Nothing: it opens incidents (D49) and answers P1 |
| Diagnose | DSO2, CI root cause | `retry_job`, `rerun_workflow`, `restart_deployment`, `pin_dependency`, `open_fix_pr`, `escalate_to_human` |
| Release | DSO3, canary decisions | `rollout_promote`, `rollout_set_weight`, `rollout_abort`, `rollout_undo`, `escalate_to_human` |
| Security | DSO4, deploy decision | `block_deployment`, `request_security_review`, `allow_deployment`, `allow_with_findings` |
| Optimizer | DSO5, CI plan | `reorder_jobs`, `skip_jobs` |

**Collaboration** (D5) goes only through the shared incident record (an SQLite table of
incidents, evidence posts and agent requests). Agents exchange evidence, never actions.
- **P1, rollback check.** Before `rollout_abort` or `rollout_undo`, Release asks Monitor
  whether the node is under stress, and asks Diagnose whether the cause looks release- or
  infrastructure-caused. On infrastructure evidence it holds and escalates.
- **P2, forced scans.** The Optimizer may not skip the dependency or security jobs after a
  dependency change, or after Security flagged the previous run.
- **P3, conflicts.** See the gate.

## Orchestrator

One LangGraph graph covers four workflows:

| Workflow | Path |
|---|---|
| A, new commit | plan, CI, Security, canary |
| B, CI result | Monitor, Diagnose, gate, executor, rerun |
| C, runtime anomaly | Monitor, then Release if the service is mid-canary, else Diagnose |
| D, canary step | Release |

- **Monitor first (D49).** Metrics ticks and CI results go to the Monitor first. An incident
  record is opened only when the Monitor reports one, with its detector and score.
- **Release first.** A canary look runs Release first. A look where Release holds opens no
  incident.
- **Gate path.** Every proposal then goes to the gate, every `ask_human` decision to the
  approver, and every allowed decision to the executor.

## Ablation levels (D10)

Each level is a configuration flag of the same code; no level has its own code path.

| Level | Adds | Gate |
|---|---|---|
| L0 | Rule baselines: the frozen alert rules, the regex classifier and playbook, the frozen canary analysis, the block-on-finding security gate, the fixed CI plan | None (DSO6's baseline); a dry-run executor only |
| L1 | Statistical and ML detectors and classifiers | Rules 0 to 7 |
| L2 | LLM diagnosis, zero-shot | Rules 0 to 7 |
| L3 | Retrieval and evidence grounding (D8) | Rules 0 to 7 |
| L4 | Collaboration protocols P1 to P3 | Rules 0 to 7 |
| L5 | L4 with the gate's confidence threshold swept (autonomy vs safety, H5) | Rules 0 to 7, threshold varied |

Final-evaluation LLM calls happen only at L2 and L3. L4 and L5 reuse L3's cached answers for
identical inputs (D39).

## Action catalog and risk tiers

Tiers are fixed in code review (`agents/flowguard/catalog.py`). No agent and no LLM output can
change them, and anything not listed is prohibited.

| Tier | Actions |
|---|---|
| Low (executes) | `retry_job`, `rerun_workflow`, `rollout_set_weight`, `block_deployment`, `allow_deployment`, `request_security_review`, `reorder_jobs`, `escalate_to_human` |
| Medium (executes in staging at calibrated confidence ≥ 0.70; otherwise a human decides) | `restart_deployment`, `scale_deployment`, `pin_dependency`, `open_fix_pr`, `rollout_promote`, `rollout_abort`, `skip_jobs` |
| High (always a human) | `rollout_undo`, `allow_with_findings` |
| Prohibited (always rejected) | `delete_namespace`, `delete_secret`, `modify_rbac`, `kubectl_exec` |

Each entry also carries:
- whether the action is reversible;
- an hourly limit per action and target, which is the loop protection.

## Risk gate

The gate (`agents/flowguard/gate.py`) applies these rules in order; the first match wins.

| Rule | Condition | Outcome |
|---|---|---|
| 0 (D36) | The proposing agent is not allowed the action | Reject |
| 1 | The action is not in the catalog | Reject |
| 2 | Prohibited tier | Reject |
| 3 | Hourly limit for (action, target) exceeded (counted from the audit log, plus proposals passed but not yet executed; D37) | Reject |
| 4 | High tier | Ask a human |
| 5 | Medium tier in prod | Ask a human |
| 6 | Medium tier with calibrated confidence below 0.70 | Ask a human |
| 7 | Otherwise | Execute |

**P3, conflicts (D5).** Proposals for the same target in the same incident are resolved
before the rules. The more conservative one goes on to the rules: reversible before
irreversible, then the lower tier. The others are rejected. If the best two cannot be ranked,
all of them go to a human.

**Confidence (D9).** The confidence the gate reads is the calibrated one, from split conformal
prediction. It is never the agent's or the LLM's own report. Without enough calibration
examples it is 0, so a medium action goes to a human.

**Risk score.** The tier base (0.15, 0.40, 0.70 or 1.0), plus 0.25 × (1 − confidence), plus
0.15 in prod, plus 0.10 if the action is irreversible, capped at 1. It orders the approval
queue only and never changes an outcome.

## Executor

The executor is the only component with write credentials: `kubectl` writes to the cluster,
and a GitHub write token for reruns.
- **It trusts no caller (D37).** Before acting, it re-reads the proposal, its gate decision
  and, for `ask_human`, the latest approval from the audit log by proposal id. It refuses if
  any is missing, differs from what it was handed, is not approved, or was already executed.
- **Without an audit log it runs nothing.** In tests and replays it is a dry run.
- **A test enforces the single-writer rule:** no other module under `agents/` runs a
  subprocess or names `kubectl`.
- **Every other cluster access is read-only:**
  - the agent service reads Prometheus;
  - Rollout status (step changes, and an aborted Rollout's follow-up) is read with a
    read-only ServiceAccount, allowed only to get, list and watch Rollouts in staging and
    prod, with a short-lived token (D59).

## Audit log and approval

- **Audit log.** An SQLite file (`state/audit.db`), insert-only. It stores every event,
  proposal, gate decision, approval and execution, each with its scenario id and ablation
  level. It holds only what a real system would see (D25).
- **Approval page.** In live runs it is a local page on 127.0.0.1 that shows pending
  proposals, most risky first, HTML-escaped, with a per-page form token. A proposal unanswered
  for 10 minutes counts as a denial.
- **Batch evaluation** uses a simulated approver instead (D17): it approves when the
  scenario's ground truth says the action is right, with a configurable error rate (0 by
  default, 10% in a sensitivity run). The approval count per incident is reported either way.

## Information firewall (D25)

Agents decide from what a real system shows, never from what the experiment knows.

- **Ground truth.** Ground truth and every injection event (scenario start with its spec and
  fault, fault injected or cleared, node stress, L0's outcome) go to an evaluation-only
  database, `evaluation/runs/groundtruth.db`. In a recording they are only in
  `ground_truth.json`, which only the scoring side reads.
- **A test** fails if code under `agents/` references the ground-truth database or a path
  into `scenarios/` or `evaluation/`, or imports from them.
- **No fault flags in what agents see.** A release's fault settings come from a per-release
  Secret (`podinfo-release-<random>`), created for every release including good ones, so the
  Rollout spec carries no fault flags. podinfo's `/env` hides them (D29).
- **No route to the runtime fault endpoint.** It is reachable only on the metrics port, which
  no Service exposes, and `kubectl_exec` is prohibited.
- **Prometheus.** It also scrapes FlowGuard's own exporters (the audit counts, and the scored
  results for the dashboard). Every selector an agent uses must name one metric literally and
  never a `flowguard_*` one. The scenario runner refuses to start a run while scored results
  are in Prometheus. The results exporter refuses to start while a run holds the run lock.
- **An agent-input leakage test** scans every input an agent receives in replay for ground
  truth and experiment words.
