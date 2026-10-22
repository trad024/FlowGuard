"""CI plan job: the stub model runs everything, the baseline filters by path in fixed order, and
no failure of the model can skip a job."""

import json

import pytest

import plan

CATALOG = plan.load_catalog()
ORDER = ["lint", "integration", "sbom", "multiarch"]


def test_catalog_has_the_four_optional_jobs_of_d2_and_nothing_mandatory():
    assert CATALOG["order"] == ORDER
    assert not {"build", "unit", "scans", "trivy", "gitleaks", "push"} & set(CATALOG["jobs"])


def test_committed_stub_model_runs_every_optional_job_in_order():
    p = plan.make_plan("optimizer", ["docs/x.md"], CATALOG, plan.MODEL_FILE)
    assert (p.model, p.run, p.warnings) == ("stub-run-all", ORDER, [])


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"policy": {"type": "lightgbm-from-the-future"}}), "{}"],
)
def test_a_broken_or_unknown_model_falls_back_to_running_everything(tmp_path, content):
    model = tmp_path / "model.json"
    model.write_text(content)
    p = plan.make_plan("optimizer", ["app/podinfo/main.go"], CATALOG, model)
    assert p.run == ORDER
    assert p.warnings


def test_a_missing_model_falls_back_to_running_everything(tmp_path):
    p = plan.make_plan("optimizer", [], CATALOG, tmp_path / "absent.json")
    assert p.run == ORDER and p.warnings


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (["app/podinfo/pkg/api/http/info.go"], ["lint", "integration"]),
        (["app/podinfo/go.sum"], ["integration", "sbom", "multiarch"]),
        (["app/podinfo/Dockerfile.xx"], ["multiarch"]),
        (["app/podinfo/ui/vue.html"], ["integration"]),
        (["app/podinfo/README.md"], []),
        (["app/podinfo/Dockerfile", "app/podinfo/cmd/podinfo/main.go"], ORDER),
    ],
)
def test_baseline_keeps_the_fixed_order_and_applies_path_filters(changed, expected):
    p = plan.make_plan("baseline", changed, CATALOG, plan.MODEL_FILE)
    assert p.run == expected
    assert set(p.run) | set(p.skipped) == set(ORDER)


def test_baseline_runs_everything_when_the_changes_are_unknown():
    assert plan.make_plan("baseline", None, CATALOG, plan.MODEL_FILE).run == ORDER


def test_unknown_mode_is_an_error():
    with pytest.raises(ValueError):
        plan.make_plan("yolo", [], CATALOG, plan.MODEL_FILE)


def test_catalog_order_must_list_every_job_once(tmp_path):
    bad = tmp_path / "jobs.json"
    bad.write_text(json.dumps({"order": ["lint", "lint"], "jobs": {"lint": {"paths": []}}}))
    with pytest.raises(ValueError):
        plan.load_catalog(bad)


def test_cli_writes_github_outputs(tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("app/podinfo/go.mod\n\n")
    out = tmp_path / "github_output"
    assert (
        plan.main(
            ["--mode", "baseline", "--changed-files", str(changed), "--github-output", str(out)]
        )
        == 0
    )
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert json.loads(lines["optional"]) == ["lint", "integration", "sbom", "multiarch"]
    assert json.loads(lines["plan"])["mode"] == "baseline"
    assert json.loads(capsys.readouterr().out)["skipped"] == {}
