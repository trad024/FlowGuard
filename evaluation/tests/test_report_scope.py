"""A report is confirmatory only for proven held-out inputs (report_scope), never by status alone.

Everything lives in a temporary git repository with a fake seed, its hash, a run order and a
truth database: the real seed, scenarios/heldout/ and groundtruth.db are never touched."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import groundtruth  # noqa: E402
import heldout  # noqa: E402
import report_scope  # noqa: E402

FAKE_SEED = "not-the-real-seed-0123456789"
HELD_OUT = ["h-0000000001", "h-0000000002", "h-0000000003"]
FROZEN = {"status": "frozen"}


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A repository that passes all four checks."""
    root = tmp_path / "repo"
    (root / "agents").mkdir(parents=True)
    (root / "agents" / "agent.py").write_text("x = 1\n")
    (root / "scenarios" / "heldout").mkdir(parents=True)
    (root / "scenarios" / "dev").mkdir()
    (root / "scenarios" / "dev" / "d-dev01.yaml").write_text("id: d-dev01\nfamily: release\n")
    (root / ".gitignore").write_text(".env\nscenarios/heldout/\n")
    (root / ".env").write_text(f"{heldout.SEED_VAR}={FAKE_SEED}\n")
    (root / "scenarios" / "heldout-seed.sha256").write_text(heldout.seed_hash(FAKE_SEED) + "\n")
    (root / "scenarios" / "heldout" / "run-order.txt").write_text("\n".join(HELD_OUT) + "\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "agents")
    git(root, "tag", heldout.FREEZE_TAG)
    return root


def truth(tmp_path, ids, name="truth.db"):
    path = tmp_path / name
    db = groundtruth.GroundTruthDB(path)
    for i, sid in enumerate(ids):
        db.record_run(f"r-{i}", spec={"id": sid, "family": "release"}, ground_truth={},
                      observed={}, l0_correct=None)  # fmt: skip
    return path


def test_proven_held_out_inputs_are_confirmatory(repo, tmp_path):
    assert report_scope.confirmatory_scope(FROZEN, truth(tmp_path, HELD_OUT), repo) is None
    assert report_scope.labels(truth(tmp_path, HELD_OUT, "t2.db"), root=repo) == {
        "synthetic": False,
        "development": False,
    }


def test_check_1_a_draft_is_never_confirmatory(repo, tmp_path):
    reason = report_scope.confirmatory_scope({"status": "draft"}, truth(tmp_path, HELD_OUT), repo)
    assert reason == "pre-registration is not frozen"


def test_check_2_without_the_eval_freeze_tag_or_with_agents_changed(repo, tmp_path):
    t = truth(tmp_path, HELD_OUT)
    (repo / "agents" / "agent.py").write_text("x = 2\n")
    assert report_scope.confirmatory_scope(FROZEN, t, repo) == "agents/ changed since eval-freeze"
    git(repo, "tag", "-d", heldout.FREEZE_TAG)
    assert report_scope.confirmatory_scope(FROZEN, t, repo) == "eval-freeze tag missing"


def test_check_3_without_the_run_order_or_with_a_seed_that_does_not_match(repo, tmp_path):
    t = truth(tmp_path, HELD_OUT)
    (repo / ".env").write_text(f"{heldout.SEED_VAR}=another-seed\n")
    reason = report_scope.confirmatory_scope(FROZEN, t, repo)
    assert reason.startswith("held-out seed check failed") and "another-seed" not in reason
    (repo / "scenarios" / "heldout" / "run-order.txt").unlink()
    assert report_scope.confirmatory_scope(FROZEN, t, repo).startswith("held-out run order missing")


def test_check_4_an_id_outside_the_run_order_is_never_confirmatory(repo, tmp_path):
    synthetic = truth(tmp_path, ["synthetic-ci-0"], "s.db")
    assert "(synthetic): synthetic-ci-0" in report_scope.confirmatory_scope(FROZEN, synthetic, repo)


def test_one_dev_id_among_held_out_ids_is_not_confirmatory(repo, tmp_path):
    mixed = truth(tmp_path, [*HELD_OUT, "d-dev01"])
    reason = report_scope.confirmatory_scope(FROZEN, mixed, repo)
    assert reason == "scored id not in the held-out run order (development): d-dev01"
    assert report_scope.labels(mixed, root=repo)["development"] is True


def test_the_synthetic_label_comes_from_the_fixture_marker(tmp_path):
    folder = tmp_path / "inputs"
    folder.mkdir()
    t = truth(folder, ["synthetic-ci-0"])
    assert report_scope.labels(t)["synthetic"] is False  # no marker, no label
    (folder / report_scope.SYNTHETIC_MARKER[0]).write_text(report_scope.SYNTHETIC_MARKER[1])
    assert report_scope.labels(t)["synthetic"] is True


def test_the_real_repository_is_not_confirmatory_today(tmp_path):
    """The repository's own state: no eval-freeze tag yet, whatever the pre-registration says."""
    reason = report_scope.confirmatory_scope(FROZEN, truth(tmp_path, HELD_OUT))
    assert reason == "eval-freeze tag missing"


# ---------------------------------------------------------------- R2: held-out Diagnose scope


def diagnose_repo(root):
    """Add the held-out Diagnose inputs to the repository, hash them, commit, tag stage A."""
    import json as _json

    split = {"templates": {"t-test": "test", "t-train": "train"},
             "variants": {"a": {"template": "t-test", "root_cause": "code"},
                          "b": {"template": "t-train", "root_cause": "code"}}}  # fmt: skip
    (root / heldout.SPLIT).parent.mkdir(parents=True, exist_ok=True)
    (root / heldout.SPLIT).write_text(_json.dumps(split), encoding="utf-8")
    for v in ("a", "b"):
        (root / heldout.CORPUS / v).mkdir(parents=True)
        (root / heldout.CORPUS / v / "run.log").write_text(f"log {v}\n", encoding="utf-8")
    raw = root / heldout.LOGCHUNKS
    (raw / "logs" / "r/x/failed").mkdir(parents=True)
    (raw / "logs" / "r/x/failed/1.log").write_text("log one\n", encoding="utf-8")
    (raw / "build-failure-reason").mkdir(parents=True)
    (raw / "build-failure-reason" / "r.xml").write_text("<Examples/>", encoding="utf-8")
    (root / heldout.H3_SAMPLE).write_text(
        "rank,log_path,status,target_lines,visible_target_lines,l2_request_chars,sha256\n"
        "1,r/x/failed/1.log,used,1,1,10,abcdef1234567890\n",
        encoding="utf-8",
    )
    with open(root / ".gitignore", "a", encoding="utf-8") as f:
        f.write("evaluation/runs/\nevaluation/datasets/raw/\n")
    heldout.freeze_harness_inputs(root)
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "harness")
    git(root, "tag", heldout.HARNESS_DIAGNOSE_TAG)
    return root


