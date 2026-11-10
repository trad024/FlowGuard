"""D77: held-out-only copies of a laptop's truth DB and audit log. Synthetic DBs only: no real
recording, no held-out file, no network."""

import json
import sqlite3
from contextlib import closing

import pytest

import groundtruth
import heldout_inputs as hi
from flowguard.audit import AuditLog
from flowguard.incidents import IncidentRecord

IDS = {"h-0000000001", "h-0000000002", "h-normal-traffic"}
KEY = "a" * 64  # a held-out Diagnose cache key
LATE, EARLY = "2026-11-09T10:00:00.000Z", "2026-11-01T10:00:00.000Z"


def sql(path, statement, args=()):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(statement, args)


def rows(path, statement):
    with closing(sqlite3.connect(path)) as db:
        return db.execute(statement).fetchall()


def run(truth, run_id, spec_id, family, folder, *, valid=True, at=LATE):
    truth.record_run(run_id, spec={"id": spec_id, "family": family},
                     ground_truth={"secret": True}, observed={}, l0_correct=None,
                     valid=valid, recording=f"/home/x/evaluation/recordings/{folder}")  # fmt: skip
    sql(truth.path, "UPDATE runs SET recorded_at = ? WHERE run_id = ?", (at, run_id))
    truth.event(run_id, "scenario_started", ts=at)


EVENT = (
    "INSERT INTO events (ts, source, kind, scenario_id, level, payload)"
    " VALUES (?, 'test', ?, ?, 'L0', ?)"
)
PROPOSAL = (
    "INSERT INTO proposals VALUES (?, ?, ?, 'L5', ?, 'ci', 'retry_job', 't', 'ci', 0.9, 'r', '{}')"
)
DECISION = (
    "INSERT INTO gate_decisions (ts, proposal_id, scenario_id, level, tier, risk_score, outcome,"
    " reason) VALUES (?, ?, ?, 'L5', 'low', 0.1, 'execute', 'r')"
)
INCIDENT = "INSERT INTO incidents (id, opened_at, status, body) VALUES (?, ?, 'open', '{}')"
EVIDENCE = (
    "INSERT INTO evidence (incident_id, agent, claim, refs, posted_at)"
    " VALUES (?, 'ci', 'c', '[]', ?)"
)


def event(path, ts, kind, scenario_id, payload=None):
    sql(path, EVENT, (ts, kind, scenario_id, json.dumps(payload or {})))


def proposal(path, pid, ts, scenario_id, incident):
    sql(path, PROPOSAL, (pid, ts, scenario_id, incident))
    sql(path, DECISION, (ts, pid, scenario_id))


@pytest.fixture
def laptop(tmp_path):
    truth = groundtruth.GroundTruthDB(tmp_path / "groundtruth.db")
    run(truth, "r-held1", "h-0000000001", "runtime", "r-held1")
    run(truth, "r-held2", "h-0000000002", "release", "r-held2")
    run(truth, "r-rerun", "h-0000000002", "release", "r-rerun", valid=False)  # re-run later
    run(truth, "r-normal", "h-normal-traffic", "normal", "r-normal")
    run(truth, "r-dev", "release-01", "release", "r-dev", at=EARLY)  # a dev spec
    run(truth, "r-odd", "h-0000000001", "runtime", "r-odd", valid=False, at=EARLY)  # pre-Step 11
    audit = tmp_path / "audit.db"
    AuditLog(audit)
    incidents = IncidentRecord(audit)
    event(audit, LATE, "security_decision", "r-held1")
    event(audit, LATE, "deploy_started", "r-rerun")
    event(audit, LATE, "llm_call", None, {"cache_key": KEY})
    event(audit, LATE, "llm_call", None, {"cache_key": "b" * 64})  # not a held-out call
    event(audit, EARLY, "deploy_started", "r-dev")
    event(audit, EARLY, "llm_call", None, {"cache_key": "c" * 64})
    for iid in ("inc-held", "inc-dev"):
        sql(audit, INCIDENT, (iid, LATE))
        sql(audit, EVIDENCE, (iid, LATE))
    proposal(audit, "p-held", LATE, "r-held1", "inc-held")
    proposal(audit, "p-dev", EARLY, None, "inc-dev")  # the null-scenario dev proposals
    del incidents
    listed = [str(tmp_path / "rec" / n) for n in ("r-held1", "r-held2", "r-normal")]
    return {"truth": truth.path, "audit": audit, "listed": listed, "tmp": tmp_path}


def test_keeps_only_held_out_rows_and_invalid_held_out_runs(laptop):
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    assert found["problems"] == []
    assert found["keep"]["runs"] == {"r-held1", "r-held2", "r-rerun", "r-normal"}
    c = found["counts"]
    assert c["truth_runs"] == [6, 4] and c["truth_events"] == [6, 4]
    assert c["events"] == [6, 3]  # security_decision, the re-run's deploy, the held-out llm_call
    assert c["proposals"] == [2, 1] and c["gate_decisions"] == [2, 1]
    assert c["incidents"] == [2, 1] and c["evidence"] == [2, 1]
    assert sorted(found["families"].values()) == ["normal", "release", "runtime"]


