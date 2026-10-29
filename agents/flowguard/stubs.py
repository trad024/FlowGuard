"""Stub agents (Step 5): deterministic stand-ins with the real agents' interfaces.

Each agent gets an AgentInput (only what a real system would show it: logs, metrics, scan
counts, changed files, evidence from the incident record) and returns proposals, label scores
for calibration (D9) and evidence posts. Steps 6 to 10 replace the logic, not the interface.

- Monitor: the real agent since Step 6 (`flowguard.monitor`), re-exported here so the
  orchestrator's Monitor slot uses it.
- Diagnose: L0 the frozen regex classifier and playbook (D3); L2+ an injected LLM; with
  grounding (L3+) quoted lines that are not in the log are dropped (D8).
- Release: the real agent since Step 8 (`flowguard.release`), re-exported here: a sequential
  test on canary vs stable counts; with collaboration (L4+) P1 before any rollback.
- Security: block on any HIGH, CRITICAL or leaked secret, else allow.
- Optimizer: skip optional jobs a docs-only change cannot affect; P2 forbids skipping the
  dependency and security jobs when dependency files changed or Security flagged the last run.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable

from flowguard import baselines
from flowguard.agent_io import AgentInput, AgentOutput
from flowguard.contracts import ActionProposal, AgentName, EvidencePost
from flowguard.monitor import Monitor
from flowguard.release import Release

__all__ = ["AgentInput", "AgentOutput", "Diagnose", "Monitor", "Optimizer", "Release", "Security"]

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
FORCED_JOBS = ("sbom", "scans")  # P2: never skipped after a dependency change or a flag


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


class Security:
    name = AgentName.SECURITY

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        if cfg.ml:
            from flowguard.security import Security as ScoredSecurity

            return ScoredSecurity().run(inp, cfg)
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
