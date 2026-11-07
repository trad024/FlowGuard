# FlowGuard

FlowGuard is a multi-agent system that watches a DevOps delivery pipeline (GitHub Actions CI,
then a canary release on a local Kubernetes cluster). It detects and diagnoses failures and
proposes fixes. Every agent proposal passes a fixed, human-controlled risk gate before anything
acts; the one exception is the Optimizer's CI plan, which app-ci applies directly (D68).
The project measures, on pre-registered held-out scenarios, which AI components (statistical
detectors, learned models, an LLM, retrieval and grounding, agent collaboration) do better than a
strong rule-based pipeline, and at what cost in autonomy risk.

Every approved design decision is in [`docs/blueprint-v2.md`](docs/blueprint-v2.md). Decisions
are cited as D1, D2, … from its decision log.

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
  - Agents only propose one action from the catalog. They exchange evidence through a shared
    incident record (collaboration protocols P1 to P3, D5), never actions.
- **Risk gate** (`gate.py`): deterministic rules over a static catalog of actions and risk tiers
  (`catalog.py`). Tiers are set by humans in code review; no agent or LLM can change one. The
  gate reads a calibrated confidence (split conformal prediction, D9), never a model's
  self-report.
- **Executor** (`executor.py`): the only component with write credentials. Before acting, it
  re-reads the proposal, the gate decision and any approval from the audit log, and refuses
  anything missing, altered, unapproved or already executed (D37).
- **Audit log** (`audit.py`, SQLite in `state/audit.db`): every event, proposal, gate decision,
  approval and execution, stamped with scenario id and ablation level.

