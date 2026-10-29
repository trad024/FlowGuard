"""Live CI path (D64): the agents decide once, at L5, on a ci scenario's failed app-ci run.

The runner calls this when a ci scenario's run fails (`runner.py --ci-agents dry-run|live`):

1. The failed run is recorded into a scratch folder by the recorder (`recorder.record_ci`) and
   its ci_run_completed event is built from that folder by the replay (`replay.ci_events`):
   the same code, on the same files, as the offline levels.
2. The orchestrator handles the event once at L5, at the default gate setting: the Monitor
   opens the incident, Diagnose (LLM with retrieval, D8 grounding) answers, its fix proposal
   is planned read-only (`github_actions.PlannedDiagnose`: Y from the last green commit), and
   the gate decides. A proposal the gate sends to a human waits on the approval page: a team
   member approves in live runs, no simulated approver (D17).
3. The executor is live, or a dry run that writes nothing to GitHub. The planning reads
   GitHub in both modes.

The runner then checks each executed pin's app-ci run on its fix branch (`record_fix_ci`).
L0 to L4 decide offline on the recording of the same run (`evaluation/ci_levels.py`), with no
live action.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "ci"), str(ROOT / "evaluation")]

import recorder  # noqa: E402
import replay  # noqa: E402
from flowguard import diagnose, github_actions, llm  # noqa: E402
from flowguard.audit import AuditLog  # noqa: E402
from flowguard.contracts import Event  # noqa: E402
from flowguard.executor import Executor  # noqa: E402
from flowguard.incidents import IncidentRecord  # noqa: E402
from flowguard.orchestrator import Orchestrator, level  # noqa: E402
from flowguard.stubs import Diagnose  # noqa: E402

LEVEL = "L5"  # at the default gate setting (THRESHOLD)
MODES = ("off", "dry-run", "live")


def failed_run_event(reader, run_id: int, folder: Path) -> Event | None:
    """The ci_run_completed event of a failed run, exactly as the replay builds it from a
    recording; None if the run could not be read or no job failed."""
    recorder.record_ci(folder, [run_id], reader)
    manifest = {"sources": {"ci": [{"run_id": run_id}]}}
    return next(
        (
            e
            for e in replay.ci_events(folder, manifest)
            if e.type == "ci_run_completed" and e.payload.get("log")
        ),
        None,
    )


def l5_diagnose(env: dict, audit: AuditLog) -> Diagnose:
    """Diagnose with L5's method: the pinned LLM (D27) with the retrieval index (D8)."""
    from flowguard.retrieval import Retriever

    client = llm.from_env(env, audit=audit)
    return Diagnose(llm=diagnose.answerer(client.complete), retriever=Retriever())


def decide(
    event: Event,
    audit_path: Path,
    run_id: str,
    *,
    diagnose,
    read_client,
    repo: str,
    approver,
    live: bool = False,
    token: str | None = None,
    github=None,  # tests: the executor's GitHub call
) -> dict:
    """One L5 decision on the event; what was proposed, decided and executed."""
    audit = AuditLog(audit_path, run_id, LEVEL)
    executor = Executor(audit=audit, dry_run=not live, repo=repo, token=token, github=github)
    orch = Orchestrator(
        cfg=level(LEVEL),
        record=IncidentRecord(audit_path),
        executor=executor,
        audit=audit,
        approver=approver,
        diagnose=github_actions.PlannedDiagnose(diagnose, read_client, repo),
    )
    state = orch.handle(event)
    out = state.get("output")
    return {
        "level": LEVEL,
        "executor": "dry run" if executor.dry_run else "live",
        "incident": state.get("incident_id"),
        "proposals": [
            {"id": p.id, "action": p.action, "agent": str(p.agent)}
            for p in (out.proposals if out else [])
        ],
        "gate": [
            {"proposal": d.proposal_id, "outcome": d.outcome, "verdict": d.human_verdict}
            for d in state.get("decisions", [])
        ],
        "executed": [
            {"proposal": r.proposal_id, "success": r.success, "output": r.output}
            for r in state.get("results", [])
        ],
    }
