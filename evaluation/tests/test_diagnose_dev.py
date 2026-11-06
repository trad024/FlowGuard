"""Diagnose dev runs never touch test templates (or LogChunks) before Step 11."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnose_dev  # noqa: E402
import heldout_guard  # noqa: E402


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


def not_frozen():
    raise SystemExit("the pre-registration is not frozen")


@pytest.fixture
def frozen(monkeypatch):
    """The staged freeze holds (whatever the repository's own tags say)."""
    calls = []
    monkeypatch.setattr(heldout_guard.heldout, "diagnose_frozen", lambda: calls.append(1))
    return calls


def test_test_templates_wait_for_the_staged_freeze(monkeypatch):
    monkeypatch.setattr(heldout_guard.heldout, "diagnose_frozen", not_frozen)
    with pytest.raises(SystemExit, match="not frozen"):
        diagnose_dev.items(diagnose_dev.load_split(), ("train", "test"), confirm_step_11=True)


def test_test_templates_need_the_confirmation_even_when_frozen(frozen):
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        diagnose_dev.items(diagnose_dev.load_split(), splits=("train", "test"))
    assert frozen == []  # refused before the freeze is even asked


def test_once_frozen_and_confirmed_a_test_template_run_gets_past_the_gate(frozen, tmp_path,
                                                                          monkeypatch):  # fmt: skip
    split = diagnose_dev.load_split()
    for variant in split["variants"]:
        (tmp_path / variant).mkdir()
        (tmp_path / variant / "job.log").write_text("log\n", encoding="utf-8")
    monkeypatch.setattr(diagnose_dev, "CORPUS", tmp_path)
    chosen = diagnose_dev.items(split, ("train", "test"), confirm_step_11=True)
    assert frozen == [1] and chosen  # diagnose_frozen ran, and the run went on


class SeenRetriever:
    def __init__(self):
        self.asked = []

    def nearest(self, log, template=None):
        self.asked.append(template)
        return []


def test_l3_is_accepted_and_never_retrieves_from_the_variants_own_template(tmp_path, monkeypatch):
    split = diagnose_dev.load_split()
    for variant in split["variants"]:
        (tmp_path / variant).mkdir()
        (tmp_path / variant / "job.log").write_text("log\n", encoding="utf-8")
    monkeypatch.setattr(diagnose_dev, "CORPUS", tmp_path)
    monkeypatch.setattr(diagnose_dev, "ready_lanes", list)  # no lane: stops before any call
    retriever = SeenRetriever()
    with pytest.raises(SystemExit, match="no usable lane"):
        diagnose_dev.run("L3", retriever=retriever)  # L3 gets past its checks
    todo = diagnose_dev.items(split)
    assert retriever.asked == [i["template"] for i in todo]  # own template excluded (D45)


def test_l3_refuses_test_templates_like_l2(frozen):
    for level in ("L2", "L3"):  # frozen, but not confirmed: refused before any lane check
        with pytest.raises(SystemExit, match="--confirm-step-11"):
            diagnose_dev.run(level, splits=("train", "test"), retriever=SeenRetriever())
