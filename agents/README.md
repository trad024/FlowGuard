# agents

The agent service (Step 5): one Python process with the orchestrator, five agents, the shared
incident record, the risk gate, the executor and the audit log. Nothing here reads the
evaluation side (`evaluation/`, `scenarios/`, the ground-truth DB): `tests/test_firewall.py`
fails if it does (D25).

| Module | Role |
| --- | --- |
| `contracts.py` | The objects every component exchanges (events, incidents, proposals, gate decisions, evidence posts, agent requests) |
| `catalog.py` | The static action catalog: tiers, reversibility, hourly limits, which agent may propose what |
| `orchestrator.py` | One LangGraph graph for workflows A to D; the ablation level (L0 to L5) is a configuration flag |
| `stubs.py` | Stub agents with the real interfaces (Steps 6 to 10 replace the logic) |
| `incidents.py` | Shared incident record (D5): incidents, evidence posts, agent requests |
| `gate.py` | Risk gate: rules 0 to 7, first match wins (D36 and v1), P3 conflicts, v1 risk score |
| `calibration.py` | Split conformal confidence for the gate (D9) |
| `approval.py` | Simulated approver (D17) and the local approval page |
| `executor.py` | The only writer: rollout actions, restarts, scaling, CI reruns |
| `baselines.py` | Code baselines: the DSO2 regex classifier and playbook (D3) |
| `llm.py` | The pinned LLM client (D27, D28) |
| `audit.py`, `audit_metrics.py` | The audit log and its metrics (DSO6) |

## Levels

| Level | Adds | Gate |
| --- | --- | --- |
| L0 | Rule baselines | none (DSO6's baseline); runs only with a dry-run executor |
| L1 | Statistical and ML detectors | rules 0 to 7 |
| L2 | LLM diagnosis | rules 0 to 7 |
| L3 | Retrieval and evidence grounding (D8) | rules 0 to 7 |
| L4 | Collaboration protocols P1 to P3 | rules 0 to 7 |
| L5 | L4 with the confidence threshold swept | rules 0 to 7, threshold varied |

## Safety

- Agents only propose catalog actions; the gate decides; only the executor writes, and it
  refuses anything the gate or a human did not allow, even when called directly: it re-reads
  the decision and the approval from the audit log and never trusts the object it is handed
  (D37).
- The loop limit counts executions in the audit log, so it holds across restarts (D37).
- The gate never reads the confidence an agent reports; it uses calibrated confidence (D9),
  which is 0 until calibration data exists, so every medium action goes to a human.
- The approval page listens on 127.0.0.1 only, escapes everything it shows, needs its form
  token, and treats 10 minutes without an answer as a denial.

```python
from flowguard.approval import ApprovalPage

page = ApprovalPage()  # open page.url in a browser; pass it as the orchestrator's approver
```
