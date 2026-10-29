"""Read the previous completed app-ci scan annotation; unknown always fails closed.

This adapter has read-only GitHub access. Tests inject its API and never use the network.
"""

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


def api(path):
    request = urllib.request.Request(
        "https://api.github.com/" + path,
        headers={
            "Authorization": "Bearer " + os.environ.get("GITHUB_TOKEN", ""),
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def previous_status(repo, branch, current_sha, get=api):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return "unknown"
    try:
        runs = get(f"repos/{repo}/actions/workflows/app-ci.yml/runs?per_page=100")["workflow_runs"]
        prior = [
            r
            for r in runs
            if r["head_branch"] == branch
            and r["head_sha"] != current_sha
            and r["status"] == "completed"
        ]
        if not prior:
            return "unknown"
        run = max(prior, key=lambda r: r["id"])
        jobs = get(f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        scan = next(
            (j for j in jobs if j["name"] == "scans" and j["conclusion"] == "success"), None
        )
        if scan is None:
            return "unknown"
        notes = get(f"repos/{repo}/check-runs/{scan['id']}/annotations?per_page=100")
        for note in notes:
            if note.get("title") != "scan-counts":
                continue
            counts = dict(
                re.findall(r"\b(trivy_high|trivy_critical|gitleaks)=(\d+)\b", note["message"])
            )
            if set(counts) != {"trivy_high", "trivy_critical", "gitleaks"}:
                return "unknown"
            return "flagged" if any(int(v) > 0 for v in counts.values()) else "clean"
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        pass
    return "unknown"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    status = previous_status(args.repo, args.branch, args.sha)
    print(status)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as out:
            out.write(f"status={status}\n")


if __name__ == "__main__":
    main()
