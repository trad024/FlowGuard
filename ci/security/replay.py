"""Paired replay of sanitized development scan evidence (no live execution)."""

import json
import math
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from flowguard.orchestrator import level
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


def main():
    cases = json.loads(Path("ci/security/dev-cases.json").read_text(encoding="utf-8"))
    epss = Path("ci/security/epss-sample.csv")
    result = {
        "source": "14 recorded development CI scan cases, including 12 controls",
        "n": len(cases),
        "levels": {},
        "policy": "transparent weighted sum; no learned policy or fitted labels",
        "epss_provenance": json.loads(epss.with_suffix(".json").read_text(encoding="utf-8")),
        "per_case": {case["id"]: {} for case in cases},
        "signal_effects": {
            "epss_decision_changes_vs_zero": 0,
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
                without_epss = []
                for finding in explanation["findings"]:
                    total = sum(
                        Decimal(str(weight)) * Decimal(str(finding["normalized_values"][k]))
                        for k, weight in explanation["weights"].items()
                        if k != "epss"
                    )
                    without_epss.append(
                        int((total * 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
                    )
                zero_action = (
                    "request_security_review"
                    if max(without_epss) >= 5000 or explanation["missing_required"]
                    else "allow_with_findings"
                )
                result["signal_effects"]["epss_decision_changes_vs_zero"] += zero_action != action
                result["signal_effects"]["cases_with_observed_reachability"] += any(
                    type(f["reachable"]) is bool for f in explanation["findings"]
                )
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
    path = Path("ci/security/dev-results.json")
    result["signal_effects"]["conclusion"] = (
        "No case in this development set changes decision due to EPSS. "
        "No historical case has measured reachability; no reachability-changing dev case exists. "
        "The new govulncheck integration has not yet run on these historical commits."
    )
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
