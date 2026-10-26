"""Scenario runner (Step 4a): executes one scenario spec, records it (D15) and cleans up.

    python scenarios/runner.py scenarios/dev/release-errors-05.yaml
    python scenarios/runner.py --validate scenarios/dev/*.yaml

A spec (YAML, see scenarios/README.md) names a family, a fault, its intensity, the timing and
the ground truth. Dev specs live in scenarios/dev/; scenarios/heldout/ is gitignored and kept
for the person who writes the held-out set (D12).

Deliveries:
  config  release family: the same image with fault flags in the Rollout's pod template, which
          starts a canary under L0 (the Step 2 mechanism, D19 amended).
  commit  a change pushed on a throwaway scenario/<run-id> branch: app-ci builds it, then the
          deployer's L0 security gate and staging canary handle it like any release (release
          and security families); for the ci family the failed CI run is the scenario.
Every scenario writes audit events under its run id, is recorded into
evaluation/recordings/<run-id>/ with the ground truth and what L0 did, and is cleaned up: the
staging template is restored and the scenario branch deleted.
"""

from __future__ import annotations

import argparse
import json
import os
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
    "security": {"kinds": {"leaked_secret"}, "deliveries": {"commit"}},
    "ci": {"kinds": set(ci_failures.CLASSES), "deliveries": {"commit"}},
}
EXPECTED_L0 = {"promote", "abort", "block", "ci_failure"}
BASE_COMMAND = ["./podinfo", "--port=9898", "--level=info", "--port-metrics=9797"]


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
    if family == "runtime":
        return errors + ["family runtime (DSO1) is not supported before the Monitor step"]
    if family not in FAMILIES:
        return errors + [f"family must be one of {sorted(FAMILIES)}"]
    fault = spec["fault"] or {}
    if fault.get("kind") not in FAMILIES[family]["kinds"]:
        errors.append(f"fault.kind for {family} must be one of {sorted(FAMILIES[family]['kinds'])}")
    if fault.get("delivery") not in FAMILIES[family]["deliveries"]:
        errors.append(
            f"fault.delivery for {family} must be one of {sorted(FAMILIES[family]['deliveries'])}"
        )
    for key in ("error_rate", "fraction"):
        if key in fault and not 0 <= float(fault[key]) <= 1:
            errors.append(f"fault.{key} must be within 0..1")
    for key in ("latency_ms", "jitter_ms"):
        if key in fault and int(fault[key]) < 0:
            errors.append(f"fault.{key} must be >= 0")
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


def run_id_for(spec: dict, now: datetime | None = None) -> str:
    return f"{spec['id']}-{(now or datetime.now(UTC)).strftime('%Y%m%d-%H%M%S')}"


def l0_correct(expected: str, observed: str) -> bool:
    return {
        "promote": "promoted",
        "abort": "aborted",
        "block": "blocked",
        "ci_failure": "failure",
    }.get(expected) == observed


# ---------------------------------------------------------------- cluster helpers


def patch_template(namespace: str, image: str, command: list[str]) -> None:
    ops = [
        {"op": "replace", "path": "/spec/template/spec/containers/0/command", "value": command},
        {"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": image},
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(ops, f)
    try:
        subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "patch",
                "rollout",
                "podinfo",
                "--type",
                "json",
                "--patch-file",
                f.name,
            ],
            check=True,
            capture_output=True,
            text=True,
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


def utc() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- runner


