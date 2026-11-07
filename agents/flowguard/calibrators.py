"""The gate's calibrators per level (D67): the one loader every entry point uses.

The gate reads a calibrated confidence for each proposal (D9). Before D67 no entry point gave
the orchestrator Diagnose's calibration, so every Diagnose confidence was 0 and every
medium-tier Diagnose action went to a human at every level, against D45's intent.

- Diagnose: `state/models/diagnose-calibration-<L>.json` (D45), fitted leave-one-template-out
  on the dev templates. L1, L2 and L3 use their own; L4 and L5 use L3's: their Diagnose
  requests are byte-identical to L3's (same method, same inputs, D39), so their scores come
  from the distribution L3 was calibrated on.
- Release: `agents/flowguard/models/release-calibration.json` (D9), as before.
- Fail closed: a missing calibration file, a file whose SHA-256 differs from the frozen one,
  or a calibrator with too few examples refuses; there is never an empty fallback. The frozen
  hashes are read from `models/diagnose-calibration.sha256` next to this module: agent code
  never reads the experiment side (D25). A test and the Diagnose freeze check keep that file
  equal to the matching lines of the freeze hashes.
- `require` is the orchestrator's guard: at L1+ every agent that may propose a medium-tier
  action must have a fitted calibrator, unless it is classified exempt below. The Optimizer is
  exempt: it acts in app-ci's plan step (Step 10, DSO5), not through the orchestrator; no
  calibration is decided for it; no event source produces `pipeline_started`. Any skip_jobs
  that reaches the orchestrator therefore goes to a human (rule 6). L0 has no gate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from flowguard import release
from flowguard.calibration import ConformalCalibrator
from flowguard.catalog import AGENT_ACTIONS, CATALOG
from flowguard.contracts import AgentName, RiskTier

ROOT = Path(__file__).resolve().parents[2]
HASHES = Path(__file__).resolve().parent / "models" / "diagnose-calibration.sha256"
# Which Diagnose calibration each level uses (D45, D67)
DIAGNOSE_CALIBRATION = {"L1": "L1", "L2": "L2", "L3": "L3", "L4": "L3", "L5": "L3"}
CALIBRATED = (AgentName.DIAGNOSE, AgentName.RELEASE)
EXEMPT = {
    AgentName.OPTIMIZER: "acts in app-ci's plan step (Step 10, DSO5), not through the "
    "orchestrator; no calibration decided; no event source produces pipeline_started; "
    "a skip_jobs reaching the orchestrator goes to a human (rule 6)",
}


class CalibrationRefused(RuntimeError):
    """A calibration the gate needs is missing, changed or not fitted."""


def medium_tier_agents() -> set[AgentName]:
    """Agents allowed at least one medium-tier action (D36's AGENT_ACTIONS)."""
    tiers = {action: entry.tier for action, entry in CATALOG.items()}
    return {
        agent
        for agent, actions in AGENT_ACTIONS.items()
        if any(tiers[a] == RiskTier.MEDIUM for a in actions)
    }


def diagnose_path(level: str, root: Path | None = None) -> Path:
    root = root or ROOT
    if level not in DIAGNOSE_CALIBRATION:
        raise CalibrationRefused(f"no Diagnose calibration is defined for {level}")
    name = f"diagnose-calibration-{DIAGNOSE_CALIBRATION[level]}.json"
    return root / "state" / "models" / name


def frozen_hashes(hashes: Path | None = None) -> dict[str, str]:
    """{relative path: SHA-256} of the frozen Diagnose calibrations."""
    hashes = hashes or HASHES
    if not hashes.is_file():
        raise CalibrationRefused(f"{hashes.name} is missing")
    out = {}
    for line in hashes.read_text(encoding="utf-8").splitlines():
        digest, _, path = line.partition("  ")
        if path.strip():
            out[path.strip()] = digest.strip()
    return out


def diagnose(
    level: str, root: Path | None = None, hashes: Path | None = None
) -> ConformalCalibrator:
    """Diagnose's calibrator for `level`, checked against its frozen SHA-256."""
    root = root or ROOT
    path = diagnose_path(level, root)
    rel = path.relative_to(root).as_posix()
    if not path.is_file():
        raise CalibrationRefused(f"refusing to start: {rel} is missing")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    frozen = frozen_hashes(hashes)
    if rel not in frozen:
        raise CalibrationRefused(f"refusing to start: {rel} has no frozen hash")
    if digest != frozen[rel]:
        raise CalibrationRefused(f"refusing to start: {rel} differs from its frozen hash")
    cal = ConformalCalibrator.load(path)
    if not cal.calibrated:
        raise CalibrationRefused(f"refusing to start: {rel} has too few examples")
    return cal


def for_level(level: str, root: Path | None = None) -> dict[AgentName, ConformalCalibrator]:
    """The calibrators the orchestrator needs at `level` (none at L0, which has no gate)."""
    if level == "L0":
        return {}
    rel = release.CALIBRATION_FILE
    if not rel.is_file():
        raise CalibrationRefused(f"refusing to start: {rel.name} is missing")
    return {AgentName.DIAGNOSE: diagnose(level, root), AgentName.RELEASE: release.calibrator()}


def require(cfg, calibrators: dict) -> None:
    """The orchestrator's guard: at L1+, every medium-tier agent not exempt has a fitted
    calibrator; an agent nobody classified refuses too."""
    if not cfg.gate:
        return
    for agent in sorted(medium_tier_agents(), key=str):
        if agent in EXEMPT:
            continue
        if agent not in CALIBRATED:
            raise CalibrationRefused(f"{agent}: medium-tier agent neither calibrated nor exempt")
        cal = calibrators.get(agent)
        if cal is None or not cal.calibrated:
            raise CalibrationRefused(
                f"refusing to start at {cfg.level}: no fitted calibrator for {agent} "
                "(use flowguard.calibrators.for_level)"
            )
