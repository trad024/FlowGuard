"""Shared data contracts (blueprint section 8).

Every component exchanges these objects: event sources, the orchestrator,
the five agents, the risk gate, the executor and the audit log. Unknown
fields are rejected so a typo or an unexpected LLM field fails loudly.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Environment = Literal["staging", "prod"]
Mode = Literal["agents", "baseline"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Contract(BaseModel):
    """Base for every contract: strict about unknown fields."""

    model_config = ConfigDict(extra="forbid")


class RiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    PROHIBITED = "prohibited"


class AgentName(StrEnum):
    MONITOR = "monitor"  # DSO1
    DIAGNOSE = "diagnose"  # DSO2
    RELEASE = "release"  # DSO3
    SECURITY = "security"  # DSO4
    OPTIMIZER = "optimizer"  # DSO5


Confidence = Field(ge=0.0, le=1.0)


# ---------------------------------------------------------------- Events

EventType = Literal[
    "metrics_tick",  # Prometheus poll or Alertmanager alert
    "ci_failure",  # GitHub Actions run failed
    "rollout_step",  # Argo Rollouts canary step began
    "scan_completed",  # CI scan job finished
    "pipeline_started",  # first "plan" job of a CI run
]


class Event(Contract):
    id: str
    type: EventType
    service: str
    environment: Environment
    payload: dict = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- DSO1 Monitor


class MetricSnapshot(Contract):
    name: str  # e.g. "error_rate", "latency_p95_ms", "build_duration_s"
    value: float
    baseline_mean: float
    baseline_std: float
    z_score: float


class Incident(Contract):
    id: str
    source: Literal["monitor", "ci_failure", "alertmanager", "rollout"]
    service: str
    environment: Environment
    detected_at: datetime = Field(default_factory=utcnow)
    anomaly_score: float | None = Field(default=None, ge=0.0, le=1.0)
    metrics: list[MetricSnapshot] = Field(default_factory=list)
    detector: Literal["runtime", "ci", "alert_rule"] | None = None
    ci_run_id: int | None = None
    commit_sha: str | None = None
    in_canary: bool = False


# ---------------------------------------------------------------- DSO2 Diagnose

RootCause = Literal["code", "dependency", "environment", "flaky_test", "infrastructure"]


class DiagnosisReport(Contract):
    incident_id: str
    root_cause: RootCause
    confidence: float = Confidence
    summary: str
    evidence: list[str] = Field(default_factory=list)
    similar_cases: list[str] = Field(default_factory=list)
    llm_model: str
    llm_tokens: int = Field(ge=0)
    llm_cost_usd: float = Field(ge=0.0)


# ---------------------------------------------------------------- DSO3 Release


class CanaryComparison(Contract):
    metric: str
    canary_value: float
    stable_value: float
    p_value: float = Field(ge=0.0, le=1.0)
    degraded: bool


class ReleaseDecision(Contract):
    rollout_name: str
    environment: Environment
    canary_weight: int = Field(ge=0, le=100)
    comparisons: list[CanaryComparison]
    decision: Literal["continue", "canary", "rollback", "escalate"]
    confidence: float = Confidence
    rationale: str


# ---------------------------------------------------------------- DSO4 Security


class Finding(Contract):
    tool: Literal["sonarcloud", "trivy", "gitleaks", "tests"]
    rule_id: str
    severity: Literal["info", "low", "medium", "high", "critical"]
    location: str = ""
    verified: bool = False  # gitleaks: secret confirmed real
    fix_available: bool | None = None
    runtime_dependency: bool | None = None


class SecurityVerdict(Contract):
    commit_sha: str
    findings: list[Finding]
    hard_rule_triggered: (
        Literal["verified_secret", "critical_cve_with_fix", "quality_gate_failed"] | None
    ) = None
    decision: Literal["allow", "review", "block"]
    classifier_score: float | None = Field(default=None, ge=0.0, le=1.0)
    reasons: list[str]


# ---------------------------------------------------------------- DSO5 Optimizer


class PipelinePlan(Contract):
    commit_sha: str
    job_order: list[str]
    skipped_jobs: list[str] = Field(default_factory=list)
    predicted_total_s: float = Field(ge=0.0)
    baseline_total_s: float = Field(ge=0.0)


# ---------------------------------------------------------------- DSO6 Risk gate


class ActionProposal(Contract):
    """What an agent asks for. The risk tier is NOT here: it comes from the catalog."""

    id: str
    incident_id: str | None = None
    agent: AgentName
    action: str  # must exist in the action catalog
    target: str  # what the action touches (deployment, rollout, commit)
    params: dict = Field(default_factory=dict)
    environment: Environment
    confidence: float = Confidence
    rationale: str
    proposed_at: datetime = Field(default_factory=utcnow)


class GateDecision(Contract):
    proposal_id: str
    tier: RiskTier
    risk_score: float = Field(ge=0.0, le=1.0)
    outcome: Literal["execute", "ask_human", "reject"]
    reason: str
    human_verdict: Literal["approved", "denied", "timeout"] | None = None
    approved_by: str | None = None
    decided_at: datetime = Field(default_factory=utcnow)


class ExecutionResult(Contract):
    proposal_id: str
    success: bool
    output: str
    validated: bool | None = None  # e.g. pipeline rerun green; None = not applicable
    finished_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------- Collaboration (D5, Step 5)

AblationLevel = Literal["L0", "L1", "L2", "L3", "L4", "L5"]


class EvidencePost(Contract):
    """A claim an agent posts to the shared incident record, with what supports it."""

    incident_id: str
    agent: AgentName
    claim: str
    refs: list[str] = Field(default_factory=list)  # metric names, log line numbers, run ids
    posted_at: datetime = Field(default_factory=utcnow)


class AgentRequest(Contract):
    """One agent asks another for evidence (P1: Release asks Monitor and Diagnose)."""

    incident_id: str
    from_agent: AgentName
    to_agent: AgentName
    question: str
    asked_at: datetime = Field(default_factory=utcnow)
