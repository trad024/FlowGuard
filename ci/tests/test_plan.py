"""CI planning retains baseline eligibility, fixed-order fallbacks and mandatory jobs."""

import json

import pytest

import plan

CATALOG = plan.load_catalog()
ORDER = ["lint", "integration", "sbom", "multiarch"]


def test_catalog_has_the_four_optional_jobs_of_d2_and_nothing_mandatory():
    assert CATALOG["order"] == ORDER
    assert not {"build", "unit", "scans", "trivy", "gitleaks", "push"} & set(CATALOG["jobs"])


def test_unknown_security_uses_baseline_without_extra_jobs():
    p = plan.make_plan("optimizer", ["docs/x.md"], CATALOG, plan.MODEL_FILE)
    assert (p.mode, p.model, p.run) == ("baseline", None, [])
    assert p.skipped == plan.baseline_plan(CATALOG, ["docs/x.md"]).skipped
    assert p.warnings == ["previous security status unknown; used baseline order and filters"]


@pytest.mark.parametrize(
    "content",
    ["not json", json.dumps({"policy": {"type": "lightgbm-from-the-future"}}), "{}"],
)
def test_a_broken_or_unknown_model_falls_back_to_baseline(tmp_path, content):
    model = tmp_path / "model.json"
    model.write_text(content)
    p = plan.make_plan(
        "optimizer", ["app/podinfo/main.go"], CATALOG, model, security_status="clean"
    )
    assert p.run == ["lint", "integration"]
    assert p.warnings


def test_a_missing_model_falls_back_to_baseline(tmp_path):
    p = plan.make_plan("optimizer", [], CATALOG, tmp_path / "absent.json", security_status="clean")
    assert p.run == [] and p.warnings


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


@pytest.mark.parametrize(
    ("explicit", "branch", "expected"),
    [
        (None, "scenario/baseline/check-1", ("baseline", "branch")),
        ("", "scenario/optimizer/run-7", ("optimizer", "branch")),
        (None, "dev", ("optimizer", "default")),
        (None, "scenario/step3-20261025", ("optimizer", "default")),
        (None, "scenario/baselines/x", ("optimizer", "default")),  # prefix must match exactly
        (None, None, ("optimizer", "default")),
        # The manual input or repo variable wins over the branch name
        ("optimizer", "scenario/baseline/check-1", ("optimizer", "input")),
        ("baseline", "dev", ("baseline", "input")),
    ],
)
def test_mode_comes_from_input_then_branch_then_default(explicit, branch, expected):
    assert plan.resolve_mode(explicit, branch) == expected


def test_an_unknown_explicit_mode_is_an_error():
    with pytest.raises(ValueError):
        plan.resolve_mode("yolo", "dev")


def test_cli_applies_the_branch_rule_and_records_its_source(tmp_path, capsys):
    changed = tmp_path / "changed.txt"
    changed.write_text("app/ci-trigger.txt\n")
    assert (
        plan.main(
            ["--mode", "", "--branch", "scenario/baseline/x", "--changed-files", str(changed)]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert (result["mode"], result["mode_source"], result["run"]) == ("baseline", "branch", [])
