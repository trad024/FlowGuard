"""Executor: the only component that writes (to the cluster, GitHub or the deployment flow).

It runs a proposal only if the gate said execute, or said ask_human and a human approved; it
refuses everything else even if called directly (defence in depth). Every attempt is written
to the audit log. Agents never hold write credentials: kubectl writes and the GitHub write
token (GITHUB_TOKEN_WRITE) appear only in this module (agents/tests/test_executor.py checks).

dry_run=True records what would be done and changes nothing (tests, replays). Live handlers:
rollout actions through `kubectl patch` on the Argo Rollout, restarts and scaling of the
Rollout, CI reruns through the GitHub API; deployment, review and escalation decisions are
audit events the deployment flow reads. Fix PRs and dependency pins (Step 7) and plan changes
(Step 10) are not available live yet and say so.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from flowguard.contracts import ActionProposal, ExecutionResult, GateDecision

GITHUB_API = "https://api.github.com"
RECORDED = {
    "block_deployment",
    "allow_deployment",
    "allow_with_findings",
    "request_security_review",
    "escalate_to_human",
}
NOT_YET = {"pin_dependency": 7, "open_fix_pr": 7, "reorder_jobs": 10, "skip_jobs": 10}


class Refused(RuntimeError):
    """The executor was asked to run something the gate did not allow."""


def allowed(decision: GateDecision) -> bool:
    return decision.outcome == "execute" or (
        decision.outcome == "ask_human" and decision.human_verdict == "approved"
    )


class Executor:
    def __init__(
        self,
        audit=None,
        dry_run: bool = True,
        repo: str = "trad024/FlowGuard",
        token: str | None = None,
        kubectl: Callable[[list[str]], str] | None = None,
        github: Callable[[str, str], int] | None = None,
    ):
        self.audit = audit
        self.dry_run = dry_run
        self.repo = repo
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN_WRITE", "")
        self._kubectl = kubectl or self._run_kubectl
        self._github = github or self._post_github

    def execute(self, proposal: ActionProposal, decision: GateDecision) -> ExecutionResult:
        if decision.proposal_id != proposal.id or not allowed(decision):
            raise Refused(f"{proposal.id}: gate outcome {decision.outcome}, not executable")
        if self.dry_run:
            result = ExecutionResult(
                proposal_id=proposal.id, success=True, output=f"dry run: {proposal.action}"
            )
        else:
            try:
                output = self._live(proposal)
                result = ExecutionResult(proposal_id=proposal.id, success=True, output=output)
            except (subprocess.CalledProcessError, OSError, ValueError, NotImplementedError) as e:
                result = ExecutionResult(
                    proposal_id=proposal.id, success=False, output=str(e)[:500]
                )
        if self.audit is not None:
            self.audit.execution(result)
        return result

    # ------------------------------------------------------------ live handlers

    def _live(self, p: ActionProposal) -> str:
        ns, name = p.environment, p.target
        if p.action in NOT_YET:
            raise NotImplementedError(
                f"{p.action} is not available live until Step {NOT_YET[p.action]}"
            )
        if p.action in RECORDED:
            if self.audit is not None:
                self.audit.event("executor", p.action, environment=ns, target=name, params=p.params)
            return f"recorded {p.action} for {name}"
        if p.action == "rollout_abort":
            return self._patch_status(ns, name, {"status": {"abort": True}})
        if p.action == "rollout_promote":
            self._kubectl(
                [
                    "-n",
                    ns,
                    "patch",
                    "rollout",
                    name,
                    "--type",
                    "merge",
                    "-p",
                    json.dumps({"spec": {"paused": False}}),
                ]
            )
            return self._patch_status(ns, name, {"status": {"pauseConditions": None}})
        if p.action == "rollout_undo":
            return self._undo(ns, name)
        if p.action == "restart_deployment":
            at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            return self._kubectl(
                [
                    "-n",
                    ns,
                    "patch",
                    "rollout",
                    name,
                    "--type",
                    "merge",
                    "-p",
                    json.dumps({"spec": {"restartAt": at}}),
                ]
            )
        if p.action == "scale_deployment":
            replicas = int(p.params["replicas"])
            return self._kubectl(
                [
                    "-n",
                    ns,
                    "patch",
                    "rollout",
                    name,
                    "--type",
                    "merge",
                    "-p",
                    json.dumps({"spec": {"replicas": replicas}}),
                ]
            )
        if p.action == "rerun_workflow":
            return self._rerun(
                f"/repos/{self.repo}/actions/runs/{int(p.params['run_id'])}/rerun-failed-jobs"
            )
        if p.action == "retry_job":
            return self._rerun(f"/repos/{self.repo}/actions/jobs/{int(p.params['job_id'])}/rerun")
        raise NotImplementedError(f"{p.action}: no live handler (without a traffic router)")

    def _patch_status(self, ns: str, name: str, body: dict) -> str:
        return self._kubectl(
            [
                "-n",
                ns,
                "patch",
                "rollout",
                name,
                "--subresource=status",
                "--type",
                "merge",
                "-p",
                json.dumps(body),
            ]
        )

    def _undo(self, ns: str, name: str) -> str:
        """Back to the previous revision's pod template, like `kubectl argo rollouts undo`."""
        rs_list = json.loads(self._kubectl(["-n", ns, "get", "replicasets", "-o", "json"]))
        revs = sorted(
            (int(rs["metadata"]["annotations"].get("rollout.argoproj.io/revision", 0)), rs)
            for rs in rs_list["items"]
            if any(o.get("name") == name for o in rs["metadata"].get("ownerReferences", []))
        )
        if len(revs) < 2:
            raise ValueError(f"{name}: no previous revision to go back to")
        template = revs[-2][1]["spec"]["template"]
        template["metadata"].get("labels", {}).pop("rollouts-pod-template-hash", None)
        ops = [{"op": "replace", "path": "/spec/template", "value": template}]
        return self._kubectl(
            ["-n", ns, "patch", "rollout", name, "--type", "json", "-p", json.dumps(ops)]
        )

    def _rerun(self, path: str) -> str:
        status = self._github("POST", path)
        if status >= 300:
            raise ValueError(f"GitHub answered {status} for {path}")
        return f"requested {path}"

    # ------------------------------------------------------------ transports

    @staticmethod
    def _run_kubectl(args: list[str]) -> str:
        return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout

    def _post_github(self, method: str, path: str) -> int:
        if not self.token:
            raise ValueError("no GitHub write token (GITHUB_TOKEN_WRITE)")
        req = urllib.request.Request(
            GITHUB_API + path,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "flowguard-executor",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
