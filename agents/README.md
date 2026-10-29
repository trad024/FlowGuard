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
| `agent_io.py` | What every agent receives and returns (`AgentInput`, `AgentOutput`) |
| `monitor.py` | Monitor (Step 6, DSO1): L0 alert rules; L1+ rolling z-score then Isolation Forest (runtime), duration and failure-streak detector (CI); P1 stress answers; proposes no action |
| `series.py` | Prometheus series to the Monitor's per-namespace and per-pod features, live and in replay; evidence references |
| `release.py` | Release (Step 8, DSO3): per-hash count windows from Prometheus, an always-valid sequential test (5xx and requests over 500 ms) with an absolute floor; continue, hold, roll back or escalate; P1 before any rollback at L4+; the rollout_step event, live and in replay |
| `retrieval.py` | L3 retrieval (Step 7): Chroma index of training cases under `state/chroma/`, all-MiniLM-L6-v2 (ONNX, CPU) under `state/models/`; the 3 nearest cases of other templates |
| `stubs.py` | Stub agents with the real interfaces (Steps 7, 9 and 10 replace the logic); re-exports the real Monitor and Release |
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
