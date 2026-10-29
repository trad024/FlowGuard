"""Live agent service: the one process on the laptop that runs the agents (blueprint section 5).
It polls; nothing pushes to it (D1).

    python -m flowguard.service --level L1 [--live] [--env staging] [--watch staging,prod]
        [--interval 30] [--prometheus http://localhost:9090] [--approval-port 8765]

Every interval:
  1. Monitor poll (workflow C): the Prometheus series of both namespaces as one metrics_tick
     for `--env`, marked in_canary while that environment's Rollout is mid-canary.
  2. Canary hold (workflow D): each watched Rollout that is paused mid-canary gives one
     rollout_step for its canary hash, with the canary's and the stable pods' 5xx ratios over
     the last minute (per-pod counters summed by pod-template hash, D21).
The orchestrator does the rest: incidents, agents, the gate, the approval page for ask_human
(the loop waits for the answer, at most its 10-minute timeout) and the executor, which is a dry
run unless --live. Everything is read from Prometheus (D1): pods up per pod-template hash, the
ReplicaSets' creation times (kube-state-metrics) and the Rollout's phase (Argo Rollouts' own
rollout_info); the executor is the only component that touches the cluster. The audit log and
incident record are state/audit.db (FLOWGUARD_AUDIT_DB overrides).
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from flowguard import series
from flowguard.approval import ApprovalPage
from flowguard.audit import AuditLog
from flowguard.contracts import Event
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import tick_event
from flowguard.orchestrator import Orchestrator, level

ROLLOUT = "podinfo"
# Rollout state, read by the service only (the Monitor's own series are series.SELECTORS):
# pods up per pod-template hash, each ReplicaSet's creation time, Argo Rollouts' phase
ROLLOUT_SELECTORS = {
    "podinfo_up": 'up{app="podinfo"}',
    "replicasets": 'kube_replicaset_created{namespace=~"staging|prod",replicaset=~"podinfo-.*"}',
    "rollout_info": f'rollout_info{{name="{ROLLOUT}"}}',
}


@dataclass(frozen=True)
class RolloutState:
    paused: bool
    canary: str | None  # the newer revision's pod-template hash, while two serve
    stable: str | None

    @property
    def mid_canary(self) -> bool:
        return bool(self.canary and self.stable and self.canary != self.stable)


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
    return RolloutState(paused, order[-1], order[0])


def ratio(counts: tuple[float, float] | None) -> float:
    return counts[1] / counts[0] if counts and counts[0] > 0 else 0.0


def canary_split(idx: series.Index, t: float, namespace: str, state: RolloutState) -> dict:
    """The canary's and the stable pods' 5xx ratios over the last minute (Release's input)."""
    counts = series.by_hash(idx, t, namespace)
    return {
        "canary_5xx_ratio": ratio(counts.get(state.canary)),
        "stable_5xx_ratio": ratio(counts.get(state.stable)),
    }


@dataclass
class Service:
    orch: Orchestrator
    source: series.PrometheusSource
    environment: str = "staging"
    watch: tuple[str, ...] = ("staging", "prod")
    asked: set[tuple[str, str]] = field(default_factory=set)  # (namespace, canary hash)

    def tick(self, t: float | None = None) -> list[dict]:
        """One poll: the Monitor's tick, then a rollout_step per newly held canary."""
        t = time.time() if t is None else t
        idx = self.source.fetch(t)
        states = {ns: rollout_state(idx, t, ns) for ns in self.watch}
        own = states.get(self.environment) or rollout_state(idx, t, self.environment)
        snapshot = series.snapshot(idx, t)
        if own.mid_canary:  # Release, if the Monitor hands it an incident, compares these
            snapshot.update(canary_split(idx, t, self.environment, own))
        tick = tick_event(
            snapshot,
            series.firing(idx, t),
            t,
            environment=self.environment,
            in_canary=own.mid_canary,
        )
        results = [self.orch.handle(tick)]
        for ns, state in states.items():
            if not (state.paused and state.mid_canary) or (ns, state.canary) in self.asked:
                continue
            self.asked.add((ns, state.canary))
            results.append(
                self.orch.handle(
                    Event(
                        id=f"rollout-{ns}-{state.canary}",
                        type="rollout_step",
                        service=ROLLOUT,
                        environment=ns,
                        payload={
                            "metrics": canary_split(idx, t, ns, state),
                            "in_canary": True,
                            "params": {"ts": t},
                        },
                        received_at=datetime.fromtimestamp(t, UTC),
                    )
                )
            )
        return results

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", default="L1", choices=("L0", "L1", "L2", "L3", "L4", "L5"))
    parser.add_argument("--live", action="store_true", help="the executor acts (else dry run)")
    parser.add_argument("--env", default="staging", choices=("staging", "prod"))
    parser.add_argument("--watch", default="staging,prod")
    parser.add_argument("--interval", type=float, default=30.0)  # NFR: detect within 2 polls
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--approval-port", type=int, default=8765)
    args = parser.parse_args(argv)
    audit = AuditLog.open(level=args.level)
    page = ApprovalPage(port=args.approval_port)
    orch = Orchestrator(
        cfg=level(args.level),
        record=IncidentRecord(audit.path),
        executor=Executor(audit=audit, dry_run=not args.live),
        audit=audit,
        approver=page,
    )
    service = Service(
        orch,
        series.PrometheusSource(
            args.prometheus, selectors={**series.SELECTORS, **ROLLOUT_SELECTORS}
        ),
        environment=args.env,
        watch=tuple(n for n in args.watch.split(",") if n),
    )
    print(
        json.dumps(
            {
                "level": args.level,
                "executor": "live" if args.live else "dry run",
                "approval_page": page.url,
                "audit_log": str(audit.path),
                "monitor_model": "fitted" if orch.agents["monitor"].model else "none (L0 rules)",
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
