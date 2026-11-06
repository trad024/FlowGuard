"""D62: the executor runs pin_dependency and open_fix_pr through flowguard.github_actions, with a
transport built from the write token it reads; a dry run never touches the transport."""

import pytest

from flowguard import github_actions
from flowguard.audit import AuditLog
from flowguard.contracts import ActionProposal, GateDecision
from flowguard.executor import Executor


class CountingTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        return 200, {}


def logged(audit, action, pid):
    p = ActionProposal(
        id=pid,
        incident_id="i-1",
        agent="diagnose",
        action=action,
        target="podinfo",
        environment="staging",
        confidence=0.5,
        rationale="test",
        params={"module": "golang.org/x/text", "version": "v0.20.0"},
    )
    d = GateDecision(
        proposal_id=pid,
        tier="medium",
        risk_score=0.5,
        outcome="ask_human",
        reason="t",
        human_verdict="approved",
    )
    audit.proposal(p)
    audit.gate_decision(d)
    return p, d


@pytest.mark.parametrize("action", ["pin_dependency", "open_fix_pr"])
def test_a_dry_run_makes_no_transport_call(tmp_path, action):
    transport = CountingTransport()
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit, dry_run=True, token="t", json_github=transport)
    result = ex.execute(*logged(audit, action, "p-1"))
    assert result.success and result.output == f"dry run: {action}"
    assert transport.calls == []


@pytest.mark.parametrize("action", ["pin_dependency", "open_fix_pr"])
def test_live_runs_the_modules_handler_with_the_executors_transport(tmp_path, monkeypatch, action):
    seen = []

    def handler(client, repo, proposal_id, params):
        seen.append((client, repo, proposal_id, params))
        return f"{action} done"

    monkeypatch.setitem(github_actions.HANDLERS, action, handler)
    transport = CountingTransport()
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit, dry_run=False, token="t", json_github=transport)
    result = ex.execute(*logged(audit, action, "p-2"))
    assert result.success and result.output == f"{action} done"
    (call,) = seen
    assert call[0] is transport and call[1] == "trad024/FlowGuard" and call[2] == "p-2"


def test_live_without_a_write_token_is_a_failed_execution(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN_WRITE", raising=False)
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit, dry_run=False, token="")
    result = ex.execute(*logged(audit, "pin_dependency", "p-3"))
    assert not result.success and "GITHUB_TOKEN_WRITE" in result.output
