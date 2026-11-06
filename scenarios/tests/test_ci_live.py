"""The live CI path (D64) with fakes only: no GitHub, no LLM, no git remote, no cluster."""

import base64
import json
import urllib.parse

import pytest

import ci_live
import leakage
import replay
import runner
from flowguard.agent_io import AgentOutput
from flowguard.monitor import ci_event
from flowguard.stubs import proposal

REPO = "acme/app"
HEAD = "a" * 40
GREEN = "b" * 40
BRANCH = "scenario/r-0000abcd"
UNKNOWN = "go: github.com/gorilla/websocket@v1.9.9: invalid version: unknown revision v1.9.9"
LOG = f"go build ./...\r\n{UNKNOWN}\r\nexit status 1\r\n"
GO_MOD = "module example.com/app\n\nrequire github.com/gorilla/websocket v1.5.3\n"
GO_SUM = (
    "github.com/gorilla/websocket v1.5.3 h1:SYNTHETIC1=\n"
    "github.com/gorilla/websocket v1.5.3/go.mod h1:SYNTHETIC2=\n"
)
RUN = {
    "id": 41, "status": "completed", "conclusion": "failure", "head_sha": HEAD,
    "head_branch": BRANCH, "display_title": "a commit message",
    "run_started_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:05:00Z",
}  # fmt: skip
JOBS = [
    {"id": 5, "name": "lint", "status": "completed", "conclusion": "success",
     "completed_at": "2026-01-01T00:02:00Z"},
    {"id": 6, "name": "unit", "status": "completed", "conclusion": "failure",
     "completed_at": "2026-01-01T00:04:00Z"},
]  # fmt: skip


class Reader:
    """The recorder's GitHub reader, answering one run."""

    repo = REPO

    def __init__(self, run=RUN, jobs=JOBS):
        self.run, self.jobs = run, jobs

    def get(self, path):
        if path.endswith("/runs/41"):
            return self.run
        if path.endswith("/runs/41/jobs?per_page=100"):
            return {"jobs": self.jobs}
        return []  # annotations

    def job_log(self, job_id):
        return LOG, None


def test_the_event_is_the_replays_on_the_recorded_run(tmp_path):
    event = ci_live.failed_run_event(Reader(), 41, tmp_path / "r-0000abcd")
    again = replay.ci_events(tmp_path / "r-0000abcd", {"sources": {"ci": [{"run_id": 41}]}})[0]
    assert event == again  # the same code on the same files
    params = event.payload["params"]
    assert (params["run_id"], params["job_id"]) == (41, 6)
    assert (params["head_sha"], params["head_branch"]) == (HEAD, BRANCH)
    # the recorder keeps the log's bytes on every platform (no newline translation on Windows),
    # and a recording reads it back with universal newlines
    saved = tmp_path / "r-0000abcd" / "ci" / "41" / "logs" / "unit.txt"
    assert saved.read_bytes() == LOG.encode("utf-8")
    assert event.payload["log"] == LOG.replace("\r\n", "\n")
    assert "display_title" not in params["ci_run"]["run"]  # never a commit message
    passed = [{**j, "conclusion": "success"} for j in JOBS]
    assert ci_live.failed_run_event(Reader(jobs=passed), 41, tmp_path / "ok") is None


class Diagnose:
    """L5's Diagnose, faked: a dependency answer that quotes the go error."""

    seen: list = []

    def missing(self, cfg):
        return None

    def run(self, inp, cfg):
        Diagnose.seen.append(inp)
        p = proposal("diagnose", "pin_dependency", inp, "root cause dependency; evidence lines [2]",
                     diagnose_method="llm_retrieval", diagnose_quotes=[UNKNOWN])  # fmt: skip
        return AgentOutput(proposals=[p], label_scores={p.id: {"dependency": 0.9}})


def reads(calls):
    """A read-only GitHub client: the last green run on dev, go.mod there, go.sum at HEAD."""

    def call(method, path, body=None):
        calls.append((method, path))
        url = urllib.parse.urlsplit(path)
        query = dict(urllib.parse.parse_qsl(url.query))
        if method != "GET":
            return 403, {"message": "read-only"}
        if url.path.endswith("/runs"):
            green = query["branch"] == "dev"
            return 200, {"workflow_runs": [{"head_sha": GREEN}] if green else []}
        text = {GREEN: GO_MOD, HEAD: GO_SUM}[query["ref"]]
        return 200, {"type": "file", "content": base64.b64encode(text.encode()).decode(),
                     "sha": "blob"}  # fmt: skip

    return call


class Approver:
    def __init__(self, verdict="approved"):
        self.verdict, self.asked = verdict, []

    def review(self, p, d):
        self.asked.append(p.action)
        return d.model_copy(update={"human_verdict": self.verdict, "approved_by": "team member"})


def event():
    return ci_event(RUN, JOBS, 1767225840.0, log=LOG)


