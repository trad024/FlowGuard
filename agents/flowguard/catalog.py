"""Static action catalog (blueprint section 8).

The risk tier of every action is fixed here by humans, in code review.
No agent and no LLM output can change it. Anything not listed is prohibited.
"""

from dataclasses import dataclass

from flowguard.contracts import AgentName, RiskTier


@dataclass(frozen=True)
class CatalogEntry:
    action: str
    tier: RiskTier
    reversible: bool
    max_per_hour: int  # loop protection, counted per (action, target)
    validate_by_rerun: bool = False


L, M, H, P = RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH, RiskTier.PROHIBITED

_ENTRIES = (
    # low: execute
    CatalogEntry("retry_job", L, True, 3, validate_by_rerun=True),
    CatalogEntry("rerun_workflow", L, True, 3, validate_by_rerun=True),
    CatalogEntry("rollout_set_weight", L, True, 10),
    CatalogEntry("block_deployment", L, True, 50),
    CatalogEntry("allow_deployment", L, True, 50),
    CatalogEntry("request_security_review", L, True, 50),
    CatalogEntry("reorder_jobs", L, True, 50),
    CatalogEntry("escalate_to_human", L, True, 50),
    # medium: execute in staging at confidence >= 0.70, otherwise ask a human
    CatalogEntry("restart_deployment", M, True, 3),
    CatalogEntry("scale_deployment", M, True, 3),
    CatalogEntry("pin_dependency", M, True, 3, validate_by_rerun=True),
    CatalogEntry("open_fix_pr", M, True, 3),
    CatalogEntry("rollout_promote", M, True, 10),
    CatalogEntry("rollout_abort", M, True, 5),
    CatalogEntry("skip_jobs", M, True, 50),
    # high: always ask a human
    CatalogEntry("rollout_undo", H, False, 2),
    CatalogEntry("allow_with_findings", H, False, 10),
    # prohibited: always reject
    CatalogEntry("delete_namespace", P, False, 0),
    CatalogEntry("delete_secret", P, False, 0),
    CatalogEntry("modify_rbac", P, False, 0),
    CatalogEntry("kubectl_exec", P, False, 0),
)

CATALOG: dict[str, CatalogEntry] = {e.action: e for e in _ENTRIES}

# Which actions each agent may propose (blueprint section 7).
AGENT_ACTIONS: dict[AgentName, frozenset[str]] = {
    AgentName.MONITOR: frozenset(),
    AgentName.DIAGNOSE: frozenset(
        {
            "retry_job",
            "rerun_workflow",
            "restart_deployment",
            "pin_dependency",
            "open_fix_pr",
            "escalate_to_human",
        }
    ),
    AgentName.RELEASE: frozenset(
        {
            "rollout_promote",
            "rollout_set_weight",
            "rollout_abort",
            "rollout_undo",
            "escalate_to_human",
        }
    ),
    AgentName.SECURITY: frozenset(
        {"block_deployment", "request_security_review", "allow_deployment", "allow_with_findings"}
    ),
    AgentName.OPTIMIZER: frozenset({"reorder_jobs", "skip_jobs"}),
}

# CI jobs the Optimizer may never skip (DSO5).
CRITICAL_JOBS: frozenset[str] = frozenset({"build", "unit", "scan"})


def lookup(action: str) -> CatalogEntry | None:
    """Return the catalog entry, or None (the gate treats None as prohibited)."""
    return CATALOG.get(action)
