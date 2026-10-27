"""Orchestrator: workflows A to D end to end with stub agents, per ablation level (dry run)."""

import pytest

from flowguard import audit_metrics
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.contracts import Event
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
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


def event(kind, env="staging", **payload):
    return Event(id="e-1", type=kind, service="podinfo", environment=env, payload=payload)


def test_b_ci_failure_is_diagnosed_gated_and_executed(tmp_path):
    orch = make(tmp_path)
    log = "--- FAIL: TestRefresh (0.02s)\n    x_test.go:9: not done after 12ms (budget 10ms)"
    out = orch.handle(event("ci_failure", log=log, params={"job_id": 5}))
    (p,) = out["output"].proposals
    assert p.action == "retry_job" and p.params["job_id"] == 5
    assert [d.outcome for d in out["decisions"]] == ["execute"]  # low tier
    assert out["results"][0].success
    assert orch.record.incident(out["incident_id"])[1] == "resolved"
    assert [a for a, _ in orch.taps] == ["diagnose"]


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
    canary = orch.handle(
        event(
            "metrics_tick",
            alerts=["HighErrorRatio"],
            in_canary=True,
            metrics={"canary_5xx_ratio": 0.08, "stable_5xx_ratio": 0.0},
        )
    )
    assert canary["agent"] == "release" and canary["output"].proposals[0].action == "rollout_abort"
    runtime = orch.handle(event("metrics_tick", alerts=["ContainerOOMKilled"]))
    assert runtime["agent"] == "diagnose"


def test_d_p1_holds_a_rollback_when_the_node_is_under_stress(tmp_path):
    metrics = {"canary_5xx_ratio": 0.05, "stable_5xx_ratio": 0.0, "node_cpu_ratio": 0.97}
    l4 = make(tmp_path / "l4", "L4")
    out = l4.handle(event("rollout_step", metrics=metrics))
    assert out["output"].proposals[0].action == "escalate_to_human"
    assert {r.to_agent for r in l4.record.requests(out["incident_id"])} == {"monitor", "diagnose"}
    assert any("stress" in e.claim for e in l4.record.evidence(out["incident_id"]))
    l3 = make(tmp_path / "l3", "L3")  # no collaboration: rolls back
    assert l3.handle(event("rollout_step", metrics=metrics))["output"].proposals[0].action == (
        "rollout_abort"
    )


def test_medium_actions_go_to_the_approver_and_denials_do_not_run(tmp_path):
    denied = make(tmp_path / "d", approve=False)
    out = denied.handle(
        event("rollout_step", metrics={"canary_5xx_ratio": 0.05, "stable_5xx_ratio": 0.0})
    )
    (d,) = out["decisions"]
    assert d.outcome == "ask_human" and d.human_verdict == "denied" and out["results"] == []
    approved = make(tmp_path / "a", approve=True)
    out = approved.handle(
        event("rollout_step", metrics={"canary_5xx_ratio": 0.05, "stable_5xx_ratio": 0.0})
    )
    assert out["decisions"][0].human_verdict == "approved" and out["results"][0].success
    assert audit_metrics.executed_without_required_approval(approved.audit) == 0


def test_l0_has_no_gate_and_never_runs_live(tmp_path):
    with pytest.raises(ValueError, match="dry-run"):
        make(tmp_path / "live", "L0", live=True)
    l0 = make(tmp_path, "L0")
    out = l0.handle(
        event(
            "rollout_step", env="prod", metrics={"canary_5xx_ratio": 0.05, "stable_5xx_ratio": 0.0}
        )
    )
    assert out["decisions"][0].reason == "L0: no gate" and out["results"][0].success


def test_l5_sweeps_the_threshold():
    assert level("L5", threshold=0.9).threshold == 0.9 and level("L4").threshold == 0.70


def test_everything_is_audited(tmp_path):
    orch = make(tmp_path)
    orch.handle(event("ci_failure", log="./a.go:3:2: undefined: foo"))
    counts = {
        t: len(orch.audit.query(f"SELECT 1 FROM {t}")) for t in ("proposals", "gate_decisions")
    }
    assert counts == {"proposals": 1, "gate_decisions": 1}
