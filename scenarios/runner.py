"""Scenario runner (Step 4a): executes one scenario spec, records it (D15) and cleans up.

    python scenarios/runner.py scenarios/dev/release-errors-05.yaml
    python scenarios/runner.py --validate scenarios/dev/*.yaml

A spec (YAML, see scenarios/README.md) names a family, a fault, its intensity, the timing and
the ground truth. Dev specs live in scenarios/dev/; scenarios/heldout/ is gitignored and kept
for the person who writes the held-out set (D12).

Deliveries:
  config   release family: the same image and command with a new release Secret
           (podinfo-release-<random>, envFrom) holding the fault settings, which starts a
           canary under L0 (D19 amended, D25). Good releases get one too, with empty values,
           so the pod spec never tells them apart. Optionally with CPU stress on the node
           during a good release (the H4 collaboration case).
  commit   a change pushed on a throwaway scenario/r-<random> branch with a neutral commit
           message; app-ci builds it, then the deployer's L0 security gate and the staging
           canary handle it like any release. For the ci family the failed CI run is the
           scenario (a template per root cause, ci_failures.py); for the pipeline family a
           slow but passing CI step.
  runtime  runtime family: an incident in the running version, with no rollout: fault
           settings changed on running staging pods through their internal /fault endpoint.
Run ids are opaque (r-<random>) so nothing CI or the app shows reveals the scenario.
Information firewall (D25): the spec, the ground truth and every injection event go to the
ground-truth DB (evaluation/groundtruth.py), never to the shared audit log, which only gets
what a real system would see (deployments, gate decisions, CI results). Every scenario is
recorded into evaluation/recordings/<run id>/ (ground truth only in its ground_truth.json) and
cleaned up: staging restored, unused release Secrets, stress pods and the branch deleted.
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
from datetime import UTC, datetime, timedelta
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
import groundtruth  # noqa: E402
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
RUNS = ROOT / "evaluation" / "runs"


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
    through_ci = family == "release" and fault.get("delivery") == "commit"
    if through_ci and (
        fault.get("kind") != "http_errors" or not float(fault.get("error_rate") or 0) > 0
    ):
        errors.append("a release through CI is an http_errors defect with error_rate > 0")
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


def release_settings(fault: dict) -> dict[str, str]:
    """The release Secret's values for a config-delivered fault (empty for kind none)."""
    if fault.get("kind") == "none":
        return {}
    return {
        "PODINFO_FAULT_ERROR_RATE": str(float(fault.get("error_rate", 0))),
        "PODINFO_FAULT_LATENCY_MS": str(int(fault.get("latency_ms", 0))),
        "PODINFO_FAULT_JITTER_MS": str(int(fault.get("jitter_ms", 0))),
        "PODINFO_FAULT_FRACTION": str(float(fault.get("fraction", 1))),
    }


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
    if fault["kind"] in ("vulnerable_dependency", "low_severity_finding") and fault.get("module"):
        # A DSO4 item from the frozen pool: pin exactly the drawn module version
        module, version = fault["module"], fault["version"]
        files = pin_module(root, module, version, go_sum_lines(module, version))
        return {
            "files": files,
            "message": f"Pin {module.rsplit('/', 1)[-1]}",
            "truth": {"finding": f"{module} {version}"},
        }
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
    # release family through CI: an ordinary code defect (D30), no fault setting in the diff
    if fault.get("kind") != "http_errors":
        raise ValueError("a release through CI supports the http_errors kind only")
    interval, rate = refresh_interval(float(fault["error_rate"]), spec)
    rel = "app/podinfo/pkg/api/http/info.go"
    text = (root / rel).read_text(encoding="utf-8")
    for old, new in (
        ('\t"net/http"\n', '\t"fmt"\n\t"net/http"\n'),
        (
            '\t"runtime"\n\t"strconv"\n',
            '\t"os"\n\t"runtime"\n\t"strconv"\n\t"strings"\n\t"sync/atomic"\n\t"time"\n',
        ),
        ("\tdefer span.End()\n\n\tdata := RuntimeResponse{", INFO_REFRESH_CALL),
    ):
        if text.count(old) != 1:
            raise ValueError(f"{rel}: unexpected source, cannot apply the change")
        text = text.replace(old, new)
    text += INFO_REFRESH_FUNC.replace("{interval}", str(interval))
    (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return {
        "files": [rel],
        "message": "Refresh runtime counters periodically",
        "truth": {
            "defect": {"file": rel, "refresh_interval_s": interval, "expected_5xx_share": rate}
        },
    }


REPLICAS = 10  # podinfo's Rollout; the Service spreads the load evenly over the pods
MIN_REFRESH_S = 5  # longer than podinfo's unit tests run, so they never trigger the defect


def refresh_interval(error_rate: float, spec: dict) -> tuple[int, float]:
    """Seconds between failing refreshes for about `error_rate` of the canary's requests, and
    the share it really gives: each pod fails one request per interval."""
    per_pod_rps = float((spec.get("load") or {}).get("rate", 10)) / REPLICAS
    interval = max(MIN_REFRESH_S, round(1 / (error_rate * per_pod_rps)))
    return interval, round(1 / (interval * per_pod_rps), 4)


# The defect, which unit tests miss: at most once per interval, an info request re-reads the
# thread count from /proc/self/stat but parses field 1 (the command name, "(podinfo)") instead
# of field 19, so that request answers 500. The clock starts with the process.
INFO_REFRESH_CALL = """\tdefer span.End()

\tif now, last := time.Now().UnixNano(), lastRefresh.Load(); now-last >= int64(refreshInterval) &&
\t\tlastRefresh.CompareAndSwap(last, now) {
\t\tif err := refreshThreads(); err != nil {
\t\t\tbody := `{"status":500,"error":%q,"hostname":%q}`
\t\t\tw.Header().Set("Content-Type", "application/json; charset=utf-8")
\t\t\tw.WriteHeader(http.StatusInternalServerError)
\t\t\t_, _ = fmt.Fprintf(w, body, err.Error(), s.config.Hostname)
\t\t\treturn
\t\t}
\t}

\tdata := RuntimeResponse{"""

INFO_REFRESH_FUNC = """
// refreshInterval is how often the thread count is re-read.
const refreshInterval = {interval} * time.Second

var (
\tlastRefresh atomic.Int64
\tthreadCount atomic.Int64
)

func init() {
\tlastRefresh.Store(time.Now().UnixNano())
}

// refreshThreads reads the process thread count from /proc/self/stat.
func refreshThreads() error {
\tstat, err := os.ReadFile("/proc/self/stat")
\tif err != nil {
\t\treturn nil // not on Linux: keep the last value
\t}
\tfields := strings.Fields(string(stat))
\tif len(fields) < 20 {
\t\treturn fmt.Errorf("short /proc/self/stat: %d fields", len(fields))
\t}
\tn, err := strconv.Atoi(fields[1])
\tif err != nil {
\t\treturn fmt.Errorf("read thread count: %w", err)
\t}
\tthreadCount.Store(int64(n))
\treturn nil
}
"""


def go_sum_lines(module: str, version: str) -> list[str]:
    """The two go.sum lines of module@version, from the Go module proxy (pinned Go image)."""
    out = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "GOFLAGS=-mod=mod",
            "golang:1.26.10-alpine",
            "go",
            "mod",
            "download",
            "-json",
            f"{module}@{version}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    info = json.loads(out)
    return [f"{module} {version} {info['Sum']}", f"{module} {version}/go.mod {info['GoModSum']}"]


def pin_module(root: Path, module: str, version: str, sums: list[str]) -> list[str]:
    """Pin module to version with a replace directive (updated if one exists) and its sums."""
    path = root / GOMOD
    text = path.read_text(encoding="utf-8")
    existing = re.search(rf"^replace {re.escape(module)} => {re.escape(module)} v\S+$", text, re.M)
    line = f"replace {module} => {module} {version}"
    text = (
        text.replace(existing.group(0), line) if existing else text.rstrip("\n") + f"\n\n{line}\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return sorted({GOMOD, append_sum(root, *sums)})


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


def env_from(secret: str) -> list[dict]:
    return [{"secretRef": {"name": secret}}]


def template_ops(current: dict, image: str, command: list[str], env: list | None) -> list[dict]:
    """JSON patch ops setting the container's image, command and envFrom (removed if None)."""
    path = "/spec/template/spec/containers/0"
    ops = [
        {"op": "replace", "path": f"{path}/command", "value": command},
        {"op": "replace", "path": f"{path}/image", "value": image},
    ]
    if env:
        ops.append({"op": "add", "path": f"{path}/envFrom", "value": env})
    elif current.get("envFrom"):
        ops.append({"op": "remove", "path": f"{path}/envFrom"})
    return ops


def patch_template(namespace: str, image: str, command: list[str], env: list | None = None) -> None:
    current = deployer.container(deployer.Cluster(namespace).rollout())
    ops = template_ops(current, image, command, env)
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


def retry_rollout(namespace: str) -> None:
    """Clear an abort so the controller runs the canary again (what `rollouts retry` does)."""
    kubectl(
        "-n",
        namespace,
        "patch",
        "rollout",
        "podinfo",
        "--subresource=status",
        "--type",
        "merge",
        "-p",
        '{"status":{"abort":false}}',
    )


def suspend_gaps(loadgen_files: list[Path], threshold_s: float = 60) -> list[dict]:
    """Periods when the host was asleep: no request for `threshold_s`, or a request that took
    that long. Load and metrics both stop then, so L0 sees no data and the run is not valid."""
    gap = timedelta(seconds=threshold_s)
    episodes: list[list[datetime]] = []  # [from, to]; one sleep stalls many requests at once
    for path in loadgen_files:
        if not Path(path).is_file():
            continue
        last = None
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))  # request start
            except (ValueError, KeyError):
                continue
            done = ts + timedelta(milliseconds=row.get("latency_ms") or 0)
            if done - ts >= gap:
                span = [ts, done]
            elif last and ts - last >= gap:
                span = [last, ts]
            else:
                span = None
            if span and episodes and span[0] <= episodes[-1][1]:
                episodes[-1][1] = max(episodes[-1][1], span[1])
            elif span:
                episodes.append(span)
            last = max(last, ts) if last else ts
    return [{"from": recorder.iso(a), "to": recorder.iso(b)} for a, b in episodes]


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
        # What a real system sees (deployments, gate decisions, CI results) ...
        self.audit = AuditLog.open(scenario_id=self.run_id, level="L0")
        self.deploys = deployer.EventLog(RUNS / "deployer-events.jsonl", self.audit)
        # ... and what only the experiment knows (D25)
        self.truth_db = groundtruth.GroundTruthDB.open()
        self.cluster = deployer.Cluster(self.env)
        self.loadgen = deployer.LoadGenerator(self.env)
        self.observed: dict = {}
        self.truth: dict = dict(spec["ground_truth"])

    def event(self, kind: str, **payload) -> None:
        """An experiment event: ground-truth DB and stdout only, never the audit log (D25)."""
        self.truth_db.event(self.run_id, kind, environment=self.env, **payload)
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
            if gaps := suspend_gaps(loadgen_files):
                # The host slept: no load and no metrics for a while, so the outcome says
                # nothing about L0. The run is kept but must be excluded from scoring.
                self.observed["host_suspended"] = gaps
                correct = None
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
            evaluation={
                "spec": self.spec,
                "observed": self.observed,
                "l0_correct": correct,
                "valid": "host_suspended" not in self.observed,
            },
            truth_db=self.truth_db,
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
            # Same image and command; only the release Secret differs, for good releases too
            settings = release_settings(self.spec["fault"])
            secret = deployer.create_release_settings(self.env, settings)
            self.release(image, command, env_from(secret), log)
            self.event("fault_injected", delivery="config", secret=secret, settings=settings)
            outcome = self.wait_release(image)
            self.observed["l0"] = outcome
            self.event("l0_outcome", outcome=outcome)
            time.sleep(self.timing["cooldown_s"])
        finally:
            if stress:
                self.stop_stress()
            self.restore(image, command, saved.get("envFrom"))
            self.loadgen.stop()
        return log

    def release(
        self, image: str, command: list[str], env: list | None, log: Path | None = None
    ) -> None:
        """Patch the template and log the deployment as the deployer would (audit log)."""
        patch_template(self.env, image, command, env)
        self.deploy_started(image, log)

    def deploy_started(self, image: str, log: Path | None = None) -> None:
        self._started = time.monotonic()
        extra = {"load_log": str(log)} if log else {}
        self.deploys.write("deploy_started", environment=self.env, image=image, **extra)

    def wait_release(self, image: str) -> str:
        outcome = wait_outcome(self.cluster, image, self.timing["observe_s"])
        self.deploys.write(
            "deploy_finished",
            environment=self.env,
            image=image,
            outcome=outcome,
            seconds=round(time.monotonic() - self._started),
        )
        return outcome

    def restore(self, image: str, command: list[str], env: list | None = None) -> None:
        """Back to the pre-scenario template (and its release Secret); a promoted fault needs a
        canary back (with load). An aborted restore (no data after a host sleep, a dead load
        generator) is retried once with fresh load, so staging is not left Degraded. It is a
        deployment like any other in the audit log."""
        self.release(image, command, env)
        for attempt in (1, 2):
            if not self.loadgen.alive():
                self.loadgen.start(f"{self.run_id}-restore-{attempt}")
            outcome = self.wait_release(image)
            if outcome == "promoted" or attempt == 2:
                break
            self.event("restore_retried", outcome=outcome)
            retry_rollout(self.env)
            self.deploy_started(image)
        self.observed["restore"] = outcome
        self.event("template_restored", outcome=outcome)
        try:
            self.event("release_settings_deleted", names=self.cluster.gc_release_settings())
        except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
            self.event("gc_error", detail=str(exc)[-300:])

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
            d = deployer.Deployer(
                cfg,
                github,
                {"staging": self.cluster, "prod": None},
                {"staging": self.loadgen, "prod": None},
                self.deploys,
            )
            result = d.poll_once()
            self.observed["l0"] = result.get("staging", "idle")
            self.event("l0_outcome", outcome=self.observed["l0"])
            if self.observed["l0"] in ("promoted", "aborted", "timeout"):
                self.loadgen.start(f"{self.run_id}-restore")
                try:
                    self.restore(
                        saved["image"],
                        list(saved.get("command") or BASE_COMMAND),
                        saved.get("envFrom"),
                    )
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
    truth = json.loads((out / "ground_truth.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {
                "recording": str(out),
                "spec": truth["spec"]["id"],
                "observed": truth["observed"],
                "l0_correct": truth["l0_correct"],
                "valid": truth["valid"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