class Runner:
    def __init__(self, spec: dict, run_id: str | None = None):
        self.spec = spec
        self.run_id = run_id or run_id_for(spec)
        self.env = spec.get("environment", "staging")
        self.timing = {"warmup_s": 30, "observe_s": 600, "cooldown_s": 20, **spec.get("timing", {})}
        self.rate = float((spec.get("load") or {}).get("rate", 10))
        self.audit = AuditLog.open(scenario_id=self.run_id, level="L0")
        self.cluster = deployer.Cluster(self.env)
        self.loadgen = deployer.LoadGenerator(self.env)
        self.observed: dict = {}

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
            ground_truth=self.spec["ground_truth"],
        )
        loadgen_files: list[Path] = []
        ci_runs: list[int] = []
        try:
            if delivery == "config":
                loadgen_files.append(self.run_config())
            else:
                ci_runs, loadgen_files = self.run_commit()
        finally:
            end = utc()
            expected = self.spec["ground_truth"]["expected_l0"]
            observed = self.observed.get("l0", "unknown")
            self.event(
                "scenario_finished",
                observed=self.observed,
                l0_correct=l0_correct(expected, observed),
            )
        return recorder.record(
            self.run_id,
            start,
            end,
            loadgen_files=loadgen_files,
            ci_runs=ci_runs,
            ground_truth=self.spec["ground_truth"],
            extra={
                "spec": self.spec,
                "observed": self.observed,
                "l0_correct": l0_correct(
                    self.spec["ground_truth"]["expected_l0"], self.observed.get("l0", "unknown")
                ),
            },
            github=recorder.GitHubReader(
                deployer.load_config(ROOT / ".env").repo, deployer.load_config(ROOT / ".env").token
            ),
            recording_id=self.run_id,
        )

    # ------------------------------------------------------------ config delivery

    def run_config(self) -> Path:
        before = self.cluster.rollout()
        status = before.get("status", {})
        if status.get("phase") != "Healthy" or status.get("stableRS") != status.get(
            "currentPodHash"
        ):
            raise RuntimeError(f"{self.env} Rollout is not Healthy and stable; refusing to start")
        saved = deployer.container(before)
        image, command = saved["image"], list(saved.get("command") or BASE_COMMAND)
        log = self.loadgen.start(self.run_id)
        try:
            time.sleep(self.timing["warmup_s"])
            fault_cmd = BASE_COMMAND + [f"--ui-message=Scenario {self.run_id}"]
            fault_cmd += fault_flags(self.spec["fault"])
            patch_template(self.env, image, fault_cmd)
            self.event("fault_injected", delivery="config", image=image, command=fault_cmd)
            outcome = wait_outcome(self.cluster, image, self.timing["observe_s"])
            self.observed["l0"] = outcome
            self.event("l0_outcome", outcome=outcome)
            time.sleep(self.timing["cooldown_s"])
        finally:
            self.restore(image, command)
            self.loadgen.stop()
        return log

    def restore(self, image: str, command: list[str]) -> None:
        """Back to the pre-scenario template; a promoted fault needs a canary back (with load)."""
        patch_template(self.env, image, command)
        outcome = wait_outcome(self.cluster, image, self.timing["observe_s"])
        self.observed["restore"] = outcome
        self.event("template_restored", outcome=outcome)

    # ------------------------------------------------------------ commit delivery

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def apply_change(self) -> dict:
        fault, family = self.spec["fault"], self.spec["family"]
        if family == "ci":
            change = ci_failures.apply(ROOT, fault["kind"])
            return {
                "files": list(change.files),
                "summary": change.summary,
                "expected_failing_job": change.expected_failing_job,
            }
        if family == "security":
            rel = "app/scenario-settings.env"
            # A fresh random value each run: a generic API key Gitleaks must catch
            (ROOT / rel).write_text(
                f'SCENARIO_API_KEY="{secrets.token_hex(20)}"\n', encoding="utf-8", newline="\n"
            )
            return {"files": [rel], "summary": "a hard-coded API key committed with the app"}
        # release family through CI: change the fault flags' defaults in podinfo's code
        rel = Path("app/podinfo/cmd/podinfo/main.go")
        text = (ROOT / rel).read_text(encoding="utf-8")
        for flag, value in (
            ("fault-error-rate", fault.get("error_rate", 0)),
            ("fault-latency-ms", fault.get("latency_ms", 0)),
            ("fault-jitter-ms", fault.get("jitter_ms", 0)),
        ):
            text, n = re.subn(rf'(fs\.\w+\("{flag}", )[^,]+,', rf"\g<1>{value},", text, count=1)
            if n != 1:
                raise ValueError(f"main.go: flag {flag} not found")
        (ROOT / rel).write_text(text, encoding="utf-8", newline="\n")
        return {"files": [str(rel)], "summary": "fault enabled by default in the code"}

    def run_commit(self) -> tuple[list[int], list[Path]]:
        if self.git("rev-parse", "--abbrev-ref", "HEAD") != "dev" or self.git(
            "status", "--porcelain"
        ):
            raise RuntimeError("commit delivery needs a clean dev checkout")
        cfg = deployer.load_config(ROOT / ".env")
        github = deployer.GitHub(cfg)
        branch = f"scenario/{self.run_id}"
        before = self.cluster.rollout()
        saved = deployer.container(before)
        self.git("switch", "-q", "-c", branch)
        pushed = False
        try:
            change = self.apply_change()
            self.git("add", *change["files"])
            self.git("commit", "-q", "-m", f"Scenario {self.spec['id']}: {change['summary']}")
            sha = self.git("rev-parse", "HEAD")
            self.git("push", "-q", "-u", "origin", branch)
            pushed = True
            self.event("fault_injected", delivery="commit", branch=branch, sha=sha, **change)
            run = self.wait_ci(github, sha)
            ci_runs = [run["id"]] if run else []
            self.observed["ci"] = run.get("conclusion") if run else "no run"
            if self.spec["family"] == "ci":
                self.observed["l0"] = self.observed["ci"]
                self.event("l0_outcome", outcome=self.observed["l0"], run_id=ci_runs[:1])
                return ci_runs, []
            if not run or run.get("conclusion") != "success":
                self.observed["l0"] = f"ci_{self.observed['ci']}"
                return ci_runs, []
            # release / security: the deployer handles the image like any release, staging only
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
            log = deployer.RUNS_DIR / f"deploy-{self.env}-{sha[:12]}.jsonl"
            return ci_runs, [log]
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("specs", nargs="+", type=Path)
    parser.add_argument("--validate", action="store_true", help="only check the specs")
    args = parser.parse_args(argv)
    if args.validate:
        bad = 0
        for path in args.specs:
            errors = validate_spec(yaml.safe_load(path.read_text(encoding="utf-8")))
            print(
                f"{'OK  ' if not errors else 'FAIL'} {path}" + ("" if not errors else f": {errors}")
            )
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
                "observed": manifest["observed"],
                "l0_correct": manifest["l0_correct"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
