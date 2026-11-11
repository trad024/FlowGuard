# FlowGuard

FlowGuard is a multi-agent system that watches a DevOps delivery pipeline: GitHub Actions CI,
then a canary release on a local Kubernetes (k3d) cluster. Five read-only agents (Monitor,
Diagnose, Release, Security, Optimizer) detect and diagnose failures and propose actions from a
fixed catalog. Every proposal passes a deterministic, human-controlled risk gate before the
executor acts; the one exception is the Optimizer's CI plan, which app-ci applies directly
(D68). The project measures, on pre-registered held-out scenarios, which AI components do better
than a strong rule-based pipeline, and at what cost in autonomy risk. The AI components are
statistical detectors, learned models, an LLM, retrieval and grounding, and agent collaboration.

Every approved design decision is in [`docs/blueprint-v2.md`](docs/blueprint-v2.md). Decisions
are cited as D1 to D83 from its decision log (D52 was not approved). The evaluation plan is
[`docs/preregistration.yaml`](docs/preregistration.yaml), version 1.8, frozen.

## Prerequisites

Versions are those of the integration laptop (Windows 11) used for Step 11.

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop, WSL2 backend | Docker Engine 29.8.2, WSL 2.6.1.0 | The cluster stays under 4 GB. For Step 11, Docker was limited to 8 CPUs and about 10 GB (`.wslconfig`) on every laptop. |
| k3d | v5.9.0 (k3s v1.35.5) | v5.8 or later ([`infra/README.md`](infra/README.md)) |
| kubectl | v1.36.1 | v1.31 or later |
| Python, evaluation | 3.13.0 with the pinned stack in [`requirements-step11.txt`](requirements-step11.txt) | the environment of the calibrations and of Step 11 (pre-registration amendment of 2026-11-08): torch 2.11.0+cpu, transformers 5.13.0, scikit-learn 1.7.2, chromadb 1.5.9, lightgbm 4.6.0 |
| Python, CI | 3.11 | `pyproject.toml` declares `>=3.11,<3.12`; `checks.yml` runs ruff and pytest on 3.11 |
| ruff | 0.17.1 | pinned in `pyproject.toml` |

## Installation

1. Clone the repository and stay on `dev` (`main` is the stable branch):

   ```powershell
   git clone https://github.com/trad024/FlowGuard.git
   cd FlowGuard
   git checkout dev
   ```

2. Python. Use the evaluation environment (Python 3.13.0) to reproduce Step 11:

   ```powershell
   py -3.13 -m pip install --no-deps -r requirements-step11.txt --extra-index-url https://download.pytorch.org/whl/cpu
   $env:PYTHONPATH = "$PWD\agents"
   ```

   Or use the declared environment (Python 3.11, as CI does):

   ```powershell
   py -3.11 -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -e ".[dev]"
   ```

3. Secrets. Copy [`.env.example`](.env.example) to `.env` and fill in your own values. `.env` is
   never committed. Each variable is optional for the step that does not use it:

   | Variable | Used by |
   |---|---|
   | `GITHUB_TOKEN_READ` | the deployer and the agents, read-only |
   | `GITHUB_TOKEN_WRITE` | the executor only (D63) |
   | `LLM_API_KEY` | the LLM, plus `GROQ_KEY_1` to `GROQ_KEY_5` as extra lanes for batch runs (D41) |
   | `FLOWGUARD_HELDOUT_SEED` | held-out generation only |

## Running it

Commands run from the repository root. The `.sh` versions do the same on Linux or macOS.

1. Cluster, created and deployed (safe to rerun), then checked:

   ```powershell
   .\infra\scripts\setup.ps1
   .\infra\scripts\smoke.ps1          # 9/9 checks pass
   ```

   Prometheus is at http://localhost:9090, Grafana at http://localhost:3000, and podinfo staging
   and prod at :9898 and :9899. `teardown.ps1` deletes it all. Windows can reserve these ports
   (WinNAT); [`docs/step-11-run-summary.md`](docs/step-11-run-summary.md), item 1, gives the fix.

