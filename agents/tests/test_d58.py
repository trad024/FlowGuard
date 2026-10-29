"""D58: every executor refusal is written to its own table, before Refused is raised; `executions`
keeps its meaning, so a refused attempt changes no reader of it; approvals rows say who answered,
on which channel, after how long."""

import sqlite3

import pytest

from flowguard import audit_metrics
from flowguard.approval import SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.contracts import ActionProposal, GateDecision
from flowguard.executor import Executor, Refused
from flowguard.gate import RiskGate
from flowguard.incidents import IncidentRecord
from flowguard.monitor import ci_event
from flowguard.orchestrator import Orchestrator, level
from flowguard.stubs import Diagnose


def proposal(pid, action="restart_deployment"):
    return ActionProposal(
        id=pid,
        incident_id="i-1",
        agent="diagnose",
        action=action,
        target="podinfo",
        environment="staging",
        confidence=0.5,
        rationale="t",
        params={},
    )


def decision(pid, outcome="ask_human", verdict=None):
    return GateDecision(
        proposal_id=pid,
        tier="medium",
        risk_score=0.5,
        outcome=outcome,
        reason="t",
        human_verdict=verdict,
    )


def readers(audit):
    """What every reader of `executions` sees: the safety counters and the gate's loop count."""
    gate = RiskGate(audit=audit)
    return (
        audit_metrics.executed_without_required_approval(audit),
        audit_metrics.executed_after_reject(audit),
        gate._recent(("restart_deployment", "podinfo")),
        len(audit.query("SELECT * FROM executions")),
    )


def test_a_refused_attempt_changes_no_reader_of_executions(tmp_path):
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit)
    asked, rejected = proposal("p-ask"), proposal("p-rej")
    for p, d in ((asked, decision("p-ask")), (rejected, decision("p-rej", outcome="reject"))):
        audit.proposal(p)
        audit.gate_decision(d)
    before = readers(audit)
    with pytest.raises(Refused, match="no approval"):  # asked a human, nobody approved
        ex.execute(asked, decision("p-ask", verdict="approved"))
    with pytest.raises(Refused):  # the gate rejected it
        ex.execute(rejected, decision("p-rej", outcome="reject"))
    with pytest.raises(Refused, match="not in the audit log"):  # never logged at all
        ex.execute(proposal("p-x"), decision("p-x", outcome="execute"))
    assert readers(audit) == before == (0, 0, 0, 0)
    rows = audit.query("SELECT proposal_id, reason FROM refusals ORDER BY id")
    assert [r["proposal_id"] for r in rows] == ["p-ask", "p-rej", "p-x"]
    assert "no approval" in rows[0]["reason"] and "not in the audit log" in rows[2]["reason"]


def test_after_a_refusal_the_legitimate_execution_runs_once(tmp_path):
    """The already-executed check reads executions only: an earlier refusal does not block."""
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit)
    p = proposal("p-1")
    audit.proposal(p)
    audit.gate_decision(decision("p-1"))
    with pytest.raises(Refused):
        ex.execute(p, decision("p-1", verdict="approved"))
    audit.approval("p-1", "approved", "Ammar", 12.0, "page")
    assert ex.execute(p, decision("p-1", verdict="approved")).success
    with pytest.raises(Refused, match="already executed"):
        ex.execute(p, decision("p-1", verdict="approved"))
    assert len(audit.query("SELECT * FROM executions")) == 1
    assert len(audit.query("SELECT * FROM refusals")) == 2


def test_an_older_audit_log_gains_the_new_table_and_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '1');"
        "CREATE TABLE approvals (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, proposal_id TEXT NOT"
        " NULL, scenario_id TEXT, level TEXT NOT NULL, verdict TEXT NOT NULL, approver TEXT,"
        " wait_seconds REAL);"
    )
    db.commit()
    db.close()
    log = AuditLog(path)
    assert log.query("SELECT value FROM meta WHERE key = 'schema_version'")[0]["value"] == "2"
    assert "channel" in {r["name"] for r in log.query("PRAGMA table_info(approvals)")}
    log.refusal("p-1", "test")
    assert log.query("SELECT reason FROM refusals")[0]["reason"] == "test"


def test_the_approvals_row_says_who_answered_on_which_channel_after_how_long(tmp_path):
    db = tmp_path / "state.db"
    audit = AuditLog(db)
    approver = SimulatedApprover(policy=lambda p: True, delay_s=2.5, sleep=lambda s: None)

    class QuotingLLM:
        def __call__(self, log, examples=None):
            return {"root_cause": "code", "evidence_lines": [1], "quotes": log.splitlines()[:1],
                    "action": "open_fix_pr"}  # fmt: skip

    class NoExamples:
        def nearest(self, log):
            return []

    orch = Orchestrator(
        cfg=level("L5", threshold=0.99),  # nothing medium passes: a human is asked
        record=IncidentRecord(db),
        executor=Executor(audit=audit),
        audit=audit,
        approver=approver,
        diagnose=Diagnose(QuotingLLM(), retriever=NoExamples()),
    )
    run = {"id": 7, "status": "completed", "conclusion": "failure"}
    jobs = [{"id": 5, "name": "unit", "conclusion": "failure"}]
    orch.handle(ci_event(run, jobs, 1.8e9, log="./a.go:3:2: undefined: foo\n"))
    rows = audit.query("SELECT approver, channel, wait_seconds FROM approvals")
    assert rows and all(
        (r["approver"], r["channel"], r["wait_seconds"]) == ("simulated", "simulated", 2.5)
        for r in rows
    )
