"""Collect read-only app-ci metadata; never create a branch, run, commit or deployment."""

import argparse
import concurrent.futures
import json
import re
import subprocess
from pathlib import Path

DEV_PREFIXES = (
    "scenario/ci-",
    "scenario/security-",
    "scenario/release-",
    "scenario/baseline/",
    "scenario/step3-",
    "scenario/optimizer/",
)


def api(path):
    return json.loads(subprocess.check_output(["gh", "api", path], text=True, encoding="utf-8"))


def collect(repo, get=api):
    runs = []
    page = 1
    while True:
        batch = get(f"repos/{repo}/actions/workflows/app-ci.yml/runs?per_page=100&page={page}")[
            "workflow_runs"
        ]
        runs.extend(
            r
            for r in batch
            if r["status"] == "completed"
            and (r["head_branch"] == "dev" or r["head_branch"].startswith(DEV_PREFIXES))
        )
        if len(batch) < 100:
            break
        page += 1

    def one(run):
        commit = get(f"repos/{repo}/commits/{run['head_sha']}?per_page=100")
        files = list(commit["files"])
        file_page = 2
        while len(commit["files"]) == 100:
            commit = get(f"repos/{repo}/commits/{run['head_sha']}?per_page=100&page={file_page}")
            files.extend(commit["files"])
            file_page += 1
        jobs = get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        status = "unknown"
        for job in jobs:
            if job["name"] != "scans" or job["conclusion"] != "success":
                continue
            notes = get(f"repos/{repo}/check-runs/{job['id']}/annotations?per_page=100")
            for note in notes:
                if note.get("title") != "scan-counts":
                    continue
                counts = dict(
                    re.findall(r"\b(trivy_high|trivy_critical|gitleaks)=(\d+)\b", note["message"])
                )
                if set(counts) == {"trivy_high", "trivy_critical", "gitleaks"}:
                    status = "flagged" if any(int(v) > 0 for v in counts.values()) else "clean"
        return {
            "run_id": run["id"],
            "sha": run["head_sha"],
            "branch": run["head_branch"],
            "created_at": run["created_at"],
            "security_status": status,
            "files": [
                {k: f.get(k) for k in ("filename", "additions", "deletions", "status")}
                for f in files
            ],
            "jobs": [
                {k: j.get(k) for k in ("name", "conclusion", "started_at", "completed_at")}
                for j in jobs
            ],
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        rows = sorted(pool.map(one, runs), key=lambda r: (r["created_at"], r["run_id"]))
    previous = {}
    for row in rows:
        row["previous_security"] = previous.get(row["branch"], "unknown")
        previous[row["branch"]] = row["security_status"]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="trad024/FlowGuard")
    parser.add_argument("--output", type=Path, default=Path("ci/optimizer/dev-history.jsonl"))
    args = parser.parse_args()
    rows = collect(args.repo)
    args.output.write_bytes(
        ("\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n").encode()
    )
    print(f"Collected {len(rows)} app-ci development runs (read-only)")


if __name__ == "__main__":
    main()
