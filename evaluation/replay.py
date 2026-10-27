"""Offline replay (D15): a recording -> the events a real system would have seen -> agents.

    python evaluation/replay.py <recording folder> [--level L3]

`events(recording)` reads only what the system could observe: CI job logs, metadata and
annotations, the load generator's per-pod log, the recorded Prometheus series (including the
L0 alerts). It never opens ground_truth.json: that file is for scoring, which happens outside
this module. Each recording gives at most one event per kind:

    ci_failure       the first failed job's log, with the run and job ids
    scan_completed   the scan counts annotated by the scans job
    rollout_step     canary vs stable 5xx ratios from the per-pod load log
    metrics_tick     the L0 alerts that fired, the 5xx ratio and the peak memory share
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents")]

from flowguard.contracts import Event  # noqa: E402

SCAN = re.compile(r"(trivy_high|trivy_critical|gitleaks)=(\d+)")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def ci_events(rec: Path, manifest: dict) -> list[Event]:
    out = []
    for entry in manifest["sources"].get("ci") or []:
        folder = rec / "ci" / str(entry["run_id"])
        jobs = _json(folder / "jobs.json") or []
        failed = next((j for j in jobs if j.get("conclusion") == "failure"), None)
        if failed:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in failed["name"])
            log_path = folder / "logs" / f"{safe}.txt"
            out.append(
                Event(
                    id=f"{rec.name}-ci",
                    type="ci_failure",
                    service="podinfo",
                    environment="staging",
                    payload={
                        "log": log_path.read_text(encoding="utf-8") if log_path.is_file() else "",
                        "params": {"run_id": entry["run_id"], "job_id": failed["id"]},
                    },
                )
            )
        notes = _json(folder / "annotations.json") or {}
        counts = {
            k: int(v)
            for n in notes.get("scans", [])
            for k, v in SCAN.findall(n.get("message") or "")
        }
        if counts:
            out.append(
                Event(
                    id=f"{rec.name}-scan",
                    type="scan_completed",
                    service="podinfo",
                    environment="staging",
                    payload={"scan_counts": counts, "params": {"run_id": entry["run_id"]}},
                )
            )
    return out


def pod_hash(pod: str | None) -> str | None:
    parts = (pod or "").rsplit("-", 2)
    return parts[1] if len(parts) == 3 else None


def rollout_event(rec: Path) -> Event | None:
    """Canary vs stable from the per-pod load log: the most recent hash is the canary."""
    rows = (
        [
            json.loads(line)
            for line in (rec / "loadgen.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if (rec / "loadgen.jsonl").is_file()
        else []
    )
    first_seen: dict[str, str] = {}
    counts: dict[str, list[int]] = {}
    for r in rows:
        h = pod_hash(r.get("pod"))
        if h is None:
            continue
        first_seen.setdefault(h, r["ts"])
        c = counts.setdefault(h, [0, 0])
        c[0] += 1
        c[1] += (r.get("status") or 0) >= 500
    if len(counts) < 2:
        return None
    stable, canary = sorted(first_seen, key=first_seen.get)[:2]
    ratio = {h: counts[h][1] / counts[h][0] for h in (stable, canary)}
    return Event(
        id=f"{rec.name}-rollout",
        type="rollout_step",
        service="podinfo",
        environment="staging",
        payload={
            "metrics": {"canary_5xx_ratio": ratio[canary], "stable_5xx_ratio": ratio[stable]},
            "in_canary": True,
        },
    )


def metrics_event(rec: Path) -> Event:
    alerts = _json(rec / "prometheus" / "alerts.json") or {}
    fired = sorted(
        {
            s["metric"].get("alertname")
            for s in alerts.get("result", [])
            if s["metric"].get("alertstate") == "firing"
        }
    )
    rows = (
        [
            json.loads(line)
            for line in (rec / "loadgen.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if (rec / "loadgen.jsonl").is_file()
        else []
    )
    total = len(rows) or 1
    errors = sum((r.get("status") or 0) >= 500 for r in rows)
    return Event(
        id=f"{rec.name}-metrics",
        type="metrics_tick",
        service="podinfo",
        environment="staging",
        payload={"alerts": fired, "metrics": {"error_ratio": errors / total}},
    )


def events(rec: Path) -> list[Event]:
    manifest = _json(rec / "manifest.json") or {"sources": {}}
    out = ci_events(rec, manifest)
    rollout = rollout_event(rec)
    if rollout:
        out.append(rollout)
    out.append(metrics_event(rec))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recording", type=Path)
    args = parser.parse_args(argv)
    for e in events(args.recording):
        summary = {k: (v if k != "log" else f"{len(v)} chars") for k, v in e.payload.items()}
        print(json.dumps({"type": e.type, "payload": summary}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
