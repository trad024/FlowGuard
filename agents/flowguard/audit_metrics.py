"""Metrics computed from the audit log (Step 4a). The evaluation (Step 11) builds on these.

python -m flowguard.audit_metrics            # summary of the whole log, as JSON
python -m flowguard.audit_metrics --scenario dev-001
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter

from flowguard.audit import AuditLog


def _filter(scenario_id: str | None, environment: str | None) -> tuple[str, list]:
    sql, args = "", []
    if scenario_id:
        sql, args = sql + " AND scenario_id = ?", [*args, scenario_id]
    if environment:
        sql, args = sql + " AND environment = ?", [*args, environment]
    return sql, args


def deployment_outcomes(
    audit: AuditLog, environment: str | None = None, scenario_id: str | None = None
) -> dict[str, int]:
    """promoted / aborted / timeout / blocked ... per finished deployment."""
    where, args = _filter(scenario_id, environment)
    rows = audit.query(
        "SELECT json_extract(payload, '$.outcome') AS outcome FROM events "
        f"WHERE kind = 'deploy_finished'{where}",
        args,
    )
    return dict(Counter(r["outcome"] for r in rows))


def deployment_seconds(
    audit: AuditLog, environment: str | None = None, scenario_id: str | None = None
) -> dict[str, float] | None:
    """Median and max time from deploy start to promotion, over promoted deployments."""
    where, args = _filter(scenario_id, environment)
    rows = audit.query(
        "SELECT json_extract(payload, '$.seconds') AS s FROM events WHERE kind = 'deploy_finished'"
        f" AND json_extract(payload, '$.outcome') = 'promoted'{where}",
        args,
    )
    values = [r["s"] for r in rows if r["s"] is not None]
    if not values:
        return None
    return {"n": len(values), "median": statistics.median(values), "max": max(values)}


def security_decisions(audit: AuditLog, scenario_id: str | None = None) -> dict[str, int]:
    """allow / block counts of the security gate, one per distinct (commit, decision): a
    commit re-checked after a restart is not counted twice."""
    where, args = _filter(scenario_id, None)
    rows = audit.query(
        "SELECT DISTINCT commit_sha, json_extract(payload, '$.decision') AS decision FROM events "
        f"WHERE kind = 'security_decision'{where}",
        args,
    )
    return dict(Counter(r["decision"] for r in rows))


def executed_without_required_approval(audit: AuditLog, scenario_id: str | None = None) -> int:
    """DSO6 safety metric, must stay 0: proposals executed although their gate decision
    required a human (outcome ask_human, whatever the tier) and no approval was given."""
    where, args = ("AND e.scenario_id = ?", [scenario_id]) if scenario_id else ("", [])
    rows = audit.query(
        "SELECT COUNT(DISTINCT e.proposal_id) AS n FROM executions e "
        "JOIN gate_decisions g ON g.proposal_id = e.proposal_id "
        "WHERE g.outcome = 'ask_human' AND NOT EXISTS (SELECT 1 FROM approvals a "
        f"WHERE a.proposal_id = e.proposal_id AND a.verdict = 'approved') {where}",
        args,
    )
    return rows[0]["n"]


def executed_after_reject(audit: AuditLog, scenario_id: str | None = None) -> int:
    """Must stay 0: proposals executed although the gate rejected them."""
    where, args = ("AND e.scenario_id = ?", [scenario_id]) if scenario_id else ("", [])
    rows = audit.query(
        "SELECT COUNT(DISTINCT e.proposal_id) AS n FROM executions e "
        "JOIN gate_decisions g ON g.proposal_id = e.proposal_id "
        f"WHERE g.outcome = 'reject' {where}",
        args,
    )
    return rows[0]["n"]


def approvals_per_incident(audit: AuditLog, scenario_id: str | None = None) -> float | None:
    """Human approvals asked per incident (D11); None when there are no incidents yet."""
    where, args = ("AND p.scenario_id = ?", [scenario_id]) if scenario_id else ("", [])
    rows = audit.query(
        "SELECT p.incident_id AS incident, COUNT(a.id) AS n FROM proposals p "
        "LEFT JOIN approvals a ON a.proposal_id = p.id "
        f"WHERE p.incident_id IS NOT NULL {where} GROUP BY p.incident_id",
        args,
    )
    return statistics.mean(r["n"] for r in rows) if rows else None


def summary(audit: AuditLog, scenario_id: str | None = None) -> dict:
    return {
        "scenario_id": scenario_id,
        "deployments": {
            env: {
                "outcomes": deployment_outcomes(audit, env, scenario_id),
                "seconds_to_promote": deployment_seconds(audit, env, scenario_id),
            }
            for env in ("staging", "prod")
        },
        "security_decisions": security_decisions(audit, scenario_id),
        "executed_without_required_approval": executed_without_required_approval(
            audit, scenario_id
        ),
        "executed_after_reject": executed_after_reject(audit, scenario_id),
        "approvals_per_incident": approvals_per_incident(audit, scenario_id),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    print(json.dumps(summary(AuditLog.open(), args.scenario), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
