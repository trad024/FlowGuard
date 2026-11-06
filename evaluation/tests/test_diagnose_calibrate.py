"""D45: every calibration score comes from a model that never saw the example's template."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import diagnose_calibrate  # noqa: E402

LOGS = {
    "dep-a": ("dependency", "go: verifying example.com/a@v1.0.0: checksum mismatch"),
    "dep-b": ("dependency", "go: example.com/b@v2.0.0: reading go.sum: checksum mismatch"),
    "code-a": ("code", "./main.go:12:3: undefined: handlerFunc"),
    "code-b": ("code", "./api.go:40:1: undefined: routeTable"),
    "env-a": ("environment", "error: environment variable PODINFO_PORT is not set"),
}


def rows(tmp_path, monkeypatch):
    out = []
    for template, (_, text) in LOGS.items():
        for i in range(3):
            log = tmp_path / f"{template}-{i}.log"
            log.write_text(f"step {i}\n{text}\n", encoding="utf-8")
            out.append({"variant": f"{template}-{i}", "template": template, "log": log})
    truth = {f"{t}-{i}": label for t, (label, _) in LOGS.items() for i in range(3)}
    monkeypatch.setattr(diagnose_calibrate, "truth", truth.__getitem__)
    return out


def test_l1_scores_come_from_a_model_refit_without_the_template(tmp_path, monkeypatch):
    r = rows(tmp_path, monkeypatch)
    scores = diagnose_calibrate.l1_scores(r)
    env = [s for s, x in zip(scores, r, strict=True) if x["template"] == "env-a"]
    # environment occurs in one template only: the model scoring it never learned that class
    assert env and all("environment" not in s for s in env)
    assert all(
        "environment" in s for s, x in zip(scores, r, strict=True) if x["template"] != "env-a"
    )


def test_calibration_reports_coverage_per_held_apart_template(tmp_path, monkeypatch):
    r = rows(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnose_calibrate, "MODELS", tmp_path / "models")
    monkeypatch.setattr(diagnose_calibrate, "ROOT", tmp_path)
    out = diagnose_calibrate.calibrate("L1", r, diagnose_calibrate.l1_scores(r))
    assert set(out["folds"]) == set(LOGS) and out["coverage"]["n"] == 15
    assert out["examples"] == 15 and not out["calibrated"]  # under MIN_CALIBRATION
    assert (tmp_path / "models" / "diagnose-calibration-L1.json").is_file()


def test_l3_calibrates_through_the_same_code_and_schema_as_l1_and_l2(tmp_path, monkeypatch):
    """L3's answers are read like L2's, from the dev answers folder, and calibrated by the same
    leave-one-template-out code: the output differs only in the level."""
    import json

    import diagnose_dev

    r = rows(tmp_path, monkeypatch)
    monkeypatch.setattr(diagnose_calibrate, "MODELS", tmp_path / "models")
    monkeypatch.setattr(diagnose_calibrate, "ROOT", tmp_path)
    answers = tmp_path / "answers"
    answers.mkdir()
    for level in ("L2", "L3"):
        with (answers / f"{level}.jsonl").open("w", encoding="utf-8") as f:
            for x in r:
                cls = diagnose_calibrate.truth(x["variant"])
                f.write(json.dumps({"variant": x["variant"], "scores": {cls: 0.8, "code": 0.2}}))
                f.write("\n")
    monkeypatch.setattr(diagnose_dev, "OUT", answers)
    l2 = diagnose_calibrate.calibrate("L2", r, diagnose_calibrate.llm_scores(r, "L2"))
    l3 = diagnose_calibrate.calibrate("L3", r, diagnose_calibrate.llm_scores(r, "L3"))
    assert set(l3) == set(l2) and set(l3["folds"]) == set(l2["folds"]) == set(LOGS)
    assert {k: v for k, v in l3.items() if k not in ("level", "calibration_file")} == {
        k: v for k, v in l2.items() if k not in ("level", "calibration_file")
    }
    saved = json.loads((tmp_path / "models" / "diagnose-calibration-L3.json").read_text())
    assert saved["level"] == "L3" and saved["method"] == "leave-one-template-out (D45)"
