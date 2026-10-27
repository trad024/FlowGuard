"""Orchestrator (Step 5): one LangGraph graph for workflows A to D, configured by ablation level.

    event -> open incident -> agent -> [Monitor found an incident: Release if mid-canary, else
    Diagnose] -> gate -> human approval (ask_human only) -> executor -> audit log

Levels (D10) are configuration flags of the same code:
    L0  rule baselines, no gate (DSO6's baseline): dry-run executor only
    L1  + statistical / ML detectors        L2  + LLM diagnosis (zero-shot)
    L3  + retrieval and evidence grounding  L4  + collaboration protocols P1 to P3
    L5  L4 with the gate threshold swept (autonomy vs safety, H5)

Every proposal, gate decision, approval and execution goes to the audit log; incidents and
evidence go to the shared incident record. `input_tap(agent, AgentInput)` sees every input any
agent receives, for the agent-input leakage test (D25).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from flowguard.calibration import ConformalCalibrator
from flowguard.catalog import CATALOG
from flowguard.contracts import (
    AblationLevel,
    AgentName,
    AgentRequest,
    Event,
    EvidencePost,
    ExecutionResult,
    GateDecision,
    Incident,
    RiskTier,
)
from flowguard.executor import Executor, allowed
from flowguard.gate import THRESHOLD, RiskGate, risk_score
from flowguard.incidents import IncidentRecord
from flowguard.stubs import AgentInput, AgentOutput, Diagnose, Monitor, Optimizer, Release, Security


@dataclass(frozen=True)
class LevelConfig:
    level: AblationLevel
    gate: bool = True
    ml: bool = False
    llm: bool = False
    retrieval: bool = False
    grounding: bool = False
    collaboration: bool = False
    threshold: float = THRESHOLD


LEVELS = {
    "L0": LevelConfig("L0", gate=False),
    "L1": LevelConfig("L1", ml=True),
    "L2": LevelConfig("L2", ml=True, llm=True),
    "L3": LevelConfig("L3", ml=True, llm=True, retrieval=True, grounding=True),
    "L4": LevelConfig("L4", ml=True, llm=True, retrieval=True, grounding=True, collaboration=True),
    "L5": LevelConfig("L5", ml=True, llm=True, retrieval=True, grounding=True, collaboration=True),
}

ROUTES = {
    "ci_failure": AgentName.DIAGNOSE,  # B
    "scan_completed": AgentName.SECURITY,  # A
    "pipeline_started": AgentName.OPTIMIZER,  # A
    "rollout_step": AgentName.RELEASE,  # D
    "metrics_tick": AgentName.MONITOR,  # C
}
SOURCES = {
    "ci_failure": "ci_failure",
    "metrics_tick": "monitor",
    "rollout_step": "rollout",
    "scan_completed": "ci_failure",
    "pipeline_started": "ci_failure",
}


def level(name: str, threshold: float | None = None) -> LevelConfig:
    cfg = LEVELS[name]
    if threshold is not None:
        cfg = LevelConfig(**{**cfg.__dict__, "threshold": threshold})
    return cfg


class State(TypedDict, total=False):
    event: Event
    incident_id: str
    agent: AgentName
    inp: AgentInput
    output: AgentOutput
    decisions: list[GateDecision]
    results: list[ExecutionResult]


@dataclass
class Orchestrator:
    cfg: LevelConfig
    record: IncidentRecord
    executor: Executor
    audit: object | None = None
    approver: object | None = None  # SimulatedApprover or ApprovalPage
    calibrators: dict[AgentName, ConformalCalibrator] = field(default_factory=dict)
    diagnose_llm: Callable[[str], dict] | None = None
    input_tap: Callable[[AgentName, AgentInput], None] | None = None

    def __post_init__(self):
        if not self.cfg.gate and not self.executor.dry_run:
            raise ValueError("L0 runs without a gate: only with a dry-run executor")
        self.gate = RiskGate(threshold=self.cfg.threshold)
        self.agents = {
            AgentName.MONITOR: Monitor(),
            AgentName.DIAGNOSE: Diagnose(self.diagnose_llm),
            AgentName.RELEASE: Release(),
            AgentName.SECURITY: Security(),
            AgentName.OPTIMIZER: Optimizer(),
        }
        graph = StateGraph(State)
        graph.add_node("open_incident", self._open_incident)
        graph.add_node("run_agent", self._run_agent)
        graph.add_node("follow_up", self._follow_up)
        graph.add_node("gate", self._gate)
        graph.add_node("approve", self._approve)
        graph.add_node("execute", self._execute)
        graph.add_edge(START, "open_incident")
        graph.add_edge("open_incident", "run_agent")
        graph.add_conditional_edges(
            "run_agent",
            lambda s: (
                "follow_up" if s["agent"] == AgentName.MONITOR and s["output"].incident else "gate"
            ),
            {"follow_up": "follow_up", "gate": "gate"},
        )
        graph.add_edge("follow_up", "gate")
        graph.add_edge("gate", "approve")
        graph.add_edge("approve", "execute")
        graph.add_edge("execute", END)
        self.graph = graph.compile()

    def handle(self, event: Event) -> State:
        return self.graph.invoke({"event": event})

    # ------------------------------------------------------------ nodes

    def _inputs(self, event: Event, incident_id: str) -> AgentInput:
        p = event.payload
        return AgentInput(
            incident_id=incident_id,
            environment=event.environment,
            target=p.get("target", event.service),
            log=p.get("log", ""),
            metrics=dict(p.get("metrics", {})),
            alerts=list(p.get("alerts", [])),
            scan_counts=dict(p.get("scan_counts", {})),
            changed_files=list(p.get("changed_files", [])),
            params=dict(p.get("params", {})),
            evidence=self.record.evidence(incident_id),
        )

    def _open_incident(self, state: State) -> State:
        event = state["event"]
        incident_id = f"i-{secrets.token_hex(5)}"
        self.record.open(
            Incident(
                id=incident_id,
                source=SOURCES[event.type],
                service=event.service,
                environment=event.environment,
                in_canary=bool(event.payload.get("in_canary")),
                ci_run_id=event.payload.get("params", {}).get("run_id"),
            )
        )
        return {"incident_id": incident_id, "agent": ROUTES[event.type]}

    def _call(self, agent: AgentName, inp: AgentInput) -> AgentOutput:
        if self.input_tap is not None:
            self.input_tap(agent, inp)
        impl = self.agents[agent]
        if agent == AgentName.RELEASE:
            return impl.run(inp, self.cfg, ask=lambda to, q: self._ask(inp, to, q))
        return impl.run(inp, self.cfg)

    def _ask(self, inp: AgentInput, to: AgentName, question: str) -> EvidencePost:
        """P1: Release asks another agent; the answer is evidence, never an action."""
        self.record.ask(
            AgentRequest(
                incident_id=inp.incident_id,
                from_agent=AgentName.RELEASE,
                to_agent=to,
                question=question,
            )
        )
        if self.input_tap is not None:
            self.input_tap(to, inp)
        if to == AgentName.MONITOR:
            post = self.agents[to].answer(inp)
        else:
            out = self.agents[to].run(inp, self.cfg)
            post = out.evidence[0]
        self.record.post(post)
        return post

    def _run_agent(self, state: State) -> State:
        inp = self._inputs(state["event"], state["incident_id"])
        out = self._call(state["agent"], inp)
        for post in out.evidence:
            self.record.post(post)
        return {"inp": inp, "output": out}

    def _follow_up(self, state: State) -> State:
        """Workflow C: Monitor opened an incident; Release if mid-canary, else Diagnose."""
        agent = AgentName.RELEASE if state["event"].payload.get("in_canary") else AgentName.DIAGNOSE
        out = self._call(agent, state["inp"])
        for post in out.evidence:
            self.record.post(post)
        return {"agent": agent, "output": out}

    def _gate(self, state: State) -> State:
        out = state["output"]
        for p in out.proposals:
            if self.audit is not None:
                self.audit.proposal(p)
        if not self.cfg.gate:  # L0: no gate, everything runs (dry run only, see __post_init__)
            decisions = []
            for p in out.proposals:
                entry = CATALOG.get(p.action)
                tier = entry.tier if entry else RiskTier.PROHIBITED
                reversible = entry.reversible if entry else False
                decisions.append(
                    GateDecision(
                        proposal_id=p.id,
                        tier=tier,
                        risk_score=risk_score(tier, p.environment, 0.0, reversible),
                        outcome="execute",
                        reason="L0: no gate",
                    )
                )
        else:
            confidences = {
                p.id: self.calibrators.get(p.agent, ConformalCalibrator()).confidence(
                    out.label_scores.get(p.id, {})
                )[1]
                for p in out.proposals
            }
            decisions = self.gate.resolve(out.proposals, confidences)
        if self.audit is not None:
            for d in decisions:
                self.audit.gate_decision(d)
        return {"decisions": decisions}

    def _approve(self, state: State) -> State:
        by_id = {p.id: p for p in state["output"].proposals}
        decisions = []
        for d in state["decisions"]:
            if d.outcome == "ask_human" and self.approver is not None:
                d = self.approver.review(by_id[d.proposal_id], d)
                if self.audit is not None:
                    self.audit.approval(d.proposal_id, d.human_verdict, d.approved_by)
            decisions.append(d)
        return {"decisions": decisions}

    def _execute(self, state: State) -> State:
        by_id = {p.id: p for p in state["output"].proposals}
        results = [
            self.executor.execute(by_id[d.proposal_id], d) for d in state["decisions"] if allowed(d)
        ]
        status = (
            "escalated"
            if any(d.outcome == "ask_human" for d in state["decisions"])
            else ("resolved" if results and all(r.success for r in results) else "open")
        )
        self.record.set_status(state["incident_id"], status)
        return {"results": results}
