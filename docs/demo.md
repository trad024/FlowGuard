# FlowGuard live demo

Two short loops on the laptop cluster, run by the real agent service (`flowguard.service`):

| Loop | What happens | Shows |
| --- | --- | --- |
| 1. Runtime fault | An error burst on half the staging stable pods → the **Monitor opens an incident** (fitted L1 detector, or the L0 alert rule) → Diagnose has no log for a runtime incident and hands it to a human (`escalate_to_human`, D60) → the gate lets this low-tier action run → the operator clears the fault and the alert resolves | Detection, the incident record and its evidence, the gate, the audit log |
| 2. Prod release | A new prod revision is held at 20% → **Release proposes `rollout_promote`** → the gate asks a human (medium tier in prod, rule 5, D23) → **approval page** → the **executor** resumes the Rollout → the frozen L0 analysis passes → 100% | Human approval, the executor's only write path, the audit trail |

## What the demo does not show, and why

- **No agent-driven recovery of a runtime fault.** Metrics ticks carry no CI log, so the
  service runs Diagnose's no-log path (D60): it records "no diagnosis (no log)", asks no model
  and proposes only `escalate_to_human`. The escalation is recorded in the audit log, not sent
  anywhere. `restart_deployment` is in the catalog (medium tier) but no agent proposes it for a
  runtime incident. In loop 1 the fault is cleared by the demo driver, standing for the human
  action. This matches the autonomy result: 0 of the 12 live L5 ci incidents were resolved
  without a human (exploratory, one gate setting, `docs/step-11-results.md`).
- **A bad canary.** The deployed Rollouts run the frozen L0 AnalysisTemplate at every step. It
  aborts a bad canary about 90 s in, before the Monitor's alert (1-minute rate, `for: 1m`)
  fires. Release deciding a canary needs the agents-mode Rollout (D53), which this demo does not
  deploy; the held-out release results come from recordings under it (D54).
- **The LLM levels.** The service runs at L1: Diagnose's LLM levels need a CI log, which a
  runtime incident does not have. Diagnose's held-out results are in `docs/step-11-results.md`.

Each loop is real: no agent logic is scripted. Release (Step 8) runs its sequential test on the
canary's and the stable pods' counts and promotes a canary with no evidence against it after a
step's four looks.

## Before you start

1. Docker Desktop is running and the cluster is up and healthy: `infra\scripts\setup.ps1` (or
   `k3d cluster start` on an existing cluster), then `infra\scripts\smoke.ps1` (9/9). After the
   laptop wakes from Modern Standby, Docker's port proxy can break: `wsl --shutdown`, then
   start Docker Desktop again.
2. Both Rollouts are Healthy and staging is in L0 mode:
   ```powershell
   kubectl get rollout podinfo -n staging -o jsonpath='{.status.phase} {.spec.strategy.canary.steps[*]}'
   kubectl get rollout podinfo -n prod -o jsonpath='{.status.phase} {.spec.strategy.canary.steps[*]}'
   ```
   Both `Healthy`, with an `analysis` step (L0 mode). If staging was left in agents mode by a
   release recording (a `pause` with no duration, no `analysis`), switch only its steps back,
   as the runner does (applying the overlay in full would also reset the image):
   ```powershell
   py -c "import sys; sys.path[:0]=['scenarios','ci','agents']; import runner; runner.set_canary_mode('staging','l0')"
   ```
3. Port-forwards from setup: Prometheus http://localhost:9090, Grafana http://localhost:3000,
   podinfo staging and prod at :9898 and :9899.
4. The audit log starts empty: the service writes `state\audit.db` (or `FLOWGUARD_AUDIT_DB`).
   Keep the Step 11 log elsewhere (on laptop A it is `state\audit-step11.db`, backed up with
   the ground-truth DB in `C:\fg-results\A-backup`). The demo never writes to
   `evaluation\runs\groundtruth.db`; its load logs go to
   `evaluation\runs\deploy-<namespace>-demo-<id>.jsonl`.
5. In each terminal, from the repo root:
   ```powershell
   $env:PYTHONPATH = "$PWD\agents"
   ```

Four terminals: **T1** for the agent service, **T2a** and **T2b** for the exporters (Grafana
only), **T3** for the demo driver.

