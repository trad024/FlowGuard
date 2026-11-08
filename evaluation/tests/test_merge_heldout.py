"""R5: the held-out outputs merged into the report's one audit log, truth DB and replay file.
Synthetic outputs of all three kinds, made by the real runners (diagnose_heldout with a fake
client, dso6_run on generator items with a dev seed, a scenario truth DB with replay records),
merged, then read by report.py and report_scope. Refusals on id collisions and on mixed dev or
synthetic ids. No network, no real seed, nothing from scenarios/heldout/."""

import json

import pytest
import test_diagnose_heldout as tdh
import yaml

import diagnose_heldout as dh
import dso6_run
import groundtruth
import heldout
import heldout_guard
import merge_heldout as mh
import report
import report_scope
from flowguard.audit import AuditLog


@pytest.fixture
def outputs(tmp_path, fitted_levels):
    """A Diagnose folder, a DSO6 folder and a scenarios source, as the runners write them."""
    tdh.FakeClient.cache, tdh.FakeClient.sent = {}, []
    log = tmp_path / "x.log"
    log.write_text(tdh.LOG, encoding="utf-8")
    items = [
        {"id": "code-06", "set": "corpus", "template": "code-06", "log": log,
         "truth": {"root_cause": "code"}},
        {"id": "lc-abcdef123456", "set": "logchunks", "template": None, "log": log,
         "truth": {"localizable": True, "relevant_lines": [2]}},
    ]  # fmt: skip
    diag = tmp_path / "diagnose"
    diag.mkdir()
    for level in ("L2", "L3"):
        dh.run_llm(level, items, diag, tdh.LANES, tdh.FakeClient, set(), tdh.Retriever(),
                   dh.Progress(lambda s: None))  # fmt: skip
    dh.record_truth(groundtruth.GroundTruthDB(diag / "groundtruth.db"), items)
    prereg = yaml.safe_load(heldout.PREREG.read_text(encoding="utf-8"))
    prereg["dso6_adversarial_set"]["count"] = 20
    dso6 = tmp_path / "dso6"
    dso6_run.run(heldout.dso6_set(prereg, "dev-r5-20261108"), dso6)
    scen = tmp_path / "scenarios"
    scen.mkdir()
    truth = groundtruth.GroundTruthDB(scen / "groundtruth.db")
    truth.record_run("r-1", spec={"id": "h-0000000001", "family": "release"},
                     ground_truth={"bad_release": False}, observed={}, l0_correct=None)  # fmt: skip
    AuditLog(scen / "audit.db")
    records = scen / "dso3-records.jsonl"
    records.write_text(
        json.dumps(
            {
                "scenario_id": "r-1",
                "level": "L3",
                "observed": {"release_outcome": "promoted", "caught_before_full": False},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sources = [
        mh.parse_source(f"diagnose:{diag}"),
        mh.parse_source(f"dso6:{dso6}"),
        mh.parse_source(f"scenarios:audit={scen / 'audit.db'},truth={scen / 'groundtruth.db'},"
                        f"replays={records}"),
    ]  # fmt: skip
    return {"sources": sources, "out": tmp_path / "merged", "tmp": tmp_path}


def test_folder_sources_take_every_replay_file_but_raw_and_item_lists(outputs):
    diag, dso6, _ = outputs["sources"]
    assert sorted(p.name for p in diag["replays"]) == ["L2.jsonl", "L3.jsonl"]
    assert [p.name for p in dso6["replays"]] == ["records.jsonl"] and dso6["audit"]


def test_the_merge_feeds_report_and_report_scope_end_to_end(outputs):
    counts = mh.merge(outputs["sources"], outputs["out"])
    out = outputs["out"]
    assert counts["truth_runs"] == 2 + 20 + 1
    assert counts["replay_records"] == 4 + 20 + 1
    result_dir = outputs["tmp"] / "report"
    report.main(["--audit", str(out / "audit.db"), "--truth", str(out / "groundtruth.db"),
                 "--replays", str(out / "replay.jsonl"), "--out", str(result_dir),
                 "--no-plots"])  # fmt: skip
    results = json.loads((result_dir / "results.json").read_text())
    assert results["metadata"]["confirmatory"] is False  # no frozen state in a test
    reason = report_scope.confirmatory_scope({"status": "frozen"}, out / "groundtruth.db")
    assert isinstance(reason, str) and reason  # a reason, never an error
    assert report_scope.labels(out / "groundtruth.db") == {"synthetic": False,
                                                           "development": False}  # fmt: skip


def test_an_id_collision_refuses(outputs):
    diag = outputs["sources"][0]
    with pytest.raises(mh.Refused, match="truth run code-06 in diagnose and again"):
        mh.merge([diag, {**diag, "name": "again"}], outputs["out"])
    assert not outputs["out"].exists()  # nothing written


def test_a_dev_or_synthetic_id_refuses(outputs, tmp_path):
    for sid in (
        "code-01",
        "synthetic-ci-0",
        "normal-traffic",
    ):  # dev variant, synthetic, dev window
        truth = tmp_path / f"{sid}.db"
        groundtruth.GroundTruthDB(truth).record_run(
            f"r-{sid}", spec={"id": sid, "family": "ci"}, ground_truth={}, observed={},
            l0_correct=None)  # fmt: skip
        src = {"name": "dev", "audit": None, "truth": truth, "replays": []}
        with pytest.raises(mh.Refused, match=f"development or synthetic id .*{sid}"):
            mh.merge([*outputs["sources"], src], tmp_path / f"out-{sid}")


def test_a_replay_record_without_a_truth_run_or_twice_refuses(outputs, tmp_path):
    orphan = tmp_path / "orphan.jsonl"
    orphan.write_text(json.dumps({"scenario_id": "nowhere", "level": "L3", "observed": {}}) + "\n")
    src = {"name": "x", "audit": None, "truth": None, "replays": [orphan]}
    with pytest.raises(mh.Refused, match="without a truth run: nowhere"):
        mh.merge([*outputs["sources"], src], tmp_path / "o1")
    twice = {**outputs["sources"][2], "name": "scenarios", "truth": None}
    with pytest.raises(mh.Refused, match="duplicate replay record"):
        mh.merge([*outputs["sources"], twice], tmp_path / "o2")


def test_the_merge_writes_only_into_an_empty_folder(outputs):
    outputs["out"].mkdir()
    (outputs["out"] / "replay.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(mh.Refused, match="merge into an empty folder"):
        mh.merge(outputs["sources"], outputs["out"])


def test_the_merge_is_gated(tmp_path, monkeypatch):
    called = []
    for name in ("frozen_agents", "diagnose_frozen", "checked_seed"):
        monkeypatch.setattr(heldout_guard.heldout, name, lambda *a, n=name, **k: called.append(n))
    with pytest.raises(SystemExit, match="--confirm-step-11"):
        mh.main(["--out", str(tmp_path), "--source", f"x:{tmp_path}"])
    assert called == []


def test_the_held_out_normal_window_has_its_own_id():
    assert heldout.NORMAL_TRAFFIC_ID == "h-normal-traffic"
    assert heldout.NORMAL_TRAFFIC_ID not in report_scope.dev_spec_ids()