Architecture, workflows A to D and the gate rules: [blueprint section 5](docs/blueprint-v2.md#5-architecture).

## Repository map

| Folder | Contents |
|---|---|
| `.github/` | The two workflows: `app-ci.yml` (build, tests, optional jobs, scans, image push, `plan` job) and `checks.yml` (lint and tests on every push) |
| `agents/` | The agent service (`flowguard` package): contracts, orchestrator, the five agents, gate, executor, audit log, approval page, exporters |
| `app/` | podinfo 6.7.1, the demo application the pipeline builds and deploys |
| `ci/` | Scripts the CI calls, the laptop deployer, and the Optimizer, Security and encoder training code |
| `docs/` | Blueprint, pre-registration, step reports, demo guide |
| `evaluation/` | Load generator, recorder, ground-truth store, offline replay, scoring and the report toolkit |
| `infra/` | k3d cluster, Kubernetes manifests (podinfo Rollouts, Prometheus, Grafana), setup and check scripts |
| `notebooks/` | The encoder fine-tuning notebook (D7) |
| `scenarios/` | Scenario specs, the scenario runner, CI failure templates, the leakage checks, the held-out generator |

## Quick start (Windows)

Prerequisites are in [`infra/README.md`](infra/README.md): Docker Desktop (WSL2, about 4 GB of
memory), k3d, kubectl and Python 3.11 (`pyproject.toml` requires 3.11; CI uses it). Commands run
from the repository root.

**1. Python environment.**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

One team laptop runs everything without a virtual environment, on Python 3.13 with the
dependencies installed and `PYTHONPATH=agents` instead of the editable install. The full test
suite passes there, but 3.13 is outside the declared range: use 3.11 for anything reported.

**2. Cluster.** This creates the k3d cluster and deploys everything, and is safe to rerun; then
the checks.

```powershell
.\infra\scripts\setup.ps1
.\infra\scripts\smoke.ps1
```

```bash
./infra/scripts/setup.sh
./infra/scripts/smoke.sh
```

Prometheus is then at http://localhost:9090, Grafana at http://localhost:3000, and podinfo
staging and prod at :9898 and :9899. `teardown` deletes it all.

**3. Agent service.** `pip install -e .` puts the `flowguard` package (`agents/flowguard`) on the
path. Without it, set `PYTHONPATH` to `agents`; the scripts under `scenarios/`, `evaluation/`
and `ci/` add their own folders to the path, and pytest reads `pythonpath` from
`pyproject.toml`. Without `--live`, the executor is a dry run.

```powershell
$env:PYTHONPATH = "agents"          # only without the editable install
py -m flowguard.service --level L1 --live
```

```bash
PYTHONPATH=agents python -m flowguard.service --level L1 --live
```

**4. Approval page.** The service prints its URL, http://127.0.0.1:8765/ (loopback only). A
proposal the gate sends to a human waits there for up to 10 minutes; no answer counts as a
denial.

**5. Grafana** (optional): the dashboard **FlowGuard / agents vs L0**, fed by two exporters.
The full commands, including how to find the address Prometheus scrapes, are in
[`docs/demo.md`](docs/demo.md#grafana-t2-optional).

```powershell
# $gw: the host.k3d.internal address from CoreDNS, as docs/demo.md shows
py -m flowguard.exporter --host $gw
py evaluation\results_exporter.py --host $gw
```

The two demo loops (a runtime fault, and a prod release approved by a human) are in
[`docs/demo.md`](docs/demo.md). That guide also lists which behaviours have not yet been
observed on the cluster.

**Checks.** `ruff check .`, `ruff format --check .` and `pytest`. The same checks run in
`checks.yml`. Unit tests are refused any call to kubectl, Docker, k3d or a non-loopback address
(`conftest.py`).

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
  | L5 | A sweep of the gate's confidence threshold (autonomy vs safety) |

- **Record and replay (D15):** each scenario is run live once on the cluster. Its raw inputs are
  recorded (Prometheus samples, per-request load logs, CI logs and events), and every level
  decides offline on the identical recording, so comparisons are exactly paired.
  - A live end-to-end set runs the full system as well. For instance, each live ci scenario
    runs once at L5 (D64).
  - Live and replayed results are reported separately.
- **Information firewall (D25):** ground truth lives only in an evaluation store that no agent
  code can open (`agents/tests/test_firewall.py`). An agent-input leakage test scans everything
  an agent receives for class names, experiment words and ground-truth values.
- **Pre-registration (D26b):** [`docs/preregistration.yaml`](docs/preregistration.yaml) fixes
  everything before any held-out result exists: families, counts, metrics, tests, exclusions and
  run order. It also holds the SHA-256 of a secret seed. Version 1.0 was frozen on 2026-11-06;
  any later change is a dated amendment listed in the report. Version 1.2 (2026-11-07) holds
  the amendments D66 (token-based request cap), D67 (the gate reads Diagnose's calibrated
  confidence) and the 6 LLM lanes, all made before any held-out result existed (D65).
- **Held-out set:** generated only at Step 11 by `scenarios/heldout.py` from the frozen
  pre-registration and the secret seed. Development uses only `scenarios/dev/`.
  - The CI failure corpus is split by template, not by run (D6).
  - External real logs (LogChunks) are used for evidence localization only (D32).
- **Staged freezes (D39):** `diagnose-freeze` tags the Diagnose code and hashes its artifacts, so
  its held-out and LogChunks runs can start early. `eval-freeze` freezes the rest of the agent
  code before the rest of Step 11; it is created by `scripts/eval-freeze.ps1` (or `.sh`), which
  refuses unless `dev` is clean and in step with origin, the Diagnose freeze holds and the test
  suite passes, and only prints the push command. `heldout.py` checks both.
- **Statistics:**
  - Every rate is reported with n and a 95% Wilson interval.
  - Paired levels are compared with an exact McNemar test, and time metrics with bootstrap
    intervals.
  - The confirmatory tests are H1 (detection) and H3 (evidence localization), with Holm
    correction across them. H2 and H4 are descriptive (D42, D56; pre-registration
    `multiplicity`).

## Current status

Steps 0 to 10 are built. **Every number below is a development result**, from the dev scenarios.
Several are in-sample (settings chosen on the same data). None is a held-out result; Step 11
produces those. Negative results are reported as they are.

- **Monitor (DSO1), development, in-sample:**
  - recall L1 17/18 against L0 10/18;
  - median time to detect 15 s against 74 s;
  - 0 false alerts in 2 unseen hours of normal traffic for both (exact Poisson bound 1.85 per
    hour, above the 1 per hour target).
  - Source: [blueprint, Step 6](docs/blueprint-v2.md#7-build-plan-v2) and
    [`evaluation/README.md`](evaluation/README.md).
- **Diagnose (DSO2), development:**
  - the regex baseline (L0) gets 61/74 (82%) on the generated failures
    ([pre-registration](docs/preregistration.yaml), `metrics.H2`);
  - LLM with retrieval (L3): 46/60 on training templates and 13/14 on validation (59/74), with
    1 ungrounded quote in 205 (run of 2026-11-05, code `ccd7ddc`; the calibration is recorded in
    the [blueprint artifact table](docs/blueprint-v2.md));
  - calibration coverage at a 90% target: L1 67/74, L2 67/74, L3 72/74, with mean prediction
    set sizes 2.54, 3.35 and 3.15 ([pre-registration](docs/preregistration.yaml),
    `calibration.dev_coverage`);
  - the fine-tuned encoder reaches macro-F1 0.72 and 10/14 on validation, below the 0.75 and
    80% targets, and misses the environment class ([`docs/d7-encoder.md`](docs/d7-encoder.md)).
- **Release (DSO3), development, 32 agents-mode recordings:**
  - L0, L3 and L4 each catch 15/20 bad releases before 100% traffic, with 0/12 false
    rollbacks. Release ties the rule baseline (McNemar 1 to 1).
  - At L4, P1 escalated 3 bad releases under node stress.
  - At the default gate setting (0.70), every staging Release decision on these recordings runs
    without a human, the 5 wrong promotions included. At 0.80 or more, every one needs a human.
    This is reported under H5; the threshold is unchanged.
  - Source: [blueprint, Step 8 dev results](docs/blueprint-v2.md#step-8-dev-results-dso3).
- **Security (DSO4), development replay:**
  - on 14 historical cases, both L0 and the transparent score block 0/12 negative controls
    (Wilson 0 to 24.25%) and block the secret;
  - the EPSS effect is not tested: every high-EPSS case also had a blocking critical finding.
  - Source: [`docs/step-9-security.md`](docs/step-9-security.md).
- **Optimizer (DSO5), development:**
  - projected saving 1.9268% on 8 validation commits, against the 20% target, which is not met;
  - 0/3 observed failures skipped;
  - no fail-fast benefit shown.
  - Source: [`docs/step-10-optimizer.md`](docs/step-10-optimizer.md).
- **Gate (DSO6), development:** on a set of 300 dev items (adversarial, conflict, and prompt
  injection with an LLM that obeys it), nothing risky executed without approval
  ([blueprint, Step 5](docs/blueprint-v2.md#7-build-plan-v2)).

## Safety rules

- **Agents never act.** An agent proposes one action from the catalog; only the executor writes,
  and only after the gate, and a human where required, allowed it.
- **Exception (D68): the Optimizer.** It is a CI-planning advisor. app-ci's plan step
  (`ci/plan.py`) applies its skip list directly, bounded by P2 and the mandatory jobs (build,
  unit tests and scans can never be planned away). It does not pass through the gate or the
  executor and writes no audit row. DSO5 measures it under these bounds.
- **Never allowed:** an action outside the catalog, an action the proposing agent may not
  propose (`AGENT_ACTIONS`, D36), and the prohibited tier (`delete_namespace`,
  `delete_secret`, `modify_rbac`, `kubectl_exec`). All three are rejected.
- **Gate rules, first match wins** (blueprint section 5):
  1. reject what the agent may not propose;
  2. reject what is not in the catalog;
  3. reject the prohibited tier;
  4. reject past the per-hour loop limit, counted from the audit log;
  5. ask a human for the high tier (`rollout_undo`, `allow_with_findings`);
  6. ask a human for the medium tier in prod;
  7. ask a human for the medium tier below calibrated confidence 0.70 (Diagnose's calibration per
     level, L4 and L5 using L3's; Release's; loaded fail-closed, D67);
  8. otherwise, execute.
- **Human approval:** on a loopback-only page with escaped text and a form token. No answer
  within 10 minutes is a denial. Batch evaluation uses a simulated approver; live runs and the
  demo use a team member (D17).
- **Refusals:**
  - the executor refuses a decision it cannot find unchanged in the audit log, and any proposal
    already executed (D37);
  - an L0 run has no gate, so it runs only with a dry-run executor;
  - prompt-injection text in logs or commits cannot get an action past the gate (D20).
- **The LLM** returns JSON from a fixed schema and never commands. Its settings are pinned and
  every call is logged (D27).
- **Credentials:** agents hold read-only credentials. The write token is used only by the
  executor and lives in `.env`, which is never committed (D63).
