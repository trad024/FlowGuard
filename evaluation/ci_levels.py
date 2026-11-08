"""Offline levels on ci recordings (D64): L0 to L4 decide on the same failed run that the live
CI path decided at L5, from the recording's files.

    python evaluation/ci_levels.py <recording> [<recording> ...] [--confirm-step-11]
        [--records FILE]

A held-out recording (test-split template, held-out id or folder) runs only with
--confirm-step-11 and the staged freeze (heldout_guard), as every held-out Diagnose run.

Each level handles the recording's ci_run_completed event (replay.events) once, with a
dry-run executor and no approver, so no level acts on anything: a proposal the gate sends to
a human stays unexecuted. Proposals and gate decisions go to the audit log under the
recording's id (the run id the live L5 rows carry) and each level, so the levels pair by
scenario. Fix proposals are not planned (no GitHub read): the levels compare root cause and
action, the live L5 run alone has the planned pin or issue.

`--records` (R7) writes one replay record per recording and level in the format report_data.load
reads: scenario_id (the recording, the truth run id), level, method (the level's Diagnose
method), proposal_ids (the level's Diagnose proposal, for the audit join) and observed:
root_cause, evidence_lines (as the proposal's rationale lists them, at most 5),
selected_action, and at the LLM levels the hallucination counts (observations.hallucination).
merge_heldout.py takes the file as a scenario replay source.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci"), str(ROOT / "evaluation")]

import heldout_guard  # noqa: E402
import observations  # noqa: E402
import replay  # noqa: E402
from flowguard import calibrators  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.executor import Executor  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402
from flowguard.orchestrator import Orchestrator, level  # noqa: E402
from report_metrics import LEVEL_METHODS, METHODS  # noqa: E402

LEVELS = ("L0", "L1", "L2", "L3", "L4")


def record(rec: Path, name: str, p) -> dict:
    """The replay record of one level's Diagnose proposal (R7)."""
    params = dict(p.params)
    cause = params.get("diagnose_root_cause")
    if cause is None and (m := re.search(r"root cause (\w+)", p.rationale or "")):
        cause = m.group(1)
    lines = re.search(r"evidence lines \[([\d, ]*)\]", p.rationale or "")
    evidence = [int(x) for x in lines.group(1).split(",") if x.strip()] if lines else []
    observed = {"selected_action": p.action, "evidence_lines": [n for n in evidence if n >= 1]}
    if cause is not None:
        observed["root_cause"] = str(cause)
    observed.update(observations.hallucination(params))
    method = params.get("diagnose_method")
    return {"scenario_id": rec.name, "level": name,
            "method": method if method in METHODS else LEVEL_METHODS[name],
            "proposal_ids": [p.id], "observed": observed}  # fmt: skip


def decide(rec: Path, audit_path: Path, diagnose, levels=LEVELS, records=None):
    """Each level's proposed actions on the recording's failed run; with `records` (a list),
    each level's replay record is appended to it."""
    events = [e for e in replay.events(rec) if e.type == "ci_run_completed"]
    actions = {}
    for name in levels:
        audit = AuditLog(audit_path, rec.name, name)
        orch = Orchestrator(
            cfg=level(name),
            record=IncidentRecord(audit_path),
            executor=Executor(audit=audit, dry_run=True),
            audit=audit,
            calibrators=calibrators.for_level(name),  # D67
            diagnose=diagnose,
        )
        actions[name] = []
        for event in events:
            out = orch.handle(event).get("output")
            actions[name] += [p.action for p in (out.proposals if out else [])]
            if records is not None and out:
                records += [record(rec, name, p) for p in out.proposals if p.agent == "diagnose"]
    return actions


def full_diagnose(audit: AuditLog):
    """Diagnose with every level's method: TF-IDF + LR, the pinned LLM, the retrieval index."""
    from flowguard import diagnose, llm
    from flowguard.classical import TfidfDiagnoser
    from flowguard.retrieval import Retriever
    from flowguard.stubs import Diagnose

    client = llm.from_env(audit=audit)
    return Diagnose(
        llm=diagnose.answerer(client.complete), l1=TfidfDiagnoser.load(), retriever=Retriever()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("recordings", nargs="+", type=Path)
    parser.add_argument(heldout_guard.FLAG, action="store_true", help="Step 11 only")
    parser.add_argument("--records", type=Path, help="replay records (JSONL) for the report")
    args = parser.parse_args(argv)
    held = [r.name for r in args.recordings if heldout_guard.is_heldout_recording(r)]
    if held:  # before any LLM client exists
        heldout_guard.require_step_11(args.confirm_step_11, f"ci_levels on {', '.join(held)}")
    audit = AuditLog.open()
    diagnose = full_diagnose(audit)
    rows: list[dict] = []
    for rec in args.recordings:
        actions = decide(rec, audit.path, diagnose, records=rows)
        print(json.dumps({"recording": rec.name, "actions": actions}), flush=True)
    if args.records:
        args.records.parent.mkdir(parents=True, exist_ok=True)
        args.records.write_text("".join(json.dumps(r) + chr(10) for r in rows), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
