"""Offline L0 to L4 on a ci recording (D64): the same failed run the live path decides at L5,
decided at each level with a dry-run executor and no approver. Synthetic recording only."""

import json

import ci_levels
from flowguard.agent_io import AgentOutput
from flowguard.audit import AuditLog
from flowguard.stubs import proposal

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
