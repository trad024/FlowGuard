"""Inferential scope from the supplied read-only pre-registration, including D42 and D44."""

import math
from datetime import date

H1, H3, H4 = "H1_L1_vs_L0", "H3_L3_vs_L2", "H4_L4_vs_L3"
H2 = ("H2_L3_vs_regex", "H2_L3_vs_tfidf_lr")


def declared_counts(specification):
    counts = {}

    def walk(value, path=""):
        if isinstance(value, dict):
            for key, item in value.items():
                key = str(key)
                name = f"{path}.{key}" if path else key
                if key in ("count", "size", "n") or key.endswith(("_count", "_size")):
                    if item is not None and (type(item) is not int or item < 0):
                        raise ValueError(
                            f"pre-registered count must be a nonnegative integer: {name}"
                        )
                    counts[name] = item
                else:
                    walk(item, name)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(specification)
    if str(specification["version"]) == "0.5":
        for path in (
            "families.release.count",
            "families.runtime.count",
            "families.ci.count",
            "families.security.count",
            "families.pipeline.count",
            "dso5_commit_sequence.count",
            "dso6_adversarial_set.count",
            "external.logchunks.sample.size",
        ):
            counts.setdefault(path, None)
    return counts


def policy(specification):
    version = str(specification["version"])
    if version not in ("0.2", "0.3", "0.5"):
        raise ValueError("unsupported pre-registration version; review its inference rules first")
    primary = specification["comparisons"]["H2"].get("primary")
    h2_primary = primary is not None and primary != "none" and primary != []
    h4_mode, rate, amendment_date = "test", None, None
    if version == "0.5":
        if h2_primary:
            raise ValueError("pre-registration 0.5 requires secondary H2")
        h4_mode = "descriptive"
    if version == "0.3":
        h4_mode = "pending_amendment"
        for amendment in specification.get("amendments", []):
            if not isinstance(amendment, dict):
                continue
            entry = amendment.get("H4")
            if not isinstance(entry, dict) or "dev_wrong_rate" not in entry:
                continue
            rate = entry["dev_wrong_rate"]
            if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 <= rate <= 1:
                raise ValueError("H4 amendment requires a finite development rate in [0,1]")
            if not isinstance(amendment.get("date"), str):
                raise ValueError("H4 development-rate amendment must be dated")
            amendment_date = date.fromisoformat(amendment["date"]).isoformat()
            h4_mode = "descriptive" if rate < 0.10 else "test"
    family = [H1, H3]
    if h2_primary:
        family.extend(H2)
    if h4_mode != "descriptive":
        family.append(H4)  # Pending H4 stays in the family size, with no p-value.
    counts = declared_counts(specification)
    return {
        "preregistration_version": version,
        "h2_primary": h2_primary,
        "h4_mode": h4_mode,
        "h4_development_wrong_rate": rate,
        "h4_amendment_date": amendment_date,
        "holm_family": family,
        "preregistered_counts": counts,
        "unset_counts": sorted(path for path, count in counts.items() if count is None),
        "note": (
            "D56: H4 is descriptive; Holm covers H1 and H3 only; no D44 count amendment required"
            if version == "0.5"
            else "D44: H4 waits for a dated development-rate amendment; never infer its mode "
            "from held-out outcomes"
            if h4_mode == "pending_amendment"
            else "H4 is descriptive below 10% development wrong decisions"
            if h4_mode == "descriptive"
            else "H4 exact McNemar eligible; missing paired observations still yield no p-value"
        ),
    }
