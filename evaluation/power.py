"""Power analysis (D16): scenario counts from the L0 baseline and the smallest effects of interest.

    python evaluation/power.py      # reads l0-baseline.json, writes power.json, prints a table

Primary tests H1 to H4 are paired McNemar tests (pre-registration); with Holm's correction
over the four (D31), each is sized at the strictest step, alpha = 0.05 / 4, for 80% power.
Each hypothesis' L0 success rate comes from the dev baseline; where the compared level is not
built yet the most conservative rate (0.5, the largest variance) is used, and said so.

Run time: each live scenario's measured duration (median of its family's recorded windows)
times its count; LLM days from the measured tokens per call (D27) and the assumed daily quota
(D35). Nothing is frozen here: the counts are shown for approval first.
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evaluation")]

import stats  # noqa: E402

RUNS = ROOT / "evaluation" / "runs"
ALPHA, POWER, PRIMARY = 0.05, 0.8, 4
TOKENS_PER_DAY, USABLE = 200_000, 0.9
LOGCHUNKS_USABLE = 130  # chunks located in their logs (D32)


def p_or_default(entry: dict | None, default: float = 0.5) -> tuple[float, str]:
    if entry and entry.get("n"):
        return min(0.95, max(0.05, entry["rate"])), f"L0 dev {entry['k']}/{entry['n']}"
    return default, "not measured yet: 0.5 (largest variance)"


def durations_min() -> dict[str, float]:
    """Median live duration per family, from the recordings' windows."""
    import sqlite3

    db = sqlite3.connect(RUNS / "groundtruth.db")
    out: dict[str, list[float]] = {}
    for family, recording in db.execute("SELECT family, recording FROM runs WHERE valid = 1"):
        manifest = Path(recording or "") / "manifest.json"
        if not manifest.is_file():
            continue
        w = json.loads(manifest.read_text(encoding="utf-8"))["window"]
        minutes = (
            datetime.fromisoformat(w["end"].replace("Z", "+00:00"))
            - datetime.fromisoformat(w["start"].replace("Z", "+00:00"))
        ).total_seconds() / 60
        out.setdefault(family, []).append(minutes)
    return {f: round(statistics.median(v), 1) for f, v in out.items()}


def main() -> int:
    base = json.loads((RUNS / "l0-baseline.json").read_text(encoding="utf-8"))
    alpha = ALPHA / PRIMARY
    rows = []

    p, src = p_or_default(base["DSO1"]["recall"])
    rows.append(
        ("H1", "DSO1 recall, L0 -> L1", p, min(1.0, p + 0.10), src, "runtime + CI incidents")
    )
    dso2 = base["DSO2"].get("accuracy")
    p, src = p_or_default(dso2)
    rows.append(
        (
            "H2",
            "DSO2 accuracy (proxy for macro-F1 +0.10), regex -> L3",
            p,
            min(1.0, p + 0.10),
            src,
            "corpus test logs",
        )
    )
    rows.append(
        (
            "H3",
            "LogChunks hit rate, L2 -> L3",
            0.5,
            0.6,
            "not measured yet: 0.5 (largest variance)",
            "LogChunks logs",
        )
    )
    stress = base["DSO3"].get("false_rollbacks_by_case", {})
    wrong = sum(v["k"] for k, v in stress.items() if "stress" in k)
    seen = sum(v["n"] for k, v in stress.items() if "stress" in k)
    p_wrong = (wrong / seen) if seen and wrong else 0.5
    rows.append(
        (
            "H4",
            "right decisions under node stress, L3 -> L4 (wrong -50%)",
            1 - p_wrong,
            1 - p_wrong / 2,
            f"L0 dev {wrong}/{seen} wrong" if seen and wrong else "not measured yet: 50% wrong",
            "collaboration scenarios",
        )
    )
    results = []
    for h, what, p0, p1, src, unit in rows:
        n = stats.mcnemar_n(p0, p1, alpha=alpha, power=POWER) if p1 > p0 else None
        results.append(
            {
                "hypothesis": h,
                "comparison": what,
                "p0": round(p0, 3),
                "p1": round(p1, 3),
                "p0_source": src,
                "n": n,
                "unit": unit,
            }
        )

    n = {r["hypothesis"]: r["n"] for r in results}
    dur = durations_min()
    counts = {
        "runtime": math.ceil(n["H1"] * 2 / 3),
        "pipeline": math.ceil(n["H1"] / 3),
        "release": n["H4"],
        "ci": 25,
        "security": 14,
    }
    live_min = sum(counts[f] * dur.get(f, 8.0) for f in counts) + 4 * 60  # + normal traffic
    llm_calls_per_level = (
        n["H2"] + min(n["H3"], LOGCHUNKS_USABLE) + counts["runtime"] + counts["release"] + 120
    )
    llm_calls = 3 * llm_calls_per_level  # L2, L3, L4 (L5 replays L4's answers)
    llm_tokens = json.loads((RUNS / "llm-budget.json").read_text(encoding="utf-8"))[
        "tokens_per_call"
    ]["mean"]
    llm_days = math.ceil(llm_calls * llm_tokens / (USABLE * TOKENS_PER_DAY))
    out = {
        "alpha_per_test": alpha,
        "power": POWER,
        "hypotheses": results,
        "proposed_counts": counts,
        "measured_minutes_per_scenario": dur,
        "live_hours": round(live_min / 60, 1),
        "llm_calls": llm_calls,
        "llm_tokens_per_call": llm_tokens,
        "llm_days_at_free_quota": llm_days,
        "gaps": [
            f"H3 needs {n['H3']} LogChunks logs; {LOGCHUNKS_USABLE} are usable"
            if n["H3"] > LOGCHUNKS_USABLE
            else None,
        ],
    }
    out["gaps"] = [g for g in out["gaps"] if g]
    (RUNS / "power.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
