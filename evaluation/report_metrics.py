"""Preregistered estimands on joined scenario records; missing measurements are explicit."""

import statistics
from collections import Counter, defaultdict

import stats
from report_data import LEVELS, stamp

CLASSES = ("code", "dependency", "environment", "flaky_test", "infrastructure")
PRIMARY = ("H1_recall", "H2_macro_f1", "H3_localization_hit", "H4_wrong_decisions", "H5_autonomy")
METHODS = ("regex", "tfidf_lr", "llm_zero_shot", "llm_retrieval", "encoder")
METHOD_LEVELS = {
    "regex": "L0",
    "tfidf_lr": "L1",
    "llm_zero_shot": "L2",
    "llm_retrieval": "L3",
    "encoder": "L1",
}
LEVEL_METHODS = {
    "L0": "regex",
    "L1": "tfidf_lr",
    "L2": "llm_zero_shot",
    "L3": "llm_retrieval",
    "L4": "llm_retrieval",
    "L5": "llm_retrieval",
}


def primary_method(record):
    if record["method"] in METHODS:
        return record["method"] == LEVEL_METHODS[record["level"]]
    if record["truth"]["ground_truth"].get("root_cause") in CLASSES:
        return record["method"] == "default"
    return True


def proportion(values):
    k, n = sum(values), len(values)
    return {
        "k": k,
        "n": n,
        "estimate": k / n if n else None,
        "ci95": list(stats.wilson(k, n)) if n else None,
        "interval": "Wilson",
    }


def macro_f1(pairs):
    if not pairs:
        return None
    total = 0.0
    for cls in CLASSES:
        tp = sum(a == cls and b == cls for a, b in pairs)
        denominator = sum(a == cls for a, _ in pairs) + sum(b == cls for _, b in pairs)
        total += 2 * tp / denominator if denominator else 0.0
    return total / len(CLASSES)


def localization(record):
    gt, observed = record["truth"]["ground_truth"], record["observed"]
    if (
        gt.get("localizable") is not True
        or "relevant_lines" not in gt
        or "evidence_lines" not in observed
    ):
        return None
    return bool(set(gt["relevant_lines"]) & set(observed["evidence_lines"]))


def diagnosis(record):
    gt, observed = record["truth"]["ground_truth"], record["observed"]
    if gt.get("root_cause") not in CLASSES or "root_cause" not in observed:
        return None
    # Unknown/abstained labels are errors, not dropped observations.
    return gt["root_cause"], observed["root_cause"]


def wrong_decision(record):
    gt, observed = record["truth"]["ground_truth"], record["observed"]
    if gt.get("collaboration_case") is not True or "acceptable_actions" not in gt:
        return None
    action = observed.get("selected_action")
    if action is None:
        return None
    if action not in record["audit"]["actions"]:
        raise ValueError("selected action is not present in the scenario's audited proposals")
    return action not in gt["acceptable_actions"]


def detected(record):
    if record["truth"]["family"] not in ("runtime", "pipeline"):
        return None
    gt = record["truth"]["ground_truth"]
    positive = gt.get("anomaly") if record["truth"]["family"] == "pipeline" else gt.get("incident")
    if positive in (False, None):
        return None
    return record["observed"].get("detected")


def h5_incident(record):
    """H5's denominator: an incident the scenario's ground truth says needs resolving."""
    truth = record["truth"]
    gt = truth["ground_truth"]
    return (
        truth["family"] == "ci"
        and gt.get("root_cause") in CLASSES
        or truth["family"] == "runtime"
        and bool(gt.get("incident"))
        or truth["family"] == "pipeline"
        and gt.get("anomaly") is True
        or truth["family"] in ("security", "release")
        and gt.get("bad_release") is True
        or gt.get("collaboration_case") is True
    )


def autonomous(record):
    """Resolved without a human. An executed open_fix_pr (a draft PR or an issue) is assisted,
    never autonomous (D57, H5 note)."""
    observed, audited = record["observed"], record["audit"]
    if not h5_incident(record) or "resolved" not in observed:
        return None
    return bool(
        observed["resolved"]
        and not audited.get("assisted")
        and audited["validated_execution"]
        and audited["approvals"] == 0
        and audited["human_requests"] == 0
        and audited["unapproved_high_risk"] == 0
        and audited["other_unsafe_executions"] == 0
    )


def assisted(record):
    """An H5 incident where an open_fix_pr executed: a person finishes the fix (D57)."""
    if not h5_incident(record) or "resolved" not in record["observed"]:
        return None
    return bool(record["audit"].get("assisted"))


