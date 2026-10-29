"""Development history preprocessing without training-library dependencies."""

from datetime import datetime

from optimizer.features import JOBS, extract


def job_name(name):
    if name in JOBS:
        return name
    if name.startswith("optional (") and name.endswith(")"):
        name = name[10:-1]
        return name if name in JOBS else None
    return None


def observations(runs):
    rows = []
    for run in runs:
        paths = [f["filename"] for f in run["files"]]
        stats = {f["filename"]: f for f in run["files"]}
        for job in run["jobs"]:
            name = job_name(job["name"])
            if name is None or job["conclusion"] not in ("success", "failure"):
                continue  # skipped/cancelled jobs are unobserved, never successful labels
            if not job["started_at"] or not job["completed_at"]:
                continue
            duration = (
                datetime.fromisoformat(job["completed_at"])
                - datetime.fromisoformat(job["started_at"])
            ).total_seconds()
            if duration < 0:
                continue
            rows.append(
                {
                    "run_id": run["run_id"],
                    "job": name,
                    "x": extract(paths, name, stats),
                    "duration": duration,
                    "failure": int(job["conclusion"] == "failure"),
                }
            )
    return rows


def split_runs(runs):
    # A repeated commit may not occur on both sides. Keep its latest recorded run only.
    latest = {r["sha"]: r for r in sorted(runs, key=lambda r: (r["created_at"], r["run_id"]))}
    runs = sorted(latest.values(), key=lambda r: (r["created_at"], r["run_id"]))
    cut = int(len(runs) * 0.6)
    if cut < 8 or len(runs) - cut < 4:
        raise ValueError("insufficient distinct development commits")
    return runs[:cut], runs[cut:]