def diagnose_truth(tmp_path, rows, name="dtruth.db"):
    path = tmp_path / name
    db = groundtruth.GroundTruthDB(path)
    for sid, family in rows:
        db.record_run(sid, spec={"id": sid, "family": family}, ground_truth={}, observed={},
                      l0_correct=None)  # fmt: skip
    return path


def test_the_held_out_diagnose_set_comes_from_the_hashed_inputs(repo):
    root = diagnose_repo(repo)
    assert report_scope.heldout_diagnose_ids(root) == {"a", "lc-abcdef123456"}


def test_a_valid_held_out_diagnose_set_is_confirmatory(repo, tmp_path):
    root = diagnose_repo(repo)
    t = diagnose_truth(tmp_path, [("a", "diagnose"), ("lc-abcdef123456", "diagnose")])
    assert report_scope.confirmatory_scope(FROZEN, t, root) is None


def test_one_dev_diagnose_id_refuses(repo, tmp_path):
    root = diagnose_repo(repo)
    t = diagnose_truth(tmp_path, [("a", "diagnose"), ("b", "diagnose")])
    reason = report_scope.confirmatory_scope(FROZEN, t, root)
    assert reason == "scored Diagnose id not in the held-out Diagnose set (development): b"


def test_a_changed_input_list_refuses(repo, tmp_path):
    root = diagnose_repo(repo)
    (root / heldout.CORPUS / "a" / "run.log").write_text("edited\n", encoding="utf-8")
    t = diagnose_truth(tmp_path, [("a", "diagnose")])
    reason = report_scope.confirmatory_scope(FROZEN, t, root)
    assert reason.startswith("held-out Diagnose harness not frozen") and "inputs changed" in reason


def test_without_the_harness_tag_diagnose_results_are_not_confirmatory(repo, tmp_path):
    root = diagnose_repo(repo)
    git(root, "tag", "-d", heldout.HARNESS_DIAGNOSE_TAG)
    t = diagnose_truth(tmp_path, [("a", "diagnose")])
    assert "harness-diagnose-freeze" in report_scope.confirmatory_scope(FROZEN, t, root)
    assert report_scope.confirmatory_scope({"status": "draft"}, t, root) == (
        "pre-registration is not frozen"
    )


def test_diagnose_scenario_and_dso6_ids_together(repo, tmp_path):
    root = diagnose_repo(repo)
    (root / "scenarios" / "heldout" / "dso6-items.yaml").write_text(
        "- {id: g-0000000001, category: prohibited_action}\n", encoding="utf-8"
    )
    rows = [("a", "diagnose"), (HELD_OUT[0], "release"), ("g-0000000001", "dso6")]
    assert report_scope.confirmatory_scope(FROZEN, diagnose_truth(tmp_path, rows), root) is None
    rows.append(("g-9999999999", "dso6"))  # not a generated item
    reason = report_scope.confirmatory_scope(FROZEN, diagnose_truth(tmp_path, rows, "x.db"), root)
    assert reason == "scored id not in the held-out run order (unknown): g-9999999999"


def test_the_held_out_normal_window_is_accepted_beside_the_run_order(repo, tmp_path):
    rows = [(HELD_OUT[0], "release"), (heldout.NORMAL_TRAFFIC_ID, "normal")]
    assert report_scope.confirmatory_scope(FROZEN, diagnose_truth(tmp_path, rows), repo) is None
    dev_window = [(HELD_OUT[0], "release"), ("normal-traffic", "normal")]
    reason = report_scope.confirmatory_scope(
        FROZEN, diagnose_truth(tmp_path, dev_window, "d.db"), repo
    )
    assert reason == "scored id not in the held-out run order (development): normal-traffic"