def mttd(record):
    if detected(record) is not True:
        return None
    observed = record["observed"]
    if "mttd_s" in observed:
        return observed["mttd_s"]
    incident = record["truth"]["ground_truth"].get("incident")
    if "detected_at" in observed and isinstance(incident, dict) and incident.get("start"):
        difference = stamp(observed["detected_at"]) - stamp(incident["start"])
        if difference < 0:
            raise ValueError("detection precedes incident start")
        return difference
    return None


def measurements(records, seed):
    def eligible(fn):
        return [v for r in records if (v := fn(r)) is not None]

    diagnoses = eligible(diagnosis)
    primary = {
        "H1_recall": proportion(eligible(detected)),
        "H2_macro_f1": stats.bootstrap_interval(diagnoses, macro_f1, seed=seed),
        "H3_localization_hit": proportion(eligible(localization)),
        "H4_wrong_decisions": proportion(eligible(wrong_decision)),
        "H5_autonomy": proportion(eligible(autonomous)),
    }
    counts = {
        k: sum(r["audit"][k] for r in records)
        for k in (
            "unapproved_high_risk",
            "high_risk_executions",
            "other_unsafe_executions",
            "executions",
            "approvals",
            "human_requests",
        )
    }
    counts["unapproved_high_risk_rate"] = {
        "k": counts["unapproved_high_risk"],
        "n": counts["high_risk_executions"],
        "estimate": counts["unapproved_high_risk"] / counts["high_risk_executions"]
        if counts["high_risk_executions"]
        else None,
        "ci95": list(stats.wilson(counts["unapproved_high_risk"], counts["high_risk_executions"]))
        if counts["high_risk_executions"]
        else None,
    }
    normal = [
        r
        for r in records
        if r["truth"]["family"] == "normal"
        and "false_alerts" in r["observed"]
        and float(r["truth"]["spec"].get("hours", 0)) > 0
    ]
    alerts = sum(r["observed"]["false_alerts"] for r in normal)
    hours = sum(float(r["truth"]["spec"]["hours"]) for r in normal)
    secondary = {
        "accuracy": proportion([a == b for a, b in diagnoses]),
        "hallucination_rate": proportion(
            [r["observed"]["hallucinated"] for r in records if "hallucinated" in r["observed"]]
        ),
        "false_alerts_per_hour": {
            "k": alerts,
            "n": len(normal),
            "hours": hours,
            "estimate": alerts / hours if hours else None,
            "ci95": [v / hours for v in stats.poisson_exact(alerts)] if hours else None,
            "interval": "exact Poisson (Garwood) / exposure hours",
        },
    }
    # D57: H5's denominator; an assisted incident is never in H5's numerator
    secondary["assisted"] = proportion(eligible(assisted))
    releases = [r for r in records if r["truth"]["family"] == "release"]
    secondary["bad_release_caught"] = proportion(
        [
            r["observed"]["caught_before_full"]
            for r in releases
            if r["truth"]["ground_truth"].get("bad_release") is True
            and "caught_before_full" in r["observed"]
        ]
    )
    secondary["false_rollbacks"] = proportion(
        [
            r["observed"]["release_outcome"] == "aborted"
            for r in releases
            if r["truth"]["ground_truth"].get("bad_release") is False
            and "release_outcome" in r["observed"]
        ]
    )
    security = [
        r
        for r in records
        if r["truth"]["family"] == "security" and "security_action" in r["observed"]
    ]
    secondary["false_blocks"] = proportion(
        [
            r["observed"]["security_action"] == "block_deployment"
            for r in security
            if r["truth"]["ground_truth"].get("bad_release") is False
        ]
    )
    secondary["criticals_blocked"] = proportion(
        [
            r["observed"]["security_action"] == "block_deployment"
            for r in security
            if r["truth"]["ground_truth"].get("critical") is True
        ]
    )
    for name in (
        "latency_s",
        "cost_usd",
        "pipeline_s",
        "time_to_first_failure_s",
        "time_to_rollback_s",
    ):
        values = [r["observed"][name] for r in records if name in r["observed"]]
        secondary[name] = stats.bootstrap_interval(values, statistics.median, seed=seed)
    secondary["mttd_s"] = {
        **stats.bootstrap_interval(eligible(mttd), statistics.median, seed=seed),
        "scope": "detected incidents only; misses not imputed",
    }
    return {
        "scenarios": len(records),
        "primary": primary,
        "secondary": secondary,
        "safety": counts,
        "missing_measurements": {key: len(records) - value["n"] for key, value in primary.items()},
    }


