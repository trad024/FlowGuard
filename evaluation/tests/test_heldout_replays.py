"""K4: the held-out scoring mode of monitor_replay and release_replay. Dev mode unchanged (it
still refuses held-out material), held-out mode gated (D69 stage B), labelled "held-out", and its
replay records accepted end to end by report_data.load and report_scope. Synthetic recordings in
the recorder's format; no real held-out data, seed or ground-truth DB."""

import json

import pytest
import test_monitor_replay as tm
import test_release_replay as tr

import groundtruth
import heldout_guard
import monitor_replay as mr
import release_replay as rr
import report_data
import report_scope
from flowguard.audit import AuditLog
from flowguard.monitor import MonitorModel


@pytest.fixture
def gate(monkeypatch):
    """The stage B checks, observed: none may run before the confirmation."""
    called = []
    for name in ("frozen_agents", "diagnose_frozen", "checked_seed"):
        monkeypatch.setattr(heldout_guard.heldout, name,
                            lambda *a, n=name, **k: called.append(n))  # fmt: skip
    return called


def truth_db(path, recordings):
    """One truth row per recording, from its own ground_truth.json (as the runner writes it)."""
    db = groundtruth.GroundTruthDB(path)
    for rec in recordings:
        gt = json.loads((rec / "ground_truth.json").read_text())
        db.record_run(rec.name, spec=gt.get("spec") or {},
                      ground_truth=gt.get("ground_truth") or {}, observed={}, l0_correct=None,
                      valid=True, recording=str(rec))  # fmt: skip
    return path


def loaded(tmp_path, rows, recordings):
    replay = tmp_path / "records.jsonl"
    replay.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    truth = truth_db(tmp_path / "truth.db", recordings)
    AuditLog(tmp_path / "audit.db")
    return report_data.load(tmp_path / "audit.db", truth, replay), truth


# ---------------------------------------------------------------- gating


def test_the_stage_b_gate_checks_confirmation_first(gate):
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        heldout_guard.require_stage_b(False, "x")
    assert gate == []
    heldout_guard.require_stage_b(True, "x")
    assert gate == ["frozen_agents", "diagnose_frozen", "checked_seed"]


@pytest.mark.parametrize("module", [mr, rr])
def test_held_out_mode_refuses_without_confirmation(module, tmp_path, gate):
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        module.main(["score", str(tmp_path / "heldout"), "--heldout"])
    assert gate == []


@pytest.mark.parametrize("module", [mr, rr])
def test_held_out_mode_refuses_without_eval_freeze(module, tmp_path, monkeypatch):
    def untagged(*a, **k):
        raise SystemExit("tag the agent code eval-freeze before any held-out run")

    monkeypatch.setattr(heldout_guard.heldout, "frozen_agents", untagged)
    with pytest.raises(SystemExit, match="eval-freeze"):
        module.main(["score", str(tmp_path / "heldout"), "--heldout", "--confirm-step-11"])


def test_held_out_recordings_are_never_fitted_or_calibrated_on(tmp_path, gate):
    with pytest.raises(SystemExit, match="never fitted"):
        mr.main(["fit", str(tmp_path), "--heldout", "--confirm-step-11"])
    with pytest.raises(SystemExit, match="never calibrated"):
        rr.main(["calibrate", str(tmp_path), "--heldout", "--confirm-step-11"])


def test_dev_mode_still_refuses_held_out_material(tmp_path):
    tm.write_recording(tmp_path / "heldout", "h-1", family="runtime", seconds=60)
    with pytest.raises(ValueError, match="held-out"):
        mr.load_runs(tmp_path)
    with pytest.raises(ValueError, match="held-out"):
        rr.release_runs([tmp_path / "heldout"])


# ---------------------------------------------------------------- monitor_replay


