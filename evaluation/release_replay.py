"""DSO3 by replay (D15, Step 8): Release at L3 and L4 against L0 on the same dev recordings.

    python evaluation/release_replay.py score     <recordings folder or zip> ... [--out FILE]
    python evaluation/release_replay.py calibrate <recordings folder or zip> ...

Scoring side: reads each release recording's ground_truth.json (bad or good, band, node stress,
L0's recorded outcome). Release sees only rollout_step events built by `flowguard.release`
from the recorded Prometheus series, and runs inside the real orchestrator (dry-run executor,
no approver), so P1 at L4 asks the real Monitor and Diagnose through the incident record.

Timeline, from the pods up per pod-template hash (the deployer events are not recorded): the
stable hash is the one serving at the start, the canary the other; a step starts when the
canary has 2 pods up (20%) and 5 (50%); the canary ends when it has 10 (100%) or when its pods
are gone (aborted). Release looks where L0 measured: 30, 60, 90 and 120 s into each step, from
counts since the 20% step began. A recording follows L0's own path: after an L0 abort there is
no 50% step to look at, so a release Release would have let through 20% there is counted as not
caught ("truncated"), never as caught.

    L0     the frozen AnalysisTemplate as it ran live (observed outcome, canary pods gone)
    L3     Release alone (L1 to L3 are the same: Release has no LLM)
    L4     Release with P1

Per recording: rollback (rollout_abort), escalate (held for a human), promoted (reached 100%)
or truncated. Bad releases are caught when rolled back or held before 100%; good releases
rolled back are false rollbacks; a wrong decision is a bad release let through or a good one
rolled back (escalations are counted apart). Time to rollback runs from the 20% step's start.
Rates with n and a Wilson interval; L0 vs L3 and L3 vs L4 paired by exact McNemar on right /
wrong; every result per band; L3's wrong-decision rate on the node-stress cases (H4's count,
D44). All of it is labelled "dev, in-sample".

calibrate: the label scores of every promote and rollback proposal at L3, labelled by the
recording (rollback if bad), fit the conformal calibrator Release's proposals are gated with
(D9); coverage is reported leave-one-recording-out.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation")]

import monitor_replay  # noqa: E402
import stats  # noqa: E402
from flowguard import release, series  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.calibration import ConformalCalibrator, coverage_by_group  # noqa: E402
from flowguard.executor import Executor  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402
from flowguard.orchestrator import Orchestrator, level  # noqa: E402

LABEL = "dev, in-sample"
GRID_S = 5.0
UP_LOOKBACK_S = 20.0  # pods up: a sample older than this no longer counts
STEP_PODS = {20: 2, 50: 5}  # canary pods up when a step's weight is reached (10 replicas)
FULL_PODS = 10
LOOKS = tuple(release.WINDOW_S * k for k in range(1, release.LOOKS_PER_STEP + 1))
OUT = ROOT / "evaluation" / "runs" / "dso3-replay.json"
EPSILON = 0.1  # conformal coverage target 90%


# ---------------------------------------------------------------- recordings


def release_runs(paths: list[Path]) -> list[dict]:
    runs = []
    for path in paths:
        runs += [
            r
            for r in monitor_replay.load_runs(monitor_replay.open_recordings(path))
            if r["family"] == "release"
        ]
    ids = [r["id"] for r in runs]
    if len(ids) != len(set(ids)):
        raise ValueError("the same recording appears twice")
    for r in runs:  # L0's live outcome
        truth = json.loads((Path(r["recording"]) / "ground_truth.json").read_text())
        r["observed"] = truth.get("observed") or {}
    return runs


def load_index(run: dict) -> series.Index:
    rec = Path(run["recording"])
    return series.index(
        {p.stem: json.loads(p.read_text()) for p in sorted((rec / "prometheus").glob("*.json"))}
    )


def pods_up(idx: series.Index, t: float, namespace: str) -> dict[str, int]:
    up: dict[str, int] = defaultdict(int)
    for s in idx.get("podinfo_up", []):
        h = s.labels.get("rollouts_pod_template_hash")
        if s.labels.get("namespace") == namespace and h and s.latest(t, UP_LOOKBACK_S) == 1:
            up[h] += 1
    return up


def timeline(idx: series.Index, namespace: str, t0: float, t1: float) -> dict | None:
    """Stable and canary hashes, each step's start, and how and when the canary ended."""
    grid = [t0 + GRID_S * k for k in range(int((t1 - t0) // GRID_S) + 1)]
    counts = [(t, pods_up(idx, t, namespace)) for t in grid]
    start = next((c for _, c in counts if c), None)
    if not start:
        return None
    stable = max(start, key=start.get)
    steps: dict[int, float] = {}
    end, how, seen = t1, "window_end", False
    canary = None
    for t, c in counts:
        others = {h: n for h, n in c.items() if h != stable}
        if canary is None and others:
            canary = max(others, key=others.get)
        if canary is None:
            continue
        n = c.get(canary, 0)
        seen |= n > 0
        for weight, pods in STEP_PODS.items():
            if n >= pods and weight not in steps:
                steps[weight] = t
        if n >= FULL_PODS:
            end, how = t, "promoted"
            break
        if seen and n == 0 and 20 in steps:
            end, how = t, "aborted"
            break
    if canary is None or 20 not in steps:
        return None
    return {"stable": stable, "canary": canary, "steps": steps, "end": end, "how": how}


# ---------------------------------------------------------------- Release on a recording


def orchestrator(name: str, folder: Path) -> Orchestrator:
    db = folder / f"{name}.db"
    audit = AuditLog(db, level=name)
    return Orchestrator(
        cfg=level(name),
        record=IncidentRecord(db),
        executor=Executor(audit=audit, dry_run=True),
        audit=audit,
    )


def replay(run: dict, name: str, folder: Path, idx: series.Index | None = None) -> dict:
    """Release's looks on one recording until it rolls back, escalates or reaches 100%."""
    idx = load_index(run) if idx is None else idx
    ns = run["spec"].get("environment", "staging")
    line = timeline(idx, ns, *run["window"])
    if line is None:
        return {"outcome": "no_canary", "decisions": []}
    orch = orchestrator(name, folder)
    since = line["steps"][20]
    decisions = []
    for weight in sorted(STEP_PODS):
        if weight not in line["steps"]:
            break
        for k, offset in enumerate(LOOKS, start=1):
            t = line["steps"][weight] + offset
            if t > line["end"]:
                return {"outcome": "truncated", "decisions": decisions, "timeline": line}
            event = release.step_event(
                idx, t, ns, line["canary"], line["stable"], since, k, weight=weight
            )
            out = orch.handle(event)
            props = out["output"].proposals
            action = props[0].action if props else None
            decisions.append(
                {
                    "t": round(t - since, 1),
                    "weight": weight,
                    "look": k,
                    "action": action,
                    "scores": out["output"].label_scores.get(props[0].id) if props else None,
                }
            )
            if action == "rollout_abort":
                return {"outcome": "rollback", "t": t - since, "decisions": decisions}
            if action == "escalate_to_human":
                return {"outcome": "escalate", "t": t - since, "decisions": decisions}
            if action == "rollout_promote":
                break
        else:
            return {"outcome": "truncated", "decisions": decisions, "timeline": line}
    if 50 in line["steps"] and decisions and decisions[-1]["action"] == "rollout_promote":
        return {"outcome": "promoted", "decisions": decisions}
    return {"outcome": "truncated", "decisions": decisions, "timeline": line}


def l0_outcome(run: dict, idx: series.Index) -> dict:
    observed = run["observed"].get("l0")
    line = timeline(idx, run["spec"].get("environment", "staging"), *run["window"])
    t = line["end"] - line["steps"][20] if line else None
    if observed == "aborted":
        return {"outcome": "rollback", "t": t}
    return {"outcome": "promoted" if observed == "promoted" else str(observed)}


# ---------------------------------------------------------------- scoring


def band(run: dict) -> str:
    b = run["ground_truth"].get("band") or "dev"
    return b + (" + node stress" if run["spec"].get("node_stress") else "")


def stopped(outcome: str) -> bool:
    return outcome in ("rollback", "escalate")


def wrong(run: dict, outcome: str) -> bool:
    bad = bool(run["ground_truth"].get("bad_release"))
    return (bad and not stopped(outcome)) or (not bad and outcome == "rollback")


def rate(k: int, n: int) -> dict:
    return monitor_replay.rate(k, n)


def summary(runs: list[dict], res: dict[str, dict]) -> dict:
    bad = [r for r in runs if r["ground_truth"].get("bad_release")]
    good = [r for r in runs if not r["ground_truth"].get("bad_release")]
    times = [res[r["id"]]["t"] for r in bad if res[r["id"]]["outcome"] == "rollback"]

    def by_band(items, pred):
        groups = defaultdict(list)
        for r in items:
            groups[band(r)].append(pred(r))
        return {b: rate(sum(v), len(v)) for b, v in sorted(groups.items())}

    caught = lambda r: stopped(res[r["id"]]["outcome"])  # noqa: E731
    false_rb = lambda r: res[r["id"]]["outcome"] == "rollback"  # noqa: E731
    return {
        "bad_caught_before_100": rate(sum(map(caught, bad)), len(bad)),
        "bad_caught_by_band": by_band(bad, caught),
        "bad_escalated": sum(res[r["id"]]["outcome"] == "escalate" for r in bad),
        "bad_truncated": sum(res[r["id"]]["outcome"] == "truncated" for r in bad),
        "false_rollbacks": rate(sum(map(false_rb, good)), len(good)),
        "false_rollbacks_by_band": by_band(good, false_rb),
        "good_escalated": sum(res[r["id"]]["outcome"] == "escalate" for r in good),
        "wrong_decisions": rate(sum(wrong(r, res[r["id"]]["outcome"]) for r in runs), len(runs)),
        "time_to_rollback_s_median": round(statistics.median(times), 1) if times else None,
        "rollbacks_timed": len(times),
    }


def paired(runs: list[dict], a: dict[str, dict], b: dict[str, dict]) -> dict:
    """Exact McNemar on right / wrong, and the median rollback-time gain where both caught."""
    only_a = sum(
        not wrong(r, a[r["id"]]["outcome"]) and wrong(r, b[r["id"]]["outcome"]) for r in runs
    )
    only_b = sum(
        wrong(r, a[r["id"]]["outcome"]) and not wrong(r, b[r["id"]]["outcome"]) for r in runs
    )
    gains = [
        a[r["id"]]["t"] - b[r["id"]]["t"]
        for r in runs
        if a[r["id"]].get("t") is not None
        and b[r["id"]].get("t") is not None
        and a[r["id"]]["outcome"] == b[r["id"]]["outcome"] == "rollback"
    ]
    interval = stats.bootstrap_median(gains)
    return {
        "right_only_first": only_a,
        "right_only_second": only_b,
        "mcnemar_p": round(stats.mcnemar_exact(only_a, only_b), 4),
        "rollback_time_gain_s_median": round(statistics.median(gains), 1) if gains else None,
        "rollback_time_gain_ci95": [round(x, 1) for x in interval] if interval else None,
        "both_rolled_back": len(gains),
    }


def node_stress(runs: list[dict], res: dict[str, dict]) -> dict:
    cases = [r for r in runs if r["spec"].get("node_stress")]
    return {
        "wrong": rate(sum(wrong(r, res[r["id"]]["outcome"]) for r in cases), len(cases)),
        "cases": {r["id"]: res[r["id"]]["outcome"] for r in cases},
        "with_node_series": sum(
            any(
                s.startswith("node") for s in r["manifest"].get("sources", {}).get("prometheus", {})
            )
            for r in cases
        ),
    }


def score(runs: list[dict]) -> dict:
    results: dict[str, dict[str, dict]] = {"L0": {}, "L3": {}, "L4": {}}
    with tempfile.TemporaryDirectory(prefix="dso3-replay-") as tmp:
        for r in runs:
            idx = load_index(r)
            results["L0"][r["id"]] = l0_outcome(r, idx)
            for name in ("L3", "L4"):
                folder = Path(tmp) / r["id"]
                folder.mkdir(exist_ok=True)
                results[name][r["id"]] = replay(r, name, folder, idx)
    usable = [r for r in runs if results["L3"][r["id"]]["outcome"] != "no_canary"]
    return {
        "label": LABEL,
        "recordings": len(runs),
        "scored": len(usable),
        "no_canary": sorted(r["id"] for r in runs if r not in usable),
        "levels": {n: summary(usable, res) for n, res in results.items()},
        "L0_vs_L3": paired(usable, results["L0"], results["L3"]),
        "L3_vs_L4": paired(usable, results["L3"], results["L4"]),
        "node_stress_L3": node_stress(usable, results["L3"]),
        "node_stress_L4": node_stress(usable, results["L4"]),
        "per_recording": {
            r["id"]: {
                "bad": bool(r["ground_truth"].get("bad_release")),
                "band": band(r),
                **{
                    n: {k: v for k, v in res[r["id"]].items() if k != "timeline"}
                    for n, res in results.items()
                },
            }
            for r in usable
        },
    }


# ---------------------------------------------------------------- calibration (D9)


def calibration_set(per_recording: dict) -> tuple[list[dict], list[str], list[str]]:
    scores, labels, groups = [], [], []
    for rid, rec in per_recording.items():
        for d in rec["L3"]["decisions"]:
            if d["action"] in ("rollout_promote", "rollout_abort") and d["scores"]:
                scores.append(d["scores"])
                labels.append("rollback" if rec["bad"] else "continue")
                groups.append(rid)
    return scores, labels, groups


def calibrate(per_recording: dict, path: Path = release.CALIBRATION_FILE) -> dict:
    scores, labels, groups = calibration_set(per_recording)
    cal = ConformalCalibrator().fit(scores, labels)
    folds = coverage_by_group(scores, labels, groups, EPSILON) if len(set(groups)) > 1 else {}
    covered = sum(f["covered"] for f in folds.values())
    n = sum(f["n"] for f in folds.values())
    confidences = [cal.confidence(s) for s in scores]
    report = {
        "label": LABEL,
        "decisions": len(scores),
        "recordings": len(set(groups)),
        "calibrated": cal.calibrated,
        "coverage_leave_one_recording_out": rate(covered, n),
        "target": 1 - EPSILON,
        "mean_set_size": round(sum(f["mean_set_size"] * f["n"] for f in folds.values()) / n, 3)
        if n
        else None,
        "rollbacks_at_confidence_0_70": sum(
            1
            for (lab, c), s in zip(confidences, scores, strict=True)
            if lab == "rollback" and c >= 0.70
        ),
    }
    cal.save(
        path, label=LABEL, epsilon=EPSILON, **{k: v for k, v in report.items() if k != "label"}
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("score", "calibrate"))
    parser.add_argument("recordings", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    runs = release_runs(args.recordings)
    result = score(runs)
    if args.command == "calibrate":
        result = {"calibration": calibrate(result["per_recording"]), **result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "per_recording"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
