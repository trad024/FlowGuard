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
import urllib.parse
from contextlib import contextmanager
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
from flowguard import github_actions  # noqa: E402
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
CI_TIMEOUT_S = 2400  # the longest a scenario waits for an app-ci run to complete


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
    if expected == "detect" or observed == "offline":  # D54: L0 replayed from the recording
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


ROLLOUT_POLL_S = 5  # step changes are logged with Argo's own times; this only bounds the rest


def wait_outcome(
    cluster, image: str, timeout: int, sleep=time.sleep, clock=time.monotonic, watch=None
) -> str:
    """Poll the Rollout until the release for `image` ends; `watch` sees every status read
    (the step log, recorder.RolloutStepLog)."""
    deadline = clock() + timeout
    while clock() < deadline:
        rollout = cluster.rollout()
        if watch is not None:
            watch(rollout)
        outcome = deployer.rollout_outcome(rollout, image)
        if outcome:
            return outcome
        sleep(ROLLOUT_POLL_S)
    return "timeout"


# ---------------------------------------------------------------- agents-mode canary (D53, D54)

PODINFO = ROOT / "infra" / "apps" / "podinfo"
HOLD_S = 120  # D54: an indefinite pause is held past Release's fourth look (step + 120 s)
AGENTS_OBSERVE_S = 900  # two held steps and the 100% step, with slow pod starts under stress


def canary_steps(mode: str) -> list[dict]:
    """The canary steps the repo defines: the base Rollout's (L0, inline analysis) or the
    agents-mode component's (D53: an indefinite pause where L0 runs its analysis)."""
    if mode == "agents":
        component = yaml.safe_load(
            (PODINFO / "agents-mode" / "kustomization.yaml").read_text(encoding="utf-8")
        )
        (op,) = yaml.safe_load(component["patches"][0]["patch"])
        return op["value"]
    docs = yaml.safe_load_all((PODINFO / "base" / "podinfo.yaml").read_text(encoding="utf-8"))
    rollout = next(d for d in docs if d and d.get("kind") == "Rollout")
    return rollout["spec"]["strategy"]["canary"]["steps"]


def canary_mode(rollout: dict) -> str:
    """'l0' (inline analysis), 'agents' (indefinite pauses, no analysis) or 'other'."""
    steps = rollout.get("spec", {}).get("strategy", {}).get("canary", {}).get("steps", [])
    if any("analysis" in s for s in steps):
        return "l0"
    return "agents" if any(s.get("pause") == {} for s in steps) else "other"


def set_canary_mode(namespace: str, mode: str) -> None:
    """Replace only the Rollout's canary steps; the pod template is untouched, so no canary
    starts (the agents-mode overlay applied in full would also reset the image)."""
    ops = [{"op": "replace", "path": "/spec/strategy/canary/steps", "value": canary_steps(mode)}]
    kubectl("-n", namespace, "patch", "rollout", "podinfo", "--type", "json", "-p", json.dumps(ops))


def indefinite_pause(rollout: dict) -> int | None:
    """The step index while the Rollout waits at an indefinite pause, else None."""
    status = rollout.get("status", {})
    steps = rollout["spec"]["strategy"]["canary"]["steps"]
    i = status.get("currentStepIndex")
    if i is None or i >= len(steps) or steps[i].get("pause") != {}:
        return None
    reasons = {c.get("reason") for c in status.get("pauseConditions") or []}
    return i if "CanaryPauseStep" in reasons else None


def resume(namespace: str) -> None:
    """Resume a paused canary, as `kubectl argo rollouts promote` does."""
    kubectl(
        "-n",
        namespace,
        "patch",
        "rollout",
        "podinfo",
        "--type",
        "merge",
        "-p",
        '{"spec":{"paused":false}}',
    )
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
        '{"status":{"pauseConditions":null}}',
    )


def pace_canary(
    cluster,
    image: str,
    timeout: int,
    resume_fn,
    hold_s: float = HOLD_S,
    on_resume=None,
    sleep=time.sleep,
    clock=time.monotonic,
    watch=None,
) -> str:
    """Agents-mode recording (D54): nothing is decided live. Each indefinite pause is held for
    `hold_s`, then resumed, so every canary reaches 100% and L0 (replayed), L3 and L4 are judged
    offline on the same path. A resume that has not taken effect is retried after 30 s."""
    deadline = clock() + timeout
    held: dict[int, float] = {}
    while clock() < deadline:
        rollout = cluster.rollout()
        if watch is not None:  # the step log (recorder.RolloutStepLog)
            watch(rollout)
        outcome = deployer.rollout_outcome(rollout, image)
        if outcome:
            return outcome
        i = indefinite_pause(rollout)
        if i is not None:
            since = held.setdefault(i, clock())
            if clock() - since >= hold_s:
                resume_fn()
                if on_resume:
                    on_resume(i)
                held[i] = clock() - hold_s + 30
        sleep(5)
    return "timeout"


QUIET_S = 120  # D50: every L0 alert resolved for at least this long before a scenario starts
QUIET_TIMEOUT_S = 900
NO_CLUSTER = {"ci", "pipeline"}
L0_ALERTS = "HighErrorRatio|HighLatencyP99|ContainerOOMKilled|PodCrashLooping|ContainerHighMemory"


