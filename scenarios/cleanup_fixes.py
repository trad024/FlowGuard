"""Close and delete what the D57 fix handlers created during evaluation runs.

    python scenarios/cleanup_fixes.py            # dry run: list only
    python scenarios/cleanup_fixes.py --apply    # close the issues and PRs, delete the branches

The handlers mark everything they create with flowguard-eval (agents/flowguard/github_actions.py):
issues and draft PRs carry the label, fix branches are named fix/flowguard-eval-<proposal id>.
Found: open issues and PRs with the label, open PRs whose head branch has the marker (in case the
label did not stick), and every branch with the marker. Nothing else is ever touched: dev, main
and scenario/* branches, and issues or PRs without the marker, are never listed. --apply closes
PRs first, then issues (as not planned), then deletes the branches; it carries on past a refusal
and exits 1 if anything was refused. Needs a token with contents, issues and pull-requests write
(GITHUB_TOKEN_WRITE, from the environment or .env); tests pass a fake transport.
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci")]

import deployer  # noqa: E402
from flowguard import github_actions  # noqa: E402

MARKER = github_actions.MARKER
PREFIX = f"fix/{MARKER}-"
PAGE = 100


def _get(client, path: str):
    status, data = client("GET", path, None)
    if not 200 <= status < 300:
        raise github_actions.GitHubError(f"GitHub answered {status} to GET {path}")
    return data


def _pages(client, path: str):
    page = 1
    while True:
        data = _get(client, f"{path}&per_page={PAGE}&page={page}")
        yield from data
        if len(data) < PAGE:
            return
        page += 1


def marked_branch(name) -> bool:
    if not isinstance(name, str) or not name.startswith(PREFIX):
        return False
    try:
        github_actions.check_branch(name)
    except github_actions.GitHubError:
        return False
    return True


def labelled(item: dict) -> bool:
    return any(label.get("name") == MARKER for label in item.get("labels") or [])


def find(client, repo: str) -> dict[str, list]:
    """The open PRs and issues and the branches that carry the marker, PRs and issues by number,
    branches by name."""
    pulls, issues = {}, {}
    for item in _pages(client, f"/repos/{repo}/issues?labels={MARKER}&state=open"):
        if not labelled(item):
            continue
        (pulls if "pull_request" in item else issues)[item["number"]] = item["title"]
    for pr in _pages(client, f"/repos/{repo}/pulls?state=open"):
        head = pr.get("head") or {}
        same_repo = (head.get("repo") or {}).get("full_name") == repo
        if same_repo and marked_branch(head.get("ref")) or labelled(pr):
            pulls[pr["number"]] = pr["title"]
    refs = _get(client, f"/repos/{repo}/git/matching-refs/heads/{PREFIX}")
    branches = sorted(
        name
        for ref in refs or []
        if marked_branch(name := str(ref.get("ref", "")).removeprefix("refs/heads/"))
    )
    return {"pulls": sorted(pulls.items()), "issues": sorted(issues.items()), "branches": branches}


def clean(client, repo: str, found: dict[str, list]) -> list[str]:
    """Close and delete what `find` returned; the refusals, if any."""
    steps = [
        ("PATCH", f"/repos/{repo}/pulls/{n}", {"state": "closed"}, f"close PR #{n}")
        for n, _ in found["pulls"]
    ]
    steps += [
        ("PATCH", f"/repos/{repo}/issues/{n}", {"state": "closed", "state_reason": "not_planned"},
         f"close issue #{n}")
        for n, _ in found["issues"]
    ]  # fmt: skip
    steps += [
        ("DELETE", f"/repos/{repo}/git/refs/heads/{urllib.parse.quote(b, safe='/')}", None,
         f"delete branch {b}")
        for b in found["branches"]
        if marked_branch(b)
    ]  # fmt: skip
    refused = []
    for method, path, body, what in steps:
        status, _ = client(method, path, body)
        ok = 200 <= status < 300
        print(f"{'done' if ok else 'REFUSED'} {what}" + ("" if ok else f" ({status})"))
        if not ok:
            refused.append(what)
    return refused


def main(argv: list[str] | None = None, client=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="close and delete (default: list)")
    parser.add_argument("--repo", help="owner/name (default: FLOWGUARD_REPO or the deployer's)")
    args = parser.parse_args(argv)
    repo = args.repo or deployer.load_config(ROOT / ".env").repo
    if client is None:
        env = {**deployer.read_env_file(ROOT / ".env"), **os.environ}
        client = github_actions.urllib_transport(env.get("GITHUB_TOKEN_WRITE", ""))
    found = find(client, repo)
    for n, title in found["pulls"]:
        print(f"PR #{n}: {title}")
    for n, title in found["issues"]:
        print(f"issue #{n}: {title}")
    for branch in found["branches"]:
        print(f"branch {branch}")
    total = sum(len(v) for v in found.values())
    if not args.apply:
        print(f"{total} found in {repo}; dry run, nothing changed (--apply to clean up)")
        return 0
    refused = clean(client, repo, found)
    print(f"{total - len(refused)} of {total} cleaned up in {repo}")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
