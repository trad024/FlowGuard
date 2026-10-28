"""Model portability, exact SHAP, safe planning and chronological data boundaries."""

import copy
import json
import math
from pathlib import Path

import pytest

import plan
from collect_optimizer import collect
from optimizer.features import FEATURES, JOBS, dependencies_changed, extract, parse_numstat
from optimizer.history import observations, split_runs
from optimizer.policy import apply
from optimizer.portable import explain, predict, raw, validate
from optimizer.security_status import previous_status

CATALOG = plan.load_catalog()


@pytest.fixture
def model():
    return json.loads(plan.MODEL_FILE.read_text(encoding="utf-8"))


def numeric_model():
    return {
        "format": 2,
        "model": "test",
        "features": list(FEATURES),
        "policy": {
            "type": "lightgbm-json-v1",
            "skip_threshold": 0.01,
            "support": {j: {"observed": 20, "failures": 0} for j in JOBS},
        },
        "duration": {"objective": "regression", "trees": [{"value": 10.0}]},
        "failure": {"objective": "binary", "trees": [{"value": -10.0}]},
        "background": [[0.0] * len(FEATURES)],
        "bounds": {"min": [0.0] * len(FEATURES), "max": [100.0] * len(FEATURES)},
    }


def test_commit_features_and_binary_diff_stats():
    paths = ["app/podinfo/a_test.go", "app/podinfo/go.mod", "docs/x.md"]
    stats = parse_numstat(
        "10\t2\tapp/podinfo/a_test.go\n3\t0\tapp/podinfo/go.mod\n-\t-\tdocs/x.md\n"
    )
    values = dict(zip(FEATURES, extract(paths, "build", stats), strict=True))
    assert values["files"] == 3 and values["go_files"] == 1 and values["test_files"] == 1
    assert values["kind_dependency"] == 1 and values["dependency_files"] == 1
    assert values["size_known"] == 0 and values["additions"] == 0
    assert values["job_build"] == 1 and values["job_scans"] == 0
    assert extract(paths, "build", stats) == extract(list(reversed(paths)), "build", stats)


@pytest.mark.parametrize(
    "path",
    [
        "go.mod",
        "x/go.sum",
        "ui/package-lock.json",
        "requirements-dev.txt",
        "uv.lock",
        "x\\requirements.txt",
    ],
)
def test_dependency_file_recognition(path):
    assert dependencies_changed([path])


def test_committed_schema_and_all_predictions_and_explanations(model):
    validate(model)
    row = extract(["app/podinfo/main.go"], "unit")
    duration, probability = predict(model, row)
    assert duration >= 0 and 0 <= probability <= 1
    for kind in ("duration", "failure"):
        explanation = explain(model[kind], row, model["background"])
        assert math.isclose(
            explanation["base_value"] + sum(explanation["values"].values()),
            raw(model[kind], row),
            abs_tol=1e-8,
        )
        assert len(explanation["top_features"]) == 3


def test_exact_tree_shap_has_analytically_known_interaction_values():
    left = {"value": 2.0}
    right = {"feature": 1, "threshold": 0.5, "left": {"value": 4.0}, "right": {"value": 8.0}}
    forest = {
        "objective": "regression",
        "trees": [{"feature": 0, "threshold": 0.5, "left": left, "right": right}],
    }
    a, b, row = [[0.0] * len(FEATURES) for _ in range(3)]
    b[0] = row[0] = row[1] = 1
    result = explain(forest, row, [a, b])
    assert result["base_value"] == 3
    assert result["values"][FEATURES[0]] == 2
    assert result["values"][FEATURES[1]] == 3


@pytest.mark.parametrize(
    "changed,status", [(["app/podinfo/go.mod"], "clean"), (["docs/x.md"], "flagged")]
)
def test_p2_overrides_even_a_model_that_wants_to_skip_everything(changed, status):
    model = numeric_model()
    stats = {p: {"additions": 1, "deletions": 0} for p in changed}
    run, skipped, _, _ = apply(model, CATALOG, changed, stats, status, explanations=False)
    assert "sbom" in run and "sbom" not in skipped
    assert not {"build", "unit", "scans", "scan"} & set(skipped)


def test_predictive_skipping_needs_size_support_and_no_historical_failures():
    model = numeric_model()
    paths = ["app/podinfo/main.go"]
    stats = {paths[0]: {"additions": 1, "deletions": 0}}
    _, skipped, _, _ = apply(model, CATALOG, paths, stats, "clean", explanations=False)
    assert "lint" in skipped
    model["policy"]["support"]["lint"]["failures"] = 1
    run, _, _, _ = apply(model, CATALOG, paths, stats, "clean", explanations=False)
    assert "lint" in run
    model["policy"]["support"]["lint"]["failures"] = 0
    run, _, _, _ = apply(model, CATALOG, paths, None, "clean", explanations=False)
    assert "lint" in run
    model["bounds"]["max"][0] = 0
    run, _, _, _ = apply(model, CATALOG, paths, stats, "clean", explanations=False)
    assert "lint" in run