def firing_within(seconds: int, base: str = recorder.PROMETHEUS) -> list[str]:
    """L0 alerts that fired at any time in the last `seconds` (Prometheus' own ALERTS series)."""
    query = (
        f'max by (alertname) (max_over_time(ALERTS{{alertstate="firing",'
        f'alertname=~"{L0_ALERTS}"}}[{seconds}s]))'
    )
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    result = recorder.http_json(url).get("data", {}).get("result", [])
    return sorted(r["metric"].get("alertname", "") for r in result)


RESULT_METRICS = "flowguard_result"  # the scored results (evaluation/results_exporter.py)


def results_in_prometheus(base: str = recorder.PROMETHEUS) -> int:
    """How many series of the scored results Prometheus holds now (one instant query). The
    agents read this Prometheus, so a run must not start while any is there (D25)."""
    query = f'count({{__name__=~"{RESULT_METRICS}.*"}})'
    url = f"{base}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    result = recorder.http_json(url).get("data", {}).get("result", [])
    return int(float(result[0]["value"][1])) if result else 0


@contextmanager
def run_lock(run_id: str):
    """recorder.RUN_LOCK for the length of a cluster run: one run at a time, and no results
    exporter started meanwhile. A lock left by a run that died must be removed by hand."""
    path = recorder.RUN_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(
                json.dumps({"run_id": run_id, "pid": os.getpid(), "since": recorder.iso(utc())})
            )
    except FileExistsError:
        held = path.read_text(encoding="utf-8")[:200]
        raise RuntimeError(
            f"{path} is held ({held}): another evaluation run is under way, or one died and "
            "left it; remove it only if no run is going on"
        ) from None
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def wait_alerts_quiet(
    firing=firing_within,
    quiet_s: int = QUIET_S,
    timeout_s: int = QUIET_TIMEOUT_S,
    sleep=time.sleep,
    clock=time.monotonic,
) -> list[str]:
    """Wait until no L0 alert has fired for `quiet_s`; [] when quiet, else the alerts still
    firing at the timeout."""
    deadline = clock() + timeout_s
    while True:
        names = firing(quiet_s)
        if not names:
            return []
        if clock() >= deadline:
            return names
        sleep(15)


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
    def __init__(self, spec: dict, run_id: str | None = None, mode: str = "l0"):
        self.spec = spec
        self.mode = mode  # "agents": recorded under the agents-mode canary (D54)
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
        # Every Rollout step change while a release runs, for the offline timeline (D54)
        self.steps = recorder.RolloutStepLog(RUNS / recorder.ROLLOUT_EVENTS, self.env)
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
        """A cluster run starts only when Prometheus holds no scored result and no other run
        holds the lock; branch-only families (ci, pipeline) need neither."""
        if self.spec["family"] in NO_CLUSTER:
            return self._run()
        if found := results_in_prometheus():
            raise RuntimeError(
                f"Prometheus holds {found} {RESULT_METRICS} series: stop "
                "evaluation/results_exporter.py and wait for them to go stale before a run (D25)"
            )
        with run_lock(self.run_id):
            return self._run()

    def _run(self) -> Path:
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
            # D50: no fault while an L0 alert from an earlier run may still be firing (the ci
            # and pipeline families run on branches, without the cluster: D44)
            if self.mode == "agents" and delivery != "config":
                raise ValueError("agents-mode recording is for config-delivered releases (D54)")
            if self.spec["family"] not in NO_CLUSTER and (still := self.wait_quiet()):
                self.observed["alerts_not_quiet"] = still
                self.event("alerts_not_quiet", alerts=still, quiet_s=QUIET_S)
            elif delivery == "config":
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
            if self.mode == "agents" and self.observed.get("canary") != "promoted":
                # D54: the canary did not reach 100%, so the recording lacks part of its path
                self.observed["canary_incomplete"] = True
            if "alerts_not_quiet" in self.observed:
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
                "valid": not {"host_suspended", "alerts_not_quiet", "canary_incomplete"}
                & set(self.observed),
            },
            truth_db=self.truth_db,
            github=recorder.GitHubReader(cfg.repo, cfg.token),
            recording_id=self.run_id,
        )

    def wait_quiet(self) -> list[str]:
        return wait_alerts_quiet()

    # ------------------------------------------------------------ config delivery

    def healthy_stable(self) -> dict:
        rollout = self.cluster.rollout()
        status = rollout.get("status", {})
        if status.get("phase") != "Healthy" or status.get("stableRS") != status.get(
            "currentPodHash"
        ):
            raise RuntimeError(f"{self.env} Rollout is not Healthy and stable; refusing to start")
        if (canary_mode(rollout) == "agents") != (self.mode == "agents"):
            raise RuntimeError(f"{self.env} Rollout is not in {self.mode} mode; refusing to start")
        return rollout

    def run_config(self) -> Path:
        saved = deployer.container(self.healthy_stable())
        image, command = saved["image"], list(saved.get("command") or BASE_COMMAND)
        stress = self.spec.get("node_stress")
        log = self.loadgen.start(self.run_id)
        try:
            if stress:
                self.start_stress(
                    int(stress.get("cpu_workers", 4)), str(stress.get("cpu_request", "10m"))
                )
            time.sleep(self.timing["warmup_s"])
            # Same image and command; only the release Secret differs, for good releases too
            settings = release_settings(self.spec["fault"])
            secret = deployer.create_release_settings(self.env, settings)
            self.release(image, command, env_from(secret), log)
            self.event("fault_injected", delivery="config", secret=secret, settings=settings)
            outcome = self.wait_release(image)
            if self.mode == "agents":  # D54: L0 is replayed offline from the recording
                self.observed.update(mode="agents", canary=outcome, l0="offline")
                self.event("canary_outcome", outcome=outcome)
            else:
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

    def wait_release(self, image: str, hold_s: float = HOLD_S) -> str:
        if self.mode == "agents":
            outcome = pace_canary(
                self.cluster,
                image,
                max(self.timing["observe_s"], AGENTS_OBSERVE_S),
                lambda: resume(self.env),
                hold_s,
                on_resume=lambda step: self.event("canary_resumed", step=step),
                watch=self.steps.observe,
            )
        else:
            outcome = wait_outcome(
                self.cluster, image, self.timing["observe_s"], watch=self.steps.observe
            )
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
            outcome = self.wait_release(image, hold_s=0)  # agents-mode: not measured
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

    def start_stress(self, workers: int, cpu_request: str = "10m") -> None:
        """A batch job on the node: `workers` busy loops. Its CPU request sets its weight
        against podinfo's (10m each), so a large request is a noisy neighbour (D44)."""
        pod = json.loads(json.dumps(STRESS_POD))
        pod["metadata"]["name"] = f"batch-worker-{self.run_id[2:]}"
        pod["spec"]["containers"][0]["resources"]["requests"]["cpu"] = cpu_request
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
        self.truth["node_stress"] = {
            "cpu_workers": workers,
            "cpu_request": cpu_request,
            "pod": pod["metadata"]["name"],
        }
        self.event("node_stress_started", workers=workers, cpu_request=cpu_request)

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
                self.record_fix_ci(github)
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

    def wait_ci(
        self, github, sha: str, timeout: int = CI_TIMEOUT_S, sleep=time.sleep
    ) -> dict | None:
        """Poll until the commit's app-ci run completes; network hiccups are retried."""
        return self._poll(lambda: github.run_for_commit(sha), time.monotonic() + timeout, sleep)

    def record_fix_ci(
        self, github, timeout: int = CI_TIMEOUT_S, sleep=time.sleep, clock=time.monotonic
    ) -> dict[str, str]:
        """D57: for each pin_dependency executed in this run, the conclusion of the app-ci run
        it started on its fix branch, polled until it completes or the scenario's CI timeout
        (shared by all pins) ends. Written to the audit log at the pin's level, as the CI result
        a real system sees: "no run" or "timeout" when there is none, and scoring counts only
        "success" (report_data)."""
        pins = self.audit.query(
            "SELECT DISTINCT p.id, p.level FROM proposals p JOIN executions e "
            "ON e.proposal_id = p.id WHERE p.scenario_id = ? AND p.action = 'pin_dependency' "
            "AND e.success = 1 ORDER BY p.id",
            (self.run_id,),
        )
        deadline = clock() + timeout
        conclusions = {}
        for pin in pins:
            branch = github_actions.fix_branch(pin["id"])
            path = (
                f"/repos/{github.cfg.repo}/actions/workflows/{github_actions.WORKFLOW}/runs"
                f"?branch={urllib.parse.quote(branch, safe='')}&event=workflow_dispatch&per_page=1"
            )
            run = self._poll(
                lambda path=path: next(iter(github.get(path).get("workflow_runs") or []), None),
                deadline,
                sleep,
                clock,
            )
            if run is None:
                conclusion = "no run"
            elif run.get("status") != "completed":
                conclusion = "timeout"
            else:
                conclusion = run.get("conclusion") or "unknown"
            AuditLog(self.audit.path, self.run_id, pin["level"]).event(
                "ci",
                "fix_ci_completed",
                proposal_id=pin["id"],
                branch=branch,
                run_id=run.get("id") if run else None,
                conclusion=conclusion,
            )
            conclusions[pin["id"]] = conclusion
        return conclusions

    def _poll(self, fetch, deadline: float, sleep=time.sleep, clock=time.monotonic):
        """The run `fetch` returns, polled until it completes or the deadline passes. It is
        fetched at least once, so a run that completed while another was awaited is seen."""
        run = None
        while True:
            try:
                run = fetch()
            except deployer.RateLimited as exc:
                wait = max(30, exc.reset_at - int(time.time()) + 5)
            except OSError as exc:  # URLError, timeouts, TLS handshake failures
                self.event("poll_retry", detail=str(exc)[:200])
                wait = 30
            else:
                if run and run.get("status") == "completed":
                    return run
                wait = 30
            if clock() >= deadline:
                return run
            sleep(wait)


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
