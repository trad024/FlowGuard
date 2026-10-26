"""Audit log: schema, append-only writers for every record type, contracts stored as-is, and
the metrics computed from it."""

import json
import sqlite3

import pytest

from flowguard import audit_metrics
from flowguard.audit import AuditLog
from flowguard.contracts import (
    ActionProposal,
    AgentName,
    ExecutionResult,
    GateDecision,
    RiskTier,
)


@pytest.fixture
def log(tmp_path):
    return AuditLog(tmp_path / "audit.db", scenario_id="dev-001", level="L0")


def proposal(pid, action="rollout_undo", incident="inc-1"):
    return ActionProposal(
        id=pid,
        incident_id=incident,
        agent=AgentName.RELEASE,
        action=action,
        target="rollout/podinfo",
        environment="staging",
        confidence=0.9,
        rationale="canary error ratio above stable",
    )


def test_schema_has_the_five_record_types(log):
    tables = {r["name"] for r in log.query("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "proposals", "gate_decisions", "approvals", "executions"} <= tables
    assert log.query("SELECT value FROM meta WHERE key='schema_version'")[0]["value"] == "1"


def test_events_carry_scenario_level_and_payload(log):
    log.event(
        "deployer",
        "deploy_finished",
        environment="staging",
        commit_sha="abc",
        outcome="promoted",
        seconds=285,
    )
    (row,) = log.events("deploy_finished")
    assert (row["scenario_id"], row["level"], row["environment"]) == ("dev-001", "L0", "staging")
    assert row["payload"] == {"outcome": "promoted", "seconds": 285}
    assert row["ts"].endswith("Z")


def test_contracts_are_stored_with_their_own_fields(log):
    pid = log.proposal(proposal("p1"))
    log.gate_decision(
        GateDecision(
            proposal_id=pid,
            tier=RiskTier.HIGH,
            risk_score=0.8,
            outcome="ask_human",
            reason="high tier",
            human_verdict="approved",
            approved_by="ammar1304",
        )
    )
    log.execution(ExecutionResult(proposal_id=pid, success=True, output="undone"))
    (p,) = log.query("SELECT * FROM proposals")
    assert (p["agent"], p["action"], json.loads(p["params"])) == ("release", "rollout_undo", {})
    (a,) = log.query("SELECT * FROM approvals")
    assert (a["verdict"], a["approver"]) == ("approved", "ammar1304")
    (e,) = log.query("SELECT * FROM executions")
    assert (e["success"], e["validated"]) == (1, None)


def test_decisions_must_reference_a_logged_proposal(log):
    with pytest.raises(sqlite3.IntegrityError):
        log.gate_decision(
            {
                "proposal_id": "nope",
                "tier": "low",
                "risk_score": 0.1,
                "outcome": "execute",
                "reason": "x",
            }
        )


def test_invalid_outcomes_and_verdicts_are_refused(log):
    pid = log.proposal(proposal("p1"))
    with pytest.raises(ValueError):
        log.gate_decision(
            {
                "proposal_id": pid,
                "tier": "low",
                "risk_score": 0.1,
                "outcome": "maybe",
                "reason": "x",
            }
        )
    with pytest.raises(ValueError):
        log.approval(pid, "sure", "me")


def test_open_uses_environment_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "x.db"))
    monkeypatch.setenv("FLOWGUARD_SCENARIO", "dev-007")
    monkeypatch.setenv("FLOWGUARD_LEVEL", "L3")
    a = AuditLog.open()
    assert (a.path.name, a.scenario_id, a.level) == ("x.db", "dev-007", "L3")


def test_metrics_deployments_and_security(log):
    for env, outcome, s in [
        ("staging", "promoted", 280),
        ("staging", "promoted", 300),
        ("staging", "aborted", 120),
        ("prod", "promoted", 290),
    ]:
        log.event("deployer", "deploy_finished", environment=env, outcome=outcome, seconds=s)
    log.event("security_gate", "security_decision", decision="block", high=3)
    log.event("security_gate", "security_decision", decision="allow", high=0)
    s = audit_metrics.summary(log, "dev-001")
    assert s["deployments"]["staging"]["outcomes"] == {"promoted": 2, "aborted": 1}
    assert s["deployments"]["staging"]["seconds_to_promote"] == {"n": 2, "median": 290, "max": 300}
    assert s["deployments"]["prod"]["outcomes"] == {"promoted": 1}
    assert s["security_decisions"] == {"block": 1, "allow": 1}
    assert audit_metrics.summary(log, "other")["security_decisions"] == {}


def test_unapproved_high_risk_executions_counts_only_unapproved_ones(log):
    for pid, verdict in [("p1", "approved"), ("p2", "denied"), ("p3", None)]:
        log.proposal(proposal(pid))
        log.gate_decision(
            {
                "proposal_id": pid,
                "tier": "high",
                "risk_score": 0.9,
                "outcome": "ask_human",
                "reason": "high tier",
                "human_verdict": verdict,
            }
        )
        log.execution({"proposal_id": pid, "success": True, "output": ""})
    # p2 (denied) and p3 (no verdict) were executed anyway: both are violations
    assert audit_metrics.unapproved_high_risk_executions(log) == 2
    assert audit_metrics.approvals_per_incident(log) == 2


def test_metrics_cli_prints_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "cli.db"))
    assert audit_metrics.main([]) == 0
    assert json.loads(capsys.readouterr().out)["unapproved_high_risk_executions"] == 0
