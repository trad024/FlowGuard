"""Explicit synthetic fixtures for toolkit checks; never a measured experiment."""

from pathlib import Path

from flowguard.audit import AuditLog
from flowguard.catalog import CATALOG
from groundtruth import GroundTruthDB

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")


def create(folder: Path):
    marker = folder / "SYNTHETIC.txt"
    if folder.exists():
        if marker.is_file() and marker.read_text() == "FlowGuard synthetic fixtures v1\n":
            return folder / "synthetic-audit.db", folder / "synthetic-truth.db"
        raise ValueError("synthetic input folder already exists without its fixture marker")
    folder.mkdir(parents=True)
    audit_path, truth_path = folder / "synthetic-audit.db", folder / "synthetic-truth.db"
    truth = GroundTruthDB(truth_path)
    cases = []
    for i, cause in enumerate(CLASSES):
        cases.append((f"synthetic-ci-{i}", "ci", {"root_cause": cause}, {}))
    for band in ("below", "near", "above"):
        cases.append((f"synthetic-runtime-{band}", "runtime", {"incident": True, "band": band}, {}))
        cases.append(
            (
                f"synthetic-release-{band}",
                "release",
                {
                    "bad_release": True,
                    "band": band,
                    "collaboration_case": True,
                    "acceptable_actions": ["rollout_abort"],
                },
                {},
            )
        )
    cases += [
        ("synthetic-normal", "normal", {}, {"hours": 4}),
        (
            "synthetic-localization",
            "logchunks",
            {"localizable": True, "relevant_lines": [3, 4]},
            {},
        ),
        ("synthetic-invalid", "runtime", {"incident": True, "band": "above"}, {}),
    ]
    for name, family, gt, extra in cases:
        truth.record_run(
            name,
            spec={"id": name, "family": family, **extra},
            ground_truth=gt,
            observed={},
            l0_correct=None,
            valid=name != "synthetic-invalid",
        )
        for index in range(6):
            level = f"L{index}"
            method = {0: "regex", 1: "tfidf_lr", 2: "llm_zero_shot"}.get(index, "llm_retrieval")
            audit = AuditLog(audit_path, name, level)
            action = (
                ("rollout_abort" if index >= 4 else "rollout_promote")
                if family == "release"
                else "retry_job"
            )
            pid = f"p-{name}-{level}"
            audit.proposal(
                {
                    "id": pid,
                    "incident_id": f"i-{name}-{level}",
                    "agent": "release" if family == "release" else "diagnose",
                    "action": action,
                    "target": "synthetic",
                    "environment": "staging",
                    "confidence": 0,
                    "rationale": "synthetic fixture",
                    "params": {},
                    "proposed_at": "2026-01-01T00:00:00Z",
                }
            )
            audit.gate_decision(
                {
                    "proposal_id": pid,
                    "tier": str(CATALOG[action].tier),
                    "risk_score": 0,
                    "outcome": "execute",
                    "reason": "synthetic",
                    "decided_at": "2026-01-01T00:00:01Z",
                }
            )
            audit.execution(
                {
                    "proposal_id": pid,
                    "success": True,
                    "validated": True,
                    "output": "synthetic",
                    "finished_at": "2026-01-01T00:00:02Z",
                }
            )
            observed = {"resolved": True, "latency_s": 0.5, "cost_usd": 0.0}
            if family == "ci":
                observed["root_cause"] = gt["root_cause"] if index >= 1 else "unclear"
                if index >= 2:
                    observed["llm_repeat_subset"] = True
            if family == "runtime":
                observed.update(detected=index >= 1)
                if index >= 1:
                    observed["mttd_s"] = 10.0
            if family == "normal":
                observed["false_alerts"] = 1 if index == 0 else 0
            if family == "logchunks":
                observed["evidence_lines"] = [3] if index >= 3 else [8]
            if family == "release":
                observed.update(
                    selected_action=action,
                    caught_before_full=index >= 4,
                    release_outcome="aborted" if index >= 4 else "held",
                )
            audit.event(
                "offline_replay",
                "replay_result",
                method=method,
                proposal_ids=[pid],
                observed=observed,
            )
            if index >= 2 and family == "ci":
                for repeat in (2, 3):
                    audit.event(
                        "offline_replay",
                        "replay_result",
                        method=method,
                        repeat=repeat,
                        proposal_ids=[],
                        observed={**observed, "llm_repeat_subset": True},
                    )
    marker.write_text("FlowGuard synthetic fixtures v1\n")
    return audit_path, truth_path
