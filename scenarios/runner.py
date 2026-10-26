"""Scenario runner (Step 4a): executes one scenario spec, records it (D15) and cleans up.

    python scenarios/runner.py scenarios/dev/release-errors-05.yaml
    python scenarios/runner.py --validate scenarios/dev/*.yaml

A spec (YAML, see scenarios/README.md) names a family, a fault, its intensity, the timing and
the ground truth. Dev specs live in scenarios/dev/; scenarios/heldout/ is gitignored and kept
for the person who writes the held-out set (D12).

Deliveries:
  config   release family: the same image with fault flags in the Rollout's pod template,
           which starts a canary under L0 (D19 amended). Optionally with CPU stress on the
           node during a good release (the H4 collaboration case).
  commit   a change pushed on a throwaway scenario/r-<random> branch with a neutral commit
           message; app-ci builds it, then the deployer's L0 security gate and the staging
           canary handle it like any release. For the ci family the failed CI run is the
           scenario (a template per root cause, ci_failures.py); for the pipeline family a
           slow but passing CI step.
  runtime  runtime family: an incident in the running version, with no rollout: fault
           settings changed on running staging pods through their internal /fault endpoint.
Run ids are opaque (r-<random>) so nothing CI or the app shows reveals the scenario. Every
scenario writes audit events under its run id, is recorded into evaluation/recordings/<run id>/
with the ground truth (including the template or incident details) and what L0 did, and is
cleaned up: staging restored, stress pods and the scenario branch deleted.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "agents"),
    str(ROOT / "ci"),
    str(ROOT / "evaluation"),
    str(ROOT / "scenarios"),
]

import ci_failures  # noqa: E402
import deployer  # noqa: E402
import recorder  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402

FAMILIES = {
    "release": {"kinds": {"http_errors", "latency", "none"}, "deliveries": {"config", "commit"}},
    "security": {
        "kinds": {"leaked_secret", "vulnerable_dependency", "low_severity_finding"},
        "deliveries": {"commit"},
    },
    "ci": {"kinds": set(ci_failures.CLASSES), "deliveries": {"commit"}},
    "runtime": {
        "kinds": {"error_burst", "latency_spike", "memory_leak"},
        "deliveries": {"runtime"},
    },
    "pipeline": {"kinds": {"slow_step"}, "deliveries": {"commit"}},
}
# detect: runtime and pipeline anomalies, judged by the L0 detectors measured in Step 4b
EXPECTED_L0 = {"promote", "abort", "block", "ci_failure", "detect"}
BASE_COMMAND = ["./podinfo", "--port=9898", "--level=info", "--port-metrics=9797"]
GOMOD, GOSUM = "app/podinfo/go.mod", "app/podinfo/go.sum"


# ---------------------------------------------------------------- spec


def load_spec(path: Path) -> dict:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors = validate_spec(spec)
    if errors:
        raise ValueError(f"{path}: " + "; ".join(errors))
    return spec


def validate_spec(spec) -> list[str]:
    """All problems found, so one run shows every mistake in a spec."""
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]
    errors = [
        f"missing '{k}'"
        for k in ("id", "family", "description", "fault", "ground_truth")
        if k not in spec
    ]
    if errors:
        return errors
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,60}", str(spec["id"])):
        errors.append("id: lowercase letters, digits and dashes, 3 to 61 characters")
    family = spec["family"]
    if family not in FAMILIES:
        return errors + [f"family must be one of {sorted(FAMILIES)}"]
    fault = spec["fault"] or {}
    if fault.get("kind") not in FAMILIES[family]["kinds"]:
        errors.append(f"fault.kind for {family} must be one of {sorted(FAMILIES[family]['kinds'])}")
    if fault.get("delivery") not in FAMILIES[family]["deliveries"]:
        errors.append(
            f"fault.delivery for {family} must be one of {sorted(FAMILIES[family]['deliveries'])}"
        )
    for key in ("error_rate", "fraction", "pods_fraction"):
        if key in fault and not 0 <= float(fault[key]) <= 1:
            errors.append(f"fault.{key} must be within 0..1")
    for key in ("latency_ms", "jitter_ms", "leak_kb_per_s", "added_s"):
        if key in fault and int(fault[key]) < 0:
            errors.append(f"fault.{key} must be >= 0")
    if "template" in fault:
        template = ci_failures.TEMPLATES.get(fault["template"])
        if family != "ci" or template is None or template.root_cause != fault.get("kind"):
            errors.append("fault.template must name a template of the ci family's fault.kind")
    if "node_stress" in spec and family != "release":
        errors.append("node_stress is only for the release family")
    if spec.get("environment", "staging") != "staging":
        errors.append("environment: only staging is supported for scenarios")
    truth = spec["ground_truth"] or {}
    if truth.get("expected_l0") not in EXPECTED_L0:
        errors.append(f"ground_truth.expected_l0 must be one of {sorted(EXPECTED_L0)}")
    if family == "ci" and truth.get("root_cause") != fault.get("kind"):
        errors.append("ground_truth.root_cause must equal fault.kind for the ci family")
    return errors


def fault_flags(fault: dict) -> list[str]:
    """podinfo flags for a config-delivered fault (empty for kind none)."""
    if fault.get("kind") == "none":
        return []
    return [
        f"--fault-error-rate={float(fault.get('error_rate', 0))}",
        f"--fault-latency-ms={int(fault.get('latency_ms', 0))}",
        f"--fault-jitter-ms={int(fault.get('jitter_ms', 0))}",
        f"--fault-fraction={float(fault.get('fraction', 1))}",
    ]


def runtime_settings(fault: dict) -> dict:
    """The JSON body for a running pod's /fault endpoint."""
    return {
        "error_rate": float(fault.get("error_rate", 0)),
        "latency_ms": int(fault.get("latency_ms", 0)),
        "jitter_ms": int(fault.get("jitter_ms", 0)),
        "fraction": float(fault.get("fraction", 1)),
        "leak_kb_per_s": int(fault.get("leak_kb_per_s", 0)),
    }


