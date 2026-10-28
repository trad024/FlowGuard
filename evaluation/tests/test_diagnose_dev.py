"""Diagnose dev runs never touch test templates (or LogChunks) before Step 11."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnose_dev  # noqa: E402


def test_only_training_and_validation_templates_are_run(tmp_path, monkeypatch):
    split = diagnose_dev.load_split()
    for variant in split["variants"]:  # the corpus itself is generated, not committed
        (tmp_path / variant).mkdir()
        (tmp_path / variant / "job.log").write_text("log\n", encoding="utf-8")
    monkeypatch.setattr(diagnose_dev, "CORPUS", tmp_path)
    chosen = diagnose_dev.items(split)
    test = {t for t, s in split["templates"].items() if s == "test"}
    assert chosen and not {i["template"] for i in chosen} & test
    assert {i["split"] for i in chosen} <= {"train", "validation"}


def test_test_templates_wait_for_the_staged_freeze():
    with pytest.raises(SystemExit, match="not frozen|diagnose-freeze"):
        diagnose_dev.items(diagnose_dev.load_split(), splits=("train", "test"))
