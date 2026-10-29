"""Stub agents (Step 5): deterministic stand-ins with the real agents' interfaces.

Each agent gets an AgentInput (only what a real system would show it: logs, metrics, scan
counts, changed files, evidence from the incident record) and returns proposals, label scores
for calibration (D9) and evidence posts. Steps 6 to 10 replace the logic, not the interface.

- Monitor: the real agent since Step 6 (`flowguard.monitor`), re-exported here so the
  orchestrator's Monitor slot uses it.
- Diagnose: one method per level, each required, never a silent fallback: L0 the frozen regex
  classifier and playbook (D3); L1 TF-IDF + logistic regression (flowguard.classical); L2 an
  LLM, zero-shot; L3+ the LLM with the 3 nearest training cases of other templates
  (flowguard.retrieval) and grounding (D8). `missing()` names what a level lacks, so the
  orchestrator refuses to start without it; a log reaching a Diagnose without its method raises.
  Every proposal records the method used (`diagnose_method`), the class it decided
  (`diagnose_root_cause`) and, from an LLM, its fix sentence (`diagnose_fix`). Without a log (empty
  or absent) every level takes one path: no model is asked, no class is guessed, the incident
  record gets "no diagnosis (no log)" and the incident goes to a human (escalate_to_human, no
  label scores) (D60).
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

from flowguard import baselines, diagnose
from flowguard.agent_io import AgentInput, AgentOutput
from flowguard.contracts import ActionProposal, AgentName, EvidencePost
from flowguard.monitor import Monitor
from flowguard.release import Release

__all__ = ["AgentInput", "AgentOutput", "Diagnose", "Monitor", "Optimizer", "Release", "Security"]

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
NO_LOG = "no diagnosis (no log)"  # D60
METHODS = ("regex", "tfidf_lr", "llm_zero_shot", "llm_retrieval", "encoder", "no_log")


class MethodUnavailable(RuntimeError):
    """A log reached a Diagnose that lacks its level's method (the orchestrator's check is the
    first guard; this is the second)."""


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

    def __init__(
        self,
        llm: Callable[..., dict] | None = None,
        l1=None,  # a fitted flowguard.classical.TfidfDiagnoser
        retriever=None,  # a flowguard.retrieval.Retriever over the training cases
        logs: bool = True,
    ):
        self.llm = llm
        self.l1 = l1
        self.retriever = retriever
        self.logs = logs  # False: built for runs that never send it a log (no_log only)

    @classmethod
    def without_logs(cls) -> Diagnose:
        """For tools whose events never carry a log (D60 answers them all)."""
        return cls(logs=False)

    def method(self, cfg) -> str:
        if not cfg.ml:
            return "regex"
        if cfg.llm:
            return "llm_retrieval" if cfg.retrieval else "llm_zero_shot"
        return "tfidf_lr"

    def missing(self, cfg) -> str | None:
        """What this level's method needs and this Diagnose lacks, or None."""
        if not self.logs:
            return None
        m = self.method(cfg)
        if m == "tfidf_lr" and self.l1 is None:
            return (
                f"{cfg.level}: Diagnose needs the TF-IDF + LR model (state/models/diagnose-l1.pkl)"
            )
        if m.startswith("llm") and self.llm is None:
            return f"{cfg.level}: Diagnose needs an LLM client"
        if m == "llm_retrieval" and self.retriever is None:
            return f"{cfg.level}: Diagnose needs the retrieval index (state/chroma/)"
        return None

    def classify(self, inp: AgentInput, cfg) -> tuple[str, dict[str, float], list[int], str]:
        """(class, label scores, evidence lines, the action the LLM proposed or "")."""
        if not self.logs:
            raise MethodUnavailable(f"{cfg.level}: this Diagnose was built without log diagnosis")
        if problem := self.missing(cfg):
            raise MethodUnavailable(problem)
        m = self.method(cfg)
        if m == "tfidf_lr":
            scores = self.l1.scores(inp.log)
            return max(scores, key=scores.get), scores, [], ""
        if m.startswith("llm"):
            cls, scores, lines, action, _ = self._ask_llm(inp, cfg)
            return cls, scores, lines, action
        cls, lines = baselines.classify(inp.log)
        scores = {c: (1.0 if c == cls else 0.0) for c in CLASSES}
        return cls, scores, lines, ""

    def _ask_llm(self, inp: AgentInput, cfg):
        """The LLM's answer, grounded by level (D8: on from L3, whatever retrieval returned),
        with the model's raw quotes kept for the record: the hallucination rate is counted on
        them at L2 and L3 alike, grounding changes only the decision."""
        if cfg.retrieval:
            answer = self.llm(inp.log, self.retriever.nearest(inp.log))
        else:
            answer = self.llm(inp.log)
        cls = answer.get("root_cause", "unclear")
        quotes = [str(q) for q in answer.get("quotes", [])]
        lines = [int(n) for n in answer.get("evidence_lines", []) if str(n).isdigit()]
        lines = [n for n in lines if 1 <= n <= len(inp.log.splitlines())]
        ungrounded = int(answer.get("ungrounded", 0))
        scores = answer.get("scores") or {c: (0.9 if c == cls else 0.025) for c in CLASSES}
        action = str(answer.get("action") or "")
        rejected = cfg.grounding and diagnose.rejected_by_grounding(quotes, ungrounded)
        fix = str(answer.get("fix") or "")
        if rejected:  # nothing of a rejected answer reaches an action (D8)
            cls, action, lines, fix = "unclear", "escalate_to_human", [], ""
        record = {
            "diagnose_quotes": quotes,  # raw, as the model wrote them
            "diagnose_ungrounded": ungrounded,  # raw quotes not found in the log
            "diagnose_grounding": bool(cfg.grounding),
            "diagnose_rejected": bool(rejected),
            "diagnose_fix": fix,  # the model's one-sentence fix, for the fix planner (D57)
        }
        return cls, scores, lines, action, record

    def run(self, inp: AgentInput, cfg) -> AgentOutput:
        if not inp.log.strip():  # D60: the same at every level, and no LLM call
            post = EvidencePost(incident_id=inp.incident_id, agent=self.name, claim=NO_LOG)
            p = proposal(self.name, "escalate_to_human", inp, NO_LOG, diagnose_method="no_log")
            return AgentOutput(proposals=[p], evidence=[post])
        method = self.method(cfg)
        record = {}
        if method.startswith("llm") and not self.missing(cfg) and self.logs:
            cls, scores, lines, proposed, record = self._ask_llm(inp, cfg)
        else:
            cls, scores, lines, proposed = self.classify(inp, cfg)
        # An LLM may propose any action; the gate, not the agent, decides what may run (D20)
        action = proposed or baselines.playbook(cls if cls in CLASSES else "unclear")
        p = proposal(
            self.name,
            action,
            inp,
            f"root cause {cls}; evidence lines {lines[:5]}",
            diagnose_method=method,
            diagnose_root_cause=cls,  # the class decided (after grounding), for the fix planner
            **record,
        )
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
