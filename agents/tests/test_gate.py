"""Risk gate: v1 rules 0 to 7 (D36), the v1 risk score, P3 conflicts, calibrated confidence."""

from datetime import UTC, datetime, timedelta

import pytest

from flowguard.contracts import ActionProposal, RiskTier
from flowguard.gate import RiskGate, risk_score

N = 0


def prop(action, agent="diagnose", env="staging", target="podinfo", incident="i-1", conf=0.0):
    global N
    N += 1
    return ActionProposal(
        id=f"p-{N}",
        incident_id=incident,
        agent=agent,
        action=action,
        target=target,
        environment=env,
        confidence=conf,
        rationale="test",
    )


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, 12, tzinfo=UTC)

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("p", "confidence", "outcome", "rule"),
    [
        (prop("rollout_undo", agent="optimizer"), 1.0, "reject", 0),  # not its role
        (prop("drop_database"), 1.0, "reject", 0),  # unknown, and no agent may propose it
        (prop("rollout_undo", agent="release"), 1.0, "ask_human", 4),
        (prop("restart_deployment", env="prod"), 1.0, "ask_human", 5),
        (prop("restart_deployment"), 0.69, "ask_human", 6),
        (prop("restart_deployment"), 0.70, "execute", 7),
        (prop("retry_job"), 0.0, "execute", 7),
    ],
)
def test_rules_first_match_wins(p, confidence, outcome, rule):
    d = RiskGate().decide(p, confidence)
    assert (d.outcome, d.reason.split(":")[0]) == (outcome, f"rule {rule}")


def test_rules_1_and_2_when_the_role_allows_the_action():
    roles = {"diagnose": frozenset({"drop_database", "kubectl_exec"})}
    gate = RiskGate(agent_actions=roles)
    assert gate.decide(prop("drop_database")).reason.startswith("rule 1")
    d = gate.decide(prop("kubectl_exec"))
    assert d.reason.startswith("rule 2") and d.tier == RiskTier.PROHIBITED


def test_the_proposals_own_confidence_is_ignored():
    # D9: a self-reported 0.99 does not pass a medium action; only the calibrated value counts
    assert RiskGate().decide(prop("restart_deployment", conf=0.99)).outcome == "ask_human"


def test_loop_limit_per_action_and_target_within_an_hour():
    clock = Clock()
    gate = RiskGate(clock=clock)
    outcomes = [gate.decide(prop("retry_job")).outcome for _ in range(4)]  # max_per_hour 3
    assert outcomes == ["execute", "execute", "execute", "reject"]
    assert gate.decide(prop("retry_job", target="other")).outcome == "execute"
    clock.now += timedelta(hours=1, seconds=1)
    assert gate.decide(prop("retry_job")).outcome == "execute"


def test_risk_score_follows_v1():
    assert risk_score(RiskTier.LOW, "staging", 1.0, True) == 0.15
    assert risk_score(RiskTier.MEDIUM, "prod", 0.6, True) == round(0.40 + 0.25 * 0.4 + 0.15, 3)
    assert risk_score(RiskTier.HIGH, "prod", 0.0, False) == 1.0  # 1.2 capped
    d = RiskGate().decide(prop("rollout_undo", agent="release"), 0.5)
    assert d.risk_score == round(0.70 + 0.25 * 0.5 + 0.10, 3)


def test_p3_keeps_the_more_conservative_proposal():
    restart = prop("restart_deployment", incident="i-9")  # reversible, medium
    undo = prop("rollout_undo", agent="release", incident="i-9")  # irreversible, high
    decisions = {d.proposal_id: d for d in RiskGate().resolve([restart, undo], {restart.id: 0.9})}
    assert decisions[restart.id].outcome == "execute"
    assert decisions[undo.id].outcome == "reject" and "conservative" in decisions[undo.id].reason


def test_p3_escalates_proposals_that_cannot_be_ranked():
    promote = prop("rollout_promote", agent="release", incident="i-8")
    abort = prop("rollout_abort", agent="release", incident="i-8")
    decisions = RiskGate().resolve([promote, abort], {promote.id: 1.0, abort.id: 1.0})
    assert {d.outcome for d in decisions} == {"ask_human"}


def test_p3_never_turns_a_rejection_into_a_question():
    bad = [prop("kubectl_exec", incident="i-7"), prop("delete_namespace", incident="i-7")]
    assert {d.outcome for d in RiskGate().resolve(bad)} == {"reject"}
