"""Recorder (D15): saves the raw inputs of one scenario window so every ablation level can be
evaluated offline on the identical data.

    python evaluation/recorder.py --scenario dev-001 --start 2026-10-26T18:00:00Z \
        --end 2026-10-26T18:10:00Z [--loadgen FILE ...] [--ci-run RUN_ID ...] \
        [--ground-truth truth.json]

writes evaluation/recordings/<recording-id>/ (gitignored):
    manifest.json            window, sources with counts, Prometheus selectors, git commit,
                             whether the run is valid; nothing about what was injected
    ground_truth.json        what really happened: ground truth, spec, what L0 did (D25: the
                             only file of a recording that holds it; also in the ground-truth DB)
    prometheus/<name>.json   raw samples of each selector over the window (not resampled)
    loadgen.jsonl            per-request log lines inside the window
    deployer-events.jsonl    deployer events inside the window
    audit-events.jsonl       audit-log events inside the window
    ci/<run>/                run, jobs (with step results), annotations; job logs when the
                             GitHub token may read them (otherwise marked unavailable)

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "evaluation" / "recordings"
RUNS_DIR = ROOT / "evaluation" / "runs"
PROMETHEUS = "http://localhost:9090"
# Held by the scenario runner during a cluster run; results_exporter.py will not start while
# it exists, so no scored result reaches the Prometheus the agents read during a run (D25)
RUN_LOCK = ROOT / "state" / "run.lock"

# Raw series, so a replay can compute any metric later (rates, ratios, quantiles, per pod).
SELECTORS = {
    "podinfo_requests": 'http_request_duration_seconds_count{app="podinfo"}',
    "podinfo_latency_buckets": 'http_request_duration_seconds_bucket{app="podinfo",path="root"}',
    "podinfo_latency_sum": 'http_request_duration_seconds_sum{app="podinfo",path="root"}',
    "podinfo_up": 'up{app="podinfo"}',
    "container_memory": (
        'container_memory_working_set_bytes{namespace=~"staging|prod",container="podinfo"}'
    ),
    "container_cpu": (
        'container_cpu_usage_seconds_total{namespace=~"staging|prod",container="podinfo"}'
    ),
    "pod_restarts": 'kube_pod_container_status_restarts_total{namespace=~"staging|prod"}',
    # The DSO1 L0 alert rules (D33), pending and firing, as Prometheus evaluated them live
    "alerts": 'ALERTS{alertname=~"HighErrorRatio|HighLatencyP99|ContainerOOMKilled'
    '|PodCrashLooping|ContainerHighMemory"}',
    # D49: memory limits and node load, so replayed P1 answers see node stress; the same
    # names and queries as flowguard.series. Node usage is cAdvisor's root cgroup (no
    # node-exporter in the stack)
    "memory_limits": (
        'kube_pod_container_resource_limits{namespace=~"staging|prod",container="podinfo",'
        'resource="memory"}'
    ),
    "node_cpu": 'container_cpu_usage_seconds_total{id="/"}',
    "node_memory": 'container_memory_working_set_bytes{id="/"}',
    "node_allocatable": 'kube_node_status_allocatable{resource=~"cpu|memory"}',
    "node_capacity": 'kube_node_status_capacity{resource=~"cpu|memory"}',
}


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def in_window(ts: str | None, start: datetime, end: datetime) -> bool:
    if not ts:
        return False
    try:
        t = parse_ts(ts)
    except ValueError:
        return False
    return start <= t <= end


def filter_jsonl(sources: list[Path], start: datetime, end: datetime, ts_key: str = "ts"):
    """Lines of JSONL files whose timestamp is inside [start, end], in file order."""
    for path in sources:
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if in_window(record.get(ts_key), start, end):
                    yield record


def write_jsonl(path: Path, records) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, sort_keys=True) + "\n")
            n += 1
    return n


def http_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        url, headers={"User-Agent": "flowguard-recorder", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def record_prometheus(out: Path, start: datetime, end: datetime, base: str = PROMETHEUS) -> dict:
    """Raw samples: an instant query of `selector[window]` at the window's end."""
    seconds = max(1, int((end - start).total_seconds()))
    summary = {}
    for name, selector in SELECTORS.items():
        query = f"{selector}[{seconds}s]"
        url = f"{base}/api/v1/query?" + urllib.parse.urlencode(
            {"query": query, "time": end.timestamp()}
        )
        try:
            data = http_json(url)
            result = data.get("data", {}).get("result", [])
            samples = sum(len(s.get("values", [])) for s in result)
            (out / "prometheus").mkdir(parents=True, exist_ok=True)
            (out / "prometheus" / f"{name}.json").write_text(
                json.dumps({"query": query, "time": iso(end), "result": result}), encoding="utf-8"
            )
            summary[name] = {"series": len(result), "samples": samples}
        except (urllib.error.URLError, OSError, ValueError) as exc:
            summary[name] = {"error": str(exc)[:200]}
    return summary


