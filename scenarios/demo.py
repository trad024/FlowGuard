"""Live demo driver (docs/demo.md): the operator's side of the two demo loops.

    python scenarios/demo.py runtime [--pods 0.5] [--error-rate 0.5] [--hold 300]
    python scenarios/demo.py prod-release [--timeout 900]
    python scenarios/demo.py status [--last 3]

runtime       waits until no L0 alert has fired for 2 minutes (D50), runs staging load, and after
              a 60 s warm-up turns an error burst on in a share of the stable pods (their /fault
              endpoint, enabled in staging only); holds it, clears it, keeps the load for a
              120 s cool-down. The fault is cleared even if the script is interrupted.
prod-release  a new prod revision (same image, an empty release Secret: a good release), held at
              its first canary step (spec.paused) once the canary pods serve 20%, so the agents'
              prod release goes to a human (D23). Runs prod load until the rollout is promoted,
              aborted or the timeout passes.
status        the latest incidents with their evidence, proposals, gate outcomes, approvals and
              executions, from the audit log.

Experiment side: it injects faults and the agents never see it (D25). It writes to the cluster
the way the scenario runner does; the agent service itself only reads.
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scenarios"), str(ROOT / "ci"), str(ROOT / "agents")]

import deployer  # noqa: E402
import runner  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402

WARMUP_S, COOLDOWN_S = 60, 120


def log(**fields) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    print(json.dumps({"ts": now, **fields}), flush=True)


def set_fault(pod: str, settings: dict | None) -> bool:
    """PUT (or DELETE) a staging pod's /fault endpoint on its metrics port, as the runner does."""
    curl = ["curl", "-sf", "-m", "5", "http://localhost:9797/fault"]
    curl += ["-X", "DELETE"] if settings is None else ["-X", "PUT", "-d", json.dumps(settings)]
    p = subprocess.run(
        ["kubectl", "-n", "staging", "exec", pod, "-c", "podinfo", "--", *curl],
        capture_output=True,
        text=True,
    )
    return p.returncode == 0


def runtime(pods_share: float, error_rate: float, hold_s: int, sleep=time.sleep) -> int:
    still = runner.wait_alerts_quiet()
    if still:
        log(step="refused", reason="L0 alerts still firing (D50)", alerts=still)
        return 1
    stable = deployer.Cluster("staging").rollout()["status"]["stableRS"]
    names = sorted(p["name"] for p in runner.stable_pods("staging", stable))
    targets = names[: max(1, math.ceil(len(names) * pods_share))]
    load = deployer.LoadGenerator("staging")
    load.start(f"demo-{secrets.token_hex(3)}")
    log(step="load_started", namespace="staging", warmup_s=WARMUP_S)
    try:
        sleep(WARMUP_S)
        settings = runner.runtime_settings({"error_rate": error_rate})
        applied = [p for p in targets if set_fault(p, settings)]
        log(step="fault_on", pods=applied, of=len(names), error_rate=error_rate, hold_s=hold_s)
        sleep(hold_s)
        cleared = [p for p in targets if set_fault(p, None)]
        log(step="fault_off", pods=cleared, cooldown_s=COOLDOWN_S)
        sleep(COOLDOWN_S)
    finally:
        for p in targets:  # idempotent: never leave a fault behind
            set_fault(p, None)
        load.stop()
        log(step="load_stopped")
    return 0


def pause(namespace: str) -> None:
    runner.kubectl(
        "-n", namespace, "patch", "rollout", "podinfo", "--type", "merge",
        "-p", json.dumps({"spec": {"paused": True}}),
    )  # fmt: skip


def prod_release(timeout_s: int, sleep=time.sleep, clock=time.monotonic) -> int:
    cluster = deployer.Cluster("prod")
    rollout = cluster.rollout()
    stable = rollout["status"]["stableRS"]
    current = deployer.container(rollout)
    image, command = current["image"], list(current.get("command") or runner.BASE_COMMAND)
    load = deployer.LoadGenerator("prod")
    load.start(f"demo-{secrets.token_hex(3)}")
    try:
        secret = deployer.create_release_settings("prod", {})
        runner.patch_template("prod", image, command, runner.env_from(secret))
        log(step="released", namespace="prod", stable=stable)
        deadline = clock() + 180
        while True:
            status = cluster.rollout().get("status", {})
            if (
                status.get("currentPodHash") not in (None, stable)
                and (status.get("currentStepIndex") or 0) >= 1
            ):
                break
            if clock() > deadline:
                log(step="refused", reason="the canary did not reach its first pause in 180 s")
                return 1
            sleep(2)
        pause("prod")
        canary = status["currentPodHash"]
        log(step="held", canary=canary, weight=20, next="the agent service asks a human")
        deadline = clock() + timeout_s
        while clock() < deadline:
            status = cluster.rollout().get("status", {})
            if status.get("abort"):
                log(step="aborted", canary=canary)
                return 0
            if status.get("stableRS") == canary and status.get("phase") == "Healthy":
                log(step="promoted", canary=canary)
                return 0
            sleep(10)
        log(step="timeout", canary=canary, hint="still held: approve or deny on the page")
        return 1
    finally:
        load.stop()
        log(step="load_stopped")


def status(last: int, path: Path | None = None) -> list[dict]:
    """The latest incidents, each with its evidence and what became of its proposals."""
    audit = AuditLog(path) if path else AuditLog.open()
    with closing(sqlite3.connect(audit.path)) as db:
        db.row_factory = sqlite3.Row
        try:
            incidents = db.execute(
                "SELECT id, opened_at, status, body FROM incidents ORDER BY opened_at DESC LIMIT ?",
                (last,),
            ).fetchall()
        except sqlite3.OperationalError:
            incidents = []
        out = []
        for inc in incidents:
            body = json.loads(inc["body"])
            evidence = db.execute(
                "SELECT agent, claim FROM evidence WHERE incident_id = ? ORDER BY id", (inc["id"],)
            ).fetchall()
            proposals = db.execute(
                "SELECT p.id, p.agent, p.action, p.environment,"
                " (SELECT outcome FROM gate_decisions g WHERE g.proposal_id = p.id) AS gate,"
                " (SELECT verdict FROM approvals a WHERE a.proposal_id = p.id"
                "  ORDER BY a.id DESC LIMIT 1) AS approval,"
                " (SELECT success FROM executions e WHERE e.proposal_id = p.id) AS executed"
                " FROM proposals p WHERE p.incident_id = ? ORDER BY p.ts",
                (inc["id"],),
            ).fetchall()
            out.append(
                {
                    "incident": inc["id"],
                    "opened_at": inc["opened_at"],
                    "status": inc["status"],
                    "detector": body.get("detector"),
                    "environment": body.get("environment"),
                    "evidence": [f"{e['agent']}: {e['claim']}" for e in evidence],
                    "proposals": [dict(p) for p in proposals],
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("runtime")
    r.add_argument("--pods", type=float, default=0.5, help="share of the stable pods")
    r.add_argument("--error-rate", type=float, default=0.5)
    r.add_argument("--hold", type=int, default=300)
    p = sub.add_parser("prod-release")
    p.add_argument("--timeout", type=int, default=900)
    s = sub.add_parser("status")
    s.add_argument("--last", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "runtime":
        return runtime(args.pods, args.error_rate, args.hold)
    if args.command == "prod-release":
        return prod_release(args.timeout)
    print(json.dumps(status(args.last), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