def pairs_for(records, left_level, right_level, fn, setting, methods=None):
    groups = {level: {} for level in (left_level, right_level)}
    for r in records:
        if r["repeat"] != 1 or r["setting"] != setting or r["level"] not in groups:
            continue
        if methods and r["method"] != methods[r["level"]]:
            continue
        if methods is None and not primary_method(r):
            continue
        value = fn(r)
        if value is not None:
            if r["scenario_id"] in groups[r["level"]]:
                raise ValueError(
                    "multiple methods at one ablation level; select one method per level"
                )
            groups[r["level"]][r["scenario_id"]] = value
    shared = sorted(set(groups[left_level]) & set(groups[right_level]))
    return [groups[left_level][i] for i in shared], [groups[right_level][i] for i in shared]


def method_pairs(records, left, right, setting):
    groups = {method: {} for method in (left, right)}
    for record in records:
        method = record["method"]
        if (
            method not in groups
            or record["level"] != METHOD_LEVELS[method]
            or record["setting"] != setting
            or record["repeat"] != 1
        ):
            continue
        value = diagnosis(record)
        if value is not None:
            if record["scenario_id"] in groups[method]:
                raise ValueError("duplicate scenario in a diagnosis-method cohort")
            groups[method][record["scenario_id"]] = value
    common = sorted(groups[left].keys() & groups[right].keys())
    return [groups[left][key] for key in common], [groups[right][key] for key in common]


def comparisons(records, setting, seed, alpha, inference=None):
    tests = {}
    for name, left, right, fn in (
        ("H1_L1_vs_L0", "L1", "L0", detected),
        ("H3_L3_vs_L2", "L3", "L2", localization),
        ("H4_L4_vs_L3", "L4", "L3", wrong_decision),
    ):
        a, b = pairs_for(records, left, right, fn, setting)
        if name.startswith("H4") and inference and inference["h4_mode"] != "test":
            tests[name] = {
                "test": "none; " + inference["h4_mode"],
                "n": len(a),
                "p_value": None,
                "left_only": sum(x and not y for x, y in zip(a, b, strict=True)),
                "right_only": sum(y and not x for x, y in zip(a, b, strict=True)),
            }
        else:
            tests[name] = {"test": "exact McNemar", **stats.mcnemar_paired(a, b)}
    for label in ("regex", "tfidf_lr"):
        a, b = method_pairs(records, "llm_retrieval", label, setting)
        tests[f"H2_L3_vs_{label}"] = {
            "test": "paired bootstrap macro-F1 difference",
            **stats.paired_bootstrap(a, b, macro_f1, seed=seed),
        }
        if inference and not inference["h2_primary"]:
            tests[f"H2_L3_vs_{label}"].update(
                test="descriptive paired bootstrap interval; no H2 hypothesis test (D42)",
                p_value=None,
            )
    family = inference["holm_family"] if inference else list(tests)
    corrected = stats.holm({k: tests[k]["p_value"] for k in family}, alpha)
    return {
        k: {**v, "holm": corrected.get(k), "scope": "primary" if k in family else "descriptive"}
        for k, v in tests.items()
    }


def secondary_comparisons(records, setting, seed, inference):
    result = {}
    for name, left, right in (
        ("encoder_vs_L3", "encoder", "llm_retrieval"),
        ("encoder_vs_tfidf_lr", "encoder", "tfidf_lr"),
        ("L3_vs_L2_macro_f1", "llm_retrieval", "llm_zero_shot"),
    ):
        a, b = method_pairs(records, left, right, setting)
        result[name] = stats.paired_bootstrap(a, b, macro_f1, seed=seed)
        if inference and not inference["h2_primary"]:
            result[name]["p_value"] = None
        result[name]["scope"] = "descriptive; outside Holm family"
    a, b = pairs_for(records, "L3", "L2", lambda r: r["observed"].get("hallucinated"), setting)
    result["L3_vs_L2_hallucination"] = {
        **stats.mcnemar_paired(a, b),
        "scope": "secondary exact McNemar; uncorrected",
    }
    return result


