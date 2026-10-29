"""Demo driver: faults go on the chosen stable pods and always come off, nothing starts while L0
alerts fire (D50), the prod canary is held only once it serves, and status reads the audit log.
Every cluster call is replaced; nothing reaches kubectl."""

import pytest

import demo
from flowguard.audit import AuditLog
from flowguard.contracts import ActionProposal, GateDecision, Incident
from flowguard.incidents import IncidentRecord


class Load:
    def __init__(self, ns):
        self.ns, self.started, self.stopped = ns, False, False

    def start(self, label):
        self.started = True

    def stop(self):
        self.stopped = True


@pytest.fixture
def cluster(monkeypatch):
    calls = {"fault": [], "loads": [], "kubectl": [], "patched": []}

    def load(ns):
        calls["loads"].append(Load(ns))
        return calls["loads"][-1]

    monkeypatch.setattr(demo.deployer, "LoadGenerator", load)
    monkeypatch.setattr(demo, "set_fault", lambda pod, s: calls["fault"].append((pod, s)) or True)
    monkeypatch.setattr(demo.runner, "wait_alerts_quiet", lambda: [])
    monkeypatch.setattr(
        demo.runner, "stable_pods", lambda ns, h: [{"name": f"podinfo-{h}-{i}"} for i in range(4)]
    )
    monkeypatch.setattr(demo.runner, "kubectl", lambda *a: calls["kubectl"].append(a) or "")
    return calls


def rollouts(monkeypatch, statuses):
    states = iter(statuses)

    class Cluster:
        def __init__(self, ns):
            self.ns = ns

        def rollout(self):
            return {
                "status": next(states),
                "spec": {"template": {"spec": {"containers": [{"image": "img:1"}]}}},
            }

    monkeypatch.setattr(demo.deployer, "Cluster", Cluster)
    monkeypatch.setattr(demo.deployer, "container", lambda r: {"image": "img:1", "command": ["x"]})


def test_runtime_faults_half_the_stable_pods_and_always_clears(monkeypatch, cluster):
    rollouts(monkeypatch, [{"stableRS": "abc"}])
    slept = []
    assert demo.runtime(0.5, 0.5, 300, sleep=slept.append) == 0
    on = [p for p, s in cluster["fault"] if s is not None]
    assert on == ["podinfo-abc-0", "podinfo-abc-1"] and slept == [60, 300, 120]
    assert cluster["fault"][0][1]["error_rate"] == 0.5
    off = [p for p, s in cluster["fault"] if s is None]
    assert sorted(set(off)) == on and cluster["loads"][0].stopped


def test_runtime_clears_the_fault_when_interrupted(monkeypatch, cluster):
    rollouts(monkeypatch, [{"stableRS": "abc"}])

    def sleep(s):
        if s == 300:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        demo.runtime(0.25, 0.5, 300, sleep=sleep)
    assert cluster["fault"][-1] == ("podinfo-abc-0", None) and cluster["loads"][0].stopped


def test_runtime_refuses_while_l0_alerts_fire(monkeypatch, cluster):
    monkeypatch.setattr(demo.runner, "wait_alerts_quiet", lambda: ["ContainerOOMKilled"])
    assert demo.runtime(0.5, 0.5, 300) == 1
    assert cluster["fault"] == [] and cluster["loads"] == []


def test_prod_release_is_held_at_the_first_pause_then_followed(monkeypatch, cluster):
    monkeypatch.setattr(demo.deployer, "create_release_settings", lambda ns, v: "podinfo-release-x")
    monkeypatch.setattr(demo.runner, "patch_template", lambda *a: cluster["patched"].append(a))
    rollouts(
        monkeypatch,
        [
            {"stableRS": "old"},
            {"stableRS": "old", "currentPodHash": "new", "currentStepIndex": 0},
            {"stableRS": "old", "currentPodHash": "new", "currentStepIndex": 1},
            {"stableRS": "old", "currentPodHash": "new", "phase": "Paused"},
            {"stableRS": "new", "currentPodHash": "new", "phase": "Healthy"},
        ],
    )
    t = iter(range(0, 10_000, 5))
    assert demo.prod_release(900, sleep=lambda s: None, clock=lambda: next(t)) == 0
    (patch,) = cluster["patched"]
    assert patch[0] == "prod" and patch[3] == [{"secretRef": {"name": "podinfo-release-x"}}]
    (pause,) = cluster["kubectl"]
    assert pause[:4] == ("-n", "prod", "patch", "rollout") and '"paused": true' in pause[-1]
    assert cluster["loads"][0].ns == "prod" and cluster["loads"][0].stopped


def test_status_lists_incidents_with_evidence_and_proposals(tmp_path):
    db = tmp_path / "audit.db"
    audit = AuditLog(db, level="L1")
    rec = IncidentRecord(db)
    rec.open(Incident(id="i-1", source="monitor", service="podinfo", environment="prod",
                      detector="alert_rule"))  # fmt: skip
    p = ActionProposal(
        id="p-1", incident_id="i-1", agent="release", action="rollout_promote",
        target="podinfo", environment="prod", confidence=0.0, rationale="r",
    )  # fmt: skip
    audit.proposal(p)
    audit.gate_decision(GateDecision(proposal_id="p-1", tier="medium", risk_score=0.6,
                                     outcome="ask_human", reason="medium in prod"))  # fmt: skip
    audit.approval("p-1", "approved", "page")
    (item,) = demo.status(3, db)
    assert item["detector"] == "alert_rule" and item["environment"] == "prod"
    (prop,) = item["proposals"]
    assert (prop["action"], prop["gate"], prop["approval"], prop["executed"]) == (
        "rollout_promote", "ask_human", "approved", None,
    )  # fmt: skip
    assert demo.status(3, tmp_path / "empty.db") == []