2. Agent service. Without `--live`, the executor is a dry run.

   ```powershell
   py -m flowguard.service --level L1 --live
   ```

   It prints the approval page URL, http://127.0.0.1:8765/ (loopback only). A proposal the gate
   sends to a human waits there for up to 10 minutes; no answer counts as a denial.

3. Demo. [`docs/demo.md`](docs/demo.md) gives the terminals, the Grafana dashboard
   **FlowGuard / agents vs L0** with its two exporters, and two loops with dry-run timings:
   - a runtime fault: incident, Diagnose, gate, recovery;
   - a prod release held at 20%, approved by a human on the approval page, then promoted to 100%
     by the executor.

4. Checks. `ruff check .`, `ruff format --check .` and `pytest`, as in `checks.yml`. Unit tests
   are refused any call to kubectl, Docker, k3d or a non-loopback address (`conftest.py`).

## Reproducing the evaluation

The procedure, block by block, is [`docs/step-11-runbook.md`](docs/step-11-runbook.md); block 11 is
the order actually used (D79). What each part needs:

| Part | Needs | Re-runnable by anyone? |
|---|---|---|
| Unit tests and the report toolkit on synthetic fixtures (`evaluation/report_demo.py`) | the repository | yes |
| Development scenarios (`scenarios/dev/`) and the dev pilot | the repository, the cluster, GitHub and LLM credentials | yes, as new live runs |
| Checking that the held-out set was fixed in advance | `scenarios/heldout-seed.sha256` (committed) against the seed | anyone holding the seed can run `scenarios/heldout.py verify-seed` |
| Generating the held-out set (319 scenarios, DSO5 commits, DSO6 items) and recording it (`pilot.py heldout`) | the secret seed (`FLOWGUARD_HELDOUT_SEED`), the frozen tags `eval-freeze-d75` and `eval-freeze-d76` and `diagnose-freeze`, the cluster, GitHub and LLM credentials | only with the seed. Generation refuses without it, and recording is live: a re-run is a new measurement, not a replay |
| Scoring the held-out recordings (replays, `merge_heldout.py`, `report.py --confirmatory`, `dso5_metrics.py`, `exploratory_heldout.py`) | the recordings and databases of the three laptops, kept outside the repository (gitignored) | with those inputs, offline and deterministic. Their SHA-256 are in `docs/step-11-results.md` |
| Reading the results and every decision behind them | the committed docs | yes: `docs/step-11-results.md`, `docs/step-11-run-summary.md`, `docs/blueprint-v2.md`, `docs/preregistration.yaml` |

Recordings, the corpus, model weights, `scenarios/heldout/`, `results/` and `.env` are never
committed.

## Project structure

