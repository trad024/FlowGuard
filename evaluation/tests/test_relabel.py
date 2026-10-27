"""Blind re-label sheet (D26a): only after a complete first pass and 7 days, seeded, blind."""

import csv
from datetime import date

import pytest

import relabel

SAMPLE = relabel.ROOT / "evaluation" / "datasets" / "logchunks-sample-150.csv"


def first_pass(tmp_path, blank_ids=()):
    with SAMPLE.open(encoding="utf-8", newline="") as f:
        sample = list(csv.DictReader(f))
    path = tmp_path / "pass1.csv"
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=relabel.COLUMNS)
        w.writeheader()
        for row in sample:
            cause = "" if row["sample_id"] in blank_ids else "code"
            w.writerow(
                {
                    "sample_id": row["sample_id"],
                    "log_path": row["log_path"],
                    "labeller": "me",
                    "root_cause": cause,
                    "confidence_1_to_3": "3",
                    "evidence_lines": "10-12",
                    "notes": "secret first answer",
                }
            )
    return path


def test_sheet_is_seeded_blind_and_written_once(tmp_path):
    first = relabel.read_first_pass(first_pass(tmp_path))
    out = tmp_path / "relabel.csv"
    chosen = relabel.write_sheet(first, date(2026, 11, 1), date(2026, 11, 8), out)
    assert len(chosen) == len(set(chosen)) == 50
    assert chosen == relabel.select([r["sample_id"] for r in first])  # reproducible
    ids = [r["sample_id"] for r in first]
    assert chosen != sorted(chosen) and chosen != [i for i in ids if i in chosen]  # reordered
    text = out.read_text(encoding="utf-8")
    assert "secret first answer" not in text and "10-12" not in text and ",code," not in text
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["sample_id"] for r in rows] == chosen
    assert all(r["root_cause"] == r["labeller"] == "" for r in rows)
    with pytest.raises(SystemExit, match="written once"):
        relabel.write_sheet(first, date(2026, 11, 1), date(2026, 11, 9), out)


def test_refuses_an_incomplete_first_pass_or_too_early(tmp_path):
    with pytest.raises(SystemExit, match="incomplete"):
        relabel.read_first_pass(first_pass(tmp_path, blank_ids={"LC007"}))
    first = relabel.read_first_pass(first_pass(tmp_path))
    with pytest.raises(SystemExit, match="wait at least 7"):
        relabel.write_sheet(first, date(2026, 11, 1), date(2026, 11, 7), tmp_path / "x.csv")
    assert not (tmp_path / "x.csv").exists()


def test_the_selection_does_not_depend_on_the_answers(tmp_path):
    ids = [f"LC{i:03d}" for i in range(1, 151)]
    assert relabel.select(ids) == relabel.select(list(reversed(ids)))
