"""Fail-fast analysis must retain paired cohorts, mandatory prefixes and actual observations."""

from datetime import UTC, datetime, timedelta

from optimizer import failfast
from optimizer.features import FEATURES


def job(name, seconds, conclusion="success"):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": start.isoformat(),
        "completed_at": (start + timedelta(seconds=seconds)).isoformat(),
    }


def test_reordering_can_catch_failure_earlier_without_requiring_later_scan(monkeypatch):
    monkeypatch.setattr(
        failfast,
        "predict",
        lambda model, row: (100, 0.9 if row[FEATURES.index("job_integration")] else 0.1),
    )
    run = {
        "run_id": 1,
        "sha": "fixture",
        "created_at": "2026-01-01T00:00:00+00:00",
        "files": [{"filename": "app/code.go", "additions": 1, "deletions": 0}],
        "jobs": [
            job("build", 10),
            job("unit", 5),
            job("lint", 20),
            job("integration", 3, "failure"),
        ],
    }
    catalog = {
        "order": ["lint", "integration"],
        "jobs": {"lint": {"paths": ["*"]}, "integration": {"paths": ["*"]}},
    }
    result = failfast.evaluate({}, [run], catalog)
    assert result["baseline_mean_seconds_to_failure"] == 38
    assert result["reordered_mean_seconds_to_failure"] == 18
    assert result["failures_caught_strictly_earlier"] == 1
    assert result["baseline_failures_on_first_optional_job"] == 0
    assert result["reordered_failures_on_first_optional_job"] == 1
    # An earlier mandatory failure cannot be improved by optional ordering.
    run["jobs"] = [job("build", 7, "failure")]
    result = failfast.evaluate({}, [run], catalog)
    assert result["paired_failed_runs"] == 1 and result["total_seconds_saved"] == 0
    assert result["optional_failure_cases"] == 0
    # An unobserved preceding job cannot be treated as zero seconds or a success.
    run["jobs"] = [job("unit", 5, "failure")]
    result = failfast.evaluate({}, [run], catalog)
    assert result["paired_failed_runs"] == 0
    assert result["excluded_unobserved_prefix_run_ids"] == [1]


def test_successes_have_no_failure_time():
    assert failfast.first_failure(["build"], {"build": {"duration": 3, "failure": 0}}) is None