def test_l5_decides_once_plans_the_pin_asks_a_human_and_a_dry_run_writes_nothing(tmp_path):
    calls, approver = [], Approver()
    Diagnose.seen.clear()
    out = ci_live.decide(event(), tmp_path / "audit.db", "r-0000abcd", diagnose=Diagnose(),
                         read_client=reads(calls), repo=REPO, approver=approver)  # fmt: skip
    assert (out["level"], out["executor"]) == ("L5", "dry run")
    assert [p["action"] for p in out["proposals"]] == ["pin_dependency"]
    # no Diagnose calibrator yet: confidence 0, so a medium-tier pin goes to a human (D17)
    assert out["gate"][0]["outcome"] == "ask_human" and approver.asked == ["pin_dependency"]
    assert out["executed"] == [
        {
            "proposal": out["proposals"][0]["id"],
            "success": True,
            "output": "dry run: pin_dependency",
        }
    ]
    assert {m for m, _ in calls} == {"GET"}  # planning reads; the dry run writes nothing
    # Diagnose saw the run's head commit and branch, not the raw run, and nothing that leaks
    (inp,) = Diagnose.seen
    assert inp.params["head_sha"] == HEAD and "ci_run" not in inp.params
    for value in [*inp.params, *map(str, inp.params.values())]:
        assert not leakage.hits(leakage.masked(value)), value
    # every row at L5 under the run id: what scoring pairs with the offline levels
    db = runner.AuditLog(tmp_path / "audit.db")
    for table in ("proposals", "gate_decisions", "approvals", "executions"):
        rows = db.query(f"SELECT scenario_id, level FROM {table}")
        assert rows == [{"scenario_id": "r-0000abcd", "level": "L5"}], table
    params = json.loads(db.query("SELECT params FROM proposals")[0]["params"])
    assert (params["module"], params["version"], params["base_branch"]) == (
        "github.com/gorilla/websocket", "v1.5.3", BRANCH
    )  # fmt: skip


def test_live_mode_hands_an_approved_fix_to_the_live_executor_and_a_denial_stops_it(tmp_path):
    out = ci_live.decide(event(), tmp_path / "a.db", "r-0000abcd", diagnose=Diagnose(),
                         read_client=reads([]), repo=REPO, approver=Approver(), live=True,
                         token="")  # fmt: skip
    # the live executor ran it; it fails here (no token, and pin_dependency needs the
    # executor's D57 wiring), and the failure is what the audit log records
    assert out["executor"] == "live" and out["executed"][0]["success"] is False
    denied = ci_live.decide(event(), tmp_path / "b.db", "r-0000abcd", diagnose=Diagnose(),
                            read_client=reads([]), repo=REPO, approver=Approver("denied"),
                            live=True, token="")  # fmt: skip
    assert denied["gate"][0]["verdict"] == "denied" and denied["executed"] == []


# ---------------------------------------------------------------- the runner


@pytest.fixture
def stores(monkeypatch, tmp_path):
    monkeypatch.setenv("FLOWGUARD_AUDIT_DB", str(tmp_path / "audit.db"))
    monkeypatch.setenv("FLOWGUARD_GROUNDTRUTH_DB", str(tmp_path / "groundtruth.db"))
    monkeypatch.setattr(runner, "RUNS", tmp_path)
    return tmp_path


def ci_spec():
    return {
        "id": "ci-dependency-01", "family": "ci", "description": "x",
        "fault": {"kind": "dependency", "delivery": "commit"},
        "ground_truth": {"root_cause": "dependency", "expected_l0": "ci_failure"},
    }  # fmt: skip


def test_the_runner_takes_the_path_only_for_the_ci_family_and_refuses_without_l5(stores):
    with pytest.raises(ValueError, match="ci family"):
        runner.Runner({**ci_spec(), "family": "release"}, ci_agents="dry-run")
    with pytest.raises(ValueError, match="ci_agents"):
        runner.Runner(ci_spec(), ci_agents="maybe")
    r = runner.Runner(ci_spec(), run_id="r-0000abcd", ci_agents="dry-run")

    class NoIndex(Diagnose):
        def missing(self, cfg):
            return f"{cfg.level}: Diagnose needs the retrieval index (state/chroma/)"

    r.ci_diagnose = NoIndex()
    with pytest.raises(RuntimeError, match="refusing to start: L5: Diagnose needs"):
        r.prepare_ci_agents()


def test_the_runner_decides_on_the_failed_run_and_records_it(stores, monkeypatch):
    r = runner.Runner(ci_spec(), run_id="r-0000abcd", ci_agents="dry-run")
    r.ci_diagnose = Diagnose()
    monkeypatch.setattr(runner.recorder, "GitHubReader", lambda repo, token: Reader())
    monkeypatch.setattr(runner.github_actions, "urllib_transport", lambda token: reads([]))
    cfg = type("C", (), {"repo": REPO, "token": "read-token"})
    approver = Approver()
    out = r.decide_ci(cfg, RUN, approver=approver)
    assert out == r.observed["agents"] and out["executed"][0]["success"] is True
    assert [e["kind"] for e in r.truth_db.events("r-0000abcd")] == ["agents_decided"]
    rows = r.audit.query("SELECT level, action FROM proposals WHERE scenario_id = 'r-0000abcd'")
    assert rows == [{"level": "L5", "action": "pin_dependency"}]
    # a run whose failed job left no log is skipped, and nothing is asked
    monkeypatch.setattr(runner.ci_live, "failed_run_event", lambda *a: None)
    assert "skipped" in r.decide_ci(cfg, RUN, approver=Approver())
