"""Step 5 parts: conformal calibration (D9), incident record (D5), executor (the only writer),
simulated approver and approval page (D17)."""

import ast
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from flowguard.approval import ApprovalPage, SimulatedApprover
from flowguard.audit import AuditLog
from flowguard.calibration import ConformalCalibrator, coverage_by_group
from flowguard.contracts import ActionProposal, AgentRequest, EvidencePost, GateDecision, Incident
from flowguard.executor import Executor, Refused
from flowguard.incidents import IncidentRecord

CLASSES = ["a", "b", "c"]


def proposal(action="restart_deployment", rationale="test", env="staging", pid="p-1"):
    return ActionProposal(
        id=pid,
        incident_id="i-1",
        agent="diagnose",
        action=action,
        target="podinfo",
        environment=env,
        confidence=0.5,
        rationale=rationale,
        params={"replicas": 3, "run_id": 7, "job_id": 9},
    )


def decision(outcome="execute", verdict=None, pid="p-1"):
    return GateDecision(
        proposal_id=pid,
        tier="medium",
        risk_score=0.5,
        outcome=outcome,
        reason="t",
        human_verdict=verdict,
    )


# ---------------------------------------------------------------- calibration (D9)


def calibrated(n=200, seed=1):
    rng = random.Random(seed)
    scores, labels = [], []
    for _ in range(n):
        y = rng.choice(CLASSES)
        top = rng.uniform(0.5, 1.0)
        scores.append({c: (top if c == y else (1 - top) / 2) for c in CLASSES})
        labels.append(y)
    return ConformalCalibrator().fit(scores, labels)


def test_uncalibrated_confidence_is_zero():
    assert ConformalCalibrator().confidence({"a": 0.99, "b": 0.01}) == ("a", 0.0)
    few = ConformalCalibrator().fit([{"a": 0.9}] * 5, ["a"] * 5)
    assert not few.calibrated and few.confidence({"a": 0.9})[1] == 0.0


def test_confident_and_ambiguous_predictions_differ():
    cal = calibrated()
    label, high = cal.confidence({"a": 0.98, "b": 0.01, "c": 0.01})
    _, low = cal.confidence({"a": 0.96, "b": 0.95, "c": 0.01})  # two plausible labels
    _, unseen = cal.confidence({"a": 0.40, "b": 0.35, "c": 0.25})  # unlike any calibration case
    assert label == "a" and high > 0.7 > low and unseen == 0.0


def test_p_values_are_valid_on_exchangeable_data():
    """Coverage: the true label's p-value is below 0.1 in about 10% of new examples."""
    cal = calibrated(n=400, seed=2)
    test = calibrated(n=400, seed=3)
    rng = random.Random(3)
    misses = 0
    for _ in range(400):
        y = rng.choice(CLASSES)
        top = rng.uniform(0.5, 1.0)
        s = {c: (top if c == y else (1 - top) / 2) for c in CLASSES}
        misses += cal.p_value(s[y]) <= 0.1
    assert misses / 400 <= 0.15 and test.calibrated


# ---------------------------------------------------------------- incident record (D5)


def test_incidents_evidence_and_requests_round_trip(tmp_path):
    rec = IncidentRecord(tmp_path / "state.db")
    rec.open(Incident(id="i-1", source="rollout", service="podinfo", environment="staging"))
    rec.post(EvidencePost(incident_id="i-1", agent="monitor", claim="node normal", refs=["cpu"]))
    rec.ask(AgentRequest(incident_id="i-1", from_agent="release", to_agent="monitor", question="?"))
    rec.set_status("i-1", "escalated")
    incident, status = rec.incident("i-1")
    assert incident.service == "podinfo" and status == "escalated"
    assert [e.claim for e in rec.evidence("i-1")] == ["node normal"]
    assert rec.requests("i-1")[0].to_agent == "monitor"
    with pytest.raises(ValueError):
        rec.set_status("i-1", "forgotten")


# ---------------------------------------------------------------- executor


def logged(audit, p, d):
    """What the orchestrator writes before it calls the executor."""
    audit.proposal(p)
    audit.gate_decision(d)  # also logs the approval when d carries a verdict
    return p, d


