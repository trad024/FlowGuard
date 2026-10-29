"""Orchestrator: workflows A to D end to end with stub agents, per ablation level (dry run)."""

import pytest

from flowguard import audit_metrics
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.contracts import Event
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.monitor import ci_event
from flowguard.orchestrator import Orchestrator, level


def make(tmp_path, name="L3", approve=True, live=False, **kw):
    db = tmp_path / "state.db"
    audit = AuditLog(db)
    taps = []
    orch = Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=not live, kubectl=lambda a: "ok"),
        audit=audit,
        approver=SimulatedApprover(policy=lambda p: approve),
        input_tap=lambda agent, inp: taps.append((agent, inp)),
        **kw,
    )
    orch.taps = taps
    return orch


# A canary 10% 5xx (12 of 120) against a clean stable: Release's per-hash count windows
WORSE = {"windows": [[60, 6, 0, 240, 0, 0], [60, 6, 0, 240, 0, 0]]}


def event(kind, env="staging", **payload):
    return Event(id="e-1", type=kind, service="podinfo", environment=env, payload=payload)


def ci(log="", conclusion="failure", run_id=7):
    """A finished CI run as GitHub shows it; a failed run's first failed job has the log."""
    jobs = [
        {"id": 4, "name": "lint", "conclusion": "success"},
        {"id": 5, "name": "unit", "conclusion": conclusion},
    ]
    run = {"id": run_id, "status": "completed", "conclusion": conclusion}
    return ci_event(run, jobs, 1.8e9, log=log)


def test_b_ci_failure_is_diagnosed_gated_and_executed(tmp_path):
    orch = make(tmp_path)
    log = "--- FAIL: TestRefresh (0.02s)\n    x_test.go:9: not done after 12ms (budget 10ms)"
    out = orch.handle(ci(log))
    (p,) = out["output"].proposals
    assert p.action == "retry_job" and p.params["job_id"] == 5
    assert "ci_run" not in p.params  # the run's JSON is the Monitor's input, not an action's
    assert [d.outcome for d in out["decisions"]] == ["execute"]  # low tier
    assert out["results"][0].success
    assert orch.record.incident(out["incident_id"])[1] == "resolved"
    assert [a for a, _ in orch.taps] == ["monitor", "diagnose"]  # Monitor first (D49)
    incident, _ = orch.record.incident(out["incident_id"])
    assert incident.detector == "ci" and incident.ci_run_id == 7


def test_a_security_blocks_and_optimizer_keeps_forced_jobs(tmp_path):
    orch = make(tmp_path)
    out = orch.handle(event("scan_completed", scan_counts={"trivy_high": 2}))
    assert out["output"].proposals[0].action == "block_deployment"
    out = orch.handle(event("pipeline_started", changed_files=["app/podinfo/go.mod"]))
    assert out["output"].proposals == []  # an app change: nothing to skip
    out = orch.handle(event("pipeline_started", changed_files=["go.sum", "docs/x.md"]))
    skipped = out["output"].proposals[0].params["jobs"]
    assert "sbom" not in skipped and "scans" not in skipped  # P2


def test_c_monitor_incident_goes_to_release_mid_canary_else_diagnose(tmp_path):
    orch = make(tmp_path)
    quiet = orch.handle(event("metrics_tick"))
    assert quiet["output"].proposals == [] and quiet["agent"] == "monitor"
    assert "incident_id" not in quiet  # D49: no incident unless the Monitor reports one
    canary = orch.handle(
        event(
            "metrics_tick",
            alerts=["HighErrorRatio"],
            in_canary=True,
            metrics=WORSE,
        )
    )
    assert canary["agent"] == "release" and canary["output"].proposals[0].action == "rollout_abort"
    runtime = orch.handle(event("metrics_tick", alerts=["ContainerOOMKilled"]))
    assert runtime["agent"] == "diagnose"
    incident, _ = orch.record.incident(runtime["incident_id"])
    assert incident.source == "monitor" and incident.detector == "alert_rule"


def test_monitor_runs_first_and_only_its_incidents_are_opened(tmp_path):
    """D49: quiet ticks and green CI runs open nothing; the Monitor fills detector and score."""
    orch = make(tmp_path)
    assert "incident_id" not in orch.handle(ci(conclusion="success"))
    assert "incident_id" not in orch.handle(event("metrics_tick"))
    with orch.record._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 0
    again = orch.handle(ci("./a.go:3:2: undefined: foo", run_id=8))
    incident, _ = orch.record.incident(again["incident_id"])
    assert incident.detector == "ci" and incident.anomaly_score is None
    assert any("CI run 8 failed: unit" in e.claim for e in orch.record.evidence(incident.id))


def test_a_runtime_detection_carries_its_score(tmp_path):
    from flowguard.stubs import AgentOutput

    orch = make(tmp_path)

    class Scored:
        def run(self, inp, cfg):
            return AgentOutput(incident=True, detector="runtime", anomaly_score=0.83)

    orch.agents["monitor"] = Scored()
    out = orch.handle(event("metrics_tick"))
    incident, _ = orch.record.incident(out["incident_id"])
    assert (incident.detector, incident.anomaly_score) == ("runtime", 0.83)


