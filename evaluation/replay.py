"""Offline replay (D15): a recording -> the events a real system would have seen -> agents.

    python evaluation/replay.py <recording folder> [--level L3]

`events(recording)` reads only what the system could observe: CI job logs, metadata and
annotations, the load generator's per-pod log, the recorded Prometheus series (including the
L0 alerts). It never opens ground_truth.json: that file is for scoring, which happens outside
this module. Each recording gives at most one event per kind:

    ci_run_completed every recorded CI run: run and job states and timings, and the first
                     failed job's log (D49: the Monitor sees it first)
    scan_completed   the scan counts annotated by the scans job
    rollout_step     canary vs stable 5xx ratios from the per-pod load log
    metrics_tick     the L0 alerts that fired, the 5xx ratio and the peak memory share
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents")]

from flowguard.contracts import Event  # noqa: E402
from flowguard.monitor import ci_event  # noqa: E402

SCAN = re.compile(r"(trivy_high|trivy_critical|gitleaks)=(\d+)")


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


# What a CI system shows about a run, without commit messages or branch names
RUN_FIELDS = ("id", "status", "conclusion", "run_started_at", "updated_at")
JOB_FIELDS = ("id", "name", "status", "conclusion", "started_at", "completed_at")


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ci_events(rec: Path, manifest: dict) -> list[Event]:
    out = []
    for entry in manifest["sources"].get("ci") or []:
        folder = rec / "ci" / str(entry["run_id"])
        jobs = [{k: j.get(k) for k in JOB_FIELDS} for j in _json(folder / "jobs.json") or []]
        meta = _json(folder / "run.json") or {"id": entry["run_id"]}
        run = {k: meta.get(k) for k in RUN_FIELDS}
        failed = next((j for j in jobs if j.get("conclusion") == "failure"), None)
        log = ""
        if failed:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in failed["name"])
            log_path = folder / "logs" / f"{safe}.txt"
            log = log_path.read_text(encoding="utf-8") if log_path.is_file() else ""
        if run.get("status") == "completed" or failed:
            ends = [j["completed_at"] for j in jobs if j.get("completed_at")]
            t = _epoch(max(ends)) if ends else 0.0
            event = ci_event(run, jobs, t, log=log)
            out.append(event.model_copy(update={"id": f"{rec.name}-ci"}))
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