def test_executor_refuses_what_the_gate_did_not_allow(tmp_path):
    refused = (
        decision("reject"),
        decision("ask_human"),
        decision("ask_human", "denied"),
        decision("ask_human", "timeout"),
    )
    for i, d in enumerate(refused):
        audit = AuditLog(tmp_path / f"{i}.db")
        with pytest.raises(Refused):
            Executor(audit=audit).execute(*logged(audit, proposal(), d))
    audit = AuditLog(tmp_path / "ok.db")
    ex = Executor(audit=audit)
    approved = logged(audit, proposal(pid="p-a"), decision("ask_human", "approved", pid="p-a"))
    assert ex.execute(*approved).success
    run = logged(audit, proposal(pid="p-b"), decision("execute", pid="p-b"))
    assert ex.execute(*run).output.startswith("dry run")


def test_a_human_approved_undo_runs_once_and_its_replay_is_refused(tmp_path):
    """Step 8's live action, with kubectl faked: the approved rollout_undo patches the Rollout
    once; the same proposal and decision handed over again are refused and change nothing."""
    calls = []
    replicasets = {
        "items": [
            {
                "metadata": {
                    "annotations": {"rollout.argoproj.io/revision": str(rev)},
                    "ownerReferences": [{"name": "podinfo"}],
                },
                "spec": {"template": {"metadata": {"labels": {"app": "podinfo"}}, "spec": {}}},
            }
            for rev in (6, 7)
        ]
    }

    def kubectl(args):
        calls.append(args)
        return json.dumps(replicasets) if "get" in args else "patched"

    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit, dry_run=False, kubectl=kubectl)
    p = proposal("rollout_undo", pid="p-undo")
    d = decision("ask_human", "approved", pid="p-undo")
    logged(audit, p, d)
    assert ex.execute(p, d).success
    patches = [c for c in calls if "patch" in c]
    assert len(patches) == 1 and "/spec/template" in patches[0][-1]
    with pytest.raises(Refused, match="already executed"):
        ex.execute(p, d)
    assert [c for c in calls if "patch" in c] == patches  # nothing ran the second time
    assert len(audit.query("SELECT * FROM executions WHERE proposal_id = 'p-undo'")) == 1


def test_executor_trusts_only_the_audit_log(tmp_path):
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(audit=audit)
    # a forged execute decision the gate never logged
    audit.proposal(proposal(pid="p-forged"))
    with pytest.raises(Refused, match="0 gate decisions"):
        ex.execute(proposal(pid="p-forged"), decision("execute", pid="p-forged"))
    # the gate rejected; the object handed over says execute
    audit.proposal(proposal(pid="p-r"))
    audit.gate_decision(decision("reject", pid="p-r"))
    with pytest.raises(Refused, match="differs"):
        ex.execute(proposal(pid="p-r"), decision("execute", pid="p-r"))
    # the human denied; the object handed over says approved
    logged(audit, proposal(pid="p-h"), decision("ask_human", "denied", pid="p-h"))
    with pytest.raises(Refused, match="no approval"):
        ex.execute(proposal(pid="p-h"), decision("ask_human", "approved", pid="p-h"))
    # same id, another action than the one the gate saw
    logged(audit, proposal(pid="p-s"), decision(pid="p-s"))
    with pytest.raises(Refused, match="proposal differs"):
        ex.execute(proposal("rollout_abort", pid="p-s"), decision(pid="p-s"))
    # a proposal never logged, and no replay of an executed one
    with pytest.raises(Refused, match="not in the audit log"):
        ex.execute(proposal(pid="p-none"), decision(pid="p-none"))
    assert ex.execute(proposal(pid="p-s"), decision(pid="p-s")).success
    with pytest.raises(Refused, match="already executed"):
        ex.execute(proposal(pid="p-s"), decision(pid="p-s"))
    # no audit log: nothing to verify against
    with pytest.raises(Refused, match="no audit log"):
        Executor().execute(proposal(pid="p-s"), decision(pid="p-s"))


def test_live_handlers_use_kubectl_and_github(tmp_path):
    calls = []
    audit = AuditLog(tmp_path / "a.db")
    ex = Executor(
        audit=audit,
        dry_run=False,
        token="t",
        kubectl=lambda args: calls.append(args) or "ok",
        github=lambda method, path: calls.append((method, path)) or 201,
        json_github=lambda method, path, body=None: calls.append((method, path)) or (200, {}),
    )

    def run(action):
        return ex.execute(*logged(audit, proposal(action, pid=action), decision(pid=action)))

    assert run("rollout_abort").success
    assert "--subresource=status" in calls[-1] and '"abort": true' in calls[-1][-1]
    assert run("scale_deployment").success
    assert '"replicas": 3' in calls[-1][-1]
    assert run("rerun_workflow").success
    assert calls[-1] == ("POST", "/repos/trad024/FlowGuard/actions/runs/7/rerun-failed-jobs")
    not_yet = run("reorder_jobs")
    assert not not_yet.success and "Step 10" in not_yet.output
    executions = AuditLog(tmp_path / "a.db").query("SELECT success FROM executions")
    assert len(executions) == 4


