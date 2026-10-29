"""Offline levels on ci recordings (D64): L0 to L4 decide on the same failed run that the live
CI path decided at L5, from the recording's files.

    python evaluation/ci_levels.py <recording> [<recording> ...]

Each level handles the recording's ci_run_completed event (replay.events) once, with a
dry-run executor and no approver, so no level acts on anything: a proposal the gate sends to
a human stays unexecuted. Proposals and gate decisions go to the audit log under the
recording's id (the run id the live L5 rows carry) and each level, so the levels pair by
scenario. Fix proposals are not planned (no GitHub read): the levels compare root cause and
action, the live L5 run alone has the planned pin or issue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci")]

import replay  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.executor import Executor  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402
from flowguard.orchestrator import Orchestrator, level  # noqa: E402

LEVELS = ("L0", "L1", "L2", "L3", "L4")


def decide(rec: Path, audit_path: Path, diagnose, levels=LEVELS) -> dict[str, list[str]]:
    """Each level's proposed actions on the recording's failed run."""
    events = [e for e in replay.events(rec) if e.type == "ci_run_completed"]
    actions = {}
    for name in levels:
        audit = AuditLog(audit_path, rec.name, name)
        orch = Orchestrator(
            cfg=level(name),
            record=IncidentRecord(audit_path),
            executor=Executor(audit=audit, dry_run=True),
            audit=audit,
            diagnose=diagnose,
        )
        actions[name] = []
        for event in events:
            out = orch.handle(event).get("output")
            actions[name] += [p.action for p in (out.proposals if out else [])]
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
    args = parser.parse_args(argv)
    audit = AuditLog.open()
    diagnose = full_diagnose(audit)
    for rec in args.recordings:
        actions = decide(rec, audit.path, diagnose)
        print(json.dumps({"recording": rec.name, "actions": actions}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
