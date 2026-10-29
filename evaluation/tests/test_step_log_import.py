"""Importing the side logger's Rollout steps (state/rollout-steps.jsonl) into recordings' step
logs (D54): poll times, step kinds from the repo's canary steps, a pause start only where the
logger saw it begin, aborts, foreign rows ignored, an existing step log kept, the replay using
the imported times."""

import json
from pathlib import Path

from test_release_replay import STEP_20, T0, iso, write_recording

import release_replay as rr
import step_log_import as imp


def rows_at(*points, **common):
    """Side-logger rows: (seconds into the recording, step index[, extra fields])."""
    out = []
    for s, i, *extra in points:
        row = {"ts": iso(T0 + s), "namespace": "staging", "canary_hash": "new", "step_index": i}
        out.append({**row, **common, **(extra[0] if extra else {})})
    return out


def run_of(root, rid):
    (run,) = [r for r in rr.release_runs([root]) if r["id"] == rid]
    return run


def step_log(run):
    path = Path(run["recording"]) / "rollout-events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_side_logger_rows_become_a_poll_step_log_the_replay_uses(tmp_path):
    write_recording(tmp_path, "r-good", bad=False)
    rows = rows_at((52, 0), (57, 1), (90, 2), (205, 3), (209, 4), (360, 6))
    rows.insert(0, {"ts": iso(T0 + 10), "namespace": "staging", "canary_hash": "old",
                    "step_index": 7})  # the previous rollout, done  # fmt: skip
    run = run_of(tmp_path, "r-good")
    assert imp.import_into(run, rows) == "7 rows (l0 steps)"
    logged = step_log(run)
    assert {r["time_source"] for r in logged} == {"poll"}
    assert [(r["canary_hash"], r["step_index"], r["step"], r["set_weight"]) for r in logged] == [
        ("old", 7, None, 100),  # every step done
        ("new", 0, "setWeight", 20),
        ("new", 1, "pause", 20),
        ("new", 2, "analysis", 20),  # the L0 Rollout's steps
        ("new", 3, "setWeight", 50),
        ("new", 4, "pause", 50),
        ("new", 6, "setWeight", 100),
    ]
    assert [r["pause_start"] for r in logged if r["step"] == "pause"] == [iso(T0 + 57),
                                                                         iso(T0 + 209)]  # fmt: skip
    line = rr.run_timeline(run, rr.load_index(run))
    assert line["source"] == "poll"  # up to 5 s late, not Argo's own times
    assert {w: t - T0 for w, t in line["steps"].items()} == {20: 57, 50: 209}
    assert (line["end"] - T0, line["how"]) == (360, "promoted")
    result = rr.score([run])
    assert result["step_source"] == {"poll": 1} and result["step_source_fallback"] == []


def test_a_logger_started_mid_canary_gives_no_step_start_and_the_estimate_is_used(tmp_path):
    write_recording(tmp_path, "r-good", bad=False)
    run = run_of(tmp_path, "r-good")
    imp.import_into(run, rows_at((100, 2)))  # 20% was never seen being reached
    assert [r["pause_start"] for r in step_log(run)] == [None]
    line = rr.run_timeline(run, rr.load_index(run))
    assert line["source"] == "pods_up_estimate"
    assert line["steps"][20] - T0 == STEP_20 - rr.READY_LAG_S
    assert rr.score([run])["step_source_fallback"] == ["r-good"]


def test_full_rows_aborts_agents_steps_and_foreign_rows(tmp_path):
    write_recording(tmp_path, "r-bad", bad=True, l0="aborted", abort_at=70, errors=0.1)
    truth = tmp_path / "r-bad" / "ground_truth.json"
    data = json.loads(truth.read_text())
    data["observed"] = {"mode": "agents", "canary": "aborted", "l0": "offline"}
    truth.write_text(json.dumps(data))
    full = {"rollout": "podinfo", "stableRS": "old", "paused": True, "phase": "Paused"}
    rows = rows_at(
        (50, 0, {"setWeight": 20, "paused": False, "phase": "Progressing"}),
        (56, 1, {"setWeight": 20}),
        (88, 2, {"setWeight": 20}),
        (131, 2, {"setWeight": 20, "abort": True, "phase": "Degraded"}),
        (136, 2, {"setWeight": 20, "abort": True, "phase": "Degraded"}),
        **full,
    )
    rows += rows_at((60, 1), namespace="prod")  # another namespace
    rows += rows_at((60, 1), rollout="other")  # another Rollout
    rows += rows_at((900, 1))  # outside the recording's window
    run = run_of(tmp_path, "r-bad")
    assert imp.import_into(run, rows) == "4 rows (agents steps)"
    logged = step_log(run)
    assert [r["changes"] for r in logged] == [
        ["step", "revision", "phase"],
        ["step", "pause_start", "phase"],
        ["step", "pause_start"],  # agents mode: index 2 is the decision pause
        ["abort", "phase"],
    ]
    assert logged[2]["step"] == "pause" and logged[-1]["aborted_at"] == iso(T0 + 131)
    assert logged[0]["stable_hash"] == "old"
    line = rr.run_timeline(run, rr.load_index(run))
    assert line["source"] == "poll" and line["steps"][20] - T0 == 56
    assert (line["end"] - T0, line["how"]) == (131, "aborted")


def test_an_existing_step_log_is_kept_unless_forced_and_the_command_reports(tmp_path, capsys):
    write_recording(tmp_path, "r-argo", bad=False, steps=[{"ts": iso(T0), "time_source": "argo"}])
    write_recording(tmp_path, "r-none", bad=False)
    run = run_of(tmp_path, "r-argo")
    rows = rows_at((52, 0), (57, 1))
    assert imp.import_into(run, rows) == "kept its own step log"
    assert step_log(run) == [{"ts": iso(T0), "time_source": "argo"}]
    assert imp.import_into(run, rows, force=True) == "2 rows (l0 steps)"
    side = tmp_path / "rollout-steps.jsonl"
    side.write_text("".join(json.dumps(r) + "\n" for r in rows_at((52, 0))))
    assert imp.main([str(side), str(tmp_path)]) == 0
    report = {
        r["recording"]: r["step_log"]
        for r in map(json.loads, capsys.readouterr().out.split("\n")[:-1])
    }
    assert report == {"r-argo": "kept its own step log", "r-none": "1 rows (l0 steps)"}