| Folder | Contents |
|---|---|
| `.github/` | `app-ci.yml` (build, tests, optional jobs, scans, image push, the Optimizer's plan job) and `checks.yml` (lint and tests on every push) |
| `agents/` | The agent service (`flowguard` package): contracts, catalog, orchestrator, the five agents, gate, executor, audit log, approval page, exporters |
| `app/` | podinfo 6.7.1, the application the pipeline builds and deploys |
| `ci/` | Scripts the CI calls, the laptop deployer, and the Optimizer, Security and encoder training code |
| `docs/` | Blueprint and decision log, pre-registration, step reports, Step 11 runbook, results and run summary, demo guide |
| `evaluation/` | Load generator, recorder, ground-truth store, offline replays, scoring and the report toolkit |
| `infra/` | k3d cluster, Kubernetes manifests (podinfo Rollouts, Prometheus, Grafana), setup and check scripts |
| `notebooks/` | The encoder fine-tuning notebook (D7) |
| `scenarios/` | Scenario specs, the scenario runner and pilot driver, CI failure templates, leakage checks, the held-out generator |
| `scripts/` | The freeze scripts (`eval-freeze`, `harness-diagnose-freeze`) |
| `results/`, `state/` | Created at run time, never committed: report outputs; audit log, LLM cache and ledgers, logs |

## Results summary

Full table with sources, the limitations and the deviations after unblinding:
[`docs/step-11-results.md`](docs/step-11-results.md). Incidents during the run and their effect:
[`docs/step-11-run-summary.md`](docs/step-11-run-summary.md).

- **H1 (detection, confirmatory): supported.** DSO1 recall rises from 145/219 (0.662) at L0 to
  209/219 (0.954) at L1. Exact McNemar 64 vs 0, Holm-adjusted p = 2.2e-19. L1 is L0 plus the
  detector, so the discordance is zero by construction.
- **H3 (evidence localization, confirmatory): not supported, and uninformative.** L2 5/230 vs L3
  4/230, p = 1. The request cap (D66) cut the target lines of 180 of 230 items at both levels.
- **H2 (diagnosis, secondary):** on 21 items, L3 macro-F1 0.95 vs regex 0.46. The difference is
  0.49 [0.22, 0.69], and 0.38 [0.10, 0.61] on the 12 corpus items alone. The items come from
  about 5 templates.
- **H4 (release, descriptive):** n = 0 in the toolkit (a truth-key mismatch). By the
  pre-registered metric (exploratory), there were 0/13 false rollbacks at L3 and at L4 on
  node-stress good releases.
- **H5 (autonomy):** not computed by the toolkit. On the 12 live L5 ci incidents, 0 were resolved
  without a human (exploratory). There were 0 unapproved high-risk actions.
- **DSO3:** bad releases caught before 100%: 23/36 at L0, 22/36 at L3 and L4. There were 0/36
  false rollbacks.
- **DSO4:** every deploy was blocked, low-severity controls included (3/3), so the scan gate
  showed no discrimination.
- **DSO5:** the Optimizer gave no reliable saving (median +4 s [-39, 35] on 25 commits), and its
  pipeline failed to start on 5 of 30 commits.
- **DSO6:** 300 adversarial items. 43/43 high-risk proposals were held, and nothing risky was
  executed without approval.

Main limitations: H3 rests on few surviving targets, and H2 on few templates. H4, H5 and DSO4
were only measurable outside the toolkit, as exploratory tables (D82). The frozen encoder was not
run on held-out items. The pooled MTTD mixes two families: runtime -77 s, pipeline 0 s.

## Status

All build steps (0 to 11) are done. The held-out evaluation was unblinded on 2026-11-10. The
confirmatory report ran on the frozen toolkit with pre-registration 1.8 (`results/final`,
confirmatory true, Holm family H1 and H3). The exploratory tables are labelled as such.
Development results (Steps 4 to 10) are recorded in the blueprint and the step reports. The
held-out results above supersede them for any claim.

## Architecture

One Python process on the laptop (the agent service) polls GitHub and Prometheus. Nothing calls
in, because GitHub cannot reach the laptop (D1).

```
GitHub Actions (CI, scans, plan job) ─┐
Prometheus (k3d cluster: podinfo     ─┼─► agents ──proposals──► risk gate ──► human approval
  staging + prod, Argo Rollouts)      │     ▲                     │            (ask_human only)
                                      │     └── incident record ◄─┤                 │
                                      │         (shared evidence) ▼                 ▼
                                      └────────────────────── audit log ◄────── executor
```

- **Agents** (`agents/flowguard/`), each read-only:
  - Monitor detects runtime and CI incidents.
  - Diagnose classifies CI failures into 5 root-cause classes and proposes a fix.
  - Release decides each canary step.
  - Security allows, reviews or blocks a deployment.
  - Optimizer plans the CI's optional jobs.

  Each agent only proposes one action from the catalog. Agents exchange evidence, never
  actions, through a shared incident record (collaboration protocols P1 to P3, D5).
- **Risk gate** (`gate.py`): deterministic rules over a static catalog of actions and risk tiers
  (`catalog.py`). Tiers are set by humans in code review; no agent or LLM can change one. The
  gate reads a calibrated confidence (split conformal prediction, D9), never a model's
  self-report.
- **Executor** (`executor.py`): the only component with write credentials. Before acting, it
  re-reads the proposal, the gate decision and any approval from the audit log. It refuses
  anything missing, altered, unapproved or already executed (D37).
- **Audit log** (`audit.py`, SQLite in `state/audit.db`): every event, proposal, gate decision,
  approval and execution, stamped with scenario id and ablation level.

Workflows A to D and the gate rules: [blueprint section 5](docs/blueprint-v2.md#5-architecture).

## How the evaluation works

- **Ablation ladder (D10):** every level runs on the same scenarios, as a configuration flag of
  the same code.

  | Level | Adds |
  |---|---|
  | L0 | Strong rule baseline: frozen alert rules, regex classifier and playbook, Argo AnalysisTemplate, scan-severity gate |
  | L1 | Statistical and ML detectors and classifiers, no LLM |
  | L2 | LLM diagnosis, zero-shot |
  | L3 | Retrieval and evidence grounding |
  | L4 | Collaboration protocols P1 to P3 |
  | L5 | The full system live, with a sweep of the gate's confidence threshold (autonomy vs safety); Step 11 ran the default setting only |

- **Record and replay (D15):** each scenario is run live once on the cluster. Its raw inputs are
  recorded (Prometheus samples, per-request load logs, CI logs and events). Every level then
  decides offline on the identical recording, so comparisons are exactly paired. Live L5 results
  (the 12 ci incidents, DSO5, DSO6) are reported separately.
- **Information firewall (D25):** ground truth lives only in an evaluation store that no agent
  code can open (`agents/tests/test_firewall.py`). A leakage test scans everything an agent
  receives.
- **Pre-registration (D26b):** [`docs/preregistration.yaml`](docs/preregistration.yaml) fixed
  everything before any held-out result existed:
  - families, counts, metrics, tests, exclusions and run order;
  - the SHA-256 of the secret seed.

  Version 1.0 was frozen on 2026-11-06. Every later change is a dated amendment made before any
  held-out result existed. The last is version 1.8 (D76, 2026-11-08). Changes after unblinding
  (D79 to D83) are procedure and reporting only, listed in `docs/step-11-results.md`.
- **Staged freezes (D39, D69):** `diagnose-freeze` and `harness-diagnose-freeze` freeze Diagnose
  and its held-out harness. `eval-freeze` (then `eval-freeze-d72`, `-d75` and `-d76`) freezes the
  agent code and the harness. The held-out generator and drivers refuse to run unless the freezes
  hold and the seed matches its committed hash.
- **Statistics:**
  - Every rate comes with n and a 95% Wilson interval.
  - Paired levels are compared with an exact McNemar test; time metrics get paired bootstrap
    intervals (10,000 resamples, seed 20261028).
  - The confirmatory tests are H1 and H3, with Holm correction across them. H2 and H4 are
    descriptive (D42, D56).

## Safety rules

- **Agents never act.** An agent proposes one action from the catalog. Only the executor writes,
  and only after the gate, and a human where required, allowed it.
- **Exception (D68): the Optimizer.** app-ci's plan step (`ci/plan.py`) applies its skip list
  directly, bounded by P2 and the mandatory jobs: build, unit tests and scans can never be
  planned away. It does not pass through the gate or the executor.
- **Never allowed:** an action outside the catalog, an action the proposing agent may not
  propose (`AGENT_ACTIONS`, D36), and the prohibited tier (`delete_namespace`, `delete_secret`,
  `modify_rbac`, `kubectl_exec`).
- **Gate rules, first match wins:**
  1. reject what the agent may not propose;
  2. reject what is not in the catalog;
  3. reject the prohibited tier;
  4. reject past the per-hour loop limit;
  5. ask a human for the high tier;
  6. ask a human for the medium tier in prod;
  7. ask a human for the medium tier below calibrated confidence 0.70 (D67);
  8. otherwise, execute.
- **Human approval:** on a loopback-only page with escaped text and a form token. No answer
  within 10 minutes is a denial.
- **Refusals:** the executor refuses a decision it cannot find unchanged in the audit log, and
  any proposal already executed (D37). An L0 run has no gate, so it runs only with a dry-run
  executor. Prompt-injection text in logs or commits cannot get an action past the gate (D20).
- **The LLM** returns JSON from a fixed schema and never commands. Its settings are pinned and
  every call is logged (D27).
- **Credentials:** agents hold read-only credentials. The write token is used only by the
  executor and lives in `.env`, never committed (D63).
