"""D78: DSO5 metrics outside report.py. Synthetic rows, a fake GitHub reader and a synthetic
job log only: no network, no held-out file."""

import json

import pytest

import dso5_metrics as dm

N = 4


def row(mode, index, change="app_code", ci="success", pipeline=100.0, ttff=None, jobs=None):
    r = {
        "mode": mode,
        "index": index,
        "branch": dm.BRANCH.format(mode=mode),
        "base": "b0",
        "change_type": change,
        "ci": ci,
        "sha": f"{mode}{index}",
        "target": "t",
    }
    if ci != "no run":
        r.update(
            run_id=1000 * (mode == "optimizer") + index,
            pipeline_s=pipeline,
            time_to_first_failure_s=ttff,
            jobs=jobs or {"plan": "success"},
        )
    return r


def write(tmp_path, rows):
    (tmp_path / "runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path


def paired(tmp_path, base, opt):
    return dm.load_rows(write(tmp_path, base + opt), count=N)


def test_refuses_rows_that_are_not_the_paired_sequence(tmp_path):
    good = [row(m, i) for m in dm.MODES for i in range(N)]
    with pytest.raises(dm.Refused, match="indices"):
        dm.load_rows(write(tmp_path, good[:-1]), count=N)
    other = [*good[:-1], {**good[-1], "branch": "scenario/optimizer/dso5-heldout"}]
    with pytest.raises(dm.Refused, match="branch"):
        dm.load_rows(write(tmp_path, other), count=N)
    two_bases = [*good[:-1], {**good[-1], "base": "b1"}]
    with pytest.raises(dm.Refused, match="base"):
        dm.load_rows(write(tmp_path, two_bases), count=N)
    no_index = [*good[:-1], {k: v for k, v in good[-1].items() if k != "index"}]
    with pytest.raises(dm.Refused, match="integer index"):
        dm.load_rows(write(tmp_path, no_index), count=N)
    no_base = [{k: v for k, v in r.items() if k != "base"} for r in good]  # all None: one "base"
    with pytest.raises(dm.Refused, match="without a base"):
        dm.load_rows(write(tmp_path, no_base), count=N)


def test_docs_only_without_a_run_is_0s_and_any_other_missing_run_is_missing(tmp_path):
    base = [
        row("baseline", 0, "docs_only", "no run"),
        row("baseline", 1, pipeline=200.0),
        row("baseline", 2, pipeline=300.0),
        row("baseline", 3, pipeline=400.0),
    ]
    opt = [
        row("optimizer", 0, "docs_only", "no run"),
        row("optimizer", 1, pipeline=100.0),
        row("optimizer", 2, "app_code", "no run"),
        row("optimizer", 3, pipeline=200.0),
    ]
    s = dm.score(paired(tmp_path, base, opt))
    p = s["pipeline_duration"]
    assert p["missing"] == {"baseline": 0, "optimizer": 1}
    assert p["no_run_counted_0s"] == {"baseline": 1, "optimizer": 1}
    assert p["paired_commits"] == 3  # index 2 left out of the pairing
    assert p["total_s_paired_commits"] == {"baseline": 600.0, "optimizer": 300.0}
    assert p["relative_change_of_total_paired_commits"] == -0.5
    assert p["median_difference"]["n"] == 3 and p["total_difference"]["estimate"] == -300.0
    assert s["label"].startswith("DSO5, descriptive, computed outside report.py")


def test_failures_missed_and_time_to_first_failure(tmp_path):
    fail = {"plan": "success", "optional (lint)": "failure", "unit": "success"}
    base = [
        row("baseline", 0, "failing", "failure", ttff=50.0, jobs=fail),
        row("baseline", 1, "failing", "failure", ttff=80.0, jobs=fail),
        row("baseline", 2),
        row("baseline", 3),
    ]
    opt = [
        row("optimizer", 0, "failing", "failure", ttff=30.0, jobs=fail),
        row(
            "optimizer",
            1,
            "failing",
            "success",
            jobs={"plan": "success", "optional (lint)": "skipped", "unit": "success"},
        ),
        row("optimizer", 2),
        row("optimizer", 3),
    ]
    s = dm.score(paired(tmp_path, base, opt))
    assert s["failures_missed"] == {
        "commits": 4,
        "baseline_failed_runs": 2,
        "runs_missed": 1,
        "jobs_missed": 1,
        "jobs_missed_definition": dm.JOBS_MISSED,
    }
    assert "skipped after an earlier failure" in dm.JOBS_MISSED
    t = s["time_to_first_failure"]
    assert t["paired_commits"] == 1 and t["median_s"] == {"baseline": 50.0, "optimizer": 30.0}


def test_a_missing_optimizer_run_is_missing_not_a_missed_failure(tmp_path):
    fail = {"plan": "success", "unit": "failure"}
    base = [row("baseline", i, "failing", "failure", ttff=50.0, jobs=fail) for i in range(N)]
    opt = [
        row("optimizer", 0, "failing", "failure", ttff=40.0, jobs=fail),
        row("optimizer", 1, "failing", "no run"),  # no run on a non-docs commit: missing
        row("optimizer", 2, "failing", "cancelled", pipeline=None),  # no duration: missing
        row("optimizer", 3, "failing", "success", jobs={"plan": "success", "unit": "skipped"}),
    ]
    s = dm.score(paired(tmp_path, base, opt))
    assert s["pipeline_duration"]["missing"] == {"baseline": 0, "optimizer": 2}
    m = s["failures_missed"]
    assert (m["commits"], m["baseline_failed_runs"], m["runs_missed"], m["jobs_missed"]) == (
        2,
        2,
        1,
        1,
    )


def test_zero_job_runs_docs_only_count_0s_and_others_leave_the_commit_out(tmp_path):
    base = [
        row("baseline", 0, "docs_only", "no run"),
        row("baseline", 1, pipeline=200.0, ttff=None),
        row("baseline", 2, "failing", "failure", pipeline=300.0, ttff=60.0,
            jobs={"plan": "success", "unit": "failure"}),
        row("baseline", 3, pipeline=400.0),
    ]  # fmt: skip
    opt = [
        {**row("optimizer", 0, "docs_only", "failure", pipeline=3.0), "jobs": {}},  # by design
        row("optimizer", 1, pipeline=100.0),
        {**row("optimizer", 2, "failing", "failure", pipeline=4.0, ttff=4.0), "jobs": {}},
        row("optimizer", 3, pipeline=200.0),
    ]
    plan = {"mode": "optimizer", "predictions": {"unit": {"duration_s": 10.0,
            "failure_probability": 0.5}}}  # fmt: skip
    t0, t1 = "2026-11-09T10:00:00Z", "2026-11-09T10:00:20Z"
    job = {"name": "unit", "conclusion": "success", "started_at": t0, "completed_at": t1}
    fetched = [
        {"run_id": 1000, "mode": "optimizer", "index": 0, "jobs": [], "plan": None},
        {"run_id": 1001, "mode": "optimizer", "index": 1, "jobs": [job], "plan": plan},
        {"run_id": 1002, "mode": "optimizer", "index": 2, "jobs": [], "plan": None},
        {"run_id": 1003, "mode": "optimizer", "index": 3, "jobs": [job], "plan": plan},
        *({"run_id": i, "mode": "baseline", "index": i, "jobs": [job]} for i in (1, 2, 3)),
    ]
    by = paired(tmp_path, base, opt)
    s = dm.score(by, fetched)
    r = s["reliability"]
    assert r["failed_to_start_0_jobs"] == {"baseline": 0, "optimizer": 1}
    assert r["statement"] == (
        "baseline-mode pipeline failed to start (0 jobs) in 0 of 4 commits; "
        "optimizer-mode pipeline failed to start (0 jobs) in 1 of 4 commits"
    )
    assert r["docs_only_zero_job_runs_counted_0s"] == {"baseline": 0, "optimizer": 1}
    assert s["primary"]["commits"] == 3 and "post-hoc exclusion" in s["primary"]["label"]
    p = s["pipeline_duration"]
    assert p["paired_commits"] == 3 and p["no_run_counted_0s"] == {"baseline": 1, "optimizer": 1}
    assert p["total_s_paired_commits"] == {"baseline": 600.0, "optimizer": 300.0}
    assert s["failures_missed"]["baseline_failed_runs"] == 0  # the failed-to-start pair is out
    assert s["time_to_first_failure"]["paired_commits"] == 0
    sens = s["sensitivity_all_commits"]
    assert sens["failures_missed"]["commits"] == 4
    assert (sens["failures_missed"]["runs_missed"], sens["failures_missed"]["jobs_missed"]) == (
        1,
        1,
    )  # the failing commit whose optimizer run never started is a missed failure
    assert sens["failed_to_start_durations_s"] == {
        "commits": 1,
        "baseline_total_s": 300.0,
        "optimizer_total_s": 4.0,
        "baseline_without_duration": 0,
        "optimizer_without_duration": 0,
    }
    with pytest.raises(dm.Refused, match="cover exactly"):  # a partial fetch
        dm.score(by, fetched[:-1])
    with pytest.raises(dm.Refused, match="cover exactly"):  # a run fetched twice
        dm.score(by, [*fetched, fetched[0]])
    pr = s["predictions"]
    assert pr["docs_only_zero_job_runs_left_out"] == 1
    assert pr["failed_to_start_commits_left_out"] == 1
    assert pr["optimizer_runs_with_plan"] == 2 and pr["optimizer_runs_without_plan"] == 0
    assert pr["duration_mae_s"] == 10.0
    fetched[1] = {**fetched[1], "jobs": []}  # GitHub shows no job where runs.jsonl has jobs
    with pytest.raises(dm.Refused, match="disagree"):
        dm.score(by, fetched)
    fetched[1], fetched[2] = {**fetched[1], "jobs": [job]}, {**fetched[2], "jobs": [job]}
    with pytest.raises(dm.Refused, match="disagree"):  # and the other way round
        dm.score(by, fetched)


def test_a_baseline_run_that_failed_to_start_and_a_missing_duration(tmp_path):
    fail = {"plan": "success", "unit": "failure"}
    base = [
        {**row("baseline", 0, "failing", "failure", pipeline=5.0), "jobs": {}},  # never started
        row("baseline", 1, "failing", "failure", pipeline=300.0, jobs=fail),
        row("baseline", 2, pipeline=200.0),
        row("baseline", 3, pipeline=200.0),
    ]
    opt = [
        row("optimizer", 0, "failing", "failure", pipeline=90.0, jobs=fail),
        {**row("optimizer", 1, "failing", "failure", pipeline=None), "jobs": {}},  # no duration
        row("optimizer", 2, pipeline=100.0),
        row("optimizer", 3, pipeline=100.0),
    ]
    s = dm.score(paired(tmp_path, base, opt))
    assert s["reliability"]["failed_to_start_0_jobs"] == {"baseline": 1, "optimizer": 1}
    assert s["primary"]["commits"] == 2 and s["pipeline_duration"]["paired_commits"] == 2
    sens = s["sensitivity_all_commits"]
    assert sens["failures_missed"]["commits"] == 3  # the baseline-side commit stays out
    assert sens["failures_missed"]["runs_missed"] == 1
    d = sens["failed_to_start_durations_s"]
    assert (
        d["commits"] == 1 and d["optimizer_total_s"] == 0 and d["optimizer_without_duration"] == 1
    )


def test_mae_and_auc_from_fetched_plans():
    plan = {
        "mode": "optimizer",
        "predictions": {
            "lint": {"duration_s": 40.0, "failure_probability": 0.9},
            "sbom": {"duration_s": 20.0, "failure_probability": 0.1},
            "multiarch": {"duration_s": 99.0, "failure_probability": 0.5},
        },
    }
    t0 = "2026-11-09T10:00:00Z"
    jobs = [
        {
            "name": "optional (lint)",
            "conclusion": "failure",
            "started_at": t0,
            "completed_at": "2026-11-09T10:00:30Z",
        },
        {
            "name": "optional (sbom)",
            "conclusion": "success",
            "started_at": t0,
            "completed_at": "2026-11-09T10:00:30Z",
        },
        {
            "name": "optional (multiarch)",
            "conclusion": "skipped",
            "started_at": None,
            "completed_at": None,
        },
        {"name": "plan", "conclusion": "success", "started_at": t0, "completed_at": t0},
    ]
    fetched = [
        {"mode": "optimizer", "plan": plan, "jobs": jobs},
        {"mode": "baseline", "plan": None, "jobs": jobs},
    ]
    out = dm.prediction_errors(fetched)
    assert out == {
        "optimizer_runs_with_plan": 1,
        "optimizer_runs_without_plan": 0,
        "jobs": 2,
        "failures": 1,
        "duration_mae_s": 10.0,
        "failure_auc": 1.0,
    }
    assert dm.auc([1, 1], [0.2, 0.3]) is None and dm.auc([1, 0, 0], [0.5, 0.5, 0.1]) == 0.75


def test_score_without_fetch_says_predictions_not_computed(tmp_path):
    by = paired(
        tmp_path, [row("baseline", i) for i in range(N)], [row("optimizer", i) for i in range(N)]
    )
    assert dm.score(by)["predictions"] == {
        "computed": False,
        "reason": "no fetched plans (github.jsonl)",
    }


LOG = """2026-11-09T19:05:11.1000000Z ##[group]Run python3 ci/plan.py --mode "" --branch x
2026-11-09T19:05:11.2000000Z ##[endgroup]
2026-11-09T19:05:12.0000000Z {
2026-11-09T19:05:12.0000000Z   "mode": "optimizer",
2026-11-09T19:05:12.0000000Z   "model": "m",
2026-11-09T19:05:12.0000000Z   "run": ["lint"],
2026-11-09T19:05:12.0000000Z   "predictions": {"lint": {"duration_s": 41.5,
2026-11-09T19:05:12.0000000Z     "failure_probability": 0.2}}
2026-11-09T19:05:12.0000000Z }
2026-11-09T19:05:12.1000000Z ::notice title=plan::mode=optimizer
"""


def test_plan_is_read_from_the_job_log():
    plan = dm.plan_from_log(LOG)
    assert plan["predictions"]["lint"]["duration_s"] == 41.5
    assert dm.plan_from_log("2026-11-09T19:05:12.0000000Z no plan here\n") is None


class FakeGitHub:
    repo = "o/r"

    def __init__(self):
        self.paths, self.logs = [], []

    def get(self, path):
        self.paths.append(path)
        return {
            "jobs": [
                {
                    "id": 7,
                    "name": "plan",
                    "conclusion": "success",
                    "started_at": "a",
                    "completed_at": "b",
                    "steps": ["dropped"],
                }
            ]
        }

    def job_log(self, job_id):
        self.logs.append(job_id)
        return LOG


def test_fetch_saves_jobs_and_optimizer_plans_and_resumes(tmp_path):
    by = paired(
        tmp_path,
        [row("baseline", 0, "docs_only", "no run")] + [row("baseline", i) for i in range(1, N)],
        [row("optimizer", i) for i in range(N)],
    )
    gh = FakeGitHub()
    counts = dm.fetch(by, gh, tmp_path)
    assert counts == {"runs": 7, "saved": 7, "already": 0, "plans": 4, "plans_missing": 0}
    assert len(gh.logs) == 4 and all(p.startswith("/repos/o/r/actions/runs/") for p in gh.paths)
    lines = [json.loads(x) for x in (tmp_path / "github.jsonl").read_text().splitlines()]
    assert "steps" not in lines[0]["jobs"][0] and lines[-1]["plan"]["mode"] == "optimizer"
    again = dm.fetch(by, FakeGitHub(), tmp_path)
    assert again["already"] == 7 and again["saved"] == 0


def test_reader_sends_get_only_and_drops_the_token_on_the_log_redirect(monkeypatch):
    seen = []

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    def urlopen(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
        return Response(b"log text")

    import urllib.error
    import urllib.request

    class Opener:
        def open(self, req, timeout=None):
            seen.append((req.get_method(), req.full_url, req.get_header("Authorization")))
            raise urllib.error.HTTPError(
                req.full_url, 302, "Found", {"Location": "https://blob.example/log"}, None
            )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: Opener())
    gh = dm.GitHubReader("o/r", "secret")
    assert gh.job_log(7) == "log text"
    assert seen[0] == (
        "GET",
        "https://api.github.com/repos/o/r/actions/jobs/7/logs",
        "Bearer secret",
    )
    assert seen[1] == ("GET", "https://blob.example/log", None)


def test_reader_get_follows_no_redirect(monkeypatch):
    import urllib.error
    import urllib.request

    handlers, seen = [], []

    class Opener:
        def open(self, req, timeout=None):
            seen.append((req.get_method(), req.get_header("Authorization")))
            raise urllib.error.HTTPError(
                req.full_url, 301, "Moved", {"Location": "https://elsewhere.example/x"}, None
            )

    def build_opener(*h):
        handlers.extend(h)
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("urlopen used"))
    with pytest.raises(urllib.error.HTTPError):
        dm.GitHubReader("o/r", "secret").get("/repos/o/r/actions/runs/1/jobs")
    assert handlers == [dm._NoRedirect] and seen == [("GET", "Bearer secret")]


def test_cli_refuses_without_confirmation():
    with pytest.raises(SystemExit, match="Step 11 only"):
        dm.main(["score"])
