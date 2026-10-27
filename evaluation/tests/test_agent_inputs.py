"""Agent-input leakage test (D25): nothing an agent receives names the class, gives the
experiment away, or carries a ground-truth value. Inputs come from the offline replay of
recordings through the real orchestrator, captured by its input tap."""

import json
from pathlib import Path

import pytest

import leakage
import replay
from flowguard.audit import AuditLog
from flowguard.executor import Executor
from flowguard.incidents import IncidentRecord
from flowguard.orchestrator import Orchestrator, level

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS = ROOT / "evaluation" / "recordings"
BASELINES = sorted((ROOT / "evaluation" / "runs" / "ci-baseline").glob("*"))
PODINFO_CI = "app/podinfo"


def agent_inputs(rec: Path, tmp_path: Path) -> list:
    db = tmp_path / "state.db"
    seen = []
    audit = AuditLog(db)
    orch = Orchestrator(
        cfg=level("L4"),
        record=IncidentRecord(db),
        executor=Executor(audit=audit),
        audit=audit,
        input_tap=lambda agent, inp: seen.append(inp),
    )
    for event in replay.events(rec):
        orch.handle(event)
    return seen


def truth_values(rec: Path) -> set[str]:
    """Ground-truth strings to look for (scoring side only reads this file)."""
    gt = json.loads((rec / "ground_truth.json").read_text(encoding="utf-8"))
    spec, truth = gt.get("spec") or {}, gt.get("ground_truth") or {}
    values = {spec.get("id"), truth.get("template_id"), "PODINFO_FAULT"}
    kind = (spec.get("fault") or {}).get("kind") or ""
    if "_" in kind:  # http_errors, memory_leak, ...: words a real log would not use
        values.add(kind)
    return {v.lower() for v in values if v}


def problems(inputs: list, truth: set[str]) -> list[str]:
    baseline = "\n".join(p.read_text(encoding="utf-8") for b in BASELINES for p in b.glob("*.txt"))
    found = []
    for inp in inputs:
        found += leakage.scan_log(inp.log, baseline)
        fields = [
            *inp.metrics,
            *inp.alerts,
            *inp.changed_files,
            *map(str, inp.params.values()),
            *inp.params,
            *inp.scan_counts,
            *(e.claim for e in inp.evidence),
        ]
        for text in fields:
            if hits := leakage.hits(leakage.masked(str(text))):
                found.append(f"{text!r}: {sorted(hits)}")
        blob = json.dumps([inp.log, fields], default=str).lower()
        found += [f"ground-truth value {v!r} in an agent input" for v in truth if v in blob]
    return found


def write_recording(folder: Path, log: str, spec_id: str = "ci-code") -> Path:
    run = folder / "ci" / "1"
    (run / "logs").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"sources": {"ci": [{"run_id": 1}]}}))
    (run / "jobs.json").write_text(json.dumps([{"id": 5, "name": "unit", "conclusion": "failure"}]))
    (run / "logs" / "unit.txt").write_text(log, encoding="utf-8")
    (folder / "ground_truth.json").write_text(
        json.dumps(
            {
                "spec": {"id": spec_id, "fault": {"kind": "code"}},
                "ground_truth": {"template_id": "code-06"},
            }
        )
    )
    return folder


def test_the_check_finds_planted_leaks_and_passes_clean_inputs(tmp_path):
    clean = write_recording(tmp_path / "clean", "--- FAIL: TestEchoHandler (0.00s)\n")
    assert problems(agent_inputs(clean, tmp_path / "a"), truth_values(clean)) == []
    leaky = write_recording(tmp_path / "leaky", "--- FAIL: TestFlakyCache (0.00s)\n")
    assert problems(agent_inputs(leaky, tmp_path / "b"), truth_values(leaky))
    named = write_recording(tmp_path / "named", "see template code-06 for details\n")
    assert any(
        "code-06" in p for p in problems(agent_inputs(named, tmp_path / "c"), truth_values(named))
    )


def recordings():
    found = sorted(p for p in RECORDINGS.glob("*") if (p / "ground_truth.json").is_file())
    return found or [pytest.param(None, marks=pytest.mark.skip(reason="no local recordings"))]


@pytest.mark.parametrize("rec", recordings(), ids=lambda p: p.name if p else "none")
def test_recorded_inputs_do_not_leak(rec, tmp_path):
    inputs = agent_inputs(rec, tmp_path)
    assert inputs  # every recording gives an agent something to read
    assert problems(inputs, truth_values(rec)) == []
