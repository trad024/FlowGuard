"""L0 baseline for every DSO on the dev data (Step 4b): the numbers the power analysis needs.

    python evaluation/l0_baseline.py        # writes evaluation/runs/l0-baseline.json

Scoring side: reads the ground-truth DB and the recordings, the corpus and its split, the CI
run data (l0-dso5.json) and the dev DSO6 set. Every rate has its count and a 95% Wilson
interval; false alerts per hour an exact Poisson interval. Release and runtime results are
also given per intensity band. Only valid runs (no host sleep) count.

    DSO1  runtime incidents detected by an L0 alert (D33) during the incident; MTTD; false
          alerts per hour on the normal-traffic window
    DSO2  the regex classifier (D3) on the corpus' training and validation templates
    DSO3  bad releases aborted before 100% (caught) and good releases aborted (false rollbacks)
    DSO4  security gate: bad commits blocked, good commits blocked
    DSO5  pipeline duration and time to first failure in the fixed order (l0-dso5.json)
    DSO6  no gate: share of risky proposals that would run without approval
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import stats  # noqa: E402
from flowguard import baselines  # noqa: E402
from flowguard.catalog import CATALOG  # noqa: E402
from flowguard.contracts import RiskTier  # noqa: E402

RUNS = ROOT / "evaluation" / "runs"
TRUTH_DB = RUNS / "groundtruth.db"
CORPUS = RUNS / "corpus"
SPLIT = ROOT / "evaluation" / "datasets" / "corpus-split.json"


def ts(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def rate(k: int, n: int) -> dict:
    lo, hi = stats.wilson(k, n)
    return {
        "k": k,
        "n": n,
        "rate": round(k / n, 3) if n else None,
        "ci95": [round(lo, 3), round(hi, 3)],
    }


def runs() -> list[dict]:
    db = sqlite3.connect(TRUTH_DB)
    db.row_factory = sqlite3.Row
    out = []
    for r in db.execute("SELECT * FROM runs WHERE valid = 1"):
        row = dict(r)
        for k in ("spec", "ground_truth", "observed"):
            row[k] = json.loads(row[k])
        out.append(row)
    return out


def firing(recording: Path) -> list[tuple[str, float]]:
    """(alertname, time) of every firing sample in the recording."""
    path = recording / "prometheus" / "alerts.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted(
        (s["metric"].get("alertname", ""), float(t))
        for s in data.get("result", [])
        if s["metric"].get("alertstate") == "firing"
        for t, _ in s.get("values", [])
    )


def alerts_recorded(recording: Path) -> bool:
    """False for recordings made before the L0 alert states were recorded (D50)."""
    return (recording / "prometheus" / "alerts.json").is_file()


def leftover(recording: Path, start: float) -> list[str]:
    """Alerts still firing when the fault was injected: a firing sample in the minute before."""
    return sorted({name for name, t in firing(recording) if start - 60 <= t <= start})


def dso1(all_runs: list[dict]) -> dict:
    by_band: dict[str, list[tuple[bool, float | None]]] = defaultdict(list)
    excluded, with_leftover, clean = [], [], []
    for r in all_runs:
        if r["family"] != "runtime" or not r["recording"]:
            continue
        incident = r["ground_truth"].get("incident") or {}
        rec = Path(r["recording"])
        if not incident.get("start") or not rec.is_dir():
            continue
        if not alerts_recorded(rec):  # D50: not an L0 miss, nothing was recorded to hit
            excluded.append(rec.name)
            continue
        start, end = ts(incident["start"]), ts(incident["end"]) + 60  # the cooldown counts
        hits = [t for _, t in firing(rec) if start <= t <= end]
        band = r["ground_truth"].get("band", "dev")
        by_band[band].append((bool(hits), (min(hits) - start) if hits else None))
        if leftover(rec, start):
            with_leftover.append(rec.name)
        else:
            clean.append(bool(hits))
    all_cases = [c for cases in by_band.values() for c in cases]
    mttd = [m for _, m in all_cases if m is not None]
    normal = [r for r in all_runs if r["family"] == "normal" and r["recording"]]
    episodes, hours = 0, 0.0
    for r in normal:
        times = [t for _, t in firing(Path(r["recording"]))]
        episodes += sum(
            1 for a, b in zip([None, *times], times, strict=False) if a is None or b - a > 60
        )
        hours += float(r["spec"].get("hours", 0))
    lo, hi = stats.poisson_exact(episodes)
    return {
        "recall": rate(sum(d for d, _ in all_cases), len(all_cases)),
        "excluded_no_alert_states": sorted(excluded),
        "sensitivity_without_leftover_alerts": {
            "recall": rate(sum(clean), len(clean)),
            "excluded": sorted(with_leftover),
        },
        "recall_by_band": {
            b: rate(sum(d for d, _ in c), len(c)) for b, c in sorted(by_band.items())
        },
        "mttd_s_median": statistics.median(mttd) if mttd else None,
        "false_alerts_per_hour": {
            "episodes": episodes,
            "hours": hours,
            "rate": round(episodes / hours, 3) if hours else None,
            "ci95": [round(lo / hours, 3), round(hi / hours, 3)] if hours else None,
        },
    }


def dso2() -> dict:
    if not SPLIT.is_file():
        return {"note": "corpus split not written yet"}
    split = json.loads(SPLIT.read_text(encoding="utf-8"))
    truth, pred = [], []
    for vid, info in split["variants"].items():
        if split["templates"][info["template"]] == "test":
            continue  # the test templates stay unseen until Step 11
        logs = list((CORPUS / vid).glob("*.log"))
        if not logs:
            continue
        cls, _ = baselines.classify(logs[0].read_text(encoding="utf-8", errors="replace"))
        truth.append(info["root_cause"])
        pred.append(cls)
    classes = sorted(set(truth))
    f1s = {}
    for c in classes:
        tp = sum(t == c and p == c for t, p in zip(truth, pred, strict=True))
        fp = sum(t != c and p == c for t, p in zip(truth, pred, strict=True))
        fn = sum(t == c and p != c for t, p in zip(truth, pred, strict=True))
        f1s[c] = round(2 * tp / (2 * tp + fp + fn), 3) if tp else 0.0
    correct = sum(t == p for t, p in zip(truth, pred, strict=True))
    return {
        "accuracy": rate(correct, len(truth)),
        "macro_f1": round(sum(f1s.values()) / len(f1s), 3) if f1s else None,
        "f1_by_class": f1s,
        "unclear": pred.count("unclear"),
        "scope": "training and validation templates only",
    }


def dso3(all_runs: list[dict]) -> dict:
    bad, good = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for r in all_runs:
        if r["family"] != "release":
            continue
        gt, aborted = r["ground_truth"], r["observed"].get("l0") == "aborted"
        band = gt.get("band") or "dev"
        if "node_stress" in r["spec"]:
            band += " + node stress"
        bucket = bad if gt.get("bad_release") else good
        bucket[band][0] += aborted
        bucket[band][1] += 1
    total = lambda d: (sum(v[0] for v in d.values()), sum(v[1] for v in d.values()))  # noqa: E731
    return {
        "bad_caught": rate(*total(bad)),
        "bad_caught_by_band": {b: rate(*v) for b, v in sorted(bad.items())},
        "false_rollbacks": rate(*total(good)),
        "false_rollbacks_by_case": {b: rate(*v) for b, v in sorted(good.items())},
    }


def dso4(all_runs: list[dict]) -> dict:
    bad = [
        r for r in all_runs if r["family"] == "security" and r["ground_truth"].get("bad_release")
    ]
    good = [
        r
        for r in all_runs
        if r["family"] == "security" and not r["ground_truth"].get("bad_release")
    ]
    blocked = lambda rs: sum(r["observed"].get("l0") == "blocked" for r in rs)  # noqa: E731
    return {
        "bad_blocked": rate(blocked(bad), len(bad)),
        "good_blocked": rate(blocked(good), len(good)),
    }


def dso6() -> dict:
    import yaml

    import heldout

    prereg = yaml.safe_load(heldout.PREREG.read_text(encoding="utf-8"))
    prereg["dso6_adversarial_set"]["count"] = 300
    items = heldout.dso6_set(prereg, "dev-dso6-20261027")
    risky = 0
    for item in items:
        for p in item.get("proposals", []):
            entry = CATALOG.get(p["action"])
            risky += entry is None or entry.tier in (RiskTier.HIGH, RiskTier.PROHIBITED)
    return {
        "risky_proposals": risky,
        "executed_without_approval_at_L0": rate(risky, risky),
        "note": "no gate: every proposal runs",
    }


def main() -> int:
    all_runs = runs()
    dso5_path = RUNS / "l0-dso5.json"
    out = {
        "valid_runs": len(all_runs),
        "DSO1": dso1(all_runs),
        "DSO2": dso2(),
        "DSO3": dso3(all_runs),
        "DSO4": dso4(all_runs),
        "DSO5": json.loads(dso5_path.read_text(encoding="utf-8"))["summary"]
        if dso5_path.is_file()
        else None,
        "DSO6": dso6(),
    }
    (RUNS / "l0-baseline.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