## Start the agent service (T1)

```powershell
py -m flowguard.service --level L1 --live
```

It prints the approval page URL (http://127.0.0.1:8765/), the audit log path and whether a fitted
Monitor model is present (`"monitor_model": "fitted"` with `agents\flowguard\models\monitor.json`).
The service reads only Prometheus, as every agent does (D1). Rollout state comes from three
series: pods up per pod-template hash, ReplicaSet creation times from kube-state-metrics, and
Argo Rollouts' `rollout_info` phase. Only the executor writes to the cluster. It polls every
30 s and prints one line for each incident it opens. Each namespace (staging, prod) is its own
Monitor tick, so an incident is followed up in the namespace it came from. While a proposal
waits for a human, the loop waits too, for at most 10 minutes; no answer counts as a denial.

Without `--live` the executor is a dry run: everything happens except the write to the cluster.

### Following up an aborted Rollout (optional, D44, not used in the two loops)

An abort is in the Rollout's status, which Prometheus does not carry. To let the service see
it, give it a short-lived token of the read-only `flowguard-reader` account (D59) and the
cluster's CA. Both go to `state/`, which is gitignored; the token expires after `--duration`.

```powershell
kubectl -n monitoring create token flowguard-reader --duration 8h | Set-Content -NoNewline state\kube-token
$ca = kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}'
[IO.File]::WriteAllBytes("$PWD\state\kube-ca.crt", [Convert]::FromBase64String($ca))
py -m flowguard.service --level L1 --live --kube-token-file state\kube-token --kube-ca-file state\kube-ca.crt
```

```bash
kubectl -n monitoring create token flowguard-reader --duration 8h > state/kube-token
kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' \
  | base64 -d > state/kube-ca.crt
python -m flowguard.service --level L1 --live --kube-token-file state/kube-token --kube-ca-file state/kube-ca.crt
```

The start line then says `"abort_follow_up": "on"`. When a watched Rollout is aborted, Release
proposes `rollout_undo` once for that revision; it is high tier, so the approval page always
asks.

## Grafana (T2a, T2b)

Prometheus runs inside k3d and reaches the laptop as `host.k3d.internal`. The exporters listen
on that one address, never on every interface:

```powershell
$gw = ((kubectl -n kube-system get configmap coredns -o jsonpath='{.data.NodeHosts}') -split "`n" |
  Select-String 'host.k3d.internal').ToString().Trim().Split()[0]
py -m flowguard.exporter --host $gw              # T2a: the audit log
py evaluation\results_exporter.py --host $gw     # T2b: scored results (optional)
```

```bash
gw=$(kubectl -n kube-system get configmap coredns -o jsonpath='{.data.NodeHosts}' \
  | awk '/host.k3d.internal/ {print $1}')
