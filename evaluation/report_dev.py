"""Import explicitly scoped development observations into isolated reporting databases.

This adapter does not run agents or invent actions, approvals, executions or missing levels.
Use the original audit/truth input mode for reports that include execution/approval evidence.
"""

import hashlib
import json
from pathlib import Path

from flowguard.audit import AuditLog
from groundtruth import GroundTruthDB
from report_data import LEVELS, validate_observed


def create(bundle_path: Path, folder: Path):
    data = bundle_path.read_bytes()
    bundle = json.loads(data)
    if bundle.get("scope") != "development":
        raise ValueError("development bundle requires explicit development scope")
    scenarios = bundle.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("development bundle requires nonempty scenarios")
    seen = set()
    for scenario in scenarios:
        name = scenario["id"]
        if not isinstance(name, str) or not name.startswith("dev-") or name in seen:
            raise ValueError("development scenario IDs must be unique and start with dev-")
        seen.add(name)
        if scenario["family"] not in (
            "ci",
            "security",
            "pipeline",
            "runtime",
            "release",
            "normal",
            "logchunks",
        ):
            raise ValueError("unknown development scenario family")
        if not isinstance(scenario["ground_truth"], dict):
            raise ValueError("development ground truth must be an object")
        observations = scenario["observations"]
        if not isinstance(observations, list) or not observations:
            raise ValueError("development scenario requires recorded observations")
        keys = set()
        for observation in observations:
            if observation["level"] not in LEVELS:
                raise ValueError("invalid development ablation level")
            if observation.get("proposal_ids", []) or any(
                key in observation["observed"] for key in ("resolved", "selected_action")
            ):
                raise ValueError("execution observations require the original audited databases")
            validate_observed(observation["observed"])
            key = (
                observation["level"],
                observation.get("method", "default"),
                observation.get("setting", "default"),
                observation.get("repeat", 1),
            )
            if key in keys:
                raise ValueError("duplicate development observation")
            keys.add(key)
    marker_value = {
        "kind": "development observation import v1",
        "bundle_sha256": hashlib.sha256(data).hexdigest(),
    }
    marker = folder / "DEVELOPMENT.json"
    audit, truth = folder / "dev-audit.db", folder / "dev-truth.db"
    if folder.exists():
        if (
            marker.is_file()
            and json.loads(marker.read_text()) == marker_value
            and audit.is_file()
            and truth.is_file()
        ):
            return audit, truth
        raise ValueError("development input folder exists without the matching bundle marker")
    folder.mkdir(parents=True)
    database = GroundTruthDB(truth)
    for scenario in scenarios:
        name = scenario["id"]
        database.record_run(
            name,
            spec={"id": name, "family": scenario["family"]},
            ground_truth=scenario["ground_truth"],
            observed={},
            l0_correct=None,
            valid=True,
        )
        for observation in scenario["observations"]:
            AuditLog(audit, name, observation["level"]).event(
                "offline_development_export",
                "replay_result",
                proposal_ids=[],
                **{
                    k: observation[k]
                    for k in ("method", "setting", "repeat", "observed")
                    if k in observation
                },
            )
    marker.write_text(json.dumps(marker_value, indent=2) + "\n", encoding="utf-8")
    return audit, truth
