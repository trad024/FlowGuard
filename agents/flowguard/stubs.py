"""Stub agents (Step 5): deterministic stand-ins with the real agents' interfaces.

Each agent gets an AgentInput (only what a real system would show it: logs, metrics, scan
counts, changed files, evidence from the incident record) and returns proposals, label scores
for calibration (D9) and evidence posts. Steps 6 to 10 replace the logic, not the interface.

- Monitor: an incident when an L0 alert fired or (L1+) a z-score above 3; answers P1 requests
  with node-stress evidence.
- Diagnose: L0 the frozen regex classifier and playbook (D3); L2+ an injected LLM; with
  grounding (L3+) quoted lines that are not in the log are dropped (D8).
- Release: abort a worse canary; with collaboration (L4+) it first asks Monitor and Diagnose
  (P1) and escalates instead when the evidence says infrastructure.
- Security: block on any HIGH, CRITICAL or leaked secret, else allow.
- Optimizer: skip optional jobs a docs-only change cannot affect; P2 forbids skipping the
  dependency and security jobs when dependency files changed or Security flagged the last run.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from flowguard import baselines
from flowguard.contracts import ActionProposal, AgentName, EvidencePost

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
FORCED_JOBS = ("sbom", "scans")  # P2: never skipped after a dependency change or a flag


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


def proposal(agent: AgentName, action: str, inp: AgentInput, rationale: str, **params):
    return ActionProposal(
        id=f"p-{secrets.token_hex(5)}",
        incident_id=inp.incident_id,
        agent=agent,
        action=action,
        target=inp.target,
        environment=inp.environment,
        params={**inp.params, **params},
        confidence=0.0,  # never used by the gate (D9)
        rationale=rationale[:500],
    )


class Monitor:
    name = AgentName.MONITOR

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        z = max((abs(v) for k, v in inp.metrics.items() if k.endswith("_z")), default=0.0)
        incident = bool(inp.alerts) or (cfg.ml and z > 3.0)
        return AgentOutput(incident=incident)

    def answer(self, inp: AgentInput) -> EvidencePost:
        cpu = float(inp.metrics.get("node_cpu_ratio", 0.0))
        claim = "node under CPU stress" if cpu > 0.85 else "node resources normal"
        return EvidencePost(
            incident_id=inp.incident_id, agent=self.name, claim=claim, refs=["node_cpu_ratio"]
        )


class Diagnose:
    name = AgentName.DIAGNOSE

    def __init__(self, llm: Callable[[str], dict] | None = None):
        self.llm = llm

    def classify(self, inp: AgentInput, cfg) -> tuple[str, dict[str, float], list[int], str]:
        """(class, label scores, evidence lines, the action the LLM proposed or "")."""
        if cfg.llm and self.llm is not None:
            answer = self.llm(inp.log)
            cls = answer.get("root_cause", "unclear")
            lines = [int(n) for n in answer.get("evidence_lines", []) if str(n).isdigit()]
            if cfg.grounding:  # D8: only lines that exist in the real log count as evidence
                lines = [n for n in lines if 1 <= n <= len(inp.log.splitlines())]
            scores = answer.get("scores") or {c: (0.9 if c == cls else 0.025) for c in CLASSES}
            return cls, scores, lines, str(answer.get("action") or "")
        cls, lines = baselines.classify(inp.log)
        scores = {c: (1.0 if c == cls else 0.0) for c in CLASSES}
        return cls, scores, lines, ""

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        cls, scores, lines, proposed = self.classify(inp, cfg)
        # An LLM may propose any action; the gate, not the agent, decides what may run (D20)
        action = proposed or baselines.playbook(cls if cls in CLASSES else "unclear")
        p = proposal(self.name, action, inp, f"root cause {cls}; evidence lines {lines[:5]}")
        cause = "infrastructure" if cls == "infrastructure" else "release"
        post = EvidencePost(
            incident_id=inp.incident_id,
            agent=self.name,
            claim=f"failure looks {cause}-caused",
            refs=[f"line {n}" for n in lines[:5]],
        )
        return AgentOutput(proposals=[p], label_scores={p.id: scores}, evidence=[post])


class Release:
    name = AgentName.RELEASE

    def run(
        self, inp: AgentInput, cfg, ask: Callable[[AgentName, str], EvidencePost] | None = None
    ):
        ratio_canary = float(inp.metrics.get("canary_5xx_ratio", 0.0))
        ratio_stable = float(inp.metrics.get("stable_5xx_ratio", 0.0))
        worse = ratio_canary > max(0.01, 2 * ratio_stable)
        if not worse:
            p = proposal(self.name, "rollout_promote", inp, "canary not worse than stable")
            return AgentOutput(proposals=[p], label_scores={p.id: {"promote": 0.8, "abort": 0.2}})
        evidence = []
        if cfg.collaboration and ask is not None:  # P1: ask before rolling back
            evidence = [
                ask(AgentName.MONITOR, "Is the node or cluster under stress?"),
                ask(AgentName.DIAGNOSE, "Does the error look release- or infrastructure-caused?"),
            ]
            if any(
                "stress" in e.claim and "normal" not in e.claim or "infrastructure" in e.claim
                for e in evidence
            ):
                p = proposal(
                    self.name, "escalate_to_human", inp, "worse canary, but infrastructure evidence"
                )
                return AgentOutput(proposals=[p], evidence=evidence)
        p = proposal(
            self.name, "rollout_abort", inp, f"canary 5xx {ratio_canary:.3f} vs {ratio_stable:.3f}"
        )
        return AgentOutput(
            proposals=[p], label_scores={p.id: {"abort": 0.8, "promote": 0.2}}, evidence=evidence
        )


class Security:
    name = AgentName.SECURITY

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        c = inp.scan_counts
        bad = c.get("trivy_high", 0) + c.get("trivy_critical", 0) + c.get("gitleaks", 0)
        action = "block_deployment" if bad else "allow_deployment"
        p = proposal(self.name, action, inp, f"scan counts {json.dumps(c, sort_keys=True)}")
        return AgentOutput(
            proposals=[p], label_scores={p.id: {"block": float(bad > 0), "allow": float(bad == 0)}}
        )


class Optimizer:
    name = AgentName.OPTIMIZER

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        app = [f for f in inp.changed_files if f.startswith("app/")]
        if app:
            return AgentOutput()
        skip = ["lint", "integration", "sbom", "multiarch"]
        deps = any(f.endswith(("go.mod", "go.sum")) for f in inp.changed_files)
        if deps or inp.params.get("security_flagged"):
            skip = [j for j in skip if j not in FORCED_JOBS]  # P2
        p = proposal(self.name, "skip_jobs", inp, "no application file changed", jobs=skip)
        return AgentOutput(proposals=[p])