def new_run_id() -> str:
    """Opaque: nothing in the id says which scenario or class it is."""
    return f"r-{secrets.token_hex(4)}"


def l0_correct(expected: str, observed: str) -> bool | None:
    """True or False when L0's decision can be judged; None when it is measured later."""
    if expected == "detect":
        return None
    return {
        "promote": "promoted",
        "abort": "aborted",
        "block": "blocked",
        "ci_failure": "failure",
    }.get(expected) == observed


# ---------------------------------------------------------------- commit content

SLOW_TEST = """package http

import (
\t"testing"
\t"time"
)

func TestResponseCacheWarmup(t *testing.T) {{
\tdeadline := time.Now().Add({seconds} * time.Second)
\twarmed := 0
\tfor time.Now().Before(deadline) {{
\t\ttime.Sleep(250 * time.Millisecond)
\t\twarmed++
\t}}
\tif warmed == 0 {{
\t\tt.Fatal("cache never warmed")
\t}}
}}
"""


def commit_change(root: Path, spec: dict, rng: random.Random) -> dict:
    """Write the scenario's commit into a checkout. Returns files, message and extra truth.
    Nothing here may name the class or the experiment (see leakage.py)."""
    family, fault = spec["family"], spec["fault"]
    if family == "ci":
        template = ci_failures.pick(fault["kind"], rng, fault.get("template"))
        files = template.apply(root)
        return {
            "files": files,
            "message": template.commit_message,
            "truth": {"template_id": template.id, "failing_job": template.failing_job},
        }
    if family == "pipeline":
        seconds = int(fault.get("added_s", 90))
        rel = "app/podinfo/pkg/api/http/warmup_test.go"
        ci_failures.write_file(root, rel, SLOW_TEST.format(seconds=seconds))
        return {
            "files": [rel],
            "message": "Add response cache warm-up test",
            "truth": {"anomaly": {"job": "unit", "added_s": seconds}},
        }
    if fault["kind"] == "leaked_secret":
        rel = "app/podinfo/deploy/staging.env"
        ci_failures.write_file(root, rel, f'API_KEY="{secrets.token_hex(20)}"\nLOG_LEVEL=info\n')
        return {"files": [rel], "message": "Add staging settings", "truth": {}}
    if fault["kind"] == "vulnerable_dependency":
        files = sorted(
            {
                ci_failures.replace_once(
                    root,
                    GOMOD,
                    "golang.org/x/text => golang.org/x/text v0.42.0",
                    "golang.org/x/text => golang.org/x/text v0.19.0",
                ),
                append_sum(
                    root,
                    "golang.org/x/text v0.19.0 h1:kTxAhCbGbxhK0IwgSKiMO5awPoDQ0RpfiVYBfK860YM=",
                    "golang.org/x/text v0.19.0/go.mod "
                    "h1:BuEKDfySbSR4drPmRPG/7iBdf8hvFMuRexcpahXilzY=",
                ),
            }
        )
        return {
            "files": files,
            "message": "Pin x/text",
            "truth": {"finding": "golang.org/x/text v0.19.0, HIGH CVEs"},
        }
    if fault["kind"] == "low_severity_finding":
        files = sorted(
            {
                ci_failures.replace_once(
                    root,
                    GOMOD,
                    "github.com/gorilla/websocket v1.5.3",
                    "github.com/gorilla/websocket v1.5.0",
                ),
                append_sum(
                    root,
                    "github.com/gorilla/websocket v1.5.0 "
                    "h1:PPwGk2jz7EePpoHN/+ClbZu8SPxiqlu12wZP/3sWmnc=",
                    "github.com/gorilla/websocket v1.5.0/go.mod "
                    "h1:YR8l580nyteQvAITg2hZ9XVh4b55+EU/adAjf1fMHhE=",
                ),
            }
        )
        return {
            "files": files,
            "message": "Pin websocket",
            "truth": {"finding": "github.com/gorilla/websocket v1.5.0, MODERATE only"},
        }
    # release family through CI: the fault is on by default in podinfo's code
    rel = "app/podinfo/cmd/podinfo/main.go"
    text = (root / rel).read_text(encoding="utf-8")
    for flag, value in (
        ("fault-error-rate", fault.get("error_rate", 0)),
        ("fault-latency-ms", fault.get("latency_ms", 0)),
        ("fault-jitter-ms", fault.get("jitter_ms", 0)),
    ):
        text, n = re.subn(rf'(fs\.\w+\("{flag}", )[^,]+,', rf"\g<1>{value},", text, count=1)
        if n != 1:
            raise ValueError(f"main.go: flag {flag} not found")
    (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return {"files": [rel], "message": "Tune request handling defaults", "truth": {}}


def append_sum(root: Path, *lines: str) -> str:
    path = root / GOSUM
    text = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    path.write_text(
        "\n".join(sorted(set(text) | set(lines))) + "\n", encoding="utf-8", newline="\n"
    )
    return GOSUM


# ---------------------------------------------------------------- cluster helpers


def kubectl(*args: str, check: bool = True, input_text: str | None = None) -> str:
    return subprocess.run(
        ["kubectl", *args], check=check, capture_output=True, text=True, input=input_text
    ).stdout


def patch_template(namespace: str, image: str, command: list[str]) -> None:
    ops = [
        {"op": "replace", "path": "/spec/template/spec/containers/0/command", "value": command},
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": image},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(ops, f)
    try:
        kubectl(
            "-n", namespace, "patch", "rollout", "podinfo", "--type", "json", "--patch-file", f.name
        )
    finally:
        os.unlink(f.name)


def wait_outcome(cluster, image: str, timeout: int, sleep=time.sleep, clock=time.monotonic) -> str:
    deadline = clock() + timeout
    while clock() < deadline:
        outcome = deployer.rollout_outcome(cluster.rollout(), image)
        if outcome:
            return outcome
        sleep(10)
    return "timeout"


def stable_pods(namespace: str, stable_hash: str) -> list[dict]:
    data = json.loads(
        kubectl(
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            f"app=podinfo,rollouts-pod-template-hash={stable_hash}",
            "-o",
            "json",
        )
    )
    return [
        {
            "name": p["metadata"]["name"],
            "restarts": sum(
                c.get("restartCount", 0) for c in p["status"].get("containerStatuses", [])
            ),
        }
        for p in data["items"]
    ]


def utc() -> datetime:
    return datetime.now(UTC)


STRESS_POD = {
    "apiVersion": "v1",
    "kind": "Pod",
    "metadata": {"name": "", "namespace": "default", "labels": {"app": "batch-worker"}},
    "spec": {
        "restartPolicy": "Never",
        "containers": [
            {
                "name": "worker",
                "image": "alpine:3.24",
                "command": ["sh", "-c", ""],
                "resources": {
                    "requests": {"cpu": "10m", "memory": "8Mi"},
                    "limits": {"memory": "32Mi"},
                },
            }
        ],
    },
}


# ---------------------------------------------------------------- runner


class Runner:
    def __init__(self, spec: dict, run_id: str | None = None):
        self.spec = spec
        self.run_id = run_id or new_run_id()
        self.rng = random.Random(self.run_id)
        self.env = spec.get("environment", "staging")
        self.timing = {
            "warmup_s": 30,
            "observe_s": 600,
            "cooldown_s": 20,
            "incident_s": 180,
            **spec.get("timing", {}),
        }
        self.rate = float((spec.get("load") or {}).get("rate", 10))
        self.audit = AuditLog.open(scenario_id=self.run_id, level="L0")
        self.cluster = deployer.Cluster(self.env)
        self.loadgen = deployer.LoadGenerator(self.env)
        self.observed: dict = {}
        self.truth: dict = dict(spec["ground_truth"])

    def event(self, kind: str, **payload) -> None:
        self.audit.event("scenario_runner", kind, environment=self.env, **payload)
        print(json.dumps({"ts": recorder.iso(utc()), "event": kind, **payload}), flush=True)

    def run(self) -> Path:
        delivery = self.spec["fault"]["delivery"]
        start = utc()
        self.event(
            "scenario_started",
            spec_id=self.spec["id"],
            family=self.spec["family"],
            fault=self.spec["fault"],
        )
        loadgen_files: list[Path] = []
        ci_runs: list[int] = []
        try:
            if delivery == "config":
                loadgen_files.append(self.run_config())
            elif delivery == "runtime":
                loadgen_files.append(self.run_runtime())
            else:
                ci_runs, loadgen_files = self.run_commit()
        finally:
            end = utc()
            correct = l0_correct(self.truth["expected_l0"], self.observed.get("l0", "unknown"))
            self.event(
                "scenario_finished",
                observed=self.observed,
                l0_correct=correct,
                ground_truth=self.truth,
            )
        cfg = deployer.load_config(ROOT / ".env")
        return recorder.record(
            self.run_id,
            start,
            end,
            loadgen_files=loadgen_files,
            ci_runs=ci_runs,
            ground_truth=self.truth,
            extra={"spec": self.spec, "observed": self.observed, "l0_correct": correct},
            github=recorder.GitHubReader(cfg.repo, cfg.token),
            recording_id=self.run_id,
        )

    # ------------------------------------------------------------ config delivery

    def healthy_stable(self) -> dict:
        rollout = self.cluster.rollout()
        status = rollout.get("status", {})
        if status.get("phase") != "Healthy" or status.get("stableRS") != status.get(
            "currentPodHash"
        ):
            raise RuntimeError(f"{self.env} Rollout is not Healthy and stable; refusing to start")
        return rollout

    def run_config(self) -> Path:
        saved = deployer.container(self.healthy_stable())
        image, command = saved["image"], list(saved.get("command") or BASE_COMMAND)
        stress = self.spec.get("node_stress")
        log = self.loadgen.start(self.run_id)
        try:
            if stress:
                self.start_stress(int(stress.get("cpu_workers", 4)))
            time.sleep(self.timing["warmup_s"])
            release_cmd = BASE_COMMAND + [f"--ui-message=build {self.run_id}"]
            release_cmd += fault_flags(self.spec["fault"])
            patch_template(self.env, image, release_cmd)
            self.event("fault_injected", delivery="config", image=image, command=release_cmd)
            outcome = wait_outcome(self.cluster, image, self.timing["observe_s"])
            self.observed["l0"] = outcome
            self.event("l0_outcome", outcome=outcome)
            time.sleep(self.timing["cooldown_s"])
        finally:
            if stress:
                self.stop_stress()
            self.restore(image, command)
            self.loadgen.stop()
        return log

    def restore(self, image: str, command: list[str]) -> None:
        """Back to the pre-scenario template; a promoted fault needs a canary back (with load)."""
        patch_template(self.env, image, command)
        outcome = wait_outcome(self.cluster, image, self.timing["observe_s"])
        self.observed["restore"] = outcome
        self.event("template_restored", outcome=outcome)

    def start_stress(self, workers: int) -> None:
        pod = json.loads(json.dumps(STRESS_POD))
        pod["metadata"]["name"] = f"batch-worker-{self.run_id[2:]}"
        pod["spec"]["containers"][0]["command"][2] = (
            f"for i in $(seq {workers}); do while :; do :; done & done; wait"
        )
        kubectl("apply", "-f", "-", input_text=json.dumps(pod))
        kubectl(
            "-n",
            "default",
            "wait",
            "--for=condition=Ready",
            f"pod/{pod['metadata']['name']}",
            "--timeout=120s",
        )
        self.truth["node_stress"] = {"cpu_workers": workers, "pod": pod["metadata"]["name"]}
        self.event("node_stress_started", workers=workers)

    def stop_stress(self) -> None:
        kubectl(
            "-n",
            "default",
            "delete",
            "pod",
            f"batch-worker-{self.run_id[2:]}",
            "--ignore-not-found",
            "--wait=false",
            check=False,
        )
        self.event("node_stress_stopped")

    # ------------------------------------------------------------ runtime delivery

    def set_pod_fault(self, pod: str, settings: dict | None) -> bool:
        """PUT (or DELETE when settings is None) the pod's /fault endpoint, on its metrics port."""
        curl = ["curl", "-sf", "-m", "5", "http://localhost:9797/fault"]
        if settings is None:
            curl += ["-X", "DELETE"]
        else:
            curl += ["-X", "PUT", "-d", json.dumps(settings)]
        p = subprocess.run(
            ["kubectl", "-n", self.env, "exec", pod, "-c", "podinfo", "--", *curl],
            capture_output=True,
            text=True,
        )
        return p.returncode == 0

    def run_runtime(self) -> Path:
        stable_hash = self.healthy_stable()["status"]["stableRS"]
        pods = stable_pods(self.env, stable_hash)
        fault = self.spec["fault"]
        n = max(1, math.ceil(len(pods) * float(fault.get("pods_fraction", 1))))
        targets = sorted(self.rng.sample([p["name"] for p in pods], n))
        restarts_before = {p["name"]: p["restarts"] for p in pods}
        settings = runtime_settings(fault)
        log = self.loadgen.start(self.run_id)
        start = end = None
        try:
            time.sleep(self.timing["warmup_s"])
            start = utc()
            self.event("fault_injected", delivery="runtime", pods=targets, settings=settings)
            deadline = time.monotonic() + self.timing["incident_s"]
            applied = 0
            while time.monotonic() < deadline:
                # Re-applied every 15 s: a pod restarted by the incident gets it again
                applied += sum(self.set_pod_fault(p, settings) for p in targets)
                time.sleep(15)
            end = utc()
        finally:
            for p in targets:
                self.set_pod_fault(p, None)
            self.event("fault_cleared", pods=targets)
            time.sleep(self.timing["cooldown_s"])
            self.loadgen.stop()
        after = {p["name"]: p["restarts"] for p in stable_pods(self.env, stable_hash)}
        restarts = {p: after.get(p, 0) - restarts_before.get(p, 0) for p in targets}
        self.truth["incident"] = {
            "kind": fault["kind"],
            "start": recorder.iso(start) if start else None,
            "end": recorder.iso(end) if end else None,
            "pods": targets,
            "settings": settings,
        }
        self.observed.update(
            container_restarts=restarts, settings_applied=applied, l0="not_measured"
        )
        self.event("l0_outcome", outcome="not_measured", container_restarts=restarts)
        return log

    # ------------------------------------------------------------ commit delivery

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def run_commit(self) -> tuple[list[int], list[Path]]:
        if self.git("rev-parse", "--abbrev-ref", "HEAD") != "dev" or self.git(
            "status", "--porcelain"
        ):
            raise RuntimeError("commit delivery needs a clean dev checkout")
        cfg = deployer.load_config(ROOT / ".env")
        github = deployer.GitHub(cfg)
        branch = f"scenario/{self.run_id}"
        saved = deployer.container(self.cluster.rollout())
        self.git("switch", "-q", "-c", branch)
        pushed = False
        try:
            change = commit_change(ROOT, self.spec, self.rng)
            self.truth.update(change["truth"])
            self.git("add", *change["files"])
            self.git("commit", "-q", "-m", change["message"])
            sha = self.git("rev-parse", "HEAD")
            self.git("push", "-q", "-u", "origin", branch)
            pushed = True
            self.event(
                "fault_injected",
                delivery="commit",
                branch=branch,
                sha=sha,
                files=change["files"],
                message=change["message"],
            )
            run = self.wait_ci(github, sha)
            ci_runs = [run["id"]] if run else []
            self.observed["ci"] = run.get("conclusion") if run else "no run"
            if self.spec["family"] == "ci":
                self.observed["l0"] = self.observed["ci"]
                self.event("l0_outcome", outcome=self.observed["l0"], run_id=ci_runs[:1])
                return ci_runs, []
            if run and self.spec["family"] == "pipeline":
                self.observed["job_seconds"] = job_seconds(github, run["id"])
            if not run or run.get("conclusion") != "success":
                self.observed["l0"] = f"ci_{self.observed['ci']}"
                return ci_runs, []
            # The deployer handles the image like any release, staging only
            cfg.branch, cfg.release_to_prod = branch, False
            events = deployer.EventLog(
                ROOT / "evaluation" / "runs" / "deployer-events.jsonl", self.audit
            )
            d = deployer.Deployer(
                cfg,
                github,
                {"staging": self.cluster, "prod": None},
                {"staging": self.loadgen, "prod": None},
                events,
            )
            result = d.poll_once()
            self.observed["l0"] = result.get("staging", "idle")
            self.event("l0_outcome", outcome=self.observed["l0"])
            if self.observed["l0"] in ("promoted", "aborted", "timeout"):
                self.loadgen.start(f"{self.run_id}-restore")
                try:
                    self.restore(saved["image"], list(saved.get("command") or BASE_COMMAND))
                finally:
                    self.loadgen.stop()
            return ci_runs, [deployer.RUNS_DIR / f"deploy-{self.env}-{sha[:12]}.jsonl"]
        finally:
            subprocess.run(
                ["git", "-C", str(ROOT), "switch", "-q", "--discard-changes", "dev"],
                capture_output=True,
            )
            if pushed:
                subprocess.run(
                    ["git", "-C", str(ROOT), "push", "-q", "origin", "--delete", branch],
                    capture_output=True,
                )
            subprocess.run(
                ["git", "-C", str(ROOT), "branch", "-q", "-D", branch], capture_output=True
            )
            self.event("branch_deleted", branch=branch, pushed=pushed)

    def wait_ci(self, github, sha: str, timeout: int = 2400, sleep=time.sleep) -> dict | None:
        """Poll until the commit's app-ci run completes; network hiccups are retried."""
        deadline = time.monotonic() + timeout
        run = None
        while time.monotonic() < deadline:
            try:
                run = github.run_for_commit(sha)
            except deployer.RateLimited as exc:
                sleep(max(30, exc.reset_at - int(time.time()) + 5))
                continue
            except OSError as exc:  # URLError, timeouts, TLS handshake failures
                self.event("poll_retry", detail=str(exc)[:200])
                sleep(30)
                continue
            if run and run.get("status") == "completed":
                return run
            sleep(30)
        return run


def job_seconds(github, run_id: int) -> dict[str, float]:
    jobs = github.get(f"/repos/{github.cfg.repo}/actions/runs/{run_id}/jobs?per_page=50")["jobs"]
    out = {}
    for j in jobs:
        if j.get("started_at") and j.get("completed_at"):
            took = recorder.parse_ts(j["completed_at"]) - recorder.parse_ts(j["started_at"])
            out[j["name"]] = took.total_seconds()
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--validate", action="store_true", help="only check the specs")
    args = parser.parse_args(argv)
    if args.validate:
        bad = 0
        for path in args.specs:
            errors = validate_spec(yaml.safe_load(path.read_text(encoding="utf-8")))
            status = "OK  " if not errors else "FAIL"
            print(f"{status} {path}" + (f": {errors}" if errors else ""))
            bad += bool(errors)
        return 1 if bad else 0
    if len(args.specs) != 1:
        parser.error("run one scenario at a time")
    out = Runner(load_spec(args.specs[0])).run()
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "recording": str(out),
                "spec": manifest["spec"]["id"],
                "observed": manifest["observed"],
                "l0_correct": manifest["l0_correct"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
