"""Paired reorder-only fail-fast projection on observed validation jobs, without imputation."""

import statistics
from datetime import datetime

import plan
from optimizer.features import extract
from optimizer.history import observations
from optimizer.portable import predict


def first_failure(order, jobs):
    elapsed = 0.0
    for name in order:
        if name not in jobs:
            return None
        elapsed += jobs[name]["duration"]
        if jobs[name]["failure"]:
            return elapsed
    return None


def evaluate(model, runs, catalog):
    measured = {r["run_id"]: {} for r in runs}
    for row in observations(runs):
        measured[row["run_id"]][row["job"]] = row
    cases, excluded = [], []
    failures = 0
    for run in runs:
        jobs = measured[run["run_id"]]
        if not any(row["failure"] for row in jobs.values()):
            continue  # A successful run has no time to first failure, not a zero.
        failures += 1
        paths = [f["filename"] for f in run["files"]]
        sizes = {f["filename"]: f for f in run["files"]}
        baseline = plan.baseline_plan(catalog, paths).run
        probabilities = {job: predict(model, extract(paths, job, sizes))[1] for job in baseline}
        reordered = sorted(baseline, key=lambda job: -probabilities[job])
        # Retain exactly the baseline's eligible optional jobs: isolate ordering from skips.
        before = first_failure(["build", "unit", *baseline, "scans"], jobs)
        after = first_failure(["build", "unit", *reordered, "scans"], jobs)
        if before is None or after is None:
            excluded.append(run["run_id"])
            continue
        reached = all(name in jobs and not jobs[name]["failure"] for name in ("build", "unit"))
        optional_failure = reached and any(jobs.get(j, {}).get("failure") for j in baseline)
        failed_at = [
            datetime.fromisoformat(j["completed_at"])
            for j in run["jobs"]
            if j["conclusion"] == "failure" and j["completed_at"]
        ]
        cases.append(
            {
                "run_id": run["run_id"],
                "sha": run["sha"],
                "baseline_order": baseline,
                "likely_failure_first_order": reordered,
                "predicted_failure_probabilities": probabilities,
                "optional_failure_reached": optional_failure,
                "baseline_first_optional_failed": bool(
                    optional_failure and jobs[baseline[0]]["failure"]
                ),
                "reordered_first_optional_failed": bool(
                    optional_failure and jobs[reordered[0]]["failure"]
                ),
                "baseline_execution_seconds_to_failure": before,
                "reordered_execution_seconds_to_failure": after,
                "seconds_saved": before - after,
                "recorded_wall_seconds_to_failure": (
                    min(failed_at) - datetime.fromisoformat(run["created_at"])
                ).total_seconds(),
            }
        )
    return {
        "validation_commits": len(runs),
        "observed_failed_runs": failures,
        "paired_failed_runs": len(cases),
        "excluded_unobserved_prefix_run_ids": excluded,
        "optional_failure_cases": sum(c["optional_failure_reached"] for c in cases),
        "baseline_failures_on_first_optional_job": sum(
            c["baseline_first_optional_failed"] for c in cases
        ),
        "reordered_failures_on_first_optional_job": sum(
            c["reordered_first_optional_failed"] for c in cases
        ),
        "failures_caught_strictly_earlier": sum(c["seconds_saved"] > 0 for c in cases),
        "failures_caught_later": sum(c["seconds_saved"] < 0 for c in cases),
        "baseline_mean_seconds_to_failure": statistics.mean(
            c["baseline_execution_seconds_to_failure"] for c in cases
        )
        if cases
        else None,
        "reordered_mean_seconds_to_failure": statistics.mean(
            c["reordered_execution_seconds_to_failure"] for c in cases
        )
        if cases
        else None,
        "total_seconds_saved": sum(c["seconds_saved"] for c in cases),
        "cases": cases,
        "scope": (
            "Reorder-only projection: descending predicted failure probability, stable ties; "
            "same baseline path-filtered jobs; observed execution durations only. Build/unit "
            "remain first; scans remain last. Queue/setup delays are excluded from projected "
            "times. Recorded wall time is descriptive and has no counterfactual measurement. "
            "Successful runs excluded from failure-time denominator; no unobserved duration "
            "is imputed. This diagnostic order differs from the production p(failure)/duration "
            "policy; unknown prior scan status still preserves the safe run-all order."
        ),
    }
