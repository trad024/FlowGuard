"""What every agent receives and returns (Step 5 interface, shared by the stubs and the real
agents of Steps 6 to 10)."""

from __future__ import annotations

from dataclasses import dataclass, field

from flowguard.contracts import ActionProposal, EvidencePost


@dataclass
class AgentInput:
    incident_id: str
    environment: str = "staging"
    target: str = "podinfo"
    log: str = ""
    metrics: dict = field(default_factory=dict)  # name -> value, as Prometheus shows them
    alerts: list[str] = field(default_factory=list)
    scan_counts: dict = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)  # run_id, job_id, ...
    evidence: list[EvidencePost] = field(default_factory=list)


@dataclass
class AgentOutput:
    proposals: list[ActionProposal] = field(default_factory=list)
    label_scores: dict[str, dict[str, float]] = field(default_factory=dict)  # proposal id -> scores
    evidence: list[EvidencePost] = field(default_factory=list)
    incident: bool = False
    # Monitor only (D49): which detector reported the incident, and its score in [0, 1]
    detector: str | None = None  # "alert_rule", "runtime" or "ci"
    anomaly_score: float | None = None