def test_unknown_changes_and_security_run_everything(model):
    for changed, status in ((None, "clean"), ([], "unknown")):
        run, skipped, _, warnings = apply(model, CATALOG, changed, security_status=status)
        assert run == CATALOG["order"] and not skipped and warnings


def test_mandatory_jobs_cannot_become_optional():
    catalog = {"order": ["build"], "jobs": {"build": {"paths": []}}}
    with pytest.raises(ValueError, match="optional job catalog"):
        apply(numeric_model(), catalog, [], security_status="clean")


def test_reorders_by_failure_probability_per_second_without_skipping():
    model = numeric_model()
    model["failure"]["trees"] = [{"value": 0.0}]
    model["duration"]["trees"] = [
        {
            "feature": FEATURES.index("job_lint"),
            "threshold": 0.5,
            "left": {"value": 1.0},
            "right": {"value": 100.0},
        }
    ]
    run, _, _, _ = apply(model, CATALOG, ["app/podinfo/main.go"], security_status="clean")
    assert run == ["integration", "lint"]


@pytest.mark.parametrize(
    "mutation",
    ["nan", "leaf", "feature", "threshold", "background", "support", "deep", "objective"],
)
def test_bad_models_always_fall_back_to_all_jobs(tmp_path, mutation):
    model = numeric_model()
    if mutation == "nan":
        model["duration"]["trees"][0]["value"] = float("nan")
    elif mutation == "leaf":
        model["failure"]["trees"][0] = {"value": "0"}
    elif mutation == "feature":
        model["features"] = ["wrong"]
    elif mutation == "threshold":
        model["policy"]["skip_threshold"] = 1.0
    elif mutation == "background":
        model["background"] = []
    elif mutation == "support":
        model["policy"]["support"] = {}
    elif mutation == "objective":
        model["failure"]["objective"] = "unknown"
    else:
        node = {"value": 0}
        for _ in range(4):
            node = {"feature": 0, "threshold": 0, "left": node, "right": {"value": 1}}
        model["duration"]["trees"] = [node]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(model))
    p = plan.make_plan("optimizer", [], CATALOG, path, security_status="clean")
    assert p.run == CATALOG["order"] and not p.skipped and p.warnings


def test_legacy_run_all_model_remains_supported(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"model": "old", "policy": {"type": "run-all"}}))
    p = plan.make_plan("optimizer", [], CATALOG, path)
    assert p.run == CATALOG["order"] and not p.warnings


def test_previous_security_status_uses_annotation_without_network():
    def get(path):
        if "workflows/" in path:
            return {
                "workflow_runs": [
                    {"id": 20, "head_branch": "dev", "head_sha": "current", "status": "completed"},
                    {"id": 10, "head_branch": "dev", "head_sha": "previous", "status": "completed"},
                ]
            }
        if "/jobs?" in path:
            assert "/10/" in path
            return {"jobs": [{"id": 3, "name": "scans", "conclusion": "success"}]}
        return [{"title": "scan-counts", "message": "trivy_high=1 trivy_critical=0 gitleaks=0"}]

    assert previous_status("o/r", "dev", "current", get) == "flagged"
    assert previous_status("o/r", "other", "current", get) == "unknown"
    assert previous_status("invalid", "dev", "current", get) == "unknown"

    def denied(path):
        raise OSError("denied")

    assert previous_status("o/r", "dev", "current", denied) == "unknown"


def test_grouped_time_ordered_split_and_unobserved_jobs_are_excluded():
    source = Path(__file__).resolve().parents[1] / "optimizer" / "dev-history.jsonl"
    history = [json.loads(line) for line in source.read_text().splitlines()]
    train, validation = split_runs(history + [copy.deepcopy(history[0])])
    assert max(r["created_at"] for r in train) <= min(r["created_at"] for r in validation)
    assert not {r["sha"] for r in train} & {r["sha"] for r in validation}
    run = copy.deepcopy(history[0])
    for job in run["jobs"]:
        job["conclusion"] = "skipped"
    assert observations([run]) == []


def test_collector_ignores_unapproved_branch_names_and_never_writes():
    def get(path):
        assert "workflows/" in path
        return {"workflow_runs": [{"status": "completed", "head_branch": "unapproved"}]}

    assert collect("o/r", get) == []