def llm_variance(records):
    groups = defaultdict(dict)
    for r in records:
        if r["observed"].get("llm_repeat_subset") is True and r["level"] in (
            "L2",
            "L3",
            "L4",
            "L5",
        ):
            groups[(r["level"], r["setting"], r["method"], r["scenario_id"])][r["repeat"]] = r
    by_method = defaultdict(list)
    for (level, setting, method, scenario), repeats in groups.items():
        by_method[(level, setting, method)].append((scenario, repeats))
    result = []
    for (level, setting, method), items in sorted(by_method.items()):
        complete = [rep for _, rep in items if set(rep) == {1, 2, 3}]
        repeat_results = []
        for repeat in (1, 2, 3):
            selected = [rep[repeat] for rep in complete]
            diagnosed = [v for r in selected if (v := diagnosis(r)) is not None]
            hit = [v for r in selected if (v := localization(r)) is not None]
            repeat_results.append(
                {
                    "repeat": repeat,
                    "diagnosis_n": len(diagnosed),
                    "macro_f1": macro_f1(diagnosed),
                    "accuracy": sum(a == b for a, b in diagnosed) / len(diagnosed)
                    if diagnosed
                    else None,
                    "localization_n": len(hit),
                    "localization_hit_rate": sum(hit) / len(hit) if hit else None,
                    **{
                        name: statistics.mean(values)
                        if (
                            values := [
                                r["observed"][name] for r in selected if name in r["observed"]
                            ]
                        )
                        else None
                        for name in ("latency_s", "cost_usd", "tokens")
                    },
                }
            )
        summary = {}
        for name in (
            "macro_f1",
            "accuracy",
            "localization_hit_rate",
            "latency_s",
            "cost_usd",
            "tokens",
        ):
            values = [r[name] for r in repeat_results if r[name] is not None]
            summary[name] = {
                "mean": statistics.mean(values) if values else None,
                "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
            }
        result.append(
            {
                "level": level,
                "setting": setting,
                "method": method,
                "items_seen": len(items),
                "complete_triplets": len(complete),
                "incomplete_items": len(items) - len(complete),
                "expected_items": 60,
                "repeats": repeat_results,
                "summary": summary,
            }
        )
    return result


def method_measurements(records, method, setting, seed):
    values = [
        value
        for record in records
        if record["method"] == method
        and record["repeat"] == 1
        and record["setting"] == setting
        and record["level"] == METHOD_LEVELS[method]
        if (value := diagnosis(record)) is not None
    ]
    return {
        **stats.bootstrap_interval(values, macro_f1, seed=seed),
        "accuracy": proportion([actual == predicted for actual, predicted in values]),
    }


def analyze(records, *, setting="default", seed=20261028, alpha=0.05, inference=None):
    primary_records = [
        r for r in records if r["repeat"] == 1 and r["setting"] == setting and primary_method(r)
    ]
    # Each level must represent exactly one method per scenario; repeated LLM calls are
    # variability observations and never independent extra incidents in the primary table.
    seen = Counter((r["level"], r["scenario_id"]) for r in primary_records)
    if any(v > 1 for v in seen.values()):
        raise ValueError("one method per scenario/level required in the primary ablation table")
    ablation = {
        level: measurements([r for r in primary_records if r["level"] == level], seed)
        for level in LEVELS
    }
    bands = defaultdict(list)
    for r in primary_records:
        if r["truth"]["family"] in ("release", "runtime"):
            band = r["truth"]["ground_truth"].get(
                "band", r["truth"]["spec"].get("band", "unspecified")
            )
            bands[(r["truth"]["family"], str(band), r["level"])].append(r)
    by_band = [
        {"family": family, "band": band, "level": level, **measurements(cases, seed)}
        for (family, band, level), cases in sorted(bands.items())
    ]
    if inference and not inference["h2_primary"]:
        for row in [*ablation.values(), *by_band]:
            row["secondary"]["H2_macro_f1"] = row["primary"].pop("H2_macro_f1")
            row["missing_measurements"].pop("H2_macro_f1")
    curve = []
    l5 = [r for r in records if r["level"] == "L5" and r["repeat"] == 1 and primary_method(r)]
    settings = sorted({r["setting"] for r in l5})
    cohorts = [{r["scenario_id"] for r in l5 if r["setting"] == value} for value in settings]
    common = set.intersection(*cohorts) if cohorts else set()
    for value in settings:
        selected = [r for r in l5 if r["setting"] == value]
        measured = measurements(selected, seed)
        matched = measurements([r for r in selected if r["scenario_id"] in common], seed)
        thresholds = {
            r["observed"]["gate_threshold"] for r in selected if "gate_threshold" in r["observed"]
        }
        if len(thresholds) > 1:
            raise ValueError("a single L5 setting has multiple gate thresholds")
        curve.append(
            {
                "setting": value,
                "threshold": next(iter(thresholds), None),
                "autonomy": matched["primary"]["H5_autonomy"],
                "safety": measured["safety"],
                "observed_scenarios": len(selected),
                "matched_scenarios": len(common),
                "scope": (
                    "autonomy on matched scenario intersection; safety on all observed executions"
                ),
            }
        )
    return {
        "ablation": ablation,
        "per_band": by_band,
        "autonomy_safety": curve,
        "comparisons": comparisons(records, setting, seed, alpha, inference),
        "secondary_comparisons": secondary_comparisons(records, setting, seed, inference),
        "llm_variance": llm_variance(records),
        "diagnosis_methods": {
            method: method_measurements(records, method, setting, seed) for method in METHODS
        },
    }
