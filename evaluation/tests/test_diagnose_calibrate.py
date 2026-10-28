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
