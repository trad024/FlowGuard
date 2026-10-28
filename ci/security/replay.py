"""Paired replay of sanitized development scan evidence (no live execution)."""

import json
import math
from collections import Counter
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
    return [center - half, center + half]


def main():
    cases = json.loads(Path("ci/security/dev-cases.json").read_text(encoding="utf-8"))
    epss = Path("ci/security/epss-sample.csv")
    result = {
        "source": "three recorded development security cases",
        "n": len(cases),
        "levels": {},
        "policy": "transparent weighted sum; no learned policy or fitted labels",
        "epss_provenance": json.loads(epss.with_suffix(".json").read_text(encoding="utf-8")),
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
            action = agent.run(inp, level(name)).proposals[0].action
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
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