class _TokenStaysOnHost(urllib.request.HTTPRedirectHandler):
    """GitHub answers a job-log request with a redirect to log storage on another host, which
    rejects (401) a forwarded API token and must not see it: drop it when the host changes."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old_host = urllib.parse.urlparse(req.full_url).netloc
            if urllib.parse.urlparse(newurl).netloc != old_host:
                new.remove_header("Authorization")
        return new


class GitHubReader:
    """Read-only access to CI runs; job logs need a token allowed to read them."""

    def __init__(self, repo: str, token: str = "", api: str = "https://api.github.com"):
        self.repo, self.token, self.api = repo, token, api.rstrip("/")

    def headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str):
        return http_json(self.api + path, self.headers())

    def job_log(self, job_id: int) -> tuple[str | None, str | None]:
        """(log text, None) or (None, reason it is unavailable)."""
        url = f"{self.api}/repos/{self.repo}/actions/jobs/{job_id}/logs"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "flowguard-recorder", **self.headers()}
            )
            opener = urllib.request.build_opener(_TokenStaysOnHost)
            with opener.open(req, timeout=60) as r:
                return r.read().decode("utf-8", errors="replace"), None
        except urllib.error.HTTPError as exc:
            reason = exc.read()[:200].decode(errors="replace")
            return None, f"HTTP {exc.code}: {reason}"


def record_ci(out: Path, run_ids: list[int], gh: GitHubReader) -> list[dict]:
    summary = []
    for run_id in run_ids:
        d = out / "ci" / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        entry = {"run_id": run_id}
        try:
            run = gh.get(f"/repos/{gh.repo}/actions/runs/{run_id}")
            jobs = gh.get(f"/repos/{gh.repo}/actions/runs/{run_id}/jobs?per_page=100")["jobs"]
            (d / "run.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
            (d / "jobs.json").write_text(json.dumps(jobs, indent=1), encoding="utf-8")
            annotations, logs = {}, {}
            for job in jobs:
                annotations[job["name"]] = gh.get(
                    f"/repos/{gh.repo}/check-runs/{job['id']}/annotations"
                )
                if job.get("conclusion") == "skipped":  # never ran, so GitHub keeps no log
                    logs[job["name"]] = "not run"
                    continue
                text, why = gh.job_log(job["id"])
                safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in job["name"])
                if text is not None:
                    (d / "logs").mkdir(exist_ok=True)
                    (d / "logs" / f"{safe}.txt").write_text(text, encoding="utf-8")
                    logs[job["name"]] = "saved"
                else:
                    logs[job["name"]] = f"unavailable ({why})"
            (d / "annotations.json").write_text(json.dumps(annotations, indent=1), encoding="utf-8")
            entry.update(
                conclusion=run.get("conclusion"),
                head_sha=run.get("head_sha"),
                head_branch=run.get("head_branch"),
                jobs={j["name"]: j.get("conclusion") for j in jobs},
                logs=logs,
            )
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            entry["error"] = str(exc)[:200]
        summary.append(entry)
    return summary


def audit_events(start: datetime, end: datetime) -> list[dict]:
    try:
        sys.path.insert(0, str(ROOT / "agents"))
        from flowguard.audit import AuditLog

        return [e for e in AuditLog.open().events() if in_window(e["ts"], start, end)]
    except Exception as exc:  # the recording must not fail because the audit log is missing
        return [{"error": f"audit log unavailable: {exc}"}]


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def record(
    scenario_id: str,
    start: datetime,
    end: datetime,
    *,
    loadgen_files: list[Path] = (),
    ci_runs: list[int] = (),
    ground_truth: dict | None = None,
    evaluation: dict | None = None,
    truth_db=None,
    github: GitHubReader | None = None,
    prometheus: str = PROMETHEUS,
    out_root: Path = RECORDINGS,
    recording_id: str | None = None,
) -> Path:
    """Write one recording and return its folder. `evaluation` holds the spec, what L0 did
    (`observed`, `l0_correct`) and `valid`: they go to ground_truth.json and, with
    `truth_db` (a GroundTruthDB), to the ground-truth DB, never to the manifest."""
    rid = recording_id or f"{scenario_id}-{start.strftime('%Y%m%dT%H%M%SZ')}"
    out = out_root / rid
    out.mkdir(parents=True, exist_ok=True)
    sources = {
        "prometheus": record_prometheus(out, start, end, prometheus),
        "loadgen": {
            "files": [str(p) for p in loadgen_files],
            "lines": write_jsonl(
                out / "loadgen.jsonl", filter_jsonl(list(loadgen_files), start, end)
            ),
        },
        "deployer_events": {
            "lines": write_jsonl(
                out / "deployer-events.jsonl",
                filter_jsonl([RUNS_DIR / "deployer-events.jsonl"], start, end),
            )
        },
        "audit_events": {
            "lines": write_jsonl(out / "audit-events.jsonl", audit_events(start, end))
        },
        "ci": record_ci(out, list(ci_runs), github) if ci_runs and github else [],
    }
    evaluation = evaluation or {}
    valid = bool(evaluation.get("valid", True))
    truth = {"ground_truth": ground_truth or {}, **evaluation, "valid": valid}
    (out / "ground_truth.json").write_text(
        json.dumps(truth, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    if truth_db is not None:
        truth_db.record_run(
            rid,
            spec=evaluation.get("spec") or {},
            ground_truth=ground_truth or {},
            observed=evaluation.get("observed") or {},
            l0_correct=evaluation.get("l0_correct"),
            valid=valid,
            recording=str(out),
        )
    manifest = {
        "recording_id": rid,
        "scenario_id": scenario_id,
        "window": {"start": iso(start), "end": iso(end)},
        "recorded_at": iso(datetime.now(UTC)),
        "git_commit": git_commit(),
        "prometheus_selectors": SELECTORS,
        "sources": sources,
        "valid": valid,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--start", required=True, type=parse_ts)
    parser.add_argument("--end", required=True, type=parse_ts)
    parser.add_argument("--loadgen", action="append", type=Path, default=[])
    parser.add_argument("--ci-run", action="append", type=int, default=[])
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--repo", default="trad024/FlowGuard")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT / "ci"))
    from deployer import load_config  # .env handling lives there

    token = load_config(ROOT / ".env").token
    truth = json.loads(args.ground_truth.read_text(encoding="utf-8")) if args.ground_truth else {}
    out = record(
        args.scenario,
        args.start,
        args.end,
        loadgen_files=args.loadgen,
        ci_runs=args.ci_run,
        ground_truth=truth,
        github=GitHubReader(args.repo, token),
    )
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
