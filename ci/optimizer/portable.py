"""Validated numeric LightGBM trees and exact interventional SHAP without dependencies.

Depth-two trees involve at most three distinct features. Enumerating those features gives
exact Shapley values cheaply; unused features have zero contribution. Background rows are
training-only observations and the explanations are in raw model output units.
"""

import itertools
import math

from optimizer.features import FEATURES, JOBS


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_node(node, depth=0):
    if not isinstance(node, dict) or depth > 2:
        raise ValueError("invalid or overly deep tree")
    if set(node) == {"value"}:
        if not finite(node["value"]):
            raise ValueError("invalid leaf")
        return
    if set(node) != {"feature", "threshold", "left", "right"}:
        raise ValueError("invalid split schema")
    if type(node["feature"]) is not int or not 0 <= node["feature"] < len(FEATURES):
        raise ValueError("invalid feature index")
    if not finite(node["threshold"]):
        raise ValueError("invalid split threshold")
    validate_node(node["left"], depth + 1)
    validate_node(node["right"], depth + 1)


def validate(model):
    if model["format"] != 2 or model["policy"]["type"] != "lightgbm-json-v1":
        raise ValueError("unsupported model format")
    if model["features"] != list(FEATURES):
        raise ValueError("feature schema mismatch")
    if not isinstance(model["model"], str):
        raise ValueError("invalid model identity")
    for kind in ("duration", "failure"):
        forest = model[kind]
        if forest["objective"] != {"duration": "regression", "failure": "binary"}[kind]:
            raise ValueError("invalid model objective")
        if not isinstance(forest["trees"], list) or not 1 <= len(forest["trees"]) <= 64:
            raise ValueError("invalid forest size")
        for tree in forest["trees"]:
            validate_node(tree)
    bg = model["background"]
    if not isinstance(bg, list) or not 1 <= len(bg) <= 32:
        raise ValueError("invalid SHAP background")
    if any(not isinstance(r, list) or len(r) != len(FEATURES) for r in bg):
        raise ValueError("invalid SHAP background shape")
    if any(not finite(v) for r in bg for v in r):
        raise ValueError("invalid SHAP background values")
    policy = model["policy"]
    if not finite(policy["skip_threshold"]) or not 0 <= policy["skip_threshold"] <= 0.01:
        raise ValueError("invalid skipping threshold")
    if set(policy["support"]) != set(JOBS):
        raise ValueError("missing job support")
    for counts in policy["support"].values():
        if any(type(counts[k]) is not int or counts[k] < 0 for k in ("observed", "failures")):
            raise ValueError("invalid job support")
        if counts["failures"] > counts["observed"]:
            raise ValueError("invalid job failure count")
    bounds = model["bounds"]
    if set(bounds) != {"min", "max"} or any(len(bounds[k]) != len(FEATURES) for k in bounds):
        raise ValueError("invalid feature bounds")
    for lo, hi in zip(bounds["min"], bounds["max"], strict=True):
        if not finite(lo) or not finite(hi) or lo > hi:
            raise ValueError("invalid feature range")


def tree_value(node, row):
    while "value" not in node:
        node = node["left"] if row[node["feature"]] <= node["threshold"] else node["right"]
    return node["value"]


def raw(forest, row):
    value = sum(tree_value(tree, row) for tree in forest["trees"])
    if not math.isfinite(value):
        raise ValueError("prediction overflow")
    return value


def predict(model, row):
    duration = max(0.0, raw(model["duration"], row))
    logit = raw(model["failure"], row)
    probability = 1 / (1 + math.exp(-max(-700, min(700, logit))))
    return duration, probability


def used_features(node):
    if "value" in node:
        return set()
    return {node["feature"]} | used_features(node["left"]) | used_features(node["right"])


def explain(forest, row, background):
    contributions = [0.0] * len(FEATURES)
    base = 0.0
    for tree in forest["trees"]:
        indices = sorted(used_features(tree))
        n = len(indices)
        values = {}
        for size in range(n + 1):
            for subset in itertools.combinations(indices, size):
                total = 0.0
                for reference in background:
                    mixed = list(reference)
                    for index in subset:
                        mixed[index] = row[index]
                    total += tree_value(tree, mixed)
                values[frozenset(subset)] = total / len(background)
        base += values[frozenset()]
        for index in indices:
            others = [i for i in indices if i != index]
            for size in range(n):
                weight = math.factorial(size) * math.factorial(n - size - 1) / math.factorial(n)
                for subset in itertools.combinations(others, size):
                    key = frozenset(subset)
                    contributions[index] += weight * (values[key | {index}] - values[key])
    order = sorted(range(len(FEATURES)), key=lambda i: abs(contributions[i]), reverse=True)[:3]
    return {
        "method": "interventional_tree_shap",
        "space": "log_odds" if forest["objective"] == "binary" else "seconds",
        "base_value": base,
        "values": dict(zip(FEATURES, contributions, strict=True)),
        "top_features": [{"feature": FEATURES[i], "value": contributions[i]} for i in order],
    }
