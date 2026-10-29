"""Live agent service: the one process on the laptop that runs the agents (blueprint section 5).
It polls; nothing pushes to it (D1).

    python -m flowguard.service --level L1 [--live] [--watch staging,prod]
        [--interval 30] [--prometheus http://localhost:9090] [--approval-port 8765]
        [--kube-token-file FILE --kube-ca-file FILE [--kube-api https://127.0.0.1:6550]]

Every interval:
  1. Monitor poll (workflow C): one metrics_tick per watched namespace, with that namespace's
     series, its L0 alerts and the node rows, tagged with the namespace as its environment (an
     incident is followed up where it happened) and marked in_canary while its Rollout is
     mid-canary.
  2. Canary looks (workflow D): while a watched Rollout is paused mid-canary, one rollout_step
     per poll once the step's 30 s warm-up has passed (the agents-mode Rollout waits there for
     Release, D53): `release.step_event`, the per-hash count windows since the canary's
     ReplicaSet was created (D21, D55), with the look's number within the step (20%, then 50%).
     Release holds (no proposal, no incident) until it proposes; after a proposal for a step,
     that step gets no more looks, so a denied promotion is not asked again every poll.
  3. Aborted Rollout: when a watched Rollout's status shows an abort (status.abort, or phase
     Degraded with an abort message), one rollout_step marked aborted for that revision, so
     Release proposes rollout_undo (high tier: always a human); asked once per aborted
     revision, again only after the Rollout recovered and aborted anew.
The orchestrator does the rest: incidents, agents, the gate (Release's proposals with the
calibrator fitted on the dev release decisions, D9), the approval page for ask_human (the loop
waits for the answer, at most its 10-minute timeout) and the executor, which is a dry run unless
--live. Metrics come from Prometheus (D1): pods up per pod-template hash, the ReplicaSets'
creation times (kube-state-metrics) and the Rollout's phase (Argo Rollouts' own rollout_info).
The abort is in the Rollout's status, which Prometheus does not carry: with --kube-token-file,
the Rollout is read from the Kubernetes API with the read-only flowguard-reader token (get,
list and watch on Rollouts, infra/base/flowguard-reader.yaml); without it, step 3 is off. The
executor is the only component that changes the cluster. The audit log and
incident record are state/audit.db (FLOWGUARD_AUDIT_DB overrides). A poll reads the last 10
minutes (series.HISTORY_S): counts older than that no longer reach Release's windows.
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from flowguard import release, series
from flowguard.approval import ApprovalPage
from flowguard.audit import AuditLog
from flowguard.contracts import AgentName
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import tick_event
from flowguard.orchestrator import Orchestrator, level
from flowguard.stubs import Diagnose

ROLLOUT = "podinfo"
WARMUP_S = 30.0  # each step's pause before L0's analysis, and before Release's first look
WEIGHTS = (20, 50)  # the canary steps (infra/apps/podinfo/base/podinfo.yaml)
# Rollout state, read by the service only (the Monitor's own series are series.SELECTORS):
# pods up per pod-template hash, each ReplicaSet's creation time, Argo Rollouts' phase
ROLLOUT_SELECTORS = {
    "podinfo_up": 'up{app="podinfo"}',
    "replicasets": 'kube_replicaset_created{namespace=~"staging|prod",replicaset=~"podinfo-.*"}',
    "rollout_info": f'rollout_info{{name="{ROLLOUT}"}}',
}
ROLLOUT_API = "/apis/argoproj.io/v1alpha1/namespaces/{namespace}/rollouts/" + ROLLOUT


def abort_of(rollout: dict | None) -> tuple[str, str | None] | None:
    """(aborted revision's pod-template hash, stable hash) when the Rollout is aborted:
    status.abort, or phase Degraded with an abort message. A Rollout Degraded for another
    reason (a progress deadline, a failed pod) is not an abort and not Release's to undo."""
    status = (rollout or {}).get("status") or {}
    message = str(status.get("message") or "").lower()
    aborted = status.get("abort") is True or (
        status.get("phase") == "Degraded" and "abort" in message
    )
    canary = status.get("currentPodHash")
    return (canary, status.get("stableRS")) if aborted and canary else None


def _get(request: urllib.request.Request, ca_file: str | None) -> bytes:
    context = ssl.create_default_context(cafile=ca_file) if ca_file else None
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        return response.read()


@dataclass
class RolloutReader:
    """GET the Rollout from the Kubernetes API with the read-only flowguard-reader token."""

    api: str
    token: str
    ca_file: str | None = None
    get: Callable[[urllib.request.Request, str | None], bytes] = _get

    def __call__(self, namespace: str) -> dict:
        request = urllib.request.Request(
            self.api.rstrip("/") + ROLLOUT_API.format(namespace=namespace),
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
        )  # a GET: the token can do nothing else
        return json.loads(self.get(request, self.ca_file))


@dataclass(frozen=True)
class RolloutState:
    paused: bool
    canary: str | None  # the newer revision's pod-template hash, while two serve
    stable: str | None
    created: float | None = None  # the canary's ReplicaSet creation time, when known
    weight: int | None = None  # the canary step its pods up are at (20 or 50)

    @property
    def mid_canary(self) -> bool:
        return bool(self.canary and self.stable and self.canary != self.stable)


@dataclass
class Look:
    """Release's looks at one canary step."""

    weight: int
    since: float  # when this step was first seen
    looks: int = 0
    decided: bool = False


def rollout_state(idx: series.Index, t: float, namespace: str) -> RolloutState:
    """From Prometheus alone: two hashes with pods up means a canary, the newer ReplicaSet
    being the canary's; paused when Argo Rollouts reports the phase Paused."""
    up: dict[str, int] = {}
    for s in idx.get("podinfo_up", []):
        h = s.labels.get("rollouts_pod_template_hash")
        if s.labels.get("namespace") == namespace and h and s.latest(t) == 1:
            up[h] = up.get(h, 0) + 1
    created = {}
    for s in idx.get("replicasets", []):
        name = s.labels.get("replicaset", "")
        mine = s.labels.get("namespace") == namespace and name.startswith(f"{ROLLOUT}-")
        if mine and (v := s.latest(t, series.HISTORY_S)) is not None:
            created[name.removeprefix(f"{ROLLOUT}-")] = v
    phases = [
        (s.times[-1], s.labels.get("phase"))
        for s in idx.get("rollout_info", [])
        if s.labels.get("namespace") == namespace and s.latest(t, 60) == 1
    ]
    paused = bool(phases) and max(phases)[1] == "Paused"
    if len(up) < 2:
        only = next(iter(up), None)
        return RolloutState(paused, only, only)
    # newest ReplicaSet first; without creation times, the hash with fewer pods
    order = sorted(up, key=lambda h: (created.get(h, float("-inf")), -up[h]))
    canary, stable = order[-1], order[0]
    share = 100 * up[canary] / (up[canary] + up[stable])
    weight = min(WEIGHTS, key=lambda w: abs(w - share))
    return RolloutState(paused, canary, stable, created.get(canary), weight)


def ratio(counts: tuple[float, float] | None) -> float:
    return counts[1] / counts[0] if counts and counts[0] > 0 else 0.0


def canary_split(idx: series.Index, t: float, namespace: str, state: RolloutState) -> dict:
    """The canary's and the stable pods' 5xx ratios over the last minute (Release's input)."""
    counts = series.by_hash(idx, t, namespace)
    return {
        "canary_5xx_ratio": ratio(counts.get(state.canary)),
        "stable_5xx_ratio": ratio(counts.get(state.stable)),
    }


def scope(snapshot: dict[str, float], namespace: str) -> dict[str, float]:
    """One namespace's rows of a snapshot (itself and its pods), plus the node rows."""
    out = {}
    for key, value in snapshot.items():
        entity = (series.split_key(key) or ("",))[0].split("/")[0]
        if entity in (namespace, "node"):
            out[key] = value
    return out


@dataclass
class Service:
    orch: Orchestrator
    source: series.PrometheusSource
    watch: tuple[str, ...] = ("staging", "prod")
    steps: dict[tuple[str, str], Look] = field(default_factory=dict)  # (namespace, canary)
    started: dict[tuple[str, str], float] = field(default_factory=dict)  # first seen
    rollouts: Callable[[str], dict] | None = None  # namespace -> the Rollout (RolloutReader)
    undo_asked: set[tuple[str, str]] = field(default_factory=set)  # (namespace, revision)

    def tick(self, t: float | None = None) -> list[dict]:
        """One poll: a Monitor tick per watched namespace, a look at each held canary, and the
        follow-up of an aborted Rollout."""
        t = time.time() if t is None else t
        idx = self.source.fetch(t)
        states = {ns: rollout_state(idx, t, ns) for ns in self.watch}
        snapshot = series.snapshot(idx, t)
        results = []
        for ns, state in states.items():
            mine = scope(snapshot, ns)
            if state.mid_canary:  # Release, if the Monitor hands it an incident, compares these
                mine.update(canary_split(idx, t, ns, state))
            tick = tick_event(
                mine,
                series.firing(idx, t, namespace=ns),
                t,
                environment=ns,
                in_canary=state.mid_canary,
            )
            results.append(self.orch.handle(tick))
        for ns, state in states.items():
            out = self.look(idx, t, ns, state)
            if out is not None:
                results.append(out)
        for ns in self.watch:
            out = self.aborted(idx, t, ns)
            if out is not None:
                results.append(out)
        return results

    def aborted(self, idx: series.Index, t: float, ns: str) -> dict | None:
        """An aborted Rollout: one look marked aborted for that revision, so Release proposes
        rollout_undo through the gate; not again for the same revision until it recovers."""
        if self.rollouts is None:
            return None
        try:
            found = abort_of(self.rollouts(ns))
        except (OSError, ValueError) as exc:  # unreachable API, bad token, not JSON
            print(json.dumps({"rollout_status": ns, "error": str(exc)[:200]}), flush=True)
            return None
        if found is None:
            self.undo_asked = {k for k in self.undo_asked if k[0] != ns}
            return None
        revision, stable = found
        if (ns, revision) in self.undo_asked:
            return None
        self.undo_asked.add((ns, revision))
        since = t - release.WINDOW_S  # the counts do not matter to an undo
        event = release.step_event(idx, t, ns, revision, stable or "", since, 0, aborted=True)
        return self.orch.handle(event)

    def look(self, idx: series.Index, t: float, ns: str, state: RolloutState) -> dict | None:
        """One look at a held canary after its step's warm-up; none once Release proposed."""
        if not state.mid_canary:
            for key in [k for k in self.steps if k[0] == ns]:
                del self.steps[key], self.started[key]
            return None
        key = (ns, state.canary)
        self.started.setdefault(key, t)
        step = self.steps.get(key)
        if step is None or step.weight != state.weight:
            step = self.steps[key] = Look(state.weight, t)
        if not state.paused or step.decided or t - step.since < WARMUP_S:
            return None
        step.looks += 1
        since = state.created if state.created is not None else self.started[key]
        event = release.step_event(
            idx, t, ns, state.canary, state.stable, since, step.looks, weight=state.weight
        )
        out = self.orch.handle(event)
        step.decided = bool(out.get("output") and out["output"].proposals)
        return out

    def run(self, interval: float, sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            started = time.monotonic()
            for out in self.tick():
                report(out)
            sleep(max(0.0, interval - (time.monotonic() - started)))


def report(out: dict) -> None:
    """One line per handled event that did something, for the operator's terminal."""
    if not out.get("incident_id"):
        return
    line = {"incident": out["incident_id"], "agent": str(out.get("agent"))}
    if out.get("output") is not None:
        line["proposals"] = [p.action for p in out["output"].proposals]
    line["gate"] = [
        d.outcome + (f"/{d.human_verdict}" if d.human_verdict else "")
        for d in out.get("decisions", [])
    ]
    line["executed"] = [r.success for r in out.get("results", [])]
    print(json.dumps(line), flush=True)


def make_orchestrator(name: str, audit: AuditLog, approver, live: bool = False) -> Orchestrator:
    """The service's orchestrator: Release's proposals are gated with its calibrator (D9)."""
    return Orchestrator(
        cfg=level(name),
        record=IncidentRecord(audit.path),
        executor=Executor(audit=audit, dry_run=not live),
        audit=audit,
        approver=approver,
        calibrators={AgentName.RELEASE: release.calibrator()},
        # metrics ticks and canary looks carry no CI log: D60 answers every Diagnose call
        diagnose=Diagnose.without_logs(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", default="L1", choices=("L0", "L1", "L2", "L3", "L4", "L5"))
    parser.add_argument("--live", action="store_true", help="the executor acts (else dry run)")
    parser.add_argument("--watch", default="staging,prod")
    parser.add_argument("--interval", type=float, default=30.0)  # NFR: detect within 2 polls
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--approval-port", type=int, default=8765)
    parser.add_argument("--kube-api", default="https://127.0.0.1:6550")
    parser.add_argument("--kube-token-file", type=Path, help="flowguard-reader token (read-only)")
    parser.add_argument("--kube-ca-file", help="the cluster's CA certificate")
    args = parser.parse_args(argv)
    rollouts = None
    if args.kube_token_file:
        token = args.kube_token_file.read_text(encoding="utf-8").strip()
        rollouts = RolloutReader(args.kube_api, token, args.kube_ca_file)
    audit = AuditLog.open(level=args.level)
    page = ApprovalPage(port=args.approval_port)
    orch = make_orchestrator(args.level, audit, page, live=args.live)
    service = Service(
        orch,
        series.PrometheusSource(
            args.prometheus, selectors={**series.SELECTORS, **ROLLOUT_SELECTORS}
        ),
        watch=tuple(n for n in args.watch.split(",") if n),
        rollouts=rollouts,
    )
    print(
        json.dumps(
            {
                "level": args.level,
                "executor": "live" if args.live else "dry run",
                "approval_page": page.url,
                "audit_log": str(audit.path),
                "monitor_model": "fitted" if orch.agents["monitor"].model else "none (L0 rules)",
                "abort_follow_up": "on" if rollouts else "off (no --kube-token-file)",
            }
        ),
        flush=True,
    )
    try:
        service.run(args.interval)
    except KeyboardInterrupt:
        page.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
