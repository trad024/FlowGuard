"""Offline L0 to L4 on a ci recording (D64): the same failed run the live path decides at L5,
decided at each level with a dry-run executor and no approver. Synthetic recording only."""

import json

import pytest

import ci_levels
from flowguard.agent_io import AgentOutput
from flowguard.audit import AuditLog
from flowguard.stubs import proposal


@pytest.fixture(autouse=True)
def calibrated(fitted_levels):
    """D67: the entry point gets fitted, never-confident calibrators from the loader."""
    return fitted_levels


LOG = "go: github.com/gorilla/websocket@v1.9.9: invalid version: unknown revision v1.9.9\n"


class Diagnose:
    """Every level's method, faked: the action depends on the level's method."""

    def missing(self, cfg):
        return None

    def run(self, inp, cfg):
        action = "pin_dependency" if cfg.llm else "retry_job"
        p = proposal("diagnose", action, inp, "root cause dependency; evidence lines [1]")
        return AgentOutput(proposals=[p], label_scores={p.id: {"dependency": 0.9}})


def recording(folder):
    run = folder / "ci" / "41"
    (run / "logs").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"sources": {"ci": [{"run_id": 41}]}}))
    (run / "run.json").write_text(json.dumps(
        {"id": 41, "status": "completed", "conclusion": "failure", "head_sha": "a" * 40,
         "head_branch": "scenario/r-0000abcd"}
    ))  # fmt: skip
    (run / "jobs.json").write_text(json.dumps(
        [{"id": 6, "name": "unit", "conclusion": "failure", "completed_at": "2026-01-01T00:04:00Z"}]
    ))  # fmt: skip
    (run / "logs" / "unit.txt").write_text(LOG, encoding="utf-8")
    return folder


def test_each_offline_level_decides_once_on_the_same_run_and_nothing_acts(tmp_path):
    rec = recording(tmp_path / "r-0000abcd")
    actions = ci_levels.decide(rec, tmp_path / "audit.db", Diagnose())
    assert actions == {
        "L0": ["retry_job"], "L1": ["retry_job"], "L2": ["pin_dependency"],
        "L3": ["pin_dependency"], "L4": ["pin_dependency"],
    }  # fmt: skip
    audit = AuditLog(tmp_path / "audit.db")
    rows = audit.query("SELECT scenario_id, level FROM proposals ORDER BY level")
    assert rows == [{"scenario_id": "r-0000abcd", "level": f"L{i}"} for i in range(5)]
    # unplanned offline: no GitHub read, so the pin carries no module or version
    params = json.loads(audit.query("SELECT params FROM proposals WHERE level='L3'")[0]["params"])
    assert "module" not in params and params["head_branch"] == "scenario/r-0000abcd"
    # L0 has no gate and executes in a dry run; from L1 a medium pin waits for a human that
    # never comes offline (no approver)
    outputs = audit.query("SELECT level, output FROM executions ORDER BY level")
    assert all(r["output"].startswith("dry run: ") for r in outputs)
    assert not any(r["level"] in ("L2", "L3", "L4") for r in outputs)
    assert audit.query("SELECT count(*) AS n FROM approvals")[0]["n"] == 0


# ---------------------------------------------------------------- R7: replay records


def test_each_level_writes_a_replay_record_the_report_reads(tmp_path):
    import groundtruth
    import merge_heldout
    import report
    import report_data
    import report_scope

    rec = recording(tmp_path / "r-0000abcd")
    audit = tmp_path / "audit.db"
    rows = []
    ci_levels.decide(rec, audit, Diagnose(), records=rows)
    assert [r["level"] for r in rows] == ["L0", "L1", "L2", "L3", "L4"]
    assert {r["method"] for r in rows} == {"regex", "tfidf_lr", "llm_zero_shot", "llm_retrieval"}
    l3 = next(r for r in rows if r["level"] == "L3")
    assert l3["observed"] == {"selected_action": "pin_dependency", "evidence_lines": [1],
                              "root_cause": "dependency"}  # fmt: skip
    records = tmp_path / "ci-records.jsonl"
    records.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    truth = tmp_path / "truth.db"
    groundtruth.GroundTruthDB(truth).record_run(
        "r-0000abcd", spec={"id": "h-0000000099", "family": "ci"},
        ground_truth={"root_cause": "dependency", "expected_l0": "ci_failure"}, observed={},
        l0_correct=None)  # fmt: skip
    joined, summary = report_data.load(audit, truth, records)
    assert summary["joined_records"] == 5
    assert all(r["audit"]["proposals"] == 1 for r in joined)  # each joined to its own proposal
    out = tmp_path / "merged"
    source = merge_heldout.parse_source(f"scenarios:audit={audit},truth={truth},replays={records}")
    assert merge_heldout.merge([source], out)["replay_records"] == 5
    report.main(["--audit", str(out / "audit.db"), "--truth", str(out / "groundtruth.db"),
                 "--replays", str(out / "replay.jsonl"), "--out", str(tmp_path / "report"),
                 "--no-plots"])  # fmt: skip
    results = json.loads((tmp_path / "report" / "results.json").read_text())
    assert results["metadata"]["confirmatory"] is False
    reason = report_scope.confirmatory_scope({"status": "frozen"}, out / "groundtruth.db")
    assert isinstance(reason, str) and reason


def test_the_cli_writes_the_records_file(tmp_path, monkeypatch):
    rec = recording(tmp_path / "r-0000abcd")
    monkeypatch.setattr(ci_levels, "full_diagnose", lambda audit: Diagnose())
    monkeypatch.setattr(ci_levels.AuditLog, "open", classmethod(
        lambda cls, **kw: AuditLog(tmp_path / "a.db")))  # fmt: skip
    out = tmp_path / "records.jsonl"
    assert ci_levels.main([str(rec), "--records", str(out)]) == 0
    assert len(out.read_text().splitlines()) == 5
