"""Paired replay of sanitized development scan evidence (no live execution)."""

import argparse
import copy
import json
import math
from collections import Counter
from dataclasses import replace
from pathlib import Path

from flowguard.epss import load
from flowguard.orchestrator import level
from flowguard.security import THRESHOLD_BPS, cvss_base, normalize, score
from flowguard.security import Security as ScoredSecurity
from flowguard.stubs import AgentInput, Security


def wilson(success, total):
    if not total:
        return None
    z = 1.959963984540054
    p = success / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total**2)) / (1 + z * z / total)
    return [0.0 if success == 0 else center - half, 1.0 if success == total else center + half]


def counterfactual(scan, snapshot, actual, *, zero_epss=False, unknown_reachability=False):
    """Change measured inputs only after the real policy reaches its scoring branch.

    Hard-rule, malformed-evidence and no-finding decisions retain the actual action.
    This diagnostic does not replace missing EPSS values with known zeros.
    """
    if not actual.params.get("score_explanation"):
        return actual.action
    if zero_epss:
        snapshot = replace(snapshot, scores=dict.fromkeys(snapshot.scores, 0.0))
    scan = copy.deepcopy(scan)
    if unknown_reachability:
        for item in scan.evidence:
            item["reachable"] = None
    _, explanation = score(scan, snapshot)
    return (
        "request_security_review"
        if explanation["score_basis_points"] >= THRESHOLD_BPS or explanation["missing_required"]
        else "allow_with_findings"
    )


def finding_rows(case, snapshot):
    rows = []
    for target in case["reports"]["trivy"]["Results"]:
        for finding in target.get("Vulnerabilities") or []:
            cvss, source = cvss_base(finding.get("CVSS"))
            fixed = finding.get("FixedVersion")
            rows.append(
                {
                    "cve": finding["VulnerabilityID"],
                    "module": finding.get("PkgName"),
                    "version": finding.get("InstalledVersion"),
                    "severity": finding["Severity"],
                    "cvss": cvss,
                    "cvss_source": source,
                    "epss": snapshot.scores.get(finding["VulnerabilityID"]),
                    "reachable": finding.get("Reachable"),
                    "fix_available": bool(fixed.strip()) if isinstance(fixed, str) else None,
                    "fixed_version": fixed,
                }
            )
    return rows


def replay(cases, epss):
    snapshot = load(epss)
    result = {
        "source": "Recorded development app-ci scan cases; deliberately selected, not held-out",
        "n": len(cases),
        "levels": {},
        "policy": "transparent weighted sum; no learned policy or fitted labels",
        "epss_provenance": json.loads(epss.with_suffix(".json").read_text(encoding="utf-8")),
        "per_case": {case["id"]: {} for case in cases},
        "signal_effects": {
            "epss_decision_changes_vs_zero": 0,
            "reachability_decision_changes_vs_unknown": 0,
            "cases_with_observed_reachability": 0,
        },
    }
    for name in ("L0", "L1"):
        actions, false_blocks, controls = Counter(), 0, 0
        for case in cases:
            inp = AgentInput(
                incident_id=case["id"],
                scan_counts=case["counts"],
                params={"scan_reports": case["reports"], "run_id": case["run_id"]},
            )
            agent = Security() if name == "L0" else ScoredSecurity(epss)
            proposal = agent.run(inp, level(name)).proposals[0]
            action = proposal.action
            explanation = proposal.params.get("score_explanation")
            result["per_case"][case["id"]][name] = {
                "action": action,
                "score_basis_points": explanation["score_basis_points"] if explanation else None,
            }
            if name == "L1" and explanation:
                scan = normalize(case["counts"], case["reports"])
                zero_action = counterfactual(scan, snapshot, proposal, zero_epss=True)
                unknown_action = counterfactual(scan, snapshot, proposal, unknown_reachability=True)
                result["signal_effects"]["epss_decision_changes_vs_zero"] += zero_action != action
                result["signal_effects"]["reachability_decision_changes_vs_unknown"] += (
                    unknown_action != action
                )
                result["signal_effects"]["cases_with_observed_reachability"] += any(
                    type(f["reachable"]) is bool for f in explanation["findings"]
                )
                result["per_case"][case["id"]]["counterfactuals"] = {
                    "zero_epss_action": zero_action,
                    "unknown_reachability_action": unknown_action,
                    "epss_changed_decision": zero_action != action,
                    "reachability_changed_decision": unknown_action != action,
                }
            if name == "L1":
                details = result["per_case"][case["id"]]
                details.setdefault(
                    "counterfactuals",
                    {
                        "zero_epss_action": action,
                        "unknown_reachability_action": action,
                        "epss_changed_decision": False,
                        "reachability_changed_decision": False,
                        "reason": "no scoring branch; preserve hard rules and evidence errors",
                    },
                )
                details["findings"] = finding_rows(case, snapshot)
                details["source"] = case["source"]
                if "target" in case:
                    details["target"] = case["target"]
            actions[action] += 1
            if not case["bad_release"]:
                controls += 1
                false_blocks += action == "block_deployment"
        result["levels"][name] = {
            "actions": dict(actions),
            "false_blocks": false_blocks,
            "controls": controls,
            "false_block_rate": false_blocks / controls if controls else None,
            "false_block_wilson95": wilson(false_blocks, controls),
        }
    effects = result["signal_effects"]
    effects["conclusion"] = (
        f"EPSS changed {effects['epss_decision_changes_vs_zero']}/{len(cases)} "
        "whole-case decisions versus zero EPSS; "
        f"reachability changed {effects['reachability_decision_changes_vs_unknown']}/{len(cases)} "
        "versus unknown reachability. Counterfactual diagnostics on development data only."
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("ci/security/dev-cases.json"))
    parser.add_argument("--epss", type=Path, default=Path("state/epss/2026-10-28.csv"))
    parser.add_argument("--out", type=Path, default=Path("ci/security/dev-results.json"))
    args = parser.parse_args()
    # Production/development replay requires the complete declared FIRST source.
    from prepare_development import verify_plan

    plan = json.loads(Path("ci/security/development-suite.json").read_text(encoding="utf-8"))
    verify_plan(plan, args.epss)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    result = replay(cases, args.epss)
    path = args.out
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(result["signal_effects"]["conclusion"])
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