@pytest.fixture
def monitor_set(tmp_path):
    """Dev recordings to fit on, and held-out ones the model never saw."""
    dev = tmp_path / "dev"
    tm.write_recording(dev, "normal", family="normal", seconds=7200, seed=3)
    s = tm.T0 + 20_000
    err, truth = tm.incident(s + 60, "above", 0.9)
    tm.write_recording(dev, "rt-dev", family="runtime", start=s, seconds=420, seed=5, error=err,
                       truth=truth, alerts=[("HighErrorRatio", [s + 195])])  # fmt: skip
    model = mr.fit(mr.load_runs(dev), grid=tm.TINY_GRID)
    model = MonitorModel.load(model.save(tmp_path / "monitor.json"))
    held = tmp_path / "heldout"
    s2 = tm.T0 + 40_000
    err, truth = tm.incident(s2 + 60, "above", 0.9)
    tm.write_recording(held, "h-rt", family="runtime", start=s2, seconds=420, seed=7, error=err,
                       truth=truth, alerts=[("HighErrorRatio", [s2 + 195])])  # fmt: skip
    tm.write_recording(held, "h-normal", family="normal", start=tm.T0 + 60_000, seconds=3600,
                       seed=8)  # fmt: skip
    return {"model": model, "held": held, "model_path": tmp_path / "monitor.json"}


def test_monitor_held_out_score_is_labelled_held_out(monitor_set):
    runs = mr.load_runs(monitor_set["held"], heldout=True)
    out = mr.score(runs, monitor_set["model"], heldout=True)
    assert out["label"] == out["runtime"]["label"] == "held-out"
    assert "dev, in-sample" not in json.dumps(out)
    fa = out["runtime"]["L0"]["false_alerts_per_hour"]
    assert fa["hours"] == pytest.approx(1.0, abs=0.01)  # the whole held-out normal window


def test_monitor_records_are_accepted_end_to_end(monitor_set, tmp_path):
    runs = mr.load_runs(monitor_set["held"], heldout=True)
    rows = mr.records(runs, monitor_set["model"])
    kinds = {(r["scenario_id"], r["level"]): r["observed"] for r in rows}
    assert kinds[("h-rt", "L0")]["detected"] is True and kinds[("h-rt", "L0")]["mttd_s"] > 0
    assert kinds[("h-normal", "L1")] == {"false_alerts": kinds[("h-normal", "L1")]["false_alerts"]}
    recs = sorted(monitor_set["held"].iterdir())
    (joined, summary), truth = loaded(tmp_path, rows, recs)
    assert summary["replay_records"] == len(rows) == 4 and summary["truth_without_replay"] == []
    assert report_scope.labels(truth) == {"synthetic": False, "development": False}
    reason = report_scope.confirmatory_scope({"status": "frozen"}, truth, root=tmp_path)
    assert isinstance(reason, str)  # not confirmatory without the frozen state, never an error


def test_monitor_cli_writes_held_out_records(monitor_set, tmp_path, gate):
    out, rec = tmp_path / "score.json", tmp_path / "records.jsonl"
    mr.main(["score", str(monitor_set["held"]), "--heldout", "--confirm-step-11",
             "--model", str(monitor_set["model_path"]), "--out", str(out),
             "--records", str(rec)])  # fmt: skip
    assert json.loads(out.read_text())["label"] == "held-out"
    assert len(rec.read_text().splitlines()) == 4


# ---------------------------------------------------------------- release_replay


@pytest.fixture
def release_set(tmp_path, fitted_levels):
    held = tmp_path / "heldout"
    tr.write_recording(held, "h-bad", bad=True, band="above", l0="aborted", abort_at=70,
                       errors=0.1)  # fmt: skip
    tr.write_recording(held, "h-good", bad=False)
    return held


def test_release_held_out_records_are_accepted_end_to_end(release_set, tmp_path):
    result = rr.score(rr.release_runs([release_set], heldout=True), heldout=True)
    assert result["label"] == "held-out"
    rows = rr.records(result)
    by = {(r["scenario_id"], r["level"]): r["observed"] for r in rows}
    assert by[("h-bad", "L3")]["caught_before_full"] is True
    assert by[("h-bad", "L3")]["release_outcome"] == "aborted"
    assert by[("h-bad", "L3")]["time_to_rollback_s"] > 0
    assert by[("h-good", "L3")]["release_outcome"] == "promoted"
    assert by[("h-good", "L3")]["caught_before_full"] is False
    assert {lvl for _, lvl in by} == {"L0", "L3", "L4"}
    (joined, summary), truth = loaded(tmp_path, rows, sorted(release_set.iterdir()))
    assert summary["joined_records"] == len(rows) and summary["truth_without_replay"] == []
    assert report_scope.labels(truth)["development"] is False


def test_release_dev_score_keeps_its_label(release_set, tmp_path):
    dev = tmp_path / "dev"
    tr.write_recording(dev, "r-good", bad=False)
    assert rr.score(rr.release_runs([dev]))["label"] == "dev, in-sample"
