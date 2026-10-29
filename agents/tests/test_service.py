"""Live service: one poll gives the Monitor's tick and, for a canary held mid-rollout, one
rollout_step with the canary vs stable 5xx ratios; the gate, the approver and a dry-run
executor do the rest. Rollout state comes from Prometheus series only; all are fakes here."""

import json

import pytest

from flowguard import series
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.orchestrator import Orchestrator, level
from flowguard.service import RolloutState, Service, report, rollout_state

T = 1_800_000_000.0


def requests(ns, pod_hash, pods, ok, errors):
    """Counters for `pods` pods of one hash: `ok` 200s and `errors` 5xx each over the minute."""
    out = []
    for i in range(pods):
        labels = {"namespace": ns, "pod": f"podinfo-{pod_hash}-{i}", "path": "root",
                  "app": "podinfo", "rollouts_pod_template_hash": pod_hash}  # fmt: skip
        out.append({"metric": {**labels, "status": "200"},
                    "values": [[T - 45, "0"], [T, str(ok)]]})  # fmt: skip
        out.append({"metric": {**labels, "status": "500"},
                    "values": [[T - 45, "0"], [T, str(errors)]]})  # fmt: skip
    return out


def rollout_series(states):
    """namespace -> (paused, canary hash, stable hash) as Prometheus would show it: pods up per
    hash, ReplicaSet creation times, Argo Rollouts' phase."""
    raw = {"podinfo_up": [], "replicasets": [], "rollout_info": []}
    for ns, (paused, canary, stable) in states.items():
        pods = {stable: 8, canary: 2} if canary != stable else {stable: 10}
        for h, n in pods.items():
            for i in range(n):
                labels = {"namespace": ns, "pod": f"podinfo-{h}-{i}", "app": "podinfo",
                          "rollouts_pod_template_hash": h}  # fmt: skip
                raw["podinfo_up"].append({"metric": labels, "values": [[T - 10, "1"]]})
            created = T - 100 if h == canary and canary != stable else T - 86_400
            raw["replicasets"].append(
                {
                    "metric": {"namespace": ns, "replicaset": f"podinfo-{h}"},
                    "values": [[T - 10, str(created)]],
                }  # fmt: skip
            )
        phase = "Paused" if paused else ("Progressing" if canary != stable else "Healthy")
        raw["rollout_info"].append(
            {
                "metric": {"namespace": ns, "name": "podinfo", "phase": phase},
                "values": [[T - 10, "1"]],
            }  # fmt: skip
        )
    return raw


class Source:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, t):
        return series.index(self.raw)


def make(tmp_path, raw, states, name="L1", approve=True):
    db = tmp_path / "state.db"
    audit = AuditLog(db, level=name)
    orch = Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=True, kubectl=lambda a: "ok"),
        audit=audit,
        approver=SimulatedApprover(policy=lambda p: approve),
    )
    return Service(orch, Source({**raw, **rollout_series(states)}))


STEADY = {"staging": (False, "old", "old"), "prod": (False, "old", "old")}


def alert(ns, name="HighErrorRatio", at=T - 5):
    return [{"metric": {"alertname": name, "alertstate": "firing", "namespace": ns},
             "values": [[at, "1"]]}]  # fmt: skip


def test_a_quiet_poll_opens_nothing(tmp_path):
    svc = make(tmp_path, {"podinfo_requests": requests("staging", "old", 2, 60, 0)}, STEADY)
    outs = svc.tick(T)
    assert [o["event"].environment for o in outs] == ["staging", "prod"]  # one tick each
    assert all(o["output"].incident is False and "incident_id" not in o for o in outs)


def test_an_alert_opens_an_incident_followed_up_by_diagnose(tmp_path):
    raw = {"podinfo_requests": requests("staging", "old", 2, 60, 20), "alerts": alert("staging")}
    svc = make(tmp_path, raw, STEADY)
    out, prod = svc.tick(T)
    assert "incident_id" not in prod
    incident, _ = svc.orch.record.incident(out["incident_id"])
    assert incident.detector == "alert_rule" and incident.environment == "staging"
    assert out["agent"] == "diagnose"
    assert [p.action for p in out["output"].proposals] == ["escalate_to_human"]
    assert [d.outcome for d in out["decisions"]] == ["execute"]  # low tier: no human


