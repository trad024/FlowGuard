"""Diagnose calibration (D45): leave-one-template-out conformal on train + validation templates.

    python evaluation/diagnose_calibrate.py --level L1   # refits L1 once per template
    python evaluation/diagnose_calibrate.py --level L2   # L2 has no training: its dev answers

Every example's class scores come from a model that never saw its template: L1 is refit without
the template, L2 has no training, L3's retrieval excludes cases of the same template. The pooled
out-of-template scores are the calibration set, written to state/models/; coverage is reported
per template held apart (a calibrator fitted on the other templates only). Test templates are
never read (D6, D39).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "agents"), str(ROOT / "evaluation"), str(ROOT / "scenarios")]

import diagnose_dev  # noqa: E402
import stats  # noqa: E402
from flowguard.calibration import (  # noqa: E402
    MIN_CALIBRATION,
    ConformalCalibrator,
    coverage_by_group,
)
from flowguard.classical import TfidfDiagnoser  # noqa: E402
from flowguard.llm import STATE  # noqa: E402

EPSILON = 0.1
MODELS = STATE / "models"


def truth(variant: str) -> str:
    meta = diagnose_dev.CORPUS / variant / "meta.json"
    return json.loads(meta.read_text(encoding="utf-8"))["root_cause"]


def dev_items() -> list[dict]:
    return diagnose_dev.items(diagnose_dev.load_split())


def l1_scores(rows: list[dict]) -> list[dict[str, float]]:
    """L1 refit once per template, without it, then scored on that template's examples."""
    logs = [r["log"].read_text(encoding="utf-8", errors="replace") for r in rows]
    labels = [truth(r["variant"]) for r in rows]
    out: list[dict[str, float] | None] = [None] * len(rows)
    for template in sorted({r["template"] for r in rows}):
        rest = [i for i, r in enumerate(rows) if r["template"] != template]
        model = TfidfDiagnoser().fit([logs[i] for i in rest], [labels[i] for i in rest])
        for i, r in enumerate(rows):
            if r["template"] == template:
                out[i] = model.scores(logs[i])
    return out  # type: ignore[return-value]


def llm_scores(rows: list[dict], level: str) -> list[dict[str, float]]:
    """The cached dev answers of an LLM level (the last answer per variant)."""
    answers = {}
    for line in (diagnose_dev.OUT / f"{level}.jsonl").read_text(encoding="utf-8").splitlines():
        a = json.loads(line)
        answers[a["variant"]] = a.get("scores") or {}
    missing = [r["variant"] for r in rows if r["variant"] not in answers]
    if missing:
        raise SystemExit(f"{level}: no dev answer for {len(missing)} variants, e.g. {missing[:3]}")
    return [answers[r["variant"]] for r in rows]


def calibrate(level: str, rows: list[dict], scores: list[dict[str, float]]) -> dict:
    labels = [truth(r["variant"]) for r in rows]
    groups = [r["template"] for r in rows]
    folds = coverage_by_group(scores, labels, groups, EPSILON)
    n = sum(f["n"] for f in folds.values())
    k = sum(f["covered"] for f in folds.values())
    cal = ConformalCalibrator().fit(scores, labels)
    path = cal.save(
        MODELS / f"diagnose-calibration-{level}.json",
        level=level,
        method="leave-one-template-out (D45)",
        templates=len(folds),
        examples=len(labels),
    )
    return {
        "level": level,
        "calibration_file": path.relative_to(ROOT).as_posix(),
        "examples": len(labels),
        "templates": len(folds),
        "calibrated": len(labels) >= MIN_CALIBRATION,
        "epsilon": EPSILON,
        "coverage": {"k": k, "n": n, "ci95": stats.wilson(k, n), "target": 1 - EPSILON},
        "mean_set_size": sum(f["mean_set_size"] * f["n"] for f in folds.values()) / n,
        "folds": folds,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--level", choices=["L1", "L2"], required=True)
    args = parser.parse_args(argv)
    rows = dev_items()
    scores = l1_scores(rows) if args.level == "L1" else llm_scores(rows, args.level)
    out = calibrate(args.level, rows, scores)
    diagnose_dev.OUT.mkdir(parents=True, exist_ok=True)
    report = diagnose_dev.OUT / f"{args.level}-calibration.json"
    report.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "folds"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
