# FlowGuard

A multi-agent system that watches a DevOps pipeline, diagnoses and acts on failures under a
human-controlled risk gate, and is measured against a rule-based pipeline on the same injected
scenarios.

The full design (objectives, DSOs, architecture, agents, evaluation, build plan) is in the
project blueprint; a summary lives in [`docs/`](docs/README.md).

## Repository layout

| Folder | Contents | Built in step |
| --- | --- | --- |
| `agents/` | Python service: contracts, orchestrator, agents, risk gate, executor | 0, 5–10 |
| `infra/` | k3d cluster, manifests, Prometheus, Grafana, Argo Rollouts | 1–2 |
| `ci/` | Scripts and reusable pieces used by the CI pipeline | 3 |
| `scenarios/` | Fault injection scripts and the scenario catalogue with ground truth | 4 |
| `evaluation/` | Metric queries, comparison reports, dashboards | 4, 11 |
| `docs/` | Blueprint summary, decisions, runbooks | all |

GitHub only runs workflows from `.github/workflows/`, so workflow files live there and call
scripts in `ci/`.

## Getting started

Requires Python 3.11.

```bash
git clone https://github.com/trad024/FlowGuard.git
cd FlowGuard
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Checks

```bash
ruff check .                       # lint
ruff format --check .              # formatting
pytest                             # tests
```

The same checks run on every push and pull request (`.github/workflows/checks.yml`).

## Status

- [x] Step 0 — repository, conventions, shared data contracts and action catalog
- [x] Step 1 — cluster platform (manifests and scripts; see `infra/README.md`)
- [ ] Step 2 — canary delivery
- [ ] Step 3 — CI pipeline and deployer
- [ ] Step 4 — baseline mode, scenario runner, audit log
- [ ] Step 5 — agent core (orchestrator, risk gate, executor, approval)
- [ ] Steps 6–10 — Monitor, Diagnose, Release, Security, Optimizer agents
- [ ] Step 11 — full evaluation and report
