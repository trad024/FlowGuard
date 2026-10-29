# FlowGuard live demo

Two short loops on the laptop cluster, run by the real agent service (`flowguard.service`):

| Loop | What happens | Shows |
| --- | --- | --- |
| 1. Runtime fault | An error burst on half the staging pods → the L0 alert fires → the **Monitor opens an incident** → Diagnose proposes → the gate lets a low-tier action run → the fault is cleared and the alert resolves | Detection, the incident record and its evidence, the gate, the audit log |
| 2. Prod release | A new prod revision is held at 20% → **Release proposes `rollout_promote`** → the gate asks a human (medium tier in prod, D23) → **approval page** → the **executor** resumes the Rollout → the L0 analysis passes → 100% | Human approval, the executor's only write path, the audit trail |

## What the demo does not show yet, and why

The full chain "fault → incident → a fix that needs a human → executor → recovery" does not exist
in the code yet:

- **Runtime incidents:** Diagnose is the Step 5 stub until Step 7. With no CI log, it classifies a
  runtime incident as unclear and proposes `escalate_to_human`, a low-tier action that runs
  without asking anyone. A proposed restart, which would clear the fault and need approval,
  comes with Step 7.
- **A bad canary:** every Rollout step runs the frozen L0 AnalysisTemplate. It aborts a bad
  canary about 90 s in, before the Monitor's alert (1-minute rate, `for: 1m`) fires. Release
  deciding a canary in agents mode is a Step 8 design point.

Each loop below is real: no agent logic is scripted. Release is still the Step 5 stub: it
promotes a canary that is no worse than stable.

## Before you start

1. The cluster is up and healthy: `infra\scripts\setup.ps1`, then `infra\scripts\smoke.ps1`.
2. Port-forwards from setup are running: Prometheus at http://localhost:9090, Grafana at
   http://localhost:3000, podinfo staging and prod at :9898 and :9899.
3. In each terminal, from the repo root, with the project's virtual environment:
   ```powershell
   .venv\Scripts\Activate.ps1
   ```

Three terminals: **T1** for the agent service, **T2** for the exporters (Grafana only), **T3**
for the demo driver.

## Start the agent service (T1)

```powershell
py -m flowguard.service --level L1 --live
```

It prints the approval page URL (http://127.0.0.1:8765/), the audit log path and whether a fitted
Monitor model is present. Without one, the Monitor reads only the frozen alert rules. The
service reads only Prometheus, as every agent does (D1). Rollout state comes from three
series: pods up per pod-template hash, ReplicaSet creation times from kube-state-metrics, and
Argo Rollouts' `rollout_info` phase from the controller, scraped by the `argo-rollouts` job.
Only the executor writes to the cluster. It then
polls every 30 s and prints one line for each incident it opens. While a proposal waits for a
human, the loop waits too, for at most 10 minutes; no answer counts as a denial.

Without `--live` the executor is a dry run: everything happens except the write to the cluster.

## Grafana (T2, optional)

```powershell
py -m flowguard.exporter --host 0.0.0.0
```
```powershell
py evaluation\results_exporter.py --host 0.0.0.0
```

Open **FlowGuard / agents vs L0** in Grafana. The live panels (L0 alerts firing, Monitor
incidents, gate outcomes, approvals) move during the loops.

## Loop 1: runtime fault (T3)

```powershell
py scenarios\demo.py runtime
```

The driver waits until no L0 alert has fired for 2 minutes (D50). It then starts staging load,
waits 60 s, turns on a 50% error rate on half the stable pods (25% of the service, five times
the 5% alert threshold), holds it for 5 minutes, clears it and keeps the load running for 2
minutes. Expected, within about 2 to 3 minutes of `fault_on`:

- T1: `{"incident": "i-…", "agent": "diagnose", "proposals": ["escalate_to_human"], "gate": ["execute"], "executed": [true]}`
- the record, read with:
  ```powershell
  py scenarios\demo.py status --last 1
  ```
  `detector: alert_rule`; evidence `monitor: L0 alert HighErrorRatio firing (staging)` and
  Diagnose's claim; the proposal with gate `execute`. The escalation is recorded, not sent
  anywhere.
- After `fault_off`, the alert resolves within about 2 minutes.

With a fitted Monitor model (`agents\flowguard\models\monitor.json`), the runtime detector can
open the incident first, with evidence `anomaly on staging/podinfo-…` and the series behind it.

## Loop 2: prod release with a human approval (T3)

```powershell
py scenarios\demo.py prod-release
```

The driver releases a new prod revision: the same image with an empty release Secret, as the
runner deploys a good release. It holds the Rollout (`spec.paused`) once the canary serves 20%,
and keeps prod load running. Expected:

1. T3: `{"step": "held", "canary": "…", "weight": 20, …}`
2. Within 30 s, the service sees the held canary and sends Release a `rollout_step` with the
   canary's and the stable pods' 5xx ratios. Release proposes `rollout_promote`, and the gate
   answers `ask_human`: medium tier in prod, rule 5.
3. Open http://127.0.0.1:8765/ and approve the proposal.
4. T1: `"gate": ["ask_human/approved"], "executed": [true]`. The executor unpauses the Rollout
   and clears its pause conditions.
5. The canary continues under the frozen L0 analysis to 50% and 100%. T3 prints
   `{"step": "promoted", …}`.

Deny instead, and nothing is executed: the Rollout stays held. Release it by hand:

```powershell
kubectl -n prod patch rollout podinfo --type merge -p '{\"spec\":{\"paused\":false}}'
```

or abort it:

```powershell
kubectl -n prod patch rollout podinfo --subresource=status --type merge -p '{\"status\":{\"abort\":true}}'
```

## Check the audit trail

```powershell
py scenarios\demo.py status --last 3
py -m flowguard.audit_metrics
```

`executed_without_required_approval` and `executed_after_reject` must both be 0.

## Bash

The same commands, with `python` and forward slashes:

```bash
source .venv/bin/activate
python -m flowguard.service --level L1 --live
python scenarios/demo.py runtime
python scenarios/demo.py prod-release
python scenarios/demo.py status --last 3
```

## Verification status

The service, the driver, the exporters and the dashboard are covered by unit tests with fakes
(no cluster). **These loops have not yet been run on the cluster.** Five behaviours are expected
from the code but not observed:
- the timings;
- Argo Rollouts holding a canary on `spec.paused` during its pause step;
- the executor's resume of a held Rollout;
- Prometheus reaching the exporters at `host.k3d.internal`;
- the Argo Rollouts metrics Service (`argo-rollouts-metrics:8090`) and its `rollout_info`
  phase label;
- the dashboard rendering.
