import pytest
from pydantic import ValidationError

from flowguard.contracts import (
    ActionProposal,
    AgentName,
    CanaryComparison,
    DiagnosisReport,
    Event,
    ExecutionResult,
    Finding,
    GateDecision,
    Incident,
    MetricSnapshot,
    PipelinePlan,
    ReleaseDecision,
    RiskTier,
    SecurityVerdict,
)


def make_proposal(**overrides) -> ActionProposal:
    data = {
        "id": "prop-1",
        "agent": "diagnose",
        "action": "pin_dependency",
        "target": "podinfo",
        "environment": "staging",
        "confidence": 0.93,
        "rationale": "invalid version pin",
    }
    data.update(overrides)
    return ActionProposal(**data)


def test_every_contract_builds_and_round_trips_through_json():
    objects = [
        Event(id="e1", type="ci_failure", service="podinfo", environment="staging"),
        Incident(
            id="inc-1",
            source="monitor",
            service="podinfo",
            environment="prod",
            anomaly_score=0.87,
            metrics=[
                MetricSnapshot(
                    name="latency_p95_ms",
                    value=1450,
                    baseline_mean=180,
                    baseline_std=40,
                    z_score=31.75,
                )
            ],
            detector="runtime",
        ),
        DiagnosisReport(
            incident_id="inc-1",
            root_cause="dependency",
            confidence=0.93,
            summary="bad pin",
            llm_model="stub",
            llm_tokens=2140,
            llm_cost_usd=0.002,
        ),
        ReleaseDecision(
            rollout_name="podinfo",
            environment="staging",
            canary_weight=20,
            comparisons=[
                CanaryComparison(
                    metric="error_rate",
                    canary_value=0.079,
                    stable_value=0.003,
                    p_value=0.0004,
                    degraded=True,
                )
            ],
            decision="rollback",
            confidence=0.96,
            rationale="error rate degraded",
        ),
        SecurityVerdict(
            commit_sha="abc123",
            findings=[
                Finding(tool="gitleaks", rule_id="aws-key", severity="critical", verified=True)
            ],
            hard_rule_triggered="verified_secret",
            decision="block",
            reasons=["secret"],
        ),
        PipelinePlan(
            commit_sha="abc123",
            job_order=["unit", "build", "scan"],
            skipped_jobs=["e2e"],
            predicted_total_s=190,
            baseline_total_s=400,
        ),
        make_proposal(),
        GateDecision(
            proposal_id="prop-1",
            tier=RiskTier.MEDIUM,
            risk_score=0.42,
            outcome="execute",
            reason="within policy",
        ),
        ExecutionResult(proposal_id="prop-1", success=True, output="ok", validated=True),
    ]
    for obj in objects:
        restored = type(obj).model_validate_json(obj.model_dump_json())
        assert restored == obj


def test_unknown_fields_are_rejected():
    # An LLM must not be able to smuggle its own risk tier into a proposal.
    with pytest.raises(ValidationError):
        make_proposal(tier="low")


@pytest.mark.parametrize("confidence", [-0.1, 1.01])
def test_confidence_is_bounded(confidence):
    with pytest.raises(ValidationError):
        make_proposal(confidence=confidence)


def test_root_cause_is_one_of_five_classes():
    with pytest.raises(ValidationError):
        DiagnosisReport(
            incident_id="i",
            root_cause="cosmic_rays",
            confidence=0.5,
            summary="?",
            llm_model="stub",
            llm_tokens=0,
            llm_cost_usd=0,
        )


def test_environment_is_staging_or_prod():
    with pytest.raises(ValidationError):
        make_proposal(environment="dev")


def test_agent_names_match_the_five_dsos():
    assert {a.value for a in AgentName} == {
        "monitor",
        "diagnose",
        "release",
        "security",
        "optimizer",
    }
