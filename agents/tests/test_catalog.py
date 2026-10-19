from flowguard.catalog import AGENT_ACTIONS, CATALOG, CRITICAL_JOBS, lookup
from flowguard.contracts import AgentName, RiskTier

# Tiers exactly as approved in blueprint section 8.
EXPECTED = {
    RiskTier.LOW: {
        "retry_job",
        "rerun_workflow",
        "rollout_set_weight",
        "block_deployment",
        "allow_deployment",
        "request_security_review",
        "reorder_jobs",
        "escalate_to_human",
    },
    RiskTier.MEDIUM: {
        "restart_deployment",
        "scale_deployment",
        "pin_dependency",
        "open_fix_pr",
        "rollout_promote",
        "rollout_abort",
        "skip_jobs",
    },
    RiskTier.HIGH: {"rollout_undo", "allow_with_findings"},
    RiskTier.PROHIBITED: {"delete_namespace", "delete_secret", "modify_rbac", "kubectl_exec"},
}


def test_catalog_tiers_match_the_blueprint():
    for tier, actions in EXPECTED.items():
        assert {a for a, e in CATALOG.items() if e.tier == tier} == actions


def test_unknown_action_is_not_in_catalog():
    assert lookup("rm_rf_everything") is None


def test_every_agent_action_exists_and_is_never_prohibited():
    for agent, actions in AGENT_ACTIONS.items():
        for action in actions:
            entry = lookup(action)
            assert entry is not None, f"{agent}: {action} missing from catalog"
            assert entry.tier != RiskTier.PROHIBITED


def test_every_agent_has_an_entry():
    assert set(AGENT_ACTIONS) == set(AgentName)
    assert AGENT_ACTIONS[AgentName.MONITOR] == frozenset()  # detection only


def test_prohibited_actions_have_no_allowance():
    for entry in CATALOG.values():
        if entry.tier == RiskTier.PROHIBITED:
            assert entry.max_per_hour == 0


def test_build_unit_and_scan_are_critical():
    assert {"build", "unit", "scan"} <= CRITICAL_JOBS
