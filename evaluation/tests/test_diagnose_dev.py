"""Diagnose dev runs never touch test templates (or LogChunks) before Step 11."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnose_dev  # noqa: E402


def test_only_training_and_validation_templates_are_run():
    split = diagnose_dev.load_split()
    chosen = diagnose_dev.items(split)
    test = {t for t, s in split["templates"].items() if s == "test"}
    assert chosen and not {i["template"] for i in chosen} & test
    assert {i["split"] for i in chosen} <= {"train", "validation"}


def test_test_templates_wait_for_the_staged_freeze():
    with pytest.raises(SystemExit, match="not frozen|diagnose-freeze"):
        diagnose_dev.items(diagnose_dev.load_split(), splits=("train", "test"))
