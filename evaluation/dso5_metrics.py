"""D78: the pre-registered DSO5 metrics, computed outside report.py (DSO5 is descriptive).

    python evaluation/dso5_metrics.py fetch --confirm-step-11   # read-only GitHub, saves inputs
    python evaluation/dso5_metrics.py score --confirm-step-11   # the metrics, as JSON

Source: evaluation/runs/dso5-heldout-d76/runs.jsonl only (D76; the pre-D76 dso5-heldout is set
aside). report_scope accepts no DSO5 id and report.py pairs levels with L0, not plan modes, so
DSO5 is scored here and labelled "computed outside report.py".

fetch: for every recorded app-ci run, its jobs (name, conclusion, started_at, completed_at),
and for optimizer-mode runs the plan job's log, from which the plan (with the Optimizer's
per-job predictions) is read. Read-only, with GITHUB_TOKEN_READ from .env; nothing is written
to GitHub. Saved to github.jsonl next to runs.jsonl, one line per run; resumable.

score (pre-registration: dso5_commit_sequence.metrics, metrics.DSO5), modes paired by index:
  - zero-job runs (an app-ci run that started no job; when github.jsonl exists it must cover
    exactly the recorded runs and agree with runs.jsonl on which had jobs, or the score is
    refused): of a change type app-ci skips by design (docs_only) it counts 0 s
    like a docs_only commit with no run and is left out of MAE and AUC only (no plan), counted
    apart; of any other change type the pipeline failed to start: a reliability line counts it
    per mode, its commit is left out of the primary metrics in both modes (a post-hoc
    exclusion, labelled so: the rule was set after run conclusions and job counts were read,
    before any score), and a sensitivity block counts an optimizer run that failed to start
    against the Optimizer, on the paired commits plus those commits (its commit count is
    printed): a failing baseline run there is a missed failure, its failing jobs absent; the
    durations of those commits are reported apart per mode, with the count of runs without a
    duration, never as 0 s. A commit whose baseline run failed to start stays out of both;
  - pipeline duration: pipeline_s per commit. A commit with no app-ci run counts 0 s only if its
    change type is one app-ci's paths filters skip by design (docs_only); any other commit
    without a duration is missing, reported as a count and left out of the paired comparison.
    Totals and medians per mode over the paired commits; the paired difference (optimizer minus
    baseline) of the median and of the total with stats.paired_bootstrap; the relative change
    of the paired total;
  - failures missed vs the full pipeline, on the paired commits (a missing run is counted as
    missing, never as a missed failure): commits whose baseline run concludes failure and
    whose optimizer run does not; jobs that fail in the baseline run and are skipped or absent
    in the optimizer run, whatever the reason (the definition is printed with the counts);
  - time to first failure: paired on the commits that fail in both modes;
  - MAE and AUC of the Optimizer's predictions on the optimizer-mode runs with a plan (their
    count is reported): each job that ran (success or failure) with a prediction; duration =
    completed_at - started_at, failure = conclusion "failure" (as in Step 10's training,
    ci/train_optimizer.py); AUC pooled over jobs, None with one class.
Refuses unless each mode has exactly the integer indices 0..count-1, on one named base, on its
D76 branch.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "agents"),
    str(ROOT / "evaluation"),
    str(ROOT / "scenarios"),
    str(ROOT / "ci"),
]

import heldout_guard  # noqa: E402

SRC = ROOT / "evaluation" / "runs" / "dso5-heldout-d76"
MODES = ("baseline", "optimizer")
BRANCH = "scenario/{mode}/dso5-heldout-d76"
COUNT = 30  # dso5_commit_sequence.count
SKIPPED_BY_DESIGN = {"docs_only"}  # app-ci's paths filters start no run
LABEL = "DSO5, descriptive, computed outside report.py (D78)"
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")


class Refused(SystemExit):
    """The DSO5 rows are not the pre-registered paired sequence."""


def load_rows(src: Path = SRC, count: int = COUNT) -> dict[str, dict[int, dict]]:
    rows = [
        json.loads(x)
        for x in (src / "runs.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    by = {m: {} for m in MODES}
    problems = []
    for r in rows:
        if r.get("mode") not in MODES or r.get("branch") != BRANCH.format(mode=r.get("mode")):
            problems.append("a row of another mode or branch")
            continue
        if not isinstance(r.get("index"), int) or isinstance(r.get("index"), bool):
            problems.append("a row without an integer index")
            continue
        if r["index"] in by[r["mode"]]:
            problems.append("an index recorded twice in one mode")
        by[r["mode"]][r["index"]] = r
    if any(not isinstance(r.get("base"), str) or not r.get("base") for r in rows):
        problems.append("a row without a base")
    elif len({r["base"] for r in rows}) != 1:
        problems.append("rows on more than one base")
    for m in MODES:
        if sorted(by[m]) != list(range(count)):
            problems.append(f"{m}: indices are not 0..{count - 1}")
    if problems:
        raise Refused("refused: " + "; ".join(dict.fromkeys(problems)))
    return by


# ------------------------------------------------------------------ fetch (read-only GitHub)


def plan_from_log(text: str) -> dict | None:
    """The plan JSON plan.py printed (tee plan.json) in the plan job's log, if any."""
    lines = [TIMESTAMP.sub("", line) for line in text.splitlines()]
    body = "\n".join(lines)
    decoder = json.JSONDecoder()
    for i, line in enumerate(lines):
        if line.strip() != "{":
            continue
        start = sum(len(x) + 1 for x in lines[:i])
        try:
            obj, _ = decoder.raw_decode(body[start:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "mode" in obj and "predictions" in obj:
            return obj
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubReader:
    """GET only. Job logs redirect to a signed URL, fetched without the token."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com"):
        self.repo, self.api = repo, api.rstrip("/")
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "flowguard-dso5",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    def get(self, path: str) -> dict:
        """No redirect is followed (urllib would forward the token to another host)."""
        req = urllib.request.Request(self.api + path, headers=self._headers, method="GET")
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as r:
            return json.load(r)

    def job_log(self, job_id: int) -> str:
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}/actions/jobs/{job_id}/logs",
            headers=self._headers,
            method="GET",
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise
            location = exc.headers["Location"]
        plain = urllib.request.Request(location, headers={"User-Agent": "flowguard-dso5"})
        with urllib.request.urlopen(plain, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace")


def fetch(by: dict, gh, out: Path) -> dict:
    """Save each recorded run's jobs, and the optimizer runs' plans; skip runs already saved."""
    path = out / "github.jsonl"
    done = set()
    if path.is_file():
        done = {
            json.loads(x)["run_id"]
            for x in path.read_text(encoding="utf-8").splitlines()
            if x.strip()
        }
    counts = {"runs": 0, "saved": 0, "already": 0, "plans": 0, "plans_missing": 0}
    for mode in MODES:
        for index in sorted(by[mode]):
            run_id = by[mode][index].get("run_id")
            if run_id is None:
                continue
            counts["runs"] += 1
            if run_id in done:
                counts["already"] += 1
                continue
            data = gh.get(f"/repos/{gh.repo}/actions/runs/{run_id}/jobs?per_page=50")
            jobs = [
                {k: j.get(k) for k in ("id", "name", "conclusion", "started_at", "completed_at")}
                for j in data.get("jobs", [])
            ]
            line = {"run_id": run_id, "mode": mode, "index": index, "jobs": jobs}
            if mode == "optimizer":
                plan_job = next((j for j in jobs if j["name"] == "plan"), None)
                plan = plan_from_log(gh.job_log(plan_job["id"])) if plan_job else None
                line["plan"] = plan
                counts["plans" if plan else "plans_missing"] += 1
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
            counts["saved"] += 1
    return counts


# ------------------------------------------------------------------ score


def _job_name(name: str) -> str:
    m = re.fullmatch(r"optional \((.+)\)", name)
    return m.group(1) if m else name


def _seconds(job: dict) -> float | None:
    if not job.get("started_at") or not job.get("completed_at"):
        return None
    start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
    return (end - start).total_seconds()


def zero_jobs(row: dict) -> bool:
    """An app-ci run that started no job (runs.jsonl; github.jsonl must agree: check_sources)."""
    return row.get("run_id") is not None and not row.get("jobs")


def check_sources(by: dict, fetched: list[dict]) -> None:
    """Refuse when runs.jsonl and github.jsonl disagree on whether a run had jobs."""
    fetched_by_run = {x["run_id"]: x for x in fetched}
    recorded = {r["run_id"] for m in MODES for r in by[m].values() if r.get("run_id") is not None}
    if recorded != set(fetched_by_run) or len(fetched_by_run) != len(fetched):
        raise Refused("refused: github.jsonl does not cover exactly the recorded runs")
    for m in MODES:
        for row in by[m].values():
            line = fetched_by_run.get(row.get("run_id"))
            if line is not None and bool(row.get("jobs")) != bool(line.get("jobs")):
                raise Refused("refused: runs.jsonl and github.jsonl disagree on a run's jobs")


JOBS_MISSED = (
    "jobs that conclude failure in the baseline run and are skipped or absent in the optimizer"
    " run, whatever the reason (dropped by the plan, or skipped after an earlier failure)"
)


def _missed(commits: list[int], base: dict, opt: dict, against: set[int] = frozenset()) -> dict:
    """Failures missed on these commits; a commit in `against` has an optimizer run that never
    started, so a failing baseline run there is missed and its failing jobs are absent."""
    runs = jobs = failed = 0
    for i in commits:
        if base[i].get("ci") != "failure":
            continue
        failed += 1
        runs += i in against or opt[i].get("ci") != "failure"
        opt_jobs = {} if i in against else opt[i].get("jobs") or {}
        for name, conclusion in (base[i].get("jobs") or {}).items():
            jobs += conclusion == "failure" and opt_jobs.get(name) in (None, "skipped")
    return {
        "commits": len(commits),
        "baseline_failed_runs": failed,
        "runs_missed": runs,
        "jobs_missed": jobs,
        "jobs_missed_definition": JOBS_MISSED,
    }


def duration(row: dict, zero: bool = False) -> float | None:
    """Seconds; 0 s for a change type app-ci skips by design (no run, or a run with no job)."""
    if zero or row.get("ci") == "no run":
        return 0.0 if row.get("change_type") in SKIPPED_BY_DESIGN else None
    if row.get("pipeline_s") is not None:
        return float(row["pipeline_s"])
    return None


def auc(labels: list[int], scores: list[float]) -> float | None:
    """ROC AUC (Mann-Whitney, ties count half); None with one class."""
    pos = [s for y, s in zip(labels, scores, strict=True) if y]
    neg = [s for y, s in zip(labels, scores, strict=True) if not y]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def prediction_errors(fetched: list[dict], left_out: set[int] = frozenset()) -> dict:
    """Over the optimizer runs with a plan; runs at a left-out index are skipped."""
    errors, labels, scores, jobs = [], [], [], 0
    runs = [x for x in fetched if x.get("mode") == "optimizer" and x.get("index") not in left_out]
    with_plan = [x for x in runs if x.get("plan")]
    for line in with_plan:
        predictions = line["plan"].get("predictions") or {}
        for job in line["jobs"]:
            name = _job_name(job["name"])
            if name not in predictions or job.get("conclusion") not in ("success", "failure"):
                continue
            seconds = _seconds(job)
            if seconds is None:
                continue
            jobs += 1
            errors.append(abs(predictions[name]["duration_s"] - seconds))
            labels.append(int(job["conclusion"] == "failure"))
            scores.append(predictions[name]["failure_probability"])
    return {
        "optimizer_runs_with_plan": len(with_plan),
        "optimizer_runs_without_plan": len(runs) - len(with_plan),
        "jobs": jobs,
        "failures": sum(labels),
        "duration_mae_s": statistics.fmean(errors) if errors else None,
        "failure_auc": auc(labels, scores),
    }


def score(by: dict, fetched: list[dict] | None = None, seed: int = 20261028) -> dict:
    import stats

    base, opt = by["baseline"], by["optimizer"]
    if fetched is not None:
        check_sources(by, fetched)
    zero = {m: {i for i, r in by[m].items() if zero_jobs(r)} for m in MODES}
    # a run with no job of a change type app-ci does not skip by design failed to start: its
    # commit is left out of the primary metrics in both modes (post-hoc, D78) and counted
    failed_start = {
        m: {i for i in zero[m] if by[m][i].get("change_type") not in SKIPPED_BY_DESIGN}
        for m in MODES
    }
    anomalies = failed_start["baseline"] | failed_start["optimizer"]
    docs_zero = {i for m in MODES for i in zero[m]} - anomalies
    commits = [i for i in sorted(base) if i not in anomalies]
    dur = {m: {i: duration(by[m][i], i in zero[m]) for i in commits} for m in MODES}
    # durations of the failed-to-start commits are reported apart (sensitivity), never as 0 s
    missing = {m: [i for i in commits if dur[m][i] is None] for m in MODES}
    paired = [
        i for i in commits if dur["baseline"][i] is not None and dur["optimizer"][i] is not None
    ]
    b = [dur["baseline"][i] for i in paired]
    o = [dur["optimizer"][i] for i in paired]
    counted_0s = {
        m: sum(
            1
            for i in commits
            if dur[m][i] == 0.0 and (by[m][i].get("ci") == "no run" or i in zero[m])
        )
        for m in MODES
    }
    both = [
        i
        for i in commits
        if base[i].get("time_to_first_failure_s") is not None
        and opt[i].get("time_to_first_failure_s") is not None
    ]
    tb = [base[i]["time_to_first_failure_s"] for i in both]
    to = [opt[i]["time_to_first_failure_s"] for i in both]
    total_b, total_o = sum(b), sum(o)
    n = len(base)
    result = {
        "label": LABEL,
        "commits_per_mode": n,
        "reliability": {
            "failed_to_start_0_jobs": {m: len(failed_start[m]) for m in MODES},
            "statement": "; ".join(
                f"{m}-mode pipeline failed to start (0 jobs) in {len(failed_start[m])} of {n}"
                " commits"
                for m in MODES
            ),
            "docs_only_zero_job_runs_counted_0s": {
                m: sum(1 for i in zero[m] if i in docs_zero) for m in MODES
            },
        },
        "primary": {
            "commits": len(commits),
            "label": (
                f"primary: {len(commits)} of {n} commits; post-hoc exclusion of the commits whose"
                " app-ci run failed to start (0 jobs), a rule set after run conclusions and job"
                " counts were read, before any score (D78)"
            ),
        },
        "pipeline_duration": {
            "paired_commits": len(paired),
            "no_run_counted_0s": counted_0s,
            "missing": {m: len(missing[m]) for m in MODES},
            "total_s_paired_commits": {"baseline": total_b, "optimizer": total_o},
            "median_s_paired_commits": {
                "baseline": statistics.median(b) if b else None,
                "optimizer": statistics.median(o) if o else None,
            },
            "relative_change_of_total_paired_commits": (
                (total_o - total_b) / total_b if total_b else None
            ),
            "median_difference": stats.paired_bootstrap(o, b, statistics.median, seed=seed),
            "total_difference": stats.paired_bootstrap(o, b, sum, seed=seed),
        },
        # on the paired commits only (a missing run is counted as missing, not as missed)
        "failures_missed": _missed(paired, base, opt),
        "time_to_first_failure": {
            "paired_commits": len(both),
            "median_s": {
                "baseline": statistics.median(tb) if tb else None,
                "optimizer": statistics.median(to) if to else None,
            },
            "median_difference": stats.paired_bootstrap(to, tb, statistics.median, seed=seed),
        },
    }
    against = failed_start["optimizer"] - failed_start["baseline"]
    result["sensitivity_all_commits"] = {
        "label": (
            "sensitivity: the commits whose optimizer run failed to start counted against the"
            " Optimizer (a failing baseline run there is a missed failure; durations separate)"
        ),
        "failures_missed": _missed(sorted(set(paired) | against), base, opt, against),
        "failed_to_start_durations_s": {
            "commits": len(against),
            **{
                f"{m}_total_s": sum(
                    by[m][i]["pipeline_s"]
                    for i in against
                    if by[m][i].get("pipeline_s") is not None
                )
                for m in MODES
            },
            **{
                f"{m}_without_duration": sum(
                    1 for i in against if by[m][i].get("pipeline_s") is None
                )
                for m in MODES
            },
        },
    }
    if fetched is None:
        result["predictions"] = {"computed": False, "reason": "no fetched plans (github.jsonl)"}
    else:
        docs_zero_opt = docs_zero & zero["optimizer"]
        result["predictions"] = {
            "computed": True,
            "docs_only_zero_job_runs_left_out": len(docs_zero_opt),
            "failed_to_start_commits_left_out": len(anomalies),
            **prediction_errors(fetched, anomalies | docs_zero_opt),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cmd", choices=("fetch", "score"))
    parser.add_argument("--confirm-step-11", action="store_true")
    parser.add_argument("--src", type=Path, default=SRC)
    args = parser.parse_args(argv)
    heldout_guard.require_stage_b(args.confirm_step_11, "dso5_metrics")
    by = load_rows(args.src)
    if args.cmd == "fetch":
        import deployer

        cfg = deployer.load_config(ROOT / ".env")
        if not cfg.token:
            raise SystemExit("GITHUB_TOKEN_READ is not set in .env")
        print(json.dumps(fetch(by, GitHubReader(cfg.repo, cfg.token), args.src)), flush=True)
        return 0
    path = args.src / "github.jsonl"
    fetched = None
    if path.is_file():
        fetched = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]
    print(json.dumps(score(by, fetched), indent=1), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
