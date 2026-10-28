"""Planning policy: model failures run all checks, P2 overrides predicted skips."""

import fnmatch

from optimizer.features import FEATURES, JOBS, dependencies_changed, extract
from optimizer.portable import explain, predict, validate

MANDATORY = frozenset({"build", "unit", "scan", "scans"})


def apply(model, catalog, changed, stats=None, security_status="unknown", explanations=True):
    validate(model)
    order = catalog["order"]
    if set(order) & MANDATORY or not set(order) <= set(JOBS):
        raise ValueError("invalid optional job catalog")
    if security_status not in ("clean", "flagged", "unknown"):
        raise ValueError("invalid previous security status")
    if changed is None:
        return list(order), {}, {}, ["changes unknown; ran all"]
    predictions, vectors = {}, {}
    for job in JOBS:
        row = extract(changed, job, stats)
        vectors[job] = row
        duration, failure = predict(model, row)
        predictions[job] = {"duration_s": duration, "failure_probability": failure}
        if explanations:
            predictions[job]["duration_shap"] = explain(model["duration"], row, model["background"])
            predictions[job]["failure_shap"] = explain(model["failure"], row, model["background"])
    if security_status == "unknown":
        return list(order), {}, predictions, ["previous security status unknown; ran all"]
    forced = {"sbom"} if dependencies_changed(changed) or security_status == "flagged" else set()
    skipped = {}
    for job in order:
        if job in forced:
            continue
        relevant = any(
            fnmatch.fnmatchcase(path, pattern)
            for path in changed
            for pattern in catalog["jobs"][job]["paths"]
        )
        if not relevant:
            skipped[job] = "no changed file matches its paths"
            continue
        support = model["policy"]["support"][job]
        row = vectors[job]
        within = all(
            lo <= v <= hi
            for lo, v, hi in zip(model["bounds"]["min"], row, model["bounds"]["max"], strict=True)
        )
        # Insufficient support, unseen feature ranges and historical failures disable
        # predictive skipping. Probability alone cannot remove a mandatory or forced job.
        if (
            within
            and row[FEATURES.index("size_known")] == 1
            and support["observed"] >= 8
            and support["failures"] == 0
            and predictions[job]["failure_probability"] <= model["policy"]["skip_threshold"]
        ):
            skipped[job] = "low predicted failure probability within observed training range"
    run = [job for job in order if job not in skipped]
    # For sequential optional jobs p(failure)/duration minimizes expected time to first
    # failure under the independent-job approximation. Ties keep the baseline order.
    run.sort(
        key=lambda job: (
            -predictions[job]["failure_probability"] / max(predictions[job]["duration_s"], 1.0)
        )
    )
    return run, skipped, predictions, []
