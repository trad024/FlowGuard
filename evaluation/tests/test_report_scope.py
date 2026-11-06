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