def test_d_p1_holds_a_rollback_when_the_node_is_under_stress(tmp_path):
    metrics = {**WORSE, "node_cpu_ratio": 0.97}
    l4 = make(tmp_path / "l4", "L4")
    out = l4.handle(event("rollout_step", metrics=metrics))
    assert out["output"].proposals[0].action == "escalate_to_human"
    assert {r.to_agent for r in l4.record.requests(out["incident_id"])} == {"monitor", "diagnose"}
    assert any("stress" in e.claim for e in l4.record.evidence(out["incident_id"]))
    l3 = make(tmp_path / "l3", "L3")  # no collaboration: rolls back
    assert l3.handle(event("rollout_step", metrics=metrics))["output"].proposals[0].action == (
        "rollout_abort"
    )


class ScriptedRelease:
    """Release stand-in: optionally asks Monitor (P1), then proposes `action` or holds."""

    def __init__(self, action=None, ask_first=False):
        self.action, self.ask_first = action, ask_first

    def run(self, inp, cfg, ask=None):
        from flowguard.contracts import ActionProposal, EvidencePost
        from flowguard.stubs import AgentOutput

        evidence = [EvidencePost(incident_id=inp.incident_id, agent="release", claim="look")]
        if self.ask_first:
            evidence.append(ask("monitor", "Is the node or cluster under stress?"))
        proposals = []
        if self.action:
            proposals.append(
                ActionProposal(id="p-1", incident_id=inp.incident_id, agent="release",
                               action=self.action, target=inp.target,
                               environment=inp.environment, confidence=0.0, rationale="x")
            )  # fmt: skip
        return AgentOutput(proposals=proposals, evidence=evidence)


def incidents(orch):
    with orch.record._connect() as db:
        return db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]


def test_a_canary_look_that_holds_opens_no_incident(tmp_path):
    orch = make(tmp_path)
    orch.agents["release"] = ScriptedRelease()
    out = orch.handle(event("rollout_step", in_canary=True))
    assert "incident_id" not in out and out["output"].proposals == []
    assert incidents(orch) == 0 and orch.audit.query("SELECT 1 FROM proposals") == []
    assert [a for a, _ in orch.taps] == ["release"]


def test_a_canary_look_that_proposes_opens_one_rollout_incident(tmp_path):
    orch = make(tmp_path)
    orch.agents["release"] = ScriptedRelease("rollout_promote")
    out = orch.handle(event("rollout_step", in_canary=True))
    incident, _ = orch.record.incident(out["incident_id"])
    assert incident.source == "rollout" and incident.in_canary and incidents(orch) == 1
    assert [e.claim for e in orch.record.evidence(incident.id)] == ["look"]
    assert [d.outcome for d in out["decisions"]] == ["ask_human"]  # medium, no calibration


def test_a_p1_question_opens_the_incident_before_it_is_recorded(tmp_path):
    orch = make(tmp_path, "L4")
    orch.agents["release"] = ScriptedRelease("escalate_to_human", ask_first=True)
    out = orch.handle(event("rollout_step", in_canary=True))
    (request,) = orch.record.requests(out["incident_id"])
    assert request.to_agent == "monitor" and incidents(orch) == 1
    claims = [e.claim for e in orch.record.evidence(out["incident_id"])]
    assert claims[0].startswith("node stress unknown") and "look" in claims


def test_medium_actions_go_to_the_approver_and_denials_do_not_run(tmp_path):
    denied = make(tmp_path / "d", approve=False)
    out = denied.handle(event("rollout_step", metrics=WORSE))
    (d,) = out["decisions"]
    assert d.outcome == "ask_human" and d.human_verdict == "denied" and out["results"] == []
    approved = make(tmp_path / "a", approve=True)
    out = approved.handle(event("rollout_step", metrics=WORSE))
    assert out["decisions"][0].human_verdict == "approved" and out["results"][0].success
    assert audit_metrics.executed_without_required_approval(approved.audit) == 0


def test_l0_has_no_gate_and_never_runs_live(tmp_path):
    with pytest.raises(ValueError, match="dry-run"):
        make(tmp_path / "live", "L0", live=True)
    l0 = make(tmp_path, "L0")
    out = l0.handle(event("rollout_step", env="prod", metrics=WORSE))
    assert out["decisions"][0].reason == "L0: no gate" and out["results"][0].success


def test_l5_sweeps_the_threshold():
    assert level("L5", threshold=0.9).threshold == 0.9 and level("L4").threshold == 0.70


def test_everything_is_audited(tmp_path):
    orch = make(tmp_path)
    orch.handle(ci("./a.go:3:2: undefined: foo"))
    counts = {
        t: len(orch.audit.query(f"SELECT 1 FROM {t}")) for t in ("proposals", "gate_decisions")
    }
    assert counts == {"proposals": 1, "gate_decisions": 1}
