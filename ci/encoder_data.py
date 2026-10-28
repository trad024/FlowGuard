"""Strict metadata allowlist before opening any training or validation log."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from flowguard.encoder import CLASSES, prepare_text


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Example:
    variant: str
    split: str
    text: str
    label: int
    sha256: str


def load_examples(split_path: Path, index_path: Path, log_root: Path, expected_hash=None):
    record = json.loads(split_path.read_text(encoding="utf-8"))
    if expected_hash is not None and canonical_hash(record) != expected_hash:
        raise ValueError("recorded split checksum mismatch")
    allowed = {}
    unverified = set(record.get("unverified", []))
    for variant, meta in record["variants"].items():
        split = record["templates"].get(meta["template"])
        if split in ("train", "validation") and variant not in unverified:
            allowed[variant] = (split, CLASSES.index(meta["root_cause"]))
    root = log_root.resolve(strict=True)
    examples, seen = [], set()
    rows = [
        json.loads(line) for line in index_path.read_text(encoding="utf-8").splitlines() if line
    ]
    # Validate the entire metadata index before opening even one log. No inline log content.
    for row in rows:
        if set(row) != {"variant_id", "log_path"}:
            raise ValueError("index rows require only variant_id and log_path")
        variant = row["variant_id"]
        if variant not in allowed or variant in seen:
            raise ValueError("excluded, unverified, unknown or duplicate variant")
        seen.add(variant)
        if row["log_path"] not in (f"{variant}/reduced.log", f"{variant}.log"):
            raise ValueError("log path must identify its recorded variant")
        path = (root / row["log_path"]).resolve()
        if (
            not path.is_relative_to(root)
            or path != root / row["log_path"]
            or path.suffix not in (".log", ".txt")
        ):
            raise ValueError("log path outside the declared root or unsupported file type")
    for row in rows:
        variant = row["variant_id"]
        path = (root / row["log_path"]).resolve()
        data = path.read_bytes()
        split, label = allowed[variant]
        examples.append(
            Example(
                variant,
                split,
                prepare_text(data.decode("utf-8")),
                label,
                hashlib.sha256(data).hexdigest(),
            )
        )
    return examples


def classification_metrics(labels, predicted):
    if len(labels) != len(predicted) or not labels:
        raise ValueError("nonempty aligned predictions required")
    if any(type(i) is not int or not 0 <= i < len(CLASSES) for i in [*labels, *predicted]):
        raise ValueError("invalid class index")
    result = {"n": len(labels), "per_class": {}}
    for i, name in enumerate(CLASSES):
        tp = sum(a == i and b == i for a, b in zip(labels, predicted, strict=True))
        actual, called = labels.count(i), predicted.count(i)
        precision, recall = tp / called if called else 0.0, tp / actual if actual else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        result["per_class"][name] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": actual,
        }
    result["macro_f1"] = sum(v["f1"] for v in result["per_class"].values()) / len(CLASSES)
    result["correct"] = sum(a == b for a, b in zip(labels, predicted, strict=True))
    result["accuracy"] = result["correct"] / len(labels)
    return result
