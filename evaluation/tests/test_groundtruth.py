"""Ground-truth store (D25): events and one row per run, kept apart from the audit log."""

import groundtruth


def test_events_and_runs_round_trip(tmp_path):
    db = groundtruth.GroundTruthDB(tmp_path / "gt.db")
    db.event("r-1", "fault_injected", delivery="config", settings={"error_rate": 0.05})
    db.event("r-2", "l0_outcome", outcome="aborted", run_id=[77])  # a CI run id in the payload
    rows = list(db.events(run_id="r-1"))
    assert [r["kind"] for r in rows] == ["fault_injected"]
    assert rows[0]["payload"]["settings"]["error_rate"] == 0.05
    (row,) = db.events(kind="l0_outcome")
    assert row["run_id"] == "r-2" and row["payload"]["run_id"] == [77]

    spec = {"id": "release-errors-05", "family": "release"}
    db.record_run(
        "r-1", spec=spec, ground_truth={"bad_release": True}, observed={}, l0_correct=None
    )
    db.record_run(
        "r-1",
        spec=spec,
        ground_truth={"bad_release": True},
        observed={"l0": "aborted"},
        l0_correct=True,
        valid=False,
    )  # recorded again: replaced
    row = db.run("r-1")
    assert (row["spec_id"], row["family"], row["l0_correct"], row["valid"]) == (
        "release-errors-05",
        "release",
        True,
        False,
    )
    assert row["observed"] == {"l0": "aborted"} and db.run("r-9") is None


def test_default_path_is_gitignored_and_env_overrides(tmp_path, monkeypatch):
    assert groundtruth.DEFAULT_PATH.relative_to(groundtruth.ROOT).as_posix() == (
        "evaluation/runs/groundtruth.db"
    )
    ignored = (groundtruth.ROOT / ".gitignore").read_text().splitlines()
    assert "evaluation/runs/" in ignored
    monkeypatch.setenv("FLOWGUARD_GROUNDTRUTH_DB", str(tmp_path / "x.db"))
    assert groundtruth.GroundTruthDB.open().path == tmp_path / "x.db"