def test_a_prod_fault_is_followed_up_as_prod(tmp_path):
    raw = {
        "podinfo_requests": requests("staging", "old", 2, 60, 0)
        + requests("prod", "old", 2, 60, 20),
        "alerts": alert("prod"),
    }
    svc = make(tmp_path, raw, STEADY)
    staging, prod = svc.tick(T)
    assert "incident_id" not in staging and not staging["event"].payload["alerts"]
    assert set(staging["event"].payload["metrics"]) == {
        "staging:error_ratio",
        "staging/podinfo-old-0:error_ratio",
        "staging/podinfo-old-1:error_ratio",
    }
    assert all(k.startswith("prod") for k in prod["event"].payload["metrics"])
    incident, _ = svc.orch.record.incident(prod["incident_id"])
    assert incident.environment == "prod" and prod["event"].environment == "prod"
    claims = [p.claim for p in svc.orch.record.evidence(prod["incident_id"])]
    assert "L0 alert HighErrorRatio firing (prod)" in claims
    assert [p.environment for p in prod["output"].proposals] == ["prod"]


def test_each_namespace_keeps_its_own_alert_episode(tmp_path):
    def poll(svc, t, *namespaces):
        svc.source.raw["alerts"] = [a for ns in namespaces for a in alert(ns, at=t - 5)]
        return [o["event"].environment for o in svc.tick(t) if o.get("incident_id")]

    svc = make(tmp_path, {}, STEADY)
    assert poll(svc, T, "staging", "prod") == ["staging", "prod"]  # one incident each
    assert poll(svc, T + 30, "staging", "prod") == []  # same episodes
    assert poll(svc, T + 60, "prod") == []  # staging resolved, prod still under way
    assert poll(svc, T + 90, "staging", "prod") == ["staging"]  # a new staging episode only


def test_a_held_prod_canary_goes_to_release_then_to_a_human_once(tmp_path):
    raw = {
        "podinfo_requests": requests("prod", "old", 8, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    states = {"staging": (False, "old", "old"), "prod": (True, "new", "old")}
    svc = make(tmp_path, raw, states)
    _, _, step = svc.tick(T)
    assert step["agent"] == "release" and step["event"].environment == "prod"
    (p,) = step["output"].proposals
    assert p.action == "rollout_promote" and p.environment == "prod"
    (d,) = step["decisions"]
    assert d.outcome == "ask_human" and d.human_verdict == "approved"  # medium in prod (D23)
    assert step["results"][0].success  # dry run
    assert len(svc.tick(T)) == 2  # the same held canary is not asked about again


def test_a_canary_that_is_not_held_is_left_to_its_steps(tmp_path):
    raw = {
        "podinfo_requests": requests("prod", "old", 8, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    states = {"staging": (False, "old", "old"), "prod": (False, "new", "old")}
    assert len(make(tmp_path, raw, states).tick(T)) == 2  # the two Monitor ticks only


def test_a_monitor_incident_mid_canary_gives_release_the_canary_split(tmp_path):
    raw = {
        "podinfo_requests": requests("staging", "old", 8, 60, 0)
        + requests("staging", "new", 2, 30, 30),
        "alerts": alert("staging"),
    }
    states = {"staging": (False, "new", "old"), "prod": (False, "old", "old")}
    svc = make(tmp_path, raw, states, approve=False)
    out, _ = svc.tick(T)  # not held: no rollout_step, but the tick is in_canary
    assert out["agent"] == "release"
    assert out["event"].payload["metrics"]["canary_5xx_ratio"] == pytest.approx(0.5)
    assert [p.action for p in out["output"].proposals] == ["rollout_abort"]  # not a promotion
    assert out["decisions"][0].human_verdict == "denied" and out["results"] == []


def test_rollout_state_from_prometheus():
    idx = series.index(rollout_series({"prod": (True, "new", "old")}))
    state = rollout_state(idx, T, "prod")
    assert state == RolloutState(True, "new", "old") and state.mid_canary
    steady = rollout_state(series.index(rollout_series({"prod": (False, "a", "a")})), T, "prod")
    assert steady == RolloutState(False, "a", "a") and not steady.mid_canary
    raw = rollout_series({"prod": (False, "new", "old")})
    raw["replicasets"] = []  # no creation times: the hash with fewer pods is the canary
    assert rollout_state(series.index(raw), T, "prod") == RolloutState(False, "new", "old")
    assert rollout_state(series.index({}), T, "prod") == RolloutState(False, None, None)


def test_report_prints_one_line_per_incident(tmp_path, capsys):
    report({"output": None})
    assert capsys.readouterr().out == ""
    raw = {
        "podinfo_requests": requests("prod", "old", 2, 60, 0) + requests("prod", "new", 2, 60, 0)
    }
    svc = make(tmp_path, raw, {"staging": (False, "o", "o"), "prod": (True, "new", "old")})
    report(svc.tick(T)[2])
    line = json.loads(capsys.readouterr().out)
    assert line["proposals"] == ["rollout_promote"] and line["gate"] == ["ask_human/approved"]
