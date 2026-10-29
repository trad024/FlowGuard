"""Audit-log exporter: counts per level from a real audit log and incident record, label
escaping, read-only access, a missing file, and the HTTP endpoint on loopback."""

import threading
import urllib.request

import pytest

from flowguard import exporter
from flowguard.audit import AuditLog
from flowguard.contracts import ActionProposal, ExecutionResult, GateDecision, Incident
from flowguard.incidents import IncidentRecord


def proposal(pid, action, agent="release", incident="i-1"):
    return ActionProposal(
        id=pid,
        incident_id=incident,
        agent=agent,
        action=action,
        target="podinfo",
        environment="staging",
        confidence=0.0,
        rationale="secret rationale text",
    )


def decision(pid, outcome, tier="medium"):
    return GateDecision(proposal_id=pid, tier=tier, risk_score=0.5, outcome=outcome, reason="r")


def build(path):
    l1 = AuditLog(path, level="L1")
    l1.proposal(proposal("p-1", "rollout_abort"))
    l1.gate_decision(decision("p-1", "ask_human"))
    l1.approval("p-1", "approved", "hakim")
    l1.execution(ExecutionResult(proposal_id="p-1", success=True, output="ok"))
    l1.proposal(proposal("p-2", 'evil"\nname', agent="diagnose", incident="i-2"))
    l1.gate_decision(decision("p-2", "reject", tier="prohibited"))
    AuditLog(path, level="L0").event(
        "deployer", "deploy_finished", environment="staging", outcome="aborted"
    )
    rec = IncidentRecord(path)
    rec.open(Incident(id="i-1", source="monitor", service="podinfo", environment="staging",
                      detector="runtime", anomaly_score=0.7))  # fmt: skip
    return path


def lines(text):
    return {line for line in text.splitlines() if not line.startswith("#")}


def test_counts_per_level_and_safety(tmp_path):
    text = exporter.render(build(tmp_path / "audit.db"))
    got = lines(text)
    assert "flowguard_audit_up 1" in got
    assert 'flowguard_gate_decisions_total{level="L1",outcome="ask_human",tier="medium"} 1' in got
    assert 'flowguard_approvals_total{level="L1",verdict="approved"} 1' in got
    assert 'flowguard_executions_total{level="L1",success="true"} 1' in got
    assert (
        'flowguard_deployments_total{level="L0",environment="staging",outcome="aborted"} 1' in got
    )
    assert 'flowguard_incidents_total{source="monitor",detector="runtime",status="open"} 1' in got
    assert "flowguard_executed_without_required_approval 0" in got
    assert "flowguard_executed_after_reject 0" in got
    # i-1 had one answer, i-2 none
    assert 'flowguard_approvals_per_incident{level="L1"} 0.5' in got
    assert "secret rationale" not in text  # counts only


def test_label_values_are_escaped():
    assert exporter.sample("m", {"a": 'x"\n\\'}, 1) == 'm{a="x\\"\\n\\\\"} 1'
    assert exporter.sample("m", {}, 2.5) == "m 2.5"


def test_a_hostile_action_name_stays_one_line(tmp_path):
    text = exporter.render(build(tmp_path / "audit.db"))
    hostile = [line for line in text.splitlines() if "evil" in line]
    assert hostile == [
        'flowguard_proposals_total{level="L1",agent="diagnose",action="evil\\"\\nname"} 1'
    ]


def test_missing_file_and_unexecuted_approval(tmp_path):
    assert lines(exporter.render(tmp_path / "none.db")) == {"flowguard_audit_up 0"}
    path = tmp_path / "audit.db"
    audit = AuditLog(path, level="L1")
    audit.proposal(proposal("p-9", "rollout_undo"))
    audit.gate_decision(decision("p-9", "ask_human", tier="high"))
    audit.execution(ExecutionResult(proposal_id="p-9", success=True, output="forced"))
    assert "flowguard_executed_without_required_approval 1" in lines(exporter.render(path))


def test_read_only(tmp_path):
    path = build(tmp_path / "audit.db")
    before = path.read_bytes()
    exporter.render(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "[::]", "", " ", "*"])
def test_it_never_listens_on_every_interface(tmp_path, host, capsys):
    with pytest.raises(ValueError, match="all interfaces"):
        exporter.serve(host, 0, tmp_path / "audit.db")
    with pytest.raises(SystemExit) as exc:
        exporter.main(["--host", host, "--port", "0"])
    assert exc.value.code == 2 and "all interfaces" in capsys.readouterr().err
    assert exporter.bind_address(" 172.18.0.1 ") == "172.18.0.1"  # one interface is fine


def test_http_endpoint_on_loopback(tmp_path):
    server = exporter.serve("127.0.0.1", 0, build(tmp_path / "audit.db"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
            assert r.headers["Content-Type"].startswith("text/plain; version=0.0.4")
            assert b"flowguard_audit_up 1" in r.read()
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/other", timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
