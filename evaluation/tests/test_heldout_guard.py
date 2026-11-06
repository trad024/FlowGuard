"""D65: every held-out Diagnose entry point needs --confirm-step-11 and the staged freeze; no
test here depends on the repository's own tags, and none reaches the network."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ci_levels  # noqa: E402
import heldout_guard  # noqa: E402


def recording(root, name, spec_id="ci-code", template="code-01"):
    rec = root / name
    rec.mkdir(parents=True)
    gt = {"spec": {"id": spec_id, "family": "ci"}, "ground_truth": {"template_id": template}}
    (rec / "ground_truth.json").write_text(json.dumps(gt), encoding="utf-8")
    return rec


def test_held_out_recordings_are_recognised(tmp_path):
    test_template = sorted(heldout_guard.test_templates())[0]
    assert not heldout_guard.is_heldout_recording(recording(tmp_path, "dev"))
    assert heldout_guard.is_heldout_recording(recording(tmp_path, "t", template=test_template))
    assert heldout_guard.is_heldout_recording(recording(tmp_path, "h", spec_id="h-0123456789"))
    assert heldout_guard.is_heldout_recording(recording(tmp_path / "heldout", "r-1"))


def test_ci_levels_refuses_a_held_out_recording_without_the_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(heldout_guard.heldout, "diagnose_frozen", lambda: None)  # frozen
    rec = recording(tmp_path, "h", spec_id="h-0123456789")
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        ci_levels.main([str(rec)])  # refused before any LLM client is built


def test_ci_levels_with_the_confirmation_still_needs_the_freeze(tmp_path, monkeypatch):
    def not_frozen():
        raise SystemExit("the pre-registration is not frozen")

    monkeypatch.setattr(heldout_guard.heldout, "diagnose_frozen", not_frozen)
    rec = recording(tmp_path, "h", spec_id="h-0123456789")
    with pytest.raises(SystemExit, match="not frozen"):
        ci_levels.main([str(rec), "--confirm-step-11"])