def test_write_prunes_copies_and_leaves_the_originals(laptop):
    before = {p: p.read_bytes() for p in (laptop["truth"], laptop["audit"])}
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    out = laptop["tmp"] / "out"
    hi.write(laptop["truth"], laptop["audit"], out, found)
    assert {p: p.read_bytes() for p in before} == before
    assert sorted(r[0] for r in rows(out / "groundtruth.db", "SELECT run_id FROM runs")) == [
        "r-held1", "r-held2", "r-normal", "r-rerun"]  # fmt: skip
    assert rows(out / "groundtruth.db", "SELECT COUNT(*) FROM events") == [(4,)]
    assert rows(out / "audit.db", "SELECT id FROM proposals") == [("p-held",)]
    assert rows(out / "audit.db", "SELECT MIN(ts) FROM events")[0][0] >= hi.CUT
    assert rows(out / "audit.db", "SELECT id FROM incidents") == [("inc-held",)]
    assert (out / "recordings-release.txt").read_text().split() == [laptop["listed"][1]]
    with pytest.raises(hi.Refused, match="empty folder"):
        hi.write(laptop["truth"], laptop["audit"], out, found)


def test_ground_truth_survives_unread_in_kept_rows(laptop):
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    out = laptop["tmp"] / "out"
    hi.write(laptop["truth"], laptop["audit"], out, found)
    assert rows(out / "groundtruth.db", "SELECT DISTINCT ground_truth FROM runs") == [
        ('{"secret": true}',)]  # fmt: skip


@pytest.mark.parametrize(
    "listed, reason",
    [
        (["r-held1", "r-held2"], "not listed"),  # a valid held-out run left out
        (["r-held1", "r-held2", "r-normal", "r-rerun"], "no single kept valid run"),  # invalid
        (["r-held1", "r-held2", "r-normal", "r-dev"], "no single kept valid run"),  # dev
        (["r-held1", "r-held1", "r-held2", "r-normal"], "listed twice"),
    ],
)
def test_refuses_a_list_that_does_not_match_the_held_out_runs(laptop, listed, reason):
    paths = [str(laptop["tmp"] / "rec" / n) for n in listed]
    found = hi.plan(laptop["truth"], laptop["audit"], paths, IDS, {KEY})
    assert any(reason in p for p in found["problems"])


def test_refuses_a_kept_audit_row_dated_before_the_cut(laptop):
    event(laptop["audit"], EARLY, "security_decision", "r-held1")
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    assert any("dated before" in p for p in found["problems"])


APPROVAL = (
    "INSERT INTO approvals (ts, proposal_id, scenario_id, level, verdict, approver,"
    " wait_seconds, channel) VALUES (?, ?, ?, 'L5', 'approve', 'lead', 12.0, 'page')"
)


def test_approvals_are_kept_by_their_kept_proposal_even_without_a_scenario_id(laptop):
    sql(laptop["audit"], APPROVAL, (LATE, "p-held", None))  # the approval page's own process
    sql(laptop["audit"], APPROVAL, (EARLY, "p-dev", None))  # a dev approval
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    assert found["problems"] == []
    assert found["counts"]["approvals"] == [2, 1]
    out = laptop["tmp"] / "out"
    hi.write(laptop["truth"], laptop["audit"], out, found)
    assert rows(out / "audit.db", "SELECT proposal_id FROM approvals") == [("p-held",)]


def test_refuses_a_row_linking_a_kept_proposal_under_another_scenario(laptop):
    sql(laptop["audit"], APPROVAL, (LATE, "p-held", "r-dev"))
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    assert any("another scenario" in p for p in found["problems"])


def test_refuses_undated_held_out_runs_and_undated_kept_audit_rows(laptop):
    # recorded_at is NOT NULL; "None" (a stringified NULL) used to sort after the cut
    sql(laptop["truth"], "UPDATE runs SET recorded_at = 'None' WHERE run_id = 'r-held2'")
    sql(laptop["audit"], "UPDATE events SET ts = 'yesterday' WHERE scenario_id = 'r-held1'")
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    assert "a held-out run has no ISO recorded_at" in found["problems"]
    assert "a kept audit row has no ISO date" in found["problems"]
    assert "r-held2" not in found["keep"]["runs"]


def test_dropped_rows_are_counted_not_dropped_silently(laptop):
    event(laptop["audit"], LATE, "llm_call_failed", None, {"cache_key": KEY})
    event(laptop["audit"], "unknown", "deploy_started", "r-dev")  # ts is NOT NULL, not ISO
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"], IDS, {KEY})
    c = found["counts"]
    assert c["heldout_runs_before_cut_dropped"] == 1  # r-odd
    assert c["events_dropped_on_or_after_cut_by_kind"] == {"llm_call": 1, "llm_call_failed": 1}
    assert c["audit_rows_dropped_on_or_after_cut"] == {"events": 2}
    assert c["audit_rows_dropped_undated"] == {"events": 1}
    assert found["problems"] == []


def test_cli_refuses_without_confirmation_and_problems_name_no_id(laptop, monkeypatch):
    with pytest.raises(SystemExit, match="Step 11 only"):
        hi.main(["--truth", str(laptop["truth"]), "--audit", str(laptop["audit"]),
                 "--list", str(laptop["tmp"] / "x.txt"), "--check"])  # fmt: skip
    found = hi.plan(laptop["truth"], laptop["audit"], laptop["listed"][:1], IDS, {KEY})
    assert found["problems"] and not any("h-" in p or "r-" in p for p in found["problems"])


def test_diagnose_cache_keys_and_ids(tmp_path):
    (tmp_path / "L2-raw.jsonl").write_text(json.dumps({"cache_key": KEY}) + "\n\n")
    (tmp_path / "L2.jsonl").write_text(json.dumps({"cache_key": "b" * 64}) + "\n")
    assert hi.diagnose_cache_keys(tmp_path) == {KEY}
    order = tmp_path / "run-order.txt"
    order.write_text("h-0000000001\nh-0000000002\n\n")
    assert hi.heldout_ids(order) == IDS
