"""Risk gate (DSO6): deterministic rules over the static catalog, first match wins.

The v1 rules (blueprint v1 section 8), with rule 0 added (D36):

    0. the proposing agent is not allowed the action (AGENT_ACTIONS) -> reject
    1. action not in the catalog                                      -> reject
    2. prohibited tier                                                -> reject
    3. loop limit exceeded (catalog max_per_hour per action + target) -> reject
    4. high tier                                                      -> ask a human
    5. medium tier in prod                                            -> ask a human
    6. medium tier with confidence below 0.70                         -> ask a human
    7. otherwise                                                      -> execute

P3 (D5): proposals for the same target in the same incident are resolved first; the more
conservative one (reversible before irreversible, then the lower tier) goes on to the rules,
the others are rejected; if the best two cannot be ranked, all of them go to a human.

The confidence is the calibrated one the orchestrator passes in (D9), never the proposal's own
`confidence` field. Tiers come only from the catalog. The risk score orders the approval queue
and never changes an outcome.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from flowguard.catalog import AGENT_ACTIONS, CATALOG, CatalogEntry
from flowguard.contracts import ActionProposal, GateDecision, RiskTier

THRESHOLD = 0.70  # rule 6 (swept at L5)
TIER_RANK = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2, RiskTier.PROHIBITED: 3}
TIER_BASE = {
    RiskTier.LOW: 0.15,
    RiskTier.MEDIUM: 0.40,
    RiskTier.HIGH: 0.70,
    RiskTier.PROHIBITED: 1.0,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def risk_score(tier: RiskTier, environment: str, confidence: float, reversible: bool) -> float:
    """Orders the approval queue only (v1): tier base + 0.25 x (1 - confidence) + 0.15 in prod
    + 0.10 if irreversible, capped at 1."""
    score = (
        TIER_BASE[tier]
        + 0.25 * (1.0 - confidence)
        + (0.15 if environment == "prod" else 0.0)
        + (0.0 if reversible else 0.10)
    )
    return round(min(1.0, score), 3)


class RiskGate:
    def __init__(
        self,
        threshold: float = THRESHOLD,
        catalog: dict[str, CatalogEntry] = CATALOG,
        agent_actions=AGENT_ACTIONS,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.threshold = threshold
        self.catalog = catalog
        self.agent_actions = agent_actions
        self.clock = clock
        self._passed: dict[tuple[str, str], deque] = defaultdict(deque)

    # ------------------------------------------------------------ rules

    def decide(self, proposal: ActionProposal, confidence: float = 0.0) -> GateDecision:
        entry = self.catalog.get(proposal.action)
        if proposal.action not in self.agent_actions.get(proposal.agent, frozenset()):
            reason = f"{proposal.agent} is not allowed {proposal.action}"
            return self._decision(proposal, entry, "reject", reason, 0, confidence)
        if entry is None:
            return self._decision(proposal, entry, "reject", "not in the catalog", 1, confidence)
        if entry.tier == RiskTier.PROHIBITED:
            return self._decision(proposal, entry, "reject", "prohibited tier", 2, confidence)
        recent = self._recent((proposal.action, proposal.target))
        if len(recent) >= entry.max_per_hour:
            reason = f"loop limit: {len(recent)} in the last hour (max {entry.max_per_hour})"
            return self._decision(proposal, entry, "reject", reason, 3, confidence)
        if entry.tier == RiskTier.HIGH:
            outcome, reason, rule = "ask_human", "high tier", 4
        elif entry.tier == RiskTier.MEDIUM and proposal.environment == "prod":
            outcome, reason, rule = "ask_human", "medium tier in prod", 5
        elif entry.tier == RiskTier.MEDIUM and confidence < self.threshold:
            reason = f"medium tier, calibrated confidence {confidence:.2f} < {self.threshold:.2f}"
            outcome, rule = "ask_human", 6
        else:
            outcome, reason, rule = "execute", f"{entry.tier} tier", 7
        recent.append(self.clock())
        return self._decision(proposal, entry, outcome, reason, rule, confidence)

    def _recent(self, key: tuple[str, str]) -> deque:
        recent = self._passed[key]
        horizon = self.clock() - timedelta(hours=1)
        while recent and recent[0] < horizon:
            recent.popleft()
        return recent

    def _decision(self, proposal, entry, outcome, reason, rule, confidence=0.0) -> GateDecision:
        tier = entry.tier if entry else RiskTier.PROHIBITED
        reversible = entry.reversible if entry else False
        return GateDecision(
            proposal_id=proposal.id,
            tier=tier,
            risk_score=risk_score(tier, proposal.environment, confidence, reversible),
            outcome=outcome,
            reason=f"rule {rule}: {reason}",
            decided_at=self.clock(),
        )

    # ------------------------------------------------------------ P3 conflicts

    def resolve(
        self, proposals: list[ActionProposal], confidences: dict[str, float] | None = None
    ) -> list[GateDecision]:
        """Decide a batch from one incident: conflicts on a target first (P3), then the rules."""
        confidences = confidences or {}
        by_target: dict[tuple, list[ActionProposal]] = defaultdict(list)
        for p in proposals:
            by_target[(p.incident_id, p.target)].append(p)
        decisions = []
        for candidates in by_target.values():
            # rules 0 to 2 reject on their own; P3 only ranks proposals that could run
            group = []
            for p in candidates:
                if self._rejected_outright(p):
                    decisions.append(self.decide(p, confidences.get(p.id, 0.0)))
                else:
                    group.append(p)
            if not group:
                continue
            if len({p.action for p in group}) == 1:
                decisions += [self.decide(p, confidences.get(p.id, 0.0)) for p in group]
                continue
            ranked = sorted(group, key=self._conservativeness)
            if self._conservativeness(ranked[0]) == self._conservativeness(ranked[1]):
                for p in group:
                    decisions.append(
                        self._decision(
                            p,
                            self.catalog.get(p.action),
                            "ask_human",
                            "conflicting proposals that cannot be ranked",
                            "P3",
                        )
                    )
                continue
            decisions.append(self.decide(ranked[0], confidences.get(ranked[0].id, 0.0)))
            for p in ranked[1:]:
                decisions.append(
                    self._decision(
                        p,
                        self.catalog.get(p.action),
                        "reject",
                        f"conflict: the more conservative {ranked[0].action} is kept",
                        "P3",
                    )
                )
        return decisions

    def _rejected_outright(self, p: ActionProposal) -> bool:
        entry = self.catalog.get(p.action)
        return (
            p.action not in self.agent_actions.get(p.agent, frozenset())
            or entry is None
            or entry.tier == RiskTier.PROHIBITED
        )

    def _conservativeness(self, p: ActionProposal) -> tuple[int, int]:
        """Smaller is more conservative: reversible first, then the lower tier."""
        entry = self.catalog.get(p.action)
        if entry is None:
            return (1, TIER_RANK[RiskTier.PROHIBITED])
        return (0 if entry.reversible else 1, TIER_RANK[entry.tier])
