"""Train two small LightGBM models with a commit-grouped, chronological dev split."""

import argparse
import hashlib
import json
from pathlib import Path

import lightgbm as lgb
import numpy as np
import shap
from sklearn.metrics import mean_absolute_error, roc_auc_score

import plan
from optimizer.failfast import evaluate as failfast_evaluate
from optimizer.features import FEATURES, JOBS
from optimizer.history import observations, split_runs
from optimizer.policy import apply
from optimizer.portable import explain, predict, validate


def export_node(node):
    if "leaf_value" in node:
        return {"value": float(node["leaf_value"])}
    if node["decision_type"] != "<=":
        raise ValueError("only numeric LightGBM splits are portable")
    return {
        "feature": node["split_feature"],
        "threshold": float(node["threshold"]),
        "left": export_node(node["left_child"]),
        "right": export_node(node["right_child"]),
    }


def export(estimator, objective):
    return {
        "objective": objective,
        "trees": [
            export_node(t["tree_structure"]) for t in estimator.booster_.dump_model()["tree_info"]
        ],
    }


def projection(model, runs, catalog):
    measured = {r["run_id"]: {} for r in runs}
    for row in observations(runs):
        measured[row["run_id"]][row["job"]] = row
    before = after = 0.0
    comparable, missing, missed, observed_failures = 0, 0, 0, 0
    order_before = order_after = 0.0
    failure_cases = 0
    per_run = []
    for run in runs:
        paths = [f["filename"] for f in run["files"]]
        stats = {f["filename"]: f for f in run["files"]}
        baseline = plan.baseline_plan(catalog, paths).run
        proposed, skipped, _, _ = apply(
            model, catalog, paths, stats, run["previous_security"], explanations=False
        )
        jobs = measured[run["run_id"]]
        failures = {name for name, row in jobs.items() if row["failure"]}
        missed += len(failures & set(skipped))
        observed_failures += len(failures)
        # Sum observed job execution seconds, excluding scheduling/runner queue delays.
        # No duration is imputed for a job that was not actually observed.
        required = {"build", "unit", "scans"} | set(baseline) | set(proposed)
        detail = {
            "run_id": run["run_id"],
            "branch": run["branch"],
            "previous_security": run["previous_security"],
            "baseline_optional": baseline,
            "optimizer_optional": proposed,
            "comparable": required <= set(jobs),
            "baseline_job_seconds": None,
            "optimizer_job_seconds": None,
        }
        per_run.append(detail)
        if not required <= set(jobs):
            missing += 1
            continue
        comparable += 1
        mandatory = sum(jobs[j]["duration"] for j in ("build", "unit", "scans"))
        detail["mandatory_job_seconds"] = mandatory
        detail["baseline_job_seconds"] = mandatory + sum(jobs[j]["duration"] for j in baseline)
        detail["optimizer_job_seconds"] = mandatory + sum(jobs[j]["duration"] for j in proposed)
        before += mandatory + sum(jobs[j]["duration"] for j in baseline)
        after += mandatory + sum(jobs[j]["duration"] for j in proposed)
        if failures & set(baseline) and failures & set(proposed):

            def until_failure(order, jobs=jobs, failures=failures):
                elapsed = 0.0
                for job in order:
                    elapsed += jobs[job]["duration"]
                    if job in failures:
                        return elapsed
                return elapsed

            order_before += until_failure(baseline)
            order_after += until_failure(proposed)
            failure_cases += 1
    return {
        "n_commits": len(runs),
        "unknown_prior_baseline_runs": sum(r["previous_security"] == "unknown" for r in runs),
        "per_run": per_run,
        "comparable_complete_runs": comparable,
        "excluded_incomplete_runs": missing,
        "baseline_job_seconds": before,
        "optimizer_job_seconds": after,
        "projected_saving_fraction": (before - after) / before if before else None,
        "observed_failures": observed_failures,
        "observed_failures_skipped": missed,
        "optional_failure_order_cases": failure_cases,
        "baseline_optional_seconds_to_failure": order_before if failure_cases else None,
        "optimizer_optional_seconds_to_failure": order_after if failure_cases else None,
        "limitation": (
            "Execution-time sum projection, not measured wall-clock saving; "
            "unobserved jobs excluded"
        ),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=Path("ci/optimizer/dev-history.jsonl"))
    parser.add_argument("--model", type=Path, default=plan.MODEL_FILE)
    parser.add_argument("--metrics", type=Path, default=Path("ci/optimizer/dev-results.json"))
    args = parser.parse_args(argv)
    runs = [
        json.loads(line) for line in args.history.read_text(encoding="utf-8").splitlines() if line
    ]
    train, validation = split_runs(runs)
    tr, va = observations(train), observations(validation)
    x = np.asarray([r["x"] for r in tr])
    xv = np.asarray([r["x"] for r in va])
    y = np.asarray([r["failure"] for r in tr])
    if len(set(y)) != 2:
        raise ValueError("training needs both successful and failed jobs")
    params = dict(
        n_estimators=40,
        max_depth=2,
        num_leaves=4,
        min_child_samples=2,
        learning_rate=0.1,
        random_state=23,
        n_jobs=1,
        verbosity=-1,
        deterministic=True,
        force_col_wise=True,
    )
    duration = lgb.LGBMRegressor(**params).fit(x, [r["duration"] for r in tr])
    failure = lgb.LGBMClassifier(**params).fit(x, y)
    # Fixed first 16 training observations: the validation set never supplies background.
    background = x[:16]
    support = {job: {"observed": 0, "failures": 0} for job in JOBS}
    for row in tr:
        support[row["job"]]["observed"] += 1
        support[row["job"]]["failures"] += row["failure"]
    model = {
        "format": 2,
        "model": "app-ci-lightgbm-dev-v1",
        "features": list(FEATURES),
        "policy": {"type": "lightgbm-json-v1", "skip_threshold": 0.01, "support": support},
        "duration": export(duration, "regression"),
        "failure": export(failure, "binary"),
        "background": background.tolist(),
        "bounds": {"min": x.min(axis=0).tolist(), "max": x.max(axis=0).tolist()},
        "training": {
            "history_sha256": hashlib.sha256(json.dumps(runs, sort_keys=True).encode()).hexdigest(),
            "split": "first 60% train, last 40% validation, grouped by commit SHA",
            "train_run_ids": [r["run_id"] for r in train],
            "validation_run_ids": [r["run_id"] for r in validation],
            "train_end": train[-1]["created_at"],
            "validation_start": validation[0]["created_at"],
            "seed": 23,
            "lightgbm": "4.6.0",
            "shap": "0.49.1",
        },
    }
    validate(model)
    predictions = np.asarray([predict(model, row) for row in xv])
    np.testing.assert_allclose(predictions[:, 0], np.maximum(0, duration.predict(xv)), atol=1e-9)
    np.testing.assert_allclose(predictions[:, 1], failure.predict_proba(xv)[:, 1], atol=1e-9)
    # Check raw-output additivity and parity with the reference SHAP package.
    for kind, estimator in (("duration", duration), ("failure", failure)):
        reference = shap.TreeExplainer(
            estimator, data=background, feature_perturbation="interventional", model_output="raw"
        )(xv[:3])
        for i, row in enumerate(xv[:3]):
            portable = explain(model[kind], row, model["background"])
            np.testing.assert_allclose(
                list(portable["values"].values()), reference.values[i], atol=1e-5
            )
            np.testing.assert_allclose(portable["base_value"], reference.base_values[i], atol=1e-5)
    yf = [r["failure"] for r in va]
    metrics = {
        "train_commits": len(train),
        "validation_commits": len(validation),
        "train_jobs": len(tr),
        "validation_jobs": len(va),
        "train_failures": int(y.sum()),
        "validation_failures": sum(yf),
        "duration_mae_s": mean_absolute_error([r["duration"] for r in va], predictions[:, 0]),
        "failure_auc": roc_auc_score(yf, predictions[:, 1]) if len(set(yf)) == 2 else None,
        "per_job": {},
        "shap_reference_rows_per_model": min(3, len(va)),
        "dev_validation_projection": projection(
            model, [r for r in validation if r["branch"] == "dev"], plan.load_catalog()
        ),
        "all_validation_projection": projection(model, validation, plan.load_catalog()),
        "fail_fast_validation": failfast_evaluate(model, validation, plan.load_catalog()),
        "fail_fast_dev_validation": failfast_evaluate(
            model, [r for r in validation if r["branch"] == "dev"], plan.load_catalog()
        ),
    }
    for job in JOBS:
        indices = [i for i, row in enumerate(va) if row["job"] == job]
        labels = [yf[i] for i in indices]
        metrics["per_job"][job] = {
            "n": len(indices),
            "failures": sum(labels),
            "mae_s": mean_absolute_error(
                [va[i]["duration"] for i in indices], predictions[indices, 0]
            )
            if indices
            else None,
            "auc": roc_auc_score(labels, predictions[indices, 1])
            if len(set(labels)) == 2
            else None,
        }
    for path, payload in ((args.model, model), (args.metrics, metrics)):
        path.write_bytes((json.dumps(payload, indent=2, allow_nan=False) + "\n").encode())
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
