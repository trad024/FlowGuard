"""D67: every Orchestrator built outside the tests takes its calibrators from the one loader,
flowguard.calibrators.for_level. Kept here, not in agents/tests: it names files of the
experiment side, which agent code and its tests never read (D25)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


ENTRY_POINTS = {
    "agents/flowguard/service.py",
    "scenarios/ci_live.py",
    "evaluation/ci_levels.py",
    "evaluation/release_replay.py",
    "scenarios/live_release.py",
    "evaluation/dso6_run.py",
}


def test_every_orchestrator_built_outside_tests_takes_the_loaders_calibrators():
    builders = set()
    for path in ROOT.glob("*/**/*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/tests/" in rel or rel == "agents/flowguard/orchestrator.py" or rel.startswith("."):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for call in re.finditer(r"Orchestrator\(", text):
            builders.add(rel)
            body = text[call.end() : call.end() + 600]
            assert "calibrators=calibrators.for_level(" in body, rel
    assert builders == ENTRY_POINTS
