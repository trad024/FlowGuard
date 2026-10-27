"""Laptop deployer (D1, D23, DSO4 baseline): polls GitHub for successful app-ci runs and
releases each new image to staging, then to prod. No webhooks: GitHub cannot reach the laptop.

On every poll, for the newest successful run on the branch:
  1. L0 security gate (DSO4 baseline): the run's scans job publishes its counts as a
     "scan-counts" annotation. Any HIGH or CRITICAL Trivy finding or any Gitleaks finding, or
     no counts at all, blocks the image: it is logged as a security decision and not deployed.
  2. Staging canary: with the load generator running (L0 analysis needs traffic) it deploys the
     image with infra/scripts/deploy-version and waits until the canary is promoted or aborted.
  3. Prod canary (D23, L0): after a promoted staging canary, the same image goes through the
     prod canary the same way. In agents mode (Step 5) Release proposes this and the gate sends
     it to a human; at L0 the rule releases it.
Which commit each environment runs is read back from its Rollout, so the deployer keeps no
state: a restart never deploys twice and picks up a pending prod release; an aborted commit
stays aborted until a newer one succeeds. Every step goes to evaluation/runs/deployer-events.jsonl
and to the audit log (agents/flowguard/audit.py).

    python ci/deployer.py run              # poll forever (Ctrl+C to stop)
    python ci/deployer.py run --once       # one poll, deploy if needed, then exit
    python ci/deployer.py --branch scenario/<id> --no-prod run   # a scenario branch, staging only
    python ci/deployer.py status --sha SHA # the app-ci run for a commit, as JSON
    python ci/deployer.py gate --sha SHA   # its scan counts and the L0 security verdict

Configuration comes from the environment or from .env at the repo root (never committed):
GITHUB_TOKEN_READ (optional read-only token; without it GitHub allows 60 requests per hour),
FLOWGUARD_REPO, FLOWGUARD_BRANCH, FLOWGUARD_WORKFLOW, FLOWGUARD_IMAGE, FLOWGUARD_POLL_SECONDS.
Standard library plus kubectl, like the other laptop scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))
from flowguard.audit import AuditLog  # noqa: E402 (needs the path above)

SCRIPTS = ROOT / "infra" / "scripts"
RUNS_DIR = ROOT / "evaluation" / "runs"
PORTS = {"staging": 9898, "prod": 9899}
WORKFLOW_PATH = ".github/workflows/app-ci.yml"
SCAN_KEYS = ("trivy_high", "trivy_critical", "gitleaks")


@dataclass
class Config:
    repo: str = "trad024/FlowGuard"
    branch: str = "dev"
    workflow: str = "app-ci.yml"
    image: str = "ghcr.io/trad024/flowguard-podinfo"
    token: str = ""
    poll_seconds: int = 60
    rollout_timeout: int = 1200
    release_to_prod: bool = True
    api_url: str = "https://api.github.com"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments ignored; surrounding quotes stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_config(env_file: Path) -> Config:
    """The process environment wins over .env, which wins over the defaults."""
    env = {**read_env_file(env_file), **os.environ}
    cfg = Config(token=env.get("GITHUB_TOKEN_READ", ""))
    cfg.repo = env.get("FLOWGUARD_REPO") or cfg.repo
    cfg.branch = env.get("FLOWGUARD_BRANCH") or cfg.branch
    cfg.workflow = env.get("FLOWGUARD_WORKFLOW") or cfg.workflow
    cfg.image = env.get("FLOWGUARD_IMAGE") or cfg.image
    # Unauthenticated polling is capped at 60 requests per hour
    default_poll = 30 if cfg.token else 60
    cfg.poll_seconds = int(env.get("FLOWGUARD_POLL_SECONDS") or default_poll)
    return cfg


def parse_scan_counts(message: str) -> dict[str, int] | None:
    """'trivy_high=0 trivy_critical=0 gitleaks=0' -> counts; None unless all three are there."""
    found = dict(re.findall(r"(\w+)=(\d+)", message or ""))
    if not all(k in found for k in SCAN_KEYS):
        return None
    return {k: int(found[k]) for k in SCAN_KEYS}


def security_verdict(counts: dict[str, int] | None) -> tuple[str, str]:
    """L0 security gate (DSO4 baseline): block on any finding; no counts also blocks."""
    if counts is None:
        return "block", "no scan counts for this run"
    findings = [f"{k}={v}" for k, v in counts.items() if v > 0]
    if findings:
        return "block", "findings: " + ", ".join(findings)
    return "allow", "no HIGH, CRITICAL or Gitleaks finding"


class RateLimited(Exception):
    def __init__(self, reset_at: int):
        super().__init__(f"GitHub rate limit reached, resets at {reset_at}")
        self.reset_at = reset_at


class GitHub:
    """Minimal read-only GitHub REST client with ETag caching: an unchanged answer comes back
    as 304, which GitHub does not count against the rate limit."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: dict[str, tuple[str, dict]] = {}

    def get(self, path: str):
        url = self.cfg.api_url.rstrip("/") + path
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "flowguard-deployer",
        }
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        cached = self._cache.get(url)
        if cached:
            headers["If-None-Match"] = cached[0]
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=20
            ) as r:
                data = json.load(r)
                if r.headers.get("ETag"):
                    self._cache[url] = (r.headers["ETag"], data)
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cached:
                return cached[1]
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                raise RateLimited(int(exc.headers.get("X-RateLimit-Reset", "0"))) from exc
            raise

    def latest_success(self) -> dict | None:
        """Newest successful app-ci run on the branch (its image was pushed by the last job)."""
        c = self.cfg
        data = self.get(
            f"/repos/{c.repo}/actions/workflows/{c.workflow}/runs"
            f"?branch={urllib.parse.quote(c.branch, safe='')}&status=success&per_page=10"
        )
        runs = [
            r
            for r in data.get("workflow_runs", [])
            if r.get("conclusion") == "success" and r.get("event") in ("push", "workflow_dispatch")
        ]
        return max(runs, key=lambda r: r["run_number"], default=None)

    def run_for_commit(self, sha: str) -> dict | None:
        """The newest app-ci run for a commit, or None if the commit triggered none."""
        data = self.get(f"/repos/{self.cfg.repo}/actions/runs?head_sha={sha}&per_page=20")
        runs = [r for r in data.get("workflow_runs", []) if r.get("path") == WORKFLOW_PATH]
        return max(runs, key=lambda r: r["run_number"], default=None)

    def scan_counts(self, run_id: int) -> dict[str, int] | None:
        """The counts the run's scans job published as its "scan-counts" annotation."""
        jobs = self.get(f"/repos/{self.cfg.repo}/actions/runs/{run_id}/jobs?per_page=50")
        for job in jobs.get("jobs", []):
            if job.get("name") != "scans":
                continue
            notes = self.get(f"/repos/{self.cfg.repo}/check-runs/{job['id']}/annotations")
            for note in notes:
                if note.get("title") == "scan-counts":
                    return parse_scan_counts(note.get("message", ""))
        return None