python -m flowguard.exporter --host "$gw"              # T2a
python evaluation/results_exporter.py --host "$gw"     # T2b (optional)
```

If that address is not one of this machine's own, the exporter cannot bind it and says so. Check
the panel **Data sources up** before recording.

Open **FlowGuard / agents vs L0**. Show the **Live** panels (L0 alerts firing, incidents
opened by the Monitor, gate outcomes, human answers, the two must-be-0 counters). The scored
panels show the development (in-sample) results, not the held-out ones: quote held-out numbers
from `docs/step-11-results.md`, not from this dashboard.

## Loop 1: runtime fault (T3)

```powershell
py scenarios\demo.py runtime --hold 180
```

The driver waits until no L0 alert has fired for 2 minutes (D50). It then starts staging load,
waits 60 s, turns on a 50% error rate on half the stable pods (25% of the service, five times
the 5% alert threshold), holds it (`--hold`, default 300 s), clears it and keeps the load
running for 2 minutes. Expected (from the code and the dev measurements, not yet observed with
this service):

- T1, within one or two polls of `fault_on`:
  `{"incident": "i-…", "agent": "diagnose", "proposals": ["escalate_to_human"], "gate": ["execute"], "executed": [true]}`
- The record:
  ```powershell
  py scenarios\demo.py status --last 1
  ```
  `detector: runtime` with evidence `monitor: anomaly on staging/…` and the series behind it
  (the fitted detector, dev median MTTD 15 s), or `detector: alert_rule` with
  `monitor: L0 alert HighErrorRatio firing (staging)` if the rule comes first; then
  `diagnose: no diagnosis (no log)`; the proposal with gate `execute`.
- Grafana: HighErrorRatio fires about 1 to 2 minutes after `fault_on` and resolves within about
  2 minutes of `fault_off`.

## Loop 2: prod release with a human approval (T3)

Start it after loop 1's incident line: the service's loop waits while a proposal waits for a
human.

```powershell
py scenarios\demo.py prod-release
```

The driver releases a new prod revision (the same image with an empty release Secret: a good
release), holds the Rollout (`spec.paused`) once the canary serves 20%, and keeps prod load
running. Expected:

1. T3: `{"step": "held", "canary": "…", "weight": 20, …}`
2. After the step's 30 s warm-up, Release gets one look per poll. It holds for three looks
   (nothing printed: a hold opens no incident) and at the fourth, about 2 minutes after the
   hold, proposes `rollout_promote`; the gate answers `ask_human` (medium tier in prod, rule 5).
   T1 prints nothing while the proposal waits: its line comes after the answer.
3. Open http://127.0.0.1:8765/, type your name (recorded as the approver, D58) and approve. The
   card disappears once the answer is recorded; if it is still there after a reload, the answer
   did not arrive.
4. T1: `"gate": ["ask_human/approved"], "executed": [true]`. The executor unpauses the Rollout
   and clears its pause conditions.
5. The canary continues under the frozen L0 analysis to 50% and 100% (about 4 minutes). T3
   prints `{"step": "promoted", …}`.

The Monitor may also open a prod incident while the canary runs (in the dry run, a memory
anomaly on one stable pod). Mid-canary it goes to Release, which decides from its own counts;
with clean counts it holds and proposes nothing.

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

How to read an incident's `status`: it is set from the gate and the executor, not from the
service's recovery. An incident whose only action, `escalate_to_human`, was executed shows
`resolved` (the escalation was recorded, not the fault fixed); one that went to a human shows
`escalated`, also after an approved, executed action; one where Release held shows `open`.

## Bash

```bash
export PYTHONPATH="$PWD/agents"
python -m flowguard.service --level L1 --live
python scenarios/demo.py runtime --hold 180
python scenarios/demo.py prod-release
python scenarios/demo.py status --last 3
```

## Verification status

Covered by unit tests with fakes (no cluster): the service, the driver, the exporters and the
dashboard JSON.

Dry run on laptop A, 2026-11-10, service at L1 with `--live`, fresh audit log (times UTC):

| Loop | Step | Time | After |
| --- | --- | --- | --- |
| 1 | `fault_on`, 5 of 10 stable pods | 17:48:40 | |
| 1 | Incident opened, `detector: runtime`, `escalate_to_human` executed | 17:49:19 | +39 s |
| 1 | HighErrorRatio pending / firing | 17:49:20 / 17:50:20 | +40 s / +100 s |
| 1 | `fault_off`; alert resolved | 17:51:42; about 17:52:35 | +53 s after `fault_off` |
| 2 | `released`; `held` at 20% (Argo phase `Paused`, `rollout_info` phase `Paused`) | 17:55:39; 17:55:42 | |
| 2 | Monitor incident on prod (memory, one stable pod); Release held | 17:56:19 | +37 s |
| 2 | Release proposed `rollout_promote`, gate `ask_human` | 17:57:49 | +2 min 6 s after `held` |
| 2 | Approved on the page; executor resumed the Rollout | 18:02:14 | (a first click did not arrive) |
| 2 | L0 analyses at 20% and 50% successful; `promoted` | 18:06:00 | +3 min 46 s after approval |

Audit: `executed_without_required_approval` 0, `executed_after_reject` 0.

Observed: the timings above; Argo Rollouts holding a canary on `spec.paused` during its pause
step and the executor's resume of it; the `rollout_info` phase label (`Paused` while held,
`Completed` when Healthy). **Not working:** the Grafana path. After a cluster restart CoreDNS
had no `host.k3d.internal` entry, so Prometheus could not resolve the exporters' targets (both
`down`); the dashboard was not shown in the dry run.
