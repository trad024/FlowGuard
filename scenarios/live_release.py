"""Live Release on the cluster (Step 8, D44): one real, human-approved rollout_undo.

    python scenarios/live_release.py undo [--port 8765] [--timeout-min 60]

Staging must run the agents-mode canary (D53; `pilot.py mode agents`) and be Healthy. A bad
release (5% errors, a release Secret like any config release) starts a canary; at its first
indefinite pause the harness aborts it, as an operator would, so that the undo is the
executor's first live action. Release then sees an aborted Rollout and proposes rollout_undo
(high tier): the gate asks a human on the approval page (127.0.0.1 only; no simulated
approver), and on approval the live executor patches the Rollout back to the previous
revision. The same proposal and decision are then handed to the executor again, which must
refuse them (already executed, D37). The audit rows of the proposal are printed. Without an
approval the template is restored by the harness, so staging is never left aborted.
Kept as the one-off D44 script; for anything new, an aborted Rollout is followed up by the
agent service (`flowguard.service`, its aborted-Rollout path), which supersedes this one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci"), str(ROOT / "evaluation")]

import deployer  # noqa: E402
import recorder  # noqa: E402
import runner  # noqa: E402
from flowguard import release, series  # noqa: E402
from flowguard.approval import ApprovalPage  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.contracts import AgentName  # noqa: E402
from flowguard.executor import Executor, Refused  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402
from flowguard.orchestrator import Orchestrator, level  # noqa: E402

ENV = "staging"
FAULT = {"kind": "http_errors", "delivery": "config", "error_rate": 0.05}
LIVE = ("podinfo_requests", "podinfo_latency_buckets", "podinfo_up", "pod_restarts")


def say(**kv) -> None:
    print(json.dumps(kv, default=str), flush=True)


def live_index(seconds: int) -> series.Index:
    """The series Release reads, as raw samples over the last `seconds` (as a recording has)."""
    raw = {}
    for name in LIVE:
        query = f"{recorder.SELECTORS[name]}[{seconds}s]"
        url = f"{recorder.PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": query})
        raw[name] = recorder.http_json(url).get("data", {}).get("result", [])
    return series.index(raw)


def wait_for(cluster, test, timeout: float, what: str) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rollout = cluster.rollout()
        if test(rollout):
            return rollout
        time.sleep(5)
    raise TimeoutError(f"{what}: not reached in {timeout:.0f} s")


def undo(port: int, timeout_min: float) -> int:
    cluster = deployer.Cluster(ENV)
    rollout = cluster.rollout()
    status = rollout.get("status", {})
    if runner.canary_mode(rollout) != "agents" or status.get("phase") != "Healthy":
        raise SystemExit("staging must be Healthy and in agents-mode (pilot.py mode agents)")
    saved = deployer.container(rollout)
    image, command = saved["image"], list(saved.get("command") or runner.BASE_COMMAND)
    stable = status["stableRS"]
    audit = AuditLog.open(level="L3")
    load = deployer.LoadGenerator(ENV)
    load.start(f"live-undo-{runner.new_run_id()}")
    page = ApprovalPage(port=port, timeout_s=timeout_min * 60)
    approved = False
    try:
        time.sleep(30)
        secret = deployer.create_release_settings(ENV, runner.release_settings(FAULT))
        since = time.time()
        runner.patch_template(ENV, image, command, runner.env_from(secret))
        audit.event("deployer", "deploy_started", environment=ENV, image=image)
        rollout = wait_for(cluster, runner.indefinite_pause, 600, "first indefinite pause")
        canary = rollout["status"]["currentPodHash"]
        say(step="canary paused indefinitely", canary=canary, stable=stable)
        runner.kubectl(
            "-n", ENV, "patch", "rollout", "podinfo", "--subresource=status", "--type", "merge",
            "-p", '{"status":{"abort":true}}',
        )  # fmt: skip
        audit.event("operator", "rollout_aborted", environment=ENV, canary=canary)
        wait_for(
            cluster,
            lambda r: r.get("status", {}).get("abort") and r["status"].get("phase") == "Degraded",
            300,
            "aborted",
        )
        say(step="canary aborted by the harness; Release decides what follows")
        t = time.time()
        event = release.step_event(
            live_index(int(t - since) + 120), t, ENV, canary, stable, since, 1,
            weight=20, aborted=True,
        )  # fmt: skip
        orch = Orchestrator(
            cfg=level("L3"),
            record=IncidentRecord(audit.path),
            executor=Executor(audit=audit, dry_run=False),
            audit=audit,
            approver=page,
            calibrators={AgentName.RELEASE: release.calibrator()},
        )
        say(step="waiting for a human", approval_page=page.url, timeout_min=timeout_min)
        out = orch.handle(event)
        (proposal,) = out["output"].proposals
        (decision,) = out["decisions"]
        say(
            proposal=proposal.action,
            rationale=proposal.rationale,
            gate=decision.outcome,
            tier=str(decision.tier),
            verdict=decision.human_verdict,
            approved_by=decision.approved_by,
            results=[r.model_dump(mode="json") for r in out.get("results", [])],
        )
        approved = decision.human_verdict == "approved"
        if approved:
            back = wait_for(
                cluster,
                lambda r: r.get("status", {}).get("phase") == "Healthy",
                600,
                "Healthy after the undo",
            )
            say(
                step="after the undo",
                phase=back["status"]["phase"],
                stable=back["status"]["stableRS"],
                back_on_previous=back["status"]["stableRS"] == stable,
                template_is_previous=deployer.container(back).get("envFrom")
                == saved.get("envFrom"),
            )
            try:  # D37: the same decision handed over again
                orch.executor.execute(proposal, decision)
                say(replay="EXECUTED AGAIN (wrong)")
            except Refused as exc:
                say(replay="refused", reason=str(exc))
        for table in ("proposals", "gate_decisions", "approvals", "executions"):
            key = "id" if table == "proposals" else "proposal_id"
            rows = audit.query(f"SELECT * FROM {table} WHERE {key} = ?", (proposal.id,))
            say(audit_table=table, rows=rows)
    finally:
        page.close()
        if not approved:  # never leave staging aborted
            runner.patch_template(ENV, image, command, saved.get("envFrom"))
            outcome = runner.pace_canary(cluster, image, 900, lambda: runner.resume(ENV), 0)
            say(step="restored by the harness", outcome=outcome)
        load.stop()
        say(release_settings_deleted=cluster.gc_release_settings())
    return 0 if approved else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("undo")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--timeout-min", type=float, default=60)
    args = parser.parse_args(argv)
    return undo(args.port, args.timeout_min)


if __name__ == "__main__":
    sys.exit(main())
