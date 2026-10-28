"""Offline final-evaluation tables, paired tests and figures. Never invokes an agent or network.

    python evaluation/report.py --out results/

Default inputs are state/audit.db and evaluation/runs/groundtruth.db. Replays are structured
replay_result audit events, or an optional --replays JSONL. --demo uses synthetic databases
only and never opens the default databases.
"""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from report_data import load  # noqa: E402
from report_metrics import analyze  # noqa: E402
from report_policy import policy  # noqa: E402


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def flatten(row, metric, value):
    return {
        **row,
        "metric": metric,
        "n": value["n"],
        "k": value.get("k"),
        "estimate": value["estimate"],
        "ci_low": value["ci95"][0] if value["ci95"] else None,
        "ci_high": value["ci95"][1] if value["ci95"] else None,
    }


def build(
    audit,
    truth,
    out,
    prereg=ROOT / "docs/preregistration.yaml",
    replays=None,
    setting="default",
    seed=20261028,
    make_plots=True,
    synthetic=False,
):
    specification = yaml.safe_load(prereg.read_text(encoding="utf-8"))
    if specification["comparisons"]["H1"]["levels"] != ["L0", "L1"]:
        raise ValueError("unsupported preregistration comparison; update the adapter explicitly")
    if specification["comparisons"]["H3"]["levels"] != ["L2", "L3"] or specification["comparisons"][
        "H4"
    ]["levels"] != ["L3", "L4"]:
        raise ValueError("unsupported preregistration comparisons")
    joined, coverage = load(audit, truth, replays)
    inference = policy(specification)
    result = analyze(
        joined, setting=setting, seed=seed, alpha=specification["alpha"], inference=inference
    )
    primary_metrics = list(result["ablation"]["L0"]["primary"])
    result["metadata"] = {
        "format": 1,
        "synthetic": synthetic,
        "preregistration_status": specification["status"],
        "preregistration_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(),
        "primary_metric_definitions": specification["metrics"],
        "amendments": specification["amendments"],
        "bootstrap_resamples": 10000,
        "bootstrap_seed": seed,
        "alpha": specification["alpha"],
        "primary_setting": setting,
        "coverage": coverage,
        "inputs_sha256": {
            "audit": hashlib.sha256(Path(audit).read_bytes()).hexdigest(),
            "truth": hashlib.sha256(Path(truth).read_bytes()).hexdigest(),
            "replays": hashlib.sha256(Path(replays).read_bytes()).hexdigest() if replays else None,
        },
        "scope": "synthetic demonstration" if synthetic else "offline supplied results",
        "confirmatory": (
            not synthetic
            and specification["status"] == "frozen"
            and inference["h4_mode"] != "pending_amendment"
        ),
        "pairing": "intersection of scenario IDs; repeat 1 only; fixed setting",
        "holm_family": inference["holm_family"],
        "primary_metrics": primary_metrics,
        "analysis_policy": inference,
    }
    result["time_comparisons"] = {}
    import statistics

    import stats
    from report_metrics import mttd, pairs_for

    for name in (
        "pipeline_s",
        "mttd_s",
        "time_to_first_failure_s",
        "latency_s",
        "time_to_rollback_s",
    ):
        for level in ("L1", "L2", "L3", "L4", "L5"):
            a, b = pairs_for(
                joined,
                level,
                "L0",
                lambda r, key=name: mttd(r) if key == "mttd_s" else r["observed"].get(key),
                setting,
            )
            result["time_comparisons"][f"{name}_{level}_minus_L0"] = {
                **stats.paired_bootstrap(a, b, statistics.median, seed=seed),
                "multiplicity": "descriptive; not part of H1–H4 Holm family",
            }
    out.mkdir(parents=True, exist_ok=True)
    columns = ["level", "metric", "n", "k", "estimate", "ci_low", "ci_high"]
    write_csv(
        out / "ablation.csv",
        [
            flatten({"level": level}, metric, values["primary"][metric])
            for level, values in result["ablation"].items()
            for metric in primary_metrics
        ],
        columns,
    )
    write_csv(
        out / "per-band.csv",
        [
            flatten({k: row[k] for k in ("family", "band", "level")}, metric, value)
            for row in result["per_band"]
            for group in ("primary", "secondary")
            for metric, value in row[group].items()
            if "n" in value
        ],
        ["family", "band", *columns],
    )
    write_csv(
        out / "diagnosis-methods.csv",
        [
            flatten({"method": method}, metric, value)
            for method, row in result["diagnosis_methods"].items()
            for metric, value in (("macro_f1", row), ("accuracy", row["accuracy"]))
        ],
        ["method", *columns[1:]],
    )
    write_csv(
        out / "autonomy-safety.csv",
        [
            {
                "setting": row["setting"],
                "threshold": row["threshold"],
                "n": row["autonomy"]["n"],
                "autonomy": row["autonomy"]["estimate"],
                "ci_low": row["autonomy"]["ci95"][0] if row["autonomy"]["ci95"] else None,
                "ci_high": row["autonomy"]["ci95"][1] if row["autonomy"]["ci95"] else None,
                "unapproved_high_risk": row["safety"]["unapproved_high_risk"],
                "high_risk_executions": row["safety"]["high_risk_executions"],
                "human_requests": row["safety"]["human_requests"],
            }
            for row in result["autonomy_safety"]
        ],
        [
            "setting",
            "threshold",
            "n",
            "autonomy",
            "ci_low",
            "ci_high",
            "unapproved_high_risk",
            "high_risk_executions",
            "human_requests",
        ],
    )
    write_csv(
        out / "llm-variance.csv",
        [
            {
                **{
                    k: row[k]
                    for k in (
                        "level",
                        "setting",
                        "method",
                        "items_seen",
                        "complete_triplets",
                        "incomplete_items",
                        "expected_items",
                    )
                },
                "metric": metric,
                **values,
            }
            for row in result["llm_variance"]
            for metric, values in row["summary"].items()
        ],
        [
            "level",
            "setting",
            "method",
            "items_seen",
            "complete_triplets",
            "incomplete_items",
            "expected_items",
            "metric",
            "mean",
            "sample_sd",
        ],
    )
    result["figures"] = []
    if make_plots:
        from report_plots import plots

        result["figures"] = plots(result, out)
    (out / "results.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (out / "README.md").write_text(
        "# Offline report\n\n"
        + (
            "SYNTHETIC DATA ONLY. These are not project evaluation results.\n\n"
            if synthetic
            else ""
        )
        + f"Pre-registration status: {specification['status']}. "
        + f"Confirmatory: {result['metadata']['confirmatory']}.\n\n"
        + "Missing measurements have n=0 and null estimates/intervals, never fabricated zeros. "
        "Only repeat 1 enters primary tables. LLM variance uses complete 1/2/3 triplets. "
        "Comparisons retain matched scenario pairs; the pre-registration sets the Holm family. "
        + inference["note"]
        + ". "
        + (
            "H2 is secondary: macro-F1 bootstrap and accuracy Wilson intervals; no H2 test. "
            if not inference["h2_primary"]
            else ""
        )
        + "Per-band and time comparisons are descriptive. "
        "See results.json for coverage and counts.\n",
        encoding="utf-8",
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audit", type=Path, default=ROOT / "state/audit.db")
    parser.add_argument("--truth", type=Path, default=ROOT / "evaluation/runs/groundtruth.db")
    parser.add_argument("--replays", type=Path)
    parser.add_argument("--preregistration", type=Path, default=ROOT / "docs/preregistration.yaml")
    parser.add_argument("--out", type=Path, default=ROOT / "results")
    parser.add_argument("--l5-setting", default="default")
    parser.add_argument("--seed", type=int, default=20261028)
    parser.add_argument("--no-plots", action="store_true")
    parser.add_argument(
        "--demo", action="store_true", help="generate synthetic fixtures, never open default inputs"
    )
    args = parser.parse_args(argv)
    if args.demo:
        from report_demo import create

        args.audit, args.truth = create(args.out / "synthetic-inputs")
        args.replays = None
    try:
        result = build(
            args.audit,
            args.truth,
            args.out,
            args.preregistration,
            args.replays,
            args.l5_setting,
            args.seed,
            not args.no_plots,
            args.demo,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"report: {exc}\n")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "coverage": result["metadata"]["coverage"],
                "synthetic": args.demo,
                "figures": result["figures"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
