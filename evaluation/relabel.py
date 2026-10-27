"""Blind re-label sheet for the LogChunks sample (D26a).

    python evaluation/relabel.py --first evaluation/datasets/labelling/<first pass>.csv \
        --finished 2026-11-02

One labeller labels all 150 logs. At least 7 days after finishing (`--finished`, the date the
first pass was completed), this writes a blank sheet for a seeded random 50 of them, in a
seeded shuffled order and without the first answers, so the second pass is blind. Intra-rater
Cohen's kappa between the two passes is computed in Step 4b.

It refuses to run if the first pass is incomplete (every row needs a class) or too recent.
The selection depends only on the sample ids and RELABEL_SEED, never on the first answers.
Output: evaluation/datasets/labelling/logchunks-relabel-50-blank.csv (never overwritten).
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABELLING = ROOT / "evaluation" / "datasets" / "labelling"
OUT = LABELLING / "logchunks-relabel-50-blank.csv"
RELABEL_SEED = 20261027
RELABEL_SIZE = 50
MIN_DAYS = 7
CLASSES = {"code", "dependency", "environment", "flaky_test", "infrastructure", "unclear"}
COLUMNS = [
    "sample_id",
    "log_path",
    "labeller",
    "root_cause",
    "confidence_1_to_3",
    "evidence_lines",
    "notes",
]


def read_first_pass(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = [r["sample_id"] for r in rows if r.get("root_cause", "").strip() not in CLASSES]
    if not rows or missing:
        shown = ", ".join(missing[:5]) + ("..." if len(missing) > 5 else "")
        raise SystemExit(f"first pass incomplete: no valid root_cause for {shown or 'any row'}")
    return rows


def select(sample_ids: list[str], size: int = RELABEL_SIZE, seed: int = RELABEL_SEED) -> list[str]:
    """A seeded random subset, in a seeded random order (not the first pass's order)."""
    rng = random.Random(seed)
    chosen = rng.sample(sorted(sample_ids), size)
    rng.shuffle(chosen)
    return chosen


def write_sheet(first: list[dict], finished: date, today: date, out: Path = OUT) -> list[str]:
    waited = (today - finished).days
    if waited < MIN_DAYS:
        raise SystemExit(f"only {waited} days since the first pass; wait at least {MIN_DAYS}")
    if out.exists():
        raise SystemExit(f"{out.name} exists; the re-label sheet is written once")
    paths = {r["sample_id"]: r["log_path"] for r in first}
    chosen = select(list(paths))
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for sid in chosen:  # log path only: no labeller, class, confidence or evidence
            writer.writerow({"sample_id": sid, "log_path": paths[sid]})
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--first", type=Path, required=True, help="the completed first pass")
    parser.add_argument("--finished", type=date.fromisoformat, required=True)
    args = parser.parse_args(argv)
    chosen = write_sheet(read_first_pass(args.first), args.finished, date.today())
    print(f"{len(chosen)} logs to re-label: {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