class Cluster:
    """kubectl and the deploy-version script for one namespace, behind a small interface."""

    def __init__(self, namespace: str):
        self.namespace = namespace

    def rollout(self) -> dict:
        out = subprocess.run(
            ["kubectl", "-n", self.namespace, "get", "rollout", "podinfo", "-o", "json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return json.loads(out)

    def deploy(self, image: str, message: str) -> None:
        if os.name == "nt":
            script = str(SCRIPTS / "deploy-version.ps1")
            cmd = ["powershell", "-NoProfile", "-File", script, "good"]
            cmd += ["-Namespace", self.namespace, "-Message", message, "-Image", image]
        else:
            script = str(SCRIPTS / "deploy-version.sh")
            cmd = ["bash", script, "good", self.namespace, message, image]
        subprocess.run(cmd, check=True, capture_output=True, text=True)


class LoadGenerator:
    """Runs evaluation/loadgen.py against one namespace for the length of one canary."""

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._proc: subprocess.Popen | None = None

    def start(self, label: str) -> Path:
        out = RUNS_DIR / f"deploy-{self.namespace}-{label}.jsonl"
        target = f"{self.namespace}=http://localhost:{PORTS[self.namespace]}/"
        self._proc = subprocess.Popen(
            [sys.executable, str(ROOT / "evaluation" / "loadgen.py"), "run"]
            + ["--target", target, "--out", str(out)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return out

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc.wait(timeout=10)
        self._proc = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class EventLog:
    """Each event goes to the JSONL file, to stdout and to the audit log."""

    def __init__(self, path: Path, audit: AuditLog | None = None):
        self.path = path
        self.audit = audit

    def write(self, event: str, source: str = "deployer", **fields) -> dict:
        record = {"ts": utc_now(), "event": event, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record) + "\n")
        print(json.dumps(record), flush=True)
        if self.audit:
            payload = {k: v for k, v in fields.items() if k not in ("environment", "sha")}
            self.audit.event(
                source,
                event,
                environment=fields.get("environment"),
                commit_sha=fields.get("sha"),
                ts=record["ts"],
                **payload,
            )
        return record


def container(rollout: dict) -> dict:
    return rollout["spec"]["template"]["spec"]["containers"][0]


def deployed_sha(rollout: dict, image_repo: str) -> str | None:
    """The commit the Rollout's spec points to, if its image comes from the CI registry."""
    image = container(rollout)["image"]
    repo, sep, tag = image.rpartition(":")
    return tag if sep and repo == image_repo else None


def rollout_outcome(rollout: dict, image: str) -> str | None:
    """'promoted', 'aborted', or None while the canary for `image` is still in progress."""
    status = rollout.get("status", {})
    if container(rollout)["image"] != image:
        return "superseded"
    if status.get("abort"):
        return "aborted"
    observed = str(status.get("observedGeneration", "")) == str(rollout["metadata"]["generation"])
    stable = status.get("stableRS") and status.get("stableRS") == status.get("currentPodHash")
    if observed and stable and status.get("phase") == "Healthy":
        return "promoted"
    return None


class Deployer:
    def __init__(
        self, cfg, github, clusters, loadgens, events, sleep=time.sleep, clock=time.monotonic
    ):
        self.cfg = cfg
        self.github = github
        self.clusters = clusters  # {"staging": Cluster, "prod": Cluster}
        self.loadgens = loadgens  # {"staging": LoadGenerator, "prod": LoadGenerator}
        self.events = events
        self.sleep = sleep
        self.clock = clock
        self.blocked: set[str] = set()  # commits already refused by the security gate

    def poll_once(self) -> dict[str, str]:
        """One poll. Returns what happened per environment; {} when there was nothing to do."""
        run = self.github.latest_success()
        if run is None:
            return {}
        sha = run["head_sha"]
        image = f"{self.cfg.image}:{sha}"
        staging = self.clusters["staging"].rollout()
        if deployed_sha(staging, self.cfg.image) != sha:
            if sha in self.blocked:
                return {}
            if not self.security_gate(run, image):
                self.blocked.add(sha)
                return {"staging": "blocked"}
            result = {"staging": self.release("staging", run)}
            if result["staging"] == "promoted" and self.cfg.release_to_prod:
                result["prod"] = self.release("prod", run)
            return result
        # Staging already runs this commit: release it to prod if that is still pending
        if (
            self.cfg.release_to_prod
            and rollout_outcome(staging, image) == "promoted"
            and deployed_sha(self.clusters["prod"].rollout(), self.cfg.image) != sha
        ):
            return {"prod": self.release("prod", run)}
        return {}

    def security_gate(self, run: dict, image: str) -> bool:
        counts = self.github.scan_counts(run["id"])
        decision, reason = security_verdict(counts)
        if self.already_decided(run["head_sha"], decision):
            return decision == "allow"  # logged by an earlier process: log each decision once
        self.events.write(
            "security_decision",
            source="security_gate",
            sha=run["head_sha"],
            run_id=run["id"],
            image=image,
            decision=decision,
            reason=reason,
            **(counts or {}),
        )
        return decision == "allow"

    def already_decided(self, sha: str, decision: str) -> bool:
        audit = getattr(self.events, "audit", None)
        if audit is None:
            return False
        rows = audit.query(
            "SELECT 1 FROM events WHERE kind = 'security_decision' AND commit_sha = ? "
            "AND json_extract(payload, '$.decision') = ? LIMIT 1",
            (sha, decision),
        )
        return bool(rows)

    def release(self, environment: str, run: dict) -> str:
        sha = run["head_sha"]
        image = f"{self.cfg.image}:{sha}"
        ids = {"sha": sha, "run_id": run["id"], "run_url": run.get("html_url"), "image": image}
        self.events.write("run_detected", environment=environment, **ids)
        loadgen = self.loadgens[environment]
        log = loadgen.start(sha[:12])
        started = self.clock()
        outcome = "error"
        try:
            self.clusters[environment].deploy(image, f"FlowGuard {sha[:7]}")
            self.events.write("deploy_started", environment=environment, load_log=str(log), **ids)
            outcome = self.wait(environment, image)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or str(exc))[-500:]
            self.events.write("deploy_error", environment=environment, detail=detail, **ids)
        finally:
            loadgen.stop()
        self.events.write(
            "deploy_finished",
            environment=environment,
            outcome=outcome,
            seconds=round(self.clock() - started),
            **ids,
        )
        return outcome

    def wait(self, environment: str, image: str) -> str:
        deadline = self.clock() + self.cfg.rollout_timeout
        while self.clock() < deadline:
            outcome = rollout_outcome(self.clusters[environment].rollout(), image)
            if outcome:
                return outcome
            self.sleep(10)
        return "timeout"

    def run_forever(self) -> None:
        self.events.write(
            "deployer_started",
            repo=self.cfg.repo,
            branch=self.cfg.branch,
            release_to_prod=self.cfg.release_to_prod,
            authenticated=bool(self.cfg.token),
            poll_seconds=self.cfg.poll_seconds,
        )
        while True:
            try:
                self.poll_once()
                delay = self.cfg.poll_seconds
            except RateLimited as exc:
                delay = max(self.cfg.poll_seconds, exc.reset_at - int(time.time()) + 5)
                self.events.write("rate_limited", resume_in_seconds=delay)
            except (urllib.error.URLError, OSError, subprocess.CalledProcessError) as exc:
                delay = self.cfg.poll_seconds
                self.events.write("poll_error", detail=str(exc)[-500:])
            self.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--branch", help="branch whose app-ci runs are deployed (default: dev)")
    parser.add_argument("--no-prod", action="store_true", help="stop after the staging canary")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run", help="poll and deploy")
    p_run.add_argument("--once", action="store_true", help="one poll, then exit")
    p_status = sub.add_parser("status", help="print the app-ci run for a commit as JSON")
    p_status.add_argument("--sha", required=True)
    p_gate = sub.add_parser("gate", help="print a commit's scan counts and L0 security verdict")
    p_gate.add_argument("--sha", required=True)
    args = parser.parse_args(argv)

    cfg = load_config(args.env_file)
    if args.branch:
        cfg.branch = args.branch
    cfg.release_to_prod = not args.no_prod
    github = GitHub(cfg)
    if args.command == "status":
        run = github.run_for_commit(args.sha)
        keys = ("id", "head_sha", "status", "conclusion", "html_url", "created_at", "updated_at")
        print(json.dumps({k: run.get(k) for k in keys} if run else None))
        return 0
    if args.command == "gate":
        run = github.run_for_commit(args.sha)
        counts = github.scan_counts(run["id"]) if run else None
        decision, reason = security_verdict(counts)
        print(json.dumps({"counts": counts, "decision": decision, "reason": reason}))
        return 0

    events = EventLog(RUNS_DIR / "deployer-events.jsonl", AuditLog.open())
    envs = ("staging", "prod")
    deployer = Deployer(
        cfg,
        github,
        {env: Cluster(env) for env in envs},
        {env: LoadGenerator(env) for env in envs},
        events,
    )
    if args.once:
        print(json.dumps(deployer.poll_once()))
        return 0
    try:
        deployer.run_forever()
    except KeyboardInterrupt:
        events.write("deployer_stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
