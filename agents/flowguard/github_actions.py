"""GitHub side of the fix actions (D57, D62): a JSON transport, the read-only lookup that plans
a fix after the diagnosis, and the pin_dependency and open_fix_pr handlers the executor calls.

The executor stays the only component that writes: it builds the transport with its write token
and calls a handler for an allowed proposal. Nothing else calls the handlers. Branch rule: the
handlers write only to fix/* and scenario/* branches, never to dev or main (`check_branch`).

Interface for executor.py:

    from flowguard import github_actions

    json_github = github_actions.urllib_transport(token)  # once, with the write token
    ...
    if p.action in github_actions.HANDLERS:  # pin_dependency, open_fix_pr
        return github_actions.HANDLERS[p.action](json_github, self.repo, p.id, p.params)

A handler returns the execution's output line and raises GitHubError (a ValueError) on a
refusal or any non-2xx answer, which the executor already records as a failed execution.
Tests pass a fake transport instead.

Planning (read-only, after the diagnosis, before the proposal): `plan_fix` finds the last
commit whose app-ci passed on the failing branch (else on dev), reads module X's version from
go.mod there and go.sum at the failing commit, and returns the proposal's action and params
(fixes.fix_action). It needs only a read token. `PlannedDiagnose` runs it on Diagnose's fix
proposals, as the orchestrator's Diagnose on the live CI path (D64).

pin_dependency writes the go.mod edit to a new branch fix/flowguard-eval-<proposal id> from the
failing commit and starts app-ci there through its workflow_dispatch (app-ci runs on push only
for dev, main and scenario/**); that run validates the pin. open_fix_pr opens an issue, or, when
it carries a pin patch, a draft PR from that branch into the failing branch.

Every branch, issue and draft PR the handlers create carries the marker flowguard-eval: in the
branch name, and as a label on issues and PRs, so a cleanup can find them all.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

from flowguard import diagnose, fixes

GITHUB_API = "https://api.github.com"
WORKFLOW = "app-ci.yml"
FIX_ACTIONS = ("pin_dependency", "open_fix_pr")
MARKER = "flowguard-eval"  # branch name prefix and label of everything the handlers create
# fix/<name> or scenario/<name>, path segments of safe characters only
BRANCH = re.compile(r"(?:fix|scenario)(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+")
SHA = re.compile(r"[0-9a-f]{40}")

Transport = Callable[[str, str, dict | None], tuple[int, Any]]


class GitHubError(ValueError):
    """GitHub refused, or the request was refused before it was sent."""


def urllib_transport(token: str, api: str = GITHUB_API, timeout: float = 30.0) -> Transport:
    """(method, path, JSON body or None) -> (status, parsed JSON answer or None)."""

    def call(method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        if not token:
            raise GitHubError("no GitHub token")
        if not path.startswith("/") or "://" in path:
            raise GitHubError(f"not an API path: {path}")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            api + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "flowguard-executor",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, _parse(r.read())
        except urllib.error.HTTPError as e:
            return e.code, _parse(e.read())

    return call


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _ok(answer: tuple[int, Any], what: str) -> Any:
    status, data = answer
    if not 200 <= status < 300:
        message = data.get("message", "") if isinstance(data, dict) else ""
        raise GitHubError(f"GitHub answered {status} to {what}: {message}"[:300])
    return data


def check_branch(name: str) -> str:
    """The branch name if a fix may write to it: fix/* or scenario/*, never dev or main."""
    if not isinstance(name, str) or not BRANCH.fullmatch(name) or ".." in name:
        raise GitHubError(f"{name!r}: fixes write only to fix/* and scenario/* branches")
    if name.endswith((".lock", ".")):
        raise GitHubError(f"{name!r}: not a valid branch name")
    return name


def fix_branch(proposal_id: str) -> str:
    """The branch a fix for this proposal is committed on, with the marker in its name."""
    return check_branch(f"fix/{MARKER}-{proposal_id}")


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise GitHubError(f"{value!r}: not a commit SHA")
    return value


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


# ---------------------------------------------------------------- reads (planning)


def read_file(client: Transport, repo: str, path: str, ref: str) -> tuple[str, str]:
    """(text, blob SHA) of a repository file at a commit or branch."""
    data = _ok(
        client("GET", f"/repos/{repo}/contents/{urllib.parse.quote(path)}?ref={_q(ref)}", None),
        f"read {path} at {ref}",
    )
    if not isinstance(data, dict) or data.get("type") != "file" or "content" not in data:
        raise GitHubError(f"{path} at {ref} is not a file")
    return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]


def last_green(client: Transport, repo: str, branches: Iterable[str]) -> str | None:
    """The head commit of the latest successful app-ci run on the first branch that has one."""
    for branch in branches:
        data = _ok(
            client(
                "GET",
                f"/repos/{repo}/actions/workflows/{WORKFLOW}/runs"
                f"?branch={_q(branch)}&status=success&per_page=1",
                None,
            ),
            f"list successful {WORKFLOW} runs on {branch}",
        )
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if runs:
            return _sha(runs[0].get("head_sha"))
    return None


def plan_fix(
    client: Transport,
    repo: str,
    action: str,
    root_cause: str,
    evidence: Iterable[str],
    fix: str,
    head_sha: str,
    head_branch: str,
) -> tuple[str, dict]:
    """The proposal's action and params under D57, with Y resolved after the diagnosis: each
    module the evidence names, at its version in go.mod at the last commit whose app-ci passed
    (on the failing branch, else on dev). A module not found there, or a lookup GitHub refuses,
    gives no pin: the diagnosis goes out as an issue."""
    evidence = list(evidence)
    if action not in FIX_ACTIONS:
        return action, {}
    used: dict[str, str] = {}
    go_sum = ""
    if root_cause == "dependency" and fixes.named(evidence):
        try:
            good = last_green(client, repo, (head_branch, "dev"))
            if good:
                go_mod, _ = read_file(client, repo, fixes.GO_MOD, good)
                for module, _version in fixes.named(evidence):
                    if (version := fixes.required(go_mod, module)) is not None:
                        used[module] = version
            if used:
                go_sum, _ = read_file(client, repo, fixes.GO_SUM, _sha(head_sha))
        except GitHubError:
            used = {}
    action, params = fixes.fix_action(action, root_cause, evidence, fix, used, go_sum)
    if params.get("kind") == "draft_pr" and not BRANCH.fullmatch(str(head_branch)):
        params = fixes.request(root_cause, evidence, fix)  # no PR into dev or main: an issue
    if action == "pin_dependency" or params.get("kind") == "draft_pr":
        params = {**params, "head_sha": head_sha, "base_branch": head_branch}
    return action, params


ROOT_CAUSE = re.compile(r"root cause (\w+);")


class PlannedDiagnose:
    """Diagnose, then D57's planning of its fix (D64): a pin_dependency or open_fix_pr proposal
    gets its action and params from `plan_fix` with a read-only client, from the CI run's head
    commit and branch in its params. Y is resolved here, after the diagnosis, never shown to
    Diagnose. Other proposals pass unchanged, and the proposal keeps its id, so its label
    scores still apply at the gate. Without a head commit, or when GitHub cannot be reached,
    the diagnosis goes out as an issue. The orchestrator takes it as its `diagnose`."""

    def __init__(self, diagnose, client: Transport, repo: str):
        self.diagnose, self.client, self.repo = diagnose, client, repo

    def missing(self, cfg) -> str | None:
        return self.diagnose.missing(cfg)

    def run(self, inp, cfg):
        out = self.diagnose.run(inp, cfg)
        planned = [self._plan(p) if p.action in FIX_ACTIONS else p for p in out.proposals]
        return dataclasses.replace(out, proposals=planned)

    def _plan(self, p):
        params = p.params
        found = ROOT_CAUSE.match(p.rationale)
        root_cause = str(params.get("diagnose_root_cause") or (found and found[1]) or "unclear")
        evidence = [str(q) for q in params.get("diagnose_quotes") or []]
        fix = str(params.get("diagnose_fix") or diagnose.ACTIONS.get(p.action, p.action))
        head_sha, head_branch = params.get("head_sha"), params.get("head_branch")
        action, planned = "open_fix_pr", fixes.request(root_cause, evidence, fix)
        if isinstance(head_sha, str) and isinstance(head_branch, str) and head_branch:
            with contextlib.suppress(OSError):  # GitHub unreachable: no pin without its lookup
                action, planned = plan_fix(
                    self.client, self.repo, p.action, root_cause, evidence, fix, head_sha,
                    head_branch,
                )  # fmt: skip
        return p.model_copy(update={"action": action, "params": {**params, **planned}})


# ---------------------------------------------------------------- writes (the executor only)


def _commit_pin(client: Transport, repo: str, branch: str, head_sha: str, patch: dict) -> str:
    """Create `branch` at the failing commit and commit the go.mod edit on it; its SHA."""
    if patch.get("file") != fixes.GO_MOD:
        raise GitHubError(f"a pin edits {fixes.GO_MOD} only, not {patch.get('file')!r}")
    module, version = str(patch.get("module")), str(patch.get("version"))
    text, blob = read_file(client, repo, fixes.GO_MOD, head_sha)
    try:
        edited = fixes.set_version(text, module, version)
    except ValueError as e:
        raise GitHubError(str(e)) from e
    ref = {"ref": f"refs/heads/{branch}", "sha": head_sha}
    _ok(client("POST", f"/repos/{repo}/git/refs", ref), f"create {branch}")
    body = {
        "message": f"Set {module} to {version}",
        "content": base64.b64encode(edited.encode("utf-8")).decode("ascii"),
        "sha": blob,
        "branch": branch,
    }
    path = urllib.parse.quote(fixes.GO_MOD)
    data = _ok(client("PUT", f"/repos/{repo}/contents/{path}", body), f"commit on {branch}")
    return str((data or {}).get("commit", {}).get("sha", ""))


def pin_dependency(client: Transport, repo: str, proposal_id: str, params: dict) -> str:
    branch = fix_branch(proposal_id)
    head = _sha(params.get("head_sha"))
    sha = _commit_pin(client, repo, branch, head, params)
    dispatch = {"ref": branch}
    _ok(
        client("POST", f"/repos/{repo}/actions/workflows/{WORKFLOW}/dispatches", dispatch),
        f"start {WORKFLOW} on {branch}",
    )
    return f"set {params['module']} to {params['version']} on {branch} ({sha[:12]}); app-ci started"


def open_fix_pr(client: Transport, repo: str, proposal_id: str, params: dict) -> str:
    kind = params.get("kind")
    text = {"title": str(params.get("title", ""))[:200], "body": str(params.get("body", ""))}
    if not text["title"] or not text["body"]:
        raise GitHubError("a fix request needs a title and a body")
    if kind == "issue":
        issue = {**text, "labels": [MARKER]}
        data = _ok(client("POST", f"/repos/{repo}/issues", issue), "open an issue")
        return f"opened issue #{data.get('number')}"
    if kind != "draft_pr" or not isinstance(params.get("patch"), dict):
        raise GitHubError(f"unknown fix request kind {kind!r}")
    base = check_branch(params.get("base_branch"))
    branch = fix_branch(proposal_id)
    _commit_pin(client, repo, branch, _sha(params.get("head_sha")), params["patch"])
    pull = {**text, "head": branch, "base": base, "draft": True}
    data = _ok(client("POST", f"/repos/{repo}/pulls", pull), f"open a draft PR into {base}")
    number = data.get("number") if isinstance(data, dict) else None
    if type(number) is not int:
        raise GitHubError(f"GitHub gave no number for the draft PR into {base}")
    # A PR takes labels only through the issues API; its head branch already has the marker
    label = {"labels": [MARKER]}
    _ok(client("POST", f"/repos/{repo}/issues/{number}/labels", label), f"label PR #{number}")
    return f"opened draft PR #{number} from {branch} into {base}"


HANDLERS: dict[str, Callable[[Transport, str, str, dict], str]] = {
    "pin_dependency": pin_dependency,
    "open_fix_pr": open_fix_pr,
}