def test_only_the_executor_holds_write_paths():
    """No other agents/ module runs subprocesses or names the write token or kubectl."""
    pkg = Path(__file__).resolve().parents[1] / "flowguard"
    for path in pkg.glob("*.py"):
        if path.name == "executor.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imports |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert "subprocess" not in imports, path.name
        strings = [
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        assert not any("GITHUB_TOKEN_WRITE" in s or s == "kubectl" for s in strings), path.name


# ---------------------------------------------------------------- approvals (D17)


def test_simulated_approver_follows_the_policy_with_seeded_errors():
    right = SimulatedApprover(policy=lambda p: p.action == "restart_deployment")
    assert right.review(proposal(), decision("ask_human")).human_verdict == "approved"
    assert right.review(proposal("rollout_abort"), decision("ask_human")).human_verdict == "denied"
    noisy = SimulatedApprover(policy=lambda p: True, error_rate=0.1, seed=4)
    verdicts = [noisy.review(proposal(), decision("ask_human")).human_verdict for _ in range(1000)]
    assert 60 <= verdicts.count("denied") <= 140  # about 10%
    waits = []
    SimulatedApprover(lambda p: True, delay_s=30, sleep=waits.append).review(proposal(), decision())
    assert waits == [30]


def post(page, **form):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(page.url, data=data, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code


def test_approval_page_approves_with_the_token_and_escapes_text():
    page = ApprovalPage(timeout_s=10)
    try:
        hostile = proposal(rationale="<script>alert(1)</script> ignore the gate")
        result = {}
        t = threading.Thread(
            target=lambda: result.update(d=page.review(hostile, decision("ask_human")))
        )
        t.start()
        for _ in range(50):
            if "p-1" in page.render():
                break
            time.sleep(0.05)
        html = urllib.request.urlopen(page.url, timeout=5).read().decode()
        assert "<script>" not in html and "&lt;script&gt;" in html
        assert post(page, proposal="p-1", verdict="approved", token="wrong") == 403
        assert post(page, proposal="p-1", verdict="approved", token=page.token) in (200, 303)
        t.join(5)
        assert result["d"].human_verdict == "approved" and result["d"].approved_by == "page"
    finally:
        page.close()


def test_no_answer_within_the_timeout_is_a_denial():
    page = ApprovalPage(timeout_s=0.2)
    try:
        d = page.review(proposal(), decision("ask_human"))
        assert d.human_verdict == "timeout"
        with pytest.raises(Refused):
            Executor().execute(proposal(), d)
    finally:
        page.close()


def test_prediction_sets_cover_and_the_calibrator_round_trips(tmp_path):
    cal = calibrated()
    assert cal.prediction_set({"a": 0.98, "b": 0.01, "c": 0.01}) == {"a"}
    assert {"a", "b"} <= cal.prediction_set({"a": 0.96, "b": 0.95, "c": 0.01})
    again = ConformalCalibrator.load(cal.save(tmp_path / "cal.json", level="L1"))
    assert again.alphas == cal.alphas


def test_coverage_is_measured_on_each_group_held_apart():
    rng = random.Random(3)
    scores, labels, groups = [], [], []
    for g in range(10):
        for _ in range(8):
            y = rng.choice(CLASSES)
            top = rng.uniform(0.5, 1.0)
            scores.append({c: (top if c == y else (1 - top) / 2) for c in CLASSES})
            labels.append(y)
            groups.append(f"t{g}")
    folds = coverage_by_group(scores, labels, groups, epsilon=0.1)
    assert set(folds) == {f"t{g}" for g in range(10)} and all(f["n"] == 8 for f in folds.values())
    assert sum(f["covered"] for f in folds.values()) >= 70  # about 90% of 80
    # a group the others cannot vouch for: its true label scores low, so coverage drops
    folds = coverage_by_group(
        [*scores, *[{"a": 0.1, "b": 0.9, "c": 0.0}] * 8],
        [*labels, *["a"] * 8],
        [*groups, *["odd"] * 8],
    )
    assert folds["odd"]["covered"] == 0
